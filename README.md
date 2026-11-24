# When is the cleanest, and the cheapest, time to use electricity?

Carbon intensity, prices and load shifting on the GB grid, 2019–2025.
Working paper and fully reproducible code. Author: Mingyuan Xu (UCL).

## Key findings
- GB average carbon intensity fell 40% (214 → 129 gCO₂/kWh, 2019 → 2025), but the daily max–min spread stayed at about 90–100 g, so timing matters relatively more (spread/mean 48% → 73%).
- In 2025 midday was on average as clean as the night for the first time (midday/night ratio 0.99), and wholesale power at midday was within 3% of the night price (22% in 2019).
- For fully flexible demand, a forecast-based schedule cut intensity by 21% (2019) and 31% (2025) compared with flat use. A fixed 01:00–03:30 rule frozen on 2019–21 data captured only 40% of that by 2025 (out of sample).
- Cheap and clean mostly coincide: choosing the cheapest wholesale slots captured 61–75% of the carbon saving while cutting wholesale energy cost by 29–46%.
- On the Agile dynamic tariff, an illustrative EV charging 7 kWh a day saved about £198 in 2025 by charging in the cheapest hour (£198–£229 across the 14 GB regions). On a flat tariff the saving is zero.
- Going beyond what prices reward is expensive: the implied abatement cost of the carbon-aware schedule over the cheapest one is £277–£1,787 per tCO₂, far above the carbon price.
- A marginal-emissions model (with or without imports) still favours the night, matching Mehlig et al. (2022): the "best time" depends on how emissions are counted.

## Reproduce
```bash
pip install -r requirements.txt
python src/fetch_data.py      # NESO Carbon Intensity API + Elexon FUELHH (~3 min)
python src/fetch_prices.py    # Elexon MID wholesale prices + Octopus Agile unit rates (~2 min)
cd src && python analysis.py && python cost_analysis.py   # all tables (results/) and figures (paper/)
cd ../paper && python build.py working_paper.md           # optional: rebuild DOCX/PDF (needs pandoc + LibreOffice)
```

## Layout
- `src/fetch_data.py`, `src/fetch_prices.py` – data download (public APIs, no keys)
- `src/analysis.py` – carbon trends, regressions, marginal emission factors, load-shifting experiment
- `src/cost_analysis.py` – price trends, cost savings, carbon–cost alignment, implied abatement cost, household (Agile) view
- `results/` – tables and `summary.json` / `cost_summary.json`
- `paper/` – working paper (Markdown / DOCX / PDF), Chinese summary and figures

Data: NESO Carbon Intensity API, Elexon Insights (FUELHH, MID) and the Octopus Energy public API. Code: MIT licence.
