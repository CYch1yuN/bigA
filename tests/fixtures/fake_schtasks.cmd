@echo off
rem Fake schtasks for tests: records args, tracks created tasks, optionally
rem fails. Mimics real schtasks semantics needed by install_scheduler.ps1:
rem   - /Create /TN <name>  : registers <name> as existing (exit 0)
rem   - /Query  /TN <name>  : exit 0 if <name> was created, else stderr + exit 1
rem   - /Delete /TN <name>  : removes <name> (exit 0)
rem
rem Task names are matched literally (tests use the default prefix
rem AShareQuantAutomation + -Daily/-Weekly), avoiding fragile cmd parsing.
rem
rem Env:
rem   FAKE_SCHTASKS_LOG   - file to append "%*" per invocation
rem   FAKE_SCHTASKS_FAIL  - substring; if present in the invocation, exit 1
rem   FAKE_SCHTASKS_STATE - directory where created task names are stored
rem                         (default: %TEMP%\fake_schtasks_state)

setlocal
if "%FAKE_SCHTASKS_STATE%"=="" set FAKE_SCHTASKS_STATE=%TEMP%\fake_schtasks_state
if not exist "%FAKE_SCHTASKS_STATE%" mkdir "%FAKE_SCHTASKS_STATE%" >nul 2>&1

if defined FAKE_SCHTASKS_LOG (
  echo %*>>"%FAKE_SCHTASKS_LOG%"
)

if defined FAKE_SCHTASKS_FAIL (
  echo %*| findstr /C:"%FAKE_SCHTASKS_FAIL%" >nul
  if not errorlevel 1 (
    exit /b 1
  )
)

rem Identify the task name by literal match (default prefix only).
set "NAME="
echo %*| findstr /C:"AShareQuantAutomation-Daily" >nul
if not errorlevel 1 set "NAME=AShareQuantAutomation-Daily"
echo %*| findstr /C:"AShareQuantAutomation-Weekly" >nul
if not errorlevel 1 set "NAME=AShareQuantAutomation-Weekly"

echo %*| findstr /C:"/Create" >nul
if not errorlevel 1 (
  if defined NAME (
    echo created> "%FAKE_SCHTASKS_STATE%\%NAME%.txt" 2>nul
  )
  exit /b 0
)

echo %*| findstr /C:"/Delete" >nul
if not errorlevel 1 (
  if defined NAME (
    del "%FAKE_SCHTASKS_STATE%\%NAME%.txt" 2>nul
  )
  exit /b 0
)

echo %*| findstr /C:"/Query" >nul
if not errorlevel 1 (
  if defined NAME (
    if exist "%FAKE_SCHTASKS_STATE%\%NAME%.txt" (
      exit /b 0
    )
  )
  echo ERROR: The system cannot find the file specified. 1>&2
  exit /b 1
)

exit /b 0
