# Buy-the-dip portfolio trade JSON

## Confirm+trail top combos (19 Sep 2026)

Top 3 combos by return in each mode (Mode A all-in $1000, Mode B 3-slot $1500).

| File | Mode | Combo | Rank | Trades | Return % |
|---|---|---|---:|---:|---:|
| `confirm_trail_allin_d5_w240.json` | allin | `d5_w240` | 1 | 51 | -11.20 |
| `confirm_trail_allin_d5_w300.json` | allin | `d5_w300` | 2 | 53 | -22.64 |
| `confirm_trail_allin_d4_w120.json` | allin | `d4_w120` | 3 | 50 | -23.96 |
| `confirm_trail_3slot_d5_w30.json` | 3slot | `d5_w30` | 1 | 157 | -5.08 |
| `confirm_trail_3slot_d4_w60.json` | 3slot | `d4_w60` | 2 | 158 | -12.17 |
| `confirm_trail_3slot_d4_w180.json` | 3slot | `d4_w180` | 3 | 152 | -14.89 |

## Per-stock best levers (19 Sep 2026)

Baseline buy-the-dip (no confirm+trail). Each symbol uses its solo-best combo from the d4/d5 wait sweep.

| File | Mode | Trades | End equity | Return % |
|---|---|---:|---:|---:|
| `perstock_best_allin.json` | allin $1000 | 49 | $883.52 | -11.65 |
| `perstock_best_3slot.json` | 3slot $1500 | 131 | $1365.25 | -8.98 |

Lever map also in `../perstock_best_levers.json`.
