import unittest

from code_mower.doctor_checks.github_actions_failure_selection import (
    _actions_job_context,
    _actions_run_context,
    _incomplete_inspection_detail,
    _is_inspectable_actions_failure,
)


class ActionsFailureSelectionTests(unittest.TestCase):
    def test_inspectable_failure_accepts_only_failed_action_records(self) -> None:
        self.assertTrue(_is_inspectable_actions_failure({"conclusion": "failure"}))
        self.assertTrue(_is_inspectable_actions_failure({"conclusion": "timed_out"}))
        self.assertFalse(_is_inspectable_actions_failure({"conclusion": "success"}))
        self.assertFalse(_is_inspectable_actions_failure("not-a-mapping"))

    def test_run_and_job_context_normalizes_missing_strings(self) -> None:
        self.assertEqual(
            _actions_run_context({"id": 12, "name": None, "head_sha": "abc"}),
            {"run_id": 12, "workflow": "", "head_sha": "abc"},
        )
        self.assertEqual(
            _actions_job_context({"id": 34, "name": None}),
            {"job_id": 34, "job": ""},
        )

    def test_incomplete_detail_keeps_ids_and_omits_empty_fields(self) -> None:
        self.assertEqual(
            _incomplete_inspection_detail(
                stage="annotations",
                run_id=0,
                workflow="",
                job_id=0,
                job="package",
                reason="missing_check_run_id",
                limit=None,
            ),
            {
                "stage": "annotations",
                "run_id": 0,
                "job_id": 0,
                "job": "package",
                "reason": "missing_check_run_id",
            },
        )


if __name__ == "__main__":
    unittest.main()
