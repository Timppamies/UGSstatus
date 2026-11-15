"""AGSI+ API access for UGS Inčukalns, shared by the app and the daily fetch script.

No Streamlit imports here, so GitHub Actions can run it without Streamlit.
"""
import math
import time

import requests

URL = "https://agsi.gie.eu/api"
COUNTRY = "LV"
COMPANY_EIC = "21X000000001379R"   # AS Conexus Baltic Grid
FACILITY_EIC = "21W000000000113A"  # UGS Incukalns
HISTORY_START_YEAR = 2022          # first year shown in the year comparison
PAGE_SIZE = 300                    # AGSI maximum rows per request

FIELDS = {  # AGSI field -> column
    "gasInStorage": "stock_twh",        # TWh, end of gas day
    "full": "full_pct",                 # %
    "injection": "injection_gwh",       # GWh/d
    "withdrawal": "withdrawal_gwh",     # GWh/d
    "workingGasVolume": "wgv_twh",      # TWh, technical capacity
}
COLUMNS = ["day", "status"] + list(FIELDS.values())


def _num(value):
    """AGSI sends numbers as strings and '-' for missing values."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _request(params, key):
    """GET one page with retries. Raises RuntimeError if all attempts fail."""
    err = "unknown error"
    for attempt in range(4):
        wait = 5 * (attempt + 1)
        try:
            r = requests.get(URL, params=params, headers={"x-key": key}, timeout=60)
            if r.status_code == 200:
                js = r.json()
                if isinstance(js, dict) and isinstance(js.get("data"), list):
                    return js
                msg = js.get("message") or js.get("error") if isinstance(js, dict) else None
                err = str(msg or "unexpected response (check the API key)")
                break  # retrying will not help
            err = f"HTTP {r.status_code}"
            if r.status_code == 429:
                wait = 61  # AGSI blocks for 60 s after too many requests
            elif 400 <= r.status_code < 500:
                break  # retrying will not help
        except (requests.RequestException, ValueError) as e:
            err = type(e).__name__
        if attempt < 3:
            time.sleep(wait)
    raise RuntimeError(err)


def fetch_range(start_str, end_str, key):
    """Daily facility rows for a gas-day range. Raises on any failed page,
    so partial results are never used."""
    rows, page, last_page = [], 1, 1
    while page <= last_page:
        js = _request({
            "country": COUNTRY,
            "company": COMPANY_EIC,
            "facility": FACILITY_EIC,
            "from": start_str,
            "to": end_str,
            "page": page,
            "size": PAGE_SIZE,
        }, key)
        try:
            last_page = int(js.get("last_page") or 1)
        except (TypeError, ValueError):
            last_page = 1
        for x in js["data"]:
            day = str(x.get("gasDayStart", ""))[:10]
            if len(day) != 10:
                continue
            row = {"day": day, "status": str(x.get("status") or "")}
            for src, col in FIELDS.items():
                row[col] = _num(x.get(src))
            rows.append(row)
        page += 1
    return rows
