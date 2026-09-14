"""Regressions for the three seams that *obtain* lineage authority.

``test_builder_lineage_consumers`` proves each helper carries lineage when it
is handed some, and ``test_builder_lineage_integration`` proves the producer
publishes. These prove the three production entrypoints between them do the
right thing under the repository's configured decision-authority contract:

* the delivery CLI refuses to publish or move a label when no authority is
  configured, because every consumer would then trust no marker at all;
* every maintained SaaS labeler entrypoint fetches the exact current head, the
  head branch and the bounded trusted comment history before it decides, and
  stops instead of mutating labels when a required read fails;
* the generated provenance job supplies the same authority contract to
  ``builder auto-record``, so a verified takeover is attributed to its current
  writer rather than to whoever opened the pull request.

Each case drives the real entrypoint -- ``lane_delivery.main``,
``saas_reviewer_labeler.main`` and the rendered workflow's own command line --
with only the network boundary replaced.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_mower import builder_lineage, lane_delivery, lane_handoff  # noqa: E402
from code_mower import saas_reviewer_labeler as labeler  # noqa: E402
from code_mower.provider_runners import lineage as reviewer_lineage  # noqa: E402

from test_builder_lineage_consumers import (  # noqa: E402
    BRANCH,
    PR,
    REPO,
    TAKEN,
    git_free_tempdir,
    takeover_episode,
)

ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = "codemower-ai"
OUTSIDER = "passer-by"
OPENER = "devin-ai-integration[bot]"
MARKER_BODY = "Builder contribution lineage for this head.\n\n"


@contextmanager
def _private_root(case: unittest.TestCase):
    """A store root outside any checkout, as the context store demands."""

    yield str(git_free_tempdir(case))


def marker_comment(author: str = AUTHORITY) -> dict:
    return {
        "user": {"login": author},
        "body": MARKER_BODY
        + builder_lineage.lineage_comment_marker((takeover_episode(),)),
    }


class _FakeGh:
    """Just enough `gh` for the delivery CLI: comments, publish, label edit."""

    def __init__(self, comments=(), publish_as: str = AUTHORITY, head: str = TAKEN):
        self.comments = [dict(item) for item in comments]
        self.publish_as = publish_as
        self.head = head
        self.posted: list[str] = []
        self.label_commands: list[list[str]] = []

    def check_output(self, command, **_kwargs):
        if "--json" in command and "headRefOid" in command:
            return json.dumps({"headRefOid": self.head})
        if "--json" in command and "comments" in command:
            return json.dumps(
                {
                    "comments": [
                        {
                            "author": {"login": item["user"]["login"]},
                            "body": item["body"],
                        }
                        for item in self.comments
                    ]
                }
            )
        raise AssertionError(f"unexpected gh read: {command}")

    def run(self, command, **_kwargs):
        if command[:3] == ["gh", "pr", "comment"]:
            body = command[command.index("--body") + 1]
            self.posted.append(body)
            self.comments.append({"user": {"login": self.publish_as}, "body": body})
            return subprocess.CompletedProcess(command, 0)
        if command[:3] == ["gh", "pr", "edit"]:
            self.label_commands.append(list(command))
            return subprocess.CompletedProcess(command, 0)
        raise AssertionError(f"unexpected gh write: {command}")


class PublisherRequiresATrustedAuthority(unittest.TestCase):
    """The delivery CLI never publishes evidence no consumer would read."""

    def _run(self, gh, *, authorities: str, extra=()):
        with _private_root(self) as root:
            builder_lineage.record_episode(
                lane_handoff.lineage_root(Path(root)), takeover_episode()
            )
            argv = [
                "lineage",
                "--repo", REPO,
                "--pr", str(PR),
                "--branch", BRANCH,
                "--head", TAKEN,
                "--author", OPENER,
                "--label", "builder:devin",
                "--state-dir", str(root),
                "--publish",
                "--reconcile-labels",
                "--json",
                *extra,
            ]
            env = {"CODE_MOWER_DECISION_AUTHORITIES": authorities}
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(lane_delivery.subprocess, "check_output",
                                      gh.check_output), \
                    mock.patch.object(lane_delivery.subprocess, "run", gh.run), \
                    mock.patch("sys.stdout", new_callable=_Capture) as out:
                code = lane_delivery.main(argv)
            return code, json.loads(out.text())

    def test_no_configured_authority_publishes_nothing_and_moves_no_label(self):
        gh = _FakeGh()
        code, payload = self._run(gh, authorities="")
        self.assertEqual(code, 3)
        self.assertEqual(payload["reason"], "lineage_no_decision_authority")
        self.assertTrue(payload["owner_action"])
        self.assertFalse(payload["applied"])
        self.assertEqual(gh.posted, [], "nothing may be published")
        self.assertEqual(gh.label_commands, [], "no label may be reconciled")

    def test_an_identical_untrusted_marker_does_not_suppress_publication(self):
        gh = _FakeGh(comments=[marker_comment(OUTSIDER)])
        code, payload = self._run(gh, authorities=AUTHORITY)
        self.assertEqual(code, 0)
        self.assertTrue(payload["published"])
        self.assertEqual(len(gh.posted), 1)

    def test_a_configured_trusted_publisher_publishes_then_reconciles(self):
        gh = _FakeGh()
        code, payload = self._run(gh, authorities=f"{AUTHORITY},someone-else")
        self.assertEqual(code, 0)
        self.assertTrue(payload["published"])
        self.assertEqual(len(gh.posted), 1)
        self.assertTrue(gh.label_commands, "the verified writer takes the label")

    def test_an_unreadable_readback_blocks_the_label(self):
        gh = _FakeGh(publish_as=OUTSIDER)
        code, payload = self._run(gh, authorities=AUTHORITY)
        self.assertEqual(code, 3)
        self.assertEqual(payload["reason"], "lineage_unpublished")
        self.assertTrue(payload["owner_action"])
        self.assertEqual(len(gh.posted), 1, "the attempt happened; it did not count")
        self.assertEqual(gh.label_commands, [], "no label may be reconciled")


class _Capture:
    """Minimal stdout stand-in that keeps what an entrypoint printed."""

    def __init__(self):
        self._chunks: list[str] = []

    def write(self, text):  # pragma: no cover - trivial
        self._chunks.append(text)
        return len(text)

    def flush(self):  # pragma: no cover - trivial
        return None

    def text(self) -> str:
        return "".join(self._chunks)


def _pull_request(head: str = TAKEN, labels=("builder:codex", "greptile-review", "gitar-audit-requested")):
    return {
        "number": PR,
        "state": "open",
        "user": {"login": OPENER},
        "body": "",
        "labels": [{"name": name} for name in labels],
        "head": {"sha": head, "ref": BRANCH},
    }


class _FakeApi:
    """A routed GitHub API, so real pagination and real fetches still run."""

    def __init__(self, *, comment_pages=None, fail_comments=False, head=TAKEN):
        self.comment_pages = (
            comment_pages if comment_pages is not None else [[marker_comment()]]
        )
        self.fail_comments = fail_comments
        self.head = head
        self.comment_requests: list[str] = []

    def __call__(self, method, path, **_kwargs):
        if "/issues/" in path and path.rstrip("/").split("?")[0].endswith("/comments"):
            if self.fail_comments:
                raise labeler.GitHubRequestError("GET", path, 500, "comments unavailable")
            self.comment_requests.append(path)
            page = int(re.search(r"[?&]page=(\d+)", path).group(1))
            pages = self.comment_pages
            return pages[page - 1] if page <= len(pages) else []
        if re.search(r"/pulls/\d+$", path):
            return _pull_request(head=self.head)
        if "/commits/" in path and path.endswith("pulls?per_page=100"):
            return [{"number": PR}]
        if "/reviews" in path:
            return []
        raise AssertionError(f"unexpected API path: {path}")


class SaaSEntrypointsCarryExactHeadLineage(unittest.TestCase):
    """Every maintained SaaS entrypoint obtains head, branch and comments."""

    def _main(self, *, adapter, event, event_name, api, authorities=AUTHORITY):
        seen: list = []
        applied: list = []
        real_context = labeler.lineage_context

        def spy(**kwargs):
            context = real_context(**kwargs)
            seen.append(context)
            return context

        with _private_root(self) as root:
            event_path = Path(root) / "event.json"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            env = {
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_REPOSITORY": REPO,
                "GITHUB_EVENT_NAME": event_name,
                "GREPTILE_LABEL_TOKEN": "t",
                "GITAR_LABEL_TOKEN": "t",
                "GITHUB_TOKEN": "t",
                "CODE_MOWER_DECISION_AUTHORITIES": authorities,
                "CODE_MOWER_DECISION_AUTHORITIES_OVERRIDE": "",
                "DRY_RUN": "",
            }
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(labeler, "lineage_context", spy), \
                    mock.patch.object(labeler, "github_request_with_fallback", api), \
                    mock.patch(
                        "code_mower.audit_labeler_lib.github_request_with_fallback", api
                    ), \
                    mock.patch.object(
                        labeler, "_apply_or_log",
                        lambda *a, **k: applied.append((a, k))
                    ), \
                    mock.patch("sys.stdout", new_callable=_Capture):
                code = labeler.main(["--adapter", adapter])
        return code, seen, applied

    def _assert_exact_head_lineage(self, seen):
        self.assertTrue(seen, "the entrypoint resolved no lineage at all")
        resolved = [item for item in seen if item.head_sha]
        self.assertTrue(resolved, "no entrypoint carried the current head")
        for context in resolved:
            self.assertEqual(context.repo, REPO)
            self.assertEqual(context.head_sha, TAKEN)
            self.assertEqual(context.branch, BRANCH)
            self.assertEqual(len(context.episodes), 1)

    def test_check_run_carries_the_trusted_comment_history(self):
        api = _FakeApi()
        event = {
            "action": "completed",
            "check_run": {
                "status": "completed",
                "conclusion": "success",
                "name": "greptile review",
                "app": {"slug": "greptile-apps"},
                "head_sha": TAKEN,
                "pull_requests": [{"number": PR}],
            },
        }
        _, seen, _ = self._main(
            adapter="greptile", event=event, event_name="check_run", api=api
        )
        self._assert_exact_head_lineage(seen)
        self.assertTrue(api.comment_requests, "check_run never fetched comments")

    def test_live_issue_comment_carries_head_branch_and_comments(self):
        api = _FakeApi()
        event = {
            "action": "created",
            "issue": {
                "number": PR,
                "pull_request": {"url": "https://example.invalid"},
                "user": {"login": OPENER},
                "body": "",
                "labels": [{"name": "gitar-audit-requested"}],
            },
            "comment": {
                "user": {"login": "gitar-ai[bot]"},
                "body": "Gitar review complete. No issues found.",
            },
        }
        _, seen, _ = self._main(
            adapter="gitar", event=event, event_name="issue_comment", api=api
        )
        self._assert_exact_head_lineage(seen)
        self.assertTrue(api.comment_requests, "issue_comment never fetched comments")

    def test_issues_replay_carries_the_current_head(self):
        api = _FakeApi()
        event = {
            "action": "labeled",
            "label": {"name": "gitar-audit-requested"},
            "issue": {
                "number": PR,
                "pull_request": {"url": "https://example.invalid"},
                "user": {"login": OPENER},
                "body": "",
                "labels": [{"name": "gitar-audit-requested"}],
            },
        }
        _, seen, _ = self._main(
            adapter="gitar", event=event, event_name="issues", api=api
        )
        self._assert_exact_head_lineage(seen)

    def test_an_untrusted_marker_yields_no_episodes(self):
        api = _FakeApi(comment_pages=[[marker_comment(OUTSIDER)]])
        event = {
            "action": "completed",
            "check_run": {
                "status": "completed",
                "conclusion": "success",
                "name": "greptile review",
                "app": {"slug": "greptile-apps"},
                "head_sha": TAKEN,
                "pull_requests": [{"number": PR}],
            },
        }
        _, seen, _ = self._main(
            adapter="greptile", event=event, event_name="check_run", api=api
        )
        self.assertTrue(seen)
        self.assertEqual(seen[0].head_sha, TAKEN)
        self.assertEqual(seen[0].episodes, ())

    def test_no_configured_authority_reads_nothing_and_still_decides(self):
        """Ordinary no-lineage behaviour is preserved, and costs no request."""

        api = _FakeApi()
        event = {
            "action": "completed",
            "check_run": {
                "status": "completed",
                "conclusion": "success",
                "name": "greptile review",
                "app": {"slug": "greptile-apps"},
                "head_sha": TAKEN,
                "pull_requests": [{"number": PR}],
            },
        }
        code, seen, _ = self._main(
            adapter="greptile", event=event, event_name="check_run", api=api,
            authorities="",
        )
        self.assertEqual(code, 0)
        self.assertEqual(api.comment_requests, [])
        self.assertTrue(seen)
        self.assertEqual(seen[0].episodes, ())

    def test_a_failed_comment_read_mutates_no_label(self):
        api = _FakeApi(fail_comments=True)
        event = {
            "action": "completed",
            "check_run": {
                "status": "completed",
                "conclusion": "success",
                "name": "greptile review",
                "app": {"slug": "greptile-apps"},
                "head_sha": TAKEN,
                "pull_requests": [{"number": PR}],
            },
        }
        code, _, applied = self._main(
            adapter="greptile", event=event, event_name="check_run", api=api
        )
        self.assertEqual(code, 0)
        self.assertEqual(applied, [], "a failed required read may not label")

    def test_a_paginated_comment_history_is_read_whole(self):
        filler = [
            {"user": {"login": OUTSIDER}, "body": f"noise {index}"}
            for index in range(100)
        ]
        api = _FakeApi(comment_pages=[filler, [marker_comment()]])
        event = {
            "action": "completed",
            "check_run": {
                "status": "completed",
                "conclusion": "success",
                "name": "greptile review",
                "app": {"slug": "greptile-apps"},
                "head_sha": TAKEN,
                "pull_requests": [{"number": PR}],
            },
        }
        _, seen, _ = self._main(
            adapter="greptile", event=event, event_name="check_run", api=api
        )
        self.assertEqual(len(api.comment_requests), 2)
        self._assert_exact_head_lineage(seen)


class GeneratedProvenanceJobCarriesTheAuthorityContract(unittest.TestCase):
    """The generated job's own inputs must decide marker trust.

    This job runs on ``pull_request``, so where the authority list comes from
    is the whole question. It is rendered into the workflow from the reviewed
    configuration the repository installed, through the same replacement map
    the gate and the labelers go through. Nothing is read from the proposed
    head at job time: a contributor who can edit a checked-out configuration
    must not be able to name themselves an authority and have their own
    lineage marker believed.
    """

    TEMPLATES = (
        ROOT / "templates/workflows/builder-provenance.yml.j2",
        ROOT / "src/code_mower/templates/workflows/builder-provenance.yml.j2",
    )

    def _render(self, path: Path, *, authorities: str) -> str:
        """The workflow `init` actually generates for a configured repository."""

        from code_mower import init

        return init._render_workflow_template(
            path.read_text(encoding="utf-8"),
            {"decision_authorities": authorities},
        )

    def _job_env(self, rendered: str) -> dict:
        workflow = yaml.safe_load(rendered)
        return dict(workflow.get("env") or {})

    def test_both_maintained_templates_generate_the_same_workflow(self):
        first, second = (
            self._render(path, authorities=AUTHORITY) for path in self.TEMPLATES
        )
        self.assertEqual(first, second)
        self.assertEqual(
            self.TEMPLATES[0].read_text(encoding="utf-8"),
            self.TEMPLATES[1].read_text(encoding="utf-8"),
        )

    def test_the_configured_authority_list_is_rendered_as_a_literal(self):
        for path in self.TEMPLATES:
            with self.subTest(template=path.name):
                env = self._job_env(self._render(path, authorities=AUTHORITY))
                self.assertEqual(env["CODE_MOWER_DECISION_AUTHORITIES"], AUTHORITY)
                # The repository variable still overrides it, unrendered.
                self.assertIn(
                    "vars.CODE_MOWER_DECISION_AUTHORITIES",
                    env["CODE_MOWER_DECISION_AUTHORITIES_OVERRIDE"],
                )

    def test_a_multi_authority_list_survives_rendering_intact(self):
        env = self._job_env(
            self._render(self.TEMPLATES[0], authorities=f"{AUTHORITY},second-owner")
        )
        self.assertEqual(
            env["CODE_MOWER_DECISION_AUTHORITIES"], f"{AUTHORITY},second-owner"
        )

    def test_the_generated_job_reads_no_configuration_from_the_proposed_head(self):
        """The defect this replaced: authority resolved from the PR checkout."""

        for path in self.TEMPLATES:
            with self.subTest(template=path.name):
                rendered = self._render(path, authorities=AUTHORITY)
                self.assertNotIn("actions/checkout", rendered)
                self.assertNotIn("code-mower.yml", rendered)
                self.assertNotIn("decision_authorities_from_config", rendered)
                # No step may add to the job environment after it is rendered:
                # that is the only way the proposed head could reach trust.
                self.assertNotIn("GITHUB_ENV", rendered)
                steps = yaml.safe_load(rendered)["jobs"]["auto-record"]["steps"]
                self.assertEqual(
                    [
                        step["uses"].split("@")[0]
                        for step in steps
                        if "uses" in step
                    ],
                    [
                        "actions/setup-python",
                        "actions/upload-artifact",
                        "actions/upload-artifact",
                    ],
                )
                for step in steps:
                    if "run" not in step:
                        continue
                    # The job may run Python -- it validates the comment page
                    # shape -- but nothing it runs may reach repository
                    # configuration or its own decision-authority inputs.
                    self.assertNotIn("load_config", step["run"])
                    self.assertNotIn("code-mower.yml", step["run"])
                    self.assertNotIn("DECISION_AUTHORITIES", step["run"])

    def _auto_record(self, *, env: Mapping[str, Any], comments, cwd=None) -> dict:
        """Run auto-record exactly as the generated job's inputs configure it."""

        from code_mower import builder_runs

        with _private_root(self) as root:
            pr_json = Path(root) / "event.json"
            pr_json.write_text(
                json.dumps({"pull_request": _pull_request()}), encoding="utf-8"
            )
            comments_json = Path(root) / "comments.json"
            comments_json.write_text(json.dumps(comments), encoding="utf-8")
            output = Path(root) / "run.json"
            job_env = {
                "CODE_MOWER_DECISION_AUTHORITIES": "",
                "CODE_MOWER_DECISION_AUTHORITIES_OVERRIDE": "",
                **{str(key): str(value) for key, value in env.items()},
            }
            captured = _Capture()
            previous = os.getcwd()
            os.chdir(cwd or previous)
            try:
                with mock.patch.dict(os.environ, job_env, clear=False), \
                        mock.patch("sys.stdout", captured):
                    code = builder_runs.main([
                        "auto-record",
                        "--pr-json", str(pr_json),
                        "--repo", REPO,
                        "--comments-json", str(comments_json),
                        "--output", str(output),
                        "--force",
                        "--json",
                    ])
            finally:
                os.chdir(previous)
            self.assertEqual(code, 0)
            return json.loads(captured.text())

    def _generated_env(self, *, authorities: str, variable: str = "") -> dict:
        """The job environment GitHub Actions would compose for this workflow.

        The rendered literal is the workflow's `env`; the repository variable
        expands into the override field, which is empty when it is unset.
        """

        env = self._job_env(self._render(self.TEMPLATES[0], authorities=authorities))
        env["CODE_MOWER_DECISION_AUTHORITIES_OVERRIDE"] = variable
        env.pop("CODE_MOWER_PACKAGE_SPEC", None)
        return env

    def test_a_generated_run_attributes_the_takeover_to_its_current_writer(self):
        payload = self._auto_record(
            env=self._generated_env(authorities=AUTHORITY),
            comments=[marker_comment()],
        )
        self.assertEqual(payload["status"], "recorded")
        # The verified current writer, not the Devin account that opened it.
        self.assertEqual(payload["executor"], "chatgpt-codex-connector")

    def test_a_pull_request_provided_configuration_cannot_grant_trust(self):
        """A hostile `code-mower.yml` on the proposed head buys nothing.

        The rendered job reaches no configuration at all -- asserted above --
        and auto-record itself resolves trust only from the environment the
        job was rendered with, never from the tree it is run in.
        """

        hostile = git_free_tempdir(self)
        (hostile / "code-mower.yml").write_text(
            f"decisions:\n  authorities:\n    - {OUTSIDER}\n", encoding="utf-8"
        )
        payload = self._auto_record(
            env=self._generated_env(authorities=""),
            comments=[marker_comment(OUTSIDER)],
            cwd=hostile,
        )
        self.assertNotEqual(payload.get("executor"), "chatgpt-codex-connector")

    def test_the_repository_variable_still_overrides_the_rendered_list(self):
        # Rendered authority alone would refuse this marker; the variable is
        # the reviewed escape hatch and still wins, as it does for the gate.
        payload = self._auto_record(
            env=self._generated_env(authorities="someone-else", variable=AUTHORITY),
            comments=[marker_comment()],
        )
        self.assertEqual(payload["executor"], "chatgpt-codex-connector")

    def test_empty_authorities_trust_no_marker(self):
        env = self._generated_env(authorities="")
        self.assertEqual(env["CODE_MOWER_DECISION_AUTHORITIES"], "")
        payload = self._auto_record(env=env, comments=[marker_comment()])
        self.assertNotEqual(payload.get("executor"), "chatgpt-codex-connector")

    def test_an_untrusted_marker_is_discarded(self):
        payload = self._auto_record(
            env=self._generated_env(authorities=AUTHORITY),
            comments=[marker_comment(OUTSIDER)],
        )
        self.assertNotEqual(payload.get("executor"), "chatgpt-codex-connector")


#: Records that are dicts but whose relevant fields cannot be read. A dict is
#: not a comment: the marker lives in `body` and trust is decided from
#: `user.login`, so a present field of the wrong type would be stringified into
#: an author or a body GitHub never sent.
MALFORMED_COMMENT_RECORDS = (
    {"user": {"login": AUTHORITY}, "body": 12345},
    {"user": {"login": AUTHORITY}, "body": {"text": "hi"}},
    {"user": {"login": AUTHORITY}, "body": ["hi"]},
    {"user": "codemower-ai", "body": "hi"},
    {"user": 7, "body": "hi"},
    {"user": ["codemower-ai"], "body": "hi"},
    {"user": {"login": {"name": AUTHORITY}}, "body": "hi"},
    {"user": {"login": 7}, "body": "hi"},
    {"user": {"login": [AUTHORITY]}, "body": "hi"},
)

#: GitHub's own schema, which must keep working: a comment from a deleted
#: account carries `user: null`, and `body` is optional on some
#: representations. Neither names an author or a marker, and neither is an
#: error.
VALID_COMMENT_RECORDS = (
    {"user": None, "body": "a deleted account said this"},
    {"user": {"login": AUTHORITY}},
    {"user": {"login": None}, "body": "hi"},
    {"user": {"login": AUTHORITY}, "body": "ordinary comment"},
)

INVALID_COMMENT_RESPONSES = (
    None,
    False,
    {},
    {"comments": [{"user": {"login": AUTHORITY}, "body": "hi"}]},
    [{"user": {"login": AUTHORITY}, "body": "hi"}, "not a comment"],
)


class ASuccessfulButInvalidCommentReadIsNotAnEmptyHistory(unittest.TestCase):
    """`None`, `False`, `{}` and a list with a non-object are not "no comments".

    Each is a *successful* read that carries no readable history. Normalising
    it away -- with `or []`, or by filtering non-mappings out -- reports "there
    is nothing here" for "this could not be read", and the takeover marker is
    in the newest part of exactly the history that got dropped.
    """

    def test_the_saas_labeler_mutates_no_label_on_an_invalid_page(self):
        applied = []
        event = {
            "action": "completed",
            "check_run": {
                "status": "completed",
                "conclusion": "success",
                "name": "greptile review",
                "app": {"slug": "greptile-apps"},
                "head_sha": TAKEN,
                "pull_requests": [{"number": PR}],
            },
        }
        for response in INVALID_COMMENT_RESPONSES:
            with self.subTest(response=response):
                applied.clear()

                def api(method, path, _response=response, **_kwargs):
                    if "/comments" in path:
                        return _response
                    if re.search(r"/pulls/\d+$", path):
                        return _pull_request()
                    if "/commits/" in path:
                        return [{"number": PR}]
                    return []

                root = git_free_tempdir(self, "code-mower-invalid-comments-")
                event_path = Path(root) / "event.json"
                event_path.write_text(json.dumps(event), encoding="utf-8")
                env = {
                    "GITHUB_EVENT_PATH": str(event_path),
                    "GITHUB_REPOSITORY": REPO,
                    "GITHUB_EVENT_NAME": "check_run",
                    "GREPTILE_LABEL_TOKEN": "t",
                    "GITHUB_TOKEN": "t",
                    "CODE_MOWER_DECISION_AUTHORITIES": AUTHORITY,
                    "CODE_MOWER_DECISION_AUTHORITIES_OVERRIDE": "",
                    "DRY_RUN": "",
                }
                with mock.patch.dict(os.environ, env, clear=False), \
                        mock.patch.object(
                            labeler, "github_request_with_fallback", api), \
                        mock.patch(
                            "code_mower.audit_labeler_lib."
                            "github_request_with_fallback", api), \
                        mock.patch.object(
                            labeler, "_apply_or_log",
                            lambda *a, **k: applied.append(a)), \
                        mock.patch("sys.stdout", new_callable=_Capture):
                    code = labeler.main(["--adapter", "greptile"])
                self.assertEqual(code, 0)
                self.assertEqual(applied, [], "no label may move on an unread history")

    def test_the_saas_labeler_mutates_no_label_on_duplicate_key_evidence(self):
        """Duplicate keys give one marker two answers; neither may be picked."""

        marker = builder_lineage.lineage_comment_marker((takeover_episode(),))
        head, _, tail = marker.partition("{")
        ambiguous = f'{head}{{"episodes":[],{tail}'
        comments = [{"user": {"login": AUTHORITY}, "body": MARKER_BODY + ambiguous}]
        applied = []

        def api(method, path, **_kwargs):
            if "/comments" in path:
                return comments if "page=1" in path else []
            if re.search(r"/pulls/\d+$", path):
                return _pull_request()
            if "/commits/" in path:
                return [{"number": PR}]
            return []

        root = git_free_tempdir(self, "code-mower-duplicate-keys-")
        event = {
            "action": "completed",
            "check_run": {
                "status": "completed", "conclusion": "success",
                "name": "greptile review", "app": {"slug": "greptile-apps"},
                "head_sha": TAKEN, "pull_requests": [{"number": PR}],
            },
        }
        event_path = Path(root) / "event.json"
        event_path.write_text(json.dumps(event), encoding="utf-8")
        env = {
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_EVENT_NAME": "check_run",
            "GREPTILE_LABEL_TOKEN": "t",
            "GITHUB_TOKEN": "t",
            "CODE_MOWER_DECISION_AUTHORITIES": AUTHORITY,
            "CODE_MOWER_DECISION_AUTHORITIES_OVERRIDE": "",
            "DRY_RUN": "",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(labeler, "github_request_with_fallback", api), \
                mock.patch(
                    "code_mower.audit_labeler_lib.github_request_with_fallback", api), \
                mock.patch.object(
                    labeler, "_apply_or_log", lambda *a, **k: applied.append(a)), \
                mock.patch("sys.stdout", new_callable=_Capture):
            code = labeler.main(["--adapter", "greptile"])
        self.assertEqual(code, 0)
        self.assertEqual(applied, [], "ambiguous evidence may move no label")

    def test_a_genuinely_empty_page_stays_ordinary(self):
        def api(method, path, **_kwargs):
            if "/comments" in path:
                return []
            if re.search(r"/pulls/\d+$", path):
                return _pull_request()
            return []

        with mock.patch.object(labeler, "github_request_with_fallback", api):
            self.assertEqual(
                labeler.fetch_issue_comments(REPO, PR, tokens=(), page_cap=5), []
            )

    def test_the_direct_wrapper_surfaces_an_invalid_read_as_unreadable(self):
        for response in INVALID_COMMENT_RESPONSES:
            with self.subTest(response=response):
                with self.assertRaises(builder_lineage.LineageError):
                    reviewer_lineage.reviewer_evidence(
                        REPO, PR,
                        authorities=(AUTHORITY,),
                        fetch_comments=lambda _response=response: _response,
                        state_dir=git_free_tempdir(self, "code-mower-invalid-"),
                    )

    def _gh_pages(self, *pages):
        """A lowest-level GitHub transport that answers page by page."""

        seen = []

        def request(method, path, **_kwargs):
            if "/comments" not in path:
                return []
            index = int(re.search(r"[?&]page=(\d+)", path).group(1))
            seen.append(index)
            return pages[index - 1] if index - 1 < len(pages) else []

        return request, seen

    def test_the_lowest_transport_refuses_a_malformed_record(self):
        from code_mower.provider_runners import github_pr

        for record in MALFORMED_COMMENT_RECORDS:
            with self.subTest(record=record):
                request, _ = self._gh_pages([record])
                with mock.patch.object(github_pr, "_gh_request", request):
                    with self.assertRaises(ValueError):
                        github_pr.fetch_issue_comments(REPO, PR, token="t")

    def test_the_lowest_transport_refuses_a_malformed_record_after_a_valid_page(self):
        from code_mower.provider_runners import github_pr

        first = [marker_comment() for _ in range(100)]
        for record in MALFORMED_COMMENT_RECORDS:
            with self.subTest(record=record):
                request, seen = self._gh_pages(first, [record])
                with mock.patch.object(github_pr, "_gh_request", request):
                    with self.assertRaises(ValueError):
                        github_pr.fetch_issue_comments(REPO, PR, token="t")
                self.assertEqual(seen, [1, 2])

    def test_the_lowest_transport_keeps_githubs_own_schema(self):
        from code_mower.provider_runners import github_pr

        request, _ = self._gh_pages(list(VALID_COMMENT_RECORDS))
        with mock.patch.object(github_pr, "_gh_request", request):
            comments = github_pr.fetch_issue_comments(REPO, PR, token="t")
        self.assertEqual(len(comments), len(VALID_COMMENT_RECORDS))

    def test_the_wrapper_launches_nothing_on_a_malformed_record(self):
        from code_mower import claude_audit_pr
        from code_mower.provider_runners import github_pr

        for record in MALFORMED_COMMENT_RECORDS:
            with self.subTest(record=record):
                request, _ = self._gh_pages([record])
                with mock.patch.object(github_pr, "_gh_request", request):
                    with self.assertRaises(RuntimeError) as raised:
                        claude_audit_pr._require_independent_review(
                            "claude", REPO, PR,
                            {"user": {"login": "a-human"},
                             "head": {"ref": BRANCH, "sha": TAKEN},
                             "labels": []},
                            TAKEN,
                            authorities=(AUTHORITY,),
                            fetch_comments=lambda: github_pr.fetch_issue_comments(
                                REPO, PR, token="t"
                            ),
                        )
                self.assertIn("lineage_unreadable", str(raised.exception))

    def test_the_saas_labeler_mutates_no_label_on_a_malformed_record(self):
        for record in MALFORMED_COMMENT_RECORDS:
            with self.subTest(record=record):
                self.assertEqual(
                    self._labeler_applied(pages=[[record]]), [],
                    "a malformed record may move no label",
                )

    def test_the_saas_labeler_reads_githubs_own_schema_whole(self):
        """Positive control, on valid fixtures only: nothing is dropped."""

        valid = list(VALID_COMMENT_RECORDS) + [marker_comment()]

        def api(method, path, **_kwargs):
            return valid if "page=1" in path else []

        with mock.patch.object(labeler, "github_request_with_fallback", api):
            comments = labeler.fetch_issue_comments(REPO, PR, tokens=(), page_cap=5)
        self.assertEqual(len(comments), len(valid))
        # And it still reaches a decision without refusing the run.
        self.assertEqual(self._labeler_applied(pages=[valid]), [])

    def _labeler_applied(self, *, pages):
        applied = []

        def api(method, path, **_kwargs):
            if "/comments" in path:
                index = int(re.search(r"[?&]page=(\d+)", path).group(1))
                return pages[index - 1] if index - 1 < len(pages) else []
            if re.search(r"/pulls/\d+$", path):
                return _pull_request()
            if "/commits/" in path:
                return [{"number": PR}]
            return []

        root = git_free_tempdir(self, "code-mower-record-shape-")
        event = {
            "action": "completed",
            "check_run": {
                "status": "completed", "conclusion": "success",
                "name": "greptile review", "app": {"slug": "greptile-apps"},
                "head_sha": TAKEN, "pull_requests": [{"number": PR}],
            },
        }
        event_path = Path(root) / "event.json"
        event_path.write_text(json.dumps(event), encoding="utf-8")
        env = {
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_EVENT_NAME": "check_run",
            "GREPTILE_LABEL_TOKEN": "t",
            "GITHUB_TOKEN": "t",
            "CODE_MOWER_DECISION_AUTHORITIES": AUTHORITY,
            "CODE_MOWER_DECISION_AUTHORITIES_OVERRIDE": "",
            "DRY_RUN": "",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(labeler, "github_request_with_fallback", api), \
                mock.patch(
                    "code_mower.audit_labeler_lib.github_request_with_fallback", api), \
                mock.patch.object(
                    labeler, "_apply_or_log", lambda *a, **k: applied.append(a)), \
                mock.patch("sys.stdout", new_callable=_Capture):
            self.assertEqual(labeler.main(["--adapter", "greptile"]), 0)
        return applied

    def test_the_lowest_transport_refuses_every_malformed_page(self):
        """The wrapper's own `fetch_issue_comments`, not a stubbed return."""

        from code_mower.provider_runners import github_pr

        for response in INVALID_COMMENT_RESPONSES:
            with self.subTest(response=response):
                request, _ = self._gh_pages(response)
                with mock.patch.object(github_pr, "_gh_request", request):
                    with self.assertRaises(ValueError):
                        github_pr.fetch_issue_comments(REPO, PR, token="t")

    def test_a_malformed_page_after_a_valid_one_is_still_refused(self):
        from code_mower.provider_runners import github_pr

        first = [marker_comment() for _ in range(100)]
        for response in INVALID_COMMENT_RESPONSES:
            with self.subTest(response=response):
                request, seen = self._gh_pages(first, response)
                with mock.patch.object(github_pr, "_gh_request", request):
                    with self.assertRaises(ValueError):
                        github_pr.fetch_issue_comments(REPO, PR, token="t")
                self.assertEqual(seen, [1, 2], "it read on and then refused")

    def test_the_transport_still_returns_a_complete_valid_history(self):
        from code_mower.provider_runners import github_pr

        pages = ([marker_comment()] * 100, [marker_comment()] * 7)
        request, seen = self._gh_pages(*pages)
        with mock.patch.object(github_pr, "_gh_request", request):
            comments = github_pr.fetch_issue_comments(REPO, PR, token="t")
        self.assertEqual(len(comments), 107)
        self.assertEqual(seen, [1, 2])

    def test_a_genuinely_empty_first_page_stays_ordinary(self):
        from code_mower.provider_runners import github_pr

        request, seen = self._gh_pages([])
        with mock.patch.object(github_pr, "_gh_request", request):
            self.assertEqual(github_pr.fetch_issue_comments(REPO, PR, token="t"), [])
        self.assertEqual(seen, [1])

    def test_the_transport_keeps_its_pagination_cap(self):
        from code_mower.provider_runners import github_pr

        request, _ = self._gh_pages(*([[marker_comment()] * 100] * 12))
        with mock.patch.object(github_pr, "_gh_request", request):
            with self.assertRaises(RuntimeError):
                github_pr.fetch_issue_comments(REPO, PR, token="t", page_cap=3)

    def test_the_claude_wrapper_refuses_a_malformed_page_and_launches_nothing(self):
        """End to end: the real wrapper, over the real transport."""

        from code_mower import claude_audit_pr
        from code_mower.provider_runners import github_pr

        for response in INVALID_COMMENT_RESPONSES:
            with self.subTest(response=response):
                request, _ = self._gh_pages(response)
                with mock.patch.object(github_pr, "_gh_request", request):
                    with self.assertRaises(RuntimeError) as raised:
                        claude_audit_pr._require_independent_review(
                            "claude", REPO, PR,
                            {"user": {"login": "a-human"},
                             "head": {"ref": BRANCH, "sha": TAKEN},
                             "labels": []},
                            TAKEN,
                            authorities=(AUTHORITY,),
                            fetch_comments=lambda: github_pr.fetch_issue_comments(
                                REPO, PR, token="t"
                            ),
                        )
                self.assertIn("lineage_unreadable", str(raised.exception))

    def test_the_direct_wrapper_accepts_a_genuinely_empty_history(self):
        episodes = reviewer_lineage.reviewer_evidence(
            REPO, PR,
            authorities=(AUTHORITY,),
            fetch_comments=lambda: [],
            state_dir=git_free_tempdir(self, "code-mower-empty-"),
        )
        self.assertEqual(episodes, ())

    def test_no_provider_is_launched_on_an_invalid_read(self):
        from code_mower import claude_audit_pr

        with self.assertRaises(RuntimeError) as raised:
            claude_audit_pr._require_independent_review(
                "claude", REPO, PR,
                {"user": {"login": "a-human"},
                 "head": {"ref": BRANCH, "sha": TAKEN},
                 "labels": []},
                TAKEN,
                authorities=(AUTHORITY,),
                fetch_comments=lambda: {},
            )
        self.assertIn("lineage_unreadable", str(raised.exception))


class TheGeneratedJobReadsTheWholeCommentHistory(unittest.TestCase):
    """Run the workflow's own recording step, with only `gh` replaced.

    The marker that proves a takeover is posted when the takeover happens, so
    on a long-running pull request it is among the *newest* comments. A job
    that reads one page, or that substitutes an empty list for a failed read,
    therefore does not fail -- it quietly attributes the run to whoever opened
    the pull request. Partial history and absent history must be refusals, and
    a refusal must leave no attribution artifact behind.
    """

    TEMPLATE = ROOT / "templates/workflows/builder-provenance.yml.j2"

    def _step_script(self) -> str:
        from code_mower import init

        rendered = init._render_workflow_template(
            self.TEMPLATE.read_text(encoding="utf-8"),
            {"decision_authorities": AUTHORITY},
        )
        workflow = yaml.safe_load(rendered)
        step = next(
            item
            for item in workflow["jobs"]["auto-record"]["steps"]
            if item.get("id") == "record"
        )
        # The PR number now arrives as an environment value the script checks,
        # not as an expression expanded into it, so nothing is substituted here.
        return step["run"]

    def _workflow_env(self, *, authorities=AUTHORITY, variable="", template=None):
        """The job environment GitHub composes from the *base* workflow file.

        The authority list is rendered into the workflow, so this is the whole
        point of the base-controlled event: the job's inputs come from this
        file, not from the pull request.
        """

        from code_mower import init

        rendered = init._render_workflow_template(
            (template or self.TEMPLATE).read_text(encoding="utf-8"),
            {"decision_authorities": authorities},
        )
        env = dict(yaml.safe_load(rendered).get("env") or {})
        env["CODE_MOWER_DECISION_AUTHORITIES_OVERRIDE"] = variable
        env.pop("CODE_MOWER_PACKAGE_SPEC", None)
        return {str(key): str(value) for key, value in env.items()}

    #: The job's own bound: MAX_PAGES pages of history plus one probe.
    MAX_REQUESTS = 21

    def _run_job(self, *, pages=None, raw=None, fail_from_page=None, job_env=None):
        """Execute the generated step with a page-aware fake `gh`.

        The fake answers each page request individually and records it, so the
        test can assert what the job actually asked for rather than trusting a
        single canned body.
        """

        root = git_free_tempdir(self, "code-mower-generated-job-")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        requests_log = root / "gh-requests.log"
        spec = bin_dir / "gh-spec.json"
        spec.write_text(
            json.dumps(
                {
                    "pages": pages if pages is not None else [[]],
                    "raw": raw,
                    "fail_from_page": fail_from_page,
                    "log": str(requests_log),
                }
            ),
            encoding="utf-8",
        )
        gh_py = bin_dir / "gh_fake.py"
        gh_py.write_text(
            "import json, re, sys\n"
            f"spec = json.load(open({str(spec)!r}, encoding='utf-8'))\n"
            "with open(spec['log'], 'a', encoding='utf-8') as fh:\n"
            "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
            "match = re.search(r'[?&]page=(\\d+)', sys.argv[-1])\n"
            "page = int(match.group(1)) if match else 1\n"
            "fail = spec['fail_from_page']\n"
            "if fail is not None and page >= fail:\n"
            "    sys.stderr.write('gh: HTTP 502\\n')\n"
            "    raise SystemExit(1)\n"
            "if spec['raw'] is not None:\n"
            "    sys.stdout.write(spec['raw'])\n"
            "    raise SystemExit(0)\n"
            "pages = spec['pages']\n"
            "body = pages[page - 1] if page - 1 < len(pages) else []\n"
            "sys.stdout.write(json.dumps(body))\n",
            encoding="utf-8",
        )
        (bin_dir / "gh").write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} {shlex.quote(str(gh_py))} \"$@\"\n",
            encoding="utf-8",
        )
        (bin_dir / "gh").chmod(0o755)
        self._requests_log = requests_log

        shim = bin_dir / "auto_record_shim.py"
        shim.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
            "from code_mower import builder_runs\n"
            "args = sys.argv[1:]\n"
            "assert args[0] == 'builder', args\n"
            "raise SystemExit(builder_runs.main(args[1:]))\n",
            encoding="utf-8",
        )
        (bin_dir / "code-mower").write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} {shlex.quote(str(shim))} \"$@\"\n",
            encoding="utf-8",
        )
        (bin_dir / "code-mower").chmod(0o755)

        event_path = root / "event.json"
        pull_request = _pull_request()
        pull_request["user"] = {"login": OPENER}
        pull_request["labels"] = [{"name": "builder:codex"}]
        event_path.write_text(
            json.dumps({"pull_request": pull_request}), encoding="utf-8"
        )
        script = root / "record.sh"
        script.write_text(self._step_script(), encoding="utf-8")
        completed = subprocess.run(
            ["bash", str(script)],
            cwd=str(root),
            env={
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "HOME": str(root),
                "GITHUB_REPOSITORY": REPO,
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_OUTPUT": str(root / "github_output"),
                "CODE_MOWER_PR_NUMBER": str(PR),
                **(self._workflow_env() if job_env is None else dict(job_env)),
            },
            capture_output=True,
            text=True,
            timeout=120,
        )
        artifact = root / f".code-mower/builder-runs/pr-{PR}.cloud-event.json"
        return completed, (
            json.loads(artifact.read_text(encoding="utf-8"))
            if artifact.is_file()
            else None
        )

    def _filler(self, count):
        return [
            {"user": {"login": OUTSIDER}, "body": f"ordinary comment {index}"}
            for index in range(count)
        ]

    def _requests(self):
        if not self._requests_log.is_file():
            return []
        return [
            line
            for line in self._requests_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _assert_refused(self, completed, artifact):
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIsNone(artifact, "a refusal may leave no attribution artifact")

    def _assert_bounded(self):
        requests = self._requests()
        self.assertLessEqual(
            len(requests), self.MAX_REQUESTS, "the job asked for more than its bound"
        )
        for request in requests:
            self.assertNotIn("--paginate", request, "pagination must stay explicit")
            self.assertIn("page=", request, "each request names the page it wants")

    def test_a_trusted_marker_beyond_the_first_page_is_still_read(self):
        completed, artifact = self._run_job(
            pages=[self._filler(100), [marker_comment()]]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsNotNone(artifact)
        self.assertEqual(
            artifact["dimensions"]["builder_current_writer"], "codex"
        )
        self.assertEqual(artifact["dimensions"]["builder_executor"],
                         "chatgpt-codex-connector")
        self._assert_bounded()
        self.assertEqual(len(self._requests()), 2, "page 1 short-circuits nothing")

    def test_an_empty_final_page_is_read_as_the_end_of_the_history(self):
        completed, artifact = self._run_job(
            pages=[self._filler(99) + [marker_comment()], []]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            artifact["dimensions"]["builder_current_writer"], "codex"
        )
        # Page one is full, so page two is asked for and comes back empty.
        self.assertEqual(len(self._requests()), 2)
        self._assert_bounded()

    def test_an_exact_page_boundary_probes_one_more_page(self):
        """A full page is not the end of the history until the next one is."""

        completed, artifact = self._run_job(
            pages=[self._filler(100), self._filler(99) + [marker_comment()]]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            artifact["dimensions"]["builder_current_writer"], "codex"
        )
        self.assertEqual(len(self._requests()), 3, "full page 2 is probed by page 3")
        self._assert_bounded()

    def test_a_first_page_failure_refuses_instead_of_attributing_the_opener(self):
        completed, artifact = self._run_job(fail_from_page=1)
        self._assert_refused(completed, artifact)

    def test_a_later_page_failure_refuses(self):
        completed, artifact = self._run_job(
            pages=[self._filler(100), self._filler(100)], fail_from_page=2
        )
        self._assert_refused(completed, artifact)
        self.assertEqual(len(self._requests()), 2, "it stopped at the failure")

    def test_a_malformed_api_shape_refuses(self):
        shapes = (
            '{"comments": []}',          # an object, not a page
            '[{"user": {}}, "text"]',    # a non-object entry
            '[[{"user": {}}]]',          # a nested page array
        )
        for shape in shapes:
            with self.subTest(shape=shape):
                completed, artifact = self._run_job(raw=shape)
                self._assert_refused(completed, artifact)

    def test_truncated_json_refuses(self):
        completed, artifact = self._run_job(raw='[{"user": {"login":')
        self._assert_refused(completed, artifact)

    def test_an_oversized_page_refuses(self):
        completed, artifact = self._run_job(pages=[self._filler(101)])
        self._assert_refused(completed, artifact)

    def test_a_history_past_the_comment_cap_refuses(self):
        # Twenty full pages, and a twenty-first that still has more: the probe
        # is what separates "exactly at the cap" from "past it".
        completed, artifact = self._run_job(
            pages=[self._filler(100) for _ in range(21)]
        )
        self._assert_refused(completed, artifact)
        self.assertIn("longer than", completed.stderr + completed.stdout)
        self._assert_bounded()
        self.assertEqual(len(self._requests()), self.MAX_REQUESTS)

    def test_a_history_exactly_at_the_cap_is_accepted(self):
        pages = [self._filler(100) for _ in range(19)]
        pages.append(self._filler(99) + [marker_comment()])
        completed, artifact = self._run_job(pages=pages)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            artifact["dimensions"]["builder_current_writer"], "codex"
        )
        self.assertEqual(len(self._requests()), self.MAX_REQUESTS)
        self._assert_bounded()

    def test_the_comments_handed_on_are_one_flat_array(self):
        """Nested page arrays are silently ignored by the reader downstream."""

        completed, _ = self._run_job(
            pages=[self._filler(100), [marker_comment()]]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        written = json.loads(
            (self._requests_log.parent / ".code-mower/pr-comments.json")
            .read_text(encoding="utf-8")
        )
        self.assertIsInstance(written, list)
        self.assertEqual(len(written), 101)
        for entry in written:
            self.assertIsInstance(entry, dict)

    def test_the_workflow_runs_from_the_base_branch_not_the_proposal(self):
        """The authority list lives in this file, so the file must be trusted.

        Under `pull_request` GitHub runs the workflow as it exists on the
        proposed revision, which would let a contributor edit their own copy
        and name themselves an authority. `pull_request_target` runs the base
        copy, in the base context.
        """

        for template in (
            ROOT / "templates/workflows/builder-provenance.yml.j2",
            ROOT / "src/code_mower/templates/workflows/builder-provenance.yml.j2",
        ):
            with self.subTest(template=template.name):
                from code_mower import init

                rendered = init._render_workflow_template(
                    template.read_text(encoding="utf-8"),
                    {"decision_authorities": AUTHORITY},
                )
                workflow = yaml.safe_load(rendered)
                triggers = workflow[True] if True in workflow else workflow["on"]
                self.assertEqual(list(triggers), ["pull_request_target"])
                self.assertEqual(
                    workflow["permissions"],
                    {"contents": "read", "pull-requests": "read"},
                    "a base-context token stays read-only",
                )
                # The base context is only safe while nothing from the
                # proposal is fetched, built or executed.
                self.assertNotIn("actions/checkout", rendered)
                self.assertNotIn("code-mower.yml", rendered)
                for step in workflow["jobs"]["auto-record"]["steps"]:
                    run = step.get("run", "")
                    self.assertNotIn("git ", run)
                    self.assertNotIn("requirements", run)
                self.assertIn("pull_request_target", rendered)

    def test_a_proposed_workflow_and_config_cannot_change_who_is_trusted(self):
        """The hostile-head case, end to end through real auto-record.

        The proposal carries its own copy of this workflow naming an outsider
        as an authority, and its own `code-mower.yml` doing the same. The job
        runs from the base copy, so neither is read: the outsider's marker
        stays untrusted and the reviewed authority's is believed.
        """

        hostile = git_free_tempdir(self, "code-mower-hostile-head-")
        (hostile / "code-mower.yml").write_text(
            f"decisions:\n  authorities:\n    - {OUTSIDER}\n", encoding="utf-8"
        )
        proposed = hostile / "builder-provenance.yml.j2"
        proposed.write_text(
            self.TEMPLATE.read_text(encoding="utf-8").replace(
                "__DECISION_AUTHORITIES__", f'"{OUTSIDER}"'
            ),
            encoding="utf-8",
        )
        # What the proposal *would* have supplied, had it been trusted.
        self.assertEqual(
            self._workflow_env(template=proposed, authorities=OUTSIDER)[
                "CODE_MOWER_DECISION_AUTHORITIES"
            ],
            OUTSIDER,
        )

        trusted = self._workflow_env(authorities=AUTHORITY)
        self.assertEqual(trusted["CODE_MOWER_DECISION_AUTHORITIES"], AUTHORITY)

        outsider_marker = {
            "user": {"login": OUTSIDER},
            "body": MARKER_BODY
            + builder_lineage.lineage_comment_marker((takeover_episode(),)),
        }
        completed, artifact = self._run_job(
            pages=[[outsider_marker]], job_env=trusted
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotEqual(
            artifact["dimensions"]["builder_executor"],
            "chatgpt-codex-connector",
            "a proposed authority may not be believed",
        )

        completed, artifact = self._run_job(
            pages=[[marker_comment()]], job_env=trusted
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            artifact["dimensions"]["builder_executor"], "chatgpt-codex-connector",
            "the reviewed authority is still believed",
        )

    def test_the_repository_variable_still_overrides_the_reviewed_list(self):
        """A repository setting is not part of any revision, so it still wins."""

        env = self._workflow_env(authorities="someone-else", variable=AUTHORITY)
        completed, artifact = self._run_job(
            pages=[[marker_comment()]], job_env=env
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            artifact["dimensions"]["builder_executor"], "chatgpt-codex-connector"
        )

    def test_a_non_numeric_bound_target_refuses(self):
        """Event data names the target; it never becomes part of a command."""

        root = git_free_tempdir(self, "code-mower-bad-number-")
        script = root / "record.sh"
        script.write_text(self._step_script(), encoding="utf-8")
        result = subprocess.run(
            ["bash", str(script)],
            cwd=str(root),
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(root),
                 "GITHUB_REPOSITORY": REPO, "CODE_MOWER_PR_NUMBER": "12; rm -rf /"},
            capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("numeric pull request", result.stderr)

    def test_an_empty_history_still_records_the_opener(self):
        """Absence of evidence is not a failure -- only unreadability is."""

        completed, artifact = self._run_job(pages=[[]])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(artifact["dimensions"]["builder_executor"], "devin")
        self.assertEqual(len(self._requests()), 1)


class AttributionMovesOnlyOnAVerifiedTransition(unittest.TestCase):
    """Lanes are coarser than the transports inside them.

    An ordinary `devin/` branch infers the local `devin_cli` transport but
    normalizes to lane `devin`, whose canonical attribution is the hosted pair.
    Comparing transports against that pair rewrote every ordinary local Devin
    CLI run into a hosted one, cleared its builder id and claimed high
    confidence -- from identity-only resolution that had seen no episode at
    all. Attribution may move only when verified evidence shows the writer
    actually changed lanes.
    """

    def _record(self, *, branch, author, comments=(), labels=("builder:devin",)):
        from code_mower import builder_runs

        with _private_root(self) as root:
            pull_request = _pull_request()
            pull_request["head"] = {"sha": TAKEN, "ref": branch}
            pull_request["user"] = {"login": author}
            pull_request["labels"] = [{"name": name} for name in labels]
            pr_json = Path(root) / "event.json"
            pr_json.write_text(
                json.dumps({"pull_request": pull_request}), encoding="utf-8"
            )
            comments_json = Path(root) / "comments.json"
            comments_json.write_text(json.dumps(list(comments)), encoding="utf-8")
            output = Path(root) / "run.json"
            env = {
                "CODE_MOWER_DECISION_AUTHORITIES": AUTHORITY,
                "CODE_MOWER_DECISION_AUTHORITIES_OVERRIDE": "",
            }
            captured = _Capture()
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch("sys.stdout", captured):
                code = builder_runs.main([
                    "auto-record",
                    "--pr-json", str(pr_json),
                    "--repo", REPO,
                    "--comments-json", str(comments_json),
                    "--output", str(output),
                    "--force",
                    "--json",
                ])
            self.assertEqual(code, 0)
            payload = json.loads(captured.text())
            payload["_event"] = json.loads(output.read_text(encoding="utf-8"))
            return payload

    def _dimensions(self, payload):
        return payload["_event"]["dimensions"]

    def test_an_ordinary_devin_slash_branch_stays_local_devin_cli(self):
        payload = self._record(branch="devin/963-thing", author="a-human")
        self.assertEqual(payload["provider"], "devin_cli")
        self.assertEqual(payload["executor"], "devin_cli")
        dimensions = self._dimensions(payload)
        self.assertTrue(dimensions["builder_id"], "the inferred id must survive")
        self.assertEqual(dimensions["builder_inference_confidence"], "medium")
        self.assertNotIn(
            "builder_lineage:devin", dimensions["builder_inference_signals"]
        )

    def test_an_ordinary_devin_dash_branch_stays_local_devin_cli(self):
        payload = self._record(branch="devin-963-thing", author="a-human")
        self.assertEqual(payload["provider"], "devin_cli")
        self.assertEqual(payload["executor"], "devin_cli")
        self.assertEqual(
            self._dimensions(payload)["builder_inference_confidence"], "medium"
        )

    def test_an_ordinary_hosted_devin_pull_request_stays_hosted(self):
        payload = self._record(branch="devin/963-thing", author=OPENER)
        self.assertEqual(payload["provider"], "devin")
        self.assertEqual(payload["executor"], "devin")
        self.assertEqual(
            self._dimensions(payload)["builder_inference_confidence"], "high"
        )

    def test_a_same_lane_continuation_keeps_its_established_transport(self):
        """Evidence exists, but the writer never left the lane it started in."""

        episodes = (
            builder_lineage.ContributionEpisode(
                sequence=1,
                kind=builder_lineage.HANDOFF_KIND,
                repo=REPO,
                pr_number=PR,
                branch="devin/963-thing",
                source_lane="codex",
                destination_lane="devin",
                expected_head="a" * 40,
                resulting_head=TAKEN,
                writer_state="terminated",
            ),
        )
        marker = {
            "user": {"login": AUTHORITY},
            "body": MARKER_BODY + builder_lineage.lineage_comment_marker(episodes),
        }
        payload = self._record(
            branch="devin/963-thing", author="a-human", comments=[marker]
        )
        self.assertEqual(payload["provider"], "devin_cli")
        self.assertEqual(payload["executor"], "devin_cli")
        self.assertEqual(
            self._dimensions(payload)["builder_current_writer"], "devin"
        )

    def test_a_verified_cross_lane_takeover_attributes_the_current_writer(self):
        payload = self._record(
            branch=BRANCH, author=OPENER, comments=[marker_comment()],
            labels=("builder:codex",),
        )
        self.assertEqual(payload["provider"], "codex")
        self.assertEqual(payload["executor"], "chatgpt-codex-connector")
        dimensions = self._dimensions(payload)
        self.assertEqual(dimensions["builder_current_writer"], "codex")
        self.assertEqual(dimensions["builder_inference_confidence"], "high")
        self.assertIn(
            "builder_lineage:codex", dimensions["builder_inference_signals"]
        )
        self.assertEqual(
            dimensions["builder_id"], "", "no id may be invented for another lane"
        )

    def test_an_untrusted_takeover_marker_moves_no_attribution(self):
        payload = self._record(
            branch=BRANCH, author=OPENER, comments=[marker_comment(OUTSIDER)],
            labels=("builder:devin",),
        )
        self.assertEqual(payload["provider"], "devin")
        self.assertEqual(payload["executor"], "devin")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
