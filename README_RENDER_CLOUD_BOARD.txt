V1046 Render 雲端看板版

用途：
- Render 只讀 Google Sheets / Drive 顯示推薦、持倉、健康狀態
- 不在 Render 上更新全市場、不做重運算，避免 512MB OOM

保留按鈕：
- 雲端看板
- 從 Google 重新載入
- 完整健康檢查
- API 狀態

正式更新請用本機版：
run_local_update_all_and_sync.bat

Render 必要環境變數：
GOOGLE_SERVICE_ACCOUNT_JSON_B64
V1046_GSHEETS_ENABLED=1
V1046_GSHEETS_ID=18YA4MvYJ0bclofBsViJ_zSnPcbmVQ4gmy0DC12Q1-3I
V1046_GDRIVE_ENABLED=1
V1046_GDRIVE_FOLDER_ID=1eW9iKeK9bwRgf0N22ChXOoI8M82GlAI7
V1046_CLOUD_READONLY=true
