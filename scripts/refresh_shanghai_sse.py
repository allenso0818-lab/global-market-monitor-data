"""Build the Shanghai heat-map snapshot from the SSE official quote universe.

The Shanghai Composite is broad and subject to index eligibility rules.  The
free SSE quote feed exposes the full Shanghai-listed equity board, which is a
transparent broad-universe proxy and is preferable to a tiny representative
basket.  Rows without a usable quote remain present and render grey.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

OUT = Path("snapshots")
OUT.mkdir(exist_ok=True)
NOW = datetime.now(timezone.utc).isoformat()

URLS = [
    "https://yunhq.sse.com.cn:32042/v1/sh1/list/exchange/equity",
    "http://yunhq.sse.com.cn:32041/v1/sh1/list/exchange/equity",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
    "Referer": "https://www.sse.com.cn/market/price/report/",
    "Accept": "application/json,text/plain,*/*",
}
PARAMS = {
    "select": "code,name,last,prev_close,chg_rate,change,amount",
    "begin": "0",
    "end": "5000",
}

last_error = None
payload = None
for url in URLS:
    try:
        r = requests.get(url, params=PARAMS, headers=HEADERS, timeout=60)
        r.raise_for_status()
        payload = r.json()
        if isinstance(payload.get("list"), list) and len(payload["list"]) > 1800:
            break
        raise RuntimeError(f"unexpected SSE row count: {len(payload.get('list') or [])}")
    except Exception as exc:
        last_error = exc
        payload = None

if payload is None:
    raise RuntimeError(f"SSE official equity feed unavailable: {last_error}")

rows = []
for item in payload["list"]:
    if not isinstance(item, list) or len(item) < 7:
        continue
    code, name, last, prev_close, pct, change, amount = item[:7]
    code = str(code).strip()
    if not code or not code.isdigit() or len(code) != 6:
        continue
    try:
        price = float(last) if last not in (None, "", "-") else None
    except Exception:
        price = None
    try:
        previous = float(prev_close) if prev_close not in (None, "", "-") else None
    except Exception:
        previous = None
    try:
        pct_value = float(pct) if pct not in (None, "", "-") else None
    except Exception:
        pct_value = None
    try:
        change_value = float(change) if change not in (None, "", "-") else (price - previous if price is not None and previous is not None else None)
    except Exception:
        change_value = price - previous if price is not None and previous is not None else None

    # Equal area is intentional here: the public SSE board feed does not expose
    # free-float index weights.  Never fabricate an index weight from turnover.
    rows.append({
        "ticker": f"{code}.SS",
        "name": str(name).strip() or code,
        "sector": "Other",
        "source_weight": 1.0,
        "heatmap_weight": 0.0,  # normalized below
        "price": price,
        "daily_change": change_value,
        "daily_change_pct": pct_value,
        "price_timestamp": NOW,
    })

# Deduplicate while preserving the official feed order.
seen = set()
clean = []
for row in rows:
    if row["ticker"] in seen:
        continue
    seen.add(row["ticker"])
    clean.append(row)

if not 1800 <= len(clean) <= 3500:
    raise RuntimeError(f"SSE official universe count outside guardrail: {len(clean)}")

weight = 100.0 / len(clean)
for row in clean:
    row["heatmap_weight"] = weight

snapshot = {
    "index": "shanghai",
    "index_name": "Shanghai Composite",
    "weight_methodology": "Official SSE listed-equity universe proxy; equal-area tiles (official free-float index weights unavailable in the free quote feed)",
    "constituent_source": "Shanghai Stock Exchange official public quote system (yunhq) — full SSE equity board",
    "price_source": "Shanghai Stock Exchange official public quote system (yunhq)",
    "holdings_updated": NOW,
    "coverage_count": len(clean),
    "constituents": clean,
}
(OUT / "shanghai.json").write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
print(f"INFO shanghai: wrote {len(clean)} official SSE equity rows")
