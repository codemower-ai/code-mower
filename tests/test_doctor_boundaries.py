import ast
import importlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DoctorBoundaryTests(unittest.TestCase):
    def test_doctor_cli_adapter_does_not_own_check_implementations(self) -> None:
        tree = ast.parse((ROOT / "src/code_mower/doctor.py").read_text(encoding="utf-8"))
        imported_roots: set[str] = set()
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_roots.add(alias.name.split(".", 1)[0])
                    imported_modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
                imported_modules.add(node.module)

        self.assertFalse(
            {
                "os",
                "shutil",
                "subprocess",
                "urllib",
            }
            & imported_roots,
            "doctor.py should stay a CLI adapter; runtime/network checks belong in doctor_checks",
        )
        self.assertFalse(
            {
                "code_mower.doctor_checks.github",
                "code_mower.doctor_checks.github_actions",
                "code_mower.doctor_checks.github_api",
                "code_mower.doctor_checks.provider_api_model",
                "code_mower.doctor_checks.provider_local_cli",
                "code_mower.doctor_checks.provider_probe",
                "code_mower.doctor_checks.privacy",
            }
            & imported_modules,
            "doctor.py should import doctor_checks facade exports, not implementation modules",
        )

    def test_doctor_check_modules_are_explicit_package_seams(self) -> None:
        expected_modules = (
            "code_mower.doctor_checks.cloud",
            "code_mower.doctor_checks.github",
            "code_mower.doctor_checks.github_actions",
            "code_mower.doctor_checks.github_api",
            "code_mower.doctor_checks.output",
            "code_mower.doctor_checks.provider_api_model",
            "code_mower.doctor_checks.provider_env",
            "code_mower.doctor_checks.provider_local_cli",
            "code_mower.doctor_checks.provider_probe",
            "code_mower.doctor_checks.providers",
            "code_mower.doctor_checks.privacy",
            "code_mower.doctor_checks.registry",
            "code_mower.doctor_checks.runner",
            "code_mower.doctor_checks.runtime",
        )
        for module_name in expected_modules:
            with self.subTest(module=module_name):
                self.assertIsNotNone(importlib.import_module(module_name))


if __name__ == "__main__":
    unittest.main()
