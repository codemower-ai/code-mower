"""Product, transport, migration, and review-policy regressions for #904."""

from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from code_mower import config, init, participants, session
from code_mower.doctor_checks.providers import check_lane_runtime
from code_mower.provider_capabilities import (
    LEGACY_CAPABILITIES, TRANSPORTS, lane_transport, normalize_config, normalize_lane, resolve_transport,
)
from code_mower.provider_registry import REFERENCE_PROVIDERS
from code_mower.package_rendering import _render_yaml
from code_mower.release_campaigns import resolve_provider_lane


ROOT = Path(__file__).resolve().parents[1]
STARTER = ROOT / "src/code_mower/templates/code-mower.example.yml"


class DevinCapabilityTests(unittest.TestCase):
    def test_registry_and_every_catalog_share_safe_defaults(self):
        catalog = config.load_config(ROOT / "templates/providers.yml")["provider_templates"]
        packaged = config.load_config(ROOT / "src/code_mower/templates/providers.yml")["provider_templates"]
        for transport in TRANSPORTS.values():
            name = transport.review_lane
            reference = participants.reference_review_config(name)
            standalone = config.load_config(ROOT / f"templates/providers/{name}.yml")[name]
            for lane in (reference, catalog[name], packaged[name], standalone):
                self.assertFalse(lane["merge_authority"])
                self.assertTrue(lane["informational"])
                self.assertFalse(lane["enabled_by_default"])
                self.assertEqual(lane_transport(name, lane), transport)
                for key, value in transport.declaration().items():
                    self.assertEqual(lane[key], value)
            self.assertEqual(REFERENCE_PROVIDERS[name].product, "devin")
            self.assertEqual(REFERENCE_PROVIDERS[name].transport, transport.transport)

    def test_hosted_aliases_do_not_resolve_to_local_transport(self):
        for alias in ("devin", "devin_cloud", "devin_api_v3", "devin-api-v3"):
            self.assertEqual(resolve_transport(alias).transport, "devin_api_v3")
            self.assertEqual(resolve_provider_lane(alias)[0], "devin")
        self.assertEqual(resolve_transport("devin_cli").product, "devin")
        self.assertEqual(resolve_transport("devin_cli").transport, "devin_cli")

    def test_unambiguous_legacy_config_migrates_without_writing(self):
        source = config.load_config(STARTER)
        source["lanes"]["devin"] = participants.reference_review_config("devin")
        for key in ("product", "transport", "capabilities", "merge_authority", "informational"):
            source["lanes"]["devin"].pop(key)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "code-mower.yml"
            path.write_text("\n".join(_render_yaml(source)) + "\n")
            before = path.read_bytes()
            loaded = config.load_config(path)
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(config.validate_config(loaded), [])
        self.assertNotIn("transport", loaded["lanes"]["devin"])
        self.assertNotIn("informational", loaded["lanes"]["devin"])
        lane = normalize_config(loaded)["lanes"]["devin"]
        self.assertEqual(lane["transport"], "devin_api_v3")
        self.assertFalse(lane["merge_authority"])
        self.assertTrue(lane["informational"])
        rendered = config.render_dry_run(loaded).data["lanes"]["devin"]
        self.assertEqual(rendered["transport"], "devin_api_v3")
        self.assertIn("informational", rendered["flags"])
        self.assertNotIn("merge-authority", rendered["flags"])

    def test_earlier_hosted_capabilities_migrate_and_only_those_declarations(self):
        current = participants.reference_review_config("devin")
        self.assertEqual(current["capabilities"]["context"], "agent_handoff")
        pre_remote_session = copy.deepcopy(current)  # Exact pre-remote-session declaration.
        pre_remote_session["capabilities"].update(
            message="unavailable", cancel="unavailable", structured_results="campaign_only",
        )
        legacy = copy.deepcopy(pre_remote_session)
        legacy["capabilities"]["context"] = "unavailable"  # Exact pre-D5 template declaration.
        self.assertEqual(LEGACY_CAPABILITIES, {"devin_api_v3": (
            pre_remote_session["capabilities"], legacy["capabilities"],
        )})
        for accepted in (pre_remote_session, legacy):
            migrated = normalize_lane("devin", copy.deepcopy(accepted))
            self.assertEqual(migrated["capabilities"], current["capabilities"])
        lane = normalize_lane("devin", legacy)
        self.assertEqual(lane["capabilities"], current["capabilities"])
        self.assertEqual((lane["transport"], lane["driver"]), ("devin_api_v3", "hosted_bridge"))
        self.assertFalse(lane["merge_authority"])
        source = config.load_config(STARTER)
        source["lanes"]["devin"] = legacy
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "code-mower.yml"
            path.write_text("\n".join(_render_yaml(source)) + "\n")
            before = path.read_bytes()
            loaded = config.load_config(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(config.validate_config(loaded), [])
            self.assertEqual(loaded["lanes"]["devin"]["capabilities"]["context"], "unavailable")
            self.assertEqual(normalize_config(loaded)["lanes"]["devin"]["capabilities"], current["capabilities"])
            rendered = config.render_dry_run(loaded).data["lanes"]["devin"]
            self.assertEqual(rendered["transport"], "devin_api_v3")
            brief = session.build_session(repo="owner/repo", host="codex", selected=("devin_api_v3",), config=loaded)
            self.assertEqual(brief["participants"][0]["execution"], TRANSPORTS["devin_api_v3"].brief())
            self.assertIn("context=agent_handoff", session.render_session(brief))
            self.assertEqual(participants.configured_transports(loaded), participants.configured_transports(
                {**loaded, "lanes": {**loaded["lanes"], "devin": current}}))
        for change in (
            {"context": "local_runner"}, {"context": "unavailable", "review": "agent_handoff"},
            {"context": "unavailable", "message": "agent_handoff"}, {"context": "unavailable", "extra": "x"},
            {"context": "unavailable", "coordinate": "agent_handoff"},
        ):
            drifted = copy.deepcopy(current)
            drifted["capabilities"] = {**current["capabilities"], **change}
            with self.subTest(change=change), self.assertRaisesRegex(config.ConfigError, "capabilities must match"):
                normalize_lane("devin", drifted)
        missing = copy.deepcopy(legacy)
        del missing["capabilities"]["structured_results"]
        with self.assertRaisesRegex(config.ConfigError, "capabilities must match"):
            normalize_lane("devin", missing)
        # The local transport never had another maintained declaration.
        local = participants.reference_review_config("devin_cli")
        local["capabilities"]["context"] = "agent_handoff"
        with self.assertRaisesRegex(config.ConfigError, "capabilities must match"):
            normalize_lane("devin_cli", local)
        with self.assertRaisesRegex(config.ConfigError, "lane identities"):
            normalize_lane("devin_cli", legacy)

    def test_shipped_root_config_loads_without_semantic_normalization(self):
        loaded = config.load_config(ROOT / "code-mower.yml")
        self.assertIn(loaded["version"], (1, "1"))
        self.assertTrue(loaded["lanes"])
        self.assertEqual(config.validate_config(loaded), [])

    def test_loading_defers_multiple_semantic_defects_to_validation(self):
        source = config.load_config(STARTER)
        source["version"] = 2
        source["project"]["name"] = ""
        source["lanes"]["devin"] = participants.reference_review_config("devin")
        source["lanes"]["devin"].update(merge_authority=True, informational=False)
        source["lanes"]["devin"].pop("transport")
        source["lanes"]["devin_cli"] = participants.reference_review_config("devin_cli")
        source["lanes"]["devin_cli"]["transport"] = "devin_api_v3"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "code-mower.yml"
            path.write_text("\n".join(_render_yaml(source)) + "\n")
            loaded = config.load_config(path)
        self.assertNotIn("transport", loaded["lanes"]["devin"])
        self.assertTrue(loaded["lanes"]["devin"]["merge_authority"])
        issues = config.validate_config(loaded)
        self.assertTrue({"version", "project.name", "lanes.devin", "lanes.devin_cli"}
                        <= {issue.path for issue in issues})
        self.assertTrue(any("Legacy Devin" in issue.message for issue in issues))
        self.assertTrue(any("provider and transport disagree" in issue.message for issue in issues))
        with self.assertRaises(config.ConfigError) as caught:
            config.render_dry_run(loaded)
        for path in ("version", "project.name", "lanes.devin", "lanes.devin_cli"):
            self.assertIn(path + ":", str(caught.exception))

    def test_every_profile_is_checked_for_devin_transport_ambiguity(self):
        source = config.load_config(STARTER)
        for name in ("devin", "devin_cli"):
            source["lanes"][name] = participants.reference_review_config(name)
        for name in ("custom", "another"):
            source["profiles"][name] = {
                "description": "Both Devin reviewers", "lanes": ["devin", "devin_cli"],
            }
        issues = config.validate_config(source)
        self.assertEqual({issue.path for issue in issues}, {"profiles.custom", "profiles.another"})
        self.assertTrue(all("Both Devin" in issue.message for issue in issues))
        for defaults in (
            {"transports": {"devin": "devin_api_v3"}},
            {"participants": ["devin_cli"]},
            {"participants": ["devin_cloud"]},
        ):
            with self.subTest(defaults=defaults):
                source["session_defaults"] = defaults
                self.assertEqual(config.validate_config(source), [])

    def test_legacy_authority_requires_explicit_migration(self):
        lane = participants.reference_review_config("devin")
        lane.update(merge_authority=True, informational=False)
        lane.pop("transport")
        with self.assertRaisesRegex(config.ConfigError, "set merge_authority: false"):
            normalize_lane("devin", lane)
        source = config.load_config(STARTER)
        source["lanes"]["devin"] = lane
        self.assertTrue(any("Legacy Devin" in issue.message for issue in config.validate_config(source)))
        # Explicit repository promotion is preserved; selection never grants it.
        lane["transport"] = "devin_api_v3"
        self.assertTrue(normalize_lane("devin", lane)["merge_authority"])

    def test_contradictory_declarations_and_forged_capabilities_fail(self):
        original = participants.reference_review_config("devin_cli")
        for change in (
            {"product": "codex"}, {"transport": "devin_api_v3"},
            {"driver": "hosted_bridge"}, {"capabilities": {"message": "supported"}},
            {"merge_authority": "false"}, {"transport": {"private": "value"}},
            {"provider": "codex"}, {"provider_config": {"campaign_transport": "devin_api_v3"}},
        ):
            with self.subTest(change=change), self.assertRaises(config.ConfigError):
                normalize_lane("devin_cli", {**original, **change})
        with self.assertRaisesRegex(config.ConfigError, "lane identities"):
            normalize_lane("devin_cli", participants.reference_review_config("devin"))
        with self.assertRaises(config.ConfigError) as caught:
            resolve_transport("private context\n" * 1000)
        self.assertLess(len(str(caught.exception)), 200)
        self.assertNotIn("private context", str(caught.exception))

    def test_session_briefs_preserve_transport_aliases_and_capability_gaps(self):
        for selection, expected in (("devin", "devin_cli"), ("devin_cli", "devin_cli"), ("devin_cloud", "devin_api_v3"), ("devin_api_v3", "devin_api_v3")):
            with self.subTest(selection=selection):
                brief = session.build_session(repo="owner/repo", host="codex", selected=(selection,), config={})
                member = brief["participants"][0]
                self.assertEqual(member["id"], "devin")
                self.assertEqual(member["execution"], TRANSPORTS[expected].brief())
                self.assertEqual(member["reviewer"]["lane"], TRANSPORTS[expected].review_lane)
                self.assertFalse(member["reviewer"]["merge_authority"])
                self.assertTrue(member["reviewer"]["informational"])
                rendered = session.render_session(brief)
                self.assertIn(expected, rendered)
                for capability in ("message", "cancel", "structured_results"):
                    expected_mode = getattr(TRANSPORTS[expected].capabilities, capability)
                    self.assertIn(f"{capability}={expected_mode}", rendered)
                    self.assertEqual(
                        expected == "devin_api_v3", expected_mode == "remote_session"
                    )
                self.assertIn("context=" + TRANSPORTS[expected].capabilities.context, rendered)
                self.assertEqual(expected == "devin_api_v3", "context=agent_handoff" in rendered)

    def test_hosted_transport_cannot_coordinate(self):
        with self.assertRaisesRegex(config.ConfigError, "cannot coordinate"):
            session.build_session(repo="owner/repo", host="devin_api_v3", selected=("devin",), config={})
        brief = session.build_session(repo="owner/repo", host="devin_cli", selected=("devin",), config={})
        self.assertEqual(brief["host"], "devin")
        self.assertEqual(brief["host_transport"], "devin_cli")

    def test_configured_transport_and_cli_alias_survive_generated_setup(self):
        source = config.load_config(STARTER)
        for selection in ("devin", "devin_api_v3"):
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as tmp:
                source["session_defaults"] = {"participants": ["devin"], "transports": {"devin": "devin_api_v3"}}
                plan = init.render_init_plan(source, participants=(selection,), package_mode=True)
                self.assertEqual(plan.data["profile"]["lanes"], ["devin"])
                init.apply_init_plan(plan, Path(tmp))
                saved = config.load_config(Path(tmp) / "code-mower.yml")
                self.assertEqual(config.validate_config(saved), [])
                self.assertEqual(saved["session_defaults"]["transports"], {"devin": "devin_api_v3"})
                self.assertFalse(saved["lanes"]["devin"]["merge_authority"])
                self.assertNotIn("local-cli-audit.yml", [item["path"] for item in plan.data["generated_files"]])

    def test_cli_session_keeps_hosted_alias(self):
        output = io.StringIO()
        with redirect_stdout(output):
            rc = session.main(["start", "--repo", "owner/repo", "--host", "codex", "--with", "devin_api_v3", "--dry-run", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue())["participants"][0]["execution"]["transport"], "devin_api_v3")

    def test_legacy_hosted_profile_is_preserved_and_ambiguity_is_bounded(self):
        source = config.load_config(STARTER)
        source["lanes"]["devin"] = participants.reference_review_config("devin")
        source["profiles"]["recommended"]["lanes"] = ["devin"]
        updated = participants.config_with_participants(source, ("devin",))
        self.assertEqual(updated["profiles"]["recommended"]["lanes"], ["devin"])
        source["profiles"]["recommended"]["lanes"].append("devin_cli")
        with self.assertRaisesRegex(config.ConfigError, "Both Devin"):
            participants.configured_transports(source)
        for defaults in (
            {"transports": {"devin": "unknown"}},
            {"transports": []},
            {"participants": ["devin_cli"], "transports": {"devin": "devin_api_v3"}},
            {"participants": ["devin_cli", "devin_api_v3"]},
        ):
            broken = copy.deepcopy(source)
            broken["session_defaults"] = defaults
            self.assertTrue(config.validate_config(broken))

    def test_doctor_reports_static_gaps_without_a_live_probe(self):
        for transport in TRANSPORTS.values():
            checks = check_lane_runtime(transport.review_lane, participants.reference_review_config(transport.review_lane), probe_runtime=False, http_timeout=1, adoption_posture="orchestrator-only")
            check = next(check for check in checks if check.name == "provider.capabilities")
            self.assertEqual(check.detail, transport.brief())
            self.assertEqual(
                transport.transport == "devin_cli", "message, cancel" in check.message
            )
            self.assertEqual(transport.transport == "devin_cli", "context" in check.message)
            self.assertEqual(check.status, "warn")

    def test_hosted_remote_session_modes_name_real_provider_operations(self):
        """Each hosted `remote_session` mode must have a shipped operation behind it."""
        from code_mower.remote_session import DevinProvider
        from code_mower.remote_session_cli import COMMANDS

        hosted = TRANSPORTS["devin_api_v3"].capabilities
        operations = {
            "message": ("message",),
            "cancel": ("cancel",),
            "structured_results": ("status", "collect"),
        }
        for capability, commands in operations.items():
            with self.subTest(capability=capability):
                self.assertEqual(getattr(hosted, capability), "remote_session")
                for command in commands:
                    self.assertIn(command, COMMANDS)
        for operation in ("get", "message", "cancel"):
            self.assertTrue(callable(getattr(DevinProvider, operation)))
        self.assertIn("completion_schema", DevinProvider.__init__.__code__.co_varnames)
        local = TRANSPORTS["devin_cli"].capabilities
        for capability in ("message", "cancel"):
            self.assertEqual(getattr(local, capability), "unavailable")
        self.assertEqual(local.structured_results, "local_runner")
        self.assertEqual(hosted.coordinate, "unavailable")
        self.assertEqual(hosted.review, "evidence_only")

    def test_shipped_schema_matches_each_complete_transport_contract(self):
        schema = json.loads((ROOT / "src/code_mower/provider_capabilities.schema.json").read_text())
        self.assertEqual(len(schema["oneOf"]), len(TRANSPORTS))
        for item in schema["oneOf"]:
            declared = {key: value["const"] for key, value in item["properties"].items()}
            self.assertEqual(declared, TRANSPORTS[declared["transport"]].brief())
            self.assertEqual(set(item["required"]), set(declared))
            self.assertFalse(item["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
