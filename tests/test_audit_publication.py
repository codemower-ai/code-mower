"""Adversarial transport/publication and real consumer tests for local audits."""

from __future__ import annotations

from copy import deepcopy
from functools import partial
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from code_mower import audit_publication as pub, audit_labeler_lib as lib
from code_mower import claude_audit_pr, codex_audit_pr, config, init, package
from code_mower import trailer_comment_labeler as labeler
from code_mower.builder_lineage import Chain, Episode, Target, render as render_lineage

ROOT = Path(__file__).resolve().parents[1]
REPO = "owner/repo"
HEAD = "b" * 40
SOURCE = "a" * 40
NOW = 1789698000
REPOSITORY = {"id": 1234, "full_name": REPO, "default_branch": "main"}


def artifact(lane="claude", verdict="PASS", **changes):
    return (
        dict(
            schema=pub.SCHEMA,
            repository_id=1234,
            pr_number=42,
            head_sha_start=HEAD,
            head_sha_end=HEAD,
            lane=lane,
            verdict=verdict,
            created_at=NOW,
            source_run_id=700,
            source_run_attempt=1,
        )
        | changes
    )


def run_for(value, *, terminal=False, comment_id=91, **changes):
    run = dict(
        id=800,
        path=pub.WORKFLOW,
        event="repository_dispatch",
        run_attempt=1,
        repository=REPOSITORY,
        head_repository=REPOSITORY,
        head_branch="main",
        head_sha=SOURCE,
        name=pub.WORKFLOW_NAME,
        display_title=pub.EVENT,
        status="completed" if terminal else "in_progress",
        conclusion="success" if terminal else None,
    )
    run["publication_jobs"] = [
        {
            "run_id": run["id"],
            "name": pub.receipt_name(value, comment_id),
            "status": "completed",
            "conclusion": "success",
        }
    ]
    return run | changes


def event_for(value):
    text = pub.canonical(value)
    return {
        "action": pub.EVENT,
        "repository": REPOSITORY,
        "client_payload": {
            "artifact": text,
            "digest": pub.digest(text),
            "pr_number": value["pr_number"],
        },
    }


def environment():
    return dict(
        GITHUB_EVENT_NAME="repository_dispatch",
        GITHUB_REF="refs/heads/main",
        GITHUB_WORKFLOW_REF=f"{REPO}/{pub.WORKFLOW}@refs/heads/main",
        GITHUB_SHA=SOURCE,
        GITHUB_RUN_ID="800",
        GITHUB_RUN_ATTEMPT="1",
    )


def pull(value, *, builder=None):
    builder = builder or ("codex" if value["lane"] == "claude" else "claude")
    return dict(
        number=42,
        state="open",
        head={"sha": HEAD, "ref": f"{builder}/topic"},
        base={"sha": SOURCE, "repo": REPOSITORY},
        user={"login": "same-owner"},
        labels=[{"name": "builder:" + builder}, {"name": "needs-" + value["lane"] + "-audit"}],
    )


def source_for(value):
    return dict(
        id=value["source_run_id"],
        path=pub.SOURCE_WORKFLOW,
        event="repository_dispatch",
        run_attempt=1,
        head_sha=SOURCE,
        head_branch="main",
        repository=REPOSITORY,
        head_repository=REPOSITORY,
        pull_requests=[dict(number=42, base=dict(ref="main", repo=REPOSITORY))],
        jobs=[
            dict(
                run_id=value["source_run_id"],
                name=f"audit ({value['lane']})",
                steps=[dict(name=pub.seal_name(value), status="completed", conclusion="success")],
            )
        ],
    )


class MemoryGitHub:
    repo = REPO

    def __init__(self, value=None):
        self.value = value or artifact()
        self.repository = deepcopy(REPOSITORY)
        self.run = run_for(self.value)
        self.source = source_for(self.value)
        self.pr = pull(self.value)
        self.comments = []
        self.runs = [self.run]
        self.writes = []
        self.pr_reads = 0
        self.move_at = None
        self.fail_patch = False
        self.dispatched = False
        self.auto_publish = False

    def request(self, path, *, method="GET", body=None):
        if method == "GET":
            if path == "":
                return deepcopy(self.repository)
            if path == "/pulls/42":
                self.pr_reads += 1
                if self.move_at == self.pr_reads:
                    self.pr["head"]["sha"] = "c" * 40
                return deepcopy(self.pr)
            if path == "/actions/runs/700":
                return deepcopy(self.source)
            if path.startswith("/actions/runs/"):
                number = int(path.rsplit("/", 1)[1])
                return deepcopy(next((run for run in self.runs if run["id"] == number), self.run))
            raise AssertionError(path)
        self.writes.append((method, path, body))
        if path == "/dispatches":
            self.dispatched = True
            if self.auto_publish:
                pub.publish(
                    {
                        "action": pub.EVENT,
                        "repository": REPOSITORY,
                        "client_payload": body["client_payload"],
                    },
                    environment(),
                    self,
                    now=self.value["created_at"],
                )
                self.run.update(status="completed", conclusion="success")
            return None
        if method == "POST":
            comment = dict(
                id=91,
                user={"login": "github-actions[bot]"},
                body=body["body"],
                html_url=f"https://github.com/{REPO}/pull/42#issuecomment-91",
            )
            self.comments.append(comment)
            return deepcopy(comment)
        if method == "PATCH":
            if self.fail_patch and pub.MARKER in body["body"]:
                raise RuntimeError("fixture failure")
            self.comments[-1]["body"] = body["body"]
            return deepcopy(self.comments[-1])
        raise AssertionError((path, method))

    def pages(self, path, key=None):
        if path.startswith("/actions/workflows/"):
            return deepcopy(self.runs)
        if path.endswith("/attempts/1/jobs"):
            return deepcopy(self.source["jobs"])
        if path.endswith("/jobs"):
            number = int(path.split("/")[-2])
            return deepcopy(
                next((run for run in self.runs if run["id"] == number), self.run)[
                    "publication_jobs"
                ]
            )
        if path == "/issues/42/comments":
            return deepcopy(self.comments)
        raise AssertionError(path)


class ContractTests(unittest.TestCase):
    def test_review_request_validates_live_target_before_provider_work(self):
        api = MemoryGitHub()
        api.pr["head"]["repo"] = REPOSITORY
        api.pr["base"]["ref"] = "main"
        env = environment() | {
            "GITHUB_WORKFLOW_REF": f"{REPO}/{pub.SOURCE_WORKFLOW}@refs/heads/main"
        }
        event = dict(
            action="code-mower-local-review",
            repository=REPOSITORY,
            client_payload=dict(pr_number=42, head_sha=HEAD),
        )
        self.assertEqual(pub.prepare_review(event, env, api), event["client_payload"])
        for changes in (
            {"pr_number": True},
            {"pr_number": "42"},
            {"head_sha": "c" * 40},
            {"head_sha": "short"},
            {"raw_output": "PRIVATE"},
        ):
            with self.subTest(changes=changes), self.assertRaises(pub.Refused):
                pub.prepare_review(
                    event | {"client_payload": event["client_payload"] | changes}, env, api
                )
        for changes in (
            {"GITHUB_REF": "refs/heads/evil"},
            {"GITHUB_RUN_ATTEMPT": "2"},
            {"GITHUB_EVENT_NAME": "workflow_dispatch"},
        ):
            with self.subTest(changes=changes), self.assertRaises(pub.Refused):
                pub.prepare_review(event, env | changes, api)
        api.pr["head"]["repo"] = {"id": 999}
        with self.assertRaises(pub.Refused):
            pub.prepare_review(event, env, api)
        self.assertEqual(api.writes, [])

    def test_fabricated_or_mismatched_source_proof_never_publishes(self):
        mutations = [
            lambda s: s.update(id=701),
            lambda s: s.update(path=".github/workflows/builder.yml"),
            lambda s: s.update(event="workflow_dispatch"),
            lambda s: s.update(event="pull_request"),
            lambda s: s.update(run_attempt=2),
            lambda s: s.update(head_sha="invalid"),
            lambda s: s.update(head_branch="builder/evil"),
            lambda s: s.update(repository={"id": 999}),
            lambda s: s.update(head_repository={"id": 999}),
            lambda s: s.update(event="pull_request_target"),
            lambda s: s["jobs"][0].update(name="audit (codex)"),
            lambda s: s["jobs"][0].update(run_id=701),
            lambda s: s["jobs"][0].update(steps=[]),
            lambda s: s["jobs"][0]["steps"][0].update(name="Code Mower reviewer seal " + "0" * 64),
            lambda s: s["jobs"][0]["steps"][0].update(status="in_progress"),
            lambda s: s["jobs"][0]["steps"][0].update(conclusion="failure"),
            lambda s: s["jobs"].append(deepcopy(s["jobs"][0])),
        ]
        for index, mutate in enumerate(mutations):
            api = MemoryGitHub()
            mutate(api.source)
            with self.subTest(index=index), self.assertRaises(pub.Refused):
                pub.publish(event_for(api.value), environment(), api, now=NOW)
            self.assertEqual(api.writes, [])
        # Even a correctly hashed fabricated PASS cannot reuse a BLOCKED seal.
        api = MemoryGitHub(artifact(verdict="BLOCKED"))
        with self.assertRaises(pub.Refused):
            pub.publish(event_for(artifact(verdict="PASS")), environment(), api, now=NOW)
        self.assertEqual(api.writes, [])

    def test_canonical_exact_schema_digest_and_field_types(self):
        value = artifact()
        for lane in ("claude", "codex"):
            for verdict in ("PASS", "BLOCKED"):
                text = pub.canonical(artifact(lane, verdict))
                self.assertEqual(pub.validate(text, pub.digest(text), now=NOW)["lane"], lane)
        bad = [
            dict(schema="v2"),
            dict(repository_id=True),
            dict(pr_number=False),
            dict(pr_number=0),
            dict(pr_number="42"),
            dict(repository_id=None),
            dict(lane="devin"),
            dict(lane=["claude"]),
            dict(verdict="UNKNOWN"),
            dict(verdict="pass"),
            dict(head_sha_end="c" * 40),
            dict(head_sha_start="b" * 7),
            dict(head_sha_start="B" * 40, head_sha_end="B" * 40),
            dict(created_at=NOW + 1),
            dict(created_at=NOW - pub.MAX_AGE - 1),
            dict(created_at=True),
            dict(source_run_id=True),
            dict(source_run_id=0),
            dict(source_run_attempt=2),
            dict(source_run_attempt=True),
            dict(source="private source"),
            dict(comment_body="raw findings"),
        ]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(pub.Refused):
                text = pub.canonical(value | change)
                pub.validate(text, pub.digest(text), now=NOW)
        text = pub.canonical(value)
        for raw, digest in (
            (text + "\n", pub.digest(text + "\n")),
            (text, "0" * 64),
            (text, "x" * 64),
            (json.dumps(value), pub.digest(json.dumps(value))),
            ('{"schema":"one","schema":"two"}', "0" * 64),
            ("x" * (pub.MAX_BYTES + 1), "0" * 64),
            ("[" * 1000, "0" * 64),
        ):
            with self.subTest(raw=raw[:30]), self.assertRaises(pub.Refused):
                pub.validate(raw, digest, now=NOW)

    def test_wrong_dispatch_bindings_never_write(self):
        cases = [
            ({"GITHUB_EVENT_NAME": "workflow_dispatch"}, None, None),
            ({"GITHUB_REF": "refs/heads/attacker"}, None, None),
            ({"GITHUB_WORKFLOW_REF": f"{REPO}/{pub.WORKFLOW}@refs/heads/attacker"}, None, None),
            ({"GITHUB_RUN_ATTEMPT": "2"}, None, None),
            ({"GITHUB_SHA": "c" * 40}, None, None),
            ({"GITHUB_RUN_ID": "801"}, None, None),
            ({}, {"action": "wrong"}, None),
            ({}, {"repository": {"id": 999}}, None),
            ({}, None, {"pr_number": 43}),
            ({}, None, {"digest": "0" * 64}),
            ({}, None, {"raw_output": "private content"}),
        ]
        for env_change, event_change, payload_change in cases:
            api = MemoryGitHub()
            event = event_for(api.value)
            event.update(event_change or {})
            event["client_payload"].update(payload_change or {})
            with (
                self.subTest(case=cases.index((env_change, event_change, payload_change))),
                self.assertRaises(pub.Refused),
            ):
                pub.publish(event, environment() | env_change, api, now=NOW)
            self.assertEqual(api.writes, [])
        for target in (
            {"repository_id": 999},
            {"pr_number": 43},
            {"head_sha_start": "c" * 40, "head_sha_end": "c" * 40},
        ):
            api = MemoryGitHub()
            with self.subTest(target=target), self.assertRaises((pub.Refused, AssertionError)):
                pub.publish(event_for(artifact(**target)), environment(), api, now=NOW)
            self.assertEqual(api.writes, [])

    def test_run_provenance_refuses_untrusted_repository_workflow_and_ref(self):
        bad = [
            dict(id=900),
            dict(path=".github/workflows/evil.yml"),
            dict(path=None),
            dict(name="Other workflow", display_title=pub.WORKFLOW_NAME),
            dict(name=None),
            dict(event="workflow_dispatch"),
            dict(run_attempt=2),
            dict(head_branch="codex/topic"),
            dict(head_repository={"id": 90}),
            dict(repository={"id": 1234, "full_name": "other/repo"}),
            dict(head_sha="bad"),
        ]
        for change in bad:
            api = MemoryGitHub()
            api.run.update(change)
            with self.subTest(change=change), self.assertRaises(pub.Refused):
                pub.publish(event_for(api.value), environment(), api, now=NOW)
            self.assertEqual(api.writes, [])

    def test_repository_dispatch_title_does_not_define_publication_identity(self):
        for title in (pub.EVENT, pub.WORKFLOW_NAME, "Custom run title", None):
            with self.subTest(title=title):
                api = MemoryGitHub()
                api.run["display_title"] = title
                pub.publish(event_for(api.value), environment(), api, now=NOW)
                self.assertEqual([method for method, _, _ in api.writes], ["POST", "PATCH"])
                api.run.update(status="completed", conclusion="success")
                self.assertTrue(
                    pub.attested(
                        body=api.comments[0]["body"],
                        comment_id=91,
                        repo=REPO,
                        issue_number=42,
                        head_sha=HEAD,
                        run=api.run,
                        repository=REPOSITORY,
                    )
                )

    def test_head_moves_at_each_publication_boundary_fail_closed(self):
        for at in (1, 2, 3):
            api = MemoryGitHub()
            api.move_at = at
            with self.subTest(at=at), self.assertRaises(pub.Refused):
                pub.publish(event_for(api.value), environment(), api, now=NOW)
            self.assertFalse(any("AUDIT_STATE:" in c["body"] for c in api.comments))
            # Even a transient PATCH is untrusted until the successful receipt/run.
            for _, _, body in api.writes:
                if "AUDIT_STATE:" in body["body"]:
                    self.assertFalse(
                        pub.attested(
                            body=body["body"],
                            comment_id=91,
                            repo=REPO,
                            issue_number=42,
                            head_sha=HEAD,
                            run=api.run,
                            repository=REPOSITORY,
                        )
                    )

    def test_failure_cleanup_and_replay_ledger(self):
        api = MemoryGitHub()
        api.fail_patch = True
        with self.assertRaises(RuntimeError):
            pub.publish(event_for(api.value), environment(), api, now=NOW)
        self.assertNotIn("AUDIT_STATE:", api.comments[-1]["body"])
        api.fail_patch = False
        with self.assertRaises(pub.Refused):
            pub.publish(event_for(api.value), environment(), api, now=NOW)
        api = MemoryGitHub()
        pub.publish(event_for(api.value), environment(), api, now=NOW)
        first = deepcopy(api.run)
        first.update(conclusion="success", status="completed")
        api.comments = []  # Removing the comment does not remove the receipt ledger.
        api.runs = [first]
        api.run.update(id=801)
        with self.assertRaises(pub.Refused):
            pub.publish(
                event_for(api.value), environment() | {"GITHUB_RUN_ID": "801"}, api, now=NOW
            )
        self.assertEqual(len([w for w in api.writes if w[0] == "POST"]), 1)

    def test_receipt_binds_comment_and_prevents_recomputed_copy(self):
        value = artifact()
        run = run_for(value, terminal=True)
        body = pub.render(value, run, 91)

        def check(**changes):
            return pub.attested(
                **(
                    dict(
                        body=body,
                        comment_id=91,
                        repo=REPO,
                        issue_number=42,
                        head_sha=HEAD,
                        run=run,
                        repository=REPOSITORY,
                    )
                    | changes
                )
            )

        self.assertTrue(check())
        for changes in (
            dict(comment_id=92),
            dict(repo="other/repo"),
            dict(issue_number=43),
            dict(head_sha="c" * 40),
            dict(body=body + "extra"),
            dict(body=pub.render(value, run, 92), comment_id=92),
            dict(body=pub.render(artifact("codex"), run, 91)),
            dict(body=pub.render(artifact(verdict="BLOCKED"), run, 91)),
        ):
            with self.subTest(changes=changes):
                self.assertFalse(check(**changes))
        for changes in (
            dict(status="in_progress"),
            dict(conclusion="failure"),
            dict(name="Other workflow", display_title=pub.WORKFLOW_NAME),
            dict(name=None),
            dict(path=".github/workflows/evil.yml"),
            dict(path=None),
            dict(run_attempt=2),
            dict(publication_jobs=[]),
            dict(publication_jobs=[run["publication_jobs"][0]] * 2),
            dict(publication_jobs=[run["publication_jobs"][0] | {"run_id": 900}]),
        ):
            self.assertFalse(check(run=run | changes))

    def test_workflow_has_no_unvalidated_output_or_pr_execution(self):
        text = (ROOT / "templates/workflows/local-audit-publication.yml.j2").read_text()
        self.assertNotIn("client_payload", text)
        self.assertNotIn("workflow_dispatch:", text)
        self.assertNotIn("pull_request", text)
        self.assertNotIn("upload-artifact", text)
        self.assertNotIn("secrets.", text)
        self.assertIn("ref: ${{ github.sha }}", text)
        self.assertIn("python3 tools/audit_publication.py publish", text)
        self.assertIn("cancel-in-progress: false", text)
        for name in (
            "local-audit-publication.yml.j2",
            "trailer-comment-labeler.yml.j2",
            "self-hosted-local-audit.yml.j2",
            "local-audit-request.yml.j2",
        ):
            self.assertEqual(
                (ROOT / "templates/workflows" / name).read_bytes(),
                (ROOT / "src/code_mower/templates/workflows" / name).read_bytes(),
            )


class WrapperTests(unittest.TestCase):
    def test_metadata_upload_imports_support_package_from_job_workspace(self):
        import yaml

        workflow = yaml.safe_load((ROOT / ".github/workflows/local-cli-audit.yml").read_text())
        step = next(
            s for s in workflow["jobs"]["audit"]["steps"]
            if s.get("name") == "Upload Code Mower audit metadata"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            support = root / "support"
            scripts = support / "scripts"
            scripts.mkdir(parents=True)
            selector = scripts / "dev-python"
            selector.write_bytes((ROOT / "scripts/dev-python").read_bytes())
            selector.chmod(0o755)
            for directory, source in (
                (support / "src", "import sys; print(sys.argv[2])\n"),
                (root / "ambient", "raise AssertionError('wrong package')\n"),
            ):
                module = directory / "code_mower"
                module.mkdir(parents=True)
                (module / "__init__.py").write_text("")
                (module / "cli.py").write_text(source)
            env = dict(
                os.environ,
                SUPPORT_PATH=str(support),
                PR_HEAD_PATH=str(root / "pr-head"),
                PYTHONPATH=str(root / "ambient"),
                CODE_MOWER_PYTHON=sys.executable,
                CODE_MOWER_LOCAL_AUDIT_PATH=os.environ["PATH"],
                CODE_MOWER_CLOUD_TOKEN="fixture-token",
                CODE_MOWER_INSTALL_ID="fixture-install",
                CODE_MOWER_REVIEWER_SPEND_PATH=str(root / "spend.json"),
                RUNNER_TEMP=str(root),
                GITHUB_REPOSITORY=REPO,
                PR_NUMBER="42",
            )
            result = subprocess.run(
                ["bash", "-c", step["run"]], cwd=root, env=env, capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ["reviewer-runs", "dogfood"])
            self.assertNotIn("fixture-token", result.stdout + result.stderr)

    def test_real_source_step_seals_blocked_without_leaking_tokens_or_output(self):
        import yaml

        workflow = yaml.safe_load((ROOT / ".github/workflows/local-cli-audit.yml").read_text())
        step = next(s for s in workflow["jobs"]["audit"]["steps"] if s.get("id") == "run_audit")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gh").write_text("#!/bin/bash\nprintf 'needs-claude-audit\\n'\n")
            (root / "wrapper").write_text("""#!/bin/bash
set -euo pipefail
test -z "${DISPATCH_TOKEN:-}"
test -z "${CUSTOM_TOKEN:-}"
read -r token
test "$token" = short-fixture
echo 'PRIVATE_SOURCE /local/path raw-provider-output'
echo '::set-output name=digest::forged'
printf '{}' > "$CODE_MOWER_AUDIT_STAGE_PATH"
exit 0
""")
            for name in ("gh", "wrapper"):
                (root / name).chmod(0o755)
            env = dict(
                os.environ,
                CODE_MOWER_LOCAL_AUDIT_PATH=str(root),
                CODE_MOWER_LOCAL_AUDIT_LANE="claude",
                GITHUB_EVENT_NAME="repository_dispatch",
                CODE_MOWER_LOCAL_AUDIT_NEEDS_LABEL="needs-claude-audit",
                CODE_MOWER_LOCAL_AUDIT_DISPLAY_NAME="Claude",
                CODE_MOWER_LOCAL_AUDIT_TOKEN_ENV="CUSTOM_TOKEN",
                CODE_MOWER_LOCAL_AUDIT_SCRIPT="wrapper",
                SUPPORT_PATH=str(root),
                RUNNER_TEMP=str(root),
                PR_NUMBER="42",
                PR_HEAD_PATH=str(root),
                GITHUB_REPOSITORY=REPO,
                GITHUB_RUN_ID="700",
                GITHUB_TOKEN="short-fixture",
                DISPATCH_TOKEN="long-fixture",
                CUSTOM_TOKEN="long-fixture",
                GITHUB_OUTPUT=str(root / "output"),
                CODE_MOWER_AUDIT_STAGE_PATH=str(root / "stage"),
            )
            result = subprocess.run(
                ["bash", "-c", step["run"]], env=env, capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "output").read_text(), "audited=true\n")
            self.assertNotIn("PRIVATE_SOURCE", result.stdout + result.stderr)
            self.assertNotIn("forged", result.stdout + result.stderr)
            self.assertEqual(
                (root / "code-mower-reviewer-700-claude.log").stat().st_mode & 0o777, 0o600
            )

    def test_workflow_failures_publish_only_visible_non_authoritative_metadata(self):
        for lane, module in (("claude", claude_audit_pr), ("codex", codex_audit_pr)):
            for change in (
                {"verdict": "STALE"},
                {"verdict": "UNKNOWN"},
                {"quarantined": True, "quarantine_reason": "PRIVATE /local/secret"},
            ):
                with self.subTest(lane=lane, change=change), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "local.json"
                    path.write_text(json.dumps(self.local(lane, **change)))
                    with (
                        patch.object(pub, "submit") as submit,
                        patch.object(module, "post_pr_comment") as post,
                    ):
                        _, body = module._post_audit_comment(
                            REPO,
                            42,
                            "PRIVATE",
                            token="fixture",
                            actions_run_id="800",
                            publication="workflow",
                            artifact_path=path,
                        )
                    post.assert_called_once_with(REPO, 42, body, token="fixture")
                    submit.assert_not_called()
                    self.assertIn("Verdict: UNKNOWN", body)
                    self.assertIn("requeue", body)
                    for private in ("AUDIT_STATE:", "AUDIT_RUN:", "PRIVATE", "/local/", REPO):
                        self.assertNotIn(private, body)

    def test_provider_children_and_token_resolver_remove_automation_credentials(self):
        from io import StringIO
        from code_mower.provider_runners import github_auth

        env = dict(
            GITHUB_TOKEN="short",
            GH_TOKEN="short",
            DISPATCH_TOKEN="long",
            CODE_MOWER_LOCAL_AUDIT_TOKEN_ENV="CUSTOM_TOKEN",
            CUSTOM_TOKEN="long",
            GITHUB_OUTPUT="output",
            GITHUB_ENV="env",
            GITHUB_STATE="state",
            GITHUB_STEP_SUMMARY="summary",
            GITHUB_PATH="path",
            ACTIONS_RUNTIME_TOKEN="runtime",
            ACTIONS_ID_TOKEN_REQUEST_TOKEN="oidc",
            ACTIONS_ID_TOKEN_REQUEST_URL="oidc-url",
            CODE_MOWER_AUDIT_STAGE_PATH="stage",
            USER="reviewer",
        )
        for child in (
            claude_audit_pr._claude_env,
            lambda: codex_audit_pr._build_subprocess_env(None),
        ):
            with patch.dict(os.environ, env, clear=True):
                actual = child()
            self.assertEqual(actual["USER"], "reviewer")
            for key in github_auth.provider_unset_env_names(env):
                self.assertNotIn(key, actual)
        for stdin in (False, True):
            with patch.dict(os.environ, env, clear=True):
                token = github_auth.resolve_github_token_from_stdin_or_env(
                    stdin, stdin=StringIO("short\n")
                )
                self.assertEqual(token, "short")
                for key in github_auth.github_secret_env_names(env):
                    self.assertNotIn(key, os.environ)

    def test_stage_saves_source_binding_without_dispatch_and_can_resume(self):
        local = self.local()
        local.pop("source_run_id")
        local.pop("source_run_attempt")
        api = MemoryGitHub(artifact(created_at=int(time.time())))
        with tempfile.TemporaryDirectory() as tmp:
            path, staged = Path(tmp) / "local.json", Path(tmp) / "metadata.json"
            path.write_text(json.dumps(local))
            env = dict(
                GITHUB_EVENT_NAME="repository_dispatch",
                GITHUB_RUN_ID="700",
                GITHUB_RUN_ATTEMPT="1",
                PR_HEAD_SHA=HEAD,
                GITHUB_WORKFLOW_REF=f"{REPO}/{pub.SOURCE_WORKFLOW}@refs/heads/main",
                CODE_MOWER_LOCAL_AUDIT_LANE="claude",
                CODE_MOWER_AUDIT_STAGE_PATH=str(staged),
            )
            pub.stage(path, token="fixture", lane="claude", env=env, io=api)
            value = pub.validate(staged.read_text(), pub.digest(staged.read_text()))
            self.assertEqual(value["source_run_id"], 700)
            self.assertNotIn("PRIVATE_SOURCE", staged.read_text())
            self.assertEqual(api.writes, [])
            api.source = source_for(value)
            api.auto_publish = True
            self.assertEqual(pub.submit(path, token="fixture", lane="claude", io=api)["id"], 91)
            with self.assertRaises(pub.Refused):
                pub.stage(
                    path,
                    token="fixture",
                    lane="claude",
                    env=env
                    | {"GITHUB_WORKFLOW_REF": f"{REPO}/{pub.SOURCE_WORKFLOW}@refs/heads/builder"},
                    io=api,
                )

    def test_unsealed_local_artifact_cannot_be_submitted_with_personal_token(self):
        local = self.local()
        local.pop("source_run_id")
        api = MemoryGitHub()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.json"
            path.write_text(json.dumps(local))
            with self.assertRaises(pub.Refused):
                pub.submit(path, token="personal-token", lane="claude", io=api)
        self.assertEqual(api.writes, [])

    def local(self, lane="claude", **changes):
        value = artifact(lane, created_at=int(time.time()))
        return (
            dict(
                schema="code_mower.auditVerdictArtifact.v1",
                repo=REPO,
                pr_number=42,
                lane_id=lane + "-audit",
                head_sha_start=HEAD,
                head_sha_end=HEAD,
                verdict="PASS",
                created_at=datetime_text(value["created_at"]),
                trailer=pub.trailer(value),
                comment_body=f"## {lane.title()} audit (merge-authority lane)\n\n"
                + pub.trailer(value)
                + "\nPRIVATE_SOURCE /private/path token transcript",
                posted_comment_url=None,
                source_run_id=700,
                source_run_attempt=1,
            )
            | changes
        )

    def test_existing_artifact_is_projected_without_private_content_and_waited(self):
        for lane in ("claude", "codex"):
            local = self.local(lane)
            value = pub.project_local(local, REPOSITORY, lane=lane, now=int(time.time()))
            api = MemoryGitHub(value)
            api.auto_publish = True
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "local.json"
                path.write_text(json.dumps(local))
                posted = pub.submit(path, token="fixture", lane=lane, io=api, sleep=lambda _: None)
            self.assertEqual(posted["id"], 91)
            outbound = next(w[2] for w in api.writes if w[1] == "/dispatches")
            self.assertEqual(set(json.loads(outbound["client_payload"]["artifact"])), pub.FIELDS)
            for private in ("PRIVATE_SOURCE", "/private/path", "transcript", REPO, "comment_body"):
                self.assertNotIn(private, json.dumps(outbound))
            self.assertTrue(
                pub.attested(
                    body=posted["body"],
                    comment_id=91,
                    repo=REPO,
                    issue_number=42,
                    head_sha=HEAD,
                    run=api.run,
                    repository=REPOSITORY,
                )
            )

    def test_local_invalid_stale_quarantined_informational_and_context_refuse(self):
        for changes in (
            {"head_sha_end": "c" * 40},
            {"verdict": "UNKNOWN"},
            {"quarantined": True},
            {"repo": "private/other"},
            {"lane_id": "codex-audit"},
            {"trailer": "wrong"},
            {"comment_body": "informational only"},
            {"created_at": "bad"},
            {"created_at": datetime_text(int(time.time()) - pub.MAX_AGE - 10)},
            {"comment_body": "(merge-authority lane) CODE_MOWER_CONTEXT_REVIEW:"},
        ):
            with self.subTest(changes=changes), self.assertRaises(pub.Refused):
                pub.project_local(
                    self.local(**changes), REPOSITORY, lane="claude", now=int(time.time())
                )

    def test_wrapper_cli_submit_never_invokes_provider_and_defaults_workflow(self):
        for module in (claude_audit_pr, codex_audit_pr):
            self.assertEqual(module._parse_args([]).publication, "workflow")
            self.assertEqual(module._parse_args(["--publication", "direct"]).publication, "direct")
            with (
                patch.object(module, "_resolve_github_token", return_value="fixture"),
                patch.object(pub, "submit", return_value={"html_url": "fixture-url"}) as submit,
                patch.object(module, "audit_pr") as provider,
            ):
                self.assertEqual(module.main(["--publish-verdict-artifact", "local.json"]), 0)
                submit.assert_called_once()
                provider.assert_not_called()
            with (
                patch.object(pub, "submit", return_value={"body": "metadata", "html_url": "url"}),
                patch.object(pub, "unavailable_notice", return_value=None),
                patch.object(module, "post_pr_comment") as direct,
            ):
                _, body = module._post_audit_comment(
                    REPO,
                    42,
                    "PRIVATE",
                    token="fixture",
                    actions_run_id=None,
                    publication="workflow",
                    artifact_path=Path("local.json"),
                )
                self.assertEqual(body, "metadata")
                direct.assert_not_called()

    def test_local_head_move_before_dispatch_has_no_side_effect(self):
        local = self.local()
        api = MemoryGitHub()
        api.move_at = 1
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "verdict.json"
            path.write_text(json.dumps(local))
            with self.assertRaises(pub.Refused):
                pub.submit(path, token="fixture", lane="claude", io=api)
        self.assertEqual(api.writes, [])


def datetime_text(timestamp):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


class ConsumerTests(unittest.TestCase):
    def setup_case(self, lane="claude", verdict="PASS"):
        value = artifact(lane, verdict)
        api = MemoryGitHub(value)
        comment = pub.publish(event_for(value), environment(), api, now=NOW)
        api.run.update(status="completed", conclusion="success")
        builder = "codex" if lane == "claude" else "claude"
        episode = Episode(
            sequence=1,
            repo=REPO,
            pr_number=42,
            branch=f"{builder}/topic",
            source_lane=builder,
            destination_lane=builder,
            expected_head=SOURCE,
            resulting_head=HEAD,
            writer_state="terminated",
            kind="creation",
        )
        lineage = {
            "id": 90,
            "user": {"login": "same-owner"},
            "body": render_lineage(
                Chain.from_arrivals(Target(REPO, 42, f"{builder}/topic", HEAD), [episode])
            ),
        }
        identity = {
            "enabled": True,
            "labels": {"builder:codex": "codex", "builder:claude": "claude"},
            "authors": {},
            "branch_prefixes": {"codex/": "codex", "claude/": "claude"},
            "require_verified_lineage": True,
        }
        return value, api, comment, lineage, identity

    def lookup(self, api):
        def response(method, path, **kwargs):
            self.assertEqual(method, "GET")
            if path.endswith("/jobs?per_page=100"):
                return {"total_count": 1, "jobs": api.run["publication_jobs"]}
            if path.endswith("/actions/runs/800"):
                return api.run
            if path == f"/repos/{REPO}":
                return REPOSITORY
            raise AssertionError(path)

        return response

    def test_actual_labeler_accepts_both_same_owner_peer_lanes_and_both_verdicts(self):
        for lane in ("claude", "codex"):
            for verdict in ("PASS", "BLOCKED"):
                value, api, comment, lineage, identity = self.setup_case(lane, verdict)
                event = pub.prepare_label_event(
                    {"workflow_run": {"id": 800}}, {"TRAILER_LANE": lane}, api
                )
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "event.json"
                    path.write_text(json.dumps(event))
                    env = {
                        "GITHUB_EVENT_PATH": str(path),
                        "GITHUB_REPOSITORY": REPO,
                        "GITHUB_TOKEN": "fixture",
                        "CODE_MOWER_AUTHOR_EXCLUSION_JSON": json.dumps(identity),
                        "CODE_MOWER_DECISION_AUTHORITIES": "same-owner",
                        "CODE_MOWER_GITHUB_ACTIONS_WORKFLOWS": pub.WORKFLOW,
                        "CODEX_BOT_AUTHORS": "same-owner",
                        "CLAUDE_AUDIT_BOT_AUTHORS": "same-owner",
                    }
                    with (
                        patch.dict(os.environ, env, clear=True),
                        patch.object(labeler, "fetch_pull_request", return_value=api.pr),
                        patch.object(
                            labeler, "fetch_issue_comments", return_value=[lineage, comment]
                        ),
                        patch.object(
                            lib, "github_request_with_fallback", side_effect=self.lookup(api)
                        ),
                        patch.object(labeler, "apply_label_decision") as apply,
                    ):
                        self.assertEqual(labeler.main(["--lane", lane]), 0)
                    apply.assert_called_once()
                    self.assertEqual(
                        apply.call_args.args[1].add_label,
                        lane + "-audit-" + ("done" if verdict == "PASS" else "blocked"),
                    )
                api.pr["head"]["sha"] = "c" * 40
                with self.assertRaises(pub.Refused):
                    pub.prepare_label_event(
                        {"workflow_run": {"id": 800}}, {"TRAILER_LANE": lane}, api
                    )

    def test_gate_runs_actual_standalone_template_with_workflow_receipt(self):
        # Execute the emitted gate's Python block in isolation; only GitHub is simulated.
        cfg = config.load_config(ROOT / "src/code_mower/templates/code-mower.example.yml")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "product"
            init.apply_init_plan(
                init.render_init_plan(cfg, package_mode=True, repo_root=ROOT),
                dest,
                source_root=ROOT,
            )
            for lane in ("claude", "codex"):
                for verdict in ("PASS", "BLOCKED"):
                    value, api, comment, lineage, identity = self.setup_case(lane, verdict)
                    api.pr["labels"].append(
                        {"name": lane + "-audit-" + ("done" if verdict == "PASS" else "blocked")}
                    )
                    raw = (dest / ".github/workflows/code-mower-gate.yml").read_text()
                    block = raw.split(
                        'python3 - "${labels_file}" "${comments_file}" "${events_file}" "${pr_file}" "${audit_runs_file}" <<\'PY\'\n',
                        1,
                    )[1].split("\n          PY", 1)[0]
                    fixture = {"run": api.run, "repository": REPOSITORY}
                    fixture_path = root / "api.json"
                    fixture_path.write_text(json.dumps(fixture))
                    program = """import sys,json,io,urllib.request
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
fixture=json.loads(Path(sys.argv.pop(1)).read_text())
def response(req,**kwargs):
    url=req.full_url
    if '/jobs?' in url: data={'total_count':1,'jobs':fixture['run']['publication_jobs']}
    elif '/actions/runs/800' in url: data=fixture['run']
    elif url.endswith('/repos/owner/repo'): data=fixture['repository']
    else: raise AssertionError(url)
    return io.BytesIO(json.dumps(data).encode())
urllib.request.urlopen=response
""" + textwrap.dedent(block)
                    paths = []
                    for name, payload in [
                        ("labels", api.pr["labels"]),
                        ("comments", [[lineage, comment]]),
                        ("events", []),
                        ("pr", api.pr),
                        ("runs", []),
                    ]:
                        path = root / (name + ".json")
                        path.write_text(json.dumps(payload))
                        paths.append(str(path))
                    lanes = [
                        {
                            "id": x,
                            "author_lane": x,
                            "done": x + "-audit-done",
                            "blocked": x + "-audit-blocked",
                            "bot_authors": "github-actions[bot]",
                            "github_actions_workflows": pub.WORKFLOW,
                        }
                        for x in ("codex", "claude")
                    ]
                    env = os.environ | {
                        "CODE_MOWER_AUTHOR_EXCLUSION_JSON": json.dumps(identity),
                        "CODE_MOWER_GATE_LANES_JSON": json.dumps(lanes),
                        "CODE_MOWER_DECISION_AUTHORITIES": "same-owner",
                        "CODE_MOWER_DECISION_AUTHORITIES_OVERRIDE": "",
                        "HEAD_SHA": HEAD,
                        "PR_NUMBER": "42",
                        "GITHUB_REPOSITORY": REPO,
                        "GH_TOKEN": "fixture",
                        "CODE_MOWER_OWNER_LOGIN": "",
                        "CODE_MOWER_OWNER_LOGIN_OVERRIDE": "",
                    }
                    result = subprocess.run(
                        [sys.executable, "-I", "-S", "-c", program, str(fixture_path), *paths],
                        cwd=dest,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    states = {
                        line.split("=", 1)[0]: shlex.split(line.split("=", 1)[1])[0]
                        for line in result.stdout.splitlines()
                        if line.startswith("gate_")
                    }
                    self.assertEqual(
                        states["gate_state"],
                        "success" if verdict == "PASS" else "failure",
                        result.stdout,
                    )

    def test_publication_cannot_downgrade_to_legacy_workflow_attestation(self):
        value, api, comment, _, _ = self.setup_case()
        api.run.update(
            path=".github/workflows/local-cli-audit.yml",
            event="pull_request_target",
            pull_requests=[{"number": 42, "head": {"sha": HEAD}}],
        )
        self.assertFalse(
            lib.github_actions_comment_attested(
                repo=REPO,
                body=comment["body"],
                comment_id=91,
                issue_number=42,
                head_sha=HEAD,
                workflow_paths=(".github/workflows/local-cli-audit.yml", pub.WORKFLOW),
                tokens=(lib.GitHubToken("fixture", "fixture"),),
                actions_run_lookup=lambda _: api.run,
            )
        )

    def test_actual_wrapper_floor_uses_publication_identity_but_keeps_builder_exclusion(self):
        from lineage_consumer_fixtures import wrapper_boundary, policy, complete_pr
        from contextlib import ExitStack

        for lane, module, cls in (
            ("claude", claude_audit_pr, "ClaudeAuditConfig"),
            ("codex", codex_audit_pr, "AuditConfig"),
        ):
            original = getattr(module, cls)
            for publication, builder, allowed in (
                ("workflow", "codex" if lane == "claude" else "claude", True),
                ("direct", "codex" if lane == "claude" else "claude", False),
                ("workflow", lane, False),
            ):
                with (
                    self.subTest(lane=lane, publication=publication, builder=builder),
                    tempfile.TemporaryDirectory() as tmp,
                    ExitStack() as stack,
                ):
                    stack.enter_context(
                        patch.dict(
                            os.environ,
                            {"CLAUDE_AUDIT_BOT_AUTHORS": "human", "CODEX_BOT_AUTHORS": "human"},
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            module,
                            cls,
                            side_effect=partial(original, publication=publication),
                        )
                    )
                    pr = complete_pr(
                        branch=f"{builder}/topic" if builder != lane else "human/topic",
                        labels=[f"builder:{builder}"],
                    )
                    pr["head"]["repo"] = {"full_name": REPO}
                    self.assertEqual(
                        wrapper_boundary(Path(tmp) / "repo", lane, policy(), pr, []), allowed
                    )

    def test_generated_helpers_and_manifest_are_current(self):
        for name in (
            "audit_publication.py",
            "audit_labeler_lib.py",
            "trailer_comment_labeler.py",
            "lane_configs/__init__.py",
            "lane_configs/claude.py",
            "lane_configs/codex.py",
        ):
            self.assertEqual(
                (ROOT / "tools" / name).read_bytes(), (ROOT / "src/code_mower" / name).read_bytes()
            )
        self.assertEqual(
            (ROOT / "code-mower-package-manifest.json").read_text(),
            package.committed_package_manifest_text(
                package.generate_committed_package_manifest(ROOT)
            ),
        )


if __name__ == "__main__":
    unittest.main()
