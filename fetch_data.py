"""Fetch all Inčukalns UGS data from AGSI+ and store it in data/incukalns_daily.csv.

Run daily by GitHub Actions (.github/workflows/update-data.yml), so the app can
read the data from the repository instead of waiting for the API.
The whole history is fetched every time, so AGSI's revisions are picked up too.

Usage:  AGSI_API_KEY=... python fetch_data.py
"""
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from agsi import COLUMNS, HISTORY_START_YEAR, fetch_range

OUT = Path(__file__).parent / "data" / "incukalns_daily.csv"
# Refuse to overwrite if the new file is much shorter than the old one
# (a sign of an incomplete API response).
MIN_SHARE_OF_OLD = 0.95


def main():
    key = os.environ.get("AGSI_API_KEY", "").strip()
    if not key:
        sys.exit("AGSI_API_KEY is not set.")

    today = date.today()
    rows = []
    for year in range(HISTORY_START_YEAR, today.year + 1):
        end = min(date(year, 12, 31), today)
        part = fetch_range(f"{year}-01-01", end.isoformat(), key)
        print(f"{year}: {len(part)} days")
        rows += part

    df = pd.DataFrame(rows, columns=COLUMNS)
    df = df.drop_duplicates(subset="day", keep="last").sort_values("day")
    if df.empty:
        sys.exit("AGSI returned no data; keeping the old file.")

    if OUT.exists():
        old = pd.read_csv(OUT)
        if len(df) < MIN_SHARE_OF_OLD * len(old):
            sys.exit(f"Only {len(df)} days fetched vs {len(old)} stored; "
                     "keeping the old file.")

    OUT.parent.mkdir(exist_ok=True)
    df.to_csv(OUT, index=False, float_format="%.4f")
    print(f"Wrote {len(df)} days ({df['day'].iloc[0]} - {df['day'].iloc[-1]}) to {OUT}")


if __name__ == "__main__":
    main()
