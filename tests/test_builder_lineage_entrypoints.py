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
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_mower import builder_lineage, lane_delivery, lane_handoff  # noqa: E402
from code_mower import saas_reviewer_labeler as labeler  # noqa: E402

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
    """The generated job must hand auto-record a usable trust rule."""

    TEMPLATES = (
        ROOT / "templates/workflows/builder-provenance.yml.j2",
        ROOT / "src/code_mower/templates/workflows/builder-provenance.yml.j2",
    )

    def _rendered(self, path: Path) -> str:
        return (
            path.read_text(encoding="utf-8")
            .replace("{% raw %}", "")
            .replace("{% endraw %}", "")
        )

    def test_both_maintained_templates_render_the_same_contract(self):
        first, second = (self._rendered(path) for path in self.TEMPLATES)
        self.assertEqual(first, second)
        for text in (first, second):
            self.assertIn("CODE_MOWER_DECISION_AUTHORITIES_OVERRIDE:", text)
            self.assertIn("vars.CODE_MOWER_DECISION_AUTHORITIES", text)
            self.assertIn("decision_authorities_from_config", text)
            self.assertIn("CODE_MOWER_DECISION_AUTHORITIES=", text)

    def _authority_step_source(self) -> str:
        text = self._rendered(self.TEMPLATES[0])
        body = text.split("python - <<'PY'", 1)[1].split("PY\n", 1)[0]
        return "\n".join(line[10:] for line in body.splitlines()[1:])

    def _authorities_from_generated_step(self, config_text: str) -> str:
        """Run the generated job's own authority step over a repo config."""

        with _private_root(self) as root:
            (Path(root) / "code-mower.yml").write_text(config_text, encoding="utf-8")
            captured = _Capture()
            cwd = os.getcwd()
            os.chdir(root)
            try:
                with mock.patch("sys.stdout", captured):
                    exec(compile(self._authority_step_source(), "<job>", "exec"), {})
            finally:
                os.chdir(cwd)
        line = captured.text().strip()
        self.assertTrue(line.startswith("CODE_MOWER_DECISION_AUTHORITIES="))
        self.assertEqual(len(line.splitlines()), 1, "one GITHUB_ENV assignment")
        return line.split("=", 1)[1]

    def _auto_record(self, *, authorities: str, comments) -> dict:
        from code_mower import builder_runs

        with _private_root(self) as root:
            pr_json = Path(root) / "event.json"
            pr_json.write_text(
                json.dumps({"pull_request": _pull_request()}), encoding="utf-8"
            )
            comments_json = Path(root) / "comments.json"
            comments_json.write_text(json.dumps(comments), encoding="utf-8")
            output = Path(root) / "run.json"
            env = {
                "CODE_MOWER_DECISION_AUTHORITIES": authorities,
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
            payload["_event"] = (
                json.loads(output.read_text(encoding="utf-8"))
                if output.is_file()
                else {}
            )
            return payload

    def test_the_generated_step_resolves_configured_authorities(self):
        resolved = self._authorities_from_generated_step(
            "decisions:\n  authorities:\n    - codemower-ai\n"
        )
        self.assertEqual(resolved, AUTHORITY)

    def test_a_generated_run_attributes_the_takeover_to_its_current_writer(self):
        authorities = self._authorities_from_generated_step(
            "decisions:\n  authorities:\n    - codemower-ai\n"
        )
        payload = self._auto_record(
            authorities=authorities, comments=[marker_comment()]
        )
        self.assertEqual(payload["status"], "recorded")
        # The verified current writer, not the Devin account that opened it.
        self.assertEqual(payload["executor"], "chatgpt-codex-connector")

    def test_empty_authorities_trust_no_marker(self):
        resolved = self._authorities_from_generated_step("owner_surface: {}\n")
        self.assertEqual(resolved, "")
        payload = self._auto_record(authorities=resolved, comments=[marker_comment()])
        self.assertNotEqual(payload.get("executor"), "chatgpt-codex-connector")

    def test_an_untrusted_marker_is_discarded(self):
        authorities = self._authorities_from_generated_step(
            "decisions:\n  authorities:\n    - codemower-ai\n"
        )
        payload = self._auto_record(
            authorities=authorities, comments=[marker_comment(OUTSIDER)]
        )
        self.assertNotEqual(payload.get("executor"), "chatgpt-codex-connector")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
