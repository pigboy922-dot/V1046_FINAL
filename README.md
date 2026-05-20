# V104.6 FINAL 台美股融合週選雲端版

這包是 Render 可部署版，規則已寫回主程式，不是只有回測檔。

## 最終規則

### 台股 TW_FINAL_H80_MAX3_DYNAMIC_CD
- 週三收盤後掃描，週四開盤買進。
- 股票池：正式執行前自動更新台股 universe，接近全台股掃描，舊持倉/舊種子會併回避免失效。
- 最多持倉 3 檔。
- 單筆停損 -10%。
- 最大持有 H80。
- 選股核心：成交金額足夠、rule_count >= 7、20 日漲幅 10%～45%、60 日漲幅 >= 40%、5 日漲幅 <= 10%、ATR <= 5、RSI <= 84。
- 冷靜期：虧損 1 筆後最多暫停 21 天；只要加權指數或櫃買指數站回 60MA，就提前解除。

### 美股 US_FINAL_H70_FUSION_QQQ100
- 美國週四收盤後掃描，台灣週五晚上買進。
- 股票池：S&P500 + Nasdaq100 + 原本種子名單自動更新。
- 最多持倉 2 檔。
- 單筆停損 -10%。
- 最大持有 H70。
- 選股核心：rule_count >= 5、score >= 75、20 日漲幅 12%～42.5%、60 日漲幅 25%～180%、RSI 60～88、ATR 2.5～20。
- 大盤濾網：QQQ 必須站上 100MA。

## 資金配置目標
- 台股 20%。
- 美股 80%。

## Render 部署
根目錄要直接看到：

```text
Procfile
render.yaml
requirements.txt
runtime.txt
v1046_server.py
v1046_cloud_daily_risk_guard.py
v1046_gs_sync.py
config/
data/
state/
output/
```

不要把整包再包進子資料夾部署，否則 Render 可能繼續跑舊根目錄檔案。

## V104.6.1 標準模式實戰功能

本版只保留標準模式，不提供穩健/積極切換。

標準規則：
- 台股：H80、最多 3 檔、-10% 停損、虧 1 筆後動態 cooldown 最多 21 天，加權或櫃買站上 60MA 提前解除。
- 美股：H70、最多 2 檔、ret20 <= 42.5%、QQQ > 100MA、-10% 停損。
- 資金配置：TW20 / US80。

新增功能：
1. 完整健康檢查：`/health_full` 與 `/api/health_full`。
2. Google Sheets 防呆備份：拉回/回寫前會保留本地備份；回寫前也會把既有工作表備份到 `backup_*` 工作表。
3. 今日動作摘要：`output/v1046_today_action_summary.csv`，首頁會直接顯示今天要不要買、賣、或只更新持倉。
4. 預估買進金額 / 股數 / 停損價：推薦檔會多出 `建議投入`、`預估股數`、`停損價`、`最大虧損額`。

本金設定：
- 預設本金 `100000`。
- 可在 Render 環境變數設定 `V1046_TOTAL_CAPITAL` 覆蓋，例如 `100000`、`300000`。

注意：本系統只做紙上交易紀錄與手動下單參考，不會連券商自動下單。

## 2026-05-20 memory-safe hotfix
- Web page no longer auto-runs the strategy on open. Use the toolbar button **手動更新正式** when you want to refresh signals.
- All displayed timestamps are forced to Asia/Taipei.
- Toolbar now has **正式推薦頁** so health pages can switch back to the recommendation page.
- Render 512MB safety: auto-universe download is disabled in this deploy package to prevent OOM/502. Full-market refresh should be run offline or on a larger Render instance.
- Backtest-only reports were removed from the deploy package.
