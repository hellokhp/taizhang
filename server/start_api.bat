@echo off
rem Yadecai Bid Ledger API server (port 8000)
cd /d "%~dp0.."
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"
echo ==========================================
echo  Yadecai Bid Ledger API  -  http://127.0.0.1:8000
echo  First run: pip install -r server\requirements.txt
echo ==========================================
rem ---- 锁后端（阶段 2/4）：默认 local 单进程，行为与旧版一致。 ----
rem 要用 Redis 分布式锁 + 多进程，二选一：
rem   1) 改 server\config.json 的 "lock_backend": "redis"
rem   2) 或在此 set "LOCK_BACKEND=redis"（环境变量优先级更高）
rem 然后下面 --workers 改成 2 或更多。切换前确认 config.json 的 redis 段可用。
rem set "LOCK_BACKEND=redis"
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
