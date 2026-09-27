"""Fill Shanghai heatmap sectors from official SSE industry stock lists.

Uses broad CSRC industry groups (A-S) from SSE detail pages. This is a
server-rendered fallback when the fine-grained industry table/API is unavailable.
It only fills missing/Other sectors and preserves the universe, weights and quotes.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pandas as pd
import requests

SNAPSHOT = Path("snapshots/shanghai.json")
S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
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

if not SNAPSHOT.exists():
    raise RuntimeError("Shanghai snapshot missing")

snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
sector_map: dict[str, str] = {}

for code, sector in BROAD.items():
    found_any = False
    for host in ("https://star.sse.com.cn", "https://www.sse.com.cn"):
        url = f"{host}/assortment/stock/areatrade/trade/detail.shtml?csrcCode={code}"
        try:
            response = S.get(url, timeout=45)
            response.raise_for_status()
            for df in pd.read_html(io.StringIO(response.text)):
                cols = {str(c).strip(): c for c in df.columns}
                stock_col = next((c for k, c in cols.items() if "A股代码" in k or "上市公司代码" in k), None)
                if stock_col is None:
                    continue
                for value in df[stock_col].tolist():
                    stock = re.sub(r"\D", "", str(value))
                    if re.fullmatch(r"6\d{5}", stock):
                        sector_map[stock + ".SS"] = sector
                        found_any = True
            if found_any:
                break
        except Exception:
            continue

if len(sector_map) < 1800:
    raise RuntimeError(f"SSE broad-industry map incomplete: {len(sector_map)}")

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
covered = sum(bool(str(r.get("sector") or "").strip()) and str(r.get("sector") or "").strip().lower() != "other" for r in rows)
if not rows or covered / len(rows) < 0.95:
    raise RuntimeError(f"Shanghai sector coverage below 95%: {covered}/{len(rows)}")

SNAPSHOT.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
print(f"INFO shanghai broad sectors: {covered}/{len(rows)} covered ({changed} newly filled)")
