"""Inčukalns UGS storage level: fill level, gas in storage, injection and withdrawal.

Data: GIE AGSI+ transparency platform (https://agsi.gie.eu), facility level.
Needs a free AGSI API key in Streamlit secrets:  AGSI_API_KEY = "..."
Run with:  streamlit run finbalt_ugs_storage.py
"""
import math
import time
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

st.set_page_config(page_title="Inčukalns UGS Storage", page_icon="🛢️", layout="wide")

URL = "https://agsi.gie.eu/api"
COUNTRY = "LV"
COMPANY_EIC = "21X000000001379R"   # AS Conexus Baltic Grid
FACILITY_EIC = "21W000000000113A"  # UGS Incukalns
HISTORY_START_YEAR = 2022          # first year shown in the year comparison
PAGE_SIZE = 300                    # AGSI maximum rows per request

# Cache: closed history years are kept for a month, the current year for 6 hours.
HISTORY_TTL = 30 * 24 * 3600
CURRENT_TTL = 6 * 3600
# A year counts as "history" only this many days after it has ended, so that
# late-confirmed December data is not locked in for a month.
YEAR_GRACE_DAYS = 60

FIELDS = {  # AGSI field -> column
    "gasInStorage": "stock_twh",        # TWh, end of gas day
    "full": "full_pct",                 # %
    "injection": "injection_gwh",       # GWh/d
    "withdrawal": "withdrawal_gwh",     # GWh/d
    "workingGasVolume": "wgv_twh",      # TWh, technical capacity
}


# -------------------------------------------------------------- AGSI fetch
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


def _fetch_range(start_str, end_str, key):
    """Daily facility rows for a gas-day range. Raises on any failed page,
    so partial results are never cached."""
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


# The key argument starts with an underscore so Streamlit does not hash it.
@st.cache_data(ttl=HISTORY_TTL, show_spinner=False)
def fetch_history_year(year, _key):
    """One closed calendar year: cached for a month."""
    return _fetch_range(f"{year}-01-01", f"{year}-12-31", _key)


@st.cache_data(ttl=CURRENT_TTL, show_spinner=False)
def fetch_current(start_str, end_str, _key):
    """Current period: cached for 6 hours."""
    return _fetch_range(start_str, end_str, _key)


def history_split(today):
    """First day of the 'current' period; everything before it is history."""
    year = (today - timedelta(days=YEAR_GRACE_DAYS)).year
    return date(max(year, HISTORY_START_YEAR), 1, 1)


def load_storage(split, today, key):
    rows, failed = [], []
    years = list(range(HISTORY_START_YEAR, split.year))
    steps = len(years) + 1
    bar = st.progress(0.0, text="Loading AGSI data...")
    for i, year in enumerate(years):
        bar.progress(i / steps, text=f"Loading {year} ({i + 1}/{steps})")
        try:
            rows += fetch_history_year(year, key)
        except Exception as e:
            failed.append(f"{year}: {e}")
    bar.progress(len(years) / steps, text=f"Loading current year ({steps}/{steps})")
    try:
        rows += fetch_current(split.isoformat(), today.isoformat(), key)
    except Exception as e:
        failed.append(f"{split} onwards: {e}")
    bar.empty()
    return rows, failed


def build_daily(rows):
    cols = ["day", "status"] + list(FIELDS.values())
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows, columns=cols)
    for c in FIELDS.values():
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.drop_duplicates(subset="day", keep="last").sort_values("day")
    df["date"] = pd.to_datetime(df["day"])
    # Fill level: use AGSI's value, calculate it where only stock and capacity exist.
    calc = df["stock_twh"] / df["wgv_twh"] * 100
    df["full_pct"] = df["full_pct"].fillna(calc.where(df["wgv_twh"] > 0))
    return df.set_index("date")


def monthly_table(daily):
    d = daily.dropna(subset=["stock_twh"])
    g = d.groupby(d.index.strftime("%Y-%m"))
    out = pd.DataFrame({
        "Gas in storage, month end (TWh)": g["stock_twh"].last(),
        "Fill level, month end (%)": g["full_pct"].last(),
        "Injection (TWh)": g["injection_gwh"].sum() / 1000,
        "Withdrawal (TWh)": g["withdrawal_gwh"].sum() / 1000,
        "Working gas volume (TWh)": g["wgv_twh"].last(),
    })
    out.index.name = "Month"
    return out


# ---------------------------------------------------------------------- UI
st.title("🛢️ Inčukalns UGS Storage Level")
st.markdown(
    "Fill level and gas in storage at the **Inčukalns underground gas storage** "
    "(Conexus Baltic Grid, Latvia), with daily injection and withdrawal. "
    "Source: GIE AGSI+."
)

try:
    api_key = str(st.secrets.get("AGSI_API_KEY", "")).strip()
except Exception:
    api_key = ""
if not api_key:
    st.error("AGSI API key is missing.")
    st.markdown(
        "Add your key to the app's secrets and reload:\n\n"
        "```toml\nAGSI_API_KEY = \"your-key-here\"\n```\n\n"
        "- **Streamlit Cloud:** app menu → Settings → Secrets\n"
        "- **Locally:** file `.streamlit/secrets.toml` next to the app "
        "(do not commit it to GitHub)\n\n"
        "A free key is available after registering at https://agsi.gie.eu."
    )
    st.stop()

st.sidebar.header("Settings")
metric = st.sidebar.radio("Year comparison shows:",
                          ["Fill level (%)", "Gas in storage (TWh)"])
months_to_show = st.sidebar.slider("Flow chart and table (months):", 3, 24, 12, 1)
if st.sidebar.button("Refresh current year 🔄"):
    fetch_current.clear()
    st.rerun()
if st.sidebar.button("Clear all cache (incl. history)"):
    st.cache_data.clear()
    st.rerun()
st.sidebar.caption(
    "History years are cached for 30 days, the current year for 6 hours. "
    "AGSI publishes new data once a day."
)

today = date.today()
split = history_split(today)
rows, failed = load_storage(split, today, api_key)

if failed:
    st.warning(
        "Some AGSI data could not be loaded, so the figures below may be "
        "incomplete. Press the refresh button to retry (the rest is cached)."
    )
    with st.expander("Failed requests"):
        for f in failed:
            st.write(f)

daily = build_daily(rows)
valid = daily.dropna(subset=["stock_twh"])
if valid.empty:
    st.error("No storage data retrieved from AGSI.")
    st.stop()

# --- KPI cards
last = valid.iloc[-1]
last_date = valid.index[-1]
prev_year_date = last_date - pd.DateOffset(years=1)
prev = valid[valid.index <= prev_year_date].tail(1)
if not prev.empty and (prev_year_date - prev.index[-1]).days > 7:
    prev = prev.iloc[0:0]  # nearest value is too far away to compare


def fmt(value, pattern, na="n/a"):
    return pattern.format(value) if pd.notna(value) else na


status_text = {"E": "estimated", "C": "confirmed"}.get(last["status"], "")
st.subheader(f"Latest gas day: {last_date.date()}"
             + (f" ({status_text})" if status_text else ""))
k = st.columns(5)
delta_full = delta_stock = None
if not prev.empty:
    p = prev.iloc[-1]
    if pd.notna(last["full_pct"]) and pd.notna(p["full_pct"]):
        delta_full = f"{last['full_pct'] - p['full_pct']:+.1f} pp vs year ago"
    delta_stock = f"{last['stock_twh'] - p['stock_twh']:+.1f} TWh vs year ago"
k[0].metric("Fill level", fmt(last["full_pct"], "{:.1f} %"), delta_full)
k[1].metric("Gas in storage", fmt(last["stock_twh"], "{:.1f} TWh"), delta_stock)
k[2].metric("Working gas volume", fmt(last["wgv_twh"], "{:.1f} TWh"))
k[3].metric("Injection", fmt(last["injection_gwh"], "{:.1f} GWh/d"))
k[4].metric("Withdrawal", fmt(last["withdrawal_gwh"], "{:.1f} GWh/d"))

st.markdown("---")

# --- Year comparison: one line per year on a January-December axis
col = "full_pct" if metric.startswith("Fill") else "stock_twh"
unit = "%" if col == "full_pct" else "TWh"
st.subheader(f"{metric} by year")
years = sorted(valid.index.year.unique())
GREYS = ["#c7c7c7", "#a6a6a6", "#858585", "#636363", "#424242"]
fig = go.Figure()
for i, yr in enumerate(years):
    part = valid[valid.index.year == yr].dropna(subset=[col])
    if part.empty:
        continue
    # Same dummy leap year for every line, so the x-axis is the day of the year.
    x = pd.to_datetime("2000-" + part.index.strftime("%m-%d"))
    is_current = yr == years[-1]
    older = len(years) - 1 - i
    fig.add_trace(go.Scatter(
        x=x, y=part[col], name=str(yr), mode="lines",
        # Current year in blue; earlier years in grey, the most recent darkest.
        line=dict(color="#1f77b4" if is_current
                  else GREYS[max(0, len(GREYS) - older)],
                  width=3.5 if is_current else 1.8),
        customdata=part.index.strftime("%Y-%m-%d"),
        hovertemplate="%{customdata}: %{y:.1f} " + unit + "<extra>" + str(yr) + "</extra>",
    ))
fig.update_layout(template="plotly_white", height=520, legend_title_text="Year",
                  hovermode="closest", legend=dict(orientation="h", y=1.08, x=0))
fig.update_xaxes(tickformat="%b", dtick="M1",
                 range=["2000-01-01", "2000-12-31"])
fig.update_yaxes(title_text=metric, tickformat=".0f", rangemode="tozero")
if col == "full_pct":
    fig.update_yaxes(range=[0, max(100, math.ceil(valid[col].max() / 10) * 10)])
st.plotly_chart(fig, use_container_width=True)

wgv = valid["wgv_twh"].dropna()
if col == "full_pct" and not wgv.empty and wgv.max() - wgv.min() > 0.05:
    st.caption(
        f"Note: the working gas volume has changed over the period "
        f"({wgv.min():.1f}-{wgv.max():.1f} TWh), so the same fill level in two "
        "years does not mean the same amount of gas. Switch to TWh in the "
        "sidebar to compare volumes."
    )

# --- Daily injection and withdrawal
st.subheader("Daily injection and withdrawal (GWh/d)")
flow_start = (last_date.to_period("M") - (months_to_show - 1)).start_time
flows = daily[daily.index >= flow_start]
fig_f = go.Figure()
fig_f.add_trace(go.Bar(x=flows.index, y=flows["injection_gwh"], name="Injection",
                       marker_color="#2ca02c", hovertemplate="%{y:.1f} GWh/d"))
fig_f.add_trace(go.Bar(x=flows.index, y=-flows["withdrawal_gwh"], name="Withdrawal",
                       marker_color="#d62728", customdata=flows["withdrawal_gwh"],
                       hovertemplate="%{customdata:.1f} GWh/d"))
fig_f.update_layout(barmode="relative", bargap=0, template="plotly_white", height=420,
                    hovermode="x unified", legend_title_text="",
                    legend=dict(orientation="h", y=1.1, x=0))
fig_f.update_yaxes(title_text="GWh/d (withdrawal shown below zero)", tickformat=".0f")
st.plotly_chart(fig_f, use_container_width=True)

# --- Monthly table and download
st.subheader("Monthly table")
table = monthly_table(daily).tail(months_to_show)
st.dataframe(table.style.format("{:.1f}", na_rep="n/a"), use_container_width=True)
latest_month = table.index[-1]
if last_date.date() < pd.Period(latest_month, freq="M").end_time.date():
    st.caption(f"{latest_month} is a partial month: data through {last_date.date()}.")
st.download_button(
    "Download monthly CSV 📥",
    table.round(2).to_csv().encode("utf-8"),
    file_name=f"incukalns_ugs_monthly_{latest_month}.csv",
    mime="text/csv",
)
st.download_button(
    "Download daily CSV 📥",
    daily.drop(columns=["day"]).rename_axis("gas_day").round(3).to_csv().encode("utf-8"),
    file_name=f"incukalns_ugs_daily_{last_date.date()}.csv",
    mime="text/csv",
)

# --- Method and data quality
with st.expander("Method and caveats"):
    st.markdown(
        f"""
**Source:** GIE AGSI+ (Aggregated Gas Storage Inventory), facility *UGS Incukalns*
(`{FACILITY_EIC}`), operator AS Conexus Baltic Grid (`{COMPANY_EIC}`), daily data.

- **Gas in storage:** TWh at the end of the gas day, as reported to AGSI.
- **Fill level:** gas in storage divided by the working gas volume (technical
  capacity), as published by AGSI.
- **Working gas volume** is set for each storage cycle and changes from year to
  year, so fill levels of different years are not measured against the same capacity.
- **Injection and withdrawal:** GWh per gas day. The monthly table sums them to TWh.
- The latest days are often *estimated* and are confirmed later, so recent
  values can still change.
- These figures come from AGSI, not ENTSOG, so the injection and withdrawal here
  can differ slightly from the flows in the FinBalt demand and supply apps.
- History years are cached for 30 days, so a revision to old data shows up only
  after that (or after "Clear all cache").
        """
    )

with st.expander("Data quality"):
    st.write(f"Cached as history (30 days): before {split}. "
             f"Current (6 hours): {split} onwards.")
    q = daily.assign(Year=daily.index.year)
    names = {"C": "Confirmed", "E": "Estimated", "N": "No data"}
    quality = (q.assign(Status=q["status"].map(names).fillna("Other"))
               .groupby(["Year", "Status"]).size().unstack("Status").fillna(0).astype(int))
    quality["Days with stock value"] = q.groupby("Year")["stock_twh"].count()
    st.markdown("**Days per year by AGSI status**")
    st.dataframe(quality, use_container_width=True)
