# Code Mower Reviewer Value Report

This checked-in snapshot comes from the richer reference calibration corpus plus
captured reviewer-run evidence used during Code Mower development. It is not
expected to be reproduced from the public starter file at
`templates/calibration-corpus.json`, which is intentionally small and sanitized
for OSS onboarding. Use the starter file to prove the command path in a new
repo; use your own corpus and run manifests to produce a product-specific value
report.

Corpus: `code-mower-known-pr-starter`
Items: 18
Adjudicated evidence: 70
Finding evidence: 70
Run dispositions: 0
Reviewer runs: 103

| Reviewer | Runs | Useful | Negative | Useful rate | Known-clean pass | Known-blocked caught/missed | Infra errors | Input gaps | Cost | Sec/run | Cost/useful | Policy | Recommended role |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `claude-audit` | 13 | 4 | 0 | 1.0 | 12 | 0/0 | 0 | 0 | 0.0 | 105.077 |  | `selective_trigger_candidate` | `selective_trigger` |
| `coderabbit-cli` | 9 | 5 | 0 | 1.0 | 4 | 0/1 | 1 | 0 | 0.0 | 145.218 |  | `informational` | `informational` |
| `coderabbit-hosted` | 8 | 1 | 0 | 1.0 | 8 | 0/0 | 0 | 0 | 0.0 | 0.0 |  | `selective_trigger_candidate` | `selective_trigger` |
| `codex-audit` | 15 | 27 | 0 | 1.0 | 12 | 0/0 | 0 | 0 | 0.0 | 66.067 |  | `merge_gate_candidate` | `merge_gate_eligible` |
| `gemini-base-audit` | 8 | 2 | 0 | 1.0 | 4 | 2/1 | 0 | 0 | 0.0 | 95.488 |  | `informational` | `informational` |
| `gemini-cli` | 8 | 1 | 0 | 1.0 | 6 | 2/0 | 0 | 0 | 0.0 | 112.204 |  | `selective_trigger_candidate` | `selective_trigger` |
| `gemini-context-driven-quality` | 3 | 0 | 0 |  | 1 | 1/1 | 0 | 0 | 0.0 | 98.794 |  | `informational` | `informational` |
| `gemini-generic-programming` | 3 | 0 | 0 |  | 1 | 1/1 | 0 | 0 | 0.0 | 108.289 |  | `informational` | `informational` |
| `gemini-operability` | 5 | 2 | 0 | 1.0 | 3 | 1/0 | 0 | 0 | 0.0 | 72.816 |  | `selective_trigger_candidate` | `selective_trigger` |
| `gemini-security-threat-model` | 5 | 2 | 0 | 1.0 | 2 | 1/0 | 1 | 0 | 0.0 | 66.841 |  | `informational` | `informational` |
| `gemma4-ollama` | 5 | 0 | 3 | 0.0 | 4 | 0/1 | 0 | 0 | 0.0 | 138.913 |  | `informational` | `informational` |
| `gitar` | 14 | 12 | 0 | 1.0 | 12 | 0/0 | 0 | 0 | 0.0 | 56.643 |  | `merge_gate_candidate` | `merge_gate_eligible` |
| `hermes-base-audit` | 4 | 0 | 0 |  | 0 | 1/0 | 0 | 1 | 0.0 | 147.583 |  | `informational` | `informational` |
| `hermes-context-driven-quality` | 4 | 0 | 0 |  | 1 | 0/1 | 0 | 1 | 0.0 | 99.786 |  | `informational` | `informational` |
| `hermes-generic-programming` | 4 | 0 | 0 |  | 0 | 0/0 | 1 | 1 | 0.0 | 107.886 |  | `informational` | `informational` |
| `qwen3-coder-next-lmstudio` | 7 | 2 | 9 | 0.1818 | 0 | 1/0 | 0 | 0 | 0.0 | 53.568 |  | `informational` | `informational` |

## Recommendations
- coderabbit-cli: missed known-blocked calibration runs; keep informational until catch rate improves.
- gemini-base-audit: missed known-blocked calibration runs; keep informational until catch rate improves.
- gemini-context-driven-quality: collect human dispositions before comparing reviewer accuracy.
- gemini-generic-programming: collect human dispositions before comparing reviewer accuracy.
- gemma4-ollama: low useful-rate; keep informational until prompt or context improves.
- gemma4-ollama: missed known-blocked calibration runs; keep informational until catch rate improves.
- hermes-base-audit: collect human dispositions before comparing reviewer accuracy.
- hermes-context-driven-quality: collect human dispositions before comparing reviewer accuracy.
- hermes-generic-programming: collect human dispositions before comparing reviewer accuracy.
- qwen3-coder-next-lmstudio: low useful-rate; keep informational until prompt or context improves.

## Policy Reasons
- `claude-audit`: `selective_trigger_candidate` / `selective_trigger` / `matching_review_class_only` - needs at least 10 adjudicated findings; suggested classes: backend-debug-upload, calibration-policy
- `coderabbit-cli`: `informational` / `informational` / `manual_or_calibration_only` - needs at least 10 adjudicated findings; missed known-blocked calibration runs; has infra/setup failures to stabilize before promotion
- `coderabbit-hosted`: `selective_trigger_candidate` / `selective_trigger` / `matching_review_class_only` - needs at least 10 adjudicated findings; suggested classes: web-debug-upload
- `codex-audit`: `merge_gate_candidate` / `merge_gate_eligible` / `repo_merge_bar_opt_in` - evidence meets current threshold heuristics; keep human review in the loop
- `gemini-base-audit`: `informational` / `informational` / `manual_or_calibration_only` - needs at least 10 adjudicated findings; missed known-blocked calibration runs
- `gemini-cli`: `selective_trigger_candidate` / `selective_trigger` / `matching_review_class_only` - needs at least 10 adjudicated findings; suggested classes: auth-history
- `gemini-context-driven-quality`: `informational` / `informational` / `manual_or_calibration_only` - needs at least 10 adjudicated findings; needs at least 2 known-clean zero-blocker runs; missed known-blocked calibration runs; useful-rate below selective-trigger threshold
- `gemini-generic-programming`: `informational` / `informational` / `manual_or_calibration_only` - needs at least 10 adjudicated findings; needs at least 2 known-clean zero-blocker runs; missed known-blocked calibration runs; useful-rate below selective-trigger threshold
- `gemini-operability`: `selective_trigger_candidate` / `selective_trigger` / `matching_review_class_only` - needs at least 10 adjudicated findings; suggested classes: auth-history
- `gemini-security-threat-model`: `informational` / `informational` / `manual_or_calibration_only` - needs at least 10 adjudicated findings; has infra/setup failures to stabilize before promotion
- `gemma4-ollama`: `informational` / `informational` / `manual_or_calibration_only` - needs at least 10 adjudicated findings; missed known-blocked calibration runs; useful-rate below selective-trigger threshold
- `gitar`: `merge_gate_candidate` / `merge_gate_eligible` / `repo_merge_bar_opt_in` - evidence meets current threshold heuristics; keep human review in the loop
- `hermes-base-audit`: `informational` / `informational` / `manual_or_calibration_only` - needs at least 10 adjudicated findings; needs at least 2 known-clean zero-blocker runs; has known-clean blocking false positives; needs richer audit input/context before promotion; useful-rate below selective-trigger threshold
- `hermes-context-driven-quality`: `informational` / `informational` / `manual_or_calibration_only` - needs at least 10 adjudicated findings; needs at least 2 known-clean zero-blocker runs; missed known-blocked calibration runs; needs richer audit input/context before promotion; useful-rate below selective-trigger threshold
- `hermes-generic-programming`: `informational` / `informational` / `manual_or_calibration_only` - needs at least 10 adjudicated findings; needs at least 2 known-clean zero-blocker runs; has infra/setup failures to stabilize before promotion; needs richer audit input/context before promotion; useful-rate below selective-trigger threshold
- `qwen3-coder-next-lmstudio`: `informational` / `informational` / `manual_or_calibration_only` - needs at least 2 known-clean zero-blocker runs; has known-clean blocking false positives; useful-rate below selective-trigger threshold

_Caveat: This is a policy recommendation from calibration evidence, not an automatic repository merge-rule change._
