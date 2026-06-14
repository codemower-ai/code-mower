# Code Mower Reviewer Value Report

Corpus: `small-known-pr-pilot`
Items: 5
Adjudicated evidence: 0
Finding evidence: 0
Run dispositions: 0
Reviewer runs: 1

| Reviewer | Runs | Useful | Negative | Useful rate | Known-clean pass | Known-blocked caught/missed | Infra errors | Input gaps | Cost | Sec/run | Cost/useful | Policy | Recommended role |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `codex-audit` | 1 | 0 | 0 |  | 1 | 0/0 | 0 | 0 | 0.0 | 120.0 |  | `informational` | `informational` |

## Recommendations
- codex-audit: collect human dispositions before comparing reviewer accuracy.

## Policy Reasons
- `codex-audit`: `informational` / `informational` / `manual_or_calibration_only` - needs at least 10 adjudicated findings; needs at least 2 known-clean zero-blocker runs; useful-rate below selective-trigger threshold

_Caveat: This is a policy recommendation from calibration evidence, not an automatic repository merge-rule change._
