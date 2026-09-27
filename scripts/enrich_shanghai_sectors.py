"""Fill Shanghai heatmap sectors from the official SSE listed-company query API.

This stage preserves the existing Shanghai constituent universe, weights and quotes.
It only fills missing/Other sectors and fails closed when official sector coverage is
insufficient, so a broken upstream source can no longer publish a green snapshot.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import requests

SNAPSHOT = Path("snapshots/shanghai.json")
QUERY_URL = "https://query.sse.com.cn/sseQuery/commonQuery.do"
S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
    "Referer": "https://www.sse.com.cn/assortment/stock/list/share/",
    "Accept": "application/json,text/plain,*/*",
})

BROAD = {
    "A": "Agriculture, Forestry, Animal Husbandry & Fishery",
    "B": "Mining",
    "C": "Manufacturing",
    "D": "Electricity, Heat, Gas & Water",
    "E": "Construction",
    "F": "Wholesale & Retail",
    "G": "Transportation, Storage & Postal",
    "H": "Accommodation & Catering",
    "I": "Information Transmission, Software & IT Services",
    "J": "Finance",
    "K": "Real Estate",
    "L": "Leasing & Business Services",
    "M": "Scientific Research & Technical Services",
    "N": "Water Conservancy, Environment & Public Facilities",
    "O": "Resident Services, Repairs & Other Services",
    "P": "Education",
    "Q": "Health & Social Work",
    "R": "Culture, Sports & Entertainment",
    "S": "Conglomerates",
}


def decode_json_or_jsonp(text: str):
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return json.loads(text)
    match = re.search(r"^[^(]+\((.*)\)\s*;?\s*$", text, re.S)
    if not match:
        raise RuntimeError("SSE query response was neither JSON nor JSONP")
    return json.loads(match.group(1))


def fetch_rows(stock_type: str):
    params = {
        "STOCK_TYPE": stock_type,
        "REG_PROVINCE": "",
        "CSRC_CODE": "",
        "STOCK_CODE": "",
        "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
        "COMPANY_STATUS": "2,4,5,7,8",
        "type": "inParams",
        "isPagination": "true",
        "pageHelp.cacheSize": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.pageSize": "5000",
        "pageHelp.pageNo": "1",
        "pageHelp.endPage": "1",
    }
    response = S.get(QUERY_URL, params=params, timeout=60)
    response.raise_for_status()
    payload = decode_json_or_jsonp(response.text)
    rows = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"Unexpected SSE result payload for STOCK_TYPE={stock_type}")
    return rows


if not SNAPSHOT.exists():
    raise RuntimeError("Shanghai snapshot missing")

snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
sector_map: dict[str, str] = {}
raw_count = 0
for stock_type in ("1", "8"):
    rows = fetch_rows(stock_type)
    raw_count += len(rows)
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("A_STOCK_CODE") or row.get("SECURITY_CODE_A") or "").strip()
        if not re.fullmatch(r"6\d{5}", code):
            continue
        desc = str(row.get("CSRC_CODE_DESC") or row.get("CSRC_DESC") or row.get("INDUSTRY_NAME") or "").strip()
        csrc = str(row.get("CSRC_CODE") or row.get("INDUSTRY_CODE") or "").strip().upper()
        sector = desc if desc and desc.lower() not in {"nan", "none", "-"} else BROAD.get(csrc[:1])
        if sector:
            sector_map[code + ".SS"] = sector

# Some SSE payload versions expose only the broad CSRC code. If the query rows
# did not carry a description, the broad A-S mapping above still gives a stable
# official classification rather than fabricating a sector from price/turnover.
if len(sector_map) < 1800:
    raise RuntimeError(f"SSE query sector map incomplete: {len(sector_map)} classified from {raw_count} rows")

changed = 0
for row in snapshot.get("constituents", []):
    current = str(row.get("sector") or "").strip()
    if current and current.lower() != "other":
        continue
    sector = sector_map.get(str(row.get("ticker") or ""))
    if sector:
        row["sector"] = sector
        changed += 1

rows = snapshot.get("constituents", [])
covered = sum(
    bool(str(r.get("sector") or "").strip())
    and str(r.get("sector") or "").strip().lower() != "other"
    for r in rows
)
ratio = covered / len(rows) if rows else 0
if ratio < 0.95:
    raise RuntimeError(f"Shanghai sector coverage below 95%: {covered}/{len(rows)} ({ratio:.1%})")

SNAPSHOT.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
print(f"INFO shanghai SSE query sectors: {covered}/{len(rows)} covered ({changed} newly filled; {raw_count} official rows)")
