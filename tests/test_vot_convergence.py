#!/usr/bin/env python3
"""Test: mock returns stubs for N calls, then real audio — proves convergence."""
import asyncio, json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, "/home/ilyah/video_translate_tg")
from app.config import VotSettings
from app.pipeline.vot import VotClient
from app.utils.urls import parse_video_ref

MOCK_BINARY = "/home/ilyah/.local/bin/vot-cli-mock"
REAL_AUDIO = "/tmp/real_audio.mp3"
COUNT_FILE = "/tmp/vot_mock_convergence_count"

os.environ["VOT_MOCK_STUBS"] = "3"       # first 3 calls = stubs
os.environ["VOT_MOCK_COUNT_FILE"] = COUNT_FILE
os.environ["VOT_MOCK_REAL_AUDIO"] = REAL_AUDIO
os.environ["VOT_MOCK_DELAY"] = "0.3"

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


async def main():
    print("=" * 70)
    print(YELLOW + "CONVERGENCE TEST: 3 stubs + 1 real audio → success after polling" + RESET)
    print("=" * 70)

    # Reset count
    Path(COUNT_FILE).write_text("0")

    assert Path(REAL_AUDIO).is_file(), "Real audio not found at {}".format(REAL_AUDIO)
    # Verify real audio is valid MP3
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", REAL_AUDIO],
        capture_output=True, text=True, timeout=10,
    )
    real_dur = float(r.stdout.strip())
    print("  Real audio duration: {:.1f}s".format(real_dur))
    assert real_dur > 100, "Real audio too short"

    settings = VotSettings(
        binary=MOCK_BINARY,
        flavor="live",
        lively_voice=True,
        backoff_start_sec=1,      # fast delays for test
        backoff_factor=1.3,
        backoff_max_sec=5,
        backoff_jitter_sec=0,
        attempt_timeout_sec=15,
        total_timeout_sec=60,
    )

    ref = parse_video_ref("https://youtube.com/watch?v=TESTCONV")
    wd = Path(tempfile.mkdtemp(prefix="vot_conv_"))
    client = VotClient(settings)

    try:
        t0 = time.monotonic()

        # Progress callback to show delays
        delays = []
        async def progress(msg):
            if "следующая через" in msg:
                # Extract delay from message: "...следующая через 1 с"
                parts = msg.split("через ")
                if len(parts) > 1:
                    delay_str = parts[1].split(" ")[0]
                    try:
                        d = int(delay_str)
                        delays.append(d)
                    except ValueError:
                        pass
            print("  [progress] {}".format(msg))

        artifact = await client.get_audio(
            ref, wd,
            expected_duration_sec=real_dur,
            progress=progress,
        )
        elapsed = time.monotonic() - t0

        print("\n  Attempts: {}".format(artifact.attempts))
        print("  Total time: {:.1f}s".format(elapsed))
        print("  File size: {} KB".format(artifact.size_bytes // 1024))
        print("  Delays between attempts: {}".format(delays))

        # Verify with ffprobe
        info = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(artifact.path)],
            capture_output=True, text=True, timeout=10,
        ).stdout)

        audio_streams = [s for s in info["streams"] if s.get("codec_type") == "audio"]
        dur = float(info["format"].get("duration", 0))
        print("  ffprobe codec: {}".format(audio_streams[0]["codec_name"]))
        print("  ffprobe duration: {:.1f}s".format(dur))

        # Assertions
        assert artifact.attempts >= 4, \
            "Expected >=4 attempts (3 stubs + 1 real), got {}".format(artifact.attempts)
        assert abs(dur - real_dur) / real_dur < 0.1, \
            "Duration mismatch: {:.1f} vs {:.1f}".format(dur, real_dur)
        assert artifact.size_bytes > 500_000, \
            "File too small: {} bytes".format(artifact.size_bytes)

        # Verify delays are growing (exponential backoff)
        assert len(delays) >= 2, "Expected at least 2 delays, got {}".format(len(delays))
        for i in range(1, len(delays)):
            assert delays[i] >= delays[i - 1] * 0.9, \
                "Delays not growing: {} → {} (expected exponential)".format(
                    delays[i - 1], delays[i])

        print(GREEN + "\n  >>> PASS <<<" + RESET)
        return True

    except Exception as e:
        print(RED + "\n  >>> FAIL: {} <<<".format(e) + RESET)
        import traceback
        traceback.print_exc()
        return False

    finally:
        await client.aclose()
        shutil.rmtree(str(wd), ignore_errors=True)


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
