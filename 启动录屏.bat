@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where pythonw.exe >nul 2>nul && (start "" pythonw.exe "screen_recorder.py" & exit /b 0)
where pyw.exe >nul 2>nul && (start "" pyw.exe -3 "screen_recorder.py" & exit /b 0)
where python.exe >nul 2>nul && (start "" python.exe "screen_recorder.py" & exit /b 0)

echo Python 3.9+ not found in PATH. Please install Python first.
pause
