# When is the cleanest time to use electricity?

Daily carbon-intensity patterns and carbon-aware load shifting on the GB grid, 2019–2025.
Working paper and fully reproducible code. Author: Mingyuan Xu (UCL).

## Key findings
- GB average carbon intensity fell 40% (214 → 129 gCO₂/kWh, 2019 → 2025), but the daily max–min spread stayed ~90–100 g, so timing matters relatively more (spread/mean 48% → 73%).
- In 2025 the cleanest hour on average was midday, not night, for the first time.
- For fully flexible demand, a forecast-based schedule cut intensity 21% (2019) → 31% (2025) vs flat use; a fixed 01:00–03:30 rule frozen on 2019–21 data captured only 40% of that by 2025 (out-of-sample).
- A marginal-emissions model (with or without imports) still favours the night, matching Mehlig et al. (2022): the "best time" depends on the accounting metric.

## Reproduce
```bash
pip install -r requirements.txt
python src/fetch_data.py   # public data: NESO Carbon Intensity API + Elexon FUELHH (~3 min)
python src/analysis.py     # all tables (results/) and figures (paper/)
```

## Layout
- `src/fetch_data.py` – data download
- `src/analysis.py` – trends, regressions, marginal emission factors, load-shifting experiment
- `results/` – tables and `summary.json`
- `paper/` – working paper (Markdown / DOCX / PDF) and figures

Data: NESO Carbon Intensity API and Elexon Insights (public). Code: MIT licence.
