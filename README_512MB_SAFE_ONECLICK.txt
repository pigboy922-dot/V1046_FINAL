V1046 512MB SAFE ONECLICK

This package changes the one-click button to memory-safe mode for Render Free 512MB:
- Button text: 一鍵更新行情+正式推薦
- It does NOT rebuild the whole market universe inside Render.
- It uses existing config/tw_universe.csv and config/us_universe.csv (TW500 / US367).
- It pulls/pushes Drive cache through Google Drive if enabled.
- It runs the final recommendation after refreshing price data.

Why:
The previous /api/update_all enabled full universe refresh inside the web worker and could exceed 512MB.

For full dynamic universe rebuild, use a larger instance or an external scheduled job.
