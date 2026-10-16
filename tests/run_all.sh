#!/usr/bin/env bash
# Every check that does not need a human at the microphone.
# Run from the project root:  ./tests/run_all.sh
set -u
cd "$(dirname "$0")/.."
fail=0
for t in tests/test_stt.py tests/test_tts.py tests/test_wake.py tests/test_brain.py tests/test_pump.py tests/test_agc.py tests/test_audio_frames.py tests/test_stt_weakmic.py tests/test_memory.py tests/test_memory_store.py tests/test_wake_phrase.py tests/test_transcriber_lock.py tests/test_hud_security.py tests/test_brain_deadline.py tests/test_tts_stream.py; do
  printf '\n=== %s ===\n' "$t"
  if uv run python "$t"; then echo "-> pass"; else echo "-> FAIL"; fail=1; fi
done
printf '\n%s\n' "$([ $fail -eq 0 ] && echo 'all green' || echo 'something failed')"
exit $fail
