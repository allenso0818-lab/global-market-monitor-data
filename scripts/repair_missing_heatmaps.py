"""Repair heat-map universes that need source-specific parsing.

This runs after refresh_heatmaps.py. It deliberately publishes complete
constituent lists even when some daily quotes cannot be enriched; missing quote
rows remain visible (grey) instead of disappearing from the heat map.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
OUT = Path("snapshots")
OUT.mkdir(exist_ok=True)
S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36 GlobalMarketMonitor/1.0",
    "Accept-Language": "en-US,en;q=0.9",
})
NOW = lambda: datetime.now(timezone.utc).isoformat()


def num(v, default=None):
    if v is None:
        return default
    try:
        s = str(v).strip().replace(",", "").replace("%", "").replace("$", "")
        return float(s) if s not in {"", "-", "--", "nan", "None"} else default
    except Exception:
        return default


def enrich(rows, limit=None):
    """Best-effort daily colours. Coverage never gates constituent publication."""
    ranked = sorted(rows, key=lambda r: num(r.get("source_weight"), 0) or 0, reverse=True)
    targets = [r for r in ranked if r.get("daily_change_pct") is None]
    if limit:
        targets = targets[:limit]
    by_symbol = {r["ticker"]: r for r in targets}
    symbols = list(by_symbol)
    for start in range(0, len(symbols), 100):
        batch = symbols[start:start + 100]
        if not batch:
            continue
        data = None
        for attempt in range(3):
            try:
                data = yf.download(batch, period="5d", interval="1d", group_by="ticker", threads=True, progress=False, auto_adjust=False)
                break
            except Exception as exc:
                logging.warning("quote batch %d-%d attempt %d failed: %s", start, start + len(batch), attempt + 1, exc)
                time.sleep(1.0 * (attempt + 1))
        if data is None:
            continue
        for symbol in batch:
            try:
                frame = data[symbol] if len(batch) > 1 else data
                closes = frame["Close"].dropna()
                if len(closes) < 2:
                    continue
                row = by_symbol[symbol]
                last = float(closes.iloc[-1]); prev = float(closes.iloc[-2])
                row["price"] = round(last, 6)
                row["daily_change"] = round(last - prev, 6)
                row["daily_change_pct"] = round((last / prev - 1) * 100, 6)
                row["price_timestamp"] = NOW()
            except Exception:
                pass
        if start + 100 < len(symbols):
            time.sleep(0.4)
    return rows


def normalize_and_write(key, name, methodology, source, rows, minimum, maximum):
    clean = []
    seen = set()
    for r in rows:
        ticker = str(r.get("ticker") or "").strip()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        r["ticker"] = ticker
        r.setdefault("name", ticker)
        r.setdefault("sector", "Other")
        r.setdefault("source_weight", 1.0)
        r.setdefault("price", None)
        r.setdefault("daily_change", None)
        r.setdefault("daily_change_pct", None)
        r.setdefault("price_timestamp", NOW())
        clean.append(r)
    if not minimum <= len(clean) <= maximum:
        raise ValueError(f"{key}: coverage {len(clean)} outside {minimum}-{maximum}")
    total = sum(max(num(r.get("source_weight"), 0) or 0, 0) for r in clean)
    if total <= 0:
        total = float(len(clean))
        for r in clean:
            r["source_weight"] = 1.0
    for r in clean:
        r["heatmap_weight"] = (max(num(r.get("source_weight"), 0) or 0, 0) / total * 100) if total else 0
    payload = {
        "index": key,
        "index_name": name,
        "weight_methodology": methodology,
        "constituent_source": source,
        "price_source": "Yahoo Finance best-effort; source-native quote where available",
        "holdings_updated": NOW(),
        "coverage_count": len(clean),
        "constituents": clean,
    }
    (OUT / f"{key}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    logging.info("%s: wrote %d constituents", key, len(clean))


def russell():
    url = "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/latest-holdings.csv"
    r = S.get(url, timeout=60); r.raise_for_status()
    lines = r.text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Ticker,") and "Weight (%)" in line)
    rows = []
    for x in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        if str(x.get("Asset Class") or "").lower() != "equity":
            continue
        ticker = str(x.get("Ticker") or "").strip().replace(".", "-")
        weight = num(x.get("Weight (%)"), 0) or 0
        market_value = num(x.get("Market Value"), 0) or 0
        if not ticker:
            continue
        source_weight = weight if weight > 0 else market_value
        if source_weight <= 0:
            source_weight = 1e-9
        rows.append({"ticker": ticker, "name": x.get("Name") or ticker, "sector": x.get("Sector") or "Other", "source_weight": source_weight, "price": num(x.get("Price"))})
    enrich(rows, limit=700)
    normalize_and_write("russell", "Russell 2000", "Full IWM tracking-ETF holdings proxy", "iShares Russell 2000 ETF (IWM) latest holdings", rows, 1700, 2200)


def ftse():
    url = "https://en.wikipedia.org/wiki/FTSE_100_Index"
    response = S.get(url, timeout=60)
    response.raise_for_status()
    tables = pd.read_html(io.StringIO(response.text))
    rows = []
    for df in tables:
        cols = {str(c).lower(): c for c in df.columns}
        ticker_col = next((c for k, c in cols.items() if "ticker" in k or "epic" in k), None)
        company_col = next((c for k, c in cols.items() if "company" in k), None)
        if ticker_col is None or company_col is None or len(df) < 80:
            continue
        for _, x in df.iterrows():
            ticker = re.sub(r"[^A-Z0-9.]", "", str(x[ticker_col]).upper())
            if not ticker or ticker == "NAN":
                continue
            ticker = ticker.replace(".", "-").rstrip("-")
            ticker += ".L"
            rows.append({"ticker": ticker, "name": str(x[company_col]), "source_weight": 1.0})
        if len(rows) >= 90:
            break
    enrich(rows)
    normalize_and_write("ftse", "FTSE 100", "Complete public constituent list; equal-area fallback", "FTSE 100 public constituent table (Wikipedia; benchmark cross-check: iShares ISF)", rows, 95, 110)


def nikkei():
    url = "https://indexes.nikkei.co.jp/en/nkave/index/component?idx=nk225"
    response = S.get(url, timeout=60)
    response.raise_for_status()
    tables = pd.read_html(io.StringIO(response.text))
    rows = []
    for df in tables:
        if df.shape[1] < 2:
            continue
        for _, x in df.iterrows():
            code = str(x.iloc[0]).strip()
            if not re.fullmatch(r"(?:\d{4}|\d{3}[A-Z])", code):
                continue
            name = str(x.iloc[1]).strip()
            rows.append({"ticker": code + ".T", "name": name, "source_weight": 1.0})
    enrich(rows)
    normalize_and_write("nikkei", "Nikkei 225", "Official complete constituent list; equal-area fallback", "Nikkei Indexes official Nikkei 225 components", rows, 225, 225)


def topix():
    url = "https://www.jpx.co.jp/automation/markets/indices/topix/files/topixweight_j.csv"
    r = S.get(url, timeout=60); r.raise_for_status()
    text = r.content.decode("shift_jis", errors="replace")
    df = pd.read_csv(io.StringIO(text), dtype=str)
    code_col = next(c for c in df.columns if "コード" in str(c))
    name_col = next(c for c in df.columns if "銘柄名" in str(c))
    weight_col = next(c for c in df.columns if "ウエイト" in str(c))
    rows = []
    for _, x in df.iterrows():
        code = str(x[code_col]).strip()
        if not re.match(r"^(?:\d{4}|\d{3}[A-Z])$", code):
            continue
        weight = num(x[weight_col], 0) or 0
        if weight <= 0:
            continue
        rows.append({"ticker": code + ".T", "name": str(x[name_col]).strip(), "source_weight": weight})
    enrich(rows, limit=700)
    normalize_and_write("topix", "TOPIX", "Official JPX free-float component weights", "JPX official TOPIX Component Stocks Weight", rows, 1400, 2200)


def shanghai():
    hosts = [
        "https://82.push2.eastmoney.com/api/qt/clist/get",
        "https://push2delay.eastmoney.com/api/qt/clist/get",
    ]
    rows = []
    for page in range(1, 60):
        params = {
            "pn": str(page), "pz": "100", "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2", "fid": "f3",
            "fs": "m:1 t:2,m:1 t:23,m:1 t:3",
            "fields": "f2,f3,f4,f12,f14,f20,f21",
        }
        chunk = None
        last_error = None
        for attempt in range(4):
            for url in hosts:
                try:
                    r = S.get(url, params=params, timeout=30)
                    r.raise_for_status()
                    candidate = ((r.json().get("data") or {}).get("diff") or [])
                    chunk = candidate
                    break
                except Exception as exc:
                    last_error = exc
            if chunk is not None:
                break
            time.sleep(0.7 * (attempt + 1))
        if chunk is None:
            raise RuntimeError(f"Shanghai page {page} unavailable after retries: {last_error}")
        if not chunk:
            break
        for x in chunk:
            ticker = str(x.get("f12") or "").strip()
            cap = num(x.get("f21")) or num(x.get("f20")) or 0
            if not ticker or cap <= 0:
                continue
            rows.append({
                "ticker": ticker + ".SS", "name": str(x.get("f14") or ticker), "source_weight": cap,
                "price": num(x.get("f2")), "daily_change": num(x.get("f4")), "daily_change_pct": num(x.get("f3")), "price_timestamp": NOW(),
            })
        if len(chunk) < 100:
            break
        time.sleep(0.15)
    normalize_and_write("shanghai", "Shanghai Composite", "Broad Shanghai-listed equity universe; free-float market-cap tile sizing", "Eastmoney Shanghai-listed A/B-share universe", rows, 1800, 3500)


for fn in (russell, ftse, nikkei, topix, shanghai):
    try:
        fn()
    except Exception as exc:
        logging.warning("%s repair failed: %s", fn.__name__, exc)
