from pathlib import Path

path = Path("scripts/refresh_heatmaps.py")
text = path.read_text()

def replace_once(old: str, new: str, label: str):
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected 1 match, found {count}")
    text = text.replace(old, new, 1)

replace_once(
'''    "stoxx": Spec(
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
    ),''',
'''    "stoxx": Spec(
        "STOXX Europe 600", "^STOXX", "ishares_csv",
        "https://www.ishares.com/de/privatanleger/de/produkte/251931/ishares-stoxx-europe-600-ucits-etf-de-fund/1478358465952.ajax?fileType=csv&fileName=EXSA_holdings&dataType=fund",
        550, 650, "iShares STOXX Europe 600 UCITS ETF (EXSA)", "Full tracking-ETF holdings proxy", 0.65,
    ),
    "nikkei": Spec(
        "Nikkei 225", "^N225", "nikkei_official",
        "https://indexes.nikkei.co.jp/en/nkave/index/component?idx=nk225",
        225, 225, "Nikkei Indexes official Nikkei 225 component list",
        "Official full constituent list; equal-area tile sizing", 0.90,
    ),
    "topix": Spec(
        "TOPIX", "^TOPX", "ishares_csv",
        "https://www.blackrock.com/jp/individual-en/en/products/279438/fund/1480664184455.ajax?fileType=csv&fileName=1475_holdings&dataType=fund",
        1500, 1800, "iShares Core TOPIX ETF (1475)",
        "Full tracking-ETF holdings proxy", 0.80,
    ),
    "hsi": Spec(
        "Hang Seng Index", "^HSI", "hsi_official_etf",
        "https://rbwm-api.hsbc.com.hk/pws-hk-hase-hsvm2-papi-prod-proxy/v1/hsvm/csv/trahkfund/holdings?mode=daily",
        80, 120, "Tracker Fund of Hong Kong (2800) official daily holdings",
        "Full tracking-ETF holdings proxy", 0.90,
    ),''',
"registry sources",
)

replace_once(
'''        "Portugal": ".LS",
    }.get(location)''',
'''        "Portugal": ".LS", "Japan": ".T",
    }.get(location)''',
"Japan Yahoo suffix",
)

insert_at = '''\ndef holdings(spec: Spec):\n'''
functions = r'''

def nikkei_official_holdings(spec: Spec):
    response = SESSION.get(spec.url, timeout=45)
    response.raise_for_status()
    frames = pd.read_html(io.StringIO(response.text))
    result = []
    for frame in frames:
        columns = [str(c).strip() for c in frame.columns]
        if "Code" not in columns or "Company Name" not in columns:
            continue
        frame.columns = columns
        for _, row in frame.iterrows():
            code = str(row.get("Code") or "").strip()
            name = str(row.get("Company Name") or code).strip()
            if not re.fullmatch(r"[0-9A-Z]{4}", code):
                continue
            result.append({
                "ticker": f"{code}.T",
                "name": name,
                "sector": "Other",
                "source_weight": 1.0,
            })
    dedup = {row["ticker"]: row for row in result}
    return list(dedup.values())


def hsi_official_etf_holdings(spec: Spec):
    response = SESSION.get(spec.url, timeout=45)
    response.raise_for_status()
    text_csv = response.content.decode("utf-8-sig", errors="replace")
    lines = text_csv.splitlines()
    start = next(
        i for i, line in enumerate(lines)
        if "Stock Code" in line and ("Weight" in line or "Weighting" in line)
    )
    result = []
    for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        code = (row.get("Stock Code") or row.get("StockCode") or row.get("Code") or "").strip()
        code = re.sub(r"\D", "", code)
        if not code:
            continue
        code = code.zfill(4)
        weight = number(row.get("Weighting") or row.get("Weight (%)") or row.get("Weight"))
        result.append({
            "ticker": f"{code}.HK",
            "name": (row.get("Stock Name") or row.get("Name") or code).strip(),
            "sector": (row.get("Sector") or "Other").strip(),
            "source_weight": weight if weight > 0 else 1.0,
        })
    return result
'''
replace_once(insert_at, functions + insert_at, "official parser insertion")

replace_once(
'''    if spec.source == "moneydj":
        return moneydj_holdings(spec)
    if spec.source == "nasdaq_exchange":''',
'''    if spec.source == "moneydj":
        return moneydj_holdings(spec)
    if spec.source == "nikkei_official":
        return nikkei_official_holdings(spec)
    if spec.source == "hsi_official_etf":
        return hsi_official_etf_holdings(spec)
    if spec.source == "nasdaq_exchange":''',
"official parser dispatch",
)

path.write_text(text)
print("patched heatmap source pipeline")
