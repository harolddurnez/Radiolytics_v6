@echo off
echo Starting Radiolytics Services...

:: Change to the script directory
cd /d "%~dp0"

:: Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo Python is not installed or not in PATH
    echo Please install Python and try again
    pause
    exit /b 1
)

:: Check if required files exist
if not exist reference_recorder.py (
    echo reference_recorder.py not found
    pause
    exit /b 1
)
if not exist fingerprint_matcher.py (
    echo fingerprint_matcher.py not found
    pause
    exit /b 1
)

:: Run the fingerprint indexer in this window (quick summary)
echo Indexing fingerprints...
python fingerprint_indexer.py --index --root "../ADMIN DO NOT COMMIT/fingerprints/"

:: Start reference recorder in a minimized window
echo Starting Reference Recorder...
start /min "Radiolytics Reference Recorder" cmd /k "python reference_recorder.py"

:: Wait a moment to ensure first window opens
timeout /t 2 /nobreak >nul

:: Start fingerprint matcher in a minimized window
echo Starting Fingerprint Matcher...
start /min "Radiolytics Fingerprint Matcher" cmd /k "python fingerprint_matcher.py --run-matcher"

echo.
echo Services have been started in separate windows (minimized).
echo This window will stay open to monitor the services.
echo Press Ctrl+C to close this window (the service windows will remain open).
echo.

:: Keep this window open
:loop
timeout /t 1 /nobreak >nul
goto loop 