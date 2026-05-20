@echo off
cd /d %~dp0
set V1046_UPDATE_MARKET=US
python v1046_cloud_daily_risk_guard.py
pause
