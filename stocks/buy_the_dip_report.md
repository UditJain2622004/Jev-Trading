# US stocks buy-the-dip — first test

**When:** 19 Sep 2026  
**Script:** `stocks/scan_buy_the_dip.py`  
**Data:** Yahoo 5-minute regular-hours bars, last 60 days, cached in `data/stocks/klines/`  
**Clip:** $1000 one long at a time  
**Fees:** 0.3% on the buy and 0.3% on the sell (0.60% round trip), including timeouts  
**Names:** INTC, MU, MRVL, AMD, SNDK, HPE, DELL, ARM, IBM, TXN, QCOM, AVGO, ORCL  
**Bars:** 4,680 five-minute bars per name (~60 calendar days, ~42 sessions)

No Jev. Code finds the dip, waits, buys, and exits.

---

## What it was

Catch a large drop, wait until price is still near that low, then buy and aim for a big bounce.

A **session** is 6.5 hours of US regular trading (390 minutes), not wall-clock. Overnight is just the gap into the next open.

The idea: 4–10% dumps do not happen every day on one name, but across 13 chip/tech names we get enough events to test.

---

## Buy logic

1. **Dip.** Look back N sessions. Find the high in that stretch. If the current close is at least X% below that high, and the high happened earlier in the stretch (it actually fell, it is not still printing the high), call it a dip.
2. **Wait.** Hold for 30–90 minutes. Price may swing during the wait.
3. **Still near the low.** At the end of the wait, the close must still be within the stable range of the dip close (1.2–2.5% depending on the combo). If it already bounced away, skip. If it kept falling and finished far below, skip.
4. **Buy** that last close.

Only one trade at a time per stock. Next setup is ignored until the current trade is done.

---

## Sell logic

- **Take profit:** close (or intra-bar high) reaches +TP%. Win = TP minus 0.60% fees.
- **Stop:** close (or intra-bar low) reaches −SL%. Loss = SL plus 0.60% fees.
- If the same 5-minute bar hits both, count it as a **stop**.
- **Timeout:** if neither hits inside the timeout (2–5 sessions), exit at that bar’s close, still pay 0.60%.

Breakeven resolved win rate is about **37–45%** depending on the TP/SL pair (fees are large, so you need the bigger targets to live).

---

## Lever points (10 locked combos)

Same ten on every stock. Windows are **trading time**.

| Combo | Dip | Dip window | End of wait still within | Wait | Take profit | Stop | Timeout |
|---|---:|---|---:|---:|---:|---:|---|
| dip4_1d | 4% | 1 session | 1.2% | 30m | 5% | 3% | 2 sessions |
| dip5_1d | 5% | 1 session | 1.5% | 30m | 6% | 3% | 2 sessions |
| dip5_2d | 5% | 2 sessions | 1.5% | 45m | 6% | 3.5% | 3 sessions |
| dip6_1d | 6% | 1 session | 1.5% | 30m | 6% | 3% | 2 sessions |
| dip6_2d | 6% | 2 sessions | 2.0% | 60m | 7% | 3.5% | 3 sessions |
| dip7_2d | 7% | 2 sessions | 2.0% | 45m | 7% | 4% | 3 sessions |
| dip8_2d | 8% | 2 sessions | 2.0% | 60m | 8% | 4% | 3 sessions |
| dip8_3d | 8% | 3 sessions | 2.5% | 90m | 8% | 4% | 4 sessions |
| dip4_2d | 4% | 2 sessions | 1.2% | 45m | 5% | 3% | 3 sessions |
| dip10_3d | 10% | 3 sessions | 2.5% | 60m | 10% | 5% | 5 sessions |

---

## Results summary

130 rows (13 stocks × 10 combos). 121 had at least 5 trades. **48** were net positive. **45** were at or above breakeven on resolved wins vs losses.

Bigger, slower dips (6–10% over 2–3 sessions) beat same-day 4–5% dips. Same-day 4–5% fired often and died in chop.

This is **one 60-day window**. DELL and HPE had a good stretch; that can be one bounce, not a law.

Raw table: `data/stocks/buy_the_dip/results.csv`.

---

## Results details

### Best rows (n ≥ 5)

| Stock | Combo | Trades | W / L / timeout | Resolved | Need | Net |
|---|---|---:|---|---:|---:|---:|
| DELL | dip6_2d | 22 | 13 / 8 / 1 | 62% | 39% | +$494 |
| DELL | dip10_3d | 8 | 5 / 2 / 1 | 71% | 37% | +$418 |
| DELL | dip8_3d | 17 | 9 / 7 / 1 | 56% | 38% | +$367 |
| DELL | dip7_2d | 20 | 11 / 8 / 1 | 58% | 42% | +$329 |
| HPE | dip10_3d | 8 | 3 / 2 / 3 | 60% | 37% | +$293 |
| HPE | dip5_2d | 22 | 12 / 8 / 2 | 60% | 43% | +$282 |
| DELL | dip5_1d | 31 | 15 / 15 / 1 | 50% | 40% | +$270 |
| MRVL | dip10_3d | 17 | 8 / 9 / 0 | 47% | 37% | +$248 |
| AMD | dip8_3d | 13 | 6 / 6 / 1 | 50% | 38% | +$203 |

### Worst rows

| Stock | Combo | Trades | W / L / timeout | Resolved | Net |
|---|---|---:|---|---:|---:|
| ARM | dip5_1d | 40 | 9 / 27 / 4 | 25% | −$469 |
| SNDK | dip5_2d | 49 | 16 / 32 / 1 | 33% | −$428 |
| INTC | dip4_1d | 36 | 9 / 24 / 3 | 27% | −$427 |
| MU | dip5_1d | 32 | 6 / 22 / 4 | 21% | −$412 |

ARM, SNDK, INTC, and MU got chopped on the frequent small-dip combos. Those names did dump a lot; they did not bounce cleanly in this window.

### Best combo per stock

| Stock | Best combo | Trades | Resolved | Net |
|---|---|---:|---:|---:|
| DELL | dip6_2d | 22 | 62% | +$494 |
| HPE | dip10_3d | 8 | 60% | +$293 |
| MRVL | dip10_3d | 17 | 47% | +$248 |
| AMD | dip8_3d | 13 | 50% | +$203 |
| AVGO | dip4_1d | 13 | 63% | +$146 |
| TXN | dip8_2d | 2 | 100% | +$89 |
| MU | dip10_3d | 17 | 38% | +$45 |
| IBM | dip5_2d | 7 | 50% | +$31 |
| INTC | dip8_2d | 20 | 37% | +$18 |
| QCOM | dip10_3d | 6 | 40% | +$16 |
| ARM | dip8_3d | 24 | 35% | −$75 |
| ORCL | dip10_3d | 7 | 20% | −$100 |
| SNDK | dip10_3d | 26 | 32% | −$155 |

TXN’s “best” is two trades. Ignore it.

### How to read it

- **DELL / HPE / AMD / MRVL:** fewer, larger dips, then a bounce. That is the shape this rule is meant to catch.
- **ARM / SNDK / INTC / ORCL:** lots of 4–5% dips that kept going or chopped. The wait-near-the-low filter did not save them.
- Across the book, **dip8_3d** and **dip10_3d** were the more interesting levers. **dip4_1d** and **dip5_1d** traded too much and lost.

Rerun without downloading again: `python -u stocks/scan_buy_the_dip.py`
