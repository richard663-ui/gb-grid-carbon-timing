"""Download public GB grid data used in the study.

Sources (both public, no key needed):
  * NESO Carbon Intensity API  - national half-hourly carbon intensity (gCO2/kWh)
    and generation mix (%), https://api.carbonintensity.org.uk
  * Elexon Insights (BMRS) FUELHH - half-hourly generation by fuel type (MW),
    https://data.elexon.co.uk/bmrs/api/v1/datasets/FUELHH

Run:  python src/fetch_data.py  (writes parquet files to data/raw/)
      python src/fetch_data.py --start 2026-01-01 --end 2026-10-31 --suffix _2026 \
          --datasets intensity mix   (out-of-period check, see check_2026.py)
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
START = dt.date(2018, 1, 1)
END = dt.date(2025, 12, 31)  # inclusive
CI_API = "https://api.carbonintensity.org.uk"
ELEXON = "https://data.elexon.co.uk/bmrs/api/v1/datasets/FUELHH"


def _get(url: str, params: dict | None = None, tries: int = 5) -> dict:
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=90)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(2 ** i)
    raise RuntimeError(f"failed: {url} {params}")


def _chunks(days: int, start: dt.date = START, end: dt.date = END):
    d = start
    while d <= end:
        e = min(d + dt.timedelta(days=days - 1), end)
        yield d, e
        d = e + dt.timedelta(days=1)


def fetch_intensity(start: dt.date = START, end: dt.date = END) -> pd.DataFrame:
    def one(span):
        a, b = span
        url = f"{CI_API}/intensity/{a}T00:00Z/{b + dt.timedelta(days=1)}T00:00Z"
        rows = _get(url).get("data", [])
        return [
            {"from": r["from"], "ci_forecast": r["intensity"].get("forecast"),
             "ci_actual": r["intensity"].get("actual")}
            for r in rows
        ]

    with ThreadPoolExecutor(6) as ex:
        out = [x for part in ex.map(one, _chunks(28, start, end)) for x in part]
    df = pd.DataFrame(out)
    df["time"] = pd.to_datetime(df.pop("from"), utc=True)
    return df.drop_duplicates("time").sort_values("time").reset_index(drop=True)


def fetch_mix(start: dt.date = START, end: dt.date = END) -> pd.DataFrame:
    def one(span):
        a, b = span
        url = f"{CI_API}/generation/{a}T00:00Z/{b + dt.timedelta(days=1)}T00:00Z"
        rows = _get(url).get("data", [])
        out = []
        for r in rows:
            rec = {"from": r["from"]}
            rec.update({f"pct_{g['fuel']}": g["perc"] for g in r.get("generationmix", [])})
            out.append(rec)
        return out

    with ThreadPoolExecutor(6) as ex:
        out = [x for part in ex.map(one, _chunks(28, start, end)) for x in part]
    df = pd.DataFrame(out)
    df["time"] = pd.to_datetime(df.pop("from"), utc=True)
    return df.drop_duplicates("time").sort_values("time").reset_index(drop=True)


def fetch_fuelhh(start: dt.date = START, end: dt.date = END) -> pd.DataFrame:
    def one(span):
        a, b = span
        js = _get(ELEXON, {"settlementDateFrom": str(a), "settlementDateTo": str(b),
                           "format": "json"})
        return js.get("data", [])

    with ThreadPoolExecutor(8) as ex:
        out = [x for part in ex.map(one, _chunks(7, start, end)) for x in part]
    df = pd.DataFrame(out)[["startTime", "fuelType", "generation"]]
    df["time"] = pd.to_datetime(df.pop("startTime"), utc=True)
    wide = df.pivot_table(index="time", columns="fuelType", values="generation",
                          aggfunc="mean").sort_index()
    wide.columns = [f"mw_{c}" for c in wide.columns]
    return wide.reset_index()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=dt.date.fromisoformat, default=START)
    ap.add_argument("--end", type=dt.date.fromisoformat, default=END)
    ap.add_argument("--suffix", default="", help="appended to the output file names")
    ap.add_argument("--datasets", nargs="+", default=["intensity", "mix", "fuelhh"])
    args = ap.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    fns = {"intensity": fetch_intensity, "mix": fetch_mix, "fuelhh": fetch_fuelhh}
    for name in args.datasets:
        t = time.time()
        df = fns[name](args.start, args.end)
        df.to_parquet(RAW / f"{name}{args.suffix}.parquet", index=False)
        print(f"{name}: {len(df):,} rows, {df['time'].min()} -> {df['time'].max()}"
              f" ({time.time() - t:.0f}s)")
