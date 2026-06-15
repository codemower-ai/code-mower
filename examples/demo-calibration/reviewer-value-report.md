# Code Mower Reviewer Value Report

Corpus: `demo-two-pr-calibration`
Items: 2
Adjudicated evidence: 3
Finding evidence: 0
Run dispositions: 3
Reviewer runs: 6

| Reviewer | Runs | Useful | Negative | Useful rate | Known-clean pass | Known-blocked caught/missed | Infra errors | Input gaps | Cost | Sec/run | Cost/useful | Policy | Recommended role |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `claude-audit` | 2 | 1 | 0 | 1.0 | 1 | 1/0 | 0 | 0 | 0.0 | 99.5 |  | `informational` | `informational` |
| `codex-audit` | 2 | 1 | 0 | 1.0 | 1 | 1/0 | 0 | 0 | 0.0 | 68.5 |  | `informational` | `informational` |
| `experimental-lens` | 2 | 0 | 1 | 0.0 | 0 | 0/1 | 0 | 0 | 0.0 | 52.0 |  | `informational` | `informational` |

## Recommendations
- experimental-lens: low useful-rate; keep informational until prompt or context improves.
- experimental-lens: missed known-blocked calibration runs; keep informational until catch rate improves.

## Policy Reasons
- `claude-audit`: `informational` / `informational` / `manual_or_calibration_only` - needs at least 10 adjudicated findings; needs at least 2 known-clean zero-blocker runs
- `codex-audit`: `informational` / `informational` / `manual_or_calibration_only` - needs at least 10 adjudicated findings; needs at least 2 known-clean zero-blocker runs
- `experimental-lens`: `informational` / `informational` / `manual_or_calibration_only` - needs at least 10 adjudicated findings; needs at least 2 known-clean zero-blocker runs; has known-clean blocking false positives; missed known-blocked calibration runs; useful-rate below selective-trigger threshold

_Caveat: This is a policy recommendation from calibration evidence, not an automatic repository merge-rule change._
