@echo off
cd /d %~dp0
set FLASK_ENV=production
python v1046_server.py
pause
