@echo off
setlocal

net session >nul 2>&1
if not "%errorlevel%"=="0" (
    echo This script must be run as Administrator.
    pause
    exit /b 1
)

sc query Waveception >nul 2>&1
if "%errorlevel%"=="0" goto REMOVE_EXISTING_SERVICE
goto INSTALL_SERVICE

:REMOVE_EXISTING_SERVICE
echo Existing Waveception service found. Removing it before installing this version...
python "%ProgramFiles%\Waveception\waveception7.py" stop
python "%ProgramFiles%\Waveception\waveception7.py" remove

sc query Waveception >nul 2>&1
if not "%errorlevel%"=="0" goto INSTALL_SERVICE

echo Existing service still appears to exist. Attempting stuck-service cleanup...
set "SERVICE_PID="
for /f "tokens=3" %%P in ('sc queryex Waveception ^| findstr /R /C:"PID"') do set "SERVICE_PID=%%P"
if defined SERVICE_PID if not "%SERVICE_PID%"=="0" taskkill /PID %SERVICE_PID% /F
timeout /t 5 /nobreak >nul

sc query Waveception >nul 2>&1
if not "%errorlevel%"=="0" goto INSTALL_SERVICE

echo Could not remove existing Waveception service. Reboot Windows and run installer again.
pause
exit /b 1

:INSTALL_SERVICE
python "%ProgramFiles%\Waveception\waveception7.py" install
sc config Waveception start= auto
python "%ProgramFiles%\Waveception\waveception7.py" start
