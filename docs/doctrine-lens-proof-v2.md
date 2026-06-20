# Code Mower Provider vs Lens Effect Report

Corpus: `doctrine-lens-proof-v2`
Items: 4
Reviewer runs: 60

## Answer

- Mean absolute lens effective-catch delta: 0.33
- Mean provider effective-catch spread: 0.60
- Provider/lens effective-effect ratio: 1.80x
- Mean absolute lens evaluable-catch delta: 0.10
- Mean provider evaluable-catch spread: 0.10
- Provider/lens evaluable-effect ratio: 1.00x
- Comparison: `provider_effect_larger`
- Interpretation: Provider/runtime choice moved outcomes more than doctrine wording on the primary coverage-inclusive metric.

## Provider/Lens Cells

| Provider | Lens | Runs | Blocked caught/missed | Input gaps | Clean pass/false block | Effective catch | Evaluable catch | False-blocker rate | Sec/run |
| --- | --- | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: |
| `antigravity` | `base-audit` | 4 | 1/0 | 1 | 2/0 | 0.50 | 1.00 | 0.00 | 34.80 |
| `antigravity` | `context-driven-quality` | 4 | 1/0 | 0 | 2/0 | 0.50 | 1.00 | 0.00 | 197.30 |
| `antigravity` | `generic-programming` | 4 | 1/0 | 1 | 1/0 | 0.50 | 1.00 | 0.00 | 89.20 |
| `antigravity` | `operability` | 4 | 1/0 | 1 | 2/0 | 0.50 | 1.00 | 0.00 | 32.80 |
| `antigravity` | `security-threat-model` | 4 | 1/0 | 0 | 2/0 | 0.50 | 1.00 | 0.00 | 188.40 |
| `claude` | `base-audit` | 4 | 1/1 | 0 | 2/0 | 0.50 | 0.50 | 0.00 | 105.00 |
| `claude` | `context-driven-quality` | 4 | 2/0 | 0 | 2/0 | 1.00 | 1.00 | 0.00 | 105.00 |
| `claude` | `generic-programming` | 4 | 2/0 | 0 | 2/0 | 1.00 | 1.00 | 0.00 | 105.00 |
| `gemini` | `base-audit` | 4 | 1/0 | 1 | 2/0 | 0.50 | 1.00 | 0.00 | 89.18 |
| `gemini` | `context-driven-quality` | 4 | 2/0 | 0 | 2/0 | 1.00 | 1.00 | 0.00 | 89.80 |
| `gemini` | `generic-programming` | 4 | 2/0 | 0 | 1/0 | 1.00 | 1.00 | 0.00 | 133.64 |
| `gemini` | `operability` | 2 | 1/0 | 0 | 1/0 | 1.00 | 1.00 | 0.00 | 72.80 |
| `gemini` | `security-threat-model` | 2 | 1/0 | 0 | 1/0 | 1.00 | 1.00 | 0.00 | 66.80 |
| `hermes` | `base-audit` | 4 | 1/0 | 1 | 1/1 | 0.50 | 1.00 | 0.50 | 147.60 |
| `hermes` | `context-driven-quality` | 4 | 0/0 | 2 | 1/0 | 0.00 | - | 0.00 | 99.80 |
| `hermes` | `generic-programming` | 4 | 0/0 | 1 | 1/0 | 0.00 | - | 0.00 | 107.90 |

## Same-Provider Lens Lift

| Provider | Lens | Base effective | Lens effective | Effective delta | Evaluable delta | False-blocker delta |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `antigravity` | `context-driven-quality` | 0.50 | 0.50 | 0.00 | 0.00 | 0.00 |
| `antigravity` | `generic-programming` | 0.50 | 0.50 | 0.00 | 0.00 | 0.00 |
| `antigravity` | `operability` | 0.50 | 0.50 | 0.00 | 0.00 | 0.00 |
| `antigravity` | `security-threat-model` | 0.50 | 0.50 | 0.00 | 0.00 | 0.00 |
| `claude` | `context-driven-quality` | 0.50 | 1.00 | 0.50 | 0.50 | 0.00 |
| `claude` | `generic-programming` | 0.50 | 1.00 | 0.50 | 0.50 | 0.00 |
| `gemini` | `context-driven-quality` | 0.50 | 1.00 | 0.50 | 0.00 | 0.00 |
| `gemini` | `generic-programming` | 0.50 | 1.00 | 0.50 | 0.00 | 0.00 |
| `gemini` | `operability` | 0.50 | 1.00 | 0.50 | 0.00 | 0.00 |
| `gemini` | `security-threat-model` | 0.50 | 1.00 | 0.50 | 0.00 | 0.00 |
| `hermes` | `context-driven-quality` | 0.50 | 0.00 | -0.50 | - | -0.50 |
| `hermes` | `generic-programming` | 0.50 | 0.00 | -0.50 | - | -0.50 |

## Cross-Provider Spread

| Lens | Providers | Effective-catch spread | Evaluable-catch spread | False-blocker spread | Evaluable blocked runs |
| --- | --- | ---: | ---: | ---: | ---: |
| `base-audit` | `antigravity`, `claude`, `gemini`, `hermes` | 0.00 | 0.50 | 0.50 | 5 |
| `context-driven-quality` | `antigravity`, `claude`, `gemini`, `hermes` | 1.00 | 0.00 | 0.00 | 5 |
| `generic-programming` | `antigravity`, `claude`, `gemini`, `hermes` | 1.00 | 0.00 | 0.00 | 5 |
| `operability` | `antigravity`, `gemini` | 0.50 | 0.00 | 0.00 | 2 |
| `security-threat-model` | `antigravity`, `gemini` | 0.50 | 0.00 | 0.00 | 2 |

_Caveat: This compares observed reviewer-run outcomes. It is strongest when each provider/lens cell sees the same corpus heads and when run-level dispositions distinguish expected catches from nearby but non-target findings._
