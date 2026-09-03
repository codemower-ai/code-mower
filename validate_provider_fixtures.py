#!/usr/bin/env python3
"""Manual validation of provider fixtures for v1.0 diversity hardening."""

import json
import sys
from pathlib import Path

FIXTURE_ROOT = Path(__file__).resolve().parent / "tests" / "fixtures"
CONTRACT_PATH = FIXTURE_ROOT / "provider_verdict_contracts.json"
ARTIFACT_ROOT = FIXTURE_ROOT / "verdict_artifacts"

def validate_fixtures():
    """Validate provider fixture contracts and artifacts."""
    
    errors = []
    
    # Load and validate contracts
    try:
        with open(CONTRACT_PATH, 'r', encoding='utf-8') as f:
            contracts = json.load(f)
    except Exception as e:
        errors.append(f"Failed to load {CONTRACT_PATH}: {e}")
        return errors
    
    # Check schema
    if contracts.get("schema") != "code_mower.providerFixtureContracts.v1":
        errors.append(f"Invalid schema: {contracts.get('schema')}")
    
    # Expected providers
    expected_providers = ["codex", "claude", "gitar", "cursor_bugbot", "antigravity_cli", "devin", "muse_cli", "grok_build"]
    
    for provider in expected_providers:
        if provider not in contracts:
            errors.append(f"Missing provider: {provider}")
            continue
        
        provider_cases = contracts[provider]
        expected_cases = ["pass", "blocked", "placeholder", "malformed_extra_key"]
        
        for case in expected_cases:
            if case not in provider_cases:
                errors.append(f"Missing case {case} for provider {provider}")
                continue
            
            case_data = provider_cases[case]
            
            # Check required fields
            if "schema" not in case_data:
                errors.append(f"Missing schema in {provider}.{case}")
            if "verdict" not in case_data:
                errors.append(f"Missing verdict in {provider}.{case}")
            if "summary" not in case_data:
                errors.append(f"Missing summary in {provider}.{case}")
            if "findings" not in case_data:
                errors.append(f"Missing findings in {provider}.{case}")
            
            # Check malformed case has extra key
            if case == "malformed_extra_key" and "extra" not in case_data:
                errors.append(f"Malformed case for {provider} missing 'extra' key")
    
    # Validate verdict artifacts
    expected_artifacts = [
        "codex-pass.json",
        "claude-blocked.json", 
        "codex-unknown-infra.json",
        "gitar-pass.json",
        "cursor-bugbot-blocked.json",
        "antigravity-pass.json",
        "devin-blocked.json",
        "muse-pass.json",
        "grok-build-pass.json"
    ]
    
    for artifact_file in expected_artifacts:
        artifact_path = ARTIFACT_ROOT / artifact_file
        if not artifact_path.exists():
            errors.append(f"Missing verdict artifact: {artifact_file}")
            continue
        
        try:
            with open(artifact_path, 'r', encoding='utf-8') as f:
                artifact = json.load(f)
        except Exception as e:
            errors.append(f"Failed to load {artifact_file}: {e}")
            continue
        
        # Check required fields
        required_fields = ["schema", "verdict", "lane_id", "pr_number", "repo", "head_sha_start", "head_sha_end"]
        for field in required_fields:
            if field not in artifact:
                errors.append(f"Missing field {field} in {artifact_file}")
        
        # Check schema
        if artifact.get("schema") != "code_mower.auditVerdictArtifact.v1":
            errors.append(f"Invalid schema in {artifact_file}: {artifact.get('schema')}")
        
        # Check metadata-only privacy boundary
        sensitive_fields = ["raw_diff", "transcript", "source_code", "auth_output", "secrets"]
        for sensitive_field in sensitive_fields:
            if sensitive_field in artifact:
                errors.append(f"Privacy violation: {sensitive_field} found in {artifact_file}")
    
    return errors

if __name__ == "__main__":
    print("Validating v1.0 provider diversity fixtures...")
    print()
    
    errors = validate_fixtures()
    
    if errors:
        print(f"❌ Validation failed with {len(errors)} error(s):")
        for error in errors:
            print(f"  - {error}")
        sys.exit(1)
    else:
        print("✅ All provider fixtures validated successfully!")
        print()
        print("Summary:")
        print("  - 8 providers with fixtures: codex, claude, gitar, cursor_bugbot, antigravity_cli, devin, muse_cli, grok_build")
        print("  - 4 test cases per provider: pass, blocked, placeholder, malformed_extra_key")
        print("  - 9 verdict artifact fixtures created")
        print("  - Metadata-only privacy boundary maintained")
        sys.exit(0)
