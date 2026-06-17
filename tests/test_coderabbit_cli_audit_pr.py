import unittest
from unittest import mock

from code_mower import coderabbit_cli_audit_pr


class CoderabbitCliAuditPrTests(unittest.TestCase):
    def test_fetch_pull_request_delegates_to_shared_provider_helper(self) -> None:
        with mock.patch.object(
            coderabbit_cli_audit_pr,
            "_fetch_pull_request",
            return_value={"head": {"sha": "abc123"}},
        ) as fetch:
            payload = coderabbit_cli_audit_pr.fetch_pull_request(
                "owner/repo",
                42,
                token="secret",
            )

        self.assertEqual(payload["head"]["sha"], "abc123")
        fetch.assert_called_once_with("owner/repo", 42, token="secret")

    def test_fetch_pull_request_rejects_non_object_response(self) -> None:
        with mock.patch.object(
            coderabbit_cli_audit_pr,
            "_fetch_pull_request",
            return_value=["not", "object"],
        ):
            with self.assertRaisesRegex(
                ValueError,
                "GitHub pull request response was not an object",
            ):
                coderabbit_cli_audit_pr.fetch_pull_request(
                    "owner/repo",
                    42,
                    token="secret",
                )


if __name__ == "__main__":
    unittest.main()
