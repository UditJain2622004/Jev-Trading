# US stocks geometric long grid — first test

**When:** 19 Sep 2026  
**Script:** `stocks/scan_grid.py`  
**Data:** Yahoo 5-minute regular-hours bars, last 60 days, cached in `data/stocks/klines/`  
**Clip:** $1000 total notional split evenly across grid levels  
**Fees:** 0.3% on the buy and 0.3% on the sell (0.60% round trip)  
**Names:** INTC, MU, MRVL, AMD, SNDK, HPE, DELL, ARM, IBM, TXN, QCOM, AVGO, ORCL  
**Bars:** 4,680 five-minute bars per name (~60 calendar days, ~42 sessions)

Port of the Binance geometric long grid (`src/jev_trading/binance/grid.py`) to `stocks.bars.Bar`. Paper / backtest only.

---

## What it was

Fixed reference = first bar close. Buy levels sit geometrically below that ref. Each filled buy arms a sell one step above the fill. Same-bar rule: buys on low first, then sells on high. Optional stop liquidates open lots at close and halts the combo.

Wider steps than the crypto 1m grids (0.8–2.0%) because stock 5m bars plus overnight gaps need room above the 0.60% fee.

---

## Lever points (6 locked combos)

| Combo | Step | Levels | Stop |
|---|---:|---:|---|
| grid_0.8pct_8lvl | 0.8% | 8 | — |
| grid_1.0pct_8lvl | 1.0% | 8 | — |
| grid_1.5pct_8lvl | 1.5% | 8 | — |
| grid_2.0pct_6lvl | 2.0% | 6 | — |
| grid_1.0pct_10lvl | 1.0% | 10 | — |
| grid_1.5pct_10lvl_stop10 | 1.5% | 10 | 10% below lowest buy |

---

## Results summary

78 rows (13 stocks × 6 combos). 77 had at least 5 round-trips. **28** of those were net positive. **8** combos hit the optional stop and halted.

Wider steps (1.5–2.0%) beat tight 0.8% grids in this window: more of the step survives after 0.60% fees. Names that finished the window near or above the starting ref (AMD, ORCL, HPE, DELL, AVGO) kept inventory mark-to-market from dragging net; names that drifted down (ARM, INTC, SNDK, MU, QCOM) paid for leftover lots.

This is **one 60-day window**. AMD’s clean finish with zero open inventory is a stretch, not a law.

Raw table: `data/stocks/grid/results.csv`.

---

## Results details

### Top 5 by net_usd

| Stock | Combo | Round-trips | Net |
|---|---|---:|---:|
| AMD | grid_2.0pct_6lvl | 67 | +$156 |
| AMD | grid_1.5pct_8lvl | 127 | +$142 |
| AMD | grid_1.5pct_10lvl_stop10 | 146 | +$131 |
| AMD | grid_1.0pct_8lvl | 188 | +$93 |
| ORCL | grid_2.0pct_6lvl | 43 | +$89 |

### Worst rows

| Stock | Combo | Round-trips | Net |
|---|---|---:|---:|
| ARM | grid_0.8pct_8lvl | 49 | −$215 |
| ARM | grid_1.0pct_8lvl | 42 | −$199 |
| INTC | grid_0.8pct_8lvl | 26 | −$196 |
| ARM | grid_1.0pct_10lvl | 67 | −$185 |
| INTC | grid_1.0pct_8lvl | 33 | −$179 |

### Best combo per stock

| Stock | Best combo | Round-trips | Net |
|---|---|---:|---:|
| AMD | grid_2.0pct_6lvl | 67 | +$156 |
| ORCL | grid_2.0pct_6lvl | 43 | +$89 |
| HPE | grid_2.0pct_6lvl | 27 | +$63 |
| DELL | grid_2.0pct_6lvl | 24 | +$56 |
| AVGO | grid_2.0pct_6lvl | 28 | +$45 |
| IBM | grid_2.0pct_6lvl | 16 | −$28 |
| TXN | grid_1.5pct_10lvl_stop10 | 62 | −$31 |
| MRVL | grid_2.0pct_6lvl | 19 | −$45 |
| MU | grid_2.0pct_6lvl | 14 | −$101 |
| QCOM | grid_2.0pct_6lvl | 6 | −$107 |
| SNDK | grid_2.0pct_6lvl | 20 | −$109 |
| ARM | grid_1.5pct_10lvl_stop10 | 43 | −$130 (stopped) |
| INTC | grid_1.5pct_10lvl_stop10 | 37 | −$131 (stopped) |

### How to read it

- **AMD / ORCL / HPE / DELL / AVGO:** enough oscillation to farm steps, and price did not leave a big underwater inventory at the end of the window.
- **ARM / INTC / SNDK / MU / QCOM:** buys filled into a downtrend; round-trip gross could not cover fees plus mark-to-market on leftover lots. Tight 0.8% steps were the worst.
- Across the book, **grid_2.0pct_6lvl** was the most consistent best lever. **grid_0.8pct_8lvl** traded a lot and lost after fees.

Rerun without downloading again: `python -u stocks/scan_grid.py` (use the project `.venv`).
