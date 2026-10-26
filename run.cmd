@echo off
REM Start ARCHY on Windows.
REM
REM   run.cmd                 talk to it, dashboard opens in a window
REM   run.cmd --no-hud        console only
REM   run.cmd --say "hi"      one typed message, no microphone
REM   run.cmd --mic-test      watch the input level while you talk
REM
REM What works here: the voice loop, the wake phrase, reminders, the briefing
REM (without the machine readings, which come from /proc on Linux), the memory
REM store, the dashboard and screen vision (through Pillow). What does not: the
REM security sweep, which reads a Linux machine.
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
  echo uv is not on PATH. Install it from https://docs.astral.sh/uv/ and try again.
  exit /b 1
)

REM only open a window for the normal talking mode
set OPEN=--open
if "%~1"=="--no-hud"  set OPEN=
if "%~1"=="--say"     set OPEN=
if "%~1"=="--mic-test" set OPEN=

if defined OPEN (
  uv run python -m archy --open %*
) else (
  uv run python -m archy %*
)
endlocal
