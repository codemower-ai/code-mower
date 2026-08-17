from __future__ import annotations

import unittest

from code_mower import prompts


class PromptDoctrineTests(unittest.TestCase):
    def test_base_audit_lens_requires_base_tree_check_before_missing_file_claim(self) -> None:
        prompt = prompts.load_review_prompt(("base-audit",))
        normalized_prompt = " ".join(prompt.split())

        self.assertIn("already exists in the base tree", normalized_prompt)
        self.assertIn("surrounding checkout context", normalized_prompt)


if __name__ == "__main__":
    unittest.main()
