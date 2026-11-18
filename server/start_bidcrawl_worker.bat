@echo off
rem Bid crawl async worker (must stay running for /api/bidcrawl jobs to execute)
cd /d "%~dp0.."
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"
echo ==========================================
echo  Bid Crawl Worker  -  queue consumer
echo  API:  python -m server.bidcrawl.cli init ^&^& import-recipes
echo ==========================================
python -m server.bidcrawl.cli worker
pause
