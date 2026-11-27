"""Download public GB electricity price data used in the cost analysis.

Sources (both public, no key needed):
  * Elexon Insights MID (Market Index Data): half-hourly wholesale reference price
    (GBP/MWh) and traded volume, provider APXMIDP (EPEX Spot short-term market).
    https://data.elexon.co.uk/bmrs/api/v1/datasets/MID
  * Octopus Energy public product API: Agile Octopus half-hourly retail unit rates
    (p/kWh incl. VAT, excluding the daily standing charge), London region (C).
    https://api.octopus.energy/v1/products/

Run:  python src/fetch_prices.py   (writes data/raw/mid.parquet, data/raw/agile_*.parquet)
      python src/fetch_prices.py --start 2026-01-01 --end 2026-11-01 --suffix _2026
          (out-of-period check: MID and London Agile only, see check_2026.py)
"""
from __future__ import annotations

import argparse
import datetime as dt
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
START = dt.datetime(2018, 12, 31, tzinfo=dt.timezone.utc)
END = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)
MID_URL = "https://data.elexon.co.uk/bmrs/api/v1/datasets/MID"
OCTO = "https://api.octopus.energy/v1/products"

# Agile import products for domestic customers, in the order they were open to new
# customers (from the API's available_from field). Business ("BB") variants excluded.
# Each product's rates are used from its launch until the next product launched; the
# autumn-2022 pause in new sign-ups is covered by the August 2022 product.
AGILE_PRODUCTS = [
    ("AGILE-18-02-21", "2018-02-21T00:00:00Z"),
    ("AGILE-22-07-22", "2022-07-22T12:00:00Z"),
    ("AGILE-22-08-31", "2022-08-29T15:00:00Z"),
    ("AGILE-FLEX-22-11-25", "2022-11-25T00:00:00Z"),
    ("AGILE-23-12-06", "2023-12-11T12:00:00Z"),
    ("AGILE-24-04-03", "2024-04-02T23:00:00Z"),
    ("AGILE-24-10-01", "2024-09-30T23:00:00Z"),
]
REGIONS = list("ABCDEFGHJKLMNP")  # the 14 GB distribution regions


def _get(url: str, params: dict | None = None, tries: int = 6) -> dict:
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=90)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(2 ** i)
    raise RuntimeError(f"failed: {url} {params}")


def fetch_mid(start: dt.datetime = START, end: dt.datetime = END) -> pd.DataFrame:
    spans, t = [], start
    while t < end:
        spans.append((t, min(t + dt.timedelta(days=6), end)))
        t += dt.timedelta(days=6)

    def one(span):
        a, b = span
        js = _get(MID_URL, {"from": a.strftime("%Y-%m-%dT%H:%MZ"),
                            "to": b.strftime("%Y-%m-%dT%H:%MZ"), "format": "json"})
        return js.get("data", [])

    with ThreadPoolExecutor(8) as ex:
        rows = [x for part in ex.map(one, spans) for x in part]
    df = pd.DataFrame(rows)
    df = df[df.dataProvider == "APXMIDP"][["startTime", "price", "volume"]]
    df["time"] = pd.to_datetime(df.pop("startTime"), utc=True)
    df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    return df.rename(columns={"price": "mid_price", "volume": "mid_volume"})


def _rates(product: str, region: str, a: str, b: str) -> list[dict]:
    url = f"{OCTO}/{product}/electricity-tariffs/E-1R-{product}-{region}/standard-unit-rates/"
    out, params = [], {"period_from": a, "period_to": b, "page_size": 1500}
    while url:
        js = _get(url, params)
        out += js.get("results", [])
        url, params = js.get("next"), None
    return out


def fetch_agile(regions: list[str], start: str = "2018-12-31T00:00:00Z",
                end: str = "2026-01-02T00:00:00Z") -> pd.DataFrame:
    jobs = []
    for i, (prod, launch) in enumerate(AGILE_PRODUCTS):
        a = max(launch, start)
        b = AGILE_PRODUCTS[i + 1][1] if i + 1 < len(AGILE_PRODUCTS) else end
        b = min(b, end)
        if a < b:
            for r in regions:
                jobs.append((prod, r, a, b))

    def one(job):
        prod, r, a, b = job
        return [{"time": x["valid_from"], "agile_p_kwh": x["value_inc_vat"],
                 "product": prod, "region": r} for x in _rates(prod, r, a, b)]

    with ThreadPoolExecutor(6) as ex:
        rows = [x for part in ex.map(one, jobs) for x in part]
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df.time, utc=True)
    return (df.drop_duplicates(["region", "time"]).sort_values(["region", "time"])
            .reset_index(drop=True))


def fetch_extension(start: str, end: str, suffix: str) -> None:
    """Prices for a later period (MID and London Agile only), saved with a suffix.
    From 1 October 2026 the Agile API's value_inc_vat equals value_exc_vat, so compare
    Agile levels across that date with care (check_2026.py uses only within-day ratios)."""
    a = dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc) - dt.timedelta(days=1)
    b = dt.datetime.fromisoformat(end).replace(tzinfo=dt.timezone.utc) + dt.timedelta(days=1)
    mid = fetch_mid(a, b)
    mid.to_parquet(RAW / f"mid{suffix}.parquet", index=False)
    print(f"mid{suffix}: {len(mid):,} rows {mid.time.min()} -> {mid.time.max()}")
    ag = fetch_agile(["C"], start=a.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     end=b.strftime("%Y-%m-%dT%H:%M:%SZ"))
    ag.to_parquet(RAW / f"agile_C{suffix}.parquet", index=False)
    print(f"agile_C{suffix}: {len(ag):,} rows {ag.time.min()} -> {ag.time.max()}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", help="later-period start date (YYYY-MM-DD)")
    ap.add_argument("--end", help="later-period end date, exclusive (YYYY-MM-DD)")
    ap.add_argument("--suffix", default="_ext")
    args = ap.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    if args.start:
        fetch_extension(args.start, args.end, args.suffix)
        raise SystemExit
    t0 = time.time()
    mid = fetch_mid()
    mid.to_parquet(RAW / "mid.parquet", index=False)
    print(f"mid: {len(mid):,} rows {mid.time.min()} -> {mid.time.max()} ({time.time()-t0:.0f}s)")
    t0 = time.time()
    ag = fetch_agile(["C"])
    ag.to_parquet(RAW / "agile_C.parquet", index=False)
    print(f"agile C: {len(ag):,} rows {ag.time.min()} -> {ag.time.max()} ({time.time()-t0:.0f}s)")
    print(ag.groupby("product").time.agg(["min", "max", "count"]))
    t0 = time.time()
    ag_all = fetch_agile(REGIONS, start="2024-12-31T00:00:00Z")
    ag_all.to_parquet(RAW / "agile_all_regions_2025.parquet", index=False)
    print(f"agile all regions 2025: {len(ag_all):,} rows ({time.time()-t0:.0f}s)")
