@echo off
setlocal

set "ISCC_EXE="

where ISCC >nul 2>&1
if "%errorlevel%"=="0" set "ISCC_EXE=ISCC"

if defined ISCC_EXE goto BUILD
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"

if defined ISCC_EXE goto BUILD
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if defined ISCC_EXE goto BUILD
if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"

if defined ISCC_EXE goto BUILD
goto MISSING_INNO

:BUILD
"%ISCC_EXE%" "%~dp0installer\Waveception.iss"
if not "%errorlevel%"=="0" goto BUILD_FAILED

echo.
echo Installer build complete.
echo Installer: %~dp0WaveceptionSetup.exe
pause
exit /b 0

:BUILD_FAILED
echo Installer build failed.
pause
exit /b 1

:MISSING_INNO
echo Inno Setup Compiler was not found on PATH.
echo I also checked the common install locations.
echo Install Inno Setup, then reopen Command Prompt and run this again.
echo Download: https://jrsoftware.org/isdl.php
pause
exit /b 1
