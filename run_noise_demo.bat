@echo off
cd /d "%~dp0"
set "PYTHON=%~dp0.venv-noise\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo Noise demo environment not found: %PYTHON%
    echo See docs\noise_frontend.md for setup instructions.
    pause
    exit /b 1
)
set "PYTHONPATH=%~dp0src"
echo Starting the raw-versus-frontend comparison demo...
echo The page will be available at: http://127.0.0.1:7860
echo Press Ctrl+C to stop.
echo.
"%PYTHON%" "%~dp0scripts\noise_demo_app.py"
