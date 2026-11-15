@echo off
cd /d "%~dp0"
set "PYTHON=%~dp0.venv-quran\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo Project virtual environment not found: %PYTHON%
    echo See README_EN.md to create an isolated environment.
    pause
    exit /b 1
)
echo Starting Quran Muaalem Gradio UI...
echo.
echo The app will be available at: http://127.0.0.1:7860
echo Press Ctrl+C to stop.
echo.
"%PYTHON%" gradio_app.py
pause
