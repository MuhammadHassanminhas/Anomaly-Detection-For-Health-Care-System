# Run report — c808685b-c0d1-42a6-822e-9a2bebfece0b

Started: 2026-07-20T09:18:51.711544+00:00
Finished: 2026-07-20T09:38:23.568677+00:00

## Per-check results

| Check | Practice | Status | Strategy | Duration (ms) | Rows examined | Pass | Fail | Indeterminate | Watermark span | Findings (new/reseen/reopened/resolved) | Narratives (composed/cached/blocked/fallback) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| appointment-invalid-status-code | practice-live | ok | bounded_full_scan | 2440 | 620854 | 3946 | 616908 | 0 | 2026-06-20T09:18:51.711544+00:00 → 2026-07-20T09:18:51.711544+00:00 | 0/616908/0/0 | 0/0/0/0 |
| demo-invoice-negative-total-amount-1784532052 | demo-practice-B-1784532052 | ok | bounded_full_scan | 233 | 16442 | 8522 | 7565 | 355 | 2026-06-20T09:18:51.711544+00:00 → 2026-07-20T09:18:51.711544+00:00 | 7565/0/0/0 | 2/7560/3/0 |
| demo-invoice-negative-total-amount-1784532364 | demo-practice-B-1784532364 | ok | bounded_full_scan | 122 | 16442 | 8522 | 7565 | 355 | 2026-06-20T09:18:51.711544+00:00 → 2026-07-20T09:18:51.711544+00:00 | 7565/0/0/0 | 2/7563/0/0 |
| demo-invoice-negative-total-amount-1784533739 | demo-practice-B-1784533739 | ok | bounded_full_scan | 159 | 16442 | 8522 | 7565 | 355 | 2026-06-20T09:18:51.711544+00:00 → 2026-07-20T09:18:51.711544+00:00 | 7565/0/0/0 | 2/7562/1/0 |
| demo-invoice-negative-total-amount-1784534222 | demo-practice-B-1784534222 | ok | bounded_full_scan | 177 | 16442 | 8522 | 7565 | 355 | 2026-06-20T09:18:51.711544+00:00 → 2026-07-20T09:18:51.711544+00:00 | 7565/0/0/0 | 2/7563/0/0 |
| demo-invoice-negative-total-amount-1784538418 | demo-practice-B-1784538418 | ok | bounded_full_scan | 176 | 16442 | 8522 | 7565 | 355 | 2026-06-20T09:18:51.711544+00:00 → 2026-07-20T09:18:51.711544+00:00 | 7565/0/0/0 | 2/7563/0/0 |
| invoice-negative-total-amount | practice-live | ok | bounded_full_scan | 116 | 16442 | 8522 | 7565 | 355 | 2026-06-20T09:18:51.711544+00:00 → 2026-07-20T09:18:51.711544+00:00 | 0/7564/1/0 | 0/0/0/0 |
| invoice-stale-unpaid-balance | practice-live | ok | bounded_full_scan | 231 | 16442 | 303 | 15817 | 322 | 2026-06-20T09:18:51.711544+00:00 → 2026-07-20T09:18:51.711544+00:00 | 0/15814/3/0 | 0/0/0/0 |
| patient-active-missing-nhi | practice-live | ok | bounded_full_scan | 339 | 30746 | 22746 | 8000 | 0 | 2026-06-20T09:18:51.711544+00:00 → 2026-07-20T09:18:51.711544+00:00 | 0/8000/0/0 | 0/0/0/0 |

## Fallback-strategy executions (unwatermarkable views)

- appointment-invalid-status-code (practice-live): bounded_full_scan fallback
- demo-invoice-negative-total-amount-1784532052 (demo-practice-B-1784532052): bounded_full_scan fallback
- demo-invoice-negative-total-amount-1784532364 (demo-practice-B-1784532364): bounded_full_scan fallback
- demo-invoice-negative-total-amount-1784533739 (demo-practice-B-1784533739): bounded_full_scan fallback
- demo-invoice-negative-total-amount-1784534222 (demo-practice-B-1784534222): bounded_full_scan fallback
- demo-invoice-negative-total-amount-1784538418 (demo-practice-B-1784538418): bounded_full_scan fallback
- invoice-negative-total-amount (practice-live): bounded_full_scan fallback
- invoice-stale-unpaid-balance (practice-live): bounded_full_scan fallback
- patient-active-missing-nhi (practice-live): bounded_full_scan fallback

## Top-cost checks (by duration)

- appointment-invalid-status-code (practice-live): 2440 ms, 620854 rows
- patient-active-missing-nhi (practice-live): 339 ms, 30746 rows
- demo-invoice-negative-total-amount-1784532052 (demo-practice-B-1784532052): 233 ms, 16442 rows
- invoice-stale-unpaid-balance (practice-live): 231 ms, 16442 rows
- demo-invoice-negative-total-amount-1784534222 (demo-practice-B-1784534222): 177 ms, 16442 rows

## Narration

- 10 composed, 37811 cached, 4 blocked (validator-rejected), 0 fallback (LLM outage)

## Reason-Code Distribution (all-time)

| Check | genuine_issue | not_genuine |
|---|---|---|
| demo-invoice-negative-total-amount-1784532052 | 16 | 14 |
| demo-invoice-negative-total-amount-1784532364 | 16 | 14 |
| demo-invoice-negative-total-amount-1784533739 | 16 | 14 |
| demo-invoice-negative-total-amount-1784534222 | 16 | 14 |
| demo-invoice-negative-total-amount-1784538418 | 16 | 14 |

## ASK recommendations

(none)
