# -*- coding: utf-8 -*-
"""
V104.6 FINAL Fusion Cloud Weekly Stock Guard
- TW: H80 max3 dynamic cooldown with TWII/TWOII MA60 early release.
- US: H70 fusion max2 with QQQ > MA100 market filter.
- Daily data from yfinance / optional TAIFEX fallback.
- Paper trading monitor only; never auto-calibrates core rules.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import math
import os
import random
import sys
import requests
import traceback
from dataclasses import dataclass, asdict
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None

try:
    from v1046_gs_sync import sync_before_run, sync_after_run, get_sheets_status
except Exception:  # pragma: no cover
    sync_before_run = None
    sync_after_run = None
    get_sheets_status = None

APP_VERSION = "V104.6 FINAL Fusion｜TW H80 Dynamic Cooldown + US H70 Fusion｜Capital TW20/US80"
ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
STATE_DIR = ROOT / "state"
OUTPUT_DIR = ROOT / "output"
LOG_DIR = ROOT / "logs"
for p in [CONFIG_DIR, DATA_DIR, STATE_DIR, OUTPUT_DIR, LOG_DIR]:
    p.mkdir(parents=True, exist_ok=True)

POSITIONS_PATH = STATE_DIR / "v1046_paper_positions.csv"
TRADES_PATH = STATE_DIR / "v1046_paper_closed_trades.csv"
EQUITY_PATH = STATE_DIR / "v1046_paper_equity_curve.csv"
TODAY_RECS_PATH = OUTPUT_DIR / "v1046_today_recommendations.csv"
TODAY_RECS_SIMPLE_PATH = OUTPUT_DIR / "v1046_today_recommendations_SIMPLE.csv"
TODAY_TW_RECS_PATH = OUTPUT_DIR / "today_tw_recommendations.csv"
TODAY_US_RECS_PATH = OUTPUT_DIR / "today_us_recommendations.csv"
DASHBOARD_PATH = OUTPUT_DIR / "v1046_paper_dashboard.html"
MONITOR_PATH = OUTPUT_DIR / "v1046_paper_monitor_summary.csv"
CALIBRATION_PATH = OUTPUT_DIR / "v1046_monthly_calibration_report.html"
HEALTH_PATH = OUTPUT_DIR / "v1046_health_report.html"
SIGNAL_LEDGER_PATH = STATE_DIR / "v1046_daily_signal_ledger.csv"
RISK_GUARD_STATUS_PATH = OUTPUT_DIR / "v1046_risk_guard_status.csv"
NO_REC_REASON_PATH = OUTPUT_DIR / "v1046_no_recommendation_reason.txt"
AUTO_UNIVERSE_REPORT_PATH = OUTPUT_DIR / "v1046_auto_universe_report.csv"
TODAY_ACTION_SUMMARY_PATH = OUTPUT_DIR / "v1046_today_action_summary.csv"
FULL_HEALTH_JSON_PATH = OUTPUT_DIR / "v1046_full_health.json"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def now_ts() -> str:
    return datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d")


def safe_float(x, default=np.nan):
    try:
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def fmt_pct(x, nd=2):
    if x is None or pd.isna(x):
        return ""
    return f"{float(x):.{nd}f}%"


def fmt_num(x, nd=4):
    if x is None or pd.isna(x):
        return ""
    return f"{float(x):.{nd}f}"


@dataclass
class RuleProfile:
    profile_id: str
    market: str
    label: str
    hold_days: int
    topn: int
    max_positions: int
    score_min: float
    rule_min: int
    liquidity_min: float
    atr_min: float
    atr_max: float
    dist_ma20_max: float
    dist_ma20_min: float
    ret1_max: float
    ret5_max: float
    ret20_min: float
    ret60_min: float
    upper_shadow_max: float
    volume_ratio_max: float
    volume_ratio_min: float
    require_anti_chase: bool
    require_bull: bool
    entry_mode: str
    risk_mode: str
    stop_loss_pct: float
    trail_stop_pct: float
    rank_mode: str
    recommendation_weekday: int
    buy_timing_note: str


TW_RULE = RuleProfile(
    profile_id="TW_FINAL_H80_MAX3_DYNAMIC_CD",
    market="TW",
    label="台股 FINAL｜全台股掃描｜Top3｜H80｜虧1筆動態冷靜期",
    hold_days=80,
    topn=3,
    max_positions=3,
    score_min=0,
    rule_min=7,
    liquidity_min=100_000_000.0,
    atr_min=0.0,
    atr_max=5.0,
    dist_ma20_max=999.0,
    dist_ma20_min=-999.0,
    ret1_max=99.0,
    ret5_max=0.10,
    ret20_min=0.10,
    ret60_min=0.40,
    upper_shadow_max=1.0,
    volume_ratio_max=999.0,
    volume_ratio_min=0.0,
    require_anti_chase=False,
    require_bull=False,
    entry_mode="mega_mom",
    risk_mode="normal_only",
    stop_loss_pct=0.10,
    trail_stop_pct=0.0,
    rank_mode="mega_mom",
    recommendation_weekday=2,  # Wednesday close data; buy next TW trading day.
    buy_timing_note="台灣時間週三收盤後產生；週四開盤買進",
)

US_RULE = RuleProfile(
    profile_id="US_FINAL_H70_FUSION_QQQ100",
    market="US",
    label="美股 FINAL 融合版｜Top2｜H70｜ret20<=42.5%｜QQQ>100MA",
    hold_days=70,
    topn=2,
    max_positions=2,
    score_min=75,
    rule_min=5,
    liquidity_min=0.0,
    atr_min=2.5,
    atr_max=20.0,
    dist_ma20_max=999.0,
    dist_ma20_min=-999.0,
    ret1_max=99.0,
    ret5_max=99.0,
    ret20_min=0.12,
    ret60_min=0.25,
    upper_shadow_max=1.0,
    volume_ratio_max=999.0,
    volume_ratio_min=0.0,
    require_anti_chase=False,
    require_bull=False,
    entry_mode="mega_mom",
    risk_mode="normal_only",
    stop_loss_pct=0.10,
    trail_stop_pct=0.0,
    rank_mode="mega_mom",
    recommendation_weekday=3,  # US Thursday close data; buy Friday night Taiwan time.
    buy_timing_note="美國週四收盤後產生；台灣時間週五晚上買進",
)

DEFAULT_SETTINGS = {
    "lookback_days": 420,
    "max_signal_age_days": 3,
    "paper_exit_due": True,
    "paper_guard_enabled": True,
    "pause_after_consecutive_losses": 999,
    "monthly_reduce_drawdown_pct": -8.0,
    "monthly_stop_drawdown_pct": -99.0,
    "batch_size_tw": 20,
    "batch_size_us": 30,
    "timezone_note": "Asia/Taipei",
    "daily_cash_unit": 1.0,
    "cost_bps_per_side_monitor": 10,
    "risk_guard_version": "V104.6_FINAL_FUSION_TW20_US80",
    "capital_weight_tw": 0.20,
    "capital_weight_us": 0.80,
    "total_capital": 100000,
    "currency_note": "user_base_currency",
    "tw_loss_streak_n": 1,
    "tw_loss_streak_cooldown_days": 21,
    "tw_dynamic_cooldown_enabled": True,
    "tw_dynamic_cooldown_release_ma": 60,
    "tw_dynamic_cooldown_release_symbols": ["^TWII", "^TWOII"],
    "tw_month_stop_loss_pct": -99.0,
    "us_loss_streak_n": 999,
    "us_loss_streak_cooldown_days": 0,
    "us_require_qqq_above_ma": True,
    "us_qqq_ma_days": 100,
    "auto_universe_enabled": True,
    "auto_universe_on_run": True,
    "auto_universe_lookback_days": 120,
    "auto_universe_min_price_tw": 10.0,
    "auto_universe_min_price_us": 5.0,
    "auto_universe_top_tw": 500,
    "auto_universe_top_us": 450,
    "auto_universe_max_download_tw": 1800,
    "auto_universe_max_download_us": 900,
    "auto_universe_us_mode": "index_plus_seed",
    "final_rule_note": "TW: H80 max3 loss1 cooldown max21d, release when TWII/TWOII > 60MA. US: H70 max2 ret20<=42.5%, QQQ > 100MA. Capital target TW20/US80.",
}

def load_settings() -> Dict:
    path = CONFIG_DIR / "settings.json"
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_SETTINGS, ensure_ascii=False, indent=2), encoding="utf-8")
        return dict(DEFAULT_SETTINGS)
    with path.open("r", encoding="utf-8") as f:
        user = json.load(f)
    s = dict(DEFAULT_SETTINGS)
    s.update(user)
    return s


def default_tw_universe() -> pd.DataFrame:
    rows = [
        ("1101", "台泥", "1101.TW"), ("1216", "統一", "1216.TW"), ("1301", "台塑", "1301.TW"),
        ("1303", "南亞", "1303.TW"), ("1326", "台化", "1326.TW"), ("1590", "亞德客-KY", "1590.TW"),
        ("2002", "中鋼", "2002.TW"), ("2049", "上銀", "2049.TW"), ("2207", "和泰車", "2207.TW"),
        ("2301", "光寶科", "2301.TW"), ("2303", "聯電", "2303.TW"), ("2308", "台達電", "2308.TW"),
        ("2317", "鴻海", "2317.TW"), ("2327", "國巨", "2327.TW"), ("2330", "台積電", "2330.TW"),
        ("2345", "智邦", "2345.TW"), ("2356", "英業達", "2356.TW"), ("2376", "技嘉", "2376.TW"),
        ("2379", "瑞昱", "2379.TW"), ("2382", "廣達", "2382.TW"), ("2395", "研華", "2395.TW"),
        ("2408", "南亞科", "2408.TW"), ("2454", "聯發科", "2454.TW"), ("2474", "可成", "2474.TW"),
        ("2603", "長榮", "2603.TW"), ("2609", "陽明", "2609.TW"), ("2615", "萬海", "2615.TW"),
        ("2881", "富邦金", "2881.TW"), ("2882", "國泰金", "2882.TW"), ("2886", "兆豐金", "2886.TW"),
        ("2891", "中信金", "2891.TW"), ("3008", "大立光", "3008.TW"), ("3017", "奇鋐", "3017.TW"),
        ("3034", "聯詠", "3034.TW"), ("3037", "欣興", "3037.TW"), ("3231", "緯創", "3231.TW"),
        ("3443", "創意", "3443.TW"), ("3661", "世芯-KY", "3661.TW"), ("3711", "日月光投控", "3711.TW"),
        ("4938", "和碩", "4938.TW"), ("5269", "祥碩", "5269.TW"), ("6415", "矽力*-KY", "6415.TW"),
        ("6669", "緯穎", "6669.TW"), ("8046", "南電", "8046.TW"), ("8069", "元太", "8069.TWO"),
    ]
    return pd.DataFrame(rows, columns=["symbol", "name_zh", "yfinance_symbol"])


def default_us_universe() -> pd.DataFrame:
    symbols = [
        ("NVDA", "輝達"), ("AMD", "超微"), ("AVGO", "博通"), ("QCOM", "高通"), ("AMAT", "應用材料"),
        ("ASML", "艾司摩爾"), ("TSM", "台積電ADR"), ("MU", "美光"), ("LRCX", "科林研發"), ("KLAC", "科磊"),
        ("AAPL", "蘋果"), ("MSFT", "微軟"), ("GOOGL", "Alphabet"), ("META", "Meta"), ("AMZN", "亞馬遜"),
        ("TSLA", "特斯拉"), ("NFLX", "Netflix"), ("CRM", "Salesforce"), ("NOW", "ServiceNow"), ("ADBE", "Adobe"),
        ("SNOW", "Snowflake"), ("PANW", "Palo Alto"), ("CRWD", "CrowdStrike"), ("NET", "Cloudflare"), ("DDOG", "Datadog"),
        ("APP", "AppLovin"), ("PLTR", "Palantir"), ("ARM", "Arm"), ("SMCI", "超微電腦"), ("DELL", "戴爾"),
        ("VRT", "Vertiv"), ("ANET", "Arista"), ("ORCL", "甲骨文"), ("INTC", "英特爾"), ("MRVL", "Marvell"),
        ("MELI", "MercadoLibre"), ("SHOP", "Shopify"), ("UBER", "Uber"), ("ABNB", "Airbnb"), ("COIN", "Coinbase"),
        ("HOOD", "Robinhood"), ("XYZ", "Block"), ("PYPL", "PayPal"), ("COST", "Costco"), ("CAT", "Caterpillar"),
        ("GE", "GE Aerospace"), ("CEG", "Constellation Energy"), ("VST", "Vistra"), ("LLY", "禮來"), ("NVO", "諾和諾德"),
        ("VRTX", "Vertex"), ("REGN", "Regeneron"), ("ISRG", "Intuitive Surgical"), ("BKNG", "Booking"), ("AXON", "Axon"),
    ]
    return pd.DataFrame([(s, n, s) for s, n in symbols], columns=["symbol", "name_zh", "yfinance_symbol"])


def ensure_universe_files() -> None:
    twp = CONFIG_DIR / "tw_universe.csv"
    usp = CONFIG_DIR / "us_universe.csv"
    if not twp.exists():
        default_tw_universe().to_csv(twp, index=False, encoding="utf-8-sig")
    if not usp.exists():
        default_us_universe().to_csv(usp, index=False, encoding="utf-8-sig")


def read_universe(market: str) -> pd.DataFrame:
    ensure_universe_files()
    path = CONFIG_DIR / ("tw_universe.csv" if market == "TW" else "us_universe.csv")
    df = pd.read_csv(path, dtype=str).fillna("")
    if "symbol" not in df.columns:
        raise RuntimeError(f"{path} missing symbol column")
    if "name_zh" not in df.columns:
        df["name_zh"] = df["symbol"]
    if "yfinance_symbol" not in df.columns:
        if market == "TW":
            df["yfinance_symbol"] = df["symbol"].astype(str).apply(lambda x: x if "." in x else f"{x}.TW")
        else:
            df["yfinance_symbol"] = df["symbol"]
    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["name_zh"] = df["name_zh"].astype(str).str.strip()
    df["yfinance_symbol"] = df["yfinance_symbol"].astype(str).str.strip()
    # 修正舊 config：元太是上櫃，Yahoo 代號要用 8069.TWO；Block 已由 SQ 改成 XYZ。
    if market == "TW":
        df.loc[df["symbol"].eq("8069"), "yfinance_symbol"] = "8069.TWO"
    else:
        df.loc[df["symbol"].eq("SQ"), "symbol"] = "XYZ"
        df.loc[df["yfinance_symbol"].eq("SQ"), "yfinance_symbol"] = "XYZ"
        df.loc[df["symbol"].eq("XYZ") & df["name_zh"].isin(["", "SQ"]), "name_zh"] = "Block"
    df = df[(df["symbol"] != "") & (df["yfinance_symbol"] != "")].drop_duplicates("symbol")
    try:
        df.to_csv(path, index=False, encoding="utf-8-sig")
    except Exception:
        pass
    return df.reset_index(drop=True)


def _setting_bool(settings: Dict, key: str, default: bool = False) -> bool:
    v = settings.get(key, default)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _http_json(url: str, timeout: int = 20):
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 V1046"})
    r.raise_for_status()
    return r.json()


def _clean_stock_name(name: str) -> str:
    return str(name or "").strip().replace("　", " ")


def _is_bad_tw_name(name: str) -> bool:
    n = _clean_stock_name(name).upper()
    bad_words = ["ETF", "ETN", "權證", "購", "售", "牛", "熊", "特別股", "受益證券", "存託憑證", "KY"]
    return any(x.upper() in n for x in bad_words)


def fetch_tw_company_universe(health: List[str]) -> pd.DataFrame:
    """抓 TWSE/TPEx 公司清單，轉成 Yahoo 可用代號；失敗時回傳空表，主流程會保留舊股票池。"""
    endpoints = [
        ("TWSE", "https://openapi.twse.com.tw/v1/opendata/t187ap03_L", ".TW"),
        ("TPEx", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O", ".TWO"),
        ("TPEx", "https://www.tpex.org.tw/openapi/v1/t187ap03_O", ".TWO"),
    ]
    rows = []
    for src, url, suffix in endpoints:
        try:
            js = _http_json(url)
            if not isinstance(js, list):
                continue
            for x in js:
                code = str(x.get("公司代號") or x.get("出表日期") and "" or x.get("Code") or x.get("SecuritiesCompanyCode") or "").strip()
                name = _clean_stock_name(x.get("公司名稱") or x.get("公司簡稱") or x.get("Name") or x.get("CompanyName") or "")
                if not code.isdigit() or len(code) != 4 or not name or _is_bad_tw_name(name):
                    continue
                rows.append({"symbol": code, "name_zh": name, "yfinance_symbol": f"{code}{suffix}", "source": src})
            health.append(f"AUTO_UNIVERSE TW source {src} rows={len(rows)}")
        except Exception as e:
            health.append(f"WARN AUTO_UNIVERSE TW source failed {src}: {type(e).__name__}: {e}")
    if not rows:
        return pd.DataFrame(columns=["symbol", "name_zh", "yfinance_symbol"])
    df = pd.DataFrame(rows).drop_duplicates("symbol")
    return df[["symbol", "name_zh", "yfinance_symbol"]].reset_index(drop=True)


def _read_pipe_table(text: str) -> pd.DataFrame:
    lines = [ln for ln in text.splitlines() if "|" in ln and not ln.startswith("File Creation Time")]
    if len(lines) < 2:
        return pd.DataFrame()
    cols = lines[0].split("|")
    data = [ln.split("|") for ln in lines[1:] if len(ln.split("|")) == len(cols)]
    return pd.DataFrame(data, columns=cols)


def fetch_us_company_universe(settings: Dict, health: List[str]) -> pd.DataFrame:
    """美股股票池。預設用 S&P500 + Nasdaq100 + 原本種子，避免全市場掃描拖垮 Render。"""
    rows = []
    # 原始收益版種子永遠保留，避免自動更新失敗或指數名單抓不到時功能失效。
    try:
        seed = default_us_universe()
        seed["source"] = "seed"
        rows.append(seed)
    except Exception:
        pass
    # Wikipedia 指數成分，能自動跟著大成分股調整。
    wiki_sources = [
        ("sp500", "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"),
        ("nasdaq100", "https://en.wikipedia.org/wiki/Nasdaq-100"),
    ]
    for src, url in wiki_sources:
        try:
            tables = pd.read_html(url)
            got = []
            for t in tables:
                cols = {str(c).lower(): c for c in t.columns}
                sym_col = None
                name_col = None
                for k, c in cols.items():
                    if "symbol" in k or "ticker" in k:
                        sym_col = c
                    if "security" in k or "company" in k:
                        name_col = c
                if sym_col is not None:
                    tmp = pd.DataFrame({"symbol": t[sym_col].astype(str).str.replace(".", "-", regex=False).str.strip()})
                    tmp["name_zh"] = t[name_col].astype(str).str.strip() if name_col is not None else tmp["symbol"]
                    tmp["yfinance_symbol"] = tmp["symbol"]
                    tmp["source"] = src
                    got.append(tmp)
            if got:
                rows.append(pd.concat(got, ignore_index=True))
                health.append(f"AUTO_UNIVERSE US source {src} rows={sum(len(x) for x in got)}")
        except Exception as e:
            health.append(f"WARN AUTO_UNIVERSE US wiki source failed {src}: {type(e).__name__}: {e}")
    # aggressive 模式才抓全美普通股名單，避免每次部署跑太久。
    if str(settings.get("auto_universe_us_mode", "index_plus_seed")).lower() in {"all", "aggressive", "all_common"}:
        for src, url in [
            ("nasdaqlisted", "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"),
            ("otherlisted", "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"),
        ]:
            try:
                txt = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0 V1046"}).text
                t = _read_pipe_table(txt)
                if t.empty:
                    continue
                if "Symbol" in t.columns:
                    sym = t["Symbol"]
                    name = t.get("Security Name", sym)
                    etf = t.get("ETF", "N")
                    test = t.get("Test Issue", "N")
                else:
                    sym = t.get("ACT Symbol", pd.Series(dtype=str))
                    name = t.get("Security Name", sym)
                    etf = t.get("ETF", "N")
                    test = t.get("Test Issue", "N")
                tmp = pd.DataFrame({"symbol": sym.astype(str).str.replace(".", "-", regex=False).str.strip(), "name_zh": name.astype(str).str.strip()})
                tmp["ETF"] = etf if hasattr(etf, "__len__") else "N"
                tmp["Test Issue"] = test if hasattr(test, "__len__") else "N"
                tmp = tmp[(tmp["ETF"].astype(str) == "N") & (tmp["Test Issue"].astype(str) == "N")]
                bad = tmp["name_zh"].str.upper().str.contains("WARRANT|RIGHT|UNIT|PREFERRED|DEPOSITARY|NOTE|ETF", regex=True, na=False)
                tmp = tmp[~bad]
                tmp["yfinance_symbol"] = tmp["symbol"]
                tmp["source"] = src
                rows.append(tmp[["symbol", "name_zh", "yfinance_symbol", "source"]])
                health.append(f"AUTO_UNIVERSE US source {src} rows={len(tmp)}")
            except Exception as e:
                health.append(f"WARN AUTO_UNIVERSE US nasdaqtrader source failed {src}: {type(e).__name__}: {e}")
    if not rows:
        return pd.DataFrame(columns=["symbol", "name_zh", "yfinance_symbol"])
    df = pd.concat(rows, ignore_index=True)
    df = df[(df["symbol"] != "") & (~df["symbol"].str.contains(r"\^|/| ", regex=True, na=False))]
    return df[["symbol", "name_zh", "yfinance_symbol"]].drop_duplicates("symbol").reset_index(drop=True)


def rank_universe_by_recent_momentum(candidates: pd.DataFrame, market: str, settings: Dict, health: List[str]) -> pd.DataFrame:
    if candidates is None or candidates.empty or yf is None:
        return pd.DataFrame(columns=["symbol", "name_zh", "yfinance_symbol"])
    max_dl = int(settings.get("auto_universe_max_download_tw" if market == "TW" else "auto_universe_max_download_us", 800))
    top_n = int(settings.get("auto_universe_top_tw" if market == "TW" else "auto_universe_top_us", 350))
    min_price = float(settings.get("auto_universe_min_price_tw" if market == "TW" else "auto_universe_min_price_us", 5.0))
    lookback = int(settings.get("auto_universe_lookback_days", 120))
    c = candidates.drop_duplicates("symbol").head(max_dl).copy()
    tickers = c["yfinance_symbol"].tolist()
    parts = []
    bsz = 40 if market == "TW" else 60
    for i in range(0, len(tickers), bsz):
        batch = tickers[i:i+bsz]
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                raw = yf.download(" ".join(batch), period=f"{lookback}d", interval="1d", group_by="column", auto_adjust=False, progress=False, threads=True, repair=False)
            part = normalize_download(raw, batch)
            if not part.empty:
                parts.append(part)
        except Exception as e:
            health.append(f"WARN AUTO_UNIVERSE {market} rank batch failed {batch[:3]}: {type(e).__name__}: {e}")
    if not parts:
        return pd.DataFrame(columns=["symbol", "name_zh", "yfinance_symbol"])
    d = pd.concat(parts, ignore_index=True).sort_values(["yfinance_symbol", "date"])
    ranked = []
    for yf_sym, g in d.groupby("yfinance_symbol"):
        g = g.dropna(subset=["close"]).sort_values("date")
        if len(g) < 65:
            continue
        close = g["close"].astype(float)
        vol = g["volume"].astype(float).replace(0, np.nan)
        last = close.iloc[-1]
        if not np.isfinite(last) or last < min_price:
            continue
        ret20 = close.iloc[-1] / close.iloc[-21] - 1 if len(close) > 21 and close.iloc[-21] else np.nan
        ret60 = close.iloc[-1] / close.iloc[-61] - 1 if len(close) > 61 and close.iloc[-61] else np.nan
        dv20 = (close * vol).tail(20).mean()
        high60 = close.tail(60).max()
        near60 = last / high60 - 1 if high60 else np.nan
        score = (np.nan_to_num(ret20) * 300) + (np.nan_to_num(ret60) * 150) + (0 if not np.isfinite(dv20) else math.log10(max(dv20, 1)) * 4) + max(0, (0.08 + np.nan_to_num(near60)) * 100)
        ranked.append({"yfinance_symbol": yf_sym, "auto_score": score, "auto_ret20": ret20, "auto_ret60": ret60, "auto_dollar_volume20": dv20, "auto_last_close": last})
    if not ranked:
        return pd.DataFrame(columns=["symbol", "name_zh", "yfinance_symbol"])
    r = pd.DataFrame(ranked).sort_values("auto_score", ascending=False)
    out = c.merge(r, on="yfinance_symbol", how="inner").sort_values("auto_score", ascending=False).head(top_n)
    report_cols = ["symbol", "name_zh", "yfinance_symbol", "auto_score", "auto_ret20", "auto_ret60", "auto_dollar_volume20", "auto_last_close"]
    rep = out[report_cols].copy()
    rep.insert(0, "market", market)
    return out[["symbol", "name_zh", "yfinance_symbol"]].reset_index(drop=True), rep


def refresh_auto_universe_files(settings: Dict, health: List[str]) -> None:
    """每次正式跑之前自動更新股票池；任何一步失敗都保留舊 CSV，避免功能失效。"""
    if not _setting_bool(settings, "auto_universe_enabled", True):
        health.append("AUTO_UNIVERSE disabled by settings")
        return
    ensure_universe_files()
    reports = []
    for market in ["TW", "US"]:
        try:
            old = read_universe(market)
            candidates = fetch_tw_company_universe(health) if market == "TW" else fetch_us_company_universe(settings, health)
            if candidates.empty:
                health.append(f"WARN AUTO_UNIVERSE {market}: no candidates, keep old universe rows={len(old)}")
                continue
            ranked, rep = rank_universe_by_recent_momentum(candidates, market, settings, health)
            if ranked.empty:
                health.append(f"WARN AUTO_UNIVERSE {market}: ranking empty, keep old universe rows={len(old)}")
                continue
            # 原本持有/觀察種子永遠併回，避免新池導致舊持倉找不到價格。
            merged = pd.concat([ranked, old], ignore_index=True).drop_duplicates("symbol").reset_index(drop=True)
            path = CONFIG_DIR / ("tw_universe.csv" if market == "TW" else "us_universe.csv")
            merged.to_csv(path, index=False, encoding="utf-8-sig")
            reports.append(rep)
            health.append(f"OK AUTO_UNIVERSE {market}: candidates={len(candidates)} ranked={len(ranked)} saved={len(merged)} -> {path.name}")
        except Exception as e:
            health.append(f"WARN AUTO_UNIVERSE {market} failed, keep old CSV: {type(e).__name__}: {e}")
    if reports:
        try:
            pd.concat(reports, ignore_index=True).to_csv(AUTO_UNIVERSE_REPORT_PATH, index=False, encoding="utf-8-sig")
        except Exception:
            pass


def normalize_download(df: pd.DataFrame, tickers: List[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        # yfinance can return either Price x Ticker or Ticker x Price.
        out = []
        level0 = set(map(str, df.columns.get_level_values(0)))
        prices = {"Open", "High", "Low", "Close", "Adj Close", "Volume"}
        if len(level0 & prices) >= 3:
            for t in tickers:
                try:
                    sub = df.xs(t, axis=1, level=1, drop_level=True).copy()
                    if not sub.empty:
                        sub["yfinance_symbol"] = t
                        out.append(sub)
                except Exception:
                    continue
        else:
            for t in tickers:
                try:
                    sub = df.xs(t, axis=1, level=0, drop_level=True).copy()
                    if not sub.empty:
                        sub["yfinance_symbol"] = t
                        out.append(sub)
                except Exception:
                    continue
        if not out:
            return pd.DataFrame()
        long = pd.concat(out).reset_index()
    else:
        long = df.copy().reset_index()
        long["yfinance_symbol"] = tickers[0] if len(tickers) == 1 else ""
    # Standardize date column
    if "Date" in long.columns:
        long = long.rename(columns={"Date": "date"})
    elif "Datetime" in long.columns:
        long = long.rename(columns={"Datetime": "date"})
    elif long.columns[0] != "date":
        long = long.rename(columns={long.columns[0]: "date"})
    rename = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Adj Close": "adj_close", "Volume": "volume"}
    long = long.rename(columns=rename)
    need = ["date", "open", "high", "low", "close", "volume", "yfinance_symbol"]
    for c in need:
        if c not in long.columns:
            long[c] = np.nan
    long = long[need]
    long["date"] = pd.to_datetime(long["date"], errors="coerce").dt.tz_localize(None)
    for c in ["open", "high", "low", "close", "volume"]:
        long[c] = pd.to_numeric(long[c], errors="coerce")
    long = long.dropna(subset=["date", "close"])
    return long


def fetch_daily_yfinance(universe: pd.DataFrame, market: str, settings: Dict, health: List[str]) -> pd.DataFrame:
    if yf is None:
        raise RuntimeError("yfinance is not installed")
    tickers = universe["yfinance_symbol"].tolist()
    batch_size = int(settings.get("batch_size_tw" if market == "TW" else "batch_size_us", 20))
    period = f"{int(settings.get('lookback_days', 420))}d"
    all_parts = []
    failed_batches = []
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                raw = yf.download(
                    tickers=" ".join(batch),
                    period=period,
                    interval="1d",
                    group_by="column",
                    auto_adjust=False,
                    progress=False,
                    threads=True,
                    repair=False,
                )
            part = normalize_download(raw, batch)
            if not part.empty:
                all_parts.append(part)
            else:
                failed_batches.append(batch)
        except Exception as e:
            failed_batches.append(batch)
            health.append(f"WARN {market} batch fetch failed {batch[:3]}... {type(e).__name__}: {e}")
    # Single fallback for failed/empty batches
    for batch in failed_batches:
        for t in batch:
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    raw = yf.download(t, period=period, interval="1d", auto_adjust=False, progress=False, threads=False, repair=False)
                part = normalize_download(raw, [t])
                if not part.empty:
                    all_parts.append(part)
            except Exception as e:
                health.append(f"WARN {market} single fetch failed {t}: {e}")
    if not all_parts:
        raise RuntimeError(f"{market} no daily data downloaded; universe={len(universe)} failed_batches={len(failed_batches)}")
    df = pd.concat(all_parts, ignore_index=True).drop_duplicates(["date", "yfinance_symbol"])
    meta = universe[["symbol", "name_zh", "yfinance_symbol"]].copy()
    df = df.merge(meta, on="yfinance_symbol", how="left")
    df["symbol"] = df["symbol"].fillna(df["yfinance_symbol"].str.replace(".TW", "", regex=False).str.replace(".TWO", "O", regex=False))
    df["name_zh"] = df["name_zh"].fillna(df["symbol"])
    df["market"] = market
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    out = DATA_DIR / ("tw_daily_420.csv" if market == "TW" else "us_daily_420.csv")
    df.to_csv(out, index=False, encoding="utf-8-sig")
    health.append(f"OK {market} daily rows={len(df)} symbols={df['symbol'].nunique()} -> {out.relative_to(ROOT)}")
    return df


def demo_daily(universe: pd.DataFrame, market: str, days: int = 260) -> pd.DataFrame:
    rng = np.random.default_rng(1043 if market == "TW" else 2043)
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
    rows = []
    sample = universe.head(20).copy()
    for idx, r in sample.iterrows():
        base = 50 + idx * 8 + (100 if market == "US" else 0)
        drift = 0.0007 + (idx % 5) * 0.00022
        vol = 0.018 + (idx % 4) * 0.004
        rets = rng.normal(drift, vol, len(dates))
        # Make some stronger trend names
        if idx % 7 in (0, 1):
            rets += 0.0015
        close = base * np.cumprod(1 + rets)
        openp = close * (1 + rng.normal(0, 0.006, len(dates)))
        high = np.maximum(openp, close) * (1 + rng.uniform(0.001, 0.022, len(dates)))
        low = np.minimum(openp, close) * (1 - rng.uniform(0.001, 0.022, len(dates)))
        volu = rng.integers(500_000, 8_000_000, len(dates))
        for d, o, h, l, c, v in zip(dates, openp, high, low, close, volu):
            rows.append({
                "date": d, "open": o, "high": h, "low": l, "close": c, "volume": v,
                "yfinance_symbol": r["yfinance_symbol"], "symbol": r["symbol"], "name_zh": r["name_zh"], "market": market
            })
    return pd.DataFrame(rows)


def compute_features(daily: pd.DataFrame, rule: RuleProfile) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame()
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values(["symbol", "date"])
    parts = []
    for sym, g in d.groupby("symbol", sort=False):
        g = g.copy().sort_values("date")
        close = g["close"].astype(float)
        high = g["high"].astype(float).fillna(close)
        low = g["low"].astype(float).fillna(close)
        openp = g["open"].astype(float).fillna(close)
        volume = g["volume"].astype(float).replace(0, np.nan)
        g["ma20"] = close.rolling(20).mean()
        g["ma60"] = close.rolling(60).mean()
        g["ma120"] = close.rolling(120).mean()
        g["ret1"] = close.pct_change(1)
        g["ret5"] = close.pct_change(5)
        g["ret20"] = close.pct_change(20)
        g["ret60"] = close.pct_change(60)
        g["high20"] = high.rolling(20).max()
        g["high60"] = high.rolling(60).max()
        g["high120"] = high.rolling(120).max()
        g["new_high_20d"] = close >= g["high20"]
        g["new_high_60d"] = close >= g["high60"]
        g["new_high_120d"] = close >= g["high120"]
        g["near20"] = close / g["high20"] - 1
        g["near60"] = close / g["high60"] - 1
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        g["rsi14"] = 100 - (100 / (1 + rs))
        g["vol20"] = volume.rolling(20).mean()
        g["volume_ratio"] = (volume / g["vol20"]).replace([np.inf, -np.inf], np.nan)
        tr = pd.concat([(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        atr14 = tr.rolling(14).mean()
        g["atr_pct"] = atr14 / close * 100
        rng = (high - low).replace(0, np.nan)
        g["upper_shadow_pct"] = ((high - np.maximum(openp, close)) / rng).clip(lower=0, upper=1)
        g["dist_ma20_pct"] = (close / g["ma20"] - 1) * 100
        g["dollar_volume"] = close * volume
        g["bull"] = (close > g["ma20"]) & (g["ma20"] > g["ma60"]) & (g["ma60"] > g["ma120"])
        parts.append(g)
    f = pd.concat(parts, ignore_index=True)
    latest_date = f["date"].max()
    latest = f[f["date"] == latest_date].copy()
    if latest.empty:
        return latest
    # liquidity percentile in latest universe
    latest["liquidity_pct"] = latest["dollar_volume"].rank(pct=True)
    # rules / score
    latest["rule_near60"] = latest["near60"].fillna(-999) >= -0.05
    latest["rule_ret20"] = latest["ret20"].fillna(-999) >= rule.ret20_min
    latest["rule_ret60"] = latest["ret60"].fillna(-999) >= rule.ret60_min
    latest["rule_volume"] = latest["volume_ratio"].fillna(1) >= max(rule.volume_ratio_min, 0.8)
    latest["rule_bull"] = latest["bull"].fillna(False)
    latest["rule_rebound"] = (latest["ret1"].fillna(0) > 0) & (latest["dist_ma20_pct"].fillna(999).between(-3, rule.dist_ma20_max))
    latest["rule_near_ma20"] = latest["dist_ma20_pct"].fillna(999).between(rule.dist_ma20_min, rule.dist_ma20_max)
    rule_cols = ["rule_near60", "rule_ret20", "rule_ret60", "rule_volume", "rule_bull", "rule_rebound", "rule_near_ma20"]
    latest["rule_count"] = latest[rule_cols].sum(axis=1)
    # Score weighted momentum but constrained by overheat filters
    ret20_score = (latest["ret20"].fillna(0) * 300).clip(-20, 45)
    ret60_score = (latest["ret60"].fillna(0) * 170).clip(-20, 45)
    near_score = ((latest["near60"].fillna(-0.2) + 0.10) * 180).clip(0, 20)
    liq_score = (latest["liquidity_pct"].fillna(0) * 20).clip(0, 20)
    latest["score"] = (ret20_score + ret60_score + near_score + liq_score).clip(0, 100)
    latest["mega_rank"] = (
        latest["score"].fillna(0)
        + latest["rule_count"].fillna(0) * 8
        + latest["ret20"].fillna(0) * 120
        + latest["ret60"].fillna(0) * 40
        + np.where(latest["new_high_20d"].fillna(False), 8, 0)
        + np.where(latest["new_high_60d"].fillna(False), 5, 0)
        + np.where(latest["new_high_120d"].fillna(False), 3, 0)
        - np.maximum(latest["rsi14"].fillna(70) - 82, 0) * 3
        - np.maximum(latest["atr_pct"].fillna(5) - 14, 0) * 2
    )
    # anti chase pass
    latest["anti_chase_pass"] = (
        latest["dist_ma20_pct"].between(rule.dist_ma20_min, rule.dist_ma20_max, inclusive="both")
        & (latest["ret1"].fillna(99) <= rule.ret1_max)
        & (latest["ret5"].fillna(99) <= rule.ret5_max)
        & (latest["volume_ratio"].fillna(1) <= rule.volume_ratio_max)
        & (latest["upper_shadow_pct"].fillna(0) <= rule.upper_shadow_max)
        & (latest["atr_pct"].fillna(999) >= rule.atr_min)
        & (latest["atr_pct"].fillna(0) <= rule.atr_max)
    )
    if rule.entry_mode == "near":
        latest["entry_pass"] = latest["rule_near_ma20"] & (latest["near60"].fillna(-999) >= -0.08)
    elif rule.entry_mode == "rebound":
        latest["entry_pass"] = latest["rule_rebound"]
    elif rule.entry_mode == "mega_mom":
        latest["entry_pass"] = True
    else:
        latest["entry_pass"] = latest["rule_near_ma20"] | latest["rule_rebound"]
    latest["rule_profile"] = rule.profile_id
    latest["strategy"] = rule.label
    return latest.reset_index(drop=True)



def market_realized_trades(trades: pd.DataFrame, market: str) -> pd.DataFrame:
    if trades is None or trades.empty:
        return pd.DataFrame()
    t = trades.copy()
    if "market" not in t.columns:
        return pd.DataFrame()
    t = t[t["market"].astype(str) == market].copy()
    if t.empty:
        return t
    t["exit_date_dt"] = pd.to_datetime(t.get("exit_date", ""), errors="coerce")
    t["realized_return_num"] = pd.to_numeric(t.get("realized_return_pct", pd.Series(dtype=float)), errors="coerce")
    t = t.dropna(subset=["exit_date_dt", "realized_return_num"]).sort_values("exit_date_dt")
    return t



def latest_close_above_ma(symbol: str, ma_days: int, health: Optional[List[str]] = None) -> Tuple[bool, str]:
    """Return whether the latest close is above MA. Safe fallback: False on failure."""
    if yf is None:
        return False, f"{symbol}: yfinance unavailable"
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            raw = yf.download(symbol, period=f"{max(ma_days * 3, 180)}d", interval="1d", auto_adjust=False, progress=False, threads=False, repair=False)
        d = normalize_download(raw, [symbol])
        d = d.dropna(subset=["close"]).sort_values("date")
        if len(d) < ma_days + 1:
            return False, f"{symbol}: not enough rows for MA{ma_days}"
        close = d["close"].astype(float)
        last = float(close.iloc[-1])
        ma = float(close.rolling(ma_days).mean().iloc[-1])
        ok = bool(np.isfinite(last) and np.isfinite(ma) and last > ma)
        return ok, f"{symbol}: close={last:.2f}, MA{ma_days}={ma:.2f}, above={ok}"
    except Exception as e:
        if health is not None:
            health.append(f"WARN index MA check failed {symbol}: {type(e).__name__}: {e}")
        return False, f"{symbol}: MA check failed {type(e).__name__}"


def tw_dynamic_cooldown_released(settings: Dict, health: Optional[List[str]] = None) -> Tuple[bool, str]:
    ma_days = int(settings.get("tw_dynamic_cooldown_release_ma", 60))
    symbols = settings.get("tw_dynamic_cooldown_release_symbols", ["^TWII", "^TWOII"])
    reasons = []
    for sym in symbols:
        ok, reason = latest_close_above_ma(str(sym), ma_days, health)
        reasons.append(reason)
        if ok:
            return True, "提前解除：" + reason
    return False, "未提前解除：" + " | ".join(reasons)


def us_market_filter_pass(settings: Dict, health: Optional[List[str]] = None) -> Tuple[bool, str]:
    if not _setting_bool(settings, "us_require_qqq_above_ma", True):
        return True, "US QQQ MA filter disabled"
    ma_days = int(settings.get("us_qqq_ma_days", 100))
    ok, reason = latest_close_above_ma("QQQ", ma_days, health)
    return ok, f"QQQ>{ma_days}MA filter: {reason}"

def evaluate_market_risk_guard(market: str, trades: pd.DataFrame, run_date: str, settings: Dict, health: Optional[List[str]] = None) -> Dict:
    """Final live guard. Blocks only new entries; existing positions still exit by STOP / SELL_DUE.

    TW: after 1 losing closed trade, cooldown up to 21 calendar days, but release early when TWII or TWOII is above MA60.
    US: no loss-streak cooldown in final H70 fusion rule; market filter is QQQ > MA100 inside recommendation step.
    """
    today = pd.to_datetime(run_date).normalize()
    t = market_realized_trades(trades, market)
    status = {
        "market": market,
        "guard_name": "TW_LOSS1_DYNAMIC_CD21_RELEASE_TWII_TWOT_MA60" if market == "TW" else "US_QQQ_MA100_FILTER_ONLY",
        "block_new_buys": False,
        "reason": "OK",
        "month_realized_pct": 0.0,
        "consecutive_losses": 0,
        "cooldown_until": "",
        "last_loss_pct": "",
        "settings": "",
    }
    if t.empty:
        return status
    month_start = today.replace(day=1)
    tm = t[(t["exit_date_dt"] >= month_start) & (t["exit_date_dt"] <= today)]
    month_sum = float(tm["realized_return_num"].sum()) if not tm.empty else 0.0
    status["month_realized_pct"] = round(month_sum, 4)
    vals = t["realized_return_num"].tolist()[::-1]
    streak = 0
    for v in vals:
        if v < 0:
            streak += 1
        else:
            break
    status["consecutive_losses"] = int(streak)
    if market == "TW":
        loss_n = int(settings.get("tw_loss_streak_n", 1))
        cd_days = int(settings.get("tw_loss_streak_cooldown_days", 21))
        ma_days = int(settings.get("tw_dynamic_cooldown_release_ma", 60))
        status["settings"] = f"loss_streak={loss_n}, max_cooldown={cd_days}d, early_release=TWII/TWOII>MA{ma_days}"
        if streak >= loss_n:
            last_loss_date = t.iloc[-1]["exit_date_dt"].normalize()
            until = last_loss_date + pd.Timedelta(days=cd_days)
            status["cooldown_until"] = until.strftime("%Y-%m-%d")
            if today <= until:
                released, release_reason = tw_dynamic_cooldown_released(settings, health)
                if released:
                    status["reason"] = f"TW_COOLDOWN_EARLY_RELEASE: {release_reason}"
                else:
                    status["block_new_buys"] = True
                    status["reason"] = f"TW_DYNAMIC_COOLDOWN: {streak} loss(es), max until {until.strftime('%Y-%m-%d')}; {release_reason}"
    else:
        status["settings"] = f"QQQ>{int(settings.get('us_qqq_ma_days',100))}MA required; no loss-streak cooldown"
    return status


def apply_v1046_guards_to_recommendations(recs: pd.DataFrame, guard_status: Dict[str, Dict], health: List[str]) -> pd.DataFrame:
    if recs is None or recs.empty:
        return recs
    keep = []
    blocked = 0
    for _, r in recs.iterrows():
        m = str(r.get("market", ""))
        gs = guard_status.get(m, {})
        if gs.get("block_new_buys"):
            blocked += 1
            health.append(f"GUARD {m} blocked new recommendation {r.get('symbol','')}: {gs.get('reason','')}")
            continue
        keep.append(r)
    if not keep:
        if blocked:
            return pd.DataFrame(columns=recs.columns)
        return recs
    return pd.DataFrame(keep).reset_index(drop=True)


def write_risk_guard_status(guard_status: Dict[str, Dict]) -> None:
    rows = list(guard_status.values()) if guard_status else []
    pd.DataFrame(rows).to_csv(RISK_GUARD_STATUS_PATH, index=False, encoding="utf-8-sig")


def weekly_gate_pass(features: pd.DataFrame, rule: RuleProfile, health: List[str]) -> bool:
    if features is None or features.empty or "date" not in features.columns:
        return False
    latest_date = pd.to_datetime(features["date"].max()).normalize()
    weekday = int(latest_date.weekday())  # Monday=0
    if weekday != int(rule.recommendation_weekday):
        names = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
        health.append(
            f"NO_REC {rule.market}: 非固定週選資料日；latest={latest_date.date()} {names[weekday]}，"
            f"規則需要 {names[int(rule.recommendation_weekday)]} 收盤資料。{rule.buy_timing_note}"
        )
        return False
    return True


def apply_final_stock_rule(c0: pd.DataFrame, rule: RuleProfile, health: List[str]) -> pd.DataFrame:
    f = c0.copy()
    if rule.profile_id == "TW_FINAL_H80_MAX3_DYNAMIC_CD":
        dv20 = f.get("dollar_volume", pd.Series(index=f.index, dtype=float)).fillna(0)
        mask = (
            dv20.ge(rule.liquidity_min)
            & f["rule_count"].fillna(0).ge(rule.rule_min)
            & f["ret20"].fillna(-999).ge(0.10)
            & f["ret20"].fillna(999).le(0.45)
            & f["ret60"].fillna(-999).ge(0.40)
            & f["ret5"].fillna(999).le(0.10)
            & f["atr_pct"].fillna(999).le(5.0)
            & f["rsi14"].fillna(999).le(84.0)
        )
        health.append(f"FINAL_RULE TW_FINAL_H80_MAX3_DYNAMIC_CD passed={int(mask.sum())} / total={len(f)}")
        return f[mask].copy()
    if rule.profile_id == "US_FINAL_H70_FUSION_QQQ100":
        mask = (
            f["rule_count"].fillna(0).ge(5)
            & f["score"].fillna(0).ge(75)
            & f["ret20"].between(0.12, 0.425, inclusive="both")
            & f["ret60"].between(0.25, 1.80, inclusive="both")
            & f["rsi14"].between(60, 88, inclusive="both")
            & f["atr_pct"].between(2.5, 20, inclusive="both")
        )
        health.append(f"FINAL_RULE US_FINAL_H70_FUSION_QQQ100 passed={int(mask.sum())} / total={len(f)}")
        return f[mask].copy()
    return pd.DataFrame(columns=f.columns)

def make_recommendations(features: pd.DataFrame, rule: RuleProfile, risk: Dict, positions: pd.DataFrame, settings: Dict, health: List[str]) -> pd.DataFrame:
    if features.empty:
        health.append(f"NO_REC {rule.market}: latest features is empty, data fetch may have failed")
        return pd.DataFrame()
    if rule.risk_mode == "normal_only" and risk.get("risk_level") != "NORMAL":
        health.append(f"NO_REC {rule.market}: market risk is {risk.get('risk_level')} / {risk.get('reason')} / rule requires NORMAL")
        return pd.DataFrame()
    if not weekly_gate_pass(features, rule, health):
        return pd.DataFrame()
    if rule.market == "US":
        ok, msg = us_market_filter_pass(settings, health)
        health.append("US_MARKET_FILTER " + msg)
        if not ok:
            health.append("NO_REC US: QQQ market filter blocked new recommendations")
            return pd.DataFrame()
    active = positions[(positions.get("market", pd.Series(dtype=str)) == rule.market) & (positions.get("status", pd.Series(dtype=str)).isin(["HOLD", "NEW", "SELL_DUE"]))] if not positions.empty else pd.DataFrame()
    open_symbols = set(active["symbol"].astype(str).tolist()) if not active.empty and "symbol" in active.columns else set()
    room = max(0, int(rule.max_positions) - len(open_symbols))
    if room <= 0:
        health.append(f"NO_REC {rule.market}: max positions reached, active={len(open_symbols)} max={rule.max_positions}")
        return pd.DataFrame()
    c0 = features.copy()
    c = apply_final_stock_rule(c0, rule, health)
    c = c[~c["symbol"].astype(str).isin(open_symbols)] if not c.empty else c
    health.append(f"FILTER {rule.market}: final_rule_pass={len(c)}, room={room}, open_positions={len(open_symbols)}, weekly={rule.buy_timing_note}")
    if c.empty:
        near = c0.copy()
        near["fail_reason"] = "未通過最終收益規則或已持有;"
        if open_symbols:
            near.loc[near["symbol"].astype(str).isin(open_symbols), "fail_reason"] += "已持有;"
        near_cols = [x for x in ["market","symbol","name_zh","date","close","score","mega_rank","rule_count","rsi14","atr_pct","volume_ratio","ret5","ret20","ret60","new_high_20d","new_high_60d","new_high_120d","fail_reason"] if x in near.columns]
        if near_cols:
            out = OUTPUT_DIR / f"v1046_{rule.market.lower()}_nearest_failed_candidates.csv"
            sort_cols_near = [x for x in ["mega_rank","score","rule_count","ret20","ret60"] if x in near.columns]
            near.sort_values(sort_cols_near, ascending=False).head(10)[near_cols].to_csv(out, index=False, encoding="utf-8-sig")
            health.append(f"NO_REC {rule.market}: no official candidate passed. See {out.name} for nearest failed candidates")
        return c
    sort_cols = ["mega_rank", "score", "rule_count", "ret20", "ret60"]
    n = min(rule.topn, room)
    c = c.sort_values(sort_cols, ascending=False).head(n).copy()
    c["recommendation_date"] = pd.to_datetime(c["date"]).dt.strftime("%Y-%m-%d")
    c["recommended_price"] = c["close"]
    c["action"] = "紙上買入候選"
    c["hold_days_plan"] = rule.hold_days
    c["stop_loss_pct"] = rule.stop_loss_pct
    c["reason"] = c.apply(lambda r: make_reason(r, rule, risk), axis=1)
    health.append(f"OK_REC {rule.market}: official recommendations={len(c)}")
    return c

def make_reason(r: pd.Series, rule: RuleProfile, risk: Dict) -> str:
    entry = "全台/美股池動能突破" if rule.entry_mode == "mega_mom" else ("接近MA20" if rule.entry_mode == "near" else "回踩轉強")
    return (
        f"{entry}；防追高通過；分數 {safe_float(r.get('score'),0):.1f}；規則 {int(safe_float(r.get('rule_count'),0))}；"
        f"離MA20 {safe_float(r.get('dist_ma20_pct'),0):.1f}%；5日 {safe_float(r.get('ret5'),0)*100:.1f}%；"
        f"停損 {rule.stop_loss_pct*100:.0f}%；H{rule.hold_days}；最多持倉{rule.max_positions}；{rule.buy_timing_note}；風控 {risk.get('risk_level','') }"
    )


def fetch_risk(settings: Dict, demo: bool, health: List[str]) -> Dict:
    if demo or yf is None:
        return {
            "risk_level": "NORMAL", "reason": "DEMO", "source": "demo",
            "tw_night_pct": 0.0, "QQQ_pct": 0.0, "SPY_pct": 0.0, "SOXX_pct": 0.0, "SMH_pct": 0.0, "VIX_pct": 0.0,
            "updated_at": now_ts(),
        }
    symbols = ["QQQ", "SPY", "SOXX", "SMH", "^VIX"]
    pct = {"QQQ_pct": np.nan, "SPY_pct": np.nan, "SOXX_pct": np.nan, "SMH_pct": np.nan, "VIX_pct": np.nan}
    latest_prices = []
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            raw = yf.download(" ".join(symbols), period="8d", interval="1d", auto_adjust=False, progress=False, threads=True, repair=False)
        norm = normalize_download(raw, symbols)
        if not norm.empty:
            for s in symbols:
                g = norm[norm["yfinance_symbol"] == s].sort_values("date")
                if len(g) >= 2:
                    last = float(g["close"].iloc[-1]); prev = float(g["close"].iloc[-2])
                    key = "VIX_pct" if s == "^VIX" else f"{s}_pct"
                    pct[key] = (last / prev - 1) * 100
                    latest_prices.append({"market":"RISK", "symbol":s, "name_zh":risk_name(s), "latest_price":last, "change_pct":pct[key], "source":f"yfinance:{s}", "updated_at":now_ts()})
    except Exception as e:
        health.append(f"WARN risk etf fetch failed: {e}")
    tw_night_pct, taifex_source = fetch_taifex_safe(health)
    qqq = pct.get("QQQ_pct", np.nan); spy = pct.get("SPY_pct", np.nan); soxx = pct.get("SOXX_pct", np.nan); smh = pct.get("SMH_pct", np.nan); vix = pct.get("VIX_pct", np.nan)
    reasons = []
    level = "NORMAL"
    def le(x, th): return pd.notna(x) and x <= th
    def ge(x, th): return pd.notna(x) and x >= th
    if le(qqq, -2.5) or le(spy, -3.0) or le(soxx, -3.5) or le(smh, -3.5) or ge(vix, 8.0) or le(tw_night_pct, -2.0):
        level = "BLOCK_BUY"
        if le(qqq, -2.5): reasons.append("QQQ<=-2.5%")
        if le(spy, -3.0): reasons.append("SPY<=-3.0%")
        if le(soxx, -3.5): reasons.append("SOXX<=-3.5%")
        if le(smh, -3.5): reasons.append("SMH<=-3.5%")
        if ge(vix, 8.0): reasons.append("VIX>=+8%")
        if le(tw_night_pct, -2.0): reasons.append("台指夜盤<=-2.0%")
    elif le(qqq, -1.5) or le(spy, -2.0) or le(soxx, -2.0) or le(smh, -2.0) or le(tw_night_pct, -1.0):
        level = "REDUCE_OR_WAIT"
        if le(qqq, -1.5): reasons.append("QQQ<=-1.5%")
        if le(spy, -2.0): reasons.append("SPY<=-2.0%")
        if le(soxx, -2.0): reasons.append("SOXX<=-2.0%")
        if le(smh, -2.0): reasons.append("SMH<=-2.0%")
        if le(tw_night_pct, -1.0): reasons.append("台指夜盤<=-1.0%")
    risk = {
        "risk_level": level,
        "reason": "; ".join(reasons) if reasons else "NORMAL",
        "source": f"yfinance_risk_etfs;{taifex_source}",
        "tw_night_pct": safe_float(tw_night_pct, 0.0),
        "QQQ_pct": safe_float(qqq, 0.0), "SPY_pct": safe_float(spy, 0.0), "SOXX_pct": safe_float(soxx, 0.0),
        "SMH_pct": safe_float(smh, 0.0), "VIX_pct": safe_float(vix, 0.0), "updated_at": now_ts(),
    }
    pd.DataFrame(latest_prices).to_csv(OUTPUT_DIR / "v1046_risk_latest_prices.csv", index=False, encoding="utf-8-sig")
    return risk


def risk_name(s: str) -> str:
    return {"QQQ":"納斯達克100 ETF", "SPY":"S&P 500 ETF", "SOXX":"費半 ETF", "SMH":"半導體 ETF", "^VIX":"VIX 恐慌指數"}.get(s, s)


def fetch_taifex_safe(health: List[str]) -> Tuple[float, str]:
    # Manual override first
    manual = DATA_DIR / "tw_night_futures_manual.csv"
    if manual.exists():
        try:
            m = pd.read_csv(manual)
            if not m.empty and "change_pct" in m.columns:
                val = float(m["change_pct"].iloc[-1])
                return val, "taifex_manual"
        except Exception as e:
            health.append(f"WARN taifex manual parse failed: {e}")
    # Try official open data CSV. If fails, return 0 and clear message.
    try:
        import urllib.request
        import io
        url = "https://www.taifex.com.tw/data_gov/taifex_open_data.asp?data_name=DailyForeignExchangeRates"
        with urllib.request.urlopen(url, timeout=8) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
        if text.strip().lower().startswith("<!doctype") or "<html" in text[:1000].lower():
            return 0.0, "taifex_unavailable_html_fallback"
        # This endpoint may not directly include night futures; kept as safe availability check.
        return 0.0, "taifex_open_data_checked_no_night_pct"
    except Exception as e:
        return 0.0, f"taifex_unavailable:{type(e).__name__}"


def read_positions() -> pd.DataFrame:
    if POSITIONS_PATH.exists():
        return pd.read_csv(POSITIONS_PATH, dtype=str).fillna("")
    cols = ["position_id", "market", "symbol", "yfinance_symbol", "name_zh", "entry_date", "entry_price", "latest_date", "latest_price", "hold_days", "planned_hold_bars", "stop_loss_pct", "status", "exit_date", "exit_price", "exit_reason", "realized_return_pct", "strategy", "profile_id"]
    return pd.DataFrame(columns=cols)


def read_trades() -> pd.DataFrame:
    if TRADES_PATH.exists():
        return pd.read_csv(TRADES_PATH, dtype=str).fillna("")
    return pd.DataFrame(columns=["position_id", "market", "symbol", "name_zh", "entry_date", "entry_price", "exit_date", "exit_price", "exit_reason", "hold_days", "realized_return_pct", "strategy", "profile_id"])


def write_positions(df: pd.DataFrame) -> None:
    df.to_csv(POSITIONS_PATH, index=False, encoding="utf-8-sig")


def write_trades(df: pd.DataFrame) -> None:
    df.to_csv(TRADES_PATH, index=False, encoding="utf-8-sig")


def update_positions(positions: pd.DataFrame, trades: pd.DataFrame, daily_all: pd.DataFrame, settings: Dict, health: List[str]) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    alerts = []
    if positions.empty:
        return positions, trades, pd.DataFrame(alerts)
    pos = positions.copy()
    active_mask = pos["status"].isin(["HOLD", "NEW", "SELL_DUE"])
    if not active_mask.any():
        return pos, trades, pd.DataFrame(alerts)
    latest_map = latest_price_map(daily_all)
    closed_rows = []
    for idx, r in pos[active_mask].iterrows():
        sym = str(r["symbol"])
        yf_sym = str(r.get("yfinance_symbol", ""))
        key = yf_sym or sym
        latest = latest_map.get(key) or latest_map.get(sym)
        if latest is None:
            alerts.append({"level":"WARN", "market":r["market"], "symbol":sym, "message":"找不到最新價，持倉未更新"})
            continue
        latest_date, latest_price, close_series_dates = latest
        entry_price = safe_float(r.get("entry_price"), np.nan)
        if pd.isna(entry_price) or entry_price <= 0:
            continue
        entry_date = pd.to_datetime(r.get("entry_date"), errors="coerce")
        # hold bars = trading rows after or equal entry date
        try:
            hold_bars = int((close_series_dates >= entry_date).sum()) - 1
        except Exception:
            hold_bars = max(0, (pd.to_datetime(latest_date) - entry_date).days)
        stop_loss_pct = safe_float(r.get("stop_loss_pct"), 0.1)
        planned = int(safe_float(r.get("planned_hold_bars"), 10))
        ret_pct = (latest_price / entry_price - 1) * 100
        pos.at[idx, "latest_date"] = pd.to_datetime(latest_date).strftime("%Y-%m-%d")
        pos.at[idx, "latest_price"] = f"{latest_price:.6f}"
        pos.at[idx, "hold_days"] = str(max(0, hold_bars))
        pos.at[idx, "realized_return_pct"] = ""
        exit_reason = None
        if latest_price <= entry_price * (1 - stop_loss_pct):
            exit_reason = "STOP_LOSS_PAPER_EXIT"
        elif hold_bars >= planned:
            if settings.get("paper_exit_due", True):
                exit_reason = "SELL_DUE_PAPER_EXIT"
            else:
                pos.at[idx, "status"] = "SELL_DUE"
                alerts.append({"level":"SELL_DUE", "market":r["market"], "symbol":sym, "message":f"H{planned} 到期，紙上提醒出場，報酬 {ret_pct:.2f}%"})
        if exit_reason:
            pos.at[idx, "status"] = "CLOSED"
            pos.at[idx, "exit_date"] = pd.to_datetime(latest_date).strftime("%Y-%m-%d")
            pos.at[idx, "exit_price"] = f"{latest_price:.6f}"
            pos.at[idx, "exit_reason"] = exit_reason
            pos.at[idx, "realized_return_pct"] = f"{ret_pct:.4f}"
            closed_rows.append({
                "position_id": r["position_id"], "market": r["market"], "symbol": sym, "name_zh": r.get("name_zh", sym),
                "entry_date": r.get("entry_date", ""), "entry_price": r.get("entry_price", ""),
                "exit_date": pd.to_datetime(latest_date).strftime("%Y-%m-%d"), "exit_price": f"{latest_price:.6f}",
                "exit_reason": exit_reason, "hold_days": str(max(0, hold_bars)), "realized_return_pct": f"{ret_pct:.4f}",
                "strategy": r.get("strategy", ""), "profile_id": r.get("profile_id", ""),
            })
            alerts.append({"level":"EXIT", "market":r["market"], "symbol":sym, "message":f"{exit_reason}，紙上報酬 {ret_pct:.2f}%"})
        else:
            if pos.at[idx, "status"] == "NEW":
                pos.at[idx, "status"] = "HOLD"
    if closed_rows:
        trades = pd.concat([trades, pd.DataFrame(closed_rows)], ignore_index=True)
        trades = trades.drop_duplicates("position_id", keep="last")
    return pos, trades, pd.DataFrame(alerts)


def latest_price_map(daily_all: pd.DataFrame) -> Dict[str, Tuple[pd.Timestamp, float, pd.Series]]:
    mp = {}
    if daily_all.empty:
        return mp
    d = daily_all.copy()
    d["date"] = pd.to_datetime(d["date"])
    for keycol in ["yfinance_symbol", "symbol"]:
        for key, g in d.groupby(keycol):
            if not key:
                continue
            g = g.sort_values("date")
            last = g.iloc[-1]
            mp[str(key)] = (last["date"], float(last["close"]), g["date"])
    return mp


def add_new_positions(positions: pd.DataFrame, recs: pd.DataFrame, settings: Dict, paper_block: bool, health: List[str]) -> pd.DataFrame:
    if recs.empty:
        return positions
    if paper_block:
        health.append("INFO paper guard blocks new positions; recommendations are shown but not added")
        return positions
    pos = positions.copy()
    active_symbols = set(pos[pos["status"].isin(["HOLD", "NEW", "SELL_DUE"])] ["symbol"].astype(str)) if not pos.empty else set()
    rows = []
    for _, r in recs.iterrows():
        sym = str(r["symbol"])
        if sym in active_symbols:
            continue
        dt = pd.to_datetime(r["date"]).strftime("%Y-%m-%d")
        pid = f"{r['market']}_{sym}_{dt}_{datetime.now(TAIPEI_TZ).strftime('%H%M%S%f')}"
        rows.append({
            "position_id": pid,
            "market": r["market"], "symbol": sym, "yfinance_symbol": r.get("yfinance_symbol", ""), "name_zh": r.get("name_zh", sym),
            "entry_date": dt, "entry_price": f"{safe_float(r.get('recommended_price', r.get('close')),0):.6f}",
            "latest_date": dt, "latest_price": f"{safe_float(r.get('recommended_price', r.get('close')),0):.6f}",
            "hold_days": "0", "planned_hold_bars": str(int(r.get("hold_days_plan", 10))),
            "stop_loss_pct": f"{safe_float(r.get('stop_loss_pct'),0.1):.4f}",
            "status": "NEW", "exit_date": "", "exit_price": "", "exit_reason": "", "realized_return_pct": "",
            "strategy": r.get("strategy", ""), "profile_id": r.get("rule_profile", ""),
        })
    if rows:
        pos = pd.concat([pos, pd.DataFrame(rows)], ignore_index=True)
        health.append(f"OK added paper positions={len(rows)}")
    return pos


def compute_monitor(positions: pd.DataFrame, trades: pd.DataFrame, settings: Dict) -> Tuple[pd.DataFrame, Dict, bool]:
    # Calculate simple paper equity: closed realized + open unrealized average, based on equal unit positions.
    closed = trades.copy() if not trades.empty else pd.DataFrame()
    openp = positions[positions["status"].isin(["HOLD", "NEW", "SELL_DUE"])] if not positions.empty else pd.DataFrame()
    realized = pd.to_numeric(closed.get("realized_return_pct", pd.Series(dtype=float)), errors="coerce").dropna()
    open_ret = pd.Series(dtype=float)
    if not openp.empty:
        ep = pd.to_numeric(openp["entry_price"], errors="coerce")
        lp = pd.to_numeric(openp["latest_price"], errors="coerce")
        open_ret = ((lp / ep - 1) * 100).replace([np.inf, -np.inf], np.nan).dropna()
    total_closed = len(realized)
    win_rate = float((realized > 0).mean() * 100) if total_closed else np.nan
    avg_realized = float(realized.mean()) if total_closed else np.nan
    avg_open = float(open_ret.mean()) if len(open_ret) else np.nan
    consecutive_losses = calc_consecutive_losses(closed)
    month_ret, month_dd = calc_month_metrics(closed, openp)
    paper_block = False
    reasons = []
    if settings.get("paper_guard_enabled", True):
        if consecutive_losses >= int(settings.get("pause_after_consecutive_losses", 4)):
            paper_block = True; reasons.append(f"連續虧損 {consecutive_losses} 筆")
        if pd.notna(month_dd) and month_dd <= float(settings.get("monthly_stop_drawdown_pct", -12.0)):
            paper_block = True; reasons.append(f"單月回撤 {month_dd:.2f}% <= {settings.get('monthly_stop_drawdown_pct')}%")
    status = {
        "paper_block_new_buys": paper_block,
        "guard_reason": "; ".join(reasons) if reasons else "OK",
        "closed_trades": total_closed,
        "open_positions": len(openp),
        "win_rate_pct": win_rate,
        "avg_realized_pct": avg_realized,
        "avg_open_pct": avg_open,
        "consecutive_losses": consecutive_losses,
        "month_return_pct_est": month_ret,
        "month_drawdown_pct_est": month_dd,
        "generated_at": now_ts(),
    }
    df = pd.DataFrame([status])
    df.to_csv(MONITOR_PATH, index=False, encoding="utf-8-sig")
    update_equity_curve(status)
    return df, status, paper_block


def calc_consecutive_losses(trades: pd.DataFrame) -> int:
    if trades.empty or "exit_date" not in trades.columns:
        return 0
    t = trades.copy()
    t["exit_date_dt"] = pd.to_datetime(t["exit_date"], errors="coerce")
    t["ret"] = pd.to_numeric(t["realized_return_pct"], errors="coerce")
    t = t.dropna(subset=["exit_date_dt", "ret"]).sort_values("exit_date_dt")
    count = 0
    for x in reversed(t["ret"].tolist()):
        if x < 0:
            count += 1
        else:
            break
    return count


def calc_month_metrics(trades: pd.DataFrame, openp: pd.DataFrame) -> Tuple[float, float]:
    # Approximate current month performance from closed trades exiting this month + open positions entered this month.
    cur = pd.Timestamp.today().strftime("%Y-%m")
    vals = []
    if not trades.empty and "exit_date" in trades.columns:
        t = trades.copy(); t["exit_dt"] = pd.to_datetime(t["exit_date"], errors="coerce")
        t = t[t["exit_dt"].dt.strftime("%Y-%m") == cur]
        vals += pd.to_numeric(t["realized_return_pct"], errors="coerce").dropna().tolist()
    if not openp.empty:
        ep = pd.to_numeric(openp["entry_price"], errors="coerce"); lp = pd.to_numeric(openp["latest_price"], errors="coerce")
        vals += ((lp / ep - 1) * 100).replace([np.inf, -np.inf], np.nan).dropna().tolist()
    if not vals:
        return np.nan, np.nan
    # average unit return as simple estimator, drawdown approximated by min cumulative average path
    arr = np.array(vals, dtype=float) / 100.0
    equity = np.cumprod(1 + arr)
    peak = np.maximum.accumulate(equity)
    dd = (equity / peak - 1) * 100
    return (equity[-1] - 1) * 100, float(dd.min())


def update_equity_curve(status: Dict):
    row = {"date": today_str(), **status}
    if EQUITY_PATH.exists():
        eq = pd.read_csv(EQUITY_PATH)
        eq = eq[eq["date"].astype(str) != row["date"]]
        eq = pd.concat([eq, pd.DataFrame([row])], ignore_index=True)
    else:
        eq = pd.DataFrame([row])
    eq.to_csv(EQUITY_PATH, index=False, encoding="utf-8-sig")




def get_total_capital(settings: Dict) -> float:
    raw = os.environ.get("V1046_TOTAL_CAPITAL") or settings.get("total_capital", 100000)
    try:
        v = float(raw)
        return v if v > 0 else 100000.0
    except Exception:
        return 100000.0


def enrich_trade_sizing(recs: pd.DataFrame, settings: Dict) -> pd.DataFrame:
    """Add practical sizing columns for recommendations.

    This is only a position-size reference for paper/live manual execution.
    It does not place orders and does not force fractional shares.
    """
    if recs is None or recs.empty:
        return recs
    out = recs.copy()
    total_capital = get_total_capital(settings)
    tw_weight = safe_float(settings.get("capital_weight_tw", 0.20), 0.20)
    us_weight = safe_float(settings.get("capital_weight_us", 0.80), 0.80)
    weight_map = {"TW": tw_weight, "US": us_weight}
    maxpos_map = {"TW": TW_RULE.max_positions, "US": US_RULE.max_positions}
    stop_map = {"TW": TW_RULE.stop_loss_pct, "US": US_RULE.stop_loss_pct}
    amounts=[]; shares=[]; stop_prices=[]; max_losses=[]; notes=[]
    for _, r in out.iterrows():
        m = str(r.get("market", ""))
        px = safe_float(r.get("recommended_price", r.get("close", np.nan)), np.nan)
        w = weight_map.get(m, 0.0)
        maxpos = max(1, int(maxpos_map.get(m, 1)))
        amt = total_capital * w / maxpos
        stop_pct = safe_float(r.get("stop_loss_pct", stop_map.get(m, 0.10)), stop_map.get(m, 0.10))
        if pd.isna(px) or px <= 0:
            sh = 0
            stop_px = np.nan
        else:
            sh = int(math.floor(amt / px))
            stop_px = px * (1 - stop_pct)
        amounts.append(round(float(amt), 2))
        shares.append(sh)
        stop_prices.append(round(float(stop_px), 4) if not pd.isna(stop_px) else "")
        max_losses.append(round(float(amt * stop_pct), 2))
        if m == "TW":
            notes.append("估算股數；台股若不足整張可用零股，實際以券商規則為準")
        elif m == "US":
            notes.append("估算股數；若券商支援碎股可自行微調")
        else:
            notes.append("估算值")
    out["target_total_capital"] = total_capital
    out["target_market_weight"] = out["market"].map(weight_map).fillna(0.0)
    out["target_position_amount"] = amounts
    out["estimated_shares"] = shares
    out["stop_loss_price"] = stop_prices
    out["max_loss_amount"] = max_losses
    out["sizing_note"] = notes
    return out


def build_today_action_summary(recs: pd.DataFrame, positions: pd.DataFrame, trades: pd.DataFrame, alerts: pd.DataFrame, risk: Dict, monitor: Dict, settings: Dict) -> pd.DataFrame:
    rows = []
    active = positions[positions["status"].isin(["HOLD", "NEW", "SELL_DUE"])] if positions is not None and not positions.empty and "status" in positions.columns else pd.DataFrame()
    sell_due = active[active["status"] == "SELL_DUE"] if not active.empty else pd.DataFrame()
    closed_today = pd.DataFrame()
    if trades is not None and not trades.empty and "exit_date" in trades.columns:
        closed_today = trades[pd.to_datetime(trades["exit_date"], errors="coerce").dt.strftime("%Y-%m-%d") == today_str()].copy()
    for market, label, maxpos in [("TW", "台股", TW_RULE.max_positions), ("US", "美股", US_RULE.max_positions)]:
        rsub = recs[recs["market"].astype(str).eq(market)] if recs is not None and not recs.empty and "market" in recs.columns else pd.DataFrame()
        asub = active[active["market"].astype(str).eq(market)] if not active.empty and "market" in active.columns else pd.DataFrame()
        ssub = sell_due[sell_due["market"].astype(str).eq(market)] if not sell_due.empty and "market" in sell_due.columns else pd.DataFrame()
        csub = closed_today[closed_today["market"].astype(str).eq(market)] if not closed_today.empty and "market" in closed_today.columns else pd.DataFrame()
        if not rsub.empty:
            action = f"新買候選 {len(rsub)} 檔：" + ", ".join(rsub["symbol"].astype(str).tolist())
        elif not ssub.empty:
            action = f"有到期/待賣 {len(ssub)} 檔：" + ", ".join(ssub["symbol"].astype(str).tolist())
        elif monitor.get("paper_block_new_buys"):
            action = "紙上防呆暫停新增；只更新持倉/出場"
        else:
            action = "今日無新買候選；只更新持倉"
        rows.append({
            "generated_at": now_ts(),
            "market": market,
            "market_label": label,
            "today_action": action,
            "new_buy_count": len(rsub),
            "open_positions": len(asub),
            "max_positions": maxpos,
            "sell_due_count": len(ssub),
            "closed_today_count": len(csub),
            "risk_level": risk.get("risk_level", ""),
            "guard_reason": monitor.get("guard_reason", ""),
            "rule": TW_RULE.profile_id if market == "TW" else US_RULE.profile_id,
        })
    return pd.DataFrame(rows)


def write_today_action_summary(df: pd.DataFrame) -> None:
    if df is None or df.empty:
        pd.DataFrame(columns=["generated_at","market","market_label","today_action","new_buy_count","open_positions","max_positions","sell_due_count","closed_today_count","risk_level","guard_reason","rule"]).to_csv(TODAY_ACTION_SUMMARY_PATH, index=False, encoding="utf-8-sig")
    else:
        df.to_csv(TODAY_ACTION_SUMMARY_PATH, index=False, encoding="utf-8-sig")

def write_recommendations(recs: pd.DataFrame) -> None:
    def _write_split(df: pd.DataFrame) -> None:
        cols = ["market", "symbol", "name_zh", "recommended_price", "recommendation_date", "strategy", "action", "reason", "hold_days_plan", "rule_profile"]
        for path, market in [(TODAY_TW_RECS_PATH, "TW"), (TODAY_US_RECS_PATH, "US")]:
            if df is None or df.empty or "market" not in df.columns:
                pd.DataFrame(columns=cols).to_csv(path, index=False, encoding="utf-8-sig")
                continue
            sub = df[df["market"].astype(str).eq(market)].copy()
            if sub.empty:
                pd.DataFrame(columns=cols).to_csv(path, index=False, encoding="utf-8-sig")
            else:
                use_cols = [c for c in cols if c in sub.columns]
                sub[use_cols].to_csv(path, index=False, encoding="utf-8-sig")

    if recs is None or recs.empty:
        pd.DataFrame(columns=["market", "symbol", "name_zh", "recommended_price", "recommendation_date", "strategy", "action", "reason"]).to_csv(TODAY_RECS_PATH, index=False, encoding="utf-8-sig")
        pd.DataFrame(columns=["市場", "代號", "名稱", "推薦價", "日期", "操作", "理由"]).to_csv(TODAY_RECS_SIMPLE_PATH, index=False, encoding="utf-8-sig")
        _write_split(pd.DataFrame())
        NO_REC_REASON_PATH.write_text("本週沒有正式推薦；非固定週選日也不會產生新買進。台股：週三收盤後產生、週四買。美股：美國週四收盤後產生、台灣週五晚上買。\n", encoding="utf-8")
        return
    full_cols = ["market", "symbol", "yfinance_symbol", "name_zh", "date", "recommendation_date",
        "recommended_price", "close", "strategy", "action", "reason",
        "score", "mega_rank", "rule_count", "rsi14", "dist_ma20_pct", "ret1", "ret5", "ret20", "ret60",
        "volume_ratio", "upper_shadow_pct", "atr_pct", "stop_loss_pct", "hold_days_plan", "rule_profile",
        "target_total_capital", "target_market_weight", "target_position_amount", "estimated_shares", "stop_loss_price", "max_loss_amount", "sizing_note",
    ]
    for c in full_cols:
        if c not in recs.columns:
            recs[c] = ""
    recs[full_cols].to_csv(TODAY_RECS_PATH, index=False, encoding="utf-8-sig")
    _write_split(recs)
    simple = pd.DataFrame({
        "市場": recs["market"], "代號": recs["symbol"], "名稱": recs["name_zh"],
        "推薦價": recs["recommended_price"].map(lambda x: fmt_num(x, 4)),
        "日期": recs["recommendation_date"], "操作": recs["action"],
        "建議投入": recs.get("target_position_amount", pd.Series([""]*len(recs))).map(lambda x: fmt_num(x, 0) if x != "" else ""),
        "預估股數": recs.get("estimated_shares", pd.Series([""]*len(recs))),
        "停損價": recs.get("stop_loss_price", pd.Series([""]*len(recs))).map(lambda x: fmt_num(x, 4) if x != "" else ""),
        "最大虧損額": recs.get("max_loss_amount", pd.Series([""]*len(recs))).map(lambda x: fmt_num(x, 0) if x != "" else ""),
        "理由": recs["reason"],
    })
    simple.to_csv(TODAY_RECS_SIMPLE_PATH, index=False, encoding="utf-8-sig")
    NO_REC_REASON_PATH.write_text(f"本週正式推薦 {len(recs)} 檔。\n", encoding="utf-8")

def signal_ledger_columns() -> List[str]:
    return [
        "run_date", "locked_at", "is_empty_marker",
        "market", "symbol", "yfinance_symbol", "name_zh", "date", "recommendation_date",
        "recommended_price", "close", "strategy", "action", "reason",
        "score", "rule_count", "dist_ma20_pct", "ret1", "ret5", "ret20", "ret60",
        "volume_ratio", "upper_shadow_pct", "atr_pct", "stop_loss_pct", "hold_days_plan", "rule_profile",
        "target_total_capital", "target_market_weight", "target_position_amount", "estimated_shares", "stop_loss_price", "max_loss_amount", "sizing_note",
    ]


def read_signal_ledger() -> pd.DataFrame:
    cols = signal_ledger_columns()
    if SIGNAL_LEDGER_PATH.exists():
        df = pd.read_csv(SIGNAL_LEDGER_PATH, dtype=str).fillna("")
        for c in cols:
            if c not in df.columns:
                df[c] = ""
        return df[cols]
    return pd.DataFrame(columns=cols)


def get_today_locked_recommendations(run_date: str, health: List[str]) -> Tuple[pd.DataFrame, bool]:
    ledger = read_signal_ledger()
    if ledger.empty:
        return pd.DataFrame(), False
    today_rows = ledger[ledger["run_date"].astype(str) == str(run_date)].copy()
    if today_rows.empty:
        return pd.DataFrame(), False
    if (today_rows["is_empty_marker"].astype(str) == "1").any():
        health.append(f"OK signal lock active for {run_date}: locked empty recommendation")
        return pd.DataFrame(), True
    for c in ["recommended_price", "close", "score", "rule_count", "dist_ma20_pct", "ret1", "ret5", "ret20", "ret60", "volume_ratio", "upper_shadow_pct", "atr_pct", "stop_loss_pct", "hold_days_plan"]:
        if c in today_rows.columns:
            today_rows[c] = pd.to_numeric(today_rows[c], errors="coerce")
    health.append(f"OK signal lock active for {run_date}: loaded locked recommendations={len(today_rows)}")
    return today_rows, True


def lock_today_recommendations(recs: pd.DataFrame, run_date: str, health: List[str]) -> None:
    cols = signal_ledger_columns()
    ledger = read_signal_ledger()
    ledger = ledger[ledger["run_date"].astype(str) != str(run_date)].copy()
    if recs is None or recs.empty:
        row = {c: "" for c in cols}
        row.update({"run_date": run_date, "locked_at": now_ts(), "is_empty_marker": "1"})
        ledger = pd.concat([ledger, pd.DataFrame([row])], ignore_index=True)
        ledger.to_csv(SIGNAL_LEDGER_PATH, index=False, encoding="utf-8-sig")
        health.append(f"OK signal locked for {run_date}: empty recommendation")
        return
    rows = recs.copy()
    rows["run_date"] = run_date
    rows["locked_at"] = now_ts()
    rows["is_empty_marker"] = "0"
    # Keep a stable snapshot so same-day reruns cannot replace symbols.
    for c in cols:
        if c not in rows.columns:
            rows[c] = ""
    rows = rows[cols]
    ledger = pd.concat([ledger, rows], ignore_index=True)
    ledger.to_csv(SIGNAL_LEDGER_PATH, index=False, encoding="utf-8-sig")
    health.append(f"OK signal locked for {run_date}: recommendations={len(rows)}")


def make_health_report(health: List[str], error: Optional[str] = None):
    rows = []
    if error:
        rows.append(("ERROR", error))
    for h in health:
        status = "OK" if h.startswith("OK") else ("WARN" if h.startswith("WARN") else "INFO")
        rows.append((status, h))
    html = "<html><head><meta charset='utf-8'><style>body{font-family:Microsoft JhengHei,Arial;background:#eef2f6}.card{max-width:900px;margin:16px auto;background:#fffdf7;border:1px solid #dde4dc;border-radius:18px;padding:16px}.tbl{width:100%;border-collapse:collapse}.tbl td{border-bottom:1px solid #ddd;padding:8px}.mono{font-family:Consolas,monospace}</style></head><body><div class='card'><h1>V104.6.1 健康檢查</h1><table class='tbl'>"
    for s, h in rows:
        html += f"<tr><td>{escape(s)}</td><td>{escape(h)}</td></tr>"
    html += "</table></div></body></html>"
    HEALTH_PATH.write_text(html, encoding="utf-8")
    try:
        full = {
            "generated_at": now_ts(),
            "app_version": APP_VERSION,
            "error": error or "",
            "standard_mode_only": True,
            "rules": {
                "TW": {"profile": TW_RULE.profile_id, "hold_days": TW_RULE.hold_days, "max_positions": TW_RULE.max_positions, "stop_loss_pct": TW_RULE.stop_loss_pct, "cooldown": "loss1 max21 days, release when TWII/TWOII above MA60"},
                "US": {"profile": US_RULE.profile_id, "hold_days": US_RULE.hold_days, "max_positions": US_RULE.max_positions, "stop_loss_pct": US_RULE.stop_loss_pct, "filter": "ret20<=42.5%, QQQ>MA100"},
            },
            "capital": {"TW": 0.20, "US": 0.80, "total_capital_default": 100000},
            "health_log": health,
        }
        FULL_HEALTH_JSON_PATH.write_text(json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def escape(x) -> str:
    import html
    return html.escape("" if x is None else str(x))


def render_table(df: pd.DataFrame, cols: List[Tuple[str, str]], empty: str) -> str:
    if df is None or df.empty:
        return f"<div class='empty'>{escape(empty)}</div>"
    out = "<div class='tblwrap'><table class='tbl'><thead><tr>"
    for _, label in cols:
        out += f"<th>{escape(label)}</th>"
    out += "</tr></thead><tbody>"
    for _, r in df.iterrows():
        out += "<tr>"
        for col, label in cols:
            val = r.get(col, "")
            out += f"<td>{escape(val)}</td>"
        out += "</tr>"
    out += "</tbody></table></div>"
    return out


def generate_dashboard(recs: pd.DataFrame, positions: pd.DataFrame, trades: pd.DataFrame, alerts: pd.DataFrame, risk: Dict, monitor: Dict, health: List[str], demo: bool):
    active = positions[positions["status"].isin(["HOLD", "NEW", "SELL_DUE"])] if not positions.empty else pd.DataFrame()
    sell_due = active[active["status"] == "SELL_DUE"] if not active.empty else pd.DataFrame()
    closed_today = trades[pd.to_datetime(trades.get("exit_date", pd.Series(dtype=str)), errors="coerce").dt.strftime("%Y-%m-%d") == today_str()] if not trades.empty else pd.DataFrame()
    # Add derived returns for active
    active_show = active.copy()
    if not active_show.empty:
        ep = pd.to_numeric(active_show["entry_price"], errors="coerce"); lp = pd.to_numeric(active_show["latest_price"], errors="coerce")
        active_show["return_pct"] = ((lp / ep - 1) * 100).map(lambda x: fmt_num(x, 2))
        active_show["entry_price_fmt"] = ep.map(lambda x: fmt_num(x, 4))
        active_show["latest_price_fmt"] = lp.map(lambda x: fmt_num(x, 4))
        active_show["stop_loss_line"] = (ep * (1 - pd.to_numeric(active_show["stop_loss_pct"], errors="coerce"))).map(lambda x: fmt_num(x, 4))
    rec_show = pd.DataFrame()
    if not recs.empty:
        rec_show = recs.copy()
        rec_show["price_fmt"] = rec_show["recommended_price"].map(lambda x: fmt_num(x, 4))
        rec_show["score_fmt"] = rec_show["score"].map(lambda x: fmt_num(x, 1))
        rec_show["target_position_amount_fmt"] = rec_show.get("target_position_amount", pd.Series([""]*len(rec_show))).map(lambda x: fmt_num(x, 0) if x != "" else "")
        rec_show["stop_loss_price_fmt"] = rec_show.get("stop_loss_price", pd.Series([""]*len(rec_show))).map(lambda x: fmt_num(x, 4) if x != "" else "")
        rec_show["max_loss_amount_fmt"] = rec_show.get("max_loss_amount", pd.Series([""]*len(rec_show))).map(lambda x: fmt_num(x, 0) if x != "" else "")
    trades_show = trades.copy() if not trades.empty else pd.DataFrame()
    action_summary = pd.read_csv(TODAY_ACTION_SUMMARY_PATH) if TODAY_ACTION_SUMMARY_PATH.exists() else pd.DataFrame()
    if not trades_show.empty:
        trades_show = trades_show.tail(50).iloc[::-1].copy()
    decision_class = "good" if risk.get("risk_level") == "NORMAL" and not monitor.get("paper_block_new_buys") else "warn"
    demo_banner = "<div class='demo'>DEMO 測試模式：不代表正式推薦</div>" if demo else ""
    html = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{APP_VERSION}</title><style>
:root{{--bg:#eef2f6;--card:#fffdf7;--line:#dde4dc;--text:#243447;--muted:#64748b;--head:#eef1e6;--good:#0f766e;--bad:#b91c1c;--warn:#92400e}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font-family:'Microsoft JhengHei',Arial,sans-serif}}.wrap{{max-width:1240px;margin:auto;padding:14px}}.card,.toolbar{{background:var(--card);border:1px solid var(--line);border-radius:18px;margin:14px 0;padding:14px;box-shadow:0 8px 24px rgba(15,23,42,.06)}}h1{{margin:0 0 6px;font-size:26px}}h2{{margin:0 0 12px;font-size:20px}}.sub{{color:var(--muted);font-size:13px;line-height:1.6}}.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}}.metric{{background:#f7f8f3;border:1px solid var(--line);border-radius:16px;padding:12px;text-align:center}}.metric b{{display:block;font-size:26px}}.tabs{{display:flex;gap:6px;flex-wrap:wrap;margin-top:12px}}button{{border:0;border-radius:12px;padding:10px 12px;background:#e8edf1;font-weight:900;cursor:pointer;color:var(--text)}}.tblwrap{{overflow:auto}}.tbl{{border-collapse:collapse;width:100%;font-size:13px;min-width:760px}}th,td{{border-bottom:1px solid #e2e8d7;padding:8px;text-align:left;white-space:nowrap}}th{{background:var(--head)}}.empty{{padding:18px;color:var(--muted)}}.decision{{border-radius:14px;padding:12px;line-height:1.8;font-weight:900}}.good{{background:#ecfdf5;border:1px solid #99f6e4;color:var(--good)}}.warn{{background:#fffbeb;border:1px solid #fde68a;color:var(--warn)}}.bad{{background:#fef2f2;border:1px solid #fecaca;color:var(--bad)}}.demo{{background:#fee2e2;border:2px solid #ef4444;color:#7f1d1d;border-radius:14px;padding:12px;margin:14px 0;font-weight:900}}.hidden{{display:none}}.mono{{font-family:Consolas,Menlo,monospace}}@media(max-width:800px){{.grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}.tbl{{font-size:12px}}}}@media(max-width:520px){{.grid{{grid-template-columns:1fr}}.wrap{{padding:8px}}}}
</style></head><body><div class="wrap">
<div class="toolbar"><h1>{APP_VERSION}</h1><div class="sub">最終融合規則：台股 H80 Top3 Max3 Stop-10% 虧1筆動態冷靜期21天/加權或櫃買站回60MA提前解除｜美股 H70 Top2 Max2 ret20≤42.5% QQQ>100MA Stop-10%｜資金配置目標 TW20% / US80%｜同一天推薦鎖定，重跑不換股</div>{demo_banner}<div class="tabs"><button onclick="show('summary')">總覽</button><button onclick="show('rec')">今日推薦</button><button onclick="show('pos')">紙上持倉</button><button onclick="show('closed')">紙上出場</button><button onclick="show('monitor')">一個月監控</button><button onclick="show('log')">健康檢查</button></div></div>
<section id="summary" class="page"><div class="card"><h2>今日動作摘要</h2>{render_table(action_summary, [('market_label','市場'),('today_action','今日動作'),('open_positions','持倉'),('max_positions','最多'),('sell_due_count','待賣'),('closed_today_count','今日出場'),('risk_level','風控'),('guard_reason','原因')], '尚無今日動作摘要')}</div><div class="card"><div class="grid"><div class="metric"><b>{len(recs)}</b><span>今日推薦</span></div><div class="metric"><b>{len(active)}</b><span>紙上 HOLD</span></div><div class="metric"><b>{len(sell_due)}</b><span>SELL_DUE</span></div><div class="metric"><b>{len(closed_today)}</b><span>今日紙上出場</span></div></div></div>
<div class="card"><h2>本週推薦｜首頁直接顯示</h2>{render_table(rec_show, [('market','市場'),('symbol','代號'),('name_zh','名稱'),('price_fmt','推薦價'),('target_position_amount_fmt','建議投入'),('estimated_shares','預估股數'),('stop_loss_price_fmt','停損價'),('max_loss_amount_fmt','最大虧損'),('recommendation_date','日期'),('action','操作'),('score_fmt','分數'),('reason','理由')], '本週沒有新推薦，非固定週選日只更新持倉與風控')}</div>
<div class="card"><h2>風控 / 紙上防呆</h2><div class="decision {decision_class}">risk_level={escape(risk.get('risk_level'))}｜{escape(risk.get('reason'))}<br>台指夜盤 {fmt_num(risk.get('tw_night_pct'),4)}%｜QQQ {fmt_num(risk.get('QQQ_pct'),4)}%｜SPY {fmt_num(risk.get('SPY_pct'),4)}%｜SOXX {fmt_num(risk.get('SOXX_pct'),4)}%｜SMH {fmt_num(risk.get('SMH_pct'),4)}%｜VIX {fmt_num(risk.get('VIX_pct'),4)}%<br>紙上新買防呆：{'暫停新增' if monitor.get('paper_block_new_buys') else '正常'}｜{escape(monitor.get('guard_reason','OK'))}<br><span class="sub">更新：{escape(risk.get('updated_at'))}｜來源：{escape(risk.get('source'))}</span></div></div>
</section>
<section id="rec" class="page hidden"><div class="card"><h2>本週推薦｜固定週選後才是紙上買入候選</h2>{render_table(rec_show, [('market','市場'),('symbol','代號'),('name_zh','名稱'),('price_fmt','推薦價'),('target_position_amount_fmt','建議投入'),('estimated_shares','預估股數'),('stop_loss_price_fmt','停損價'),('max_loss_amount_fmt','最大虧損'),('recommendation_date','日期'),('action','操作'),('score_fmt','分數'),('reason','理由')], '本週沒有新推薦')}</div></section>
<section id="pos" class="page hidden"><div class="card"><h2>紙上持倉 / SELL_DUE / 停損線</h2>{render_table(active_show, [('market','市場'),('symbol','代號'),('name_zh','名稱'),('entry_date','買進日'),('entry_price_fmt','買進價'),('latest_date','最新日'),('latest_price_fmt','最新價'),('return_pct','報酬%'),('hold_days','持有天數'),('planned_hold_bars','H'),('stop_loss_line','停損線'),('status','狀態')], '目前沒有紙上持倉')}</div></section>
<section id="closed" class="page hidden"><div class="card"><h2>最近紙上出場</h2>{render_table(trades_show, [('market','市場'),('symbol','代號'),('name_zh','名稱'),('entry_date','買進日'),('entry_price','買進價'),('exit_date','出場日'),('exit_price','出場價'),('realized_return_pct','報酬%'),('exit_reason','原因')], '尚無紙上出場')}</div></section>
<section id="monitor" class="page hidden"><div class="card"><h2>紙上交易監控</h2>{render_table(pd.DataFrame([monitor]), [('closed_trades','已出場筆數'),('open_positions','持倉數'),('win_rate_pct','勝率%'),('avg_realized_pct','平均已實現%'),('avg_open_pct','平均未實現%'),('consecutive_losses','連續虧損'),('month_return_pct_est','本月估算%'),('month_drawdown_pct_est','本月回撤估算%'),('guard_reason','防呆狀態')], '無監控資料')}<div class="sub">說明：V104.6 只監控，不會自動校正核心規則；一個月後產生校正建議報告。</div></div>
<div class="card"><h2>今日提醒</h2>{render_table(alerts, [('level','等級'),('market','市場'),('symbol','代號'),('message','訊息')], '今日沒有提醒')}</div></section>
<section id="log" class="page hidden"><div class="card"><h2>健康檢查</h2><ul>{''.join('<li>'+escape(h)+'</li>' for h in health)}</ul><div class="sub mono">dashboard: output/v1046_paper_dashboard.html<br>positions: state/v1046_paper_positions.csv<br>closed trades: state/v1046_paper_closed_trades.csv<br>today recs: output/v1046_today_recommendations_SIMPLE.csv<br>signal ledger: state/v1046_daily_signal_ledger.csv<br>risk guard: output/v1046_risk_guard_status.csv</div></div></section>
</div><script>function show(id){{document.querySelectorAll('.page').forEach(x=>x.classList.add('hidden'));document.getElementById(id).classList.remove('hidden')}}window.addEventListener('DOMContentLoaded',()=>show('summary'));</script></body></html>"""
    DASHBOARD_PATH.write_text(html, encoding="utf-8")


def generate_calibration_report(monitor: Dict, trades: pd.DataFrame, positions: pd.DataFrame):
    # Always generate a short report. After 20+ closed trades, it will have more info.
    total_closed = len(trades) if trades is not None else 0
    if trades is not None and not trades.empty:
        t = trades.copy(); t["ret"] = pd.to_numeric(t["realized_return_pct"], errors="coerce")
        by_market = t.groupby("market")["ret"].agg(["count", "mean", lambda s: (s > 0).mean()*100]).reset_index()
        by_market.columns = ["market", "closed_trades", "avg_return_pct", "win_rate_pct"]
    else:
        by_market = pd.DataFrame(columns=["market", "closed_trades", "avg_return_pct", "win_rate_pct"])
    suggestion = "樣本不足，繼續紙上跑；不自動改規則。" if total_closed < 20 else "已有初步樣本，可以人工檢查是否需要 V104.4 校正；系統仍不自動改規則。"
    html = f"""<html><head><meta charset='utf-8'><style>body{{font-family:Microsoft JhengHei,Arial;background:#eef2f6}}.card{{max-width:980px;margin:16px auto;background:#fffdf7;border:1px solid #dde4dc;border-radius:18px;padding:16px}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:8px;text-align:left}}</style></head><body><div class='card'><h1>V104.6 一個月校正監控報告</h1><p>產生時間：{now_ts()}</p><p><b>結論：</b>{escape(suggestion)}</p><p>注意：這份報告只產生建議，不會自動修改 FINAL 核心規則。</p></div><div class='card'><h2>監控摘要</h2>{by_market.to_html(index=False, escape=True)}</div></body></html>"""
    CALIBRATION_PATH.write_text(html, encoding="utf-8")


def run_pipeline(demo: bool = False, force: bool = False) -> int:
    settings = load_settings()
    health: List[str] = []
    error = None
    sheets_ctx = None
    try:
        ensure_universe_files()
        if _setting_bool(settings, "auto_universe_on_run", True) and not demo:
            refresh_auto_universe_files(settings, health)
        if sync_before_run is not None:
            sheets_ctx = sync_before_run(health, demo=demo)
        tw_univ = read_universe("TW")
        us_univ = read_universe("US")
        if demo:
            tw_daily = demo_daily(tw_univ, "TW")
            us_daily = demo_daily(us_univ, "US")
            health.append(f"OK DEMO TW daily rows={len(tw_daily)} symbols={tw_daily['symbol'].nunique()}")
            health.append(f"OK DEMO US daily rows={len(us_daily)} symbols={us_daily['symbol'].nunique()}")
        else:
            tw_daily = fetch_daily_yfinance(tw_univ, "TW", settings, health)
            us_daily = fetch_daily_yfinance(us_univ, "US", settings, health)
        daily_all = pd.concat([tw_daily, us_daily], ignore_index=True)
        risk = fetch_risk(settings, demo, health)
        tw_feat = compute_features(tw_daily, TW_RULE)
        us_feat = compute_features(us_daily, US_RULE)
        tw_feat.to_csv(OUTPUT_DIR / "v1046_tw_latest_features.csv", index=False, encoding="utf-8-sig")
        us_feat.to_csv(OUTPUT_DIR / "v1046_us_latest_features.csv", index=False, encoding="utf-8-sig")
        health.append(f"OK TW latest features date={tw_feat['date'].max() if not tw_feat.empty else 'NA'} rows={len(tw_feat)}")
        health.append(f"OK US latest features date={us_feat['date'].max() if not us_feat.empty else 'NA'} rows={len(us_feat)}")
        positions = read_positions()
        trades = read_trades()
        positions, trades, alerts = update_positions(positions, trades, daily_all, settings, health)
        monitor_df, monitor, paper_block = compute_monitor(positions, trades, settings)
        run_date = today_str()
        guard_status = {
            "TW": evaluate_market_risk_guard("TW", trades, run_date, settings, health),
            "US": evaluate_market_risk_guard("US", trades, run_date, settings, health),
        }
        write_risk_guard_status(guard_status)
        for m, gs in guard_status.items():
            state = "BLOCK" if gs.get("block_new_buys") else "OK"
            health.append(f"V104.6 GUARD {m}: {state} / {gs.get('guard_name')} / {gs.get('reason')} / {gs.get('settings')}")
        locked_recs, locked = (pd.DataFrame(), False) if demo else get_today_locked_recommendations(run_date, health)
        if locked:
            recs = apply_v1046_guards_to_recommendations(locked_recs, guard_status, health)
        else:
            tw_rec = make_recommendations(tw_feat, TW_RULE, risk, positions, settings, health)
            us_rec = make_recommendations(us_feat, US_RULE, risk, positions, settings, health)
            recs = pd.concat([tw_rec, us_rec], ignore_index=True) if (not tw_rec.empty or not us_rec.empty) else pd.DataFrame()
            recs = apply_v1046_guards_to_recommendations(recs, guard_status, health)
            if not demo:
                lock_today_recommendations(recs, run_date, health)
        recs = enrich_trade_sizing(recs, settings)
        action_summary = build_today_action_summary(recs, positions, trades, alerts, risk, monitor, settings)
        write_today_action_summary(action_summary)
        write_recommendations(recs)
        if not demo:
            positions = add_new_positions(positions, recs, settings, paper_block, health)
            # recompute monitor after additions
            monitor_df, monitor, paper_block = compute_monitor(positions, trades, settings)
            action_summary = build_today_action_summary(recs, positions, trades, alerts, risk, monitor, settings)
            write_today_action_summary(action_summary)
            write_positions(positions)
            write_trades(trades)
        else:
            health.append("INFO DEMO mode: recommendations not written into official paper positions")
        generate_dashboard(recs, positions, trades, alerts, risk, monitor, health, demo)
        generate_calibration_report(monitor, trades, positions)
        if sync_after_run is not None:
            try:
                sync_after_run(sheets_ctx, health, demo=demo, status="success")
            except Exception as sync_exc:
                health.append(f"WARN Google Sheets sync after run failed: {type(sync_exc).__name__}: {sync_exc}")
        make_health_report(health)
        print(f"[OK] dashboard: {DASHBOARD_PATH}")
        print(f"[OK] today recs: {TODAY_RECS_SIMPLE_PATH}")
        print(f"[OK] positions: {POSITIONS_PATH}")
        return 0
    except Exception:
        error = traceback.format_exc()
        print(error)
        # Attempt failure dashboard
        risk = {"risk_level":"ERROR", "reason": str(error).splitlines()[-1] if error else "ERROR", "source":"exception", "updated_at":now_ts(), "tw_night_pct":0,"QQQ_pct":0,"SPY_pct":0,"SOXX_pct":0,"SMH_pct":0,"VIX_pct":0}
        monitor = {"paper_block_new_buys": True, "guard_reason":"ERROR", "closed_trades":0, "open_positions":0, "win_rate_pct":"", "avg_realized_pct":"", "avg_open_pct":"", "consecutive_losses":0, "month_return_pct_est":"", "month_drawdown_pct_est":""}
        try:
            generate_dashboard(pd.DataFrame(), read_positions(), read_trades(), pd.DataFrame([{"level":"ERROR", "market":"", "symbol":"", "message":risk["reason"]}]), risk, monitor, health, demo)
        except Exception:
            pass
        if sync_after_run is not None and sheets_ctx is not None:
            try:
                sync_after_run(sheets_ctx, health, demo=demo, status="failed", error=error or "")
            except Exception as sync_exc:
                health.append(f"WARN Google Sheets failure sync failed: {type(sync_exc).__name__}: {sync_exc}")
        make_health_report(health, error=error)
        return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="Run demo mode without network and without writing official paper positions")
    ap.add_argument("--force", action="store_true", help="Reserved for future use")
    args = ap.parse_args()
    return run_pipeline(demo=args.demo, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
