# -*- coding: utf-8 -*-
"""V104.6 cloud web app.

Routes:
- /              latest dashboard with cloud controls
- /api/run       start real/demo pipeline in a guarded worker thread
- /api/status    current/last run status
- /files         download center for generated CSV/HTML outputs
- /health        generated health report, or basic OK
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import json
import os
import re
import threading
import traceback
import time
from typing import Dict, Iterable, List, Tuple

from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory, url_for

from v1046_cloud_daily_risk_guard import run_pipeline, load_settings, refresh_auto_universe_files

try:
    from v1046_gs_sync import get_sheets_status, get_drive_status, public_sheet_url
except Exception:  # pragma: no cover
    get_sheets_status = None
    get_drive_status = None
    public_sheet_url = None

ROOT = Path(__file__).resolve().parent
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
UTC_TZ = ZoneInfo("UTC")
OUTPUT = ROOT / "output"
STATE = ROOT / "state"
CONFIG = ROOT / "config"
LOGS = ROOT / "logs"
DASHBOARD = OUTPUT / "v1046_paper_dashboard.html"
HEALTH_REPORT = OUTPUT / "v1046_health_report.html"
APP_NAME = "V104.6 FINAL 台美股融合週選雲端版"

app = Flask(__name__)

_job_lock = threading.Lock()
_job: Dict[str, object] = {
    "running": False,
    "mode": "idle",
    "status": "idle",
    "message": "尚未從雲端啟動本次任務",
    "last_started_at": "",
    "last_finished_at": "",
    "last_exit_code": None,
    "auto_open_enabled": True,
    "auto_open_last_attempt_at": "",
    "auto_open_last_skipped_reason": "",
}
_auto_open_lock = threading.Lock()
_auto_open_last_attempt_ts = 0.0


def now_ts() -> str:
    return datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")

def utc_ts() -> str:
    return datetime.now(UTC_TZ).strftime("%Y-%m-%d %H:%M:%S")

def fmt_mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S") if path.exists() else ""
    except Exception:
        return ""


def public_base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or request.host_url.rstrip("/")


def token_required() -> bool:
    return bool(os.environ.get("V1046_RUN_TOKEN") or os.environ.get("RUN_TOKEN"))


def check_token() -> bool:
    token = os.environ.get("V1046_RUN_TOKEN") or os.environ.get("RUN_TOKEN")
    if not token:
        return True
    got = request.args.get("token") or request.headers.get("X-V1046-Token") or request.form.get("token")
    return got == token


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def auto_open_enabled() -> bool:
    # Default ON: opening the home page starts a guarded cloud update.
    return env_bool("V1046_AUTO_RUN_ON_OPEN", False)


def auto_open_demo() -> bool:
    # Default REAL because the user's expectation is "open page -> update live dashboard".
    # Set V1046_AUTO_RUN_MODE=demo to make page-open updates test-only.
    return os.environ.get("V1046_AUTO_RUN_MODE", "real").strip().lower() in {"demo", "test", "1", "true"}


def auto_open_cooldown_seconds() -> int:
    # Prevents reload loops and repeated runs when several visitors open the page.
    return max(0, env_int("V1046_AUTO_OPEN_COOLDOWN_SECONDS", 600))


def _set_job(**kwargs) -> None:
    with _job_lock:
        _job.update(kwargs)


def _job_snapshot() -> Dict[str, object]:
    with _job_lock:
        data = dict(_job)
    data["dashboard_exists"] = DASHBOARD.exists()
    data["health_exists"] = HEALTH_REPORT.exists()
    data["token_required"] = token_required()
    data["base_url"] = public_base_url()
    data["auto_open_enabled"] = auto_open_enabled()
    data["auto_open_mode"] = "DEMO" if auto_open_demo() else "REAL"
    data["auto_open_cooldown_seconds"] = auto_open_cooldown_seconds()
    if get_sheets_status is not None:
        try:
            data["google_sheets"] = get_sheets_status(light=True)
        except Exception as exc:
            data["google_sheets"] = {"enabled": False, "error": f"{type(exc).__name__}: {exc}"}
    if get_drive_status is not None:
        try:
            data["google_drive"] = get_drive_status(light=True)
        except Exception as exc:
            data["google_drive"] = {"enabled": False, "error": f"{type(exc).__name__}: {exc}"}
    return data


def _worker(demo: bool) -> None:
    mode_label = "DEMO" if demo else "REAL"
    _set_job(
        running=True,
        mode=mode_label,
        status="running",
        message=f"{mode_label} 執行中",
        last_started_at=now_ts(),
        last_finished_at="",
        last_exit_code=None,
    )
    try:
        code = int(run_pipeline(demo=demo))
        _set_job(
            running=False,
            status="success" if code == 0 else "failed",
            message="執行完成" if code == 0 else "執行失敗，請看健康檢查",
            last_finished_at=now_ts(),
            last_exit_code=code,
        )
    except Exception as exc:  # keep the web app alive even when the strategy fails
        err = traceback.format_exc()
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / "v1046_cloud_server_error.log").write_text(err, encoding="utf-8")
        _set_job(
            running=False,
            status="failed",
            message=f"例外錯誤：{type(exc).__name__}: {exc}",
            last_finished_at=now_ts(),
            last_exit_code=1,
        )




def _write_oneclick_universe_report(health: List[str], code: int | None = None) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at_taipei": now_ts(),
        "server_utc": utc_ts(),
        "exit_code": code,
        "tw_universe_rows": _csv_rows(CONFIG / "tw_universe.csv"),
        "us_universe_rows": _csv_rows(CONFIG / "us_universe.csv"),
        "health": health,
    }
    (OUTPUT / "v1046_oneclick_universe_update.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = "".join(f"<tr><td>{i+1}</td><td>{line}</td></tr>" for i, line in enumerate(health[-300:])) or "<tr><td colspan='2'>尚無紀錄</td></tr>"
    html = f"""<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>一鍵更新股池報告</title><style>body{{font-family:'Microsoft JhengHei',Arial,sans-serif;background:#eef2f6;color:#243447}}.wrap{{max-width:1100px;margin:18px auto;background:#fffdf7;border:1px solid #dde4dc;border-radius:18px;padding:16px}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{border-bottom:1px solid #e2e8d7;padding:7px;text-align:left}}th{{background:#eef1e6}}</style></head><body><div class='wrap'><h1>一鍵更新台美股股池報告</h1><p>台灣時間：{payload['generated_at_taipei']}｜exit_code={code}</p><p>TW universe rows: <b>{payload['tw_universe_rows']}</b>｜US universe rows: <b>{payload['us_universe_rows']}</b></p><p><a href='/?auto=0'>回正式推薦頁</a>｜<a href='/api/status'>API 狀態</a></p><table><thead><tr><th>#</th><th>紀錄</th></tr></thead><tbody>{rows}</tbody></table></div></body></html>"""
    (OUTPUT / "v1046_oneclick_universe_update_report.html").write_text(html, encoding="utf-8")


def _csv_rows(path: Path) -> int:
    try:
        if not path.exists():
            return 0
        import pandas as pd
        return int(len(pd.read_csv(path)))
    except Exception:
        return -1


def _worker_update_all_then_run() -> None:
    """512MB-safe one-click path.

    Render free instances have only 512MB RAM. Rebuilding the whole TW/US
    universe in-process can exceed that limit, so this button updates prices
    and recommendations for the existing expanded pools (TW500 / US367).
    Large cache files are pulled/pushed via Google Drive inside run_pipeline
    through sync_before_run/sync_after_run.
    """
    _set_job(
        running=True,
        mode="CACHE+REAL",
        status="running",
        message="512MB安全模式：更新既有TW500/US367行情與正式推薦",
        last_started_at=now_ts(),
        last_finished_at="",
        last_exit_code=None,
    )
    health: List[str] = []
    try:
        health.append(f"START 512MB-safe cache+REAL run at Taipei {now_ts()}")
        health.append("INFO skip full-market universe rebuild on Render 512MB; use existing config/tw_universe.csv and config/us_universe.csv")
        health.append("INFO Google Drive cache pull/push will be handled by run_pipeline if enabled")
        code = int(run_pipeline(demo=False))
        health.append(f"FINISH REAL recommendation at Taipei {now_ts()} exit_code={code}")
        _write_oneclick_universe_report(health, code=code)
        _set_job(
            running=False,
            status="success" if code == 0 else "failed",
            message="更新既有股池行情 + 正式推薦完成" if code == 0 else "更新行情後正式推薦失敗，請看健康檢查",
            last_finished_at=now_ts(),
            last_exit_code=code,
        )
    except Exception as exc:
        err = traceback.format_exc()
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / "v1046_oneclick_update_error.log").write_text(err, encoding="utf-8")
        health.append(f"ERROR {type(exc).__name__}: {exc}")
        _write_oneclick_universe_report(health, code=1)
        _set_job(
            running=False,
            status="failed",
            message=f"更新行情例外錯誤：{type(exc).__name__}: {exc}",
            last_finished_at=now_ts(),
            last_exit_code=1,
        )



def _worker_market_cache_then_run(market: str) -> None:
    market = market.upper()
    label = "台股" if market == "TW" else "美股"
    _set_job(
        running=True,
        mode=f"{market}+REAL",
        status="running",
        message=f"512MB安全模式：只更新{label}行情，再跑正式推薦",
        last_started_at=now_ts(),
        last_finished_at="",
        last_exit_code=None,
    )
    health: List[str] = []
    old = os.environ.get("V1046_UPDATE_MARKET")
    try:
        os.environ["V1046_UPDATE_MARKET"] = market
        health.append(f"START {market}-only cache+REAL run at Taipei {now_ts()}")
        health.append(f"INFO update only {market}; the other market uses Google Drive/local cache if available")
        code = int(run_pipeline(demo=False))
        health.append(f"FINISH {market}-only REAL recommendation at Taipei {now_ts()} exit_code={code}")
        _write_oneclick_universe_report(health, code=code)
        _set_job(
            running=False,
            status="success" if code == 0 else "failed",
            message=f"只更新{label}行情 + 正式推薦完成" if code == 0 else f"只更新{label}後正式推薦失敗，請看健康檢查",
            last_finished_at=now_ts(),
            last_exit_code=code,
        )
    except Exception as exc:
        err = traceback.format_exc()
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / f"v1046_update_{market.lower()}_error.log").write_text(err, encoding="utf-8")
        health.append(f"ERROR {type(exc).__name__}: {exc}")
        _write_oneclick_universe_report(health, code=1)
        _set_job(
            running=False,
            status="failed",
            message=f"只更新{label}例外錯誤：{type(exc).__name__}: {exc}",
            last_finished_at=now_ts(),
            last_exit_code=1,
        )
    finally:
        if old is None:
            os.environ.pop("V1046_UPDATE_MARKET", None)
        else:
            os.environ["V1046_UPDATE_MARKET"] = old


def start_market_cache_then_run(market: str) -> Tuple[bool, Dict[str, object]]:
    market = market.upper()
    if market not in {"TW", "US"}:
        market = "TW"
    with _job_lock:
        if _job.get("running"):
            return False, dict(_job)
        _job.update(
            running=True,
            mode=f"{market}+REAL",
            status="queued",
            message=f"已排入：只更新{('台股' if market == 'TW' else '美股')}行情 + 正式推薦",
            last_started_at=now_ts(),
            last_finished_at="",
            last_exit_code=None,
        )
    t = threading.Thread(target=_worker_market_cache_then_run, args=(market,), daemon=True)
    t.start()
    return True, _job_snapshot()

def start_run(demo: bool) -> Tuple[bool, Dict[str, object]]:
    with _job_lock:
        if _job.get("running"):
            return False, dict(_job)
        # mark immediately so double clicks cannot start two runs
        _job.update(
            running=True,
            mode="DEMO" if demo else "REAL",
            status="queued",
            message="已排入雲端執行",
            last_started_at=now_ts(),
            last_finished_at="",
            last_exit_code=None,
        )
    t = threading.Thread(target=_worker, args=(demo,), daemon=True)
    t.start()
    return True, _job_snapshot()




def start_update_all_then_run() -> Tuple[bool, Dict[str, object]]:
    with _job_lock:
        if _job.get("running"):
            return False, dict(_job)
        _job.update(
            running=True,
            mode="UNIVERSE+REAL",
            status="queued",
            message="已排入：一鍵更新台美股股池 + 正式推薦",
            last_started_at=now_ts(),
            last_finished_at="",
            last_exit_code=None,
        )
    t = threading.Thread(target=_worker_update_all_then_run, daemon=True)
    t.start()
    return True, _job_snapshot()

def maybe_auto_run_on_open() -> None:
    """Start one guarded run when the home page is opened.

    This is intentionally server-side instead of exposing a token in JavaScript.
    A cooldown prevents reload loops after the dashboard refreshes itself.
    """
    global _auto_open_last_attempt_ts
    enabled = auto_open_enabled()
    _set_job(auto_open_enabled=enabled)
    if not enabled:
        _set_job(auto_open_last_skipped_reason="V1046_AUTO_RUN_ON_OPEN 已關閉")
        return
    # Allow quick manual override for debugging or sharing a static page.
    if request.args.get("auto", "").strip().lower() in {"0", "false", "off", "no"}:
        _set_job(auto_open_last_skipped_reason="本次網址參數 auto=0，未自動更新")
        return

    cooldown = auto_open_cooldown_seconds()
    now = time.time()
    with _auto_open_lock:
        elapsed = now - _auto_open_last_attempt_ts if _auto_open_last_attempt_ts else None
        if elapsed is not None and elapsed < cooldown:
            left = int(cooldown - elapsed)
            _set_job(auto_open_last_skipped_reason=f"防連續重跑冷卻中，約 {left} 秒後可再次自動更新")
            return
        _auto_open_last_attempt_ts = now

    demo = auto_open_demo()
    started, data = start_run(demo=demo)
    _set_job(
        auto_open_last_attempt_at=now_ts(),
        auto_open_last_skipped_reason="" if started else str(data.get("message") or "已有任務執行中"),
    )


def important_files() -> List[Tuple[str, Path]]:
    files: List[Tuple[str, Path]] = []
    for folder, names in [
        ("output", [
            "v1046_paper_dashboard.html",
            "v1046_today_recommendations_SIMPLE.csv",
            "v1046_today_recommendations.csv",
            "v1046_risk_guard_status.csv",
            "v1046_today_action_summary.csv",
            "v1046_full_health.json",
            "v1046_paper_monitor_summary.csv",
            "v1046_health_report.html",
            "v1046_monthly_calibration_report.html",
            "v1046_no_recommendation_reason.txt",
            "v1046_risk_latest_prices.csv",
            "v1046_tw_latest_features.csv",
            "v1046_us_latest_features.csv",
        ]),
        ("state", [
            "v1046_paper_positions.csv",
            "v1046_paper_closed_trades.csv",
            "v1046_paper_equity_curve.csv",
            "v1046_daily_signal_ledger.csv",
        ]),
        ("config", ["settings.json", "tw_universe.csv", "us_universe.csv"]),
        ("logs", ["v1046_cloud_server_error.log"]),
    ]:
        base = {"output": OUTPUT, "state": STATE, "config": CONFIG, "logs": LOGS}[folder]
        for name in names:
            p = base / name
            if p.exists():
                files.append((folder, p))
    return files


def safe_send(folder: str, name: str):
    roots = {"output": OUTPUT, "state": STATE, "config": CONFIG, "logs": LOGS}
    base = roots.get(folder)
    if base is None:
        abort(404)
    # only single file names are exposed; no traversal, no nested paths.
    clean = Path(name).name
    target = base / clean
    if not target.exists() or not target.is_file():
        abort(404)
    return send_from_directory(base, clean, as_attachment=False)


def cloud_css() -> str:
    # Scoped, override-style CSS only touches the injected cloud toolbar/status.
    return """
<style id="v1046-cloud-override-css">
#v1046-cloudbar,#v1046-cloudbar *{box-sizing:border-box!important}
#v1046-cloudbar{position:sticky!important;top:0!important;z-index:9999!important;display:flex!important;align-items:center!important;gap:8px!important;flex-wrap:wrap!important;padding:10px 14px!important;background:#0f172a!important;color:#fff!important;border-bottom:1px solid rgba(255,255,255,.18)!important;font-family:'Microsoft JhengHei',Arial,sans-serif!important;box-shadow:0 8px 18px rgba(15,23,42,.18)!important}
#v1046-cloudbar strong{font-size:15px!important;margin-right:4px!important;color:#fff!important}
#v1046-cloudbar button,#v1046-cloudbar a{appearance:none!important;border:1px solid rgba(255,255,255,.22)!important;border-radius:999px!important;background:#1e293b!important;color:#fff!important;text-decoration:none!important;padding:8px 12px!important;font-weight:900!important;font-size:13px!important;line-height:1!important;cursor:pointer!important}
#v1046-cloudbar button:hover,#v1046-cloudbar a:hover{background:#334155!important}
#v1046-cloudbar .v1046-primary{background:#2563eb!important;border-color:#60a5fa!important}
#v1046-cloudbar .v1046-demo{background:#047857!important;border-color:#34d399!important}
#v1046-cloudbar .v1046-status{margin-left:auto!important;max-width:100%!important;font-size:12px!important;color:#cbd5e1!important;white-space:normal!important}
#v1046-cloudbar .v1046-dot{display:inline-block!important;width:8px!important;height:8px!important;border-radius:50%!important;background:#94a3b8!important;margin-right:5px!important;vertical-align:middle!important}
#v1046-cloudbar[data-status="running"] .v1046-dot,#v1046-cloudbar[data-status="queued"] .v1046-dot{background:#f59e0b!important}
#v1046-cloudbar[data-status="success"] .v1046-dot{background:#22c55e!important}
#v1046-cloudbar[data-status="failed"] .v1046-dot{background:#ef4444!important}
@media(max-width:720px){#v1046-cloudbar{position:relative!important}#v1046-cloudbar .v1046-status{width:100%!important;margin-left:0!important}}
</style>
"""


def cloud_bar_html() -> str:
    locked = "｜已啟用執行密碼" if token_required() else ""
    auto = "｜開頁自動更新已開啟" if auto_open_enabled() else "｜開頁自動更新已關閉"
    return f"""
<div id="v1046-cloudbar" data-status="idle">
  <strong>{APP_NAME}</strong>
  <a class="v1046-primary" href="/?auto=0">正式推薦頁</a>
  <button class="v1046-primary" type="button" data-v1046-run="tw">更新台股行情+正式推薦</button>
  <button class="v1046-primary" type="button" data-v1046-run="us">更新美股行情+正式推薦</button>
  <button type="button" data-v1046-run="real">只跑正式推薦</button>
  <a href="{url_for('health_full')}">完整健康檢查</a>
  <a href="{url_for('api_status')}">API 狀態</a>
  <span class="v1046-status"><span class="v1046-dot"></span><span id="v1046-cloud-status-text">雲端控制列已載入{auto}{locked}</span></span>
</div>
"""


def cloud_script() -> str:
    return """
<script id="v1046-cloud-override-js">
(function(){
  const bar = document.getElementById('v1046-cloudbar');
  const text = document.getElementById('v1046-cloud-status-text');
  if(!bar || !text) return;
  let sawActive = false;
  let reloadScheduled = false;
  function setStatus(s){
    bar.dataset.status = s.status || 'idle';
    const bits = [];
    if(s.status) bits.push(s.status);
    if(s.mode && s.mode !== 'idle') bits.push(s.mode);
    if(s.message) bits.push(s.message);
    if(s.last_started_at) bits.push('台灣時間開始 ' + s.last_started_at);
    if(s.last_finished_at) bits.push('台灣時間完成 ' + s.last_finished_at);
    if(s.auto_open_enabled) bits.push('開頁自動更新 ' + (s.auto_open_mode || 'REAL'));
    if(s.auto_open_last_skipped_reason) bits.push(s.auto_open_last_skipped_reason);
    text.textContent = bits.join('｜') || 'idle';
  }
  async function poll(){
    try{
      const r = await fetch('/api/status', {cache:'no-store'});
      const s = await r.json();
      setStatus(s);
      const active = (s.status === 'running' || s.status === 'queued');
      if(active){
        sawActive = true;
        setTimeout(poll, 2500);
        return;
      }
      if(sawActive && s.status === 'success' && !reloadScheduled){
        reloadScheduled = true;
        text.textContent = '更新完成，正在重新載入最新 dashboard...';
        setTimeout(()=>location.href='/?auto=0', 700);
      }
    }catch(e){ text.textContent = '狀態讀取失敗：' + e; }
  }
  async function run(mode){
    let url = (mode === 'tw') ? '/api/update_tw' : (mode === 'us') ? '/api/update_us' : (mode === 'update_all') ? '/api/update_all' : ('/api/run?mode=' + encodeURIComponent(mode));
    if(mode === 'tw' && !confirm('512MB安全模式：只更新台股行情，再跑正式推薦。要開始嗎？')) return;
    if(mode === 'us' && !confirm('512MB安全模式：只更新美股行情，再跑正式推薦。要開始嗎？')) return;
    if(mode === 'update_all' && !confirm('會更新台股+美股行情並跑正式推薦，免費機可能爆記憶體。要開始嗎？')) return;
    try{
      const r = await fetch(url, {method:'POST'});
      const s = await r.json();
      setStatus(s);
      sawActive = true;
      poll();
    }catch(e){ text.textContent = '啟動失敗：' + e; }
  }
  document.querySelectorAll('[data-v1046-run]').forEach(btn=>{
    btn.addEventListener('click', function(){ run(this.dataset.v1046Run || 'real'); });
  });
  poll();
})();
</script>
"""


def inject_cloud_controls(html: str) -> str:
    if "id=\"v1046-cloudbar\"" in html:
        return html
    css = cloud_css()
    bar = cloud_bar_html()
    js = cloud_script()
    if "</head>" in html:
        html = html.replace("</head>", css + "\n</head>", 1)
    else:
        html = css + html
    html = re.sub(r"<body([^>]*)>", lambda m: f"<body{m.group(1)}>\n{bar}", html, count=1, flags=re.I)
    if bar not in html:
        html = bar + html
    if "</body>" in html:
        html = html.replace("</body>", js + "\n</body>", 1)
    else:
        html += js
    return html


def html_response(html: str, status: int = 200) -> Response:
    return Response(html, status=status, content_type="text/html; charset=utf-8")


def landing_page() -> str:
    files = important_files()
    rows = "".join(
        f"<tr><td>{folder}</td><td><a href='{url_for('download_file', folder=folder, name=p.name)}'>{p.name}</a></td><td>{p.stat().st_size:,}</td><td>{fmt_mtime(p)}</td></tr>"
        for folder, p in files[:12]
    ) or "<tr><td colspan='4'>尚無輸出檔</td></tr>"
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{APP_NAME}</title>{cloud_css()}<style>
body{{margin:0;background:#eef2f6;color:#243447;font-family:'Microsoft JhengHei',Arial,sans-serif}}.wrap{{max-width:1080px;margin:0 auto;padding:18px}}.card{{background:#fffdf7;border:1px solid #dde4dc;border-radius:18px;margin:14px 0;padding:16px;box-shadow:0 8px 24px rgba(15,23,42,.06)}}h1{{margin:0 0 8px}}.sub{{color:#64748b;line-height:1.7}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border-bottom:1px solid #e2e8d7;padding:8px;text-align:left}}th{{background:#eef1e6}}a{{color:#2563eb;font-weight:800}}
</style></head><body>{cloud_bar_html()}<div class="wrap"><div class="card"><h1>{APP_NAME}</h1><div class="sub">這是雲端 Web 啟動頁。為避免 Render 512MB 記憶體爆掉，開頁預設不自動重跑；Render 512MB 版本已拆成兩段：先按「更新台股行情+正式推薦」，再按「更新美股行情+正式推薦」；平常只想重跑推薦，就按「只跑正式推薦」。</div></div><div class="card"><h2>目前檔案</h2><table><thead><tr><th>資料夾</th><th>檔案</th><th>大小</th><th>更新時間</th></tr></thead><tbody>{rows}</tbody></table></div></div>{cloud_script()}</body></html>"""


@app.route("/")
def index():
    maybe_auto_run_on_open()
    if DASHBOARD.exists():
        html = DASHBOARD.read_text(encoding="utf-8", errors="replace")
        return html_response(inject_cloud_controls(html))
    return html_response(landing_page())


@app.route("/dashboard")
def dashboard():
    return redirect(url_for("index"))


@app.route("/run", methods=["GET", "POST"])
def run_daily():
    if not check_token():
        abort(403)
    demo = request.args.get("demo", "0").lower() in {"1", "true", "yes"}
    start_run(demo=demo)
    return redirect(url_for("index"))


@app.route("/demo", methods=["GET", "POST"])
def run_demo():
    if not check_token():
        abort(403)
    start_run(demo=True)
    return redirect(url_for("index"))


@app.route("/api/run", methods=["GET", "POST"])
def api_run():
    if not check_token():
        return jsonify({"status": "forbidden", "message": "需要正確的 V1046_RUN_TOKEN / RUN_TOKEN"}), 403
    mode = (request.args.get("mode") or request.form.get("mode") or "real").lower()
    demo = mode in {"demo", "test", "1", "true"}
    started, data = start_run(demo=demo)
    data["started"] = started
    if not started:
        data["message"] = "已有任務執行中，沒有重複啟動"
    return jsonify(data)




@app.route("/update_all", methods=["GET", "POST"])
def update_all_page():
    if not check_token():
        abort(403)
    start_update_all_then_run()
    return redirect(url_for("index"))


@app.route("/api/update_all", methods=["GET", "POST"])
def api_update_all():
    if not check_token():
        return jsonify({"status": "forbidden", "message": "需要正確的 V1046_RUN_TOKEN / RUN_TOKEN"}), 403
    started, data = start_update_all_then_run()
    data["started"] = started
    data["action"] = "update_tw_us_universe_then_real_run"
    if not started:
        data["message"] = "已有任務執行中，沒有重複啟動"
    return jsonify(data)

@app.route("/api/update_tw", methods=["GET", "POST"])
def api_update_tw():
    if not check_token():
        return jsonify({"status": "forbidden", "message": "需要正確的 V1046_RUN_TOKEN / RUN_TOKEN"}), 403
    started, data = start_market_cache_then_run("TW")
    data["started"] = started
    data["action"] = "update_tw_cache_then_real_run"
    if not started:
        data["message"] = "已有任務執行中，沒有重複啟動"
    return jsonify(data)


@app.route("/api/update_us", methods=["GET", "POST"])
def api_update_us():
    if not check_token():
        return jsonify({"status": "forbidden", "message": "需要正確的 V1046_RUN_TOKEN / RUN_TOKEN"}), 403
    started, data = start_market_cache_then_run("US")
    data["started"] = started
    data["action"] = "update_us_cache_then_real_run"
    if not started:
        data["message"] = "已有任務執行中，沒有重複啟動"
    return jsonify(data)


@app.route("/api/status")
def api_status():
    return jsonify(_job_snapshot())


@app.route("/api/sheets/status")
def api_sheets_status():
    if get_sheets_status is None:
        return jsonify({"enabled": False, "configured": False, "error": "v1046_gs_sync import failed"})
    check = request.args.get("check", "0").strip().lower() in {"1", "true", "yes"}
    return jsonify(get_sheets_status(light=not check))


@app.route("/api/drive/status")
def api_drive_status():
    if get_drive_status is None:
        return jsonify({"enabled": False, "configured": False, "error": "v1046_gs_sync import failed"})
    check = request.args.get("check", "0").strip().lower() in {"1", "true", "yes"}
    return jsonify(get_drive_status(light=not check))


@app.route("/sheets")
def sheets_page():
    status = get_sheets_status(light=True) if get_sheets_status is not None else {"enabled": False, "configured": False, "error": "v1046_gs_sync import failed"}
    url = status.get("sheet_url") or ""
    sa = status.get("service_account_email") or ""
    rows = "".join(
        f"<tr><th>{k}</th><td>{v}</td></tr>"
        for k, v in status.items()
        if k not in {"sheet_url"}
    )
    sheet_link = f"<a href='{url}' target='_blank' rel='noopener'>打開 Google Sheet</a>" if url else "尚未設定 V1046_GOOGLE_SHEET_ID"
    share_hint = f"把這個 service account 加到 Google Sheet 共用名單：<b>{sa}</b>，權限選 Editor。" if sa else "尚未讀到 service account email，請確認 GOOGLE_SERVICE_ACCOUNT_JSON_B64。"
    html = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{APP_NAME}｜Google Sheets</title>{cloud_css()}<style>
body{{margin:0;background:#eef2f6;color:#243447;font-family:'Microsoft JhengHei',Arial,sans-serif}}.wrap{{max-width:1080px;margin:0 auto;padding:18px}}.card{{background:#fffdf7;border:1px solid #dde4dc;border-radius:18px;margin:14px 0;padding:16px;box-shadow:0 8px 24px rgba(15,23,42,.06)}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border-bottom:1px solid #e2e8d7;padding:8px;text-align:left;vertical-align:top}}th{{background:#eef1e6;width:240px}}a{{color:#2563eb;font-weight:800}}code{{background:#e2e8f0;border-radius:8px;padding:2px 6px}}.ok{{color:#15803d;font-weight:900}}.warn{{color:#b45309;font-weight:900}}
</style></head><body>{cloud_bar_html()}<div class="wrap"><div class="card"><h1>Google Sheets 同步狀態</h1><p>{sheet_link}</p><p>{share_hint}</p><table>{rows}</table><p><a href="{url_for('api_sheets_status')}?check=1">連線測試 /api/sheets/status?check=1</a></p></div><div class="card"><h2>同步內容</h2><p><b>state</b>：positions、closed_trades、equity_curve、signal_ledger。這是紙上交易帳本，開頁更新前會先從 Sheets 抓，跑完再回存。</p><p><b>output</b>：今日推薦、風控、監控摘要、FILTER/NO_REC 相關表格。dashboard HTML 不塞進 Sheets，仍由 Render 產生。</p><p><b>run_lock</b>：避免兩個瀏覽器同時打開造成互相覆蓋。</p></div></div>{cloud_script()}</body></html>"""
    return html_response(html)


@app.route("/api/files")
def api_files():
    out = []
    for folder, p in important_files():
        out.append({
            "folder": folder,
            "name": p.name,
            "size": p.stat().st_size,
            "modified_at": fmt_mtime(p),
            "url": url_for("download_file", folder=folder, name=p.name),
        })
    return jsonify({"files": out})


@app.route("/files")
def files_page():
    rows = "".join(
        f"<tr><td>{folder}</td><td><a href='{url_for('download_file', folder=folder, name=p.name)}'>{p.name}</a></td><td>{p.stat().st_size:,}</td><td>{fmt_mtime(p)}</td></tr>"
        for folder, p in important_files()
    ) or "<tr><td colspan='4'>尚無檔案</td></tr>"
    html = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{APP_NAME}｜檔案</title>{cloud_css()}<style>
body{{margin:0;background:#eef2f6;color:#243447;font-family:'Microsoft JhengHei',Arial,sans-serif}}.wrap{{max-width:1080px;margin:0 auto;padding:18px}}.card{{background:#fffdf7;border:1px solid #dde4dc;border-radius:18px;margin:14px 0;padding:16px;box-shadow:0 8px 24px rgba(15,23,42,.06)}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border-bottom:1px solid #e2e8d7;padding:8px;text-align:left}}th{{background:#eef1e6}}a{{color:#2563eb;font-weight:800}}
</style></head><body>{cloud_bar_html()}<div class="wrap"><div class="card"><h1>檔案下載</h1><table><thead><tr><th>資料夾</th><th>檔案</th><th>大小</th><th>更新時間</th></tr></thead><tbody>{rows}</tbody></table></div></div>{cloud_script()}</body></html>"""
    return html_response(html)


@app.route("/download/<folder>/<path:name>")
def download_file(folder: str, name: str):
    return safe_send(folder, name)


@app.route("/output/<path:name>")
def output_file(name: str):
    return safe_send("output", name)


@app.route("/state/<path:name>")
def state_file(name: str):
    return safe_send("state", name)


@app.route("/health")
def health():
    if HEALTH_REPORT.exists():
        html = HEALTH_REPORT.read_text(encoding="utf-8", errors="replace")
        return html_response(inject_cloud_controls(html))
    return html_response(f"<!doctype html><html><head><meta charset='utf-8'><title>{APP_NAME} Health</title>{cloud_css()}</head><body>{cloud_bar_html()}<div style='font-family:Microsoft JhengHei,Arial;padding:20px'>OK｜尚未產生健康檢查檔</div>{cloud_script()}</body></html>")




def _read_csv_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        import csv
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        return max(len(rows)-1, 0)
    except Exception:
        return -1


def _file_mtime(path: Path) -> str:
    return fmt_mtime(path)


def build_full_health_payload() -> Dict[str, object]:
    settings_path = CONFIG / "settings.json"
    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception as exc:
            settings = {"error": f"settings read failed: {type(exc).__name__}: {exc}"}
    status = _job_snapshot()
    sheets = {}
    if get_sheets_status is not None:
        try:
            sheets = get_sheets_status(light=False)
        except Exception as exc:
            sheets = {"enabled": False, "error": f"{type(exc).__name__}: {exc}"}
    drive = {}
    if get_drive_status is not None:
        try:
            drive = get_drive_status(light=False)
        except Exception as exc:
            drive = {"enabled": False, "error": f"{type(exc).__name__}: {exc}"}
    files = {
        "dashboard": str(DASHBOARD.relative_to(ROOT)) if DASHBOARD.exists() else "",
        "health_report": str(HEALTH_REPORT.relative_to(ROOT)) if HEALTH_REPORT.exists() else "",
        "positions_rows": _read_csv_count(STATE / "v1046_paper_positions.csv"),
        "closed_trades_rows": _read_csv_count(STATE / "v1046_paper_closed_trades.csv"),
        "signal_ledger_rows": _read_csv_count(STATE / "v1046_daily_signal_ledger.csv"),
        "today_recommendations_rows": _read_csv_count(OUTPUT / "v1046_today_recommendations.csv"),
        "today_action_summary_rows": _read_csv_count(OUTPUT / "v1046_today_action_summary.csv"),
        "positions_mtime": _file_mtime(STATE / "v1046_paper_positions.csv"),
        "today_action_summary_mtime": _file_mtime(OUTPUT / "v1046_today_action_summary.csv"),
        "health_json_mtime": _file_mtime(OUTPUT / "v1046_full_health.json"),
    }
    return {
        "ok": True,
        "generated_at_taipei": now_ts(),
        "server_utc": utc_ts(),
        "timezone": "Asia/Taipei",
        "app": APP_NAME,
        "standard_mode_only": True,
        "rules": {
            "TW": "H80 / max 3 / stop -10% / loss1 dynamic cooldown max 21 days / TWII or TWOII above MA60 release",
            "US": "H70 / max 2 / ret20 <= 42.5% / QQQ > MA100 / stop -10% / no cooldown",
            "capital": "TW20 / US80",
        },
        "settings": settings,
        "job": status,
        "google_sheets": sheets,
        "google_drive": drive,
        "files": files,
    }


@app.route("/api/health_full")
def api_health_full():
    return jsonify(build_full_health_payload())


@app.route("/health_full")
def health_full():
    payload = build_full_health_payload()
    rows = "".join(f"<tr><th>{k}</th><td><pre>{json.dumps(v, ensure_ascii=False, indent=2)}</pre></td></tr>" for k, v in payload.items())
    html = f"""<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{APP_NAME}｜完整健康檢查</title>{cloud_css()}<style>
body{{margin:0;background:#eef2f6;color:#243447;font-family:'Microsoft JhengHei',Arial,sans-serif}}.wrap{{max-width:1180px;margin:0 auto;padding:18px}}.card{{background:#fffdf7;border:1px solid #dde4dc;border-radius:18px;margin:14px 0;padding:16px;box-shadow:0 8px 24px rgba(15,23,42,.06)}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border-bottom:1px solid #e2e8d7;padding:8px;text-align:left;vertical-align:top}}th{{background:#eef1e6;width:220px}}pre{{white-space:pre-wrap;margin:0;font-family:Consolas,monospace;font-size:12px}}a{{color:#2563eb;font-weight:800}}
</style></head><body>{cloud_bar_html()}<div class='wrap'><div class='card'><h1>完整健康檢查</h1><p>標準模式固定：台股 H80 動態冷靜期，美股 H70 融合版，資金 TW20/US80。</p><p><a href='/?auto=0'>回正式推薦頁</a> ｜ <a href='{url_for('api_health_full')}'>JSON /api/health_full</a></p><table>{rows}</table></div></div>{cloud_script()}</body></html>"""
    return html_response(html)

@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "app": APP_NAME, "time": now_ts()})


@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
