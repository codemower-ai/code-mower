import os
import unittest
from pathlib import Path
from unittest.mock import patch

from code_mower import coderabbit_cli_audit_pr, gemini_cli_audit_pr, hermes_cli_audit_pr
from code_mower.provider_runners.process import build_allowlisted_child_env


class ProviderRunnersProcessTests(unittest.TestCase):
    def test_allowlisted_env_copies_only_requested_ambient_keys(self) -> None:
        with patch.dict(
            os.environ,
            {"KEEP_ME": "yes", "DROP_ME": "no", "EMPTY_VALUE": ""},
            clear=True,
        ):
            child_env = build_allowlisted_child_env(
                ("KEEP_ME", "DROP_ME", "EMPTY_VALUE"),
                exclude_env=("DROP_ME",),
            )

        self.assertEqual(child_env, {"KEEP_ME": "yes"})

    def test_home_env_is_isolated_and_stringified_by_default(self) -> None:
        home_dir = Path("/tmp/code-mower-provider-home")
        with patch.dict(
            os.environ,
            {"HOME": "/ambient-home", "XDG_CONFIG_HOME": "/ambient-config"},
            clear=True,
        ):
            child_env = build_allowlisted_child_env(
                ("PATH",),
                home_env={
                    "HOME": home_dir,
                    "XDG_CONFIG_HOME": home_dir / ".config",
                    "XDG_CACHE_HOME": home_dir / ".cache",
                },
            )

        self.assertEqual(child_env["HOME"], str(home_dir))
        self.assertEqual(child_env["XDG_CONFIG_HOME"], str(home_dir / ".config"))
        self.assertEqual(child_env["XDG_CACHE_HOME"], str(home_dir / ".cache"))

    def test_preserve_ambient_home_uses_selected_ambient_keys(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HOME": "/ambient-home",
                "XDG_CONFIG_HOME": "/ambient-config",
                "XDG_CACHE_HOME": "/ambient-cache",
                "XDG_STATE_HOME": "/ambient-state",
            },
            clear=True,
        ):
            child_env = build_allowlisted_child_env(
                ("PATH",),
                home_env={"HOME": "/isolated-home"},
                preserve_ambient_home=True,
            )

        self.assertEqual(child_env["HOME"], "/ambient-home")
        self.assertEqual(child_env["XDG_CONFIG_HOME"], "/ambient-config")
        self.assertEqual(child_env["XDG_CACHE_HOME"], "/ambient-cache")
        self.assertEqual(child_env["XDG_STATE_HOME"], "/ambient-state")

    def test_extra_env_overrides_ambient_values_and_ignores_none(self) -> None:
        with patch.dict(os.environ, {"API_KEY": "ambient", "PATH": "/bin"}, clear=True):
            child_env = build_allowlisted_child_env(
                ("API_KEY", "PATH"),
                extra_env={"API_KEY": "explicit", "SKIP_ME": None, "CACHE_DIR": Path("/tmp/cache")},
            )

        self.assertEqual(child_env["API_KEY"], "explicit")
        self.assertEqual(child_env["PATH"], "/bin")
        self.assertEqual(child_env["CACHE_DIR"], "/tmp/cache")
        self.assertNotIn("SKIP_ME", child_env)

    def test_gemini_child_env_keeps_isolated_home_and_explicit_key_mapping(self) -> None:
        home_dir = Path("/tmp/code-mower-gemini-home")
        with patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "HOME": "/ambient-home",
                "GEMINI_API_KEY": "ambient-key",
                "UNRELATED_SECRET": "do-not-copy",
            },
            clear=True,
        ):
            child_env = gemini_cli_audit_pr.build_gemini_child_env(
                home_dir,
                gemini_api_key="explicit-key",
                exclude_env=("PATH",),
            )

        self.assertNotIn("PATH", child_env)
        self.assertEqual(child_env["GEMINI_API_KEY"], "explicit-key")
        self.assertEqual(child_env["GOOGLE_API_KEY"], "explicit-key")
        self.assertEqual(child_env["HOME"], str(home_dir))
        self.assertEqual(child_env["XDG_CONFIG_HOME"], str(home_dir / ".config"))
        self.assertNotIn("UNRELATED_SECRET", child_env)

    def test_gemini_child_env_can_preserve_ambient_home_for_logged_in_cli(self) -> None:
        home_dir = Path("/tmp/code-mower-gemini-home")
        with patch.dict(
            os.environ,
            {"HOME": "/ambient-home", "XDG_CONFIG_HOME": "/ambient-config"},
            clear=True,
        ):
            child_env = gemini_cli_audit_pr.build_gemini_child_env(
                home_dir,
                preserve_ambient_home=True,
            )

        self.assertEqual(child_env["HOME"], "/ambient-home")
        self.assertEqual(child_env["XDG_CONFIG_HOME"], "/ambient-config")

    def test_hermes_child_env_keeps_deterministic_flags_and_home(self) -> None:
        home_dir = Path("/tmp/code-mower-hermes-home")
        with patch.dict(
            os.environ,
            {"PATH": "/usr/bin", "HOME": "/ambient-home", "HERMES_HOME": "/ambient-hermes"},
            clear=True,
        ):
            child_env = hermes_cli_audit_pr.build_hermes_child_env(
                home_dir,
                preserve_ambient_home=False,
            )

        self.assertEqual(child_env["PATH"], "/usr/bin")
        self.assertEqual(child_env["HOME"], str(home_dir))
        self.assertEqual(child_env["HERMES_HOME"], str(home_dir / ".hermes"))
        self.assertEqual(child_env["HERMES_IGNORE_USER_CONFIG"], "1")
        self.assertEqual(child_env["HERMES_IGNORE_RULES"], "1")
        self.assertEqual(child_env["HERMES_CORE_TOOLS"], "")
        self.assertEqual(child_env["HERMES_TOOL_PROGRESS"], "0")
        self.assertEqual(child_env["HERMES_QUIET"], "1")

    def test_hermes_child_env_can_preserve_ambient_home_for_logged_in_cli(self) -> None:
        home_dir = Path("/tmp/code-mower-hermes-home")
        with patch.dict(
            os.environ,
            {"HOME": "/ambient-home", "HERMES_HOME": "/ambient-hermes"},
            clear=True,
        ):
            child_env = hermes_cli_audit_pr.build_hermes_child_env(
                home_dir,
                preserve_ambient_home=True,
            )

        self.assertEqual(child_env["HOME"], "/ambient-home")
        self.assertEqual(child_env["HERMES_HOME"], "/ambient-hermes")

    def test_coderabbit_child_env_copies_only_its_allowlist(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CODERABBIT_API_KEY": "coderabbit-key",
                "PATH": "/usr/bin",
                "HOME": "/ambient-home",
                "UNRELATED_SECRET": "do-not-copy",
            },
            clear=True,
        ):
            child_env = coderabbit_cli_audit_pr.build_coderabbit_child_env()

        self.assertEqual(child_env["CODERABBIT_API_KEY"], "coderabbit-key")
        self.assertEqual(child_env["PATH"], "/usr/bin")
        self.assertEqual(child_env["HOME"], "/ambient-home")
        self.assertNotIn("UNRELATED_SECRET", child_env)


if __name__ == "__main__":
    unittest.main()
