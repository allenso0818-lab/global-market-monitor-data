"""Daily full-universe heatmap snapshots. No representative-stock fallback."""
from __future__ import annotations
import csv, io, json, logging, re, time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import requests
import yfinance as yf

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
OUT = Path("snapshots"); OUT.mkdir(exist_ok=True)
SESSION = requests.Session(); SESSION.headers["User-Agent"] = "GlobalMarketMonitor/1.0"

@dataclass(frozen=True)
class Spec:
    name: str; yahoo_index: str; kind: str; url: str | None; minimum: int; maximum: int
    note: str

# Sources are deliberately omitted until individually verified; no small substitute is allowed.
REGISTRY = {
 "sp": Spec("S&P 500", "^GSPC", "ETF Proxy", "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/latest-holdings.csv", 450, 550, "iShares Core S&P 500 ETF (IVV)"),
 "nasdaq": Spec("Nasdaq Composite", "^IXIC", "ETF Proxy", None, 2000, 6000, "ONEQ holdings source awaiting verification; never Nasdaq-100/QQQ"),
 "nikkei": Spec("Nikkei 225", "^N225", "Official / ETF Proxy", None, 200, 250, "Official Nikkei constituent source awaiting verification"),
 "topix": Spec("TOPIX", "^TOPX", "Official / ETF Proxy", None, 1500, 2500, "JPX constituent source awaiting verification"),
 "hsi": Spec("Hang Seng Index", "^HSI", "Official / ETF Proxy", None, 70, 100, "Hang Seng Indexes constituent source awaiting verification"),
 "shanghai": Spec("Shanghai Composite", "000001.SS", "Official / ETF Proxy", None, 1200, 3000, "SSE constituent source awaiting verification"),
 "ftse": Spec("FTSE 100", "^FTSE", "ETF Proxy", "https://www.ishares.com/uk/individual/en/products/251795/ishares-core-ftse-100-ucits-etf/latest-holdings.csv", 90, 130, "iShares Core FTSE 100 UCITS ETF (ISF)"),
 "eurofirst": Spec("FTSE Eurofirst 300", "^FTEU3", "Official / ETF Proxy", None, 250, 400, "Free complete tracking holdings source awaiting verification"),
 "msci": Spec("MSCI Emerging Markets", "EEM", "ETF Proxy", "https://www.ishares.com/us/products/239637/ishares-msci-emerging-markets-etf/latest-holdings.csv", 500, 2000, "iShares MSCI Emerging Markets ETF (EEM)"),
}

def yahoo_symbol(ticker: str, location: str, spec: Spec) -> str:
    ticker = ticker.strip().replace(".", "-")
    # iShares US files use local exchange tickers.  Map those explicitly rather
    # than silently dropping non-US constituents from a full ETF universe.
    if spec.yahoo_index == "^FTSE": return f"{ticker}.L"
    suffix = {
      "Hong Kong": ".HK", "Taiwan": ".TW", "South Korea": ".KS",
      "India": ".NS", "Brazil": ".SA", "Mexico": ".MX", "South Africa": ".JO",
      "Malaysia": ".KL", "Indonesia": ".JK", "Thailand": ".BK", "Turkey": ".IS",
      "Poland": ".WA", "China": ".SS", "Saudi Arabia": ".SR",
    }.get(location)
    if suffix:
        if suffix == ".HK" and ticker.isdigit(): ticker = ticker.zfill(4)
        return ticker + suffix
    return ticker

def holdings(spec: Spec):
    if not spec.url: raise RuntimeError("full constituent source not yet validated")
    r = SESSION.get(spec.url, timeout=45); r.raise_for_status()
    lines = r.text.splitlines(); start = next(i for i,l in enumerate(lines) if l.startswith("Ticker,"))
    rows = list(csv.DictReader(io.StringIO("\n".join(lines[start:]))))
    result = []
    for row in rows:
        ticker = (row.get("Ticker") or "").strip()
        location = (row.get("Location") or row.get("Country") or "").strip()
        weight = float((row.get("Weight (%)") or "0").replace(",", "") or 0)
        if not ticker or weight <= 0 or (row.get("Asset Class") or "").lower() != "equity": continue
        result.append({"ticker": yahoo_symbol(ticker, location, spec), "name": row.get("Name") or ticker,
          "sector": row.get("Sector") or "Other", "source_weight": weight})
    return result

def enrich(rows):
    symbols = [r["ticker"] for r in rows]
    now = datetime.now(timezone.utc).isoformat()
    frames = {}
    # Small batches avoid a single huge Yahoo request being rate-limited.
    for start in range(0, len(symbols), 80):
        batch = symbols[start:start + 80]
        try:
            data = yf.download(batch, period="5d", interval="1d", group_by="ticker", threads=True, progress=False, auto_adjust=False)
            for symbol in batch: frames[symbol] = data[symbol] if len(batch) > 1 else data
        except Exception as exc: logging.warning("Yahoo batch failed: %s", exc)
        if start + 80 < len(symbols): time.sleep(1)
    for r in rows:
        try:
            df = frames[r["ticker"]]
            closes = df["Close"].dropna()
            r["price"] = round(float(closes.iloc[-1]), 6)
            r["daily_change"] = round(float(closes.iloc[-1]-closes.iloc[-2]), 6)
            r["daily_change_pct"] = round(float((closes.iloc[-1]/closes.iloc[-2]-1)*100), 6)
        except Exception: r["price"]=r["daily_change"]=r["daily_change_pct"]=None
        r["price_timestamp"] = now
    return rows

def valid(spec, rows):
    keys=set(); clean=[]
    for r in rows:
        if r["ticker"] in keys: continue
        keys.add(r["ticker"]); clean.append(r)
    total=sum(r["source_weight"] for r in clean)
    if not spec.minimum <= len(clean) <= spec.maximum: raise ValueError(f"coverage {len(clean)} outside {spec.minimum}-{spec.maximum}")
    if total <= 0: raise ValueError("non-positive total weight")
    for r in clean: r["heatmap_weight"] = r["source_weight"] / total * 100
    if sum(r["price"] is not None for r in clean) / len(clean) < .75: raise ValueError("insufficient Yahoo price coverage")
    return clean

def refresh(key, spec):
    try:
        rows=valid(spec, enrich(holdings(spec)))
        payload={"index":key,"index_name":spec.name,"yahoo_index_ticker":spec.yahoo_index,
          "weight_methodology":spec.kind,"constituent_source":spec.note,"price_source":"Yahoo Finance via yfinance",
          "holdings_updated":datetime.now(timezone.utc).isoformat(),"constituents":rows}
        (OUT/f"{key}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        logging.info("%s: wrote %s full constituents", key, len(rows))
    except Exception as exc:
        logging.warning("%s: retained previous validated snapshot (%s)", key, exc)

for key, spec in REGISTRY.items(): refresh(key, spec)
