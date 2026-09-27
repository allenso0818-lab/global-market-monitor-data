"""Enrich sector classifications for heat-map snapshots without changing constituents.

This stage runs after the existing constituent/price refreshes. It only fills rows
whose sector is missing/Other, preserves the existing universe, weights and quote
coverage, and never overwrites a validated snapshot if an upstream sector source
fails. The final audit prints per-index classified coverage for verification.
"""
from __future__ import annotations

import io
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
import yfinance as yf
from lxml import html as lxml_html

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
OUT = Path("snapshots")
S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36 GlobalMarketMonitor/1.0",
    "Accept-Language": "en-US,en;q=0.9",
})


def load_snapshot(key: str):
    path = OUT / f"{key}.json"
    if not path.exists():
        return None, path
    return json.loads(path.read_text(encoding="utf-8")), path


def save_snapshot(snapshot, path: Path):
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")


def missing_sector(row) -> bool:
    sector = str(row.get("sector") or "").strip()
    return not sector or sector.lower() == "other"


def apply_map(key: str, sector_map: dict[str, str]):
    snapshot, path = load_snapshot(key)
    if not snapshot:
        return
    changed = 0
    for row in snapshot.get("constituents", []):
        ticker = str(row.get("ticker") or "").strip()
        sector = sector_map.get(ticker)
        if missing_sector(row) and sector and sector.lower() != "other":
            row["sector"] = sector
            changed += 1
    if changed:
        save_snapshot(snapshot, path)
    covered = sum(not missing_sector(r) for r in snapshot.get("constituents", []))
    total = len(snapshot.get("constituents", []))
    logging.info("%s sectors: %d/%d covered (%d newly filled)", key, covered, total, changed)


def ftse_map() -> dict[str, str]:
    response = S.get("https://en.wikipedia.org/wiki/FTSE_100_Index", timeout=45)
    response.raise_for_status()
    for df in pd.read_html(io.StringIO(response.text)):
        cols = {str(c).lower(): c for c in df.columns}
        ticker_col = next((c for k, c in cols.items() if "ticker" in k or "epic" in k), None)
        sector_col = next((c for k, c in cols.items() if "sector" in k or "industry classification" in k), None)
        if ticker_col is None or sector_col is None or len(df) < 80:
            continue
        result = {}
        for _, row in df.iterrows():
            ticker = re.sub(r"[^A-Z0-9.]", "", str(row[ticker_col]).upper())
            if not ticker or ticker == "NAN":
                continue
            ticker = ticker.replace(".", "-").rstrip("-") + ".L"
            sector = str(row[sector_col]).strip()
            if sector and sector.lower() != "nan":
                result[ticker] = sector
        if result:
            return result
    raise RuntimeError("FTSE sector table not found")


def nikkei_map() -> dict[str, str]:
    url = "https://indexes.nikkei.co.jp/en/nkave/index/component?idx=nk225"
    response = S.get(url, timeout=45)
    response.raise_for_status()
    root = lxml_html.fromstring(response.content)
    result: dict[str, str] = {}
    valid_industries = {
        "Fishery", "Mining", "Construction", "Foods", "Textiles & Apparel", "Pulp & Paper",
        "Chemicals", "Pharmaceuticals", "Petroleum", "Rubber", "Glass & Ceramics", "Steel",
        "Nonferrous Metals", "Machinery", "Electric Machinery", "Shipbuilding",
        "Automobiles & Auto parts", "Transportation Equipment", "Precision Instruments",
        "Other Manufacturing", "Trading Companies", "Retail", "Banking", "Other Financial Services",
        "Securities", "Insurance", "Real Estate", "Railway & Bus", "Land Transport",
        "Marine Transport", "Air Transport", "Communications", "Warehousing", "Electric Power",
        "Gas", "Services",
    }
    for heading in root.xpath("//h2|//h3|//h4"):
        sector = " ".join(heading.text_content().split()).strip()
        if sector not in valid_industries:
            continue
        node = heading.getnext()
        while node is not None and node.tag.lower() not in {"h2", "h3", "h4"}:
            if node.tag.lower() == "table":
                for tr in node.xpath(".//tr"):
                    cells = [" ".join(x.text_content().split()) for x in tr.xpath("./th|./td")]
                    if len(cells) < 2:
                        continue
                    code = cells[0].strip()
                    if re.fullmatch(r"(?:\d{4}|\d{3}[A-Z])", code):
                        result[code + ".T"] = sector
                break
            node = node.getnext()
    if len(result) < 200:
        raise RuntimeError(f"Nikkei sector parse incomplete: {len(result)}")
    return result


def topix_map() -> dict[str, str]:
    page_url = "https://www.jpx.co.jp/english/markets/statistics-equities/misc/01.html"
    page = S.get(page_url, timeout=45)
    page.raise_for_status()
    links = re.findall(r'href=["\']([^"\']+\.xls(?:\?[^"\']*)?)["\']', page.text, re.I)
    if not links:
        # Japanese page historically exposes the same free listed-issues workbook.
        jp_url = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
        page = S.get(jp_url, timeout=45)
        page.raise_for_status()
        links = re.findall(r'href=["\']([^"\']+\.xls(?:\?[^"\']*)?)["\']', page.text, re.I)
    if not links:
        raise RuntimeError("JPX listed-issues workbook link not found")
    workbook_url = urljoin(page.url, links[0])
    data = S.get(workbook_url, timeout=60)
    data.raise_for_status()
    df = pd.read_excel(io.BytesIO(data.content), dtype=str)
    cols = {str(c).strip().lower(): c for c in df.columns}
    code_col = next((c for k, c in cols.items() if k == "code" or "code" == k.split()[-1]), None)
    sector_col = next((c for k, c in cols.items() if "33 sector" in k and "code" not in k), None)
    if code_col is None:
        code_col = next((c for k, c in cols.items() if "コード" in k), None)
    if sector_col is None:
        sector_col = next((c for k, c in cols.items() if "33業種" in k and "コード" not in k), None)
    if code_col is None or sector_col is None:
        raise RuntimeError(f"JPX sector columns not found: {list(df.columns)}")
    result = {}
    for _, row in df.iterrows():
        code = str(row[code_col]).strip()
        sector = str(row[sector_col]).strip()
        if re.fullmatch(r"(?:\d{4}|\d{3}[A-Z])", code) and sector and sector.lower() != "nan":
            result[code + ".T"] = sector
    if len(result) < 1000:
        raise RuntimeError(f"JPX sector map incomplete: {len(result)}")
    return result


def shanghai_map() -> dict[str, str]:
    hosts = [
        "https://82.push2.eastmoney.com/api/qt/clist/get",
        "https://push2delay.eastmoney.com/api/qt/clist/get",
    ]
    result = {}
    for page in range(1, 60):
        params = {
            "pn": str(page), "pz": "100", "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2", "fid": "f3",
            "fs": "m:1 t:2,m:1 t:23,m:1 t:3",
            "fields": "f12,f14,f100",
        }
        chunk = None
        for host in hosts:
            try:
                response = S.get(host, params=params, timeout=30)
                response.raise_for_status()
                chunk = ((response.json().get("data") or {}).get("diff") or [])
                break
            except Exception:
                continue
        if chunk is None:
            raise RuntimeError(f"Shanghai sector page {page} unavailable")
        if not chunk:
            break
        for row in chunk:
            code = str(row.get("f12") or "").strip()
            sector = str(row.get("f100") or "").strip()
            if code and sector and sector not in {"-", "None"}:
                result[code + ".SS"] = sector
        if len(chunk) < 100:
            break
    if len(result) < 1000:
        raise RuntimeError(f"Shanghai sector map incomplete: {len(result)}")
    return result


def hsi_yahoo_map() -> dict[str, str]:
    snapshot, _ = load_snapshot("hsi")
    if not snapshot:
        return {}
    tickers = [str(r.get("ticker") or "") for r in snapshot.get("constituents", []) if missing_sector(r)]

    def fetch_one(symbol: str):
        try:
            info = yf.Ticker(symbol).get_info()
            sector = str(info.get("sector") or "").strip()
            return symbol, sector if sector else None
        except Exception:
            return symbol, None

    result = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_one, symbol) for symbol in tickers]
        for future in as_completed(futures):
            symbol, sector = future.result()
            if sector:
                result[symbol] = sector
    if len(result) < max(30, len(tickers) // 2):
        raise RuntimeError(f"HSI Yahoo sector enrichment incomplete: {len(result)}/{len(tickers)}")
    return result


def audit():
    keys = ["sp", "nasdaq", "russell", "ftse", "stoxx", "nikkei", "topix", "hsi", "shanghai", "msci"]
    for key in keys:
        snapshot, _ = load_snapshot(key)
        if not snapshot:
            logging.warning("%s snapshot missing", key)
            continue
        rows = snapshot.get("constituents", [])
        covered = sum(not missing_sector(r) for r in rows)
        logging.info("AUDIT %s: %d/%d sector-classified (%.1f%%)", key, covered, len(rows), (100 * covered / len(rows)) if rows else 0)


for key, builder in [
    ("ftse", ftse_map),
    ("nikkei", nikkei_map),
    ("topix", topix_map),
    ("hsi", hsi_yahoo_map),
    ("shanghai", shanghai_map),
]:
    try:
        apply_map(key, builder())
    except Exception as exc:
        logging.warning("%s sector enrichment failed safely: %s", key, exc)

audit()
