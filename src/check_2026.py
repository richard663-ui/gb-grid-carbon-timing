"""Out-of-period check (v1.0): did the 2025 pattern hold in 2026?

Compares January-October 2026 with January-October 2025 (like for like, so that the
missing winter months do not bias the comparison). The rules are not re-fitted: the
fixed clock rule (01:00-03:30) and the battery timer are the ones frozen on 2019-2021.

Run after fetch_data.py / fetch_prices.py with the 2026 options (see their docstrings),
analysis.py, cost_analysis.py and storage_analysis.py:  python src/check_2026.py
Outputs: results/table13_check_2026.csv, results/check_2026.json, paper/fig9_check_2026.png

Agile: from 1 October 2026 the public API reports the same value with and without VAT, so
only the within-day (midday-to-night) ratio of Agile prices is reported, not their level.
"""
from __future__ import annotations

import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from analysis import FIG, RAW, RES, TZ
from cost_analysis import matrices
from storage_analysis import GRID, daily, optimise, timer

MONTHS = range(1, 11)  # January to October


def load(suffix: str, year: int) -> pd.DataFrame:
    ci = pd.read_parquet(RAW / f"intensity{suffix}.parquet")
    mix = pd.read_parquet(RAW / f"mix{suffix}.parquet")
    mid = pd.read_parquet(RAW / f"mid{suffix}.parquet")
    mid.loc[mid.mid_volume <= 0, "mid_price"] = np.nan  # zero-volume placeholder
    ag = pd.read_parquet(RAW / f"agile_C{suffix}.parquet")[["time", "agile_p_kwh"]]
    df = (ci.merge(mix, on="time", how="left")
            .merge(mid[["time", "mid_price"]], on="time", how="left")
            .merge(ag, on="time", how="left")
            .rename(columns={"mid_price": "mid", "agile_p_kwh": "agile"}))
    df.loc[(df.ci_forecast > 1000) | (df.ci_forecast <= 0), "ci_forecast"] = np.nan
    df["local"] = df.time.dt.tz_convert(TZ)
    df["year"] = df.local.dt.year
    df = df[(df.year == year) & df.local.dt.month.isin(MONTHS)]
    return df.reset_index(drop=True)


def describe(d: pd.DataFrame) -> dict:
    d = d.assign(hod=d.local.dt.hour, date=d.local.dt.date)
    day = d.groupby("date").ci_actual.agg(["max", "min"])
    prof = d.groupby("hod").ci_actual.mean()
    r = {"n_halfhours": len(d), "ci_mean": d.ci_actual.mean(),
         "wind_pct": d.pct_wind.mean(), "solar_pct": d.pct_solar.mean(),
         "gas_pct": d.pct_gas.mean(),
         "share_below_100": (d.ci_actual < 100).mean() * 100,
         "daily_spread_median": (day["max"] - day["min"]).median(),
         "cleanest_hour": int(prof.idxmin()), "dirtiest_hour": int(prof.idxmax()),
         "mid_mean": d.mid.mean(), "mid_negative_pct": (d.mid.dropna() < 0).mean() * 100,
         "mid_cheapest_hour": int(d.groupby("hod").mid.mean().idxmin())}
    r["spread_rel_pct"] = r["daily_spread_median"] / r["ci_mean"] * 100
    for v in ["ci_actual", "mid", "agile"]:
        r[f"{v}_midday_night_ratio"] = (d[d.hod.between(11, 13)][v].mean()
                                        / d[d.hod.between(1, 3)][v].mean())
    return r


def shifting(M: dict, fixed_clock: list[int], n: int = 6) -> dict:
    flat = M["ci_actual"].mean(1)

    def at(idx):
        return np.take_along_axis(M["ci_actual"], idx, 1).mean(1)

    fc = np.argsort(M["ci_forecast"], 1, kind="stable")[:, :n]
    orc = np.argsort(M["ci_actual"], 1, kind="stable")[:, :n]
    cheap = np.argsort(M["mid"], 1, kind="stable")[:, :n]
    mask = np.isin(M["clock"], fixed_clock)
    ok = mask.sum(1) == n
    fix = np.argsort(~mask, 1, kind="stable")[:, :n]
    hod_fc = np.take_along_axis(M["clock"], fc, 1) // 2
    F = flat[ok].mean()
    red = lambda x: (1 - x[ok].mean() / F) * 100
    r = {"n_days": int(ok.sum()), "fixed_red_pct": red(at(fix)), "forecast_red_pct": red(at(fc)),
         "oracle_red_pct": red(at(orc)), "cheapest_mid_red_pct": red(at(cheap)),
         "forecast_daytime_share_pct": ((hod_fc >= 9) & (hod_fc < 17))[ok].mean() * 100}
    r["fixed_share_of_forecast_pct"] = r["fixed_red_pct"] / r["forecast_red_pct"] * 100
    r["cheapest_share_of_forecast_pct"] = r["cheapest_mid_red_pct"] / r["forecast_red_pct"] * 100
    mid_flat = M["mid"].mean(1)[ok].mean()
    r["cheapest_mid_cost_red_pct"] = (1 - np.take_along_axis(M["mid"], cheap, 1).mean(1)[ok].mean()
                                      / mid_flat) * 100
    rc = M["ci_actual"].argsort(1).argsort(1)
    rp = M["mid"].argsort(1).argsort(1)
    rc, rp = rc - rc.mean(1, keepdims=True), rp - rp.mean(1, keepdims=True)
    r["rho_mid_mean"] = float(((rc * rp).sum(1) / np.sqrt((rc ** 2).sum(1) * (rp ** 2).sum(1))).mean())
    return r


def storage(M: dict, timer_clock: tuple[list[int], list[int]]) -> dict:
    nanm = np.full_like(M["ci_actual"], np.nan)
    out = {}
    for k, sig in [("price", M["mid"]), ("carbon_fc", M["ci_forecast"])]:
        out[k] = daily(M, *optimise(sig, GRID), nanm, nanm, "mid")
    out["timer"] = daily(M, *timer(M, GRID, *timer_clock), nanm, nanm, "mid")
    p, c, t = out["price"], out["carbon_fc"], out["timer"]
    return {"battery_value_per_day_gbp_mw": p.value.mean(),
            "battery_dco2_avg_per_out": p.dco2_avg.sum() / p.e_out.sum(),
            "battery_carbon_fc_dco2_avg_per_out": c.dco2_avg.sum() / c.e_out.sum(),
            "battery_midday_charge_pct": p.c_midday.sum() / p.c_sum.sum() * 100,
            "battery_value_share_carbon_fc_pct": c.value.mean() / p.value.mean() * 100,
            "battery_value_share_timer_pct": t.value.mean() / p.value.mean() * 100}


def fig_check(d25: pd.DataFrame, d26: pd.DataFrame) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    for d, c, lab in [(d25, "#9ecae1", "2025 (Jan-Oct)"), (d26, "#08519c", "2026 (Jan-Oct)")]:
        h = d.local.dt.hour
        axes[0].plot(range(24), d.groupby(h).ci_actual.mean(), color=c, lw=2, label=lab)
        axes[1].plot(range(24), d.groupby(h).mid.mean(), color=c, lw=2, label=lab)
    axes[0].set(title="(a) Carbon intensity by hour", xlabel="Hour of day (UK local time)",
                ylabel="gCO$_2$/kWh")
    axes[1].set(title="(b) Wholesale price by hour", xlabel="Hour of day (UK local time)",
                ylabel="£/MWh")
    for ax in axes:
        ax.set_xticks(range(0, 24, 3))
        ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(FIG / "fig9_check_2026.png", dpi=200)
    plt.close(fig)


def main() -> None:
    summ = json.loads((RES / "summary.json").read_text())
    fixed_txt = summ["fixed_schedule"]["flexible_24h"]["fixed_slots_local"]
    fixed_clock = [int(t[:2]) * 2 + int(t[3:]) // 30 for t in fixed_txt]
    st = json.loads((RES / "storage_summary.json").read_text())
    tclock = tuple([int(t[:2]) * 2 + int(t[3:]) // 30 for t in st[k]]
                   for k in ["timer_charge_local", "timer_discharge_local"])
    rows, frames = {}, {}
    for y, suf in [(2025, ""), (2026, "_2026")]:
        d = load(suf, y)
        frames[y] = d
        M = matrices(d, 0, 24)
        r = describe(d)
        r.update(shifting(M, fixed_clock))
        r.update(storage(M, tclock))
        r["n_days_common"] = len(M["year"])
        rows[f"{y} Jan-Oct"] = r
    tab = pd.DataFrame(rows)
    tab.round(3).to_csv(RES / "table13_check_2026.csv")
    (RES / "check_2026.json").write_text(json.dumps(
        {"months": list(MONTHS), "fixed_rule": fixed_txt, "timer": st["timer_charge_local"]
         + st["timer_discharge_local"], "table": tab.round(3).to_dict()}, indent=1, default=str))
    fig_check(frames[2025], frames[2026])
    pd.set_option("display.width", 200)
    print(tab.round(2).to_string())


if __name__ == "__main__":
    main()
