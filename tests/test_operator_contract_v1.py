from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path
from typing import Any

from code_mower.operator_contract_v1 import (
    action_intent_semantic_errors,
    lease_semantic_errors,
    policy_binding_errors,
    qualification_semantic_errors,
    recovery_transition_errors,
)


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "code_mower"
DOC = ROOT / "docs" / "operator-contract-v1.md"
FIXTURE_PATH = PACKAGE / "operator_contract_v1.fixtures.json"
SCHEMA_PATHS = {
    "operator_policy_v1.schema.json": PACKAGE / "operator_policy_v1.schema.json",
    "operator_state_v1.schema.json": PACKAGE / "operator_state_v1.schema.json",
}


def _same_json_value(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def _resolve(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise AssertionError(f"test validator only accepts local references: {reference}")
    value: Any = root
    for part in reference[2:].split("/"):
        value = value[part.replace("~1", "/").replace("~0", "~")]
    if not isinstance(value, dict):
        raise AssertionError(f"schema reference is not an object: {reference}")
    return value


def _type_matches(value: object, expected: str) -> bool:
    return {
        "array": isinstance(value, list),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "null": value is None,
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "object": isinstance(value, dict),
        "string": isinstance(value, str),
    }[expected]


def _errors(
    value: object,
    schema: dict[str, Any],
    root: dict[str, Any],
    path: str = "$",
) -> list[str]:
    if "$ref" in schema:
        return _errors(value, _resolve(root, schema["$ref"]), root, path)

    errors: list[str] = []
    if "oneOf" in schema:
        matches = [
            not _errors(value, branch, root, path)
            for branch in schema["oneOf"]
        ]
        if sum(matches) != 1:
            errors.append(f"{path}: expected exactly one oneOf branch, got {sum(matches)}")
    if "anyOf" in schema and not any(
        not _errors(value, branch, root, path) for branch in schema["anyOf"]
    ):
        errors.append(f"{path}: did not match anyOf")
    for branch in schema.get("allOf", []):
        errors.extend(_errors(value, branch, root, path))
    condition = schema.get("if")
    if isinstance(condition, dict) and not _errors(value, condition, root, path):
        then = schema.get("then")
        if isinstance(then, dict):
            errors.extend(_errors(value, then, root, path))

    if "const" in schema and not _same_json_value(value, schema["const"]):
        errors.append(f"{path}: value differs from const")
    if "enum" in schema and not any(
        _same_json_value(value, candidate) for candidate in schema["enum"]
    ):
        errors.append(f"{path}: value is outside enum")

    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _type_matches(value, expected_type):
        errors.append(f"{path}: expected {expected_type}")
        return errors

    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{path}: missing required property {key}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in sorted(set(value) - set(properties)):
                errors.append(f"{path}: unexpected property {key}")
        for key, child_schema in properties.items():
            if key in value:
                errors.extend(_errors(value[key], child_schema, root, f"{path}.{key}"))

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: too many items")
        if schema.get("uniqueItems"):
            encoded = [json.dumps(item, sort_keys=True) for item in value]
            if len(encoded) != len(set(encoded)):
                errors.append(f"{path}: items are not unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(_errors(item, item_schema, root, f"{path}[{index}]"))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: string is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: string is too long")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{path}: string does not match pattern")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: value is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: value is above maximum")
    return errors


class OperatorContractV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = {
            name: json.loads(path.read_text(encoding="utf-8"))
            for name, path in SCHEMA_PATHS.items()
        }
        cls.fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def _validate(self, schema_name: str, document: object) -> list[str]:
        schema = self.schemas[schema_name]
        return _errors(document, schema, schema)

    def _semantic_errors(
        self,
        document: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> tuple[str, ...]:
        context = context or {}
        if document.get("schema") == "code_mower.operatorQualification.v1":
            policy = next(
                case["document"]
                for case in self.fixtures["accepted"]
                if case["name"] == "single_tenant_manual_merge_policy"
            )
            return qualification_semantic_errors(
                document,
                max_evidence_age_seconds=policy["qualification"]["max_evidence_age_seconds"],
                now=context.get("now", 2_000_000_100),
            )
        if document.get("schema") == "code_mower.operatorPolicy.v1":
            return ()
        policy = copy.deepcopy(
            next(
                case["document"]
                for case in self.fixtures["accepted"]
                if case["name"] == "single_tenant_manual_merge_policy"
            )
        )
        removed_mutation = context.get("policy_remove_mutation")
        if isinstance(removed_mutation, str):
            policy["authority"]["mutations"].remove(removed_mutation)
        return policy_binding_errors(
            policy,
            document,
            current_work_generation=context.get("current_work_generation"),
            current_lease=context.get("current_lease"),
            current_head_sha=context.get("current_head_sha"),
            now=context.get("now"),
        )

    def test_schemas_are_versioned_and_every_object_definition_is_closed(self) -> None:
        self.assertEqual(
            {schema["$id"] for schema in self.schemas.values()},
            {"code_mower.operator_policy.v1", "code_mower.operator_state.v1"},
        )
        for name, schema in self.schemas.items():
            with self.subTest(schema=name):
                self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
                stack: list[object] = [schema]
                while stack:
                    current = stack.pop()
                    if isinstance(current, dict):
                        if current.get("type") == "object":
                            self.assertIs(
                                current.get("additionalProperties"),
                                False,
                                f"open object in {name}: {current}",
                            )
                        stack.extend(current.values())
                    elif isinstance(current, list):
                        stack.extend(current)

    def test_all_canonical_accepted_fixtures_validate(self) -> None:
        self.assertEqual(self.fixtures["schema"], "code_mower.operatorContractFixtures.v1")
        self.assertEqual(self.fixtures["contract"], "code_mower.operator.v1")
        self.assertEqual(
            set(self.fixtures),
            {"schema", "contract", "accepted", "rejected", "failure_scenarios"},
        )
        names: set[str] = set()
        for case in self.fixtures["accepted"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(
                    set(case),
                    {"name", "contract_schema", "document"}
                    | ({"semantic_context"} if "semantic_context" in case else set()),
                )
                self.assertNotIn(case["name"], names)
                names.add(case["name"])
                self.assertEqual(
                    self._validate(case["contract_schema"], case["document"]),
                    [],
                )
                self.assertEqual(
                    self._semantic_errors(case["document"], case.get("semantic_context")),
                    (),
                )

    def test_all_canonical_rejected_fixtures_fail_validation(self) -> None:
        names: set[str] = set()
        for case in self.fixtures["rejected"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(
                    set(case),
                    {"name", "contract_schema", "expected_violation", "document"}
                    | ({"semantic_context"} if "semantic_context" in case else set()),
                )
                self.assertNotIn(case["name"], names)
                names.add(case["name"])
                self.assertTrue(case["expected_violation"])
                schema_errors = self._validate(case["contract_schema"], case["document"])
                semantic_errors = self._semantic_errors(
                    case["document"],
                    case.get("semantic_context"),
                )
                self.assertTrue(
                    schema_errors or semantic_errors,
                    f"rejected fixture unexpectedly validated: {case['name']}",
                )

    def test_provider_qualification_is_data_driven_and_fail_closed(self) -> None:
        qualification = self.schemas["operator_policy_v1.schema.json"]["$defs"]["qualification"]
        self.assertEqual(qualification["properties"]["provider_id"], {"$ref": "#/$defs/opaque_id"})

        pending = next(
            case["document"]
            for case in self.fixtures["accepted"]
            if case["name"] == "devin_builder_pending_without_exclusion"
        )
        future_provider = copy.deepcopy(pending)
        future_provider["provider_id"] = "future_provider"
        future_provider["transport_id"] = "future_transport"
        self.assertEqual(
            self._validate("operator_policy_v1.schema.json", future_provider),
            [],
        )
        self.assertEqual(future_provider["decision"], "denied")

        initial = {
            case["document"]["provider_id"]
            for case in self.fixtures["accepted"]
            if case["document"].get("schema") == "code_mower.operatorQualification.v1"
            and case["document"]["status"] == "qualified"
        }
        self.assertEqual(initial, {"codex", "claude"})
        supervisors = {
            case["document"]["provider_id"]
            for case in self.fixtures["accepted"]
            if case["document"].get("schema") == "code_mower.operatorQualification.v1"
            and case["document"]["role"] == "orchestrator"
            and case["document"]["status"] == "qualified"
        }
        self.assertEqual(supervisors, {"codex", "claude"})

    def test_policy_keeps_human_merge_and_bounded_lease_cadence(self) -> None:
        policy = next(
            case["document"]
            for case in self.fixtures["accepted"]
            if case["name"] == "single_tenant_manual_merge_policy"
        )
        authority = policy["authority"]
        self.assertEqual(authority["merge_approval"], "human_required")
        self.assertIs(authority["operator_merge"], False)
        self.assertIn("merge_pull_request", authority["denied"])
        self.assertLess(
            policy["budgets"]["lease_renewal_seconds"],
            policy["budgets"]["lease_ttl_seconds"],
        )

    def test_failure_fixtures_execute_required_and_forbidden_recovery_transitions(self) -> None:
        scenarios = {case["name"]: case for case in self.fixtures["failure_scenarios"]}
        for case in scenarios.values():
            self.assertEqual(set(case), {"name", "before", "after", "forbidden_afters"})
            for phase in ("before", "after"):
                for record in case[phase]:
                    self.assertEqual(set(record), {"contract_schema", "document"})
                    self.assertEqual(
                        self._validate(record["contract_schema"], record["document"]),
                        [],
                        f"{case['name']} {phase} contains an invalid contract record",
                    )
            for forbidden in case["forbidden_afters"]:
                self.assertEqual(set(forbidden), {"name", "records"})
                self.assertTrue(forbidden["name"])
                for record in forbidden["records"]:
                    self.assertEqual(set(record), {"contract_schema", "document"})
                    self.assertEqual(
                        self._validate(record["contract_schema"], record["document"]),
                        [],
                        f"{case['name']} {forbidden['name']} is not shape-valid",
                    )
        self.assertEqual(
            set(scenarios),
            {
                "restart_after_dispatch",
                "duplicate_delivery",
                "lease_takeover",
                "stale_head",
                "provider_timeout",
                "partial_success",
                "budget_exhaustion",
                "owner_escalation_redelivery",
                "owner_stop",
            },
        )
        for name, case in scenarios.items():
            before = [record["document"] for record in case["before"]]
            after = [record["document"] for record in case["after"]]
            with self.subTest(case=name, outcome="required"):
                self.assertEqual(recovery_transition_errors(name, before, after), ())
            for forbidden_case in case["forbidden_afters"]:
                forbidden = [record["document"] for record in forbidden_case["records"]]
                with self.subTest(case=name, outcome=forbidden_case["name"]):
                    self.assertTrue(recovery_transition_errors(name, before, forbidden))

    def test_takeover_after_unknown_preserves_dispatch_fence_and_uses_current_reconciler(self) -> None:
        intent = next(
            case["document"]
            for case in self.fixtures["accepted"]
            if case["name"] == "takeover_reconciles_unknown_without_redispatch"
        )
        self.assertEqual(intent["lease_epoch"], 8)
        self.assertEqual(intent["fence_status"], "stale")
        self.assertEqual(intent["reconciliation_authority"]["lease_epoch"], 9)
        self.assertEqual(intent["certainty"], "unknown")
        self.assertEqual(intent["next_action"], "reconcile")

    def test_owner_stop_reasons_and_escalation_identity_are_aligned(self) -> None:
        policy = next(
            case["document"]
            for case in self.fixtures["accepted"]
            if case["name"] == "single_tenant_manual_merge_policy"
        )
        self.assertTrue({"stale_head", "stale_lease"}.issubset(policy["owner_action"]["stop_reasons"]))
        work = next(
            case["document"]
            for case in self.fixtures["accepted"]
            if case["name"] == "work_waiting_for_owner_after_budget_exhaustion"
        )
        self.assertEqual(len(work["owner_escalation_key"]), 64)

    def test_intent_generation_is_bound_to_current_work_generation(self) -> None:
        current = next(
            case["document"]
            for case in self.fixtures["accepted"]
            if case["name"] == "durable_intent_precedes_dispatch"
        )
        context = next(
            case["semantic_context"]
            for case in self.fixtures["accepted"]
            if case["name"] == "durable_intent_precedes_dispatch"
        )
        self.assertEqual(
            action_intent_semantic_errors(
                current,
                current_work_generation=4,
                current_lease=context["current_lease"],
                current_head_sha=context["current_head_sha"],
                now=context["now"],
            ),
            (),
        )
        self.assertTrue(
            action_intent_semantic_errors(
                current,
                current_work_generation=5,
                current_lease=context["current_lease"],
                current_head_sha=context["current_head_sha"],
                now=context["now"],
            )
        )

    def test_mutation_authority_matrix_fails_closed(self) -> None:
        cases = [
            case
            for case in self.fixtures["accepted"]
            if case["document"].get("schema") == "code_mower.operatorActionIntent.v1"
            and case["document"].get("next_action") != "abandon"
        ]
        for case in cases:
            intent = case["document"]
            context = case["semantic_context"]
            kwargs = {
                "current_work_generation": context["current_work_generation"],
                "current_lease": context["current_lease"],
                "current_head_sha": context["current_head_sha"],
                "now": context["now"],
            }
            with self.subTest(case=case["name"], mutation="baseline"):
                self.assertEqual(action_intent_semantic_errors(intent, **kwargs), ())
            with self.subTest(case=case["name"], mutation="generation"):
                changed = dict(kwargs, current_work_generation=5)
                self.assertTrue(action_intent_semantic_errors(intent, **changed))
            for field, value in (
                ("tenant_id", "tenant_other"),
                ("repository_id", "repository_other"),
                ("lease_id", "operator_other"),
                ("schema", "code_mower.operatorProjection.v1"),
                ("epoch", context["current_lease"]["epoch"] + 1),
                ("fence_token", "a" * 64),
                ("state", "expired"),
                ("renew_by", context["now"]),
            ):
                changed_lease = copy.deepcopy(context["current_lease"])
                changed_lease[field] = value
                changed = dict(kwargs, current_lease=changed_lease)
                with self.subTest(case=case["name"], mutation=f"lease.{field}"):
                    self.assertTrue(action_intent_semantic_errors(intent, **changed))
            if intent["next_action"] in {"dispatch", "retry"} or intent["certainty"] in {
                "confirmed_success",
                "confirmed_failure",
            }:
                with self.subTest(case=case["name"], mutation="head"):
                    changed = dict(kwargs, current_head_sha="f" * 40)
                    self.assertTrue(action_intent_semantic_errors(intent, **changed))

    def test_policy_binding_matrix_enforces_record_level_ceilings(self) -> None:
        policy = next(
            case["document"]
            for case in self.fixtures["accepted"]
            if case["name"] == "single_tenant_manual_merge_policy"
        )
        action_case = next(
            case
            for case in self.fixtures["accepted"]
            if case["name"] == "durable_intent_precedes_dispatch"
        )
        action = action_case["document"]
        context = action_case["semantic_context"]

        def action_errors(document: dict[str, Any], selected_policy: dict[str, Any] = policy):
            return policy_binding_errors(
                selected_policy,
                document,
                current_work_generation=context["current_work_generation"],
                current_lease=context["current_lease"],
                current_head_sha=context["current_head_sha"],
                now=context["now"],
            )

        self.assertEqual(action_errors(action), ())
        for field, ceiling in (
            ("attempt_count", "max_attempts_per_action"),
            ("reconciliation_count", "max_reconciliations_per_action"),
            ("elapsed_seconds", "max_action_seconds"),
        ):
            changed = copy.deepcopy(action)
            changed["budget"][field] = policy["budgets"][ceiling] + 1
            with self.subTest(record="action", ceiling=ceiling):
                self.assertTrue(action_errors(changed))
        changed = copy.deepcopy(action)
        changed["budget"]["spend_usd"] = "25.01"
        self.assertTrue(action_errors(changed))
        changed_policy = copy.deepcopy(policy)
        changed_policy["authority"]["mutations"].remove(action["operation"])
        self.assertTrue(action_errors(action, changed_policy))

        work = next(
            case["document"]
            for case in self.fixtures["accepted"]
            if case["name"] == "work_waiting_for_owner_after_budget_exhaustion"
        )
        for field, value in (
            ("owner_escalation_count", policy["budgets"]["max_owner_escalations"] + 1),
            ("elapsed_seconds", policy["budgets"]["max_work_seconds"] + 1),
            ("spend_usd", "25.01"),
        ):
            changed = copy.deepcopy(work)
            changed[field] = value
            with self.subTest(record="work", ceiling=field):
                self.assertTrue(policy_binding_errors(policy, changed))

        lease = next(
            case["document"]
            for case in self.fixtures["accepted"]
            if case["name"] == "singleton_lease_with_fencing_epoch"
        )
        self.assertEqual(policy_binding_errors(policy, lease), ())
        changed = copy.deepcopy(lease)
        changed["expires_at"] += 1
        self.assertTrue(policy_binding_errors(policy, changed))

    def test_recovery_matrix_rejects_scope_chronology_and_all_counter_rollbacks(self) -> None:
        for case in self.fixtures["failure_scenarios"]:
            before = [copy.deepcopy(item["document"]) for item in case["before"]]
            after = [copy.deepcopy(item["document"]) for item in case["after"]]
            self.assertEqual(recovery_transition_errors(case["name"], before, after), ())
            before_by_schema = {item["schema"]: item for item in before}
            after_by_schema = {item["schema"]: item for item in after}
            action_schema = "code_mower.operatorActionIntent.v1"
            if action_schema in before_by_schema and action_schema in after_by_schema:
                for field, old_value, new_value in (
                    ("attempt_count", 2, 1),
                    ("reconciliation_count", 2, 1),
                    ("elapsed_seconds", 2, 1),
                    ("spend_usd", "2.00", "1.00"),
                ):
                    changed_before = copy.deepcopy(before)
                    changed_after = copy.deepcopy(after)
                    next(item for item in changed_before if item["schema"] == action_schema)["budget"][
                        field
                    ] = old_value
                    next(item for item in changed_after if item["schema"] == action_schema)["budget"][
                        field
                    ] = new_value
                    with self.subTest(case=case["name"], rollback=f"action.{field}"):
                        self.assertTrue(
                            recovery_transition_errors(case["name"], changed_before, changed_after)
                        )
            work_schema = "code_mower.operatorWorkItem.v1"
            if work_schema in before_by_schema and work_schema in after_by_schema:
                for field, old_value, new_value in (
                    ("owner_escalation_count", 2, 1),
                    ("elapsed_seconds", 2, 1),
                    ("spend_usd", "2.00", "1.00"),
                ):
                    changed_before = copy.deepcopy(before)
                    changed_after = copy.deepcopy(after)
                    next(item for item in changed_before if item["schema"] == work_schema)[field] = old_value
                    next(item for item in changed_after if item["schema"] == work_schema)[field] = new_value
                    with self.subTest(case=case["name"], rollback=f"work.{field}"):
                        self.assertTrue(
                            recovery_transition_errors(case["name"], changed_before, changed_after)
                        )

        takeover = next(
            case for case in self.fixtures["failure_scenarios"] if case["name"] == "lease_takeover"
        )
        before = [item["document"] for item in takeover["before"]]
        after = [item["document"] for item in takeover["after"]]
        lease_index = next(
            index
            for index, item in enumerate(after)
            if item["schema"] == "code_mower.operatorLease.v1"
        )
        for field, value in (
            ("tenant_id", "tenant_other"),
            ("repository_id", "repository_other"),
            ("lease_id", "operator_other"),
            ("holder_id", next(item for item in before if item["schema"] == "code_mower.operatorLease.v1")["holder_id"]),
        ):
            changed = copy.deepcopy(after)
            changed[lease_index][field] = value
            with self.subTest(case="lease_takeover", scope=field):
                self.assertTrue(recovery_transition_errors("lease_takeover", before, changed))
        changed = copy.deepcopy(after)
        changed[lease_index]["renew_by"] = changed[lease_index]["expires_at"] + 1
        self.assertTrue(recovery_transition_errors("lease_takeover", before, changed))

    def test_lease_chronology_matrix_is_strict(self) -> None:
        lease = next(
            case["document"]
            for case in self.fixtures["accepted"]
            if case["name"] == "singleton_lease_with_fencing_epoch"
        )
        self.assertEqual(lease_semantic_errors(lease), ())
        for acquired, renew, expires in (
            (10, 9, 11),
            (10, 11, 11),
            (10, 12, 11),
        ):
            changed = copy.deepcopy(lease)
            changed.update({"acquired_at": acquired, "renew_by": renew, "expires_at": expires})
            self.assertTrue(lease_semantic_errors(changed))

    def test_full_work_transition_matrix_matches_the_closed_state_machine(self) -> None:
        state_schema = self.schemas["operator_state_v1.schema.json"]
        transitions = state_schema["$defs"]["transition"]["enum"]
        base = next(
            case["document"]
            for case in self.fixtures["accepted"]
            if case["name"] == "work_waiting_for_owner_after_budget_exhaustion"
        )
        terminal_reasons = {
            "completed": "work_completed",
            "failed": "work_failed",
            "cancelled": "owner_cancelled",
        }
        for transition in transitions:
            _, target = transition.split(":", 1)
            document = copy.deepcopy(base)
            document.update(
                {
                    "state": target,
                    "terminal": target in terminal_reasons,
                    "last_transition": transition,
                    "reason": terminal_reasons.get(
                        target,
                        "approval_required" if target == "awaiting_owner" else "none",
                    ),
                    "owner_escalation_count": 1 if target == "awaiting_owner" else 0,
                    "owner_escalation_key": "9" * 64 if target == "awaiting_owner" else None,
                }
            )
            with self.subTest(transition=transition):
                self.assertEqual(
                    self._validate("operator_state_v1.schema.json", document),
                    [],
                )

    def test_full_lease_state_matrix_preserves_history_but_grants_only_live_authority(self) -> None:
        lease = next(
            case["document"]
            for case in self.fixtures["accepted"]
            if case["name"] == "singleton_lease_with_fencing_epoch"
        )
        for state in ("active", "expired", "released"):
            document = copy.deepcopy(lease)
            document["state"] = state
            with self.subTest(state=state, validation="history"):
                self.assertEqual(lease_semantic_errors(document), ())
            with self.subTest(state=state, validation="authority"):
                errors = lease_semantic_errors(document, now=document["acquired_at"], require_live=True)
                if state == "active":
                    self.assertEqual(errors, ())
                else:
                    self.assertTrue(errors)

    def test_metadata_projection_is_exactly_the_policy_allowlist(self) -> None:
        policy = next(
            case["document"]
            for case in self.fixtures["accepted"]
            if case["name"] == "single_tenant_manual_merge_policy"
        )
        projection = self.schemas["operator_state_v1.schema.json"]["$defs"]["projection"]
        self.assertEqual(
            set(projection["properties"]),
            set(policy["privacy"]["projection_fields"]),
        )
        forbidden = set(policy["privacy"]["excluded_content"])
        self.assertTrue(
            {
                "source",
                "diffs",
                "prompts",
                "transcripts",
                "issue_bodies",
                "raw_provider_output",
                "credentials",
                "private_content",
            }.issubset(forbidden)
        )

    def test_canonical_document_names_every_record_and_non_runtime_boundary(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        for record in (
            "code_mower.operatorPolicy.v1",
            "code_mower.operatorQualification.v1",
            "code_mower.operatorWorkItem.v1",
            "code_mower.operatorLease.v1",
            "code_mower.operatorActionIntent.v1",
            "code_mower.operatorProjection.v1",
        ):
            self.assertIn(record, text)
        self.assertIn("always requires a human to approve and perform a merge", text)
        self.assertIn("Active dispatch", text)
        self.assertIn("does not change an existing Board, telemetry, or cloud schema", text)

    def test_contract_artifacts_are_in_package_and_document_inventory(self) -> None:
        package_manifest = (PACKAGE / "package_manifest.py").read_text(encoding="utf-8")
        for path in (
            "src/code_mower/operator_contract_v1.py",
            "src/code_mower/operator_policy_v1.schema.json",
            "src/code_mower/operator_state_v1.schema.json",
            "src/code_mower/operator_contract_v1.fixtures.json",
            "docs/operator-contract-v1.md",
        ):
            self.assertIn(path, package_manifest)

        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"docs/operator-contract-v1.md"', project)
        docs_manifest = (ROOT / "docs" / "docs-manifest.yml").read_text(encoding="utf-8")
        self.assertIn("path: docs/operator-contract-v1.md", docs_manifest)
        self.assertIn("subject: operator-contract", docs_manifest)


if __name__ == "__main__":
    unittest.main()
