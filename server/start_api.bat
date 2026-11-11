@echo off
rem Yadecai Bid Ledger API server (port 8000)
cd /d "%~dp0.."
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"
echo ==========================================
echo  Yadecai Bid Ledger API  -  http://127.0.0.1:8000
echo  First run: pip install -r server\requirements.txt
echo ==========================================
rem ---- ONLYOFFICE online preview/edit (optional; delete this block if unused) ----
rem OFFICE_SERVER   : browser-reachable DS address
rem OFFICE_SECRET   : MUST equal the DS container JWT_SECRET
rem OFFICE_BASE_URL : DS container callback address; 127.0.0.1 inside a container is the
rem                   container itself. Use host.docker.internal WITH the port (8000).
set "OFFICE_SERVER=http://127.0.0.1:8888"
set "OFFICE_SECRET=dev_secret_123"
set "OFFICE_BASE_URL=http://host.docker.internal:8000"
python -m uvicorn server.main:app --host 0.0.0.0 --port 8000
pause
