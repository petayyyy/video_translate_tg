#!/usr/bin/env python3
"""Test: mock always returns stubs — should fail with TranslationTimeout, not hang."""
import asyncio, os, shutil, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, "/home/ilyah/video_translate_tg")
from app.config import VotSettings
from app.pipeline.vot import VotClient
from app.pipeline.errors import TranslationTimeout
from app.utils.urls import parse_video_ref

MOCK_BINARY = "/home/ilyah/.local/bin/vot-cli-mock"
COUNT_FILE = "/tmp/vot_mock_budget_count"

# Always stubs (0 = never give real audio)
os.environ["VOT_MOCK_STUBS"] = "999999"
os.environ["VOT_MOCK_COUNT_FILE"] = COUNT_FILE
os.environ["VOT_MOCK_REAL_AUDIO"] = "/tmp/real_audio.mp3"
os.environ["VOT_MOCK_DELAY"] = "0.2"

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


async def main():
    print("=" * 70)
    print(YELLOW + "BUDGET EXHAUSTION TEST: always stubs → timeout, not hang" + RESET)
    print("=" * 70)

    Path(COUNT_FILE).write_text("0")

    settings = VotSettings(
        binary=MOCK_BINARY,
        flavor="live",
        lively_voice=True,
        backoff_start_sec=1,
        backoff_factor=1.3,
        backoff_max_sec=3,
        backoff_jitter_sec=0,
        attempt_timeout_sec=10,
        total_timeout_sec=15,   # short budget — should exhaust quickly
    )

    ref = parse_video_ref("https://youtube.com/watch?v=TESTBUDGET")
    wd = Path(tempfile.mkdtemp(prefix="vot_budget_"))
    client = VotClient(settings)

    try:
        t0 = time.monotonic()
        try:
            await client.get_audio(ref, wd)
            print(RED + "\n  >>> FAIL: Should have timed out, got success <<<" + RESET)
            return False
        except TranslationTimeout as e:
            elapsed = time.monotonic() - t0
            print("  TranslationTimeout after {:.1f}s".format(elapsed))
            print("  Attempts: {}".format(e.detail))
            # Should fail within budget (+ small overhead for process start)
            assert elapsed < settings.total_timeout_sec + 20, \
                "Timed out too late: {:.1f}s vs budget {:.1f}s".format(
                    elapsed, settings.total_timeout_sec)
            print(GREEN + "\n  >>> PASS <<<" + RESET)
            return True

    except Exception as e:
        elapsed = time.monotonic() - t0
        print(RED + "\n  >>> FAIL: {} after {:.1f}s <<<".format(type(e).__name__, elapsed) + RESET)
        import traceback
        traceback.print_exc()
        return False

    finally:
        await client.aclose()
        shutil.rmtree(str(wd), ignore_errors=True)


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
