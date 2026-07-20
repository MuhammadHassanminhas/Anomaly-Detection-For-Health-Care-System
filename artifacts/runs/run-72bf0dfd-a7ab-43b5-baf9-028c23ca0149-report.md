# Run report — 72bf0dfd-a7ab-43b5-baf9-028c23ca0149

Started: 2026-07-20T06:37:15.210079+00:00
Finished: 2026-07-20T06:37:34.993440+00:00

## Per-check results

| Check | Practice | Status | Strategy | Duration (ms) | Rows examined | Pass | Fail | Indeterminate | Watermark span | Findings (new/reseen/reopened/resolved) | Narratives (composed/cached/blocked/fallback) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| invoice-negative-total-amount | practice-live | ok | bounded_full_scan | 243 | 16442 | 8522 | 7565 | 355 | 2026-06-20T06:37:15.210079+00:00 → 2026-07-20T06:37:15.210079+00:00 | 7565/0/0/0 | 0/0/0/7565 |

## Fallback-strategy executions (unwatermarkable views)

- invoice-negative-total-amount (practice-live): bounded_full_scan fallback

## Top-cost checks (by duration)

- invoice-negative-total-amount (practice-live): 243 ms, 16442 rows

## Narration

- 0 composed, 0 cached, 0 blocked (validator-rejected), 7565 fallback (LLM outage)

## Reason-Code Distribution (all-time)

(no reason-coded dismissals recorded yet)

## ASK recommendations

(none)
