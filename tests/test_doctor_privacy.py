import json
import unittest

from code_mower import doctor
from code_mower.doctor_checks import privacy, runtime


class DoctorPrivacyTests(unittest.TestCase):
    def test_auth_probe_output_detail_redacts_content(self) -> None:
        detail = privacy.auth_probe_output_detail("user@example.com\nscope repo\n")

        self.assertEqual(detail, {"output_redacted": True, "output_line_count": 2})
        self.assertNotIn("user@example.com", json.dumps(detail))
        self.assertNotIn("scope repo", json.dumps(detail))

    def test_empty_auth_probe_output_is_not_marked_redacted(self) -> None:
        self.assertEqual(
            privacy.auth_probe_output_detail(" \n "),
            {"output_redacted": False, "output_line_count": 0},
        )

    def test_compatibility_exports_use_privacy_helper(self) -> None:
        output = "secret-ish output"

        self.assertEqual(
            runtime.auth_probe_output_detail(output),
            privacy.auth_probe_output_detail(output),
        )
        self.assertEqual(
            doctor._auth_probe_output_detail(output),
            privacy.auth_probe_output_detail(output),
        )


if __name__ == "__main__":
    unittest.main()
