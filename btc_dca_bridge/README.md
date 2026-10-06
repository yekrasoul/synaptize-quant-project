# BTC Adaptive DCA data bridge

This repository supports BTC Adaptive DCA V1 with machine-readable market-data collection and reconciled deployment history.

## Market-data policy

The collector may use a reliable BTC **spot** market source. Current price and rolling 7-day high must come from the **same** spot source in each run. Derivatives, mark prices, index prices, and mixed-exchange calculations are not valid inputs.

## Reconciled deployment history

Source of truth for execution history: Project conversation **"Chat 03 — Portfolio & Budget Tracker"**.

Only confirmed executed purchases are recorded below. Later corrections/replacements supersede earlier intended or daily purchases.

### September 2026

| Date | Executed USD | BTC reference price | Reconciliation note |
|---|---:|---:|---|
| 08 Sep 2026 | $20 | 79,395 | Confirmed executed purchase; no purchase on the originally referenced day. |
| 10 Sep 2026 | $20 | 76,843 | Confirmed executed purchase. |
| 12 Sep 2026 | $10 | 77,305 | Confirmed executed purchase. |
| 15 Sep 2026 | $20 | 76,933 | Replaces no-purchase days 13–14 Sep. |
| 16 Sep 2026 | $10 | 77,025 | Confirmed executed purchase. |
| 18 Sep 2026 | $20 | 76,655 | Replaces 17–18 Sep daily buys. |
| 23 Sep 2026 | $50 | 84,444 | Replaces no-purchase days 19–23 Sep. |
| 24 Sep 2026 | $20 | 83,444 | Confirmed executed purchase. |
| 28 Sep 2026 | $30 | 82,895 | No purchases were executed on 25–27 Sep; this $30 purchase was executed on 28 Sep instead. |
| 30 Sep 2026 | $20 | 83,900 | Replaces the intended 29 Sep purchase. |

**September reconciled total from the confirmed numeric entries above: $220.**

A separate Chat 03 message referring to **6 Sep 2026** is visible only in truncated form in the currently available project context; its replacement purchase amount and price are not recoverable with confidence here. It is therefore **not fabricated or added** to the table above. If that complete message is later available, this history should be amended and the September total recomputed.

### October 2026

| Date | Executed USD | BTC reference price | Reconciliation note |
|---|---:|---:|---|
| 05 Oct 2026 | $60 | 86,164 | Confirmed executed purchase. |

**October spent through 05 Oct 2026: $60.**  
**October remaining against the $500 hard cap: $440.**

## Reconciliation rules

- Count only confirmed executed purchases.
- Never count recommendations or scheduled outputs as purchases.
- Later correction/replacement messages supersede earlier entries.
- Do not double-count replaced purchases.
- Preserve uncertainty explicitly rather than inventing missing dates, amounts, or prices.
