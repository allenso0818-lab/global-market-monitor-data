"""Daily heat-map snapshots for every equity index used by the dashboard.

The pipeline prefers a complete official constituent list. Where a free official
weight feed is unavailable, it uses the broadest transparent tracking-ETF or
exchange-universe proxy available and labels that methodology in the snapshot.
A failed refresh never overwrites a previously validated snapshot.
"""
from __future__ import annotations

import csv
import html
import io
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
import yfinance as yf

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
OUT = Path("snapshots")
OUT.mkdir(exist_ok=True)
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; GlobalMarketMonitor/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/json,text/csv,*/*",
})


@dataclass(frozen=True)
class Spec:
    name: str
    yahoo_index: str
    source: str
    url: str
    minimum: int
    maximum: int
    note: str
    methodology: str
    min_price_ratio: float = 0.65


REGISTRY: dict[str, Spec] = {
    "sp": Spec(
        "S&P 500", "^GSPC", "ishares_csv",
        "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/latest-holdings.csv",
        450, 550, "iShares Core S&P 500 ETF (IVV)", "Full ETF holdings proxy", 0.90,
    ),
    "nasdaq": Spec(
        "Nasdaq Composite", "^IXIC", "nasdaq_exchange",
        "https://api.nasdaq.com/api/screener/stocks",
        2000, 6000,
        "Nasdaq public stock screener — Nasdaq-listed equity universe proxy",
        "Nasdaq-listed equity universe proxy; market-cap tile sizing", 0.85,
    ),
    "dow": Spec(
        "Dow Jones Industrial Average", "^DJI", "disabled", "", 30, 30,
        "Local verified 30-component list is served by the website API",
        "Official-size component basket", 0.90,
    ),
    "russell": Spec(
        "Russell 2000", "^RUT", "ishares_csv",
        "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund",
        1700, 2200, "iShares Russell 2000 ETF (IWM)", "Full tracking-ETF holdings proxy", 0.85,
    ),
    "ftse": Spec(
        "FTSE 100", "^FTSE", "ishares_csv",
        "https://www.ishares.com/uk/individual/en/products/251795/ishares-ftse-100-ucits-etf/1467271812596.ajax?fileType=csv&fileName=ISF_holdings&dataType=fund",
        90, 130, "iShares Core FTSE 100 UCITS ETF (ISF)", "Full tracking-ETF holdings proxy", 0.85,
    ),
    "stoxx": Spec(
        "STOXX Europe 600", "^STOXX", "ishares_page",
        "https://www.ishares.com/ch/individual/en/products/251931/ishares-stoxx-europe-600-ucits-etf-de-fund?siteEntryPassthrough=true&tab=holdings",
        550, 650, "iShares STOXX Europe 600 UCITS ETF (EXSA)", "Full tracking-ETF holdings proxy", 0.65,
    ),
    "nikkei": Spec(
        "Nikkei 225", "^N225", "moneydj",
        "https://www.moneydj.com/ETF/X/Basic/Basic0007B.xdjhtm?etfid=1321.JP",
        220, 230, "NEXT FUNDS Nikkei 225 ETF (1321) holdings",
        "Full tracking-ETF holdings proxy", 0.90,
    ),
    "topix": Spec(
        "TOPIX", "^TOPX", "moneydj",
        "https://www.moneydj.com/ETF/X/Basic/Basic0007B.xdjhtm?etfid=1306.JP",
        1500, 2500, "NEXT FUNDS TOPIX ETF (1306) holdings",
        "Full tracking-ETF holdings proxy", 0.80,
    ),
    "hsi": Spec(
        "Hang Seng Index", "^HSI", "moneydj",
        "https://www.moneydj.com/ETF/X/Basic/Basic0007B.xdjhtm?etfid=2800.HK",
        80, 110, "Tracker Fund of Hong Kong (2800) holdings",
        "Full tracking-ETF holdings proxy", 0.90,
    ),
    "shanghai": Spec(
        "Shanghai Composite", "000001.SS", "eastmoney_shanghai",
        "https://push2delay.eastmoney.com/api/qt/clist/get",
        1800, 3500, "Eastmoney Shanghai-listed A/B-share universe",
        "Shanghai-listed equity universe proxy; free-float market-cap tile sizing", 0.85,
    ),
    "msci": Spec(
        "MSCI Emerging Markets", "EEM", "ishares_csv",
        "https://www.ishares.com/us/products/239637/ishares-msci-emerging-markets-etf/latest-holdings.csv",
        500, 2000, "iShares MSCI Emerging Markets ETF (EEM)", "Full tracking-ETF holdings proxy", 0.70,
    ),
}


def number(value, default=0.0):
    if value is None:
        return default
    text = str(value).strip().replace(",", "").replace("%", "").replace("$", "")
    if text in {"", "-", "--", "N/A", "None", "nan"}:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def yahoo_symbol(ticker: str, location: str, spec: Spec) -> str:
    ticker = html.unescape(str(ticker)).strip()
    if ticker.endswith(".JP"):
        return ticker[:-3] + ".T"
    if ticker.endswith(".HK"):
        return ticker
    ticker = re.sub(r"\s+", "-", ticker.replace(".", "-"))
    if spec.yahoo_index == "^FTSE":
        return f"{ticker}.L"
    if spec.source == "eastmoney_shanghai":
        return ticker + ".SS"
    if location == "China" and ticker.isdigit():
        if len(ticker) <= 5:
            return ticker.zfill(4) + ".HK"
        return ticker + (".SS" if ticker.startswith(("6", "9")) else ".SZ")
    suffix = {
        "Hong Kong": ".HK", "Taiwan": ".TW", "South Korea": ".KS",
        "India": ".NS", "Brazil": ".SA", "Mexico": ".MX", "South Africa": ".JO",
        "Malaysia": ".KL", "Indonesia": ".JK", "Thailand": ".BK", "Turkey": ".IS",
        "Poland": ".WA", "Saudi Arabia": ".SR", "United Kingdom": ".L",
        "Germany": ".DE", "France": ".PA", "Netherlands": ".AS", "Switzerland": ".SW",
        "Spain": ".MC", "Italy": ".MI", "Sweden": ".ST", "Denmark": ".CO",
        "Finland": ".HE", "Norway": ".OL", "Belgium": ".BR", "Austria": ".VI",
        "Portugal": ".LS",
    }.get(location)
    if suffix:
        if suffix == ".HK" and ticker.isdigit():
            ticker = ticker.zfill(4)
        return ticker + suffix
    return ticker


def parse_ishares_csv(text: str, spec: Spec):
    lines = text.splitlines()
    start = next(
        i for i, line in enumerate(lines)
        if "Ticker" in line and "Weight" in line and "," in line
    )
    result = []
    for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        ticker = (row.get("Ticker") or row.get("Issuer Ticker") or "").strip()
        location = (row.get("Location") or row.get("Country") or "").strip()
        weight = number(row.get("Weight (%)"))
        asset_class = (row.get("Asset Class") or "").lower()
        if not ticker or weight <= 0 or (asset_class and asset_class != "equity"):
            continue
        result.append({
            "ticker": yahoo_symbol(ticker, location, spec),
            "name": (row.get("Name") or ticker).strip(),
            "sector": (row.get("Sector") or "Other").strip(),
            "source_weight": weight,
        })
    return result


def ishares_holdings(spec: Spec):
    url = spec.url
    if spec.source == "ishares_page":
        page = SESSION.get(url, timeout=45)
        page.raise_for_status()
        candidates = re.findall(r'href=["\']([^"\']+(?:fileType=csv|dataType=fund)[^"\']*)', page.text, re.I)
        candidates = [html.unescape(x) for x in candidates if "hold" in x.lower() or "ajax" in x.lower()]
        if not candidates:
            # iShares uses a stable holdings-download endpoint token on many European product pages.
            product_id = re.search(r"/products/(\d+)/", url)
            if not product_id:
                raise RuntimeError("unable to discover iShares holdings download")
            pid = product_id.group(1)
            candidates = [
                f"https://www.ishares.com/ch/individual/en/products/{pid}/_/1506575576011.ajax?fileType=csv&fileName=EXSA_holdings&dataType=fund",
                f"https://www.ishares.com/ch/individual/en/products/{pid}/_/1467271812596.ajax?fileType=csv&fileName=EXSA_holdings&dataType=fund",
            ]
        errors = []
        for candidate in candidates:
            try:
                target = urljoin(url, candidate)
                response = SESSION.get(target, timeout=45)
                response.raise_for_status()
                rows = parse_ishares_csv(response.text, spec)
                if rows:
                    return rows
            except Exception as exc:
                errors.append(str(exc))
        raise RuntimeError("iShares holdings download failed: " + "; ".join(errors[-2:]))
    response = SESSION.get(url, timeout=45)
    response.raise_for_status()
    return parse_ishares_csv(response.text, spec)


def moneydj_holdings(spec: Spec):
    response = SESSION.get(spec.url, timeout=45)
    response.raise_for_status()
    tables = pd.read_html(io.StringIO(response.text))
    best = []
    for frame in tables:
        if frame.shape[1] < 2:
            continue
        candidate = []
        for _, row in frame.iterrows():
            label = str(row.iloc[0]).strip()
            match = re.search(r"\((\d{4}[A-Z]?\.(?:JP|HK))\)", label)
            if not match:
                continue
            ticker = match.group(1)
            weight = number(row.iloc[1])
            if weight <= 0:
                continue
            name = re.sub(r"\s*\([^)]*\)\s*$", "", label).strip() or ticker
            candidate.append({
                "ticker": yahoo_symbol(ticker, "", spec),
                "name": name,
                "sector": "Other",
                "source_weight": weight,
            })
        if len(candidate) > len(best):
            best = candidate
    if not best:
        raise RuntimeError("MoneyDJ full holdings table not found")
    return best


def nasdaq_exchange_holdings(spec: Spec):
    response = SESSION.get(
        spec.url,
        params={"tableonly": "true", "limit": "5000", "offset": "0", "exchange": "nasdaq", "download": "true"},
        headers={"Referer": "https://www.nasdaq.com/market-activity/stocks/screener"},
        timeout=60,
    )
    response.raise_for_status()
    rows = ((response.json().get("data") or {}).get("rows") or [])
    now = datetime.now(timezone.utc).isoformat()
    result = []
    for row in rows:
        ticker = str(row.get("symbol") or "").strip().replace(".", "-")
        cap = number(row.get("marketCap"))
        price = number(row.get("lastsale"), None)
        pct = number(row.get("pctchange"), None)
        if not ticker or not cap or cap <= 0:
            continue
        previous = None
        change = None
        if price is not None and pct is not None and abs(100 + pct) > 1e-9:
            previous = price / (1 + pct / 100)
            change = price - previous
        result.append({
            "ticker": ticker,
            "name": str(row.get("name") or ticker).strip(),
            "sector": str(row.get("sector") or "Other").strip(),
            "source_weight": cap,
            "price": price,
            "daily_change": change,
            "daily_change_pct": pct,
            "price_timestamp": now,
        })
    return result


def eastmoney_shanghai_holdings(spec: Spec):
    params = {
        "pn": "1", "pz": "5000", "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:1 t:2,m:1 t:23,m:1 t:3",
        "fields": "f2,f3,f4,f12,f13,f14,f20,f21",
    }
    response = SESSION.get(spec.url, params=params, timeout=60)
    response.raise_for_status()
    data = response.json().get("data") or {}
    rows = data.get("diff") or []
    now = datetime.now(timezone.utc).isoformat()
    result = []
    for row in rows:
        ticker = str(row.get("f12") or "").strip()
        cap = number(row.get("f21")) or number(row.get("f20"))
        price = number(row.get("f2"), None)
        pct = number(row.get("f3"), None)
        change = number(row.get("f4"), None)
        if not ticker or not cap or cap <= 0:
            continue
        result.append({
            "ticker": yahoo_symbol(ticker, "", spec),
            "name": str(row.get("f14") or ticker).strip(),
            "sector": "Other",
            "source_weight": cap,
            "price": price,
            "daily_change": change,
            "daily_change_pct": pct,
            "price_timestamp": now,
        })
    return result


def holdings(spec: Spec):
    if spec.source in {"ishares_csv", "ishares_page"}:
        return ishares_holdings(spec)
    if spec.source == "moneydj":
        return moneydj_holdings(spec)
    if spec.source == "nasdaq_exchange":
        return nasdaq_exchange_holdings(spec)
    if spec.source == "eastmoney_shanghai":
        return eastmoney_shanghai_holdings(spec)
    if spec.source == "disabled":
        raise RuntimeError("served by website-local verified list")
    raise RuntimeError(f"unsupported source {spec.source}")


def enrich(rows):
    missing = [r["ticker"] for r in rows if r.get("price") is None or r.get("daily_change_pct") is None]
    now = datetime.now(timezone.utc).isoformat()
    frames = {}
    for start in range(0, len(missing), 40):
        batch = missing[start:start + 40]
        if not batch:
            continue
        try:
            data = yf.download(
                batch, period="5d", interval="1d", group_by="ticker",
                threads=True, progress=False, auto_adjust=False,
            )
            for symbol in batch:
                try:
                    frames[symbol] = data[symbol] if len(batch) > 1 else data
                except Exception:
                    pass
        except Exception as exc:
            logging.warning("Yahoo batch failed: %s", exc)
        if start + 40 < len(missing):
            time.sleep(0.6)
    for row in rows:
        if row.get("price") is not None and row.get("daily_change_pct") is not None:
            row.setdefault("price_timestamp", now)
            continue
        try:
            closes = frames[row["ticker"]]["Close"].dropna()
            if len(closes) < 2:
                raise ValueError("insufficient closes")
            row["price"] = round(float(closes.iloc[-1]), 6)
            row["daily_change"] = round(float(closes.iloc[-1] - closes.iloc[-2]), 6)
            row["daily_change_pct"] = round(float((closes.iloc[-1] / closes.iloc[-2] - 1) * 100), 6)
        except Exception:
            row["price"] = row.get("price")
            row["daily_change"] = row.get("daily_change")
            row["daily_change_pct"] = row.get("daily_change_pct")
        row["price_timestamp"] = row.get("price_timestamp") or now
    return rows


def validate(spec: Spec, rows):
    seen = set()
    clean = []
    for row in rows:
        ticker = row.get("ticker")
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        clean.append(row)
    if not spec.minimum <= len(clean) <= spec.maximum:
        raise ValueError(f"coverage {len(clean)} outside {spec.minimum}-{spec.maximum}")
    total = sum(number(row.get("source_weight")) for row in clean)
    if total <= 0:
        raise ValueError("non-positive total weight")
    for row in clean:
        row["heatmap_weight"] = number(row.get("source_weight")) / total * 100
    quote_ratio = sum(row.get("price") is not None and row.get("daily_change_pct") is not None for row in clean) / len(clean)
    if quote_ratio < spec.min_price_ratio:
        raise ValueError(f"insufficient quote coverage {quote_ratio:.1%} < {spec.min_price_ratio:.1%}")
    return clean


def refresh(key: str, spec: Spec):
    if spec.source == "disabled":
        logging.info("%s: served from website-local complete list", key)
        return
    try:
        rows = validate(spec, enrich(holdings(spec)))
        payload = {
            "index": key,
            "index_name": spec.name,
            "yahoo_index_ticker": spec.yahoo_index,
            "weight_methodology": spec.methodology,
            "constituent_source": spec.note,
            "price_source": "Source snapshot / Yahoo Finance via yfinance",
            "holdings_updated": datetime.now(timezone.utc).isoformat(),
            "coverage_count": len(rows),
            "constituents": rows,
        }
        (OUT / f"{key}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        logging.info("%s: wrote %s validated constituents", key, len(rows))
    except Exception as exc:
        logging.warning("%s: retained previous validated snapshot (%s)", key, exc)


for key, spec in REGISTRY.items():
    refresh(key, spec)
