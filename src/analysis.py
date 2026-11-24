"""Main analysis for: "Is a fixed overnight schedule still the low-carbon choice?
Wind, solar and the value of carbon-aware load shifting on the GB grid, 2018-2025".

Run after fetch_data.py:  python src/analysis.py
Outputs: results/*.csv, results/summary.json, paper/fig*.png
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf

ROOT = Path(__file__).resolve().parents[1]
RAW, RES, FIG = ROOT / "data" / "raw", ROOT / "results", ROOT / "paper"
RES.mkdir(exist_ok=True)
FIG.mkdir(exist_ok=True)
TZ = "Europe/London"
YEARS = list(range(2019, 2026))  # full calendar years with generation-mix data
RNG = np.random.default_rng(42)

# Direct-combustion emission factors (gCO2/kWh) as used in the NESO Carbon Intensity
# methodology (Bruce et al., 2024, Table 1). Main specification: interconnectors and
# pumped storage are zero-emission at the GB boundary (domestic-emissions perspective).
EF = {"mw_COAL": 937, "mw_CCGT": 394, "mw_OCGT": 651, "mw_OIL": 935,
      "mw_BIOMASS": 120, "mw_OTHER": 300}
# Robustness: imports (positive flows only) charged at the NESO default factors for the
# connected system (France ~53, Netherlands ~474, Belgium ~179, Ireland ~458). Norway (NSL)
# and Denmark (Viking) are not in the NESO table and are left at zero.
EF_IMPORT = {"mw_INTFR": 53, "mw_INTIFA2": 53, "mw_INTELEC": 53, "mw_INTNED": 474,
             "mw_INTNEM": 179, "mw_INTIRL": 458, "mw_INTEW": 458, "mw_INTGRNL": 458}


# ----------------------------------------------------------------------------- data
def load() -> pd.DataFrame:
    ci = pd.read_parquet(RAW / "intensity.parquet")
    mix = pd.read_parquet(RAW / "mix.parquet")
    fu = pd.read_parquet(RAW / "fuelhh.parquet")
    df = ci.merge(mix, on="time", how="left").merge(fu, on="time", how="left")
    # implausible forecast spikes (API artefacts) -> missing
    df.loc[(df.ci_forecast > 1000) | (df.ci_forecast <= 0), "ci_forecast"] = np.nan
    df["local"] = df.time.dt.tz_convert(TZ)
    df["date"] = df.local.dt.date
    df["year"] = df.local.dt.year
    df["month"] = df.local.dt.month
    df["hour"] = df.local.dt.hour + df.local.dt.minute / 60
    mw = [c for c in df.columns if c.startswith("mw_")]
    df[mw] = df[mw].fillna(0)
    # tCO2 per half hour: MW * 1000 (kW) * g/kWh = g/h ; /1e6 -> t/h ; *0.5 h
    df["emis_t"] = sum(df[c].clip(lower=0) * f for c, f in EF.items()) * 1000 / 1e6 * 0.5
    df["emis_imp_t"] = df.emis_t + sum(df[c].clip(lower=0) * f for c, f in EF_IMPORT.items()
                                       if c in df) * 1000 / 1e6 * 0.5
    df["supply_mw"] = df[mw].sum(axis=1)  # domestic gen + net imports + net storage
    return df[df.year.between(2018, 2025)].reset_index(drop=True)


# ------------------------------------------------------------ 1. descriptive trends
def trends(df: pd.DataFrame) -> pd.DataFrame:
    d = df[df.year.isin(YEARS)]
    daily = d.groupby("date").agg(ci_max=("ci_actual", "max"), ci_min=("ci_actual", "min"),
                                  year=("year", "first"))
    daily["spread"] = daily.ci_max - daily.ci_min
    out = d.groupby("year").agg(
        ci_mean=("ci_actual", "mean"), wind_pct=("pct_wind", "mean"),
        solar_pct=("pct_solar", "mean"), gas_pct=("pct_gas", "mean"),
        coal_pct=("pct_coal", "mean"),
        share_below_100=("ci_actual", lambda s: (s < 100).mean() * 100),
    )
    out["daily_spread_median"] = daily.groupby("year").spread.median()
    out["daily_spread_rel"] = out.daily_spread_median / out.ci_mean * 100
    # hour of day with the lowest mean intensity
    prof = d.groupby(["year", d.local.dt.hour]).ci_actual.mean().unstack()
    out["cleanest_hour"] = prof.idxmin(axis=1)
    out["dirtiest_hour"] = prof.idxmax(axis=1)
    out.round(2).to_csv(RES / "table1_trends.csv")

    fig, ax = plt.subplots(figsize=(7, 4))
    cmap = plt.get_cmap("viridis", len(YEARS))
    for i, y in enumerate(YEARS):
        ax.plot(prof.columns, prof.loc[y], color=cmap(i), label=str(y), lw=1.8)
    ax.set(xlabel="Hour of day (UK local time)", ylabel="Mean carbon intensity (gCO$_2$/kWh)",
           title="Average daily profile of GB carbon intensity")
    ax.set_xticks(range(0, 24, 3))
    ax.legend(ncol=4, fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(FIG / "fig1_diurnal_profile.png", dpi=200)
    plt.close(fig)
    return out


# --------------------------------------------- 2. what drives intensity: regression
def drivers(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for y in YEARS:
        d = df[(df.year == y)].dropna(subset=["ci_actual", "pct_wind", "pct_solar"])
        d = d.assign(hod=d.local.dt.hour)
        m = smf.ols("ci_actual ~ pct_wind + pct_solar + pct_nuclear + C(month) + C(hod)",
                    data=d).fit(cov_type="HAC", cov_kwds={"maxlags": 48})
        for v in ["pct_wind", "pct_solar", "pct_nuclear"]:
            rows.append({"year": y, "var": v, "coef": m.params[v], "se": m.bse[v],
                         "r2": m.rsquared, "n": int(m.nobs)})
    out = pd.DataFrame(rows)
    out.round(3).to_csv(RES / "table2_drivers.csv", index=False)
    return out


# ------------------------------------------------ 3. marginal emission factors (MEF)
def mef(df: pd.DataFrame, n_boot: int = 300, col: str = "emis_t",
        tag: str = "") -> pd.DataFrame:
    """Hawkes (2010) style: regress change in GB fossil emissions on change in total
    supply between consecutive half hours. Estimated by year and 4-hour block."""
    d = df.sort_values("time").copy()
    d["dE"] = d[col].diff() * 2  # tCO2/h
    d["dD"] = d.supply_mw.diff()   # MW
    gap = d.time.diff() != pd.Timedelta("30min")
    d.loc[gap, ["dE", "dD"]] = np.nan
    d = d.dropna(subset=["dE", "dD"])
    d = d[d.dD.abs() > 50]  # ignore near-zero changes
    d["block"] = (d.local.dt.hour // 4) * 4
    rows = []
    for (y, b), g in d[d.year.isin(YEARS)].groupby(["year", "block"]):
        x, yv = g.dD.values, g.dE.values
        slope = (x @ yv) / (x @ x) * 1000  # t/MWh -> g/kWh  (tCO2/h / MW = t/MWh)
        idx = RNG.integers(0, len(x), size=(n_boot, len(x)))
        bs = (x[idx] * yv[idx]).sum(1) / (x[idx] ** 2).sum(1) * 1000
        rows.append({"year": y, "block": b, "mef": slope,
                     "lo": np.percentile(bs, 2.5), "hi": np.percentile(bs, 97.5), "n": len(x)})
    out = pd.DataFrame(rows)
    yr = []
    for y, g in d[d.year.isin(YEARS)].groupby("year"):
        x, yv = g.dD.values, g.dE.values
        yr.append({"year": y, "mef_all": (x @ yv) / (x @ x) * 1000})
    out = out.merge(pd.DataFrame(yr), on="year")
    out.round(1).to_csv(RES / f"table3_mef{tag}.csv", index=False)
    return out


# ------------------------------------------------- 4. load-shifting experiment
WINDOWS = {
    # name: (plug-in hour, plug-out hour next day or same day, baseline start)
    "overnight_ev": (18, 7),
    "daytime_workplace": (9, 17),
    "flexible_24h": (0, 24),
}


def _window_frame(df: pd.DataFrame, start: int, end: int) -> pd.DataFrame:
    d = df[["time", "local", "year", "ci_actual", "ci_forecast"]].dropna(subset=["ci_actual"]).copy()
    h = d.local.dt.hour
    if start < end:
        inwin = (h >= start) & (h < end)
        d["session"] = d.local.dt.date
    else:
        inwin = (h >= start) | (h < end)
        d["session"] = (d.local - pd.Timedelta(hours=end)).dt.date
    d = d[inwin].sort_values("time")
    return d


def shifting(df: pd.DataFrame, mef_tab: pd.DataFrame, n_slots: int = 6,
             tag: str = "") -> tuple[pd.DataFrame, dict]:
    """n_slots half-hours (3 h, e.g. ~21 kWh at 7 kW) of flexible demand per session.

    Strategies
      dumb      : start charging at plug-in
      fixed     : the fixed clock slots that were cleanest on average in 2019-2021
                  (frozen in-sample, then applied unchanged to 2022-2025)
      forecast  : choose the n lowest *forecast* intensity slots (implementable)
      oracle    : choose the n lowest *actual* intensity slots (upper bound)
    Evaluated on actual average intensity and on block-level marginal factors.
    """
    mef_map = mef_tab.set_index(["year", "block"]).mef.to_dict()
    results, details = [], {}
    for name, (s, e) in WINDOWS.items():
        d = _window_frame(df, s, e)
        d["slot"] = d.local.dt.hour * 2 + d.local.dt.minute // 30
        d["mef"] = [mef_map.get((y, (h // 4) * 4), np.nan)
                    for y, h in zip(d.year, d.local.dt.hour)]
        # frozen fixed schedule from training period
        train = d[d.year.between(2019, 2021)]
        best_slots = train.groupby("slot").ci_actual.mean().nsmallest(n_slots).index.tolist()
        details[name] = {"fixed_slots_local": sorted(f"{s_ // 2:02d}:{(s_ % 2) * 30:02d}"
                                                     for s_ in best_slots)}
        recs = []
        for sess, g in d.groupby("session"):
            if len(g) < n_slots * 2 or g.ci_forecast.isna().any():
                continue
            pick = {
                "dumb": g.head(n_slots),
                "fixed": g[g.slot.isin(best_slots)],
                "forecast": g.nsmallest(n_slots, "ci_forecast"),
                "oracle": g.nsmallest(n_slots, "ci_actual"),
            }
            if len(pick["fixed"]) != n_slots:
                continue
            rec = {"session": sess, "year": g.year.iloc[0],
                   "aef_flat": g.ci_actual.mean(), "mef_flat": g.mef.mean(),
                   "forecast_daytime_share": pick["forecast"].local.dt.hour.between(9, 16).mean()}
            for k, p in pick.items():
                rec[f"aef_{k}"] = p.ci_actual.mean()
                rec[f"mef_{k}"] = p.mef.mean()
            recs.append(rec)
        r = pd.DataFrame(recs)
        r = r[r.year.isin(YEARS)]
        r.to_csv(RES / f"sessions_{name}{tag}.csv", index=False)
        for y, g in r.groupby("year"):
            row = {"window": name, "year": y, "n_sessions": len(g)}
            for k in ["dumb", "fixed", "forecast", "oracle"]:
                row[f"aef_{k}"] = g[f"aef_{k}"].mean()
                row[f"mef_{k}"] = g[f"mef_{k}"].mean()
            row["aef_flat"], row["mef_flat"] = g.aef_flat.mean(), g.mef_flat.mean()
            row["forecast_daytime_share"] = g.forecast_daytime_share.mean() * 100
            for k in ["dumb", "fixed", "forecast", "oracle"]:
                row[f"vsflat_aef_{k}_pct"] = (1 - row[f"aef_{k}"] / row["aef_flat"]) * 100
                row[f"vsflat_mef_{k}_pct"] = (1 - row[f"mef_{k}"] / row["mef_flat"]) * 100
            for k in ["fixed", "forecast", "oracle"]:
                row[f"saving_aef_{k}_pct"] = (1 - row[f"aef_{k}"] / row["aef_dumb"]) * 100
                row[f"saving_mef_{k}_pct"] = (1 - row[f"mef_{k}"] / row["mef_dumb"]) * 100
            # forecast vs fixed: paired bootstrap of mean difference (g/kWh)
            diff = (g.aef_fixed - g.aef_forecast).values
            bs = RNG.choice(diff, size=(2000, len(diff))).mean(1)
            row["forecast_minus_fixed_gain"] = diff.mean()
            row["gain_lo"], row["gain_hi"] = np.percentile(bs, [2.5, 97.5])
            results.append(row)
    out = pd.DataFrame(results)
    out.round(2).to_csv(RES / f"table4_shifting{tag}.csv", index=False)
    return out, details


def fig_shifting(sh: pd.DataFrame) -> None:
    titles = {"overnight_ev": "Overnight plug-in (18:00-07:00)",
              "daytime_workplace": "Workplace plug-in (09:00-17:00)",
              "flexible_24h": "Fully flexible (00:00-24:00)"}
    fig, axes = plt.subplots(1, 3, figsize=(12, 3.8), sharey=True)
    for ax, w in zip(axes, titles):
        g = sh[sh.window == w]
        for k, c, lab in [("fixed", "#7f7f7f", "Fixed clock rule (frozen 2019-21)"),
                          ("forecast", "#1f77b4", "Forecast-based (implementable)"),
                          ("oracle", "#2ca02c", "Perfect foresight (bound)")]:
            ax.plot(g.year, g[f"vsflat_aef_{k}_pct"], marker="o", color=c, label=lab)
        ax.axvspan(2018.6, 2021.5, color="#eeeeee", zorder=0)
        ax.set(title=titles[w], xlabel="Year")
    axes[0].set_ylabel("Intensity reduction vs. flat use (%)")
    axes[0].legend(frameon=False, fontsize=7.5, loc="lower left")
    fig.tight_layout()
    fig.savefig(FIG / "fig3_shifting_savings.png", dpi=200)
    plt.close(fig)

    g = sh[sh.window == "flexible_24h"]
    fig, ax = plt.subplots(figsize=(5.5, 3.4))
    ax.bar(g.year, g.forecast_daytime_share, color="#ff7f0e")
    ax.set(xlabel="Year", ylabel="% of chosen slots in 09:00-17:00",
           title="Where the forecast rule puts flexible load")
    fig.tight_layout()
    fig.savefig(FIG / "fig4_daytime_share.png", dpi=200)
    plt.close(fig)


def fig_drivers(tr: pd.DataFrame, mf: pd.DataFrame) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.6))
    ax = axes[0]
    ax.plot(tr.index, tr.ci_mean, marker="o", label="Average intensity (AEF)")
    yr = mf.drop_duplicates("year")
    ax.plot(yr.year, yr.mef_all, marker="s", label="Marginal factor (MEF)")
    ax.set(xlabel="Year", ylabel="gCO$_2$/kWh", title="Average vs marginal emissions")
    ax.legend(frameon=False, fontsize=8)
    ax = axes[1]
    ax.plot(tr.index, tr.daily_spread_median, marker="o", color="#d62728")
    ax.set(xlabel="Year", ylabel="Median daily max-min (gCO$_2$/kWh)",
           title="Within-day variability")
    fig.tight_layout()
    fig.savefig(FIG / "fig2_aef_mef_spread.png", dpi=200)
    plt.close(fig)


def main() -> None:
    df = load()
    tr = trends(df)
    dr = drivers(df)
    mf = mef(df)
    sh, det = shifting(df, mf)
    mf_imp = mef(df, col="emis_imp_t", tag="_with_imports")
    sh_imp, _ = shifting(df, mf_imp, tag="_with_imports")
    fig_drivers(tr, mf)
    fig_shifting(sh)
    summary = {
        "n_halfhours": int(len(df)),
        "period": [str(df.time.min()), str(df.time.max())],
        "forecast_mae": float((df.ci_forecast - df.ci_actual).abs().mean()),
        "trends": tr.round(2).reset_index().to_dict("records"),
        "drivers": dr.round(3).to_dict("records"),
        "mef_year": mf.drop_duplicates("year")[["year", "mef_all"]].round(1).to_dict("records"),
        "mef_year_with_imports": mf_imp.drop_duplicates("year")[["year", "mef_all"]]
        .round(1).to_dict("records"),
        "shifting": sh.round(2).to_dict("records"),
        "fixed_schedule": det,
    }
    (RES / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print(tr.round(1).to_string())
    print(dr[dr["var"] == "pct_wind"].round(2).to_string())
    print(mf.drop_duplicates("year")[["year", "mef_all"]].round(0).to_string())
    print(mf.pivot(index="year", columns="block", values="mef").round(0).to_string())
    print(sh[["window", "year", "aef_flat", "vsflat_aef_dumb_pct", "vsflat_aef_fixed_pct",
              "vsflat_aef_forecast_pct", "vsflat_aef_oracle_pct", "vsflat_mef_fixed_pct",
              "vsflat_mef_forecast_pct", "forecast_daytime_share"]].round(1).to_string())
    cols = ["window", "year", "aef_dumb", "saving_aef_fixed_pct", "saving_aef_forecast_pct",
            "saving_aef_oracle_pct", "saving_mef_forecast_pct", "forecast_minus_fixed_gain",
            "gain_lo", "gain_hi"]
    print(sh[cols].round(1).to_string())
    print(det, "forecast MAE", summary["forecast_mae"])
    print("WITH IMPORTS")
    print(mf_imp.pivot(index="year", columns="block", values="mef").round(0).to_string())
    print(mf_imp.drop_duplicates("year")[["year", "mef_all"]].round(0).to_string())
    print(sh_imp[sh_imp.window == "flexible_24h"][["year", "vsflat_mef_fixed_pct",
          "vsflat_mef_forecast_pct", "vsflat_mef_oracle_pct"]].round(1).to_string())


if __name__ == "__main__":
    main()
