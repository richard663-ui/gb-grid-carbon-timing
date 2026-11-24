"""Cost extension: wholesale and household electricity prices, and whether the cheapest
and the cleanest times to use electricity coincide (GB, 2019-2025).

Run after fetch_data.py and fetch_prices.py:  python src/cost_analysis.py
Outputs: results/table5_*.csv ... results/table9_*.csv, results/cost_summary.json,
         paper/fig5_*.png ... paper/fig7_*.png
"""
from __future__ import annotations

import json
import warnings

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import rankdata

from analysis import FIG, RAW, RES, YEARS, load

RNG = np.random.default_rng(2026)
# a day with a constant (capped) Agile price has no rank correlation; it is reported as
# undefined and excluded from the mean, so the division warning is expected
warnings.filterwarnings("ignore", message="invalid value encountered in divide")
STRATS = ["flat", "arrival", "fixed_carbon", "fixed_cost", "carbon_fc", "carbon_oracle",
          "cost_mid", "cost_agile"]
LABEL = {"flat": "Flat use (reference)", "arrival": "Charge on arrival",
         "fixed_carbon": "Fixed clock rule, carbon-trained", "fixed_cost":
         "Fixed clock rule, price-trained", "carbon_fc": "Carbon-aware (forecast)",
         "carbon_oracle": "Carbon perfect foresight", "cost_mid": "Cheapest wholesale slots",
         "cost_agile": "Cheapest Agile slots"}
WINDOWS = {"flexible_24h": (0, 24), "overnight_ev": (18, 7), "daytime_workplace": (9, 17)}
VARS = ["ci_actual", "ci_forecast", "mid", "agile"]
EV_KWH_PER_DAY = 7.0  # illustrative: one hour at a 7 kW home charger every day


# ------------------------------------------------------------------------- data
def build() -> pd.DataFrame:
    df = load()[["time", "local", "year", "ci_actual", "ci_forecast", "pct_wind", "pct_solar"]]
    mid = pd.read_parquet(RAW / "mid.parquet")
    # zero-volume periods carry a placeholder price of 0 -> missing
    mid.loc[mid.mid_volume <= 0, "mid_price"] = np.nan
    ag = pd.read_parquet(RAW / "agile_C.parquet")[["time", "agile_p_kwh"]]
    df = (df.merge(mid[["time", "mid_price"]], on="time", how="left")
            .merge(ag, on="time", how="left")
            .rename(columns={"mid_price": "mid", "agile_p_kwh": "agile"}))
    return df[df.year.isin(YEARS)].reset_index(drop=True)


def matrices(df: pd.DataFrame, start: int, end: int, req: list[str] = VARS) -> dict:
    """Session x slot matrices for one availability window. Sessions with a clock change
    (wrong number of slots) or any missing value in VARS are dropped, so carbon and cost
    are always compared on exactly the same days (common sample)."""
    d = df.copy()
    h = d.local.dt.hour
    if start < end:
        d = d[(h >= start) & (h < end)].copy()
        d["session"] = d.local.dt.date
    else:
        d = d[(h >= start) | (h < end)].copy()
        d["session"] = (d.local - pd.Timedelta(hours=end)).dt.date
    d = d.sort_values("time")
    d["k"] = d.groupby("session").cumcount()
    d["clock"] = d.local.dt.hour * 2 + d.local.dt.minute // 30
    k_nom = ((end - start) % 24 or 24) * 2
    size = d.groupby("session").size()
    d = d[d.session.isin(size[size == k_nom].index)]
    n_raw = d.session.nunique()
    out = {c: d.pivot(index="session", columns="k", values=c) for c in VARS + ["clock"]}
    ok = np.ones(len(out["clock"]), bool)
    # every session must follow the same clock sequence (guards against data gaps that
    # coincide with a clock change and would misalign slots)
    expected = out["clock"].mode().iloc[0].to_numpy()
    ok &= (out["clock"].to_numpy() == expected).all(1)
    for c in req:
        ok &= out[c].notna().all(axis=1).values
    sess = out["clock"].index[ok]
    res = {c: out[c].loc[sess].to_numpy(float) for c in VARS + ["clock"]}
    res["session"] = np.array(sess)
    res["year"] = np.array([s.year for s in sess])
    res["n_dropped"] = n_raw - len(sess)
    return res


def _pick(M: dict, key: np.ndarray, n: int) -> np.ndarray:
    return np.argsort(key, axis=1, kind="stable")[:, :n]


def _mean_at(V: np.ndarray, idx: np.ndarray) -> np.ndarray:
    return np.take_along_axis(V, idx, axis=1).mean(1)


def _fixed(M: dict, slots: list[int], n: int) -> tuple[np.ndarray, np.ndarray]:
    mask = np.isin(M["clock"], slots)
    valid = mask.sum(1) == n
    idx = np.argsort(~mask, axis=1, kind="stable")[:, :n]
    return idx, valid


def train_fixed(M: dict, var: str, n: int) -> list[int]:
    tr = M["year"] <= 2021
    mean_by_k = M[var][tr].mean(0)
    clock = M["clock"][tr][0]
    return sorted(int(c) for c in clock[np.argsort(mean_by_k, kind="stable")[:n]])


def evaluate(M: dict, n: int, rules: dict[str, list[int]]) -> tuple[pd.DataFrame, dict, dict]:
    """Per-session mean carbon intensity, wholesale price and Agile price for every
    strategy. `rules` holds the frozen clock rules (trained on 2019-2021)."""
    picks = {
        "arrival": np.tile(np.arange(n), (len(M["year"]), 1)),
        "carbon_fc": _pick(M, M["ci_forecast"], n),
        "carbon_oracle": _pick(M, M["ci_actual"], n),
        "cost_mid": _pick(M, M["mid"], n),
        "cost_agile": _pick(M, M["agile"], n),
    }
    valid = np.ones(len(M["year"]), bool)
    for k, slots in rules.items():
        picks[k], v = _fixed(M, slots, n)
        valid &= v
    rec = {"session": M["session"], "year": M["year"]}
    for v in ["ci_actual", "mid", "agile"]:
        rec[f"{v}_flat"] = M[v].mean(1)
        for s, idx in picks.items():
            rec[f"{v}_{s}"] = _mean_at(M[v], idx)
    S = pd.DataFrame(rec)[valid]
    # alignment between the carbon-aware and the cheapest-slot choices
    for c in ["cost_mid", "cost_agile"]:
        a, b = np.sort(picks["carbon_fc"], 1), np.sort(picks[c], 1)
        S[f"overlap_{c}"] = np.array([len(np.intersect1d(x, y)) / n for x, y in zip(a, b)])[valid]
    rk = lambda X: rankdata(X, axis=1)
    rc = rk(M["ci_actual"])
    for v in ["mid", "agile"]:
        rv = rk(M[v])
        rcc, rvc = rc - rc.mean(1, keepdims=True), rv - rv.mean(1, keepdims=True)
        rho = (rcc * rvc).sum(1) / np.sqrt((rcc ** 2).sum(1) * (rvc ** 2).sum(1))
        S[f"rho_{v}"] = rho[valid]
    fixed_txt = {k: [f"{c // 2:02d}:{(c % 2) * 30:02d}" for c in v] for k, v in rules.items()}
    return S, fixed_txt, {"n_sessions": int(valid.sum()), "n_dropped": int(M["n_dropped"])}


def _ratio_ci(num: np.ndarray, den: np.ndarray, B: int = 2000) -> tuple[float, float, float]:
    idx = RNG.integers(0, len(num), size=(B, len(num)))
    bs = num[idx].mean(1) / den[idx].mean(1)
    return num.mean() / den.mean(), *np.percentile(bs, [2.5, 97.5])


def summarise(S: pd.DataFrame, window: str, n: int) -> pd.DataFrame:
    rows = []
    for y, g in S.groupby("year"):
        r = {"window": window, "n_slots": n, "year": y, "n_sessions": len(g)}
        for v in ["ci_actual", "mid", "agile"]:
            flat = g[f"{v}_flat"].mean()
            r[f"{v}_flat"] = flat
            for s in STRATS[1:]:
                m = g[f"{v}_{s}"].mean()
                r[f"{v}_{s}"] = m
                r[f"{v}_{s}_red_pct"] = (1 - m / flat) * 100
                r[f"{v}_{s}_saving_abs"] = flat - m
        # how much of each optimum the other objective captures
        r["carbon_share_captured_by_cost_mid"] = ((g.ci_actual_flat - g.ci_actual_cost_mid).mean()
                                                  / (g.ci_actual_flat - g.ci_actual_carbon_fc).mean() * 100)
        r["cost_share_captured_by_carbon_fc"] = ((g.mid_flat - g.mid_carbon_fc).mean()
                                                 / (g.mid_flat - g.mid_cost_mid).mean() * 100)
        r["agile_share_captured_by_carbon_fc"] = ((g.agile_flat - g.agile_carbon_fc).mean()
                                                  / (g.agile_flat - g.agile_cost_agile).mean() * 100)
        r["carbon_share_captured_by_cost_agile"] = ((g.ci_actual_flat - g.ci_actual_cost_agile).mean()
                                                    / (g.ci_actual_flat - g.ci_actual_carbon_fc).mean() * 100)
        # implied cost of abatement when choosing the carbon-aware over the cheapest slots
        # GBP/MWh divided by tCO2/MWh (g/kWh = kg/MWh -> /1000)
        dco2 = (g.ci_actual_cost_mid - g.ci_actual_carbon_fc).to_numpy() / 1000
        dmid = (g.mid_carbon_fc - g.mid_cost_mid).to_numpy()
        r["abate_gbp_t_wholesale"], r["abate_lo"], r["abate_hi"] = _ratio_ci(dmid, dco2)
        # robustness: both objectives with perfect information (carbon oracle vs MID)
        dco2o = (g.ci_actual_cost_mid - g.ci_actual_carbon_oracle).to_numpy() / 1000
        dmido = (g.mid_carbon_oracle - g.mid_cost_mid).to_numpy()
        (r["abate_gbp_t_wholesale_oracle"], r["abate_oracle_lo"],
         r["abate_oracle_hi"]) = _ratio_ci(dmido, dco2o)
        r["carbon_share_captured_by_cost_mid_vs_oracle"] = (
            (g.ci_actual_flat - g.ci_actual_cost_mid).mean()
            / (g.ci_actual_flat - g.ci_actual_carbon_oracle).mean() * 100)
        dco2a = (g.ci_actual_cost_agile - g.ci_actual_carbon_fc).to_numpy() / 1000
        dag = (g.agile_carbon_fc - g.agile_cost_agile).to_numpy() * 10  # p/kWh -> GBP/MWh
        r["abate_gbp_t_agile"], r["abate_agile_lo"], r["abate_agile_hi"] = _ratio_ci(dag, dco2a)
        r["rho_mid_mean"] = g.rho_mid.mean()
        r["rho_mid_share_pos"] = (g.rho_mid > 0).mean() * 100
        r["rho_agile_mean"] = g.rho_agile.mean()  # days with a constant (capped) price excluded
        r["rho_agile_undefined_days"] = int(g.rho_agile.isna().sum())
        r["overlap_mid_pct"] = g.overlap_cost_mid.mean() * 100
        r["overlap_agile_pct"] = g.overlap_cost_agile.mean() * 100
        k_win = {"flexible_24h": 48, "overnight_ev": 26, "daytime_workplace": 16}[window]
        r["overlap_random_pct"] = n / k_win * 100  # expected overlap of two random choices
        # household: annual saving for an illustrative EV (Agile, cheapest slots)
        r["agile_ev_gbp_per_year"] = ((g.agile_flat - g.agile_cost_agile).mean()
                                      * EV_KWH_PER_DAY * 365 / 100)
        r["agile_ev_gbp_per_year_carbon_fc"] = ((g.agile_flat - g.agile_carbon_fc).mean()
                                                * EV_KWH_PER_DAY * 365 / 100)
        rows.append(r)
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ price trends
def price_trends(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["hod"] = d.local.dt.hour
    d["date"] = d.local.dt.date
    out = {}
    for v, unit in [("mid", "gbp_mwh"), ("agile", "p_kwh")]:
        day = d.dropna(subset=[v]).groupby("date").agg(mx=(v, "max"), mn=(v, "min"),
                                                       year=("year", "first"), n=(v, "size"))
        day = day[day.n >= 46]
        day["spread"] = day.mx - day.mn
        g = d.groupby("year")[v]
        prof = d.groupby(["year", "hod"])[v].mean().unstack()
        out[f"{v}_mean_{unit}"] = g.mean()
        out[f"{v}_median_{unit}"] = g.median()
        out[f"{v}_daily_spread_median"] = day.groupby("year").spread.median()
        out[f"{v}_spread_rel_pct"] = out[f"{v}_daily_spread_median"] / g.mean() * 100
        out[f"{v}_negative_pct"] = g.apply(lambda s: (s.dropna() < 0).mean() * 100)
        out[f"{v}_cheapest_hour"] = prof.idxmin(axis=1)
        out[f"{v}_dearest_hour"] = prof.idxmax(axis=1)
        out[f"{v}_coverage_pct"] = g.apply(lambda s: s.notna().mean() * 100)
    # depth of the midday trough relative to the night: mean(11:00-14:00) / mean(01:00-04:00)
    for v in ["ci_actual", "mid", "agile"]:
        mid_day = d[d.hod.between(11, 13)].groupby("year")[v].mean()
        night = d[d.hod.between(1, 3)].groupby("year")[v].mean()
        out[f"{v}_midday_night_ratio"] = mid_day / night
    t = pd.DataFrame(out)
    t.round(2).to_csv(RES / "table5_price_trends.csv")
    return t


def fig_price_profiles(df: pd.DataFrame) -> None:
    d = df.copy()
    d["hod"] = d.local.dt.hour
    fig, axes = plt.subplots(1, 3, figsize=(15, 3.9))
    cmap = plt.get_cmap("viridis", len(YEARS))
    for ax, v, ttl in [(axes[0], "mid", "(a) Wholesale price (Elexon MID)"),
                       (axes[1], "agile", "(b) Household dynamic tariff (Agile, London)")]:
        prof = d.groupby(["year", "hod"])[v].mean().unstack()
        norm = prof.div(prof.mean(axis=1), axis=0)
        for i, y in enumerate(YEARS):
            ax.plot(norm.columns, norm.loc[y], color=cmap(i), lw=1.8, label=str(y))
        ax.axhline(1, color="k", lw=0.6, ls=":")
        ax.set(title=ttl, xlabel="Hour of day (UK local time)",
               ylabel="Price relative to that year's mean")
        ax.set_xticks(range(0, 24, 3))
    axes[0].legend(ncol=4, fontsize=7.5, frameon=False)
    t = pd.read_csv(RES / "table5_price_trends.csv", index_col=0)
    for v, c, lab in [("ci_actual", "#2ca02c", "Carbon intensity"),
                      ("mid", "#d62728", "Wholesale price"), ("agile", "#ff7f0e", "Agile price")]:
        axes[2].plot(t.index, t[f"{v}_midday_night_ratio"], marker="o", color=c, label=lab)
    axes[2].axhline(1, color="k", lw=0.6, ls=":")
    axes[2].set(title="(c) Midday (11-14h) relative to night (01-04h)", xlabel="Year",
                ylabel="Ratio of means (1 = equally low)")
    axes[2].legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(FIG / "fig5_price_profiles.png", dpi=200)
    plt.close(fig)


def fig_cobenefits(tab: pd.DataFrame) -> None:
    g = tab[(tab.window == "flexible_24h") & (tab.n_slots == 6)]
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.9), sharey=False)
    spec = [("cost_mid", "#d62728"), ("carbon_fc", "#1f77b4"), ("fixed_carbon", "#7f7f7f")]
    for s, c in spec:
        axes[0].plot(g.year, g[f"ci_actual_{s}_red_pct"], marker="o", color=c, label=LABEL[s])
        axes[1].plot(g.year, g[f"mid_{s}_red_pct"], marker="o", color=c, label=LABEL[s])
    axes[0].set(title="Carbon: intensity reduction vs flat use", xlabel="Year", ylabel="%")
    axes[1].set(title="Cost: wholesale cost reduction vs flat use", xlabel="Year", ylabel="%")
    axes[1].legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(FIG / "fig6_carbon_cost_cobenefits.png", dpi=200)
    plt.close(fig)


def fig_alignment(tab: pd.DataFrame) -> None:
    g = tab[(tab.window == "flexible_24h") & (tab.n_slots == 6)]
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6))
    axes[0].plot(g.year, g.rho_mid_mean, marker="o", label="Wholesale price vs carbon")
    axes[0].plot(g.year, g.rho_agile_mean, marker="s", label="Agile price vs carbon")
    axes[0].axhline(0, color="k", lw=0.6)
    axes[0].set(title="Within-day rank correlation (mean over days)", xlabel="Year",
                ylabel="Spearman ρ", ylim=(-0.2, 1))
    axes[0].legend(frameon=False, fontsize=8)
    w = 0.38
    axes[1].bar(g.year - w / 2, g.overlap_mid_pct, w, label="vs cheapest wholesale slots")
    axes[1].bar(g.year + w / 2, g.overlap_agile_pct, w, label="vs cheapest Agile slots")
    axes[1].axhline(g.overlap_random_pct.iloc[0], color="k", ls=":", lw=1, label="random choice")
    axes[1].set(title="Slots shared by the carbon-aware and cheapest choices", xlabel="Year",
                ylabel="% of chosen slots", ylim=(0, 100))
    axes[1].legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(FIG / "fig7_carbon_cost_alignment.png", dpi=200)
    plt.close(fig)


def regions_2025(n: int = 2) -> pd.DataFrame:
    """Household saving in each of the 14 GB regions in 2025 (24 h flexibility)."""
    ag = pd.read_parquet(RAW / "agile_all_regions_2025.parquet")
    ag["local"] = ag.time.dt.tz_convert("Europe/London")
    ag = ag[ag.local.dt.year == 2025]
    ag["date"] = ag.local.dt.date
    rows = []
    for r, g in ag.groupby("region"):
        size = g.groupby("date").size()
        g = g[g.date.isin(size[size == 48].index)].sort_values("time")
        X = g.agile_p_kwh.to_numpy().reshape(-1, 48)
        flat = X.mean(1)
        cheap = np.sort(X, 1)[:, :n].mean(1)
        rows.append({"region": r, "flat_p_kwh": flat.mean(), "cheapest_p_kwh": cheap.mean(),
                     "saving_pct": (1 - cheap.mean() / flat.mean()) * 100,
                     "ev_gbp_per_year": (flat - cheap).mean() * EV_KWH_PER_DAY * 365 / 100})
    out = pd.DataFrame(rows)
    out.round(2).to_csv(RES / "table9_agile_regions_2025.csv", index=False)
    return out


def main() -> None:
    df = build()
    tr = price_trends(df)
    fig_price_profiles(df)
    tabs, fixed, samples = [], {}, {}
    for w, (s, e) in WINDOWS.items():
        M = matrices(df, s, e)                                   # common sample
        Mc = matrices(df, s, e, req=["ci_actual", "ci_forecast"])  # as in the carbon analysis
        Mm = matrices(df, s, e, req=["mid"])                       # all days with prices
        for n in [2, 6, 12]:
            rules = {"fixed_carbon": train_fixed(Mc, "ci_actual", n),
                     "fixed_cost": train_fixed(Mm, "mid", n)}
            S, fx, info = evaluate(M, n, rules)
            if n == 6:
                S.to_csv(RES / f"cost_sessions_{w}.csv", index=False)
            tabs.append(summarise(S, w, n))
            fixed[f"{w}_n{n}"] = fx
            samples[f"{w}_n{n}"] = info
    tab = pd.concat(tabs, ignore_index=True)
    tab.round(3).to_csv(RES / "table6_cost_shifting_all.csv", index=False)
    fig_cobenefits(tab)
    fig_alignment(tab)
    reg = regions_2025()
    main_cols = ["year", "n_sessions", "ci_actual_carbon_fc_red_pct", "ci_actual_cost_mid_red_pct",
                 "mid_flat", "mid_cost_mid_red_pct", "mid_carbon_fc_red_pct",
                 "mid_fixed_carbon_red_pct", "carbon_share_captured_by_cost_mid",
                 "cost_share_captured_by_carbon_fc", "abate_gbp_t_wholesale", "abate_lo",
                 "abate_hi", "rho_mid_mean", "rho_mid_share_pos", "overlap_mid_pct"]
    m = tab[(tab.window == "flexible_24h") & (tab.n_slots == 6)][main_cols]
    m.round(2).to_csv(RES / "table7_cost_main.csv", index=False)
    hh_cols = ["year", "agile_flat", "agile_cost_agile_red_pct", "agile_cost_agile_saving_abs",
               "agile_carbon_fc_red_pct", "agile_fixed_carbon_red_pct",
               "agile_share_captured_by_carbon_fc", "carbon_share_captured_by_cost_agile",
               "agile_ev_gbp_per_year", "agile_ev_gbp_per_year_carbon_fc", "abate_gbp_t_agile",
               "abate_agile_lo", "abate_agile_hi", "rho_agile_mean", "overlap_agile_pct"]
    hh = tab[(tab.window == "flexible_24h") & (tab.n_slots == 2)][hh_cols]
    hh.round(2).to_csv(RES / "table8_household_agile.csv", index=False)
    summary = {"price_trends": tr.round(2).reset_index().to_dict("records"),
               "fixed_rules": fixed, "samples": samples,
               "main_flexible24h_n6": m.round(2).to_dict("records"),
               "household_flexible24h_n2": hh.round(2).to_dict("records"),
               "regions_2025_n2": reg.round(2).to_dict("records")}
    (RES / "cost_summary.json").write_text(json.dumps(summary, indent=1, default=str))
    pd.set_option("display.width", 250)
    print(tr.round(1).T.to_string())
    print(m.round(1).to_string())
    print(hh.round(1).to_string())
    print(reg.round(2).to_string())
    print(json.dumps(fixed, indent=0))
    print(json.dumps(samples))


if __name__ == "__main__":
    main()
