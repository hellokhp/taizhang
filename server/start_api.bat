@echo off
rem Yadecai Bid Ledger API server (port 8000)
cd /d "%~dp0.."
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"
echo ==========================================
echo  Yadecai Bid Ledger API  -  http://127.0.0.1:8000
echo  First run: pip install -r server\requirements.txt
echo ==========================================
python -m uvicorn server.main:app --host 0.0.0.0 --port 8000
pause
