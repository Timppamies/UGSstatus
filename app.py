"""Inčukalns UGS storage level: fill level, gas in storage, injection and withdrawal.

Data: GIE AGSI+ transparency platform (https://agsi.gie.eu), facility level.
GitHub Actions fetches the data every evening into data/incukalns_daily.csv
(fetch_data.py), so the app normally reads it from the repository. The app
calls the API itself only if that file is missing or more than a few days old,
or when "Fetch latest from AGSI" is pressed; that needs a free AGSI API key in
Streamlit secrets:  AGSI_API_KEY = "..."
Run with:  streamlit run app.py
"""
import math
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from agsi import COMPANY_EIC, FACILITY_EIC, FIELDS, HISTORY_START_YEAR, fetch_range

st.set_page_config(page_title="Inčukalns UGS Storage", page_icon="🛢️", layout="wide")

DATA_FILE = Path(__file__).parent / "data" / "incukalns_daily.csv"
# AGSI publishes a gas day on the following day, so the stored file is normally
# 1-2 days behind. Older than this, the app tops it up from the API.
STALE_DAYS = 3
# A live top-up also re-fetches this many days before the stored end, because
# AGSI revises recent estimated days.
TOPUP_OVERLAP_DAYS = 14
LIVE_TTL = 6 * 3600  # cache for live API calls

# Latvian energy supply security reserve held in Inčukalns. It is not part of
# the stock that AGSI reports, so the app adds it on top. Each entry is
# (first gas day it applies from, TWh); add a line if the reserve changes.
SECURITY_RESERVE = [
    ("2022-01-01", 1.8),
]

# ------------------------------------------------------------- data loading
@st.cache_data(show_spinner=False)
def read_stored(_path, mtime):
    """Rows from the stored CSV; the file's modification time is the cache key,
    so a new file from GitHub Actions is read again automatically."""
    df = pd.read_csv(_path, dtype={"day": str, "status": str})
    df["status"] = df["status"].fillna("")
    return df.sort_values("day").to_dict("records")


# The key argument starts with an underscore so Streamlit does not hash it.
@st.cache_data(ttl=LIVE_TTL, show_spinner=False)
def fetch_live(start_str, end_str, _key):
    """Live AGSI call, cached for 6 hours."""
    return fetch_range(start_str, end_str, _key)


def load_storage(today, key, force_live=False):
    """Stored rows, topped up from the API when needed.
    Returns (rows, failed, stored_last_day, live_from)."""
    rows, failed, stored_last, live_from = [], [], None, None
    if DATA_FILE.exists():
        try:
            rows = read_stored(DATA_FILE, DATA_FILE.stat().st_mtime)
        except Exception as e:
            failed.append(f"Stored file: {e}")
        if rows:
            stored_last = date.fromisoformat(rows[-1]["day"])
    stale = stored_last is None or (today - stored_last).days > STALE_DAYS
    if (stale or force_live) and key:
        live_from = (date(HISTORY_START_YEAR, 1, 1) if stored_last is None
                     else stored_last - timedelta(days=TOPUP_OVERLAP_DAYS))
        with st.spinner("Loading latest data from AGSI..."):
            try:
                # Live rows come last, so they replace stored rows of the same day.
                rows = rows + fetch_live(live_from.isoformat(), today.isoformat(), key)
            except Exception as e:
                failed.append(f"AGSI API from {live_from}: {e}")
                live_from = None
    elif stale:
        failed.append("The stored data is out of date and no AGSI API key is set, "
                      "so newer days cannot be fetched.")
    return rows, failed, stored_last, live_from


def reserve_on(days):
    """Security reserve (TWh) that applies on each gas day (ISO date strings)."""
    out = pd.Series(0.0, index=days.index)
    for start, twh in sorted(SECURITY_RESERVE):
        out[days >= start] = twh
    return out


def build_daily(rows, include_reserve=True):
    cols = ["day", "status"] + list(FIELDS.values())
    if not rows:
        return pd.DataFrame(columns=cols + ["agsi_stock_twh", "agsi_full_pct",
                                            "reserve_twh"])
    df = pd.DataFrame(rows, columns=cols)
    for c in FIELDS.values():
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.drop_duplicates(subset="day", keep="last").sort_values("day")
    df["date"] = pd.to_datetime(df["day"])
    # Fill level: use AGSI's value, calculate it where only stock and capacity exist.
    calc = df["stock_twh"] / df["wgv_twh"] * 100
    df["full_pct"] = df["full_pct"].fillna(calc.where(df["wgv_twh"] > 0))
    # Keep the figures as AGSI reports them, then add the security reserve.
    df["agsi_stock_twh"] = df["stock_twh"]
    df["agsi_full_pct"] = df["full_pct"]
    df["reserve_twh"] = reserve_on(df["day"]) if include_reserve else 0.0
    if include_reserve:
        df["stock_twh"] = df["agsi_stock_twh"] + df["reserve_twh"]
        with_reserve = df["stock_twh"] / df["wgv_twh"] * 100
        df["full_pct"] = with_reserve.where(df["wgv_twh"] > 0, df["agsi_full_pct"])
    return df.set_index("date")


def monthly_table(daily):
    d = daily.dropna(subset=["stock_twh"])
    g = d.groupby(d.index.strftime("%Y-%m"))
    out = pd.DataFrame({
        "Gas in storage, month end (TWh)": g["stock_twh"].last(),
        "Fill level, month end (%)": g["full_pct"].last(),
        "of which security reserve (TWh)": g["reserve_twh"].last(),
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
if not api_key and not DATA_FILE.exists():
    st.error("No stored data yet and the AGSI API key is missing.")
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
include_reserve = st.sidebar.checkbox(
    "Include Latvian security reserve", value=True,
    help="Adds the energy supply security reserve to the stock reported by "
         "AGSI. Untick to see the AGSI figures as published.")
if api_key and st.sidebar.button("Fetch latest from AGSI 🔄"):
    fetch_live.clear()
    # Kept for the session, so the live data stays when other settings change.
    st.session_state["force_live"] = True
force_live = st.session_state.get("force_live", False)
if st.sidebar.button("Clear cache"):
    st.cache_data.clear()
    st.rerun()
st.sidebar.caption(
    "Data is fetched from AGSI into the repository every evening, so the app "
    "opens without waiting for the API. AGSI publishes each gas day on the "
    "following day."
)

today = date.today()
rows, failed, stored_last, live_from = load_storage(today, api_key, force_live)

if failed:
    st.warning(
        "Some AGSI data could not be loaded, so the figures below may be "
        "incomplete or out of date."
    )
    with st.expander("Failed requests"):
        for f in failed:
            st.write(f)

daily = build_daily(rows, include_reserve)
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
if include_reserve and last["reserve_twh"] > 0:
    st.caption(
        f"Includes the Latvian energy supply security reserve of "
        f"{last['reserve_twh']:.1f} TWh, which AGSI does not report. "
        f"AGSI figures without it: {fmt(last['agsi_stock_twh'], '{:.1f} TWh')}, "
        f"{fmt(last['agsi_full_pct'], '{:.1f} %')}."
    )
elif not include_reserve:
    st.caption("AGSI figures as published: the Latvian energy supply security "
               "reserve is not included.")

st.markdown("---")

# --- Year comparison: one line per year (gas year April-March for TWh)
col = "full_pct" if metric.startswith("Fill") else "stock_twh"
unit = "%" if col == "full_pct" else "TWh"
st.subheader(f"{metric} by year")
# Gas in storage (TWh) follows the storage cycle: April-March gas years
# (injection season first, then withdrawal). Fill level keeps January-December.
gas_year = col == "stock_twh"
if gas_year:
    # A gas year starts on 1 April: January-March belong to the previous one.
    year_of = pd.Series(valid.index.year - (valid.index.month < 4), index=valid.index)
else:
    year_of = pd.Series(valid.index.year, index=valid.index)
years = sorted(year_of.unique())
# One fixed colour per year, so a year keeps its colour in both views and when
# a new year is added (validated for colour-blind separation). Red is left out
# because the security reserve line is red.
YEAR_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
               "#e87ba4", "#008300", "#6250d6"]
LABEL_COLOR = "#444444"
x_end = pd.Timestamp("2000-03-31" if gas_year else "2000-12-31")
end_labels = []  # (x, y, name) at the end of each line
fig = go.Figure()
for i, yr in enumerate(years):
    part = valid[year_of == yr].dropna(subset=[col])
    if part.empty:
        continue
    if gas_year:
        # April-December on dummy year 1999, January-March on leap year 2000,
        # so the x-axis runs from April to March and 29 February fits.
        dummy = pd.Index(["2000-" if m < 4 else "1999-" for m in part.index.month])
        x = pd.to_datetime(dummy + part.index.strftime("%m-%d"))
        name = f"{yr}/{str(yr + 1)[-2:]}"
    else:
        # Same dummy leap year for every line, so the x-axis is the day of the year.
        x = pd.to_datetime("2000-" + part.index.strftime("%m-%d"))
        name = str(yr)
    is_current = yr == years[-1]
    fig.add_trace(go.Scatter(
        x=x, y=part[col], name=name, mode="lines",
        # Current year thicker; every year has its own colour.
        line=dict(color=YEAR_COLORS[(int(yr) - 2021) % len(YEAR_COLORS)],
                  width=3.5 if is_current else 2),
        customdata=part.index.strftime("%Y-%m-%d"),
        hovertemplate="%{customdata}: %{y:.1f} " + unit + "<extra>" + name + "</extra>",
    ))
    end_labels.append([x[-1], float(part[col].iloc[-1]), name])

# Year labels at the line ends, so the years can be told apart without the
# legend. Labels that end at the same place are pushed apart vertically.
y_span = max(valid[col].max(), 1)
min_gap = y_span * 0.045
for group in (
        [l for l in end_labels if (x_end - l[0]).days <= 7],
        [l for l in end_labels if (x_end - l[0]).days > 7]):
    group.sort(key=lambda l: l[1])
    for a, b in zip(group, group[1:]):
        if b[1] - a[1] < min_gap and abs((a[0] - b[0]).days) <= 20:
            b[1] = a[1] + min_gap
for lx, ly, name in end_labels:
    fig.add_annotation(x=lx, y=ly, text=name, showarrow=False, xanchor="left",
                       xshift=6, font=dict(size=11, color=LABEL_COLOR))
if include_reserve and last["reserve_twh"] > 0:
    # Reference line: the part of the stock that is the security reserve.
    level = last["reserve_twh"]
    if col == "full_pct":
        level = level / last["wgv_twh"] * 100 if last["wgv_twh"] > 0 else None
    if level is not None and pd.notna(level):
        fig.add_hline(y=level, line_dash="dot", line_color="#d62728", line_width=1.5,
                      annotation_text=f"Security reserve ({level:.1f} {unit})",
                      annotation_position="bottom right",
                      annotation_font_color="#d62728")
fig.update_layout(template="plotly_white", height=520,
                  legend_title_text="Gas year" if gas_year else "Year",
                  hovermode="closest", legend=dict(orientation="h", y=1.08, x=0))
fig.update_xaxes(tickformat="%b", dtick="M1",
                 range=["1999-04-01", "2000-03-31"] if gas_year
                 else ["2000-01-01", "2000-12-31"])
# Room on the right for the year labels at the line ends.
fig.update_layout(margin=dict(r=70))
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
if not include_reserve:
    table = table.drop(columns=["of which security reserve (TWh)"])
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

- **Gas in storage:** TWh at the end of the gas day, as reported to AGSI, plus
  the Latvian energy supply security reserve (see below).
- **Fill level:** gas in storage divided by the working gas volume (technical
  capacity) published by AGSI.
- **Security reserve:** the Latvian energy supply security reserve
  ({SECURITY_RESERVE[-1][1]:.1f} TWh) is held in Inčukalns but is not part of the
  stock in AGSI. The app adds it as a fixed amount on every day from
  {SECURITY_RESERVE[0][0]}; the true amount and start date may differ, and reserves
  that other countries hold in the storage are not added. Untick "Include Latvian
  security reserve" in the sidebar to see the AGSI figures as published.
- **Working gas volume** is set for each storage cycle and changes from year to
  year, so fill levels of different years are not measured against the same capacity.
- **Injection and withdrawal:** GWh per gas day. The monthly table sums them to TWh.
- The latest days are often *estimated* and are confirmed later, so recent
  values can still change.
- These figures come from AGSI, not ENTSOG, so the injection and withdrawal here
  can differ slightly from the flows in the FinBalt demand and supply apps.
- The full history is re-fetched every evening, so AGSI's revisions to old days
  show up the next day.
        """
    )

with st.expander("Data quality"):
    src = (f"Stored file (updated every evening): through {stored_last}."
           if stored_last else "No stored file.")
    if live_from:
        src += f" Fetched live from AGSI: {live_from} onwards."
    st.write(src)
    q = daily.assign(Year=daily.index.year)
    names = {"C": "Confirmed", "E": "Estimated", "N": "No data"}
    quality = (q.assign(Status=q["status"].map(names).fillna("Other"))
               .groupby(["Year", "Status"]).size().unstack("Status").fillna(0).astype(int))
    quality["Days with stock value"] = q.groupby("Year")["stock_twh"].count()
    st.markdown("**Days per year by AGSI status**")
    st.dataframe(quality, use_container_width=True)
