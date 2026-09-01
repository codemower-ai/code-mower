from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from code_mower import builder_experiment
from code_mower.cloud_client import EVENT_SCHEMA, parse_event_args

SPEC = {
    "version": 1,
    "name": "executor-test",
    "tasks": [{"task_id": "small-doc", "repo": "codemower-ai/code-mower", "task_class": "docs", "review_classes": ["docs"]}],
    "builders": [{"builder_id": "codex-local", "provider": "codex", "tool": "codex-cli", "model": "gpt-5"}],
}


class BuilderExperimentExecutorTest(unittest.TestCase):
    def test_executor_records_source_free_authoring_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = builder_experiment.build_plan(SPEC, output_dir=root / "runs")
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            run_id = str(plan["runs"][0]["run_id"])

            def invoke(output: Path, *args: str) -> dict[str, object]:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = builder_experiment.main(
                        [
                            "run",
                            str(plan_path),
                            "--run-id",
                            run_id,
                            "--output",
                            str(output),
                            *args,
                        ]
                    )
                self.assertEqual(code, 0)
                return json.loads(output.read_text(encoding="utf-8"))

            marker = root / "should-not-exist.txt"
            dry = invoke(
                root / "dry.json",
                "--dry-run",
                "--cwd",
                str(root),
                "--",
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
            )
            self.assertFalse(marker.exists())
            self.assertEqual(dry["schema"], "code_mower.authoringRun.v1")
            self.assertEqual(dry["status"], "planned")
            self.assertTrue(dry["executor"]["dry_run"])  # type: ignore[index]

            success_path = root / "success.json"
            success = invoke(
                success_path,
                "--branch",
                "codex/example",
                "--",
                sys.executable,
                "-c",
                "print('SECRET_OUTPUT')",
            )
            raw_success = success_path.read_text(encoding="utf-8")
            self.assertNotIn("SECRET_OUTPUT", raw_success)
            self.assertEqual(success["status"], "completed")
            self.assertEqual(success["branch"], "codex/example")
            self.assertEqual(success["executor"]["exit_code"], 0)  # type: ignore[index]
            self.assertFalse(success["privacy"]["raw_stdout_stderr"])  # type: ignore[index]
            cloud_events = parse_event_args([f"builder_run={success_path}"])
            self.assertEqual(len(cloud_events), 1)
            self.assertEqual(cloud_events[0]["schema"], EVENT_SCHEMA)
            self.assertEqual(cloud_events[0]["event_type"], "builder_run")
            self.assertEqual(cloud_events[0]["source"], "code-mower-builder-experiment")
            self.assertEqual(cloud_events[0]["metrics"]["elapsed_seconds"], success["elapsed_seconds"])
            self.assertEqual(
                cloud_events[0]["dimensions"]["command_output_capture"],
                "disabled",
            )
            self.assertNotIn("SECRET_OUTPUT", json.dumps(cloud_events[0]))
            normalized_cloud_event = json.dumps(cloud_events[0])
            self.assertNotIn("command_executable", normalized_cloud_event)
            self.assertNotIn('"stdout_stderr":', normalized_cloud_event)
            self.assertNotIn('"raw_stdout_stderr":', normalized_cloud_event)

            failure = invoke(
                root / "failure.json",
                "--",
                sys.executable,
                "-c",
                "raise SystemExit(3)",
            )
            self.assertEqual(failure["status"], "failed")
            self.assertEqual(failure["executor"]["exit_code"], 3)  # type: ignore[index]

            report = builder_experiment.build_report(plan, [failure])
            self.assertEqual(report["reported_run_count"], 1)
            self.assertEqual(report["builders"]["codex-local"]["failed_runs"], 1)

            stderr = io.StringIO()
            bad_output = root / "missing-executable.json"
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                code = builder_experiment.main(
                    [
                        "run",
                        str(plan_path),
                        "--run-id",
                        run_id,
                        "--output",
                        str(bad_output),
                        "--",
                        "definitely-missing-code-mower-builder",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertFalse(bad_output.exists())
            self.assertIn("error: unable to start builder command", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
