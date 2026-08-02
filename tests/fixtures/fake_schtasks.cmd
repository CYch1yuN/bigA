@echo off
rem Fake schtasks for tests: records args, optionally fails.
rem Env:
rem   FAKE_SCHTASKS_LOG  - file to append "arg1|arg2|..." per invocation
rem   FAKE_SCHTASKS_FAIL - substring; if present in the invocation, exit 1
setlocal
if defined FAKE_SCHTASKS_LOG (
  echo %*>>"%FAKE_SCHTASKS_LOG%"
)
if defined FAKE_SCHTASKS_FAIL (
  echo %*| findstr /C:"%FAKE_SCHTASKS_FAIL%" >nul
  if not errorlevel 1 (
    exit /b 1
  )
)
exit /b 0
