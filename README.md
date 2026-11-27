# When is the cleanest, and the cheapest, time to use electricity?

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.24515213.svg)](https://doi.org/10.5281/zenodo.24515213)

Carbon intensity, prices, load shifting and storage on the GB grid, 2019–2025, with an out-of-period check on January–October 2026.
Working paper (version 1.0) and fully reproducible code. Author: Mingyuan Xu (UCL).

## Key findings
- GB average carbon intensity fell 40% (214 → 129 gCO₂/kWh, 2019 → 2025), but the daily max–min spread stayed at about 90–100 g, so timing matters relatively more (spread/mean 48% → 73%).
- In 2025 midday was on average as clean as the night for the first time (midday/night ratio 0.99), and wholesale power at midday was within 3% of the night price (22% in 2019).
- For fully flexible demand, a forecast-based schedule cut intensity by 21% (2019) and 31% (2025) compared with flat use. A fixed 01:00–03:30 rule frozen on 2019–21 data captured 61–63% of that saving in sample, but only 37–56% out of sample (2022–25; 40% in 2025).
- Cheap and clean mostly coincide: choosing the cheapest wholesale slots captured 61–75% of the carbon saving while cutting wholesale energy cost by 29–46%.
- On the Agile dynamic tariff, an illustrative EV charging 7 kWh a day saved about £198 in 2025 by charging in the cheapest hour (£198–£229 across the 14 GB regions). On a flat tariff the saving is zero.
- Going beyond what prices reward is expensive: the implied abatement cost of the carbon-aware schedule over the cheapest one is £277–£1,787 per tCO₂, far above the carbon price.
- A marginal-emissions model (with or without imports) still favours the night, matching Mehlig et al. (2022): the "best time" depends on how emissions are counted.
- Storage (new in v1.0): a 1 MW / 2 MWh battery trading on wholesale prices (perfect foresight, arbitrage only) earned £16k–£82k per MW a year and cut emissions in every year on average factors (20–47 kgCO₂ per MWh discharged), increasingly by charging at midday (13% → 42% of charging in 09:00–17:00). On marginal factors it added emissions in 2019–2022 and cut them from 2023. Carbon-driven operation cut more but gave up 36–67% of the revenue (£454–£2,633 per tCO₂), and a timer frozen on 2019–21 prices kept only 45–56% of the optimised revenue in 2022–25.
- Out-of-period check (new in v1.0), January–October 2026 vs January–October 2025, nothing re-fitted: midday was cleaner and cheaper than the night (midday/night ratio 0.81 for carbon, 0.85 for wholesale price). The frozen 01:00–03:30 rule captured 13% of the forecast-based saving (32% in Jan–Oct 2025) and the frozen battery timer 25% of the optimised revenue (42%).

## Reproduce
```bash
pip install -r requirements.txt
python src/fetch_data.py      # NESO Carbon Intensity API + Elexon FUELHH (~3 min)
python src/fetch_prices.py    # Elexon MID wholesale prices + Octopus Agile unit rates (~2 min)
python src/analysis.py        # carbon trends, marginal emission factors, load shifting
python src/cost_analysis.py   # prices, cost savings, carbon-cost trade-off, household view
python src/storage_analysis.py  # battery storage (under a minute on 4 cores)

# out-of-period check, January-October 2026
python src/fetch_data.py --start 2026-01-01 --end 2026-10-31 --suffix _2026 --datasets intensity mix
python src/fetch_prices.py --start 2026-01-01 --end 2026-11-01 --suffix _2026
python src/check_2026.py

cd paper && python build.py working_paper.md   # optional: rebuild DOCX/PDF (needs pandoc + LibreOffice)
```
Tables are written to `results/` and figures to `paper/`.

## Layout
- `src/fetch_data.py`, `src/fetch_prices.py` – data download (public APIs, no keys); `--start/--end/--suffix` fetch extra periods
- `src/analysis.py` – carbon trends, regressions, marginal emission factors, load-shifting experiment
- `src/cost_analysis.py` – price trends, cost savings, carbon–cost alignment, implied abatement cost, household (Agile) view
- `src/storage_analysis.py` – battery storage: a daily linear programme for price-driven, carbon-driven and fixed-timer operation; emissions on average and marginal factors; sensitivity; home battery
- `src/check_2026.py` – out-of-period check, January–October 2026 against January–October 2025
- `results/` – tables and `summary.json` / `cost_summary.json` / `storage_summary.json` / `check_2026.json`
- `paper/` – working paper (Markdown / DOCX / PDF), Chinese summary and figures

Data: NESO Carbon Intensity API, Elexon Insights (FUELHH, MID) and the Octopus Energy public API. Code: MIT licence.

## Cite
Xu, M. (2026). *When is the cleanest, and the cheapest, time to use electricity? Carbon intensity, prices, load shifting and storage on the GB grid, 2019–2025* (working paper, version 1.0). Zenodo. https://doi.org/10.5281/zenodo.24515213

The DOI above always points to the latest version; the earlier v0.3.0 is https://doi.org/10.5281/zenodo.24515214. GitHub's "Cite this repository" button uses `CITATION.cff`.
