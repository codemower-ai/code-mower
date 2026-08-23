from __future__ import annotations

import unittest

from code_mower import prompts


class PromptDoctrineTests(unittest.TestCase):
    def test_base_audit_lens_requires_base_tree_check_before_missing_file_claim(self) -> None:
        prompt = prompts.load_review_prompt(("base-audit",))
        normalized_prompt = " ".join(prompt.split())

        self.assertIn("already exists in the base tree", normalized_prompt)
        self.assertIn("surrounding checkout context", normalized_prompt)
        self.assertIn("acknowledged by decision <id>", normalized_prompt)
        self.assertIn("never block on it", normalized_prompt)
        self.assertIn("contradict a prior verdict", normalized_prompt)

    def test_commerce_scaffold_lenses_guard_shared_sandbox_namespaces(self) -> None:
        prompt = prompts.load_review_prompt(("security-threat-model", "operability"))
        normalized_prompt = " ".join(prompt.split())

        self.assertIn("sandbox/test credentials can share object-id namespaces", normalized_prompt)
        self.assertIn("live parent-account namespaces", normalized_prompt)
        self.assertIn("read existing resources before create", normalized_prompt)
        self.assertIn("idempotency keys", normalized_prompt)


if __name__ == "__main__":
    unittest.main()
