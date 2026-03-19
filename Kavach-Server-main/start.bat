@echo off
title Kavach Server
cd /d "%~dp0"

:: Create venv if it doesn't exist
if not exist "venv\Scripts\python.exe" (
    echo [Kavach] Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [Kavach] Virtual environment created.
)

:: Activate venv and install/update dependencies
echo [Kavach] Activating virtual environment...
call venv\Scripts\activate.bat

echo [Kavach] Installing dependencies...
pip install -r Requirements.txt --quiet
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

:: Run the server
echo [Kavach] Starting server...
python app.py

pause
