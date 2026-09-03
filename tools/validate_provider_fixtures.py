#!/usr/bin/env python3
"""Manual validation for provider fixture contracts and verdict artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures"
CONTRACT_PATH = FIXTURE_ROOT / "provider_verdict_contracts.json"
ARTIFACT_ROOT = FIXTURE_ROOT / "verdict_artifacts"

EXPECTED_PROVIDERS = (
    "codex",
    "claude",
    "gitar",
    "cursor_bugbot",
    "antigravity_cli",
    "devin",
    "muse_cli",
    "grok_build",
)
EXPECTED_CASES = ("pass", "blocked", "placeholder", "malformed_extra_key")
EXPECTED_ARTIFACTS = (
    "codex-pass.json",
    "claude-blocked.json",
    "codex-unknown-infra.json",
    "gitar-pass.json",
    "cursor-bugbot-blocked.json",
    "antigravity-pass.json",
    "devin-blocked.json",
    "muse-pass.json",
    "grok-build-pass.json",
)
REQUIRED_ARTIFACT_FIELDS = (
    "schema",
    "verdict",
    "lane_id",
    "pr_number",
    "repo",
    "head_sha_start",
    "head_sha_end",
)
SENSITIVE_KEYS = {
    "auth_output",
    "raw_diff",
    "raw_stderr",
    "raw_stdout",
    "secret",
    "secrets",
    "source_code",
    "transcript",
}


def validate_fixtures() -> list[str]:
    errors: list[str] = []

    try:
        contracts = _load_object(CONTRACT_PATH)
    except OSError as exc:
        return [f"Failed to load {CONTRACT_PATH}: {exc}"]
    except ValueError as exc:
        return [str(exc)]

    if contracts.get("schema") != "code_mower.providerFixtureContracts.v1":
        errors.append(f"Invalid schema: {contracts.get('schema')}")

    for provider in EXPECTED_PROVIDERS:
        provider_cases = contracts.get(provider)
        if not isinstance(provider_cases, dict):
            errors.append(f"Missing provider: {provider}")
            continue

        for case in EXPECTED_CASES:
            case_data = provider_cases.get(case)
            if not isinstance(case_data, dict):
                errors.append(f"Missing case {case} for provider {provider}")
                continue

            for field in ("schema", "verdict", "summary", "findings"):
                if field not in case_data:
                    errors.append(f"Missing {field} in {provider}.{case}")
            if case == "malformed_extra_key" and "extra" not in case_data:
                errors.append(f"Malformed case for {provider} missing 'extra' key")

    for artifact_file in EXPECTED_ARTIFACTS:
        artifact_path = ARTIFACT_ROOT / artifact_file
        if not artifact_path.exists():
            errors.append(f"Missing verdict artifact: {artifact_file}")
            continue

        try:
            artifact = _load_object(artifact_path)
        except OSError as exc:
            errors.append(f"Failed to load {artifact_file}: {exc}")
            continue
        except ValueError as exc:
            errors.append(str(exc))
            continue

        for field in REQUIRED_ARTIFACT_FIELDS:
            if field not in artifact:
                errors.append(f"Missing field {field} in {artifact_file}")
        if artifact.get("schema") != "code_mower.auditVerdictArtifact.v1":
            errors.append(f"Invalid schema in {artifact_file}: {artifact.get('schema')}")
        for path in _sensitive_key_paths(artifact):
            errors.append(f"Privacy violation: {path} found in {artifact_file}")

    return errors


def _load_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload


def _sensitive_key_paths(value: object, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            child_prefix = f"{prefix}.{key_text}" if prefix else key_text
            if key_text.lower() in SENSITIVE_KEYS:
                paths.append(child_prefix)
            paths.extend(_sensitive_key_paths(child, child_prefix))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_sensitive_key_paths(child, f"{prefix}[{index}]"))
    return paths


def main() -> int:
    print("Validating v1.0 provider diversity fixtures...")
    print()

    errors = validate_fixtures()
    if errors:
        print(f"Validation failed with {len(errors)} error(s):")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("All provider fixtures validated successfully.")
    print("Summary:")
    print(f"  - {len(EXPECTED_PROVIDERS)} providers with fixture contracts")
    print(f"  - {len(EXPECTED_CASES)} test cases per provider")
    print(f"  - {len(EXPECTED_ARTIFACTS)} verdict artifact fixtures")
    print("  - Metadata-only privacy boundary maintained")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
