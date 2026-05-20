V1046 one-click universe update

新增：
- 首頁按鈕「一鍵更新股池+正式推薦」
- API：POST /api/update_all
- 頁面：/update_all

流程：
1. 啟用本次股池更新（不改 settings.json 的安全設定）
2. 同時更新台股與美股 universe CSV
3. 接著跑正式推薦
4. 輸出 output/v1046_oneclick_universe_update_report.html 與 JSON

平常流程：
- 禮拜五早上按「一鍵更新股池+正式推薦」
- 其他時間只要重跑推薦，按「只跑正式推薦」

注意：
- 首頁開啟仍不會自動跑，避免 Render 512MB 一打開就爆。
- 若一鍵更新仍 OOM，代表 Render 免費機不夠，需要改用外部排程或升級記憶體。
