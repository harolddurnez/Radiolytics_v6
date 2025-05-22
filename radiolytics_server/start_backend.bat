@echo off
cd /d %~dp0

REM Optional: activate virtual environment if you use one
REM call venv\Scripts\activate

REM Install dependencies
"C:\Users\harol\AppData\Local\Programs\Python\Python313\python.exe" -m pip install -r requirements.txt

REM Launch the backend server
"C:\Users\harol\AppData\Local\Programs\Python\Python313\python.exe" radiolytics_backend.py

pause