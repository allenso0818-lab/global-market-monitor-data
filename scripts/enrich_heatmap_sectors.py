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
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
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
    # JPX publishes a stable listed-issues workbook containing the official
    # 33-sector classification. Use the stable file first, then discover a
    # replacement link if JPX changes the attachment path.
    workbook_urls = [
        "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls",
    ]
    for page_url in [
        "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html",
        "https://www.jpx.co.jp/english/markets/statistics-equities/misc/01.html",
    ]:
        try:
            page = S.get(page_url, timeout=45)
            page.raise_for_status()
            links = re.findall(r'href=["\']([^"\']+\.(?:xls|xlsx)(?:\?[^"\']*)?)["\']', page.text, re.I)
            workbook_urls.extend(urljoin(page.url, link) for link in links)
        except Exception:
            pass

    last_error = None
    for workbook_url in dict.fromkeys(workbook_urls):
        try:
            data = S.get(workbook_url, timeout=60)
            data.raise_for_status()
            df = pd.read_excel(io.BytesIO(data.content), dtype=str)
            cols = {str(c).strip(): c for c in df.columns}
            code_col = next((c for k, c in cols.items() if k in {"コード", "Code"}), None)
            sector_col = next((c for k, c in cols.items() if k in {"33業種区分", "33 Sector(name)", "33 Sector"}), None)
            if code_col is None:
                code_col = next((c for k, c in cols.items() if "コード" in k or k.lower() == "code"), None)
            if sector_col is None:
                sector_col = next((c for k, c in cols.items() if ("33業種" in k and "コード" not in k) or ("33 sector" in k.lower() and "code" not in k.lower())), None)
            if code_col is None or sector_col is None:
                raise RuntimeError(f"JPX sector columns not found: {list(df.columns)}")
            result = {}
            for _, row in df.iterrows():
                code = str(row[code_col]).strip()
                sector = str(row[sector_col]).strip()
                if re.fullmatch(r"(?:\d{4}|\d{3}[A-Z])", code) and sector and sector.lower() != "nan":
                    result[code + ".T"] = sector
            if len(result) >= 1000:
                return result
            last_error = RuntimeError(f"JPX sector map incomplete: {len(result)}")
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"JPX listed-issues sector source unavailable: {last_error}")


def hsi_map() -> dict[str, str]:
    # The HSI's traditional sector/sub-index classification is available in a
    # structured components table. It is a robust fallback when Yahoo profile
    # metadata blocks automated access.
    urls = [
        "https://en.wikipedia.org/wiki/Hang_Seng_Index",
        "https://zh.wikipedia.org/wiki/%E6%81%92%E7%94%9F%E6%8C%87%E6%95%B8",
    ]
    result: dict[str, str] = {}
    for url in urls:
        try:
            response = S.get(url, timeout=45)
            response.raise_for_status()
            for df in pd.read_html(io.StringIO(response.text)):
                cols = {str(c).strip().lower(): c for c in df.columns}
                ticker_col = next((c for k, c in cols.items() if "ticker" in k or "股份代號" in k), None)
                sector_col = next((c for k, c in cols.items() if "sub-index" in k or "sector" in k or "行業" in k or "分类" in k or "分類" in k), None)
                if ticker_col is None or sector_col is None:
                    continue
                for _, row in df.iterrows():
                    raw_ticker = str(row[ticker_col])
                    match = re.search(r"(\d{1,5})", raw_ticker.replace(",", ""))
                    if not match:
                        continue
                    sector = str(row[sector_col]).strip()
                    if not sector or sector.lower() == "nan":
                        continue
                    result[match.group(1).zfill(4) + ".HK"] = sector
        except Exception:
            continue

    # Recent additions may not yet be reflected in mirrored tables. These are
    # all non-financial/non-property/non-utility constituents and therefore sit
    # in the HSI Commerce & Industry sub-index until the source tables catch up.
    for ticker in ["1801.HK", "3750.HK", "6181.HK"]:
        result.setdefault(ticker, "Commerce & Industry")

    snapshot, _ = load_snapshot("hsi")
    if snapshot:
        missing = [str(r.get("ticker") or "") for r in snapshot.get("constituents", []) if str(r.get("ticker") or "") not in result]
        if missing:
            logging.warning("HSI sector source missing %d tickers: %s", len(missing), ", ".join(missing[:12]))
    if len(result) < 80:
        raise RuntimeError(f"HSI sector map incomplete: {len(result)}")
    return result


def shanghai_map() -> dict[str, str]:
    # Prefer the official SSE industry-classification pages. This avoids a
    # single-point dependency on Eastmoney's occasionally unavailable API.
    main_url = "https://www.sse.com.cn/assortment/stock/areatrade/trade/"
    response = S.get(main_url, timeout=45)
    response.raise_for_status()
    industry_rows: list[tuple[str, str]] = []
    for df in pd.read_html(io.StringIO(response.text)):
        cols = {str(c).strip(): c for c in df.columns}
        code_col = next((c for k, c in cols.items() if "行业代码" in k), None)
        name_col = next((c for k, c in cols.items() if "行业名称" in k), None)
        if code_col is None or name_col is None:
            continue
        for _, row in df.iterrows():
            code = str(row[code_col]).strip()
            name = str(row[name_col]).strip()
            if re.fullmatch(r"[A-Z]\d{2}", code) and name and name.lower() != "nan":
                industry_rows.append((code, name))
    if not industry_rows:
        raise RuntimeError("SSE industry classification table unavailable")

    result: dict[str, str] = {}
    for code, industry_name in industry_rows:
        detail_url = f"https://www.sse.com.cn/assortment/stock/areatrade/trade/detail.shtml?csrcCode={code}"
        detail = S.get(detail_url, timeout=45)
        detail.raise_for_status()
        for df in pd.read_html(io.StringIO(detail.text)):
            cols = {str(c).strip(): c for c in df.columns}
            stock_col = next((c for k, c in cols.items() if "A股代码" in k or "上市公司代码" in k), None)
            if stock_col is None:
                continue
            for value in df[stock_col].tolist():
                stock = re.sub(r"\D", "", str(value))
                if re.fullmatch(r"6\d{5}", stock):
                    result[stock + ".SS"] = industry_name
    if len(result) < 1800:
        raise RuntimeError(f"SSE sector map incomplete: {len(result)}")
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
    ("hsi", hsi_map),
    ("shanghai", shanghai_map),
]:
    try:
        apply_map(key, builder())
    except Exception as exc:
        logging.warning("%s sector enrichment failed safely: %s", key, exc)

audit()
