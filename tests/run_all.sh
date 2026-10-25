#!/usr/bin/env bash
# Every check that does not need a human at the microphone.
# Run from the project root:  ./tests/run_all.sh
set -u
cd "$(dirname "$0")/.."
fail=0
for t in tests/test_stt.py tests/test_tts.py tests/test_wake.py tests/test_brain.py tests/test_pump.py tests/test_agc.py tests/test_audio_frames.py tests/test_stt_weakmic.py tests/test_memory.py tests/test_memory_store.py tests/test_wake_phrase.py tests/test_transcriber_lock.py tests/test_hud_security.py tests/test_brain_deadline.py tests/test_tts_stream.py tests/test_focus.py tests/test_interrupt.py tests/test_mic_toggle.py tests/test_vad.py tests/test_vision.py tests/test_sweep.py tests/test_sweep_browser.py tests/test_briefing.py tests/test_reminders.py tests/test_intents.py tests/test_session_archive.py tests/test_model_chain.py tests/test_mic_state.py tests/test_brain_concurrency.py tests/test_phrasings.py tests/test_platform.py tests/test_macos.py tests/test_fetch_models.py tests/test_net.py tests/test_degrades.py tests/test_local_chain.py tests/test_permissions.py tests/test_privacy.py; do
  printf '\n=== %s ===\n' "$t"
  if uv run python "$t"; then echo "-> pass"; else echo "-> FAIL"; fail=1; fi
done
printf '\n%s\n' "$([ $fail -eq 0 ] && echo 'all green' || echo 'something failed')"
exit $fail
