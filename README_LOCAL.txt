V1046 本機版使用方式

1. 解壓縮本機版 zip
2. 在資料夾內開 PowerShell / CMD
3. 第一次執行：
   pip install -r requirements.txt
4. 啟動本機網站：
   python v1046_server.py
   或雙擊 start_local_server.bat
5. 打開：
   http://127.0.0.1:10000/?auto=0

本機版可以承受比 Render 免費機更大的記憶體，適合跑台美股完整更新。

建議本機流程：
- 先設定 .env 或系統環境變數：
  V1046_GSHEETS_ENABLED=1
  V1046_GSHEETS_STATE_ENABLED=1
  V1046_GSHEETS_OUTPUT_ENABLED=1
  V1046_GSHEETS_ID=你的 Sheet ID
  V1046_GDRIVE_ENABLED=1
  V1046_GDRIVE_FOLDER_ID=你的 Drive Folder ID
  GOOGLE_SERVICE_ACCOUNT_JSON_B64=你的 service account JSON base64

如果只要本機直接跑正式推薦：
  python v1046_cloud_daily_risk_guard.py

如果只更新台股行情再跑正式推薦：
  set V1046_UPDATE_MARKET=TW
  python v1046_cloud_daily_risk_guard.py

如果只更新美股行情再跑正式推薦：
  set V1046_UPDATE_MARKET=US
  python v1046_cloud_daily_risk_guard.py

注意：本機不要把 JSON 私鑰公開上傳到 GitHub。
