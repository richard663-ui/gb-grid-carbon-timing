"""Storage extension (v1.0): can a battery that moves electricity in time earn money and
cut emissions on the GB grid, and do price-driven and carbon-driven operation agree?

A battery differs from flexible demand in two ways: it both charges (adds demand) and
discharges (supplies the grid or the home), and it loses energy in the round trip. Its
effect on emissions is therefore
    dCO2 = sum_t EF_t * (charge_t - discharge_t) * dt,
which is negative (a cut) only if the energy it releases displaces enough emissions to
outweigh the extra energy it draws. EF is the average intensity (main) or the marginal
factor (robustness), exactly as in the load-shifting analysis.

Each day is optimised separately as a small linear programme with perfect foresight of
that day's prices (or forecast intensities), a periodic state of charge (the battery ends
the day as full as it started) and at most one full cycle a day.

Run after analysis.py and cost_analysis.py:  python src/storage_analysis.py
Outputs: results/table10_storage_grid.csv, results/table11_storage_sensitivity.csv,
         results/table12_storage_home.csv, results/storage_summary.json,
         paper/fig8_storage.png
"""
from __future__ import annotations

import json
from multiprocessing import Pool

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import sparse
from scipy.optimize import linprog

from analysis import FIG, RES, YEARS
from cost_analysis import build, matrices

RNG = np.random.default_rng(1001)
T, DT = 48, 0.5  # half-hours per day, hours per half-hour

# Grid-scale battery: 1 MW / 2 MWh (two-hour duration), 85% round-trip efficiency
# (the value used in NREL's utility-scale battery cost projections), one cycle a day.
GRID = {"P": 1.0, "E": 2.0, "eta": 0.85, "ncyc": 1}
# Home battery: 5 kWh usable, 2.5 kW, same efficiency, one cycle a day. Discharged energy
# is assumed to replace the home's own imports at the Agile rate (no export).
HOME = {"P": 2.5, "E": 5.0, "eta": 0.85, "ncyc": 1}
SENS = {
    "main (2 h, 85%, 1 cycle)": GRID,
    "1-hour battery": {**GRID, "E": 1.0},
    "4-hour battery": {**GRID, "E": 4.0},
    "efficiency 75%": {**GRID, "eta": 0.75},
    "efficiency 95%": {**GRID, "eta": 0.95},
    "up to 2 cycles a day": {**GRID, "ncyc": 2},
}


# ------------------------------------------------------------------ optimisation
def _lp_parts(P: float, E: float, eta: float, ncyc: float):
    """Variables x = [charge (T), discharge (T), state of charge at end of slot (T)].
    s_t = s_{t-1} + eta_c*c_t*dt - d_t*dt/eta_d, with s_0 = s_T (periodic)."""
    ec = ed = np.sqrt(eta)
    eye = sparse.identity(T, format="csr")
    prev = sparse.csr_matrix((np.ones(T), (np.arange(T), (np.arange(T) - 1) % T)),
                             shape=(T, T))
    a_eq = sparse.hstack([-ec * DT * eye, (DT / ed) * eye, eye - prev]).tocsr()
    # throughput: energy taken out of storage per day <= ncyc full cycles
    a_ub = sparse.csr_matrix(np.r_[np.zeros(T), np.full(T, DT / ed), np.zeros(T)][None, :])
    bounds = [(0, P)] * (2 * T) + [(0, E)] * T
    return a_eq, np.zeros(T), a_ub, np.array([ncyc * E]), bounds


def _solve_chunk(args):
    signal, cfg = args
    a_eq, b_eq, a_ub, b_ub, bounds = _lp_parts(cfg["P"], cfg["E"], cfg["eta"], cfg["ncyc"])
    C, D = np.zeros_like(signal), np.zeros_like(signal)
    for i, s in enumerate(signal):
        # minimise the signal-weighted net energy drawn: cost (price) or emissions
        cost = np.r_[s * DT, -s * DT, np.zeros(T)]
        r = linprog(cost, A_ub=a_ub, b_ub=b_ub, A_eq=a_eq, b_eq=b_eq, bounds=bounds,
                    method="highs")
        if r.status != 0:
            raise RuntimeError(f"LP failed on row {i}: {r.message}")
        C[i], D[i] = r.x[:T], r.x[T:2 * T]
    return C, D


def optimise(signal: np.ndarray, cfg: dict, workers: int = 4) -> tuple[np.ndarray, np.ndarray]:
    parts = np.array_split(signal, workers * 4)
    with Pool(workers) as pool:
        out = pool.map(_solve_chunk, [(p, cfg) for p in parts])
    return np.vstack([o[0] for o in out]), np.vstack([o[1] for o in out])


def timer(M: dict, cfg: dict, charge_clock: list[int], discharge_clock: list[int]):
    """Fixed timer, chosen on 2019-2021 data and then left unchanged: charge at full power
    in the charge slots until full, discharge at full power in the discharge slots until
    empty, every day."""
    ec = ed = np.sqrt(cfg["eta"])
    n = len(M["year"])
    C, D = np.zeros((n, T)), np.zeros((n, T))
    s = np.zeros(n)
    for k in range(T):  # charge slots come before discharge slots within the day
        clk = M["clock"][:, k]
        ch, dis = np.isin(clk, charge_clock), np.isin(clk, discharge_clock)
        c = np.where(ch, np.minimum(cfg["P"], (cfg["E"] - s) / (ec * DT)), 0)
        d = np.where(dis, np.minimum(cfg["P"], s * ed / DT), 0)
        C[:, k], D[:, k] = c, d
        s = s + ec * c * DT - d * DT / ed
    return C, D


def train_timer(M: dict, var: str, cfg: dict) -> tuple[list[int], list[int]]:
    ec = ed = np.sqrt(cfg["eta"])
    n_c = int(np.ceil(cfg["E"] / (ec * cfg["P"] * DT)))   # slots to fill
    n_d = int(np.ceil(cfg["E"] * ed / (cfg["P"] * DT)))   # slots to empty
    tr = M["year"] <= 2021
    mean = M[var][tr].mean(0)
    clock = M["clock"][tr][0]
    order = np.argsort(mean, kind="stable")
    charge = sorted(int(c) for c in clock[order[:n_c]])
    discharge = sorted(int(c) for c in clock[order[::-1][:n_d]])
    if max(charge) > min(discharge):
        raise ValueError("timer would discharge before charging")
    return charge, discharge


# ------------------------------------------------------------------ evaluation
def mef_matrix(M: dict, tag: str = "") -> np.ndarray:
    t = pd.read_csv(RES / f"table3_mef{tag}.csv")
    mp = t.set_index(["year", "block"]).mef.to_dict()
    blocks = (M["clock"] // 2 // 4 * 4).astype(int)
    return np.array([[mp[(y, b)] for b in row] for y, row in zip(M["year"], blocks)])


def daily(M: dict, C: np.ndarray, D: np.ndarray, mef: np.ndarray, mef_imp: np.ndarray,
          price: str) -> pd.DataFrame:
    """Per-day outcomes. Grid units: MW, MWh, GBP and kgCO2 (g/kWh = kg/MWh).
    Home units: kW, kWh, pence and gCO2."""
    net = (C - D) * DT  # energy drawn from the grid (negative when supplying)
    hod = M["clock"] // 2
    out = pd.DataFrame({
        "year": M["year"],
        "e_in": (C * DT).sum(1), "e_out": (D * DT).sum(1),
        "value": -(M[price] * net).sum(1),          # arbitrage revenue or bill saving
        "dco2_avg": (M["ci_actual"] * net).sum(1),  # + = more emissions
        "dco2_mef": (mef * net).sum(1),
        "dco2_mef_imp": (mef_imp * net).sum(1),
        "ci_in": (M["ci_actual"] * C).sum(1), "ci_out": (M["ci_actual"] * D).sum(1),
        "mef_in": (mef * C).sum(1), "mef_out": (mef * D).sum(1),
        "p_in": (M[price] * C).sum(1), "p_out": (M[price] * D).sum(1),
        "c_sum": C.sum(1), "d_sum": D.sum(1),
        "c_midday": (C * ((hod >= 9) & (hod < 17))).sum(1),
        "c_night": (C * (hod < 7)).sum(1),
        "d_evening": (D * ((hod >= 16) & (hod < 20))).sum(1),
        "both": ((C > 1e-6) & (D > 1e-6)).sum(1),  # simultaneous charge and discharge
    })
    return out


def _ratio_ci(num: np.ndarray, den: np.ndarray, B: int = 2000):
    idx = RNG.integers(0, len(num), size=(B, len(num)))
    bs = num[idx].mean(1) / den[idx].mean(1)
    return num.mean() / den.mean(), *np.percentile(bs, [2.5, 97.5])


def summarise(runs: dict[str, pd.DataFrame], cfg: dict, scale: float, label: str) -> pd.DataFrame:
    """Annual results. `scale` converts daily value/emissions to the reported unit per
    year per unit of power (grid: GBP/MW/yr and tCO2/MW/yr; home: GBP/yr and kgCO2/yr)."""
    rows = []
    years = sorted(set(runs["price"].year))
    for y in years:
        r = {"case": label, "year": y}
        for k, d in runs.items():
            g = d[d.year == y]
            r["n_days"] = len(g)
            r[f"{k}_value"] = g.value.mean() * 365 * scale["value"]
            r[f"{k}_cycles"] = g.e_out.sum() / (cfg["E"] * np.sqrt(cfg["eta"])) / len(g) * 365
            for m in ["avg", "mef", "mef_imp"]:
                r[f"{k}_dco2_{m}"] = g[f"dco2_{m}"].mean() * 365 * scale["co2"]
                r[f"{k}_dco2_{m}_per_out"] = g[f"dco2_{m}"].sum() / g.e_out.sum()  # g/kWh
            r[f"{k}_ci_in"] = g.ci_in.sum() / g.c_sum.sum()
            r[f"{k}_ci_out"] = g.ci_out.sum() / g.d_sum.sum()
            r[f"{k}_mef_in"] = g.mef_in.sum() / g.c_sum.sum()
            r[f"{k}_mef_out"] = g.mef_out.sum() / g.d_sum.sum()
            r[f"{k}_p_in"] = g.p_in.sum() / g.c_sum.sum()
            r[f"{k}_p_out"] = g.p_out.sum() / g.d_sum.sum()
            r[f"{k}_midday_charge_pct"] = g.c_midday.sum() / g.c_sum.sum() * 100
            r[f"{k}_night_charge_pct"] = g.c_night.sum() / g.c_sum.sum() * 100
            r[f"{k}_evening_discharge_pct"] = g.d_evening.sum() / g.d_sum.sum() * 100
            r[f"{k}_simultaneous_slots"] = int(g.both.sum())
        p, c = runs["price"][runs["price"].year == y], runs["carbon_fc"][runs["carbon_fc"].year == y]
        r["value_share_carbon_fc_pct"] = c.value.mean() / p.value.mean() * 100
        r["co2cut_share_price_pct"] = p.dco2_avg.mean() / c.dco2_avg.mean() * 100
        # implied abatement cost of carbon-driven over price-driven operation
        # value units -> GBP; emissions -> tonnes
        dval = (p.value - c.value).to_numpy() * scale["gbp"]
        dco2 = (p.dco2_avg - c.dco2_avg).to_numpy() * scale["t"]
        r["abate_gbp_t"], r["abate_lo"], r["abate_hi"] = _ratio_ci(dval, dco2)
        if "timer" in runs:
            t = runs["timer"][runs["timer"].year == y]
            r["value_share_timer_pct"] = t.value.mean() / p.value.mean() * 100
        rows.append(r)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------- figure
def fig_storage(tab: pd.DataFrame) -> None:
    g = tab[tab.case == "grid"]
    fig, axes = plt.subplots(1, 3, figsize=(15, 3.9))
    ax = axes[0]
    for k, c, lab in [("price", "#d62728", "Price-driven (optimised)"),
                      ("carbon_fc", "#1f77b4", "Carbon-driven (forecast)"),
                      ("timer", "#7f7f7f", "Fixed timer (frozen 2019-21)")]:
        ax.plot(g.year, g[f"{k}_value"] / 1000, marker="o", color=c, label=lab)
    ax.set(title="(a) Wholesale arbitrage revenue", xlabel="Year", ylabel="£ thousand per MW per year")
    ax.axvspan(2018.6, 2021.5, color="#eeeeee", zorder=0)
    ax.legend(frameon=False, fontsize=8)
    ax = axes[1]
    for k, c, lab in [("avg", "#1f77b4", "Average intensity"),
                      ("mef", "#ff7f0e", "Marginal factor"),
                      ("mef_imp", "#ff7f0e", "Marginal factor, imports charged")]:
        ax.plot(g.year, g[f"price_dco2_{k}_per_out"], marker="o" if k != "mef_imp" else "s",
                color=c, ls="-" if k != "mef_imp" else "--", label=lab)
    ax.axhline(0, color="k", lw=0.6)
    ax.set(title="(b) Emissions change, price-driven battery", xlabel="Year",
           ylabel="kgCO$_2$ per MWh discharged (− = cut)")
    ax.legend(frameon=False, fontsize=8)
    ax = axes[2]
    ax.plot(g.year, g.price_ci_in, marker="o", color="#2ca02c", label="When charging")
    ax.plot(g.year, g.price_ci_out, marker="o", color="#8c564b", label="When discharging")
    ax.plot(g.year, g.price_ci_out * GRID["eta"], color="#8c564b", ls=":", lw=1,
            label="Discharging × efficiency (break-even)")
    ax.set(title="(c) Average intensity at charge and discharge", xlabel="Year",
           ylabel="gCO$_2$/kWh")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(FIG / "fig8_storage.png", dpi=200)
    plt.close(fig)


# ------------------------------------------------------------------------ main
def main() -> None:
    df = build()
    M = matrices(df, 0, 24)  # common sample: carbon, forecast, MID and Agile all present
    mef, mef_imp = mef_matrix(M), mef_matrix(M, "_with_imports")
    grid_scale = {"value": 1 / GRID["P"], "co2": 1 / 1000 / GRID["P"], "gbp": 1, "t": 1 / 1000}
    home_scale = {"value": 1 / 100, "co2": 1 / 1000, "gbp": 1 / 100, "t": 1 / 1e6}

    # ---- grid-scale battery on wholesale prices (main case)
    runs = {}
    for k, sig in [("price", M["mid"]), ("carbon_fc", M["ci_forecast"]),
                   ("carbon_oracle", M["ci_actual"])]:
        C, D = optimise(sig, GRID)
        runs[k] = daily(M, C, D, mef, mef_imp, "mid")
    t_ch, t_dis = train_timer(M, "mid", GRID)
    runs["timer"] = daily(M, *timer(M, GRID, t_ch, t_dis), mef, mef_imp, "mid")
    tab_grid = summarise(runs, GRID, grid_scale, "grid")

    # ---- sensitivity (price-driven and carbon-driven only)
    sens = []
    for name, cfg in SENS.items():
        rs = {}
        for k, sig in [("price", M["mid"]), ("carbon_fc", M["ci_forecast"])]:
            rs[k] = daily(M, *optimise(sig, cfg), mef, mef_imp, "mid")
        s = summarise(rs, cfg, grid_scale, name)
        sens.append(s)
    tab_sens = pd.concat(sens, ignore_index=True)

    # ---- home battery on Agile (London)
    hr = {}
    for k, sig in [("price", M["agile"]), ("carbon_fc", M["ci_forecast"])]:
        hr[k] = daily(M, *optimise(sig, HOME), mef, mef_imp, "agile")
    tab_home = summarise(hr, HOME, home_scale, "home")

    fig_storage(tab_grid)
    tab_grid.round(3).to_csv(RES / "table10_storage_grid.csv", index=False)
    tab_sens.round(3).to_csv(RES / "table11_storage_sensitivity.csv", index=False)
    tab_home.round(3).to_csv(RES / "table12_storage_home.csv", index=False)
    summary = {
        "config_grid": GRID, "config_home": HOME, "sensitivity": SENS,
        "timer_charge_local": [f"{c // 2:02d}:{(c % 2) * 30:02d}" for c in t_ch],
        "timer_discharge_local": [f"{c // 2:02d}:{(c % 2) * 30:02d}" for c in t_dis],
        "n_days": tab_grid[["year", "n_days"]].to_dict("records"),
        "grid": tab_grid.round(2).to_dict("records"),
        "home": tab_home.round(2).to_dict("records"),
    }
    (RES / "storage_summary.json").write_text(json.dumps(summary, indent=1, default=str))

    pd.set_option("display.width", 250)
    cols = ["year", "n_days", "price_value", "carbon_fc_value", "timer_value", "price_cycles",
            "price_dco2_avg_per_out", "carbon_fc_dco2_avg_per_out", "price_dco2_mef_per_out",
            "price_dco2_mef_imp_per_out", "price_ci_in", "price_ci_out", "price_mef_in",
            "price_mef_out", "price_midday_charge_pct", "price_night_charge_pct",
            "value_share_carbon_fc_pct", "co2cut_share_price_pct", "abate_gbp_t", "abate_lo",
            "abate_hi", "value_share_timer_pct", "timer_dco2_avg_per_out",
            "price_simultaneous_slots"]
    print(tab_grid[cols].round(1).T.to_string())
    print("timer", summary["timer_charge_local"], summary["timer_discharge_local"])
    print(tab_sens[["case", "year", "price_value", "price_dco2_avg_per_out", "price_dco2_mef_per_out",
                    "carbon_fc_dco2_avg_per_out", "value_share_carbon_fc_pct", "co2cut_share_price_pct",
                    "abate_gbp_t"]].round(1).to_string())
    print(tab_home[["year", "price_value", "carbon_fc_value", "price_dco2_avg", "carbon_fc_dco2_avg",
                    "price_dco2_avg_per_out", "carbon_fc_dco2_avg_per_out", "price_dco2_mef_per_out",
                    "price_p_in", "price_p_out", "price_midday_charge_pct", "price_evening_discharge_pct",
                    "price_cycles", "value_share_carbon_fc_pct", "co2cut_share_price_pct",
                    "abate_gbp_t"]].round(1).T.to_string())


if __name__ == "__main__":
    main()
