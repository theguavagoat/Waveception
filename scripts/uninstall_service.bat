@echo off
setlocal

net session >nul 2>&1
if not "%errorlevel%"=="0" (
    echo This script must be run as Administrator.
    pause
    exit /b 1
)

python "%ProgramFiles%\Waveception\waveception7.py" stop
python "%ProgramFiles%\Waveception\waveception7.py" remove

sc query Waveception >nul 2>&1
if not "%errorlevel%"=="0" exit /b 0

echo Waveception service still appears to exist. Attempting stuck-service cleanup...
set "SERVICE_PID="
for /f "tokens=3" %%P in ('sc queryex Waveception ^| findstr /R /C:"PID"') do set "SERVICE_PID=%%P"
if defined SERVICE_PID if not "%SERVICE_PID%"=="0" taskkill /PID %SERVICE_PID% /F
timeout /t 5 /nobreak >nul
