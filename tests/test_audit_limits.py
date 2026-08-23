import unittest

from code_mower import audit_limits


class AuditLimitsTests(unittest.TestCase):
    def test_size_aware_budget_scales_with_included_diff_size(self) -> None:
        self.assertEqual(
            audit_limits.resolve_audit_budget_usd(180_000),
            "2.00",
        )
        self.assertEqual(
            audit_limits.resolve_audit_budget_usd(1_000_000),
            "8.00",
        )
        self.assertEqual(
            audit_limits.resolve_audit_budget_usd(5_000_000),
            "10.00",
        )

    def test_explicit_budget_overrides_scaling(self) -> None:
        self.assertEqual(
            audit_limits.resolve_audit_budget_usd(
                1_000_000,
                explicit_budget_usd="3.5",
            ),
            "3.50",
        )

    def test_config_limits_parse_defaults_and_reject_bad_hard_limit(self) -> None:
        defaults = audit_limits.audit_limits_from_config({})
        self.assertEqual(defaults.max_diff_bytes, 180_000)
        self.assertEqual(defaults.max_diff_hard_limit_bytes, 1_500_000)
        self.assertEqual(defaults.budget_usd, "")

        with self.assertRaisesRegex(ValueError, "greater than or equal"):
            audit_limits.audit_limits_from_config(
                {
                    "audit": {
                        "max_diff_bytes": "2000",
                        "max_diff_hard_limit_bytes": "1000",
                    }
                }
            )


if __name__ == "__main__":
    unittest.main()
