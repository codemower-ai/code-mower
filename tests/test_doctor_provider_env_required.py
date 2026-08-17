import os
import unittest
from unittest.mock import patch

from code_mower.doctor_checks.provider_env import check_required_env
from code_mower.doctor_checks.provider_env_required import provider_required_env_status


class ProviderRequiredEnvStatusTests(unittest.TestCase):
    def test_non_mapping_provider_config_declares_nothing(self) -> None:
        status = provider_required_env_status({"provider_config": "bad"})

        self.assertFalse(status.declares_required_env)
        self.assertTrue(status.all_present)
        self.assertEqual(check_required_env("custom-lane", {"provider_config": "bad"}), [])

    def test_no_required_env_returns_no_checks(self) -> None:
        status = provider_required_env_status({"provider_config": {}})

        self.assertFalse(status.declares_required_env)
        self.assertEqual(check_required_env("custom-lane", {"provider_config": {}}), [])

    def test_required_env_detects_missing_and_present_values(self) -> None:
        lane = {"provider_config": {"required_env": ["PRESENT_TOKEN", "MISSING_TOKEN"]}}

        with patch.dict(os.environ, {"PRESENT_TOKEN": "ok"}, clear=True):
            status = provider_required_env_status(lane)
            [check] = check_required_env("custom-lane", lane)

        self.assertEqual(status.required, ("PRESENT_TOKEN", "MISSING_TOKEN"))
        self.assertEqual(status.missing, ("MISSING_TOKEN",))
        self.assertEqual(check.status, "warn")
        self.assertEqual(check.detail["missing"], ["MISSING_TOKEN"])

    def test_required_truthy_detects_falsey_values(self) -> None:
        lane = {"provider_config": {"required_env_truthy": ["FEATURE_ON", "FEATURE_OFF"]}}

        with patch.dict(os.environ, {"FEATURE_ON": "yes", "FEATURE_OFF": "0"}, clear=True):
            status = provider_required_env_status(lane)
            [check] = check_required_env("custom-lane", lane)

        self.assertEqual(status.required_truthy, ("FEATURE_ON", "FEATURE_OFF"))
        self.assertEqual(status.missing_truthy, ("FEATURE_OFF",))
        self.assertEqual(check.status, "warn")
        self.assertEqual(check.detail["missing_truthy"], ["FEATURE_OFF"])

    def test_required_env_passes_when_all_values_are_present(self) -> None:
        lane = {
            "provider_config": {
                "required_env": ["PRESENT_TOKEN"],
                "required_env_truthy": ["FEATURE_ON"],
            }
        }

        with patch.dict(
            os.environ,
            {"PRESENT_TOKEN": "ok", "FEATURE_ON": "true"},
            clear=True,
        ):
            status = provider_required_env_status(lane)
            [check] = check_required_env("custom-lane", lane)

        self.assertTrue(status.all_present)
        self.assertEqual(check.status, "pass")
        self.assertEqual(check.detail["required_env"], ["PRESENT_TOKEN"])
        self.assertEqual(check.detail["required_env_truthy"], ["FEATURE_ON"])

    def test_required_env_any_or_truthy_allows_key_or_trust_flag(self) -> None:
        lane = {
            "provider_config": {
                "required_env_any": ["XAI_API_KEY", "GROK_DEPLOYMENT_KEY"],
                "required_env_truthy_any": ["GROK_BUILD_USE_AMBIENT_HOME"],
            }
        }

        with patch.dict(os.environ, {"XAI_API_KEY": "xai-key"}, clear=True):
            status = provider_required_env_status(lane)
            [check] = check_required_env("grok-build", lane)

        self.assertTrue(status.all_present)
        self.assertFalse(status.missing_any)
        self.assertEqual(check.status, "pass")

        with patch.dict(
            os.environ,
            {"GROK_BUILD_USE_AMBIENT_HOME": "1"},
            clear=True,
        ):
            status = provider_required_env_status(lane)
            [check] = check_required_env("grok-build", lane)

        self.assertTrue(status.all_present)
        self.assertFalse(status.missing_any)
        self.assertEqual(check.status, "pass")

        with patch.dict(os.environ, {"GROK_BUILD_USE_AMBIENT_HOME": "0"}, clear=True):
            status = provider_required_env_status(lane)
            [check] = check_required_env("grok-build", lane)

        self.assertFalse(status.all_present)
        self.assertTrue(status.missing_any)
        self.assertEqual(check.status, "warn")
        self.assertIn("one of:", check.message)
        self.assertEqual(check.detail["required_env_any"], ["XAI_API_KEY", "GROK_DEPLOYMENT_KEY"])
        self.assertEqual(
            check.detail["required_env_truthy_any"],
            ["GROK_BUILD_USE_AMBIENT_HOME"],
        )


if __name__ == "__main__":
    unittest.main()
