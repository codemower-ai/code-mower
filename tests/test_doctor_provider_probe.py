import json
import unittest

from code_mower.doctor_checks import provider_probe


class ProviderProbeTests(unittest.TestCase):
    def test_parse_probe_json_extracts_object_from_noisy_output(self) -> None:
        payload, detail = provider_probe.parse_probe_json(
            "startup warning\n{\"result\":\"ok\"}\n"
        )

        self.assertEqual(payload, {"result": "ok"})
        self.assertTrue(detail["json_parsed"])
        self.assertTrue(detail["json_extracted"])

    def test_json_field_reads_nested_values(self) -> None:
        self.assertEqual(provider_probe.json_field({"a": {"b": 3}}, "a.b"), 3)
        self.assertIsNone(provider_probe.json_field({"a": {}}, "a.b"))

    def test_evaluate_json_probe_detects_auth_without_raw_output_leak(self) -> None:
        status, message, detail = provider_probe.evaluate_json_probe(
            {
                "doctor_probe_error_fields": ("is_error", "api_error_status"),
                "doctor_probe_auth_status_fields": ("api_error_status",),
                "doctor_probe_expect_json_field": "result",
                "doctor_probe_expect_json_value": "ok",
            },
            json.dumps(
                {
                    "is_error": True,
                    "api_error_status": 401,
                    "result": "Invalid authentication credentials",
                }
            ),
            returncode=0,
        )

        self.assertEqual(status, "warn")
        self.assertEqual(message, "probe reported provider authentication failure")
        self.assertEqual(detail["auth_status_code"], "401")
        self.assertNotIn("Invalid authentication credentials", json.dumps(detail))


if __name__ == "__main__":
    unittest.main()
