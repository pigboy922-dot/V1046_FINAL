# -*- coding: utf-8 -*-
"""Google Sheets sync for V104.6 cloud version.

Purpose
- Google Sheets becomes the long-term storage for state and table-style output.
- Render local state/output stay as temporary working files only.
- Opening the web page can pull state from Sheets, run the strategy, then push updated
  state/output back to Sheets.

Environment variables
- V1046_SHEETS_ENABLED=true
- V1046_GOOGLE_SHEET_ID=<spreadsheet id>
- V1046_GOOGLE_SERVICE_ACCOUNT_JSON=<service account json, raw or base64>
  or V1046_GOOGLE_SERVICE_ACCOUNT_FILE=<path to json file>
- V1046_SHEETS_STATE_ENABLED=true
- V1046_SHEETS_OUTPUT_ENABLED=true
- V1046_SHEETS_LOCK_ENABLED=true
- V1046_SHEETS_LOCK_TTL_SECONDS=900
"""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
OUTPUT_DIR = ROOT / "output"
LOG_DIR = ROOT / "logs"
BACKUP_DIR = STATE_DIR / "backups"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
for p in [STATE_DIR, OUTPUT_DIR, LOG_DIR, BACKUP_DIR]:
    p.mkdir(parents=True, exist_ok=True)

STATE_FILES: Dict[str, Path] = {
    "state_positions": STATE_DIR / "v1046_paper_positions.csv",
    "state_closed_trades": STATE_DIR / "v1046_paper_closed_trades.csv",
    "state_equity_curve": STATE_DIR / "v1046_paper_equity_curve.csv",
    "state_signal_ledger": STATE_DIR / "v1046_daily_signal_ledger.csv",
}

# Only table-style output goes to Sheets. HTML dashboard/reports stay on Render.
OUTPUT_FILES: Dict[str, Path] = {
    "output_today_recommendations": OUTPUT_DIR / "v1046_today_recommendations.csv",
    "output_today_recommendations_simple": OUTPUT_DIR / "v1046_today_recommendations_SIMPLE.csv",
    "output_today_tw_recommendations": OUTPUT_DIR / "today_tw_recommendations.csv",
    "output_today_us_recommendations": OUTPUT_DIR / "today_us_recommendations.csv",
    "output_monitor_summary": OUTPUT_DIR / "v1046_paper_monitor_summary.csv",
    "output_risk_guard_status": OUTPUT_DIR / "v1046_risk_guard_status.csv",
    "output_today_action_summary": OUTPUT_DIR / "v1046_today_action_summary.csv",
    "output_full_health_json": OUTPUT_DIR / "v1046_full_health.json",
    "output_risk_latest_prices": OUTPUT_DIR / "v1046_risk_latest_prices.csv",
    "output_tw_nearest_failed": OUTPUT_DIR / "v1046_tw_nearest_failed_candidates.csv",
    "output_us_nearest_failed": OUTPUT_DIR / "v1046_us_nearest_failed_candidates.csv",
}

RUN_LOCK_SHEET = "_run_lock"
RUN_LOG_SHEET = "run_log"
HEALTH_SHEET = "output_health_log"
NO_REC_SHEET = "output_no_rec_reason"
NO_REC_PATH = OUTPUT_DIR / "v1046_no_recommendation_reason.txt"


def now_ts() -> str:
    return datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")


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
    except Exception:
        return default


def sheet_id() -> str:
    return (os.environ.get("V1046_GOOGLE_SHEET_ID") or os.environ.get("GOOGLE_SHEET_ID") or "").strip()


def service_account_email() -> str:
    raw = os.environ.get("V1046_GOOGLE_SERVICE_ACCOUNT_JSON") or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or ""
    path = os.environ.get("V1046_GOOGLE_SERVICE_ACCOUNT_FILE") or os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or ""
    try:
        info = _parse_service_account_json(raw) if raw else None
        if info:
            return str(info.get("client_email", ""))
        if path and Path(path).exists():
            info = json.loads(Path(path).read_text(encoding="utf-8"))
            return str(info.get("client_email", ""))
    except Exception:
        return ""
    return ""


def configured() -> bool:
    return bool(sheet_id()) and bool(
        os.environ.get("V1046_GOOGLE_SERVICE_ACCOUNT_JSON")
        or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        or os.environ.get("V1046_GOOGLE_SERVICE_ACCOUNT_FILE")
        or os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    )


def sheets_enabled() -> bool:
    # Explicit true is preferred. Auto-enable when sheet id + credentials exist to reduce setup mistakes.
    return env_bool("V1046_SHEETS_ENABLED", False) or configured()


def state_enabled() -> bool:
    return env_bool("V1046_SHEETS_STATE_ENABLED", True)


def output_enabled() -> bool:
    return env_bool("V1046_SHEETS_OUTPUT_ENABLED", True)


def lock_enabled() -> bool:
    return env_bool("V1046_SHEETS_LOCK_ENABLED", True)


def masked_sheet_id() -> str:
    sid = sheet_id()
    if not sid:
        return ""
    if len(sid) <= 10:
        return "*" * len(sid)
    return sid[:6] + "..." + sid[-4:]


def public_sheet_url() -> str:
    sid = sheet_id()
    return f"https://docs.google.com/spreadsheets/d/{sid}" if sid else ""


def get_sheets_status(light: bool = True) -> Dict[str, object]:
    status = {
        "enabled": sheets_enabled(),
        "configured": configured(),
        "sheet_id_masked": masked_sheet_id(),
        "sheet_url": public_sheet_url(),
        "service_account_email": service_account_email(),
        "state_enabled": state_enabled(),
        "output_enabled": output_enabled(),
        "lock_enabled": lock_enabled(),
    }
    if not light and sheets_enabled():
        try:
            ss = _spreadsheet()
            status["connect_ok"] = True
            status["title"] = getattr(ss, "title", "")
            status["worksheets"] = [w.title for w in ss.worksheets()]
        except Exception as exc:
            status["connect_ok"] = False
            status["error"] = f"{type(exc).__name__}: {exc}"
    return status


def _parse_service_account_json(raw: str) -> Dict:
    raw = raw.strip()
    if not raw:
        raise RuntimeError("empty service account json")
    # Render env vars may contain raw JSON or base64-encoded JSON.
    if raw.startswith("{"):
        return json.loads(raw)
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        # Some users paste JSON with escaped newlines; try once more as JSON string content.
        return json.loads(raw.replace("\\n", "\n"))


def _client():
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except Exception as exc:
        raise RuntimeError("缺少 gspread/google-auth，請確認 requirements.txt 已部署") from exc

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
    ]
    raw = os.environ.get("V1046_GOOGLE_SERVICE_ACCOUNT_JSON") or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or ""
    path = os.environ.get("V1046_GOOGLE_SERVICE_ACCOUNT_FILE") or os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or ""
    if raw:
        info = _parse_service_account_json(raw)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    elif path:
        creds = Credentials.from_service_account_file(path, scopes=scopes)
    else:
        raise RuntimeError("沒有 Google service account credentials")
    return gspread.authorize(creds)


def _spreadsheet():
    sid = sheet_id()
    if not sid:
        raise RuntimeError("沒有 V1046_GOOGLE_SHEET_ID")
    return _client().open_by_key(sid)


def _worksheet(ss, title: str, rows: int = 100, cols: int = 26):
    try:
        return ss.worksheet(title)
    except Exception:
        return ss.add_worksheet(title=title, rows=max(rows, 10), cols=max(cols, 2))


def _csv_rows(path: Path) -> List[List[str]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    return [["" if c is None else str(c) for c in row] for row in reader]




def _backup_local_csv(path: Path, label: str, health: Optional[List[str]] = None) -> None:
    if not path.exists() or not path.is_file():
        return
    try:
        stamp = datetime.now(TAIPEI_TZ).strftime("%Y%m%d-%H%M%S")
        target = BACKUP_DIR / f"{label}_{stamp}_{path.name}"
        target.write_bytes(path.read_bytes())
        if health is not None:
            health.append(f"OK local backup: {path.relative_to(ROOT)} -> {target.relative_to(ROOT)}")
        # keep newest 30 backups per original file label
        backups = sorted(BACKUP_DIR.glob(f"{label}_*_{path.name}"), key=lambda x: x.stat().st_mtime, reverse=True)
        for old in backups[30:]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception as exc:
        if health is not None:
            health.append(f"WARN local backup failed: {path.relative_to(ROOT)} {type(exc).__name__}: {exc}")


def _backup_sheet_values(ss, sheet_name: str, values: List[List[str]], health: Optional[List[str]] = None) -> None:
    if not values or not _has_real_values(values):
        return
    try:
        backup_name = f"backup_{sheet_name}"[:95]
        rows = [["backup_generated_at", now_ts(), "source_sheet", sheet_name], []] + values
        _upload_rows(_worksheet(ss, backup_name, rows=max(len(rows), 10), cols=max(max((len(r) for r in rows), default=1), 2)), rows)
        if health is not None:
            health.append(f"OK Google Sheets backup: {sheet_name} -> {backup_name} rows={max(len(values)-1,0)}")
    except Exception as exc:
        if health is not None:
            health.append(f"WARN Google Sheets backup failed: {sheet_name} {type(exc).__name__}: {exc}")

def _write_csv_rows(path: Path, rows: List[List[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerows(rows)


def _sheet_values(ws) -> List[List[str]]:
    values = ws.get_all_values()
    # Trim fully empty trailing rows to avoid making local CSV huge.
    while values and not any(str(c).strip() for c in values[-1]):
        values.pop()
    return values


def _has_real_values(values: List[List[str]]) -> bool:
    if not values:
        return False
    if len(values) == 1 and not any(str(c).strip() for c in values[0]):
        return False
    return True


def _resize_for_rows(ws, rows: List[List[str]]) -> None:
    try:
        nrows = max(len(rows), 10)
        ncols = max(max((len(r) for r in rows), default=1), 2)
        ws.resize(rows=nrows, cols=ncols)
    except Exception:
        pass


def _upload_rows(ws, rows: List[List[str]]) -> None:
    ws.clear()
    if not rows:
        return
    _resize_for_rows(ws, rows)
    ws.update("A1", rows, value_input_option="RAW")


def _pull_or_seed_csv(ss, sheet_name: str, local_path: Path, health: List[str]) -> str:
    ws = _worksheet(ss, sheet_name)
    values = _sheet_values(ws)
    if _has_real_values(values):
        _backup_local_csv(local_path, f"before_pull_{sheet_name}", health)
        _write_csv_rows(local_path, values)
        health.append(f"OK Google Sheets → local: {sheet_name} -> {local_path.relative_to(ROOT)} rows={max(len(values)-1,0)}")
        return "pulled"
    rows = _csv_rows(local_path)
    if rows:
        _upload_rows(ws, rows)
        health.append(f"OK local seed → Google Sheets: {local_path.relative_to(ROOT)} -> {sheet_name} rows={max(len(rows)-1,0)}")
        return "seeded"
    health.append(f"INFO Google Sheets empty and local missing: {sheet_name}")
    return "empty"


def _push_csv(ss, sheet_name: str, local_path: Path, health: List[str]) -> str:
    if not local_path.exists():
        health.append(f"INFO skip Sheets push missing file: {local_path.relative_to(ROOT)}")
        return "missing"
    rows = _csv_rows(local_path)
    ws = _worksheet(ss, sheet_name, rows=max(len(rows), 10), cols=max(max((len(r) for r in rows), default=1), 2))
    try:
        existing = _sheet_values(ws)
        _backup_sheet_values(ss, sheet_name, existing, health)
    except Exception as exc:
        health.append(f"WARN existing sheet backup skipped: {sheet_name} {type(exc).__name__}: {exc}")
    _backup_local_csv(local_path, f"before_push_{sheet_name}", health)
    _upload_rows(ws, rows)
    health.append(f"OK local → Google Sheets: {local_path.relative_to(ROOT)} -> {sheet_name} rows={max(len(rows)-1,0)}")
    return "pushed"


def _epoch_from_text(s: str) -> float:
    if not s:
        return 0.0
    try:
        return float(s)
    except Exception:
        pass
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0.0


def _lock_rows_dict(rows: List[List[str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for row in rows[1:]:
        if len(row) >= 2 and row[0]:
            out[str(row[0])] = str(row[1])
    return out


def acquire_lock(ss, run_id: str, demo: bool, health: List[str]) -> None:
    if not lock_enabled():
        return
    ws = _worksheet(ss, RUN_LOCK_SHEET, rows=10, cols=4)
    rows = _sheet_values(ws)
    lock = _lock_rows_dict(rows)
    ttl = max(60, env_int("V1046_SHEETS_LOCK_TTL_SECONDS", 900))
    status = lock.get("status", "")
    owner = lock.get("owner", "")
    updated_epoch = _epoch_from_text(lock.get("updated_epoch", ""))
    age = time.time() - updated_epoch if updated_epoch else 10**9
    if status == "running" and owner != run_id and age < ttl:
        raise RuntimeError(f"Google Sheets run_lock active: owner={owner}, age={int(age)}s, ttl={ttl}s")
    now_epoch = str(time.time())
    new_rows = [
        ["key", "value", "updated_at"],
        ["status", "running", now_ts()],
        ["owner", run_id, now_ts()],
        ["mode", "DEMO" if demo else "REAL", now_ts()],
        ["updated_epoch", now_epoch, now_ts()],
        ["ttl_seconds", str(ttl), now_ts()],
    ]
    _upload_rows(ws, new_rows)
    health.append(f"OK Google Sheets run_lock acquired: {run_id}")


def release_lock(ss, run_id: str, status: str, health: Optional[List[str]] = None) -> None:
    if not lock_enabled():
        return
    try:
        ws = _worksheet(ss, RUN_LOCK_SHEET, rows=10, cols=4)
        rows = _sheet_values(ws)
        lock = _lock_rows_dict(rows)
        owner = lock.get("owner", "")
        # Do not overwrite someone else's active lock.
        if owner and owner != run_id:
            if health is not None:
                health.append(f"WARN Google Sheets run_lock owner changed; skip release owner={owner}")
            return
        new_rows = [
            ["key", "value", "updated_at"],
            ["status", "idle", now_ts()],
            ["owner", run_id, now_ts()],
            ["last_status", status, now_ts()],
            ["updated_epoch", str(time.time()), now_ts()],
        ]
        _upload_rows(ws, new_rows)
        if health is not None:
            health.append(f"OK Google Sheets run_lock released: {status}")
    except Exception as exc:
        if health is not None:
            health.append(f"WARN Google Sheets run_lock release failed: {type(exc).__name__}: {exc}")


def sync_state_from_sheets(ss, health: List[str]) -> None:
    if not state_enabled():
        health.append("INFO Google Sheets state sync disabled")
        return
    for sheet_name, path in STATE_FILES.items():
        _pull_or_seed_csv(ss, sheet_name, path, health)


def sync_state_to_sheets(ss, health: List[str]) -> None:
    if not state_enabled():
        return
    for sheet_name, path in STATE_FILES.items():
        _push_csv(ss, sheet_name, path, health)


def sync_output_to_sheets(ss, health: List[str]) -> None:
    if not output_enabled():
        health.append("INFO Google Sheets output sync disabled")
        return
    for sheet_name, path in OUTPUT_FILES.items():
        _push_csv(ss, sheet_name, path, health)
    # TXT output becomes a small one-column worksheet.
    if NO_REC_PATH.exists():
        text = NO_REC_PATH.read_text(encoding="utf-8", errors="replace")
        rows = [["generated_at", "message"], [now_ts(), text.strip()]]
        _upload_rows(_worksheet(ss, NO_REC_SHEET, rows=10, cols=2), rows)
        health.append(f"OK local → Google Sheets: {NO_REC_PATH.relative_to(ROOT)} -> {NO_REC_SHEET}")
    # Current run health list also goes to a worksheet for quick debugging.
    health_rows = [["generated_at", "level", "message"]]
    for msg in health:
        level = "OK" if str(msg).startswith("OK") else ("WARN" if str(msg).startswith("WARN") else "INFO")
        health_rows.append([now_ts(), level, str(msg)])
    _upload_rows(_worksheet(ss, HEALTH_SHEET, rows=max(len(health_rows), 10), cols=3), health_rows)


def append_run_log(ss, run_id: str, demo: bool, status: str, error: str = "") -> None:
    ws = _worksheet(ss, RUN_LOG_SHEET, rows=200, cols=8)
    rows = _sheet_values(ws)
    header = ["run_id", "finished_at", "mode", "status", "error", "sheet_sync_version", "app"]
    if not rows:
        rows = [header]
    elif rows[0] != header:
        rows = [header] + rows[1:]
    rows.append([run_id, now_ts(), "DEMO" if demo else "REAL", status, (error or "")[:1000], "gs-sync-v2-safe-backup", "V104.6"])
    # Keep run log bounded.
    rows = [rows[0]] + rows[-300:]
    _upload_rows(ws, rows)


def sync_before_run(health: List[str], demo: bool = False) -> Optional[Dict[str, object]]:
    if not sheets_enabled():
        health.append("INFO Google Sheets sync disabled")
        return None
    run_id = f"{'DEMO' if demo else 'REAL'}-{datetime.now(TAIPEI_TZ).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    ss = _spreadsheet()
    acquire_lock(ss, run_id, demo, health)
    if not demo:
        sync_state_from_sheets(ss, health)
    else:
        health.append("INFO DEMO mode: skip Google Sheets state pull/write")
    return {"run_id": run_id, "spreadsheet": ss, "demo": demo}


def sync_after_run(ctx: Optional[Dict[str, object]], health: List[str], demo: bool, status: str = "success", error: str = "") -> None:
    if not sheets_enabled():
        return
    run_id = (ctx or {}).get("run_id") or f"{'DEMO' if demo else 'REAL'}-{datetime.now(TAIPEI_TZ).strftime('%Y%m%d-%H%M%S')}-noctx"
    ss = (ctx or {}).get("spreadsheet")
    try:
        if ss is None:
            ss = _spreadsheet()
        if not demo and status == "success":
            sync_state_to_sheets(ss, health)
        elif demo:
            health.append("INFO DEMO mode: skip Google Sheets state push")
        sync_output_to_sheets(ss, health)
        append_run_log(ss, str(run_id), demo, status, error)
    finally:
        try:
            if ss is not None:
                release_lock(ss, str(run_id), status, health)
        except Exception:
            pass
