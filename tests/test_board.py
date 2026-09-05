from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import http.client
import json
import math
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch
from io import StringIO

from code_mower import board, board_store, lane_status, reviewer_spend


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


BOARD_POLL_HARNESS = """
__CONSTANTS__
const put = () => {};
const render = () => {};
const renderEvents = () => {};
const timers = [];
let clearedCount = 0;
const setTimeout = (fn, ms) => {
  const timer = {fn, ms};
  timers.push(timer);
  return timer;
};
const clearTimeout = () => {
  clearedCount += 1;
};
let failNext = false;
let nextCache = null;
const fetch = async (url) => {
  if (failNext) throw new Error("network down");
  const status = {board: nextCache === null ? {} : {cache: nextCache}};
  return {json: async () => (url === "/api/status" ? status : {events: []})};
};
__SCHEDULING__
(async () => {
  const steps = [];
  for (const step of JSON.parse(process.argv[2])) {
    failNext = step === "fetch-error";
    nextCache = failNext ? null : step;
    const armedBefore = timers.length;
    const clearedBefore = clearedCount;
    await load();
    const armed = timers[timers.length - 1];
    steps.push({
      delay: armed.ms,
      armed: timers.length - armedBefore,
      cleared: clearedCount - clearedBefore,
      attempts: fastPollAttempts,
      pending: pollTimer === armed,
    });
  }
  console.log(JSON.stringify(steps));
})();
"""


def _run_board_poll_script(steps: list[dict[str, object] | str | None]) -> list[dict[str, object]]:
    """Replay the Board page's own next-poll decision for a sequence of responses.

    The constants, predicates, and the whole self-scheduling loop are lifted
    verbatim out of the rendered page, so the test drives the shipped
    JavaScript rather than a Python restatement of it. Each step runs one real
    ``load()`` against a stubbed fetch: a cache metadata object, ``None`` for a
    payload carrying no cache metadata, or ``"fetch-error"`` for a request that
    fails outright.
    """

    html = board.render_board_html(board.BoardConfig(repo="owner/repo"))
    constants = re.search(
        r"^ *const REFRESH_MS = .*?\n( *)const freshDelayMs = .*?\n\1\};\n",
        html,
        re.MULTILINE | re.DOTALL,
    )
    scheduling = re.search(
        r"^( *)let pollTimer = null;.*?\n\1async function load\(\) \{.*?\n\1\}\n",
        html,
        re.MULTILINE | re.DOTALL,
    )
    if constants is None or scheduling is None:  # pragma: no cover - guards the extraction
        raise AssertionError("board HTML no longer exposes the self-scheduling poll loop")
    script = BOARD_POLL_HARNESS.replace("__CONSTANTS__", constants.group(0)).replace(
        "__SCHEDULING__", scheduling.group(0)
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "poll.js"
        path.write_text(script, encoding="utf-8")
        completed = subprocess.run(
            [shutil.which("node") or "node", str(path), json.dumps(steps)],
            capture_output=True,
            text=True,
            check=True,
        )
    return json.loads(completed.stdout)


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def _gh_json(args: list[str]) -> object:
    if args[:2] == ["pr", "list"]:
        return [
            {
                "number": 7,
                "title": "Adopt board",
                "url": "https://github.com/owner/repo/pull/7",
                "headRefName": "codex/board",
                "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                "author": {"login": "codex-bot"},
                "isDraft": False,
                "mergeStateStatus": "CLEAN",
                "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                "labels": [{"name": "builder:codex"}, {"name": "claude-audit-done"}],
                "statusCheckRollup": [{"context": "code-mower/gate", "state": "SUCCESS"}],
            },
        ]
    if args[:2] == ["run", "list"]:
        return [
            {
                "databaseId": 77,
                "workflowName": "Code Mower gate",
                "displayTitle": "publish gate",
                "status": "completed",
                "conclusion": "success",
                "event": "pull_request",
                "headBranch": "codex/board",
                "createdAt": NOW.isoformat().replace("+00:00", "Z"),
                "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                "url": "https://github.com/owner/repo/actions/runs/77",
            },
        ]
    raise lane_status.LaneStatusUnavailable("unexpected gh call")


def _command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    if args[:4] == ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]:
        return _completed("p123\ncnode\nn127.0.0.1:5332\n")
    if args == ["ps", "-p", "123", "-o", "command="]:
        return _completed("code-mower board serve --repo owner/repo\n")
    if args == ["lsof", "-a", "-p", "123", "-d", "cwd", "-Fn"]:
        return _completed("p123\nn/tmp/lane-checkout\n")
    if args == ["ps", "-axo", "pid=,command="]:
        return _completed(" 456 codex exec review\n")
    if args == ["lsof", "-a", "-p", "456", "-d", "cwd", "-Fn"]:
        return _completed("p456\nn/tmp/codex-lane\n")
    return _completed("", returncode=1)


def _write_board_config(path: Path) -> None:
    path.write_text(
        """
version: 1
project:
  name: demo
  state_dir: .code-mower
repositories:
  - slug: owner/repo
    default_branch: main
owner_surface:
  ready_label: tier:R
  needs_owner_label: needs-owner
  builder_wip_cap: 2
merge_authority_excludes_author: true
builder_identity:
  labels:
    builder:codex: codex
    builder:cursor: cursor
lanes:
  codex:
    type: audit
    driver: local_cli
    provider: codex
    merge_authority: true
    labels:
      needs: needs-codex-audit
      done: codex-audit-done
      blocked: codex-audit-blocked
  claude_audit:
    type: audit
    driver: local_cli
    provider: claude
    trailer_lane: claude
    merge_authority: true
    labels:
      needs: needs-claude-audit
      done: claude-audit-done
      blocked: claude-audit-blocked
""",
        encoding="utf-8",
    )


def _fetch_status(base_url: str) -> dict:
    with urllib.request.urlopen(f"{base_url}/api/status", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _await_warm_status(base_url: str, *, timeout: float = 5.0) -> dict:
    """Poll /api/status until the background status cache completes its first refresh."""
    deadline = time.monotonic() + timeout
    payload = _fetch_status(base_url)
    while payload["board"]["cache"]["state"] == "cold" and time.monotonic() < deadline:
        time.sleep(0.02)
        payload = _fetch_status(base_url)
    if payload["board"]["cache"]["state"] == "cold":
        raise AssertionError("status cache did not warm up in time")
    return payload


def _await_cache_generation(base_url: str, generation: int, *, timeout: float = 5.0) -> dict:
    """Poll /api/status until the cache reports at least the requested completed generation."""
    deadline = time.monotonic() + timeout
    payload = _fetch_status(base_url)
    while payload["board"]["cache"]["generation"] < generation and time.monotonic() < deadline:
        time.sleep(0.02)
        payload = _fetch_status(base_url)
    if payload["board"]["cache"]["generation"] < generation:
        raise AssertionError(f"status cache did not reach generation {generation} in time")
    return payload


class _FakeClock:
    """A controllable monotonic-style clock for deterministic StatusCache tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, delta: float) -> None:
        self.value += delta


def _occupy_loopback_port(start: int = 5332, stop: int = 5400) -> socket.socket:
    for port in range(start, stop):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            sock.listen()
            return sock
        except OSError:
            sock.close()
    raise RuntimeError("could not reserve a loopback port for Board test")


class BoardTests(TestCase):
    def test_render_board_html_contains_local_app_shell(self) -> None:
        html = board.render_board_html(board.BoardConfig(repo="owner/repo"))

        self.assertIn("Code Mower Board", html)
        self.assertIn("/api/status", html)
        self.assertIn("/api/events", html)
        self.assertIn("Owner Queue", html)
        self.assertIn("Productivity", html)
        self.assertIn("Agent Cards", html)
        self.assertIn("Open PRs", html)
        self.assertIn("Recent Local History", html)
        self.assertIn("Reviewer Verdict Timeline", html)
        self.assertIn("Spend And Latency", html)
        self.assertIn("Supervised Pilot", html)
        self.assertIn('id="supervised"', html)
        self.assertIn('id="productivity"', html)
        self.assertIn("const href", html)
        self.assertIn('id="version"', html)
        self.assertIn("servingVersion", html)
        self.assertIn("const localTime", html)
        self.assertIn("Intl.DateTimeFormat(undefined", html)
        self.assertIn('title="UTC ', html)
        self.assertIn("next_detail", html)
        self.assertIn("productivityMetrics", html)

    def test_render_board_html_escapes_script_terminators(self) -> None:
        html = board.render_board_html(board.BoardConfig(repo="owner/repo</script><b>bad</b>"))

        self.assertIn("owner/repo<\\/script><b>bad<\\/b>", html)
        self.assertNotIn("owner/repo</script><b>bad</b>", html)

    def test_render_board_html_polls_with_one_self_scheduling_timer(self) -> None:
        html = board.render_board_html(board.BoardConfig(repo="owner/repo"))

        self.assertIn("FAST_POLL_MS = 750", html)
        self.assertIn("FAST_POLL_MAX_ATTEMPTS = 20", html)
        self.assertIn("MIN_POLL_MS = 250", html)
        # A stale snapshot with a refresh already in flight is treated exactly
        # like the cold warming state: the next completed snapshot is seconds
        # away. Both states require refresh_in_progress, so a cold or stale
        # cache sitting in the server's retry backoff (or wedged by a failed
        # thread start) is not fast polled -- no refresh is coming to wait for.
        self.assertIn(
            '(cache?.state === "cold" || cache?.state === "stale") && cache?.refresh_in_progress === true',
            html,
        )
        self.assertIn("fastPollAttempts >= FAST_POLL_MAX_ATTEMPTS) return REFRESH_MS", html)
        # A fresh response paces itself off the server's own TTL metadata
        # instead of a page-load-anchored interval, and only trusts real JSON
        # numbers.
        self.assertIn("const finiteNumber = (value) => (typeof value === \"number\" && Number.isFinite(value) ? value : null);", html)
        self.assertIn("Math.min(Math.max((ttl - age) * 1000, MIN_POLL_MS), REFRESH_MS)", html)
        self.assertIn("return freshDelayMs(cache) ?? REFRESH_MS;", html)
        # One timer variable, one scheduling helper, no competing interval: the
        # helper clears the pending timer before arming the next one, so a
        # normal-interval poll and a fast poll can never stack.
        self.assertNotIn("setInterval", html)
        self.assertNotIn("fastPollTimer", html)
        self.assertEqual(html.count("let pollTimer"), 1)
        self.assertEqual(html.count("setTimeout(load,"), 1)
        self.assertIn("pollTimer = setTimeout(load, delayMs);", html)
        self.assertIn("clearTimeout(pollTimer);", html)
        self.assertLess(html.index("clearTimeout(pollTimer);"), html.index("pollTimer = setTimeout(load, delayMs);"))
        # Definition plus exactly one call site, on the single path every
        # load() takes whether it succeeded or threw.
        self.assertEqual(html.count("scheduleNextLoad("), 2)
        self.assertIn("      scheduleNextLoad(delayMs);\n    }", html)

    @skipUnless(shutil.which("node"), "node is required to execute the board polling script")
    def test_board_poll_delay_for_each_cache_state(self) -> None:
        cold = {"state": "cold", "refresh_in_progress": True}
        # Each interval case is preceded by an awaiting response so the
        # assertion proves the budget was actually cleared rather than never
        # raised.
        cases = [
            cold,
            {"state": "stale", "refresh_in_progress": True},
            {"state": "fresh", "refresh_in_progress": False, "ttl_seconds": 15.0, "age_seconds": 6.0},
            cold,
            {"state": "stale", "refresh_in_progress": False, "retry_in_seconds": 5.0},
            cold,
            {"state": "cold", "refresh_in_progress": False, "retry_in_seconds": 5.0},
            cold,
            None,
        ]

        results = _run_board_poll_script(cases)

        # Cold and stale-while-refreshing both fast poll and keep counting
        # attempts toward the shared cap.
        self.assertEqual(results[0]["delay"], 750)
        self.assertEqual(results[0]["attempts"], 1)
        self.assertEqual(results[1]["delay"], 750)
        self.assertEqual(results[1]["attempts"], 2)
        # A completed fresh snapshot waits out its own remaining TTL (15s ttl,
        # 6s old) rather than a full interval anchored at page load, and resets
        # the attempt budget.
        self.assertEqual(results[2]["delay"], 9000)
        self.assertEqual(results[2]["attempts"], 0)
        self.assertEqual(results[3]["delay"], 750)
        # Stale with no refresh in flight (the refresh thread failed to start,
        # or the server is in its retry backoff) has nothing to wait for, so it
        # uses the normal interval and resets the budget.
        self.assertEqual(results[4]["delay"], 15000)
        self.assertEqual(results[4]["attempts"], 0)
        self.assertEqual(results[5]["delay"], 750)
        # Same for a cold cache in the retry backoff: no refresh is running, so
        # fast polling would just burn a request every 750ms until the backoff
        # deadline passes.
        self.assertEqual(results[6]["delay"], 15000)
        self.assertEqual(results[6]["attempts"], 0)
        self.assertEqual(results[7]["delay"], 750)
        # A response without cache metadata never fast polls, and likewise
        # returns the budget to zero.
        self.assertEqual(results[8]["delay"], 15000)
        self.assertEqual(results[8]["attempts"], 0)
        # Every load arms exactly one timer and clears the one it replaces, so
        # timers can never stack.
        self.assertTrue(all(step["armed"] == 1 and step["pending"] for step in results))
        self.assertEqual([step["cleared"] for step in results], [0] + [1] * (len(cases) - 1))

    @skipUnless(shutil.which("node"), "node is required to execute the board polling script")
    def test_board_poll_tracks_the_remaining_ttl_of_a_fresh_snapshot(self) -> None:
        # Cache age is measured from the moment the background refresh
        # completed, so the page schedules the next load against the age the
        # server just reported instead of a fixed interval that started at page
        # load and can sit just under the TTL forever.
        results = _run_board_poll_script(
            [
                {"state": "fresh", "refresh_in_progress": False, "ttl_seconds": 15, "age_seconds": 12},
                {"state": "fresh", "refresh_in_progress": False, "ttl_seconds": 15, "age_seconds": 0},
                {"state": "fresh", "refresh_in_progress": False, "ttl_seconds": 15, "age_seconds": 14.99},
                {"state": "fresh", "refresh_in_progress": False, "ttl_seconds": 15, "age_seconds": 15},
            ]
        )

        self.assertEqual(results[0]["delay"], 3000)
        # A snapshot computed just now still waits no longer than the
        # configured interval.
        self.assertEqual(results[1]["delay"], 15000)
        # A snapshot that is fresh by a hair (or by nothing at all, if it
        # expired between the server's own age computation and this branch)
        # floors at MIN_POLL_MS instead of scheduling a zero-delay loop.
        self.assertEqual(results[2]["delay"], 250)
        self.assertEqual(results[3]["delay"], 250)
        self.assertTrue(all(step["attempts"] == 0 for step in results))

    @skipUnless(shutil.which("node"), "node is required to execute the board polling script")
    def test_board_poll_uses_the_normal_interval_for_unusable_cache_metadata(self) -> None:
        # Only real JSON numbers may drive the delay: null and "" would coerce
        # to 0 and schedule a 250ms poll forever, and a non-numeric value would
        # produce NaN.
        results = _run_board_poll_script(
            [
                {"state": "fresh", "refresh_in_progress": False, "ttl_seconds": 15, "age_seconds": None},
                {"state": "fresh", "refresh_in_progress": False, "ttl_seconds": None, "age_seconds": 6},
                {"state": "fresh", "refresh_in_progress": False, "age_seconds": 6},
                {"state": "fresh", "refresh_in_progress": False, "ttl_seconds": "15", "age_seconds": "6"},
                {"state": "fresh", "refresh_in_progress": False, "ttl_seconds": 0, "age_seconds": 0},
                {"state": "fresh", "refresh_in_progress": False, "ttl_seconds": 15, "age_seconds": -1},
                {"refresh_in_progress": False, "ttl_seconds": 15, "age_seconds": 6},
            ]
        )

        self.assertEqual([step["delay"] for step in results], [15000] * 7)
        self.assertTrue(all(step["armed"] == 1 and step["pending"] for step in results))

    @skipUnless(shutil.which("node"), "node is required to execute the board polling script")
    def test_board_fast_poll_is_capped_for_a_stale_refreshing_cache(self) -> None:
        stale = {"state": "stale", "refresh_in_progress": True}
        fresh = {"state": "fresh", "refresh_in_progress": False, "ttl_seconds": 15, "age_seconds": 0}

        results = _run_board_poll_script([stale] * 22 + [fresh, stale])

        self.assertTrue(all(step["delay"] == 750 for step in results[:20]))
        self.assertEqual(results[19]["attempts"], 20)
        # Attempt 21 hits the cap: the browser stops fast polling a cache that
        # never completes and falls back to the normal interval. The exhausted
        # counter is *not* reset here, so a later normal-interval poll that
        # still finds the same pending refresh cannot start another 20-attempt
        # burst (which would repeat indefinitely).
        self.assertEqual(results[20], {"delay": 15000, "armed": 1, "cleared": 1, "attempts": 20, "pending": True})
        self.assertEqual(results[21]["delay"], 15000)
        self.assertEqual(results[21]["attempts"], 20)
        # Only a response that is no longer awaiting a refresh clears the
        # budget; the next pending refresh may then fast poll again.
        self.assertEqual(results[22]["delay"], 15000)
        self.assertEqual(results[22]["attempts"], 0)
        self.assertEqual(results[23]["delay"], 750)
        self.assertEqual(results[23]["attempts"], 1)

    @skipUnless(shutil.which("node"), "node is required to execute the board polling script")
    def test_board_poll_reschedules_at_the_normal_interval_after_a_fetch_error(self) -> None:
        cold = {"state": "cold", "refresh_in_progress": True}

        results = _run_board_poll_script([cold, "fetch-error", cold, "fetch-error"])

        self.assertEqual(results[0]["delay"], 750)
        # A failed request still schedules the next load -- the loop can never
        # die -- and backs off to the normal interval. It tells us nothing
        # about the server's cache, so the fast-poll budget is left alone
        # rather than reset.
        self.assertEqual(results[1]["delay"], 15000)
        self.assertEqual(results[1]["attempts"], 1)
        self.assertEqual(results[2]["delay"], 750)
        self.assertEqual(results[2]["attempts"], 2)
        self.assertEqual(results[3]["delay"], 15000)
        self.assertEqual(results[3]["attempts"], 2)
        self.assertTrue(all(step["armed"] == 1 and step["pending"] for step in results))

    def test_candidate_ports_only_auto_fall_forward_for_default_port(self) -> None:
        self.assertEqual(
            board._candidate_ports(board.BoardConfig(repo="owner/repo", port=5332, port_was_default=True)),
            list(range(5332, 5342)),
        )
        self.assertEqual(
            board._candidate_ports(board.BoardConfig(repo="owner/repo", port=6000, port_was_default=False)),
            [6000],
        )

    def test_explicit_port_conflict_message_clamps_suggestions(self) -> None:
        self.assertIn("65535", board._explicit_port_conflict_message("127.0.0.1", 65534))
        self.assertNotIn("65536", board._explicit_port_conflict_message("127.0.0.1", 65534))
        self.assertNotIn("such as", board._explicit_port_conflict_message("127.0.0.1", 65535))
        self.assertIn("code-mower board list", board._explicit_port_conflict_message("127.0.0.1", 5332))
        self.assertIn("code-mower board stop --port 5332 --yes", board._explicit_port_conflict_message("127.0.0.1", 5332))

    def test_bind_board_server_falls_forward_when_default_port_is_busy(self) -> None:
        with _occupy_loopback_port() as occupied:
            busy_port = int(occupied.getsockname()[1])
            handler = board.make_handler(board.BoardConfig(repo="owner/repo", port=busy_port))

            server = board._bind_board_server(
                board.BoardConfig(repo="owner/repo", port=busy_port, port_was_default=True),
                handler,
            )

        self.assertIsNotNone(server)
        assert server is not None
        try:
            self.assertGreaterEqual(int(server.server_address[1]), busy_port + 1)
        finally:
            server.server_close()

    def test_bind_board_server_reports_explicit_port_conflict(self) -> None:
        with _occupy_loopback_port() as occupied:
            busy_port = int(occupied.getsockname()[1])
            handler = board.make_handler(board.BoardConfig(repo="owner/repo", port=busy_port))
            err = StringIO()

            with redirect_stderr(err):
                server = board._bind_board_server(
                    board.BoardConfig(repo="owner/repo", port=busy_port, port_was_default=False),
                    handler,
                )

        self.assertIsNone(server)
        self.assertIn(f"port {busy_port} is already in use", err.getvalue())
        self.assertIn("pass --port", err.getvalue())

    def test_serve_treats_abbreviated_port_flag_as_explicit(self) -> None:
        with _occupy_loopback_port() as occupied:
            busy_port = int(occupied.getsockname()[1])
            err = StringIO()

            with redirect_stderr(err):
                code = board.main(["serve", "--repo", "owner/repo", "--po", str(busy_port)])

        self.assertEqual(code, 2)
        self.assertIn(f"port {busy_port} is already in use", err.getvalue())

    def test_serve_rejects_invalid_port_before_binding(self) -> None:
        err = StringIO()

        with redirect_stderr(err):
            code = board.serve(board.BoardConfig(repo="owner/repo", port=70000))

        self.assertEqual(code, 2)
        self.assertIn("--port", err.getvalue())

    def test_serve_rejects_zero_refresh_seconds(self) -> None:
        err = StringIO()

        with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            board.main(["serve", "--repo", "owner/repo", "--refresh-seconds", "0"])

        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--refresh-seconds", err.getvalue())

    def test_serve_rejects_negative_refresh_seconds(self) -> None:
        err = StringIO()

        with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            board.main(["serve", "--repo", "owner/repo", "--refresh-seconds", "-1"])

        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--refresh-seconds", err.getvalue())

    def test_serve_accepts_positive_refresh_seconds(self) -> None:
        with patch("code_mower.board.serve", return_value=0) as fake_serve:
            code = board.main(["serve", "--repo", "owner/repo", "--refresh-seconds", "5"])

        self.assertEqual(code, 0)
        fake_serve.assert_called_once()
        config = fake_serve.call_args.args[0]
        self.assertEqual(config.refresh_seconds, 5)

    def test_board_inventory_payload_enriches_versions_and_redacts_paths(self) -> None:
        def status_probe(_board_item: dict[str, object]) -> dict[str, object]:
            return {
                "schema": lane_status.LANE_STATUS_SCHEMA,
                "repo": "owner/repo",
                "board": {
                    "version": {
                        "serving_version": "0.9.3b1",
                        "installed_version": "0.9.4b1",
                        "restart_recommended": True,
                    }
                },
            }

        payload = board.board_inventory_payload(command_runner=_command_runner, status_probe=status_probe)

        self.assertEqual(payload["schema"], board.BOARD_INVENTORY_SCHEMA)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["boards"][0]["repo"], "owner/repo")
        self.assertEqual(payload["boards"][0]["url"], "http://127.0.0.1:5332/")
        self.assertEqual(payload["boards"][0]["serving_version"], "0.9.3b1")
        self.assertEqual(payload["boards"][0]["installed_version"], "0.9.4b1")
        self.assertTrue(payload["boards"][0]["restart_recommended"])
        self.assertEqual(payload["boards"][0]["cwd"], lane_status.LOCAL_PATH_REDACTION)
        self.assertEqual(payload["next_action"], "restart stale Board")
        self.assertIn("port(s) 5332", payload["next_detail"])

    def test_board_inventory_payload_handles_missing_process_permissions(self) -> None:
        payload = board.board_inventory_payload(
            command_runner=lambda _args: _completed("", returncode=1),
            status_probe=None,
        )

        self.assertFalse(payload["available"])
        self.assertEqual(payload["boards"], [])
        self.assertEqual(payload["next_action"], "fix local process inspection")

    def test_board_inventory_payload_marks_unresponsive_listener_without_restart(self) -> None:
        payload = board.board_inventory_payload(
            command_runner=_command_runner,
            status_probe=lambda _board_item: {"available": False, "message": "connection refused"},
        )

        self.assertEqual(payload["boards"][0]["health"], "unresponsive")
        self.assertFalse(payload["boards"][0]["restart_recommended"])
        self.assertEqual(payload["next_action"], "inspect unresponsive Board")
        self.assertIn("did not answer", payload["next_detail"])

    def test_board_inventory_payload_marks_legacy_listener_restart_recommended(self) -> None:
        payload = board.board_inventory_payload(
            command_runner=_command_runner,
            status_probe=lambda _board_item: {
                "available": False,
                "reason": "legacy_identity_endpoint_missing",
                "message": "endpoint missing",
            },
        )

        self.assertEqual(payload["boards"][0]["health"], "legacy")
        self.assertTrue(payload["boards"][0]["restart_recommended"])
        self.assertIn("legacy / restart recommended", payload["boards"][0]["status_message"])
        self.assertEqual(payload["next_action"], "restart stale Board")
        rendered = board.render_inventory_text(payload)
        self.assertIn("health=legacy / restart recommended", rendered)

    def test_stop_board_requires_confirmation_and_stops_matching_board(self) -> None:
        stopped: list[tuple[int, int]] = []

        dry_run = board.stop_board(port=5332, command_runner=_command_runner, killer=lambda *_args: stopped.append(_args))

        self.assertEqual(dry_run["status"], "confirmation_required")
        self.assertEqual(stopped, [])

        result = board.stop_board(
            port=5332,
            yes=True,
            command_runner=_command_runner,
            killer=lambda pid, sig: stopped.append((pid, sig)),
        )

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(stopped, [(123, signal.SIGTERM)])
        self.assertEqual(result["stopped"][0]["repo"], "owner/repo")
        self.assertEqual(result["matches"][0]["cwd"], lane_status.LOCAL_PATH_REDACTION)
        self.assertEqual(result["stopped"][0]["cwd"], lane_status.LOCAL_PATH_REDACTION)
        self.assertNotIn("/tmp/lane-checkout", json.dumps(result))

    def test_stop_board_does_not_stop_unknown_process(self) -> None:
        stopped: list[tuple[int, int]] = []

        result = board.stop_board(
            pid=999,
            yes=True,
            command_runner=_command_runner,
            killer=lambda pid, sig: stopped.append((pid, sig)),
        )

        self.assertEqual(result["status"], "not_found")
        self.assertEqual(stopped, [])

    def test_stop_board_does_not_stop_medium_confidence_listener(self) -> None:
        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:4] == ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]:
                return _completed("p321\ncPython\nn127.0.0.1:5332\n")
            if args == ["ps", "-p", "321", "-o", "command="]:
                return _completed("python -m http.server 5332\n")
            if args == ["lsof", "-a", "-p", "321", "-d", "cwd", "-Fn"]:
                return _completed("p321\nn/tmp/not-code-mower\n")
            return _completed("", returncode=1)

        stopped: list[tuple[int, int]] = []
        result = board.stop_board(
            port=5332,
            yes=True,
            command_runner=command_runner,
            killer=lambda pid, sig: stopped.append((pid, sig)),
        )

        self.assertEqual(result["status"], "not_found")
        self.assertEqual(stopped, [])

    def test_status_payload_redacts_local_paths_by_default(self) -> None:
        payload = board.status_payload(
            board.BoardConfig(repo="owner/repo"),
            gh_json_runner=_gh_json,
            command_runner=_command_runner,
        )

        serialized = json.dumps(payload)
        self.assertEqual(payload["schema"], lane_status.LANE_STATUS_SCHEMA)
        self.assertEqual(payload["board"]["schema"], "code_mower.board.v1")
        self.assertEqual(payload["board"]["mode"], "local_read_only")
        self.assertEqual(payload["board"]["local_paths"], "redacted")
        self.assertEqual(payload["board"]["version"]["serving_version"], board.CODE_MOWER_VERSION)
        self.assertIn("restart_recommended", payload["board"]["version"])
        self.assertEqual(payload["productivity"]["schema"], "code_mower.boardProductivity.v1")
        self.assertEqual(payload["productivity"]["current"]["open_pr_count"], 1)
        self.assertEqual(payload["productivity"]["current"]["active_lane_count"], 1)
        self.assertIn(lane_status.LOCAL_PATH_REDACTION, serialized)
        self.assertNotIn("/tmp/lane-checkout", serialized)
        self.assertNotIn("/tmp/codex-lane", serialized)

    def test_board_version_payload_detects_upgrade_restart_hint(self) -> None:
        with patch("code_mower.board.CODE_MOWER_VERSION", "0.9.1b1"):
            with patch("code_mower.board._installed_package_version", return_value="0.9.2b1"):
                payload = board.board_version_payload()

        self.assertEqual(payload["serving_version"], "0.9.1b1")
        self.assertEqual(payload["installed_version"], "0.9.2b1")
        self.assertTrue(payload["restart_recommended"])

    def test_status_payload_can_show_local_paths_for_debugging(self) -> None:
        payload = board.status_payload(
            board.BoardConfig(repo="owner/repo", show_local_paths=True),
            gh_json_runner=_gh_json,
            command_runner=_command_runner,
        )

        serialized = json.dumps(payload)
        self.assertEqual(payload["board"]["local_paths"], "shown")
        self.assertIn("/tmp/lane-checkout", serialized)
        self.assertIn("/tmp/codex-lane", serialized)

    def test_status_payload_includes_empty_agent_adapters_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = board.status_payload(
                board.BoardConfig(repo="owner/repo", repo_path=tmp),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )

        self.assertEqual(payload["agent_adapters"]["schema"], board.BOARD_AGENT_ADAPTERS_SCHEMA)
        self.assertTrue(payload["agent_adapters"]["available"])
        self.assertFalse(payload["agent_adapters"]["path_exists"])
        self.assertEqual(payload["agent_adapters"]["agents"], [])
        self.assertEqual(payload["agent_adapters"]["message"], "no local agent adapter files found")

    def test_agent_adapters_payload_loads_cards_and_redacts_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter_dir = Path(tmp) / ".code-mower" / "board" / "agents"
            adapter_dir.mkdir(parents=True)
            (adapter_dir / "codex.json").write_text(
                json.dumps(
                    {
                        "provider": "codex",
                        "role": "builder",
                        "status": "running",
                        "lane": "builder:codex",
                        "repo": "owner/repo",
                        "pr_number": 7,
                        "issue_number": 521,
                        "pid": 123,
                        "cwd": "/tmp/private/checkout",
                        "head_sha": "abcdef0123456789",
                        "url": "https://github.com/owner/repo/pull/7",
                        "title": "Implement Board cards",
                        "next_action": "waiting for peer audit",
                    }
                ),
                encoding="utf-8",
            )

            payload = board.agent_adapters_payload(board.BoardConfig(repo="owner/repo", repo_path=tmp))

        serialized = json.dumps(payload)
        self.assertEqual(payload["schema"], board.BOARD_AGENT_ADAPTERS_SCHEMA)
        self.assertTrue(payload["path_exists"])
        self.assertEqual(payload["warnings"], [])
        self.assertEqual(payload["agents"][0]["source_file"], "codex.json")
        self.assertEqual(payload["agents"][0]["provider"], "codex")
        self.assertEqual(payload["agents"][0]["role"], "builder")
        self.assertEqual(payload["agents"][0]["status"], "running")
        self.assertEqual(payload["agents"][0]["pr_number"], 7)
        self.assertEqual(payload["agents"][0]["head_sha_prefix"], "abcdef012345")
        self.assertEqual(payload["agents"][0]["cwd"], lane_status.LOCAL_PATH_REDACTION)
        self.assertNotIn("/tmp/private/checkout", serialized)

    def test_agent_adapters_payload_handles_malformed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter_dir = Path(tmp) / ".code-mower" / "board" / "agents"
            adapter_dir.mkdir(parents=True)
            (adapter_dir / "bad.json").write_text("{not json", encoding="utf-8")
            (adapter_dir / "binary.json").write_bytes(b'{"provider":"codex", "title":"bad \\xff"}')
            (adapter_dir / "empty.json").write_text("[]", encoding="utf-8")

            payload = board.agent_adapters_payload(board.BoardConfig(repo="owner/repo", repo_path=tmp))

        serialized = json.dumps(payload)
        self.assertEqual(payload["agents"], [])
        self.assertEqual(
            payload["warnings"],
            [
                {"file": "bad.json", "message": "could not parse agent adapter file"},
                {"file": "binary.json", "message": "could not parse agent adapter file"},
                {"file": "empty.json", "message": "agent adapter file had no cards"},
            ],
        )
        self.assertNotIn(str(Path(tmp)), serialized)

    def test_agent_adapters_payload_omits_secret_like_values(self) -> None:
        secret = "github_pat_abcdefghijklmnopqrstuvwxyz123456"
        with tempfile.TemporaryDirectory() as tmp:
            adapter_dir = Path(tmp) / ".code-mower" / "board" / "agents"
            adapter_dir.mkdir(parents=True)
            (adapter_dir / "claude.json").write_text(
                json.dumps(
                    {
                        "provider": "claude",
                        "role": "reviewer",
                        "status": "blocked",
                        "title": f"token {secret}",
                        "next_action": "fix audit finding",
                        "url": f"https://example.test/run?token={secret}",
                        "head_sha": secret,
                        "stdout": "raw output must not appear",
                        "token": secret,
                    }
                ),
                encoding="utf-8",
            )

            payload = board.agent_adapters_payload(board.BoardConfig(repo="owner/repo", repo_path=tmp))

        serialized = json.dumps(payload)
        self.assertEqual(payload["agents"][0]["title"], "[redacted]")
        self.assertNotIn("url", payload["agents"][0])
        self.assertNotIn("head_sha_prefix", payload["agents"][0])
        self.assertNotIn(secret, serialized)
        self.assertNotIn("raw output must not appear", serialized)

    def test_record_status_appends_redacted_local_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"

            result = board.record_status(
                board.BoardConfig(repo="owner/repo", store_path=str(store_path)),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )
            report = board_store.event_report(path=store_path, limit=5)

        serialized = json.dumps(report)
        ack = board.record_result_payload(result)
        self.assertEqual(ack["schema"], board_store.BOARD_RECORD_SCHEMA)
        self.assertEqual(result.event["schema"], board_store.BOARD_EVENT_SCHEMA)
        self.assertEqual(result.event["snapshot_schema"], lane_status.LANE_STATUS_SCHEMA)
        self.assertEqual(result.event["board_schema"], "code_mower.board.v1")
        self.assertNotIn("productivity", result.event["snapshot"])
        self.assertEqual(report["schema"], board_store.BOARD_EVENT_STORE_SCHEMA)
        self.assertEqual(report["event_count"], 1)
        self.assertEqual(report["events"][0]["summary"]["open_prs"], 1)
        self.assertIn(lane_status.LOCAL_PATH_REDACTION, serialized)
        self.assertNotIn("/tmp/lane-checkout", serialized)
        self.assertNotIn("/tmp/codex-lane", serialized)

    def test_store_retention_prunes_old_events_and_skips_malformed_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            store_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "schema": board_store.BOARD_EVENT_SCHEMA,
                                "created_at": "2026-08-01T12:00:00Z",
                                "summary": {"next_action": "old"},
                            }
                        ),
                        "not json",
                        json.dumps(
                            {
                                "schema": board_store.BOARD_EVENT_SCHEMA,
                                "created_at": "not-a-time",
                                "summary": {"next_action": "bad time"},
                            }
                        ),
                        json.dumps(
                            {
                                "schema": board_store.BOARD_EVENT_SCHEMA,
                                "created_at": "2026-09-01T11:59:00Z",
                                "summary": {"next_action": "recent"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = board_store.append_snapshot(
                {"schema": lane_status.LANE_STATUS_SCHEMA, "repo": "owner/repo"},
                path=store_path,
                now=NOW,
                retention_days=1,
                max_events=2,
            )
            report = board_store.event_report(path=store_path, limit=10)
            empty_report = board_store.event_report(path=store_path, limit=0)

        self.assertEqual(result.malformed, 1)
        self.assertEqual(result.pruned, 2)
        self.assertEqual(result.kept, 2)
        self.assertEqual(report["malformed"], 0)
        self.assertEqual([event["created_at"] for event in report["events"]], ["2026-09-01T11:59:00Z", "2026-09-01T12:00:00Z"])
        self.assertEqual(empty_report["events"], [])

    def test_append_snapshot_preserves_store_when_existing_read_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            original = json.dumps(
                {
                    "schema": board_store.BOARD_EVENT_SCHEMA,
                    "created_at": "2026-09-01T11:59:00Z",
                }
            ) + "\n"
            store_path.write_text(original, encoding="utf-8")

            with patch("code_mower.board_store._read_valid_events", side_effect=OSError("boom")):
                with self.assertRaises(board_store.BoardStoreError):
                    board_store.append_snapshot(
                        {"schema": lane_status.LANE_STATUS_SCHEMA, "repo": "owner/repo"},
                        path=store_path,
                        now=NOW,
                    )

            self.assertEqual(store_path.read_text(encoding="utf-8"), original)

    def test_event_report_degrades_when_existing_read_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            store_path.write_text("", encoding="utf-8")

            with patch("code_mower.board_store._read_valid_events", side_effect=OSError("boom")):
                report = board_store.event_report(path=store_path, limit=10)

        self.assertFalse(report["available"])
        self.assertEqual(report["events"], [])
        self.assertIn("could not read local board event store", report["message"])

    def test_record_command_rejects_invalid_retention_before_collecting_status(self) -> None:
        err = StringIO()

        with redirect_stderr(err):
            code = board.main(["record", "--repo", "owner/repo", "--retention-days", "-1"])

        self.assertEqual(code, 2)
        self.assertIn("--retention-days", err.getvalue())

    def test_doctor_payload_reports_local_only_state_without_paths(self) -> None:
        def unavailable_gh(_args: list[str]) -> object:
            raise lane_status.LaneStatusUnavailable("offline /tmp/private/repo")

        with tempfile.TemporaryDirectory() as tmp:
            payload = board.doctor_payload(
                board.BoardConfig(repo="owner/repo", repo_path=tmp),
                gh_json_runner=unavailable_gh,
                command_runner=_command_runner,
            )

        serialized = json.dumps(payload)
        self.assertEqual(payload["schema"], board.BOARD_DOCTOR_SCHEMA)
        self.assertEqual(payload["status"], "warn")
        self.assertEqual(payload["summary"]["next_action"], "remote unavailable; inspect local lanes")
        self.assertIn("github.remote", {check["id"] for check in payload["checks"]})
        self.assertIn(lane_status.LOCAL_PATH_REDACTION, serialized)
        self.assertNotIn(str(Path(tmp)), serialized)
        self.assertNotIn("/tmp/private/repo", serialized)

    def test_doctor_payload_detects_malformed_local_board_inputs_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            spend_path = Path(tmp) / "reviewer-spend.json"
            adapter_dir = Path(tmp) / "agents"
            store_path.write_text("{bad json\n", encoding="utf-8")
            spend_path.write_text("{bad json", encoding="utf-8")
            adapter_dir.mkdir()
            (adapter_dir / "bad.json").write_text("{bad json", encoding="utf-8")

            payload = board.doctor_payload(
                board.BoardConfig(
                    repo="owner/repo",
                    repo_path=tmp,
                    store_path=str(store_path),
                    spend_path=str(spend_path),
                    agent_adapters_path=str(adapter_dir),
                ),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )

        checks = {check["id"]: check for check in payload["checks"]}
        serialized = json.dumps(payload)
        self.assertEqual(payload["status"], "warn")
        self.assertEqual(checks["store.events"]["status"], "warn")
        self.assertEqual(checks["agent.adapters"]["status"], "warn")
        self.assertEqual(checks["spend.timeline"]["status"], "warn")
        self.assertNotIn(str(Path(tmp)), serialized)

    def test_reset_command_requires_explicit_yes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            store_path.write_text("keep me\n", encoding="utf-8")
            err = StringIO()

            with redirect_stderr(err):
                code = board.main(["reset", "--repo", "owner/repo", "--store-path", str(store_path)])

            self.assertEqual(code, 2)
            self.assertTrue(store_path.exists())
            self.assertIn("--yes", err.getvalue())

    def test_reset_command_deletes_only_local_history_and_redacts_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            adapter_path = Path(tmp) / "agents" / "codex.json"
            adapter_path.parent.mkdir()
            store_path.write_text("delete me\n", encoding="utf-8")
            adapter_path.write_text("keep me\n", encoding="utf-8")
            out = StringIO()

            with redirect_stdout(out):
                code = board.main(
                    [
                        "reset",
                        "--repo",
                        "owner/repo",
                        "--store-path",
                        str(store_path),
                        "--yes",
                        "--json",
                    ]
                )

            payload = json.loads(out.getvalue())
            store_exists = store_path.exists()
            adapter_exists = adapter_path.exists()
            serialized = json.dumps(payload)

        self.assertEqual(code, 0)
        self.assertEqual(payload["schema"], board_store.BOARD_RESET_SCHEMA)
        self.assertTrue(payload["deleted"])
        self.assertEqual(payload["store_path"], lane_status.LOCAL_PATH_REDACTION)
        self.assertFalse(store_exists)
        self.assertTrue(adapter_exists)
        self.assertNotIn(str(Path(tmp)), serialized)

    def test_status_payload_marks_live_recording_disabled_by_default(self) -> None:
        payload = board.status_payload(
            board.BoardConfig(repo="owner/repo"),
            gh_json_runner=_gh_json,
            command_runner=_command_runner,
        )

        self.assertEqual(
            payload["board"]["recording"],
            {"enabled": False, "interval_seconds": 60},
        )

    def test_status_payload_includes_empty_owner_queue_for_clean_pr(self) -> None:
        payload = board.status_payload(
            board.BoardConfig(repo="owner/repo"),
            gh_json_runner=_gh_json,
            command_runner=_command_runner,
        )

        self.assertEqual(payload["owner_queue"]["schema"], board.BOARD_OWNER_QUEUE_SCHEMA)
        self.assertTrue(payload["owner_queue"]["available"])
        self.assertEqual(payload["owner_queue"]["entries"], [])
        self.assertEqual(payload["owner_queue"]["message"], "no owner queue items")

    def test_status_payload_includes_supervised_controller_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_board_config(Path(tmp) / "code-mower.yml")

            def gh_json(args: list[str]) -> object:
                if args[:2] == ["issue", "list"]:
                    return [
                        {
                            "number": 12,
                            "url": "https://github.com/owner/repo/issues/12",
                            "author": {"login": "owner"},
                            "labels": [{"name": "tier:R"}, {"name": "builder:cursor"}],
                            "assignees": [],
                            "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        }
                    ]
                return _gh_json(args)

            payload = board.status_payload(
                board.BoardConfig(repo="owner/repo", repo_path=tmp),
                gh_json_runner=gh_json,
                command_runner=_command_runner,
            )

        supervised = payload["supervised_pilot"]
        self.assertEqual(supervised["schema"], board.controller.SUPERVISED_PILOT_SCHEMA)
        self.assertTrue(supervised["enabled"])
        self.assertEqual(supervised["cycle_state"], "ready")
        self.assertEqual(supervised["decision"]["decision_state"], "ready_to_merge")
        self.assertEqual(supervised["decision"]["pr_number"], 7)
        self.assertEqual(supervised["queue"]["metrics"]["ready_issue_count"], 1)
        self.assertEqual(supervised["active_issues"][0]["number"], 12)
        self.assertNotIn(str(Path(tmp)), json.dumps(supervised))

    def test_supervised_pilot_disabled_without_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = board.status_payload(
                board.BoardConfig(repo="owner/repo", repo_path=tmp),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )

        supervised = payload["supervised_pilot"]
        self.assertFalse(supervised["enabled"])
        self.assertEqual(supervised["cycle_state"], "unavailable")
        self.assertIn("code-mower.yml not found", supervised["message"])

    def test_owner_queue_payload_detects_attention_states(self) -> None:
        payload = board.owner_queue_payload(
            {
                "remote": {
                    "available": True,
                    "pull_requests": [
                        {
                            "number": 1,
                            "title": "Owner decision",
                            "url": "https://github.com/owner/repo/pull/1",
                            "branch": "codex/one",
                            "author": "codex",
                            "updated_at": "2026-09-01T12:00:00Z",
                            "head_sha": "1111111111111111",
                            "labels": {"needs": ["needs-owner"], "blocked": []},
                            "checks": [],
                            "next_action": "waiting for audits or owner input",
                        },
                        {
                            "number": 2,
                            "title": "Blocked",
                            "url": "https://github.com/owner/repo/pull/2",
                            "branch": "codex/two",
                            "author": "codex",
                            "updated_at": "2026-09-01T12:00:00Z",
                            "head_sha": "2222222222222222",
                            "labels": {"needs": [], "blocked": ["claude-audit-blocked"]},
                            "checks": [],
                            "next_action": "fix BLOCKED audit",
                        },
                        {
                            "number": 3,
                            "title": "Stale",
                            "url": "https://github.com/owner/repo/pull/3",
                            "branch": "codex/three",
                            "author": "codex",
                            "updated_at": "2026-09-01T12:00:00Z",
                            "head_sha": "3333333333333333",
                            "labels": {"needs": [], "blocked": []},
                            "checks": [{"name": "code-mower/gate", "state": "success"}],
                            "stale": True,
                            "next_action": "waiting for checks",
                        },
                        {
                            "number": 4,
                            "title": "Failing",
                            "url": "https://github.com/owner/repo/pull/4",
                            "branch": "codex/four",
                            "author": "codex",
                            "updated_at": "2026-09-01T12:00:00Z",
                            "head_sha": "4444444444444444",
                            "labels": {"needs": [], "blocked": []},
                            "checks": [{"name": "package", "state": "failure"}],
                            "next_action": "fix failing check",
                        },
                        {
                            "number": 5,
                            "title": "Behind",
                            "url": "https://github.com/owner/repo/pull/5",
                            "branch": "codex/five",
                            "author": "codex",
                            "updated_at": "2026-09-01T12:00:00Z",
                            "head_sha": "5555555555555555",
                            "labels": {"needs": [], "blocked": []},
                            "checks": [],
                            "merge_state": "BEHIND",
                            "next_action": "rebase/behind",
                        },
                        {
                            "number": 6,
                            "title": "Draft",
                            "url": "file:///tmp/secret",
                            "branch": "codex/six",
                            "author": "codex",
                            "updated_at": "2026-09-01T12:00:00Z",
                            "head_sha": "6666666666666666",
                            "labels": {"needs": [], "blocked": []},
                            "checks": [],
                            "is_draft": True,
                            "next_action": "finish draft PR",
                        },
                    ],
                }
            }
        )

        kinds = {entry["kind"] for entry in payload["entries"]}
        self.assertEqual(
            kinds,
            {"needs-owner", "blocked-audit", "stale-gate", "failing-check", "rebase-needed", "draft"},
        )
        self.assertEqual(payload["count"], 6)
        self.assertEqual(payload["entries"][0]["priority"], 0)
        self.assertEqual(payload["entries"][-1]["kind"], "draft")
        self.assertNotIn("/tmp/secret", json.dumps(payload))

    def test_owner_queue_payload_reports_github_unavailable(self) -> None:
        payload = board.owner_queue_payload({"remote": {"available": False}})

        self.assertFalse(payload["available"])
        self.assertEqual(payload["entries"], [])
        self.assertIn("GitHub unavailable", payload["message"])

    def test_http_status_does_not_write_events_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            handler = board.make_handler(
                board.BoardConfig(repo="owner/repo", store_path=str(store_path)),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )
            server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(f"{base_url}/api/status", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertFalse(store_path.exists())
        self.assertFalse(payload["board"]["recording"]["enabled"])

    def test_http_status_records_events_when_explicitly_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            handler = board.make_handler(
                board.BoardConfig(
                    repo="owner/repo",
                    store_path=str(store_path),
                    record_events=True,
                    record_interval_seconds=0,
                ),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )
            server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                payload = _await_warm_status(base_url)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            report = board_store.event_report(path=store_path, limit=10)

        self.assertEqual(report["event_count"], 1)
        self.assertEqual(payload["board"]["recording"]["status"], "recorded")
        self.assertEqual(payload["board"]["recording"]["kept"], 1)

    def test_http_status_throttles_live_recording_by_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            handler = board.make_handler(
                board.BoardConfig(
                    repo="owner/repo",
                    store_path=str(store_path),
                    record_events=True,
                    record_interval_seconds=3600,
                ),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )
            server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                first = _await_warm_status(base_url)
                second = _fetch_status(base_url)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            report = board_store.event_report(path=store_path, limit=10)

        self.assertEqual(report["event_count"], 1)
        self.assertEqual(first["board"]["recording"]["status"], "recorded")
        self.assertEqual(second["board"]["recording"]["status"], "skipped")

    def test_http_status_live_record_error_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            handler = board.make_handler(
                board.BoardConfig(repo="owner/repo", store_path=str(store_path), record_events=True),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )
            server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with patch(
                    "code_mower.board._record_live_snapshot",
                    side_effect=board_store.BoardStoreError("secret /tmp/private/path"),
                ):
                    payload = _await_warm_status(base_url)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        serialized = json.dumps(payload)
        self.assertEqual(payload["board"]["recording"]["status"], "error")
        self.assertIn("could not update local board event store", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("/tmp/private/path", serialized)

    def test_http_status_does_not_rerecord_a_stale_snapshot_but_records_the_next_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            release = threading.Event()
            calls: list[int] = []

            def fake_status_payload(*_args: object, **_kwargs: object) -> dict:
                calls.append(1)
                if len(calls) == 2:
                    self.assertTrue(release.wait(timeout=5))
                return {
                    "schema": lane_status.LANE_STATUS_SCHEMA,
                    "repo": "owner/repo",
                    "n": len(calls),
                    "board": {"schema": "code_mower.board.v1", "mode": "local_recording"},
                }

            handler = board.make_handler(
                board.BoardConfig(
                    repo="owner/repo",
                    store_path=str(store_path),
                    record_events=True,
                    record_interval_seconds=0,
                    refresh_seconds=1,
                ),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )
            server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with patch("code_mower.board.status_payload", side_effect=fake_status_payload):
                    first = _await_warm_status(base_url)
                    self.assertEqual(first["board"]["cache"]["state"], "fresh")
                    self.assertEqual(first["board"]["recording"]["status"], "recorded")

                    time.sleep(1.1)  # let the 1-second TTL expire so the cached snapshot goes stale

                    stale = _fetch_status(base_url)
                    self.assertEqual(stale["board"]["cache"]["state"], "stale")
                    self.assertTrue(stale["board"]["cache"]["refresh_in_progress"])
                    self.assertEqual(stale["board"]["cache"]["generation"], 1)
                    # Same generation as the first response: aging out does not make it
                    # a new snapshot, so it must not be written to the store twice.
                    self.assertEqual(stale["board"]["recording"]["status"], "skipped")
                    self.assertEqual(stale["board"]["recording"]["message"], "snapshot already recorded")

                    mid_report = board_store.event_report(path=store_path, limit=10)
                    self.assertEqual(mid_report["event_count"], 1)  # generation 1 stays recorded once

                    release.set()
                    deadline = time.monotonic() + 5
                    fresh = stale
                    while fresh["board"]["cache"]["state"] != "fresh" and time.monotonic() < deadline:
                        time.sleep(0.02)
                        fresh = _fetch_status(base_url)
                    self.assertEqual(fresh["board"]["cache"]["state"], "fresh")
                    self.assertEqual(fresh["board"]["cache"]["generation"], 2)
                    self.assertEqual(fresh["board"]["recording"]["status"], "recorded")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            report = board_store.event_report(path=store_path, limit=10)

        self.assertEqual(report["event_count"], 2)

    def test_http_status_records_a_generation_first_observed_after_it_went_stale(self) -> None:
        """A browser polling slower than the TTL still gets every completed snapshot recorded.

        The background refresh completes while nobody is asking, and the snapshot
        ages out before the next request. Freshness therefore cannot be the
        recording identity -- the cache generation is.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            release_second = threading.Event()
            calls: list[int] = []
            calls_lock = threading.Lock()

            def fake_status_payload(*_args: object, **_kwargs: object) -> dict:
                with calls_lock:
                    calls.append(1)
                    index = len(calls)
                if index >= 2:
                    self.assertTrue(release_second.wait(timeout=5))
                return {
                    "schema": lane_status.LANE_STATUS_SCHEMA,
                    "repo": "owner/repo",
                    "n": index,
                    "board": {"schema": "code_mower.board.v1", "mode": "local_recording"},
                }

            handler = board.make_handler(
                board.BoardConfig(
                    repo="owner/repo",
                    store_path=str(store_path),
                    record_events=True,
                    record_interval_seconds=0,
                    refresh_seconds=1,
                ),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )
            server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with patch("code_mower.board.status_payload", side_effect=fake_status_payload):
                    cold = _fetch_status(base_url)
                    self.assertEqual(cold["board"]["cache"]["state"], "cold")
                    self.assertEqual(cold["board"]["cache"]["generation"], 0)
                    self.assertEqual(cold["board"]["recording"]["status"], "pending")

                    # No request at all until past the 1-second TTL, so generation 1 is
                    # never observed while it is still fresh.
                    time.sleep(1.2)

                    stale = _fetch_status(base_url)
                    self.assertEqual(stale["board"]["cache"]["state"], "stale")
                    self.assertEqual(stale["board"]["cache"]["generation"], 1)
                    self.assertEqual(stale["board"]["recording"]["status"], "recorded")
                    self.assertEqual(board_store.event_report(path=store_path, limit=10)["event_count"], 1)

                    # Repeated stale polls, with refresh 2 still in flight, keep reporting
                    # the same generation and must never write it a second time.
                    for _ in range(3):
                        repeat = _fetch_status(base_url)
                        self.assertEqual(repeat["board"]["cache"]["generation"], 1)
                        self.assertTrue(repeat["board"]["cache"]["refresh_in_progress"])
                        self.assertEqual(repeat["board"]["recording"]["status"], "skipped")
                        self.assertEqual(repeat["board"]["recording"]["message"], "snapshot already recorded")
                    self.assertEqual(board_store.event_report(path=store_path, limit=10)["event_count"], 1)

                    release_second.set()
                    second = _await_cache_generation(base_url, 2)
                    self.assertEqual(second["board"]["recording"]["status"], "recorded")
                    report = board_store.event_report(path=store_path, limit=10)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual(report["event_count"], 2)

    def test_http_status_records_a_generation_once_under_concurrent_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            computed = threading.Event()
            calls: list[int] = []
            calls_lock = threading.Lock()
            statuses: list[str] = []
            statuses_lock = threading.Lock()

            def fake_status_payload(*_args: object, **_kwargs: object) -> dict:
                with calls_lock:
                    calls.append(1)
                    index = len(calls)
                payload = {
                    "schema": lane_status.LANE_STATUS_SCHEMA,
                    "repo": "owner/repo",
                    "n": index,
                    "board": {"schema": "code_mower.board.v1", "mode": "local_recording"},
                }
                computed.set()
                return payload

            handler = board.make_handler(
                board.BoardConfig(
                    repo="owner/repo",
                    store_path=str(store_path),
                    record_events=True,
                    record_interval_seconds=0,
                    refresh_seconds=3600,  # exactly one completed generation for the whole test
                ),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )
            server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with patch("code_mower.board.status_payload", side_effect=fake_status_payload):
                    cold = _fetch_status(base_url)
                    self.assertEqual(cold["board"]["recording"]["status"], "pending")
                    self.assertTrue(computed.wait(timeout=5))
                    time.sleep(0.2)  # let the background refresh publish generation 1

                    barrier = threading.Barrier(8)

                    def worker() -> None:
                        barrier.wait(timeout=5)
                        payload = _fetch_status(base_url)
                        with statuses_lock:
                            statuses.append(payload["board"]["recording"]["status"])

                    workers = [threading.Thread(target=worker) for _ in range(8)]
                    for worker_thread in workers:
                        worker_thread.start()
                    for worker_thread in workers:
                        worker_thread.join(timeout=10)
                    report = board_store.event_report(path=store_path, limit=10)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        # All eight requests race on the same generation; the recording lock lets
        # exactly one of them win, and none of the others rewrites it.
        self.assertEqual(len(statuses), 8)
        self.assertEqual(statuses.count("recorded"), 1)
        self.assertEqual(statuses.count("skipped"), 7)
        self.assertEqual(report["event_count"], 1)

    def test_http_status_interval_throttle_keeps_a_new_generation_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            calls: list[int] = []
            calls_lock = threading.Lock()
            recording_now = [datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)]

            def fake_status_payload(*_args: object, **_kwargs: object) -> dict:
                with calls_lock:
                    calls.append(1)
                    index = len(calls)
                return {
                    "schema": lane_status.LANE_STATUS_SCHEMA,
                    "repo": "owner/repo",
                    "n": index,
                    "board": {"schema": "code_mower.board.v1", "mode": "local_recording"},
                }

            handler = board.make_handler(
                board.BoardConfig(
                    repo="owner/repo",
                    store_path=str(store_path),
                    record_events=True,
                    record_interval_seconds=60,
                    refresh_seconds=1,
                ),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )
            server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with (
                    patch("code_mower.board.status_payload", side_effect=fake_status_payload),
                    patch("code_mower.board._utc_now", side_effect=lambda: recording_now[0]),
                ):
                    first = _await_cache_generation(base_url, 1)
                    self.assertEqual(first["board"]["recording"]["status"], "recorded")

                    # Generation 2 completes well inside the record interval, so it is
                    # skipped for the interval -- not consumed.
                    second = _await_cache_generation(base_url, 2, timeout=8)
                    self.assertEqual(second["board"]["recording"]["status"], "skipped")
                    self.assertEqual(second["board"]["recording"]["message"], "record interval not reached")
                    self.assertEqual(board_store.event_report(path=store_path, limit=10)["event_count"], 1)

                    recording_now[0] = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)  # interval elapses
                    due = _fetch_status(base_url)
                    self.assertGreaterEqual(due["board"]["cache"]["generation"], 2)
                    self.assertEqual(due["board"]["recording"]["status"], "recorded")
                    report = board_store.event_report(path=store_path, limit=10)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual(report["event_count"], 2)

    def test_serve_rejects_invalid_recording_options_before_binding_port(self) -> None:
        err = StringIO()

        with redirect_stderr(err):
            code = board.main(["serve", "--repo", "owner/repo", "--record-events", "--max-events", "0"])

        self.assertEqual(code, 2)
        self.assertIn("--max-events", err.getvalue())

    def test_timelines_payload_summarizes_verdicts_and_spend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            spend_path = Path(tmp) / "reviewer-spend.json"
            board_store.append_snapshot(
                {
                    "schema": lane_status.LANE_STATUS_SCHEMA,
                    "repo": "owner/repo",
                    "remote": {
                        "available": True,
                        "pull_requests": [
                            {
                                "number": 7,
                                "url": "https://github.com/owner/repo/pull/7",
                                "head_sha": "abcdef0123456789",
                                "labels": {
                                    "done": ["claude-audit-done"],
                                    "blocked": ["codex-audit-blocked"],
                                },
                            }
                        ],
                    },
                    "board": {"schema": "code_mower.board.v1"},
                },
                path=store_path,
                now=NOW,
            )
            spend_path.write_text(
                json.dumps(
                    {
                        "schema": reviewer_spend.SPEND_SCHEMA,
                        "runs": [
                            {
                                "created_at": "2026-09-01T12:01:00+00:00",
                                "lane": "claude-audit",
                                "repo": "owner/repo",
                                "pr_number": 7,
                                "head_sha": "abcdef0123456789",
                                "model": "sonnet",
                                "wall_seconds": 12.5,
                                "cost_usd": 0.125,
                                "total_tokens": 1000,
                                "verdict": "PASS",
                            },
                            {"repo": "owner/repo"},
                            "not a row",
                            {"lane": "claude-audit", "repo": "other/repo", "pr_number": 1, "verdict": "PASS"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            payload = board.timelines_payload(
                board.BoardConfig(repo="owner/repo", store_path=str(store_path), spend_path=str(spend_path)),
                limit=10,
            )

        self.assertEqual(payload["schema"], board.BOARD_TIMELINES_SCHEMA)
        self.assertEqual(
            [(entry["lane"], entry["verdict"], entry["head_sha_prefix"]) for entry in payload["verdicts"]["entries"]],
            [("claude-audit", "PASS", "abcdef012345"), ("codex-audit", "BLOCKED", "abcdef012345")],
        )
        self.assertEqual(payload["spend"]["skipped_rows"], 2)
        self.assertEqual(payload["spend"]["filtered_rows"], 1)
        self.assertEqual(payload["spend"]["groups"][0]["lane"], "claude-audit")
        self.assertEqual(payload["spend"]["groups"][0]["runs"], 1)
        self.assertEqual(payload["spend"]["groups"][0]["wall_seconds_total"], 12.5)
        self.assertEqual(payload["spend"]["groups"][0]["cost_usd_total"], 0.125)
        self.assertEqual(payload["spend"]["groups"][0]["total_tokens"], 1000)
        self.assertEqual(payload["spend"]["recent_runs"][0]["head_sha_prefix"], "abcdef012345")
        self.assertNotIn(str(Path(tmp)), json.dumps(payload))

    def test_timelines_payload_handles_missing_and_malformed_spend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = board.timelines_payload(
                board.BoardConfig(repo="owner/repo", repo_path=tmp, store_path=str(Path(tmp) / "events.jsonl")),
                limit=10,
            )
            spend_path = Path(tmp) / "reviewer-spend.json"
            spend_path.write_text("{not json", encoding="utf-8")
            malformed = board.timelines_payload(
                board.BoardConfig(repo="owner/repo", store_path=str(Path(tmp) / "events.jsonl"), spend_path=str(spend_path)),
                limit=10,
            )

        self.assertFalse(missing["spend"]["available"])
        self.assertIn("no reviewer spend file yet", missing["spend"]["message"])
        self.assertFalse(malformed["spend"]["available"])
        self.assertEqual(malformed["spend"]["message"], "could not read reviewer spend file")
        self.assertNotIn(str(Path(tmp)), json.dumps(malformed))

    def test_http_status_includes_local_timelines_when_github_is_unavailable(self) -> None:
        def unavailable_gh(_args: list[str]) -> object:
            raise lane_status.LaneStatusUnavailable("offline")

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            spend_path = Path(tmp) / "reviewer-spend.json"
            board_store.append_snapshot(
                {
                    "schema": lane_status.LANE_STATUS_SCHEMA,
                    "repo": "owner/repo",
                    "remote": {
                        "available": True,
                        "pull_requests": [
                            {
                                "number": 9,
                                "url": "https://github.com/owner/repo/pull/9",
                                "head_sha": "9999999999999999",
                                "labels": {"done": ["gitar-audit-done"]},
                            }
                        ],
                    },
                    "board": {"schema": "code_mower.board.v1"},
                },
                path=store_path,
                now=NOW,
            )
            spend_path.write_text(
                json.dumps(
                    {
                        "schema": reviewer_spend.SPEND_SCHEMA,
                        "runs": [
                            {
                                "created_at": "2026-09-01T12:02:00+00:00",
                                "lane": "gitar-audit",
                                "repo": "owner/repo",
                                "pr_number": 9,
                                "head_sha": "9999999999999999",
                                "wall_seconds": 1.0,
                                "verdict": "PASS",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            handler = board.make_handler(
                board.BoardConfig(repo="owner/repo", store_path=str(store_path), spend_path=str(spend_path)),
                gh_json_runner=unavailable_gh,
                command_runner=_command_runner,
            )
            server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(f"{base_url}/api/status", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertFalse(payload["remote"]["available"])
        self.assertFalse(payload["owner_queue"]["available"])
        self.assertEqual(payload["timelines"]["verdicts"]["entries"][0]["lane"], "gitar-audit")
        self.assertEqual(payload["timelines"]["spend"]["groups"][0]["lane"], "gitar-audit")

    def test_http_handler_serves_page_status_and_health(self) -> None:
        handler = board.make_handler(
            board.BoardConfig(repo="owner/repo"),
            gh_json_runner=_gh_json,
            command_runner=_command_runner,
        )
        server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            with urllib.request.urlopen(f"{base_url}/", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("text/html", response.headers["Content-Type"])
            cold_payload = _fetch_status(base_url)
            self.assertEqual(cold_payload["repo"], "owner/repo")
            self.assertEqual(cold_payload["board"]["cache"]["state"], "cold")
            self.assertEqual(cold_payload["generated_at"], "")
            payload = _await_warm_status(base_url)
            self.assertEqual(payload["repo"], "owner/repo")
            self.assertEqual(payload["remote"]["pull_requests"][0]["number"], 7)
            self.assertEqual(payload["board"]["cache"]["state"], "fresh")
            self.assertNotIn("/tmp/lane-checkout", json.dumps(payload))
            with urllib.request.urlopen(f"{base_url}/api/identity", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["schema"], board.BOARD_IDENTITY_SCHEMA)
                self.assertEqual(payload["repo"], "owner/repo")
                self.assertEqual(payload["board"]["version"]["serving_version"], board.CODE_MOWER_VERSION)
                self.assertNotIn("remote", payload)
            with urllib.request.urlopen(f"{base_url}/api/events", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["schema"], board_store.BOARD_EVENT_STORE_SCHEMA)
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=5) as response:
                self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_handler_serves_events_without_github(self) -> None:
        def unavailable_gh(_args: list[str]) -> object:
            raise AssertionError("events endpoint should not call GitHub")

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            board_store.append_snapshot(
                {"schema": lane_status.LANE_STATUS_SCHEMA, "repo": "owner/repo"},
                path=store_path,
                now=NOW,
            )
            handler = board.make_handler(
                board.BoardConfig(repo="owner/repo", store_path=str(store_path)),
                gh_json_runner=unavailable_gh,
                command_runner=_command_runner,
            )
            server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(f"{base_url}/api/events", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(response.status, 200)
                    self.assertEqual(payload["event_count"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_http_handler_rejects_non_loopback_host_and_origin(self) -> None:
        handler = board.make_handler(
            board.BoardConfig(repo="owner/repo"),
            gh_json_runner=_gh_json,
            command_runner=_command_runner,
        )
        server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = int(server.server_address[1])
            self.assertEqual(
                self._http_status(port, "/api/status", {"Host": f"127.0.0.1:{port}"}),
                200,
            )
            self.assertEqual(
                self._http_status(port, "/api/status", {"Host": "evil.example"}),
                403,
            )
            self.assertEqual(
                self._http_status(
                    port,
                    "/api/status",
                    {"Host": f"127.0.0.1:{port}", "Origin": "https://evil.example"},
                ),
                403,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_non_loopback_hosts_are_rejected(self) -> None:
        self.assertTrue(board._is_loopback("127.0.0.1"))
        self.assertTrue(board._is_loopback("localhost"))
        self.assertTrue(board._is_loopback("::1"))
        self.assertFalse(board._is_loopback("0.0.0.0"))

    def test_ipv6_loopback_uses_ipv6_server_and_url(self) -> None:
        self.assertEqual(board._server_class("::1").address_family, socket.AF_INET6)
        self.assertEqual(board._server_url("::1", 5332), "http://[::1]:5332/")

    def _http_status(self, port: int, path: str, headers: dict[str, str]) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            response.read()
            return int(response.status)
        finally:
            connection.close()


class StatusCacheTests(TestCase):
    """Deterministic coverage for the /api/status stale-while-refresh cache, with no live network calls."""

    def test_cold_get_returns_placeholder_and_starts_exactly_one_refresh(self) -> None:
        pending: list = []
        calls: list[int] = []

        def compute() -> dict:
            calls.append(1)
            return {"n": len(calls)}

        cache = board.StatusCache(compute, ttl_seconds=10, clock=lambda: 0.0, now=lambda: NOW, start_thread=pending.append)

        snapshot, meta = cache.get()

        self.assertIsNone(snapshot)
        self.assertEqual(meta["state"], "cold")
        self.assertTrue(meta["refresh_in_progress"])
        self.assertEqual(meta["generated_at"], "")
        self.assertIsNone(meta["age_seconds"])
        self.assertEqual(len(pending), 1)
        self.assertEqual(calls, [])

        # A concurrent cold read must observe the in-flight refresh, not start a second one.
        snapshot_again, meta_again = cache.get()
        self.assertIsNone(snapshot_again)
        self.assertTrue(meta_again["refresh_in_progress"])
        self.assertEqual(len(pending), 1)

    def test_generation_advances_only_once_per_completed_snapshot(self) -> None:
        """The generation is recording identity, so it must track completions, not freshness."""
        pending: list = []
        attempts: list[int] = []

        def compute() -> dict:
            attempts.append(1)
            if len(attempts) == 2:
                raise RuntimeError("github unavailable")
            return {"n": len(attempts)}

        clock = _FakeClock(0.0)
        cache = board.StatusCache(
            compute,
            ttl_seconds=5,
            clock=clock,
            now=lambda: NOW,
            start_thread=pending.append,
            retry_base_seconds=5.0,
        )

        _snapshot, meta = cache.get()
        self.assertEqual(meta["generation"], 0)  # cold: nothing has completed yet
        self.assertEqual(cache.generation, 0)

        pending.pop()()  # the first refresh completes
        snapshot, meta = cache.get()
        self.assertEqual(snapshot, {"n": 1})
        self.assertEqual(meta["generation"], 1)
        self.assertEqual(cache.generation, 1)

        clock.advance(6.0)
        snapshot, meta = cache.get()  # aging out is not a new snapshot
        self.assertEqual(snapshot, {"n": 1})
        self.assertEqual(meta["state"], "stale")
        self.assertEqual(meta["generation"], 1)

        pending.pop()()  # the second refresh fails
        snapshot, meta = cache.get()
        self.assertEqual(snapshot, {"n": 1})
        self.assertEqual(meta["generation"], 1)  # a failed refresh never advances it
        self.assertEqual(meta["last_error"], "status refresh failed: RuntimeError")
        self.assertEqual(cache.generation, 1)

        clock.advance(5.0)
        cache.get()  # starts exactly one retry once the backoff window expires
        pending.pop()()  # the retry succeeds
        snapshot, meta = cache.get()
        self.assertEqual(snapshot, {"n": 3})
        self.assertEqual(meta["generation"], 2)  # one step per completed snapshot
        self.assertEqual(cache.generation, 2)

    def test_warm_snapshot_is_served_without_recompute_inside_ttl(self) -> None:
        pending: list = []
        calls: list[int] = []

        def compute() -> dict:
            calls.append(1)
            return {"n": len(calls)}

        clock = _FakeClock(100.0)
        cache = board.StatusCache(compute, ttl_seconds=10, clock=clock, now=lambda: NOW, start_thread=pending.append)

        cache.get()
        pending.pop()()  # run the queued refresh, as a background thread would

        snapshot, meta = cache.get()
        self.assertEqual(snapshot, {"n": 1})
        self.assertEqual(meta["state"], "fresh")
        self.assertFalse(meta["refresh_in_progress"])
        self.assertEqual(meta["generated_at"], board._format_timestamp(NOW))
        self.assertEqual(meta["last_error"], "")

        clock.advance(1.0)
        snapshot_again, meta_again = cache.get()
        self.assertEqual(snapshot_again, {"n": 1})
        self.assertEqual(meta_again["state"], "fresh")
        self.assertEqual(calls, [1])  # not recomputed while fresh

    def test_stale_snapshot_is_served_while_one_background_refresh_runs(self) -> None:
        pending: list = []
        calls: list[int] = []

        def compute() -> dict:
            calls.append(1)
            return {"n": len(calls)}

        clock = _FakeClock(0.0)
        cache = board.StatusCache(compute, ttl_seconds=5, clock=clock, now=lambda: NOW, start_thread=pending.append)
        cache.get()
        pending.pop()()

        clock.advance(6.0)
        snapshot, meta = cache.get()
        self.assertEqual(snapshot, {"n": 1})
        self.assertEqual(meta["state"], "stale")
        self.assertTrue(meta["refresh_in_progress"])
        self.assertEqual(len(pending), 1)

        # A second stale read while the refresh is in flight must not queue another one.
        snapshot_again, meta_again = cache.get()
        self.assertEqual(snapshot_again, {"n": 1})
        self.assertTrue(meta_again["refresh_in_progress"])
        self.assertEqual(len(pending), 1)

        pending.pop()()
        snapshot_final, meta_final = cache.get()
        self.assertEqual(snapshot_final, {"n": 2})
        self.assertEqual(meta_final["state"], "fresh")

    def test_concurrent_cold_requests_start_exactly_one_background_refresh(self) -> None:
        started = threading.Event()
        release = threading.Event()
        calls: list[int] = []

        def compute() -> dict:
            calls.append(1)
            started.set()
            self.assertTrue(release.wait(timeout=5))
            return {"n": len(calls)}

        cache = board.StatusCache(compute, ttl_seconds=10)

        results: list[tuple] = []

        def worker() -> None:
            results.append(cache.get())

        workers = [threading.Thread(target=worker) for _ in range(8)]
        for worker_thread in workers:
            worker_thread.start()
        self.assertTrue(started.wait(timeout=5))
        for worker_thread in workers:
            worker_thread.join(timeout=5)

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(snapshot is None for snapshot, _ in results))
        self.assertTrue(all(meta["refresh_in_progress"] for _, meta in results))

        release.set()
        deadline = time.monotonic() + 5
        snapshot = None
        while time.monotonic() < deadline:
            snapshot, _meta = cache.get()
            if snapshot is not None:
                break
            time.sleep(0.01)
        self.assertEqual(snapshot, {"n": 1})
        self.assertEqual(len(calls), 1)

    def test_failed_refresh_records_safe_summarized_error_and_recovers(self) -> None:
        pending: list = []
        attempts: list[int] = []
        secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
        local_path = "/" + "Users/name/private/repo"

        def compute() -> dict:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError(f"failed at {local_path} with token {secret}")
            return {"ok": True}

        clock = _FakeClock(0.0)
        cache = board.StatusCache(
            compute,
            ttl_seconds=10,
            clock=clock,
            now=lambda: NOW,
            start_thread=pending.append,
            retry_base_seconds=5.0,
        )

        snapshot, _meta = cache.get()
        self.assertIsNone(snapshot)
        pending.pop()()  # run the failing refresh

        snapshot, meta = cache.get()
        self.assertIsNone(snapshot)
        self.assertEqual(meta["state"], "cold")
        self.assertEqual(meta["last_error"], "status refresh failed: RuntimeError")
        self.assertNotIn(secret, meta["last_error"])
        self.assertNotIn(local_path, meta["last_error"])
        self.assertFalse(meta["refresh_in_progress"])  # the retry backoff is armed
        self.assertEqual(meta["retry_in_seconds"], 5.0)
        self.assertEqual(pending, [])

        clock.advance(5.0)
        snapshot, meta = cache.get()
        self.assertIsNone(snapshot)
        self.assertTrue(meta["refresh_in_progress"])  # this get() started the retry
        self.assertIsNone(meta["retry_in_seconds"])
        self.assertEqual(len(pending), 1)

        pending.pop()()  # run the retry, which succeeds
        snapshot, meta = cache.get()
        self.assertEqual(snapshot, {"ok": True})
        self.assertEqual(meta["state"], "fresh")
        self.assertEqual(meta["last_error"], "")
        self.assertIsNone(meta["retry_in_seconds"])

    def test_persistent_refresh_failure_is_not_recomputed_on_every_request(self) -> None:
        pending: list = []
        attempts: list[int] = []

        def compute() -> dict:
            attempts.append(1)
            raise RuntimeError("github unavailable")

        clock = _FakeClock(0.0)
        cache = board.StatusCache(
            compute,
            ttl_seconds=10,
            clock=clock,
            now=lambda: NOW,
            start_thread=pending.append,
            retry_base_seconds=5.0,
        )

        cache.get()
        pending.pop()()  # first refresh fails
        self.assertEqual(attempts, [1])

        # A browser fast-polling every 750ms must not license one expensive
        # recomputation per poll while the failure persists.
        for tick in range(6):
            clock.advance(0.75)
            snapshot, meta = cache.get()
            self.assertIsNone(snapshot)
            self.assertEqual(meta["state"], "cold")
            self.assertFalse(meta["refresh_in_progress"])
            self.assertEqual(meta["retry_in_seconds"], round(5.0 - 0.75 * (tick + 1), 3))
            self.assertEqual(meta["last_error"], "status refresh failed: RuntimeError")
            self.assertEqual(pending, [])
            self.assertEqual(attempts, [1])

    def test_persistent_refresh_failure_keeps_serving_the_stale_snapshot(self) -> None:
        pending: list = []
        attempts: list[int] = []

        def compute() -> dict:
            attempts.append(1)
            if len(attempts) == 1:
                return {"n": 1}
            raise RuntimeError("github unavailable")

        clock = _FakeClock(0.0)
        cache = board.StatusCache(
            compute,
            ttl_seconds=5,
            clock=clock,
            now=lambda: NOW,
            start_thread=pending.append,
            retry_base_seconds=5.0,
        )
        cache.get()
        pending.pop()()  # first refresh succeeds

        clock.advance(6.0)
        cache.get()
        pending.pop()()  # the refresh of the now-stale snapshot fails

        clock.advance(0.75)
        snapshot, meta = cache.get()
        self.assertEqual(snapshot, {"n": 1})  # the last good snapshot is still served
        self.assertEqual(meta["state"], "stale")
        self.assertFalse(meta["refresh_in_progress"])
        self.assertEqual(meta["retry_in_seconds"], 4.25)
        self.assertEqual(pending, [])
        self.assertEqual(attempts, [1, 1])

    def test_exactly_one_retry_starts_after_the_backoff_deadline(self) -> None:
        pending: list = []
        attempts: list[int] = []

        def compute() -> dict:
            attempts.append(1)
            raise RuntimeError("github unavailable")

        clock = _FakeClock(0.0)
        cache = board.StatusCache(
            compute,
            ttl_seconds=10,
            clock=clock,
            now=lambda: NOW,
            start_thread=pending.append,
            retry_base_seconds=5.0,
        )
        cache.get()
        pending.pop()()

        clock.advance(4.999)
        _snapshot, meta = cache.get()
        self.assertFalse(meta["refresh_in_progress"])  # still inside the window
        self.assertEqual(meta["retry_in_seconds"], 0.001)
        self.assertEqual(pending, [])

        clock.advance(0.001)
        _snapshot, meta = cache.get()
        self.assertTrue(meta["refresh_in_progress"])
        self.assertEqual(len(pending), 1)

        # Requests arriving while that single retry is in flight must not queue
        # another one, and the backoff must not be re-reported as pending.
        for _ in range(3):
            _snapshot, meta_again = cache.get()
            self.assertTrue(meta_again["refresh_in_progress"])
            self.assertIsNone(meta_again["retry_in_seconds"])
            self.assertEqual(len(pending), 1)

        pending.pop()()  # the retry fails too
        self.assertEqual(attempts, [1, 1])

        # The second consecutive failure doubles the window, so the next
        # request is refused for 10s rather than 5s.
        clock.advance(5.0)
        _snapshot, meta = cache.get()
        self.assertFalse(meta["refresh_in_progress"])
        self.assertEqual(meta["retry_in_seconds"], 5.0)
        self.assertEqual(pending, [])

    def test_retry_backoff_doubles_and_is_bounded_by_the_maximum(self) -> None:
        pending: list = []

        def compute() -> dict:
            raise RuntimeError("github unavailable")

        clock = _FakeClock(0.0)
        cache = board.StatusCache(
            compute,
            ttl_seconds=10,
            clock=clock,
            now=lambda: NOW,
            start_thread=pending.append,
            retry_base_seconds=5.0,
            retry_max_seconds=20.0,
        )

        observed: list[float] = []
        for _ in range(5):
            _snapshot, meta = cache.get()
            self.assertTrue(meta["refresh_in_progress"])
            pending.pop()()  # the refresh fails
            _snapshot, meta = cache.get()
            observed.append(meta["retry_in_seconds"])
            clock.advance(meta["retry_in_seconds"])

        # Deterministic doubling, clamped so a long outage never parks the
        # cache beyond retry_max_seconds.
        self.assertEqual(observed, [5.0, 10.0, 20.0, 20.0, 20.0])

    def test_retry_delay_never_overflows_for_a_huge_failure_streak(self) -> None:
        def compute() -> dict:
            raise RuntimeError("github unavailable")

        cache = board.StatusCache(
            compute,
            ttl_seconds=10,
            clock=_FakeClock(0.0),
            now=lambda: NOW,
            start_thread=lambda target: None,
            retry_base_seconds=5.0,
            retry_max_seconds=60.0,
        )

        # A Board left in persistent failure keeps incrementing the streak, so
        # the delay must stay finite and capped no matter how large it grows.
        # Computing base * 2.0 ** streak directly raises OverflowError here.
        for failures in (1, 2, 4, 5, 100, 1024, 10_000, 10**6, 10**18):
            with self.subTest(failures=failures):
                with cache._lock:
                    cache._consecutive_failures = failures
                    delay = cache._retry_delay_locked()
                self.assertTrue(math.isfinite(delay))
                self.assertLessEqual(delay, 60.0)
                self.assertEqual(delay, min(5.0 * 2 ** min(failures - 1, 10), 60.0))

        # Extreme but finite base/max values stay bounded too.
        wide = board.StatusCache(
            compute,
            ttl_seconds=10,
            clock=_FakeClock(0.0),
            now=lambda: NOW,
            start_thread=lambda target: None,
            retry_base_seconds=1e-9,
            retry_max_seconds=1e9,
        )
        with wide._lock:
            wide._consecutive_failures = 10**9
            wide_delay = wide._retry_delay_locked()
        self.assertTrue(math.isfinite(wide_delay))
        self.assertEqual(wide_delay, 1e9)

    def test_huge_failure_streak_still_arms_a_capped_retry_window(self) -> None:
        pending: list = []
        attempts: list[int] = []

        def compute() -> dict:
            attempts.append(1)
            raise RuntimeError("github unavailable")

        clock = _FakeClock(0.0)
        cache = board.StatusCache(
            compute,
            ttl_seconds=10,
            clock=clock,
            now=lambda: NOW,
            start_thread=pending.append,
            retry_base_seconds=5.0,
            retry_max_seconds=60.0,
        )

        with cache._lock:
            cache._consecutive_failures = 10**9  # a very long outage

        cache.get()
        pending.pop()()  # one more failure on top of the huge streak

        snapshot, meta = cache.get()
        self.assertIsNone(snapshot)
        self.assertEqual(meta["state"], "cold")
        self.assertFalse(meta["refresh_in_progress"])
        self.assertEqual(meta["retry_in_seconds"], 60.0)  # capped, not overflowed
        self.assertEqual(meta["last_error"], "status refresh failed: RuntimeError")
        self.assertEqual(pending, [])
        self.assertEqual(attempts, [1])

        # The window still expires normally, so the cache is not wedged.
        clock.advance(60.0)
        _snapshot, meta = cache.get()
        self.assertTrue(meta["refresh_in_progress"])
        self.assertEqual(len(pending), 1)

    def test_zero_retry_base_disables_the_backoff_window(self) -> None:
        pending: list = []
        attempts: list[int] = []

        def compute() -> dict:
            attempts.append(1)
            raise RuntimeError("github unavailable")

        clock = _FakeClock(0.0)
        cache = board.StatusCache(
            compute,
            ttl_seconds=10,
            clock=clock,
            now=lambda: NOW,
            start_thread=pending.append,
            retry_base_seconds=0.0,
            retry_max_seconds=0.0,
        )

        cache.get()
        pending.pop()()  # the refresh fails

        with cache._lock:
            cache._consecutive_failures = 10**9
            self.assertEqual(cache._retry_delay_locked(), 0.0)

        # A zero base means no window at all: the next request retries at once
        # and no bogus retry_in_seconds is reported.
        _snapshot, meta = cache.get()
        self.assertIsNone(meta["retry_in_seconds"])
        self.assertTrue(meta["refresh_in_progress"])
        self.assertEqual(len(pending), 1)

    def test_retry_max_below_base_pins_the_delay_to_the_base(self) -> None:
        pending: list = []

        def compute() -> dict:
            raise RuntimeError("github unavailable")

        clock = _FakeClock(0.0)
        cache = board.StatusCache(
            compute,
            ttl_seconds=10,
            clock=clock,
            now=lambda: NOW,
            start_thread=pending.append,
            retry_base_seconds=5.0,
            retry_max_seconds=0.0,  # clamped up to the base by __init__
        )

        observed: list[float] = []
        for _ in range(3):
            _snapshot, meta = cache.get()
            self.assertTrue(meta["refresh_in_progress"])
            pending.pop()()  # the refresh fails
            _snapshot, meta = cache.get()
            observed.append(meta["retry_in_seconds"])
            clock.advance(meta["retry_in_seconds"])

        self.assertEqual(observed, [5.0, 5.0, 5.0])  # never doubles past the max

    def test_successful_refresh_clears_the_error_and_the_backoff(self) -> None:
        pending: list = []
        attempts: list[int] = []
        failing = [True]

        def compute() -> dict:
            attempts.append(1)
            if failing[0]:
                raise RuntimeError("github unavailable")
            return {"n": len(attempts)}

        clock = _FakeClock(0.0)
        cache = board.StatusCache(
            compute,
            ttl_seconds=10,
            clock=clock,
            now=lambda: NOW,
            start_thread=pending.append,
            retry_base_seconds=5.0,
        )

        cache.get()
        pending.pop()()  # failure 1
        clock.advance(5.0)
        cache.get()
        pending.pop()()  # failure 2 -> window doubled to 10s
        clock.advance(10.0)
        failing[0] = False
        cache.get()
        pending.pop()()  # success

        snapshot, meta = cache.get()
        self.assertEqual(snapshot, {"n": 3})
        self.assertEqual(meta["state"], "fresh")
        self.assertEqual(meta["last_error"], "")
        self.assertEqual(meta["last_error_at"], "")
        self.assertIsNone(meta["retry_in_seconds"])
        self.assertFalse(meta["refresh_in_progress"])

        # A later failure starts the backoff over at the base delay rather than
        # resuming the pre-recovery streak.
        clock.advance(10.0)
        failing[0] = True
        cache.get()
        pending.pop()()
        _snapshot, meta = cache.get()
        self.assertEqual(meta["retry_in_seconds"], 5.0)

    def test_start_thread_failure_resets_refreshing_flag_and_recovers(self) -> None:
        starts: list = []

        def flaky_start_thread(target) -> None:
            starts.append(target)
            if len(starts) == 1:
                raise RuntimeError("boom: could not spawn OS thread at /tmp/secret-path")

        calls: list[int] = []

        def compute() -> dict:
            calls.append(1)
            return {"n": len(calls)}

        clock = _FakeClock(0.0)
        cache = board.StatusCache(
            compute,
            ttl_seconds=10,
            clock=clock,
            now=lambda: NOW,
            start_thread=flaky_start_thread,
            retry_base_seconds=5.0,
        )

        snapshot, meta = cache.get()

        self.assertIsNone(snapshot)
        self.assertEqual(meta["state"], "cold")
        self.assertFalse(meta["refresh_in_progress"])  # a failed thread start must not wedge the flag forever
        self.assertEqual(meta["last_error"], "status refresh failed: RuntimeError")
        self.assertNotIn("secret", meta["last_error"])
        self.assertNotIn("/tmp", meta["last_error"])
        self.assertEqual(meta["retry_in_seconds"], 5.0)  # a failed start arms the same backoff
        self.assertEqual(calls, [])  # compute was never reached

        # The endpoint stays responsive, but requests inside the backoff window
        # must not keep retrying the thread start on every poll.
        snapshot_backoff, meta_backoff = cache.get()
        self.assertIsNone(snapshot_backoff)
        self.assertFalse(meta_backoff["refresh_in_progress"])
        self.assertEqual(meta_backoff["retry_in_seconds"], 5.0)
        self.assertEqual(len(starts), 1)

        # After the deadline, one request retries starting the refresh.
        clock.advance(5.0)
        snapshot_again, meta_again = cache.get()
        self.assertIsNone(snapshot_again)
        self.assertTrue(meta_again["refresh_in_progress"])
        self.assertEqual(meta_again["last_error"], "status refresh failed: RuntimeError")
        self.assertEqual(len(starts), 2)

        starts.pop()()  # run the retried refresh, as a background thread would
        final_snapshot, final_meta = cache.get()
        self.assertEqual(final_snapshot, {"n": 1})
        self.assertEqual(final_meta["state"], "fresh")
        self.assertEqual(final_meta["last_error"], "")

    def test_cache_error_summary_never_includes_exception_message_content(self) -> None:
        secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
        local_path = "/" + "Users/name/private/repo"
        exc = RuntimeError(f"failed at {local_path} with token {secret}\nstdout: some raw output")

        summary = board._cache_error_summary(exc)

        self.assertEqual(summary, "status refresh failed: RuntimeError")
        self.assertNotIn(secret, summary)
        self.assertNotIn(local_path, summary)
        self.assertNotIn("stdout", summary)

    def test_refresh_thread_base_exception_recovers_and_advances(self) -> None:
        class CustomThreadExit(BaseException):
            pass

        def make_compute(target_exc: BaseException) -> tuple[object, list[int]]:
            attempts: list[int] = []

            def compute() -> dict:
                attempts.append(1)
                if len(attempts) == 1:
                    raise target_exc
                return {"n": len(attempts)}

            return compute, attempts

        for exc in (SystemExit(1), CustomThreadExit("aborted")):
            with self.subTest(exc=exc.__class__.__name__):
                pending: list = []
                compute, attempts = make_compute(exc)
                clock = _FakeClock(0.0)
                cache = board.StatusCache(
                    compute,
                    ttl_seconds=10,
                    clock=clock,
                    now=lambda: NOW,
                    start_thread=pending.append,
                    retry_base_seconds=5.0,
                )

                _snapshot, meta = cache.get()
                self.assertTrue(meta["refresh_in_progress"])
                pending.pop()()  # raises BaseException

                _snapshot, meta = cache.get()
                self.assertFalse(meta["refresh_in_progress"])
                self.assertEqual(meta["last_error"], f"status refresh failed: {exc.__class__.__name__}")
                self.assertEqual(meta["retry_in_seconds"], 5.0)

                clock.advance(5.0)
                cache.get()  # starts retry
                pending.pop()()  # retry succeeds
                snapshot, meta = cache.get()
                self.assertEqual(snapshot, {"n": 2})
                self.assertEqual(meta["generation"], 1)
                self.assertEqual(meta["state"], "fresh")
                self.assertEqual(meta["last_error"], "")

    def test_abandoned_refresh_recovery_preserves_stale_and_advances_generation(self) -> None:
        pending: list = []
        calls: list[int] = []

        def compute() -> dict:
            calls.append(1)
            return {"n": len(calls)}

        clock = _FakeClock(0.0)
        cache = board.StatusCache(
            compute,
            ttl_seconds=5,
            clock=clock,
            now=lambda: NOW,
            start_thread=pending.append,
            retry_base_seconds=5.0,
            refresh_timeout_seconds=20.0,
        )

        # Generation 1 completes
        cache.get()
        pending.pop()()
        snapshot, meta = cache.get()
        self.assertEqual(snapshot, {"n": 1})
        self.assertEqual(meta["generation"], 1)

        # Stale snapshot served while refresh is in flight
        clock.advance(6.0)
        snapshot, meta = cache.get()
        self.assertEqual(snapshot, {"n": 1})
        self.assertTrue(meta["refresh_in_progress"])
        self.assertEqual(len(pending), 1)

        # Still in progress before recovery bound
        clock.advance(19.9)
        snapshot, meta = cache.get()
        self.assertTrue(meta["refresh_in_progress"])

        # Exceeds recovery bound: dead refresh recovered into bounded backoff
        clock.advance(0.1)
        snapshot, meta = cache.get()
        self.assertEqual(snapshot, {"n": 1})  # stale snapshot preserved
        self.assertFalse(meta["refresh_in_progress"])
        self.assertEqual(meta["last_error"], "status refresh failed: TimeoutError")
        self.assertEqual(meta["retry_in_seconds"], 5.0)
        self.assertEqual(meta["generation"], 1)

        # Late-running abandoned worker is ignored and does not mutate generation
        abandoned_worker = pending.pop(0)
        abandoned_worker()
        self.assertEqual(cache.generation, 1)

        # Backoff expires: single retry starts, succeeds, and advances generation
        clock.advance(5.0)
        cache.get()
        self.assertEqual(len(pending), 1)
        pending.pop()()
        snapshot, meta = cache.get()
        self.assertEqual(snapshot, {"n": 3})
        self.assertEqual(meta["generation"], 2)
        self.assertEqual(meta["state"], "fresh")
        self.assertEqual(meta["last_error"], "")

    def test_live_board_rehearsal_generation_advances_after_recovery(self) -> None:
        """Live Board rehearsal demonstrating that generation advances after recovery."""
        calls: list[int] = []
        fail_with_exit = threading.Event()

        def compute() -> dict:
            calls.append(len(calls) + 1)
            if fail_with_exit.is_set():
                fail_with_exit.clear()
                raise SystemExit("simulated thread exit")
            return {"board": {"schema": "code_mower.board.v1"}, "n": len(calls)}

        cache = board.StatusCache(
            compute,
            ttl_seconds=0.1,
            retry_base_seconds=0.1,
            retry_max_seconds=0.5,
            refresh_timeout_seconds=1.0,
        )
        server = board.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            board.make_handler(board.BoardConfig(repo="owner/repo"), status_cache=cache),
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            port = int(server.server_address[1])
            url = f"http://127.0.0.1:{port}/api/status"

            def poll_status() -> dict:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    return json.loads(resp.read().decode("utf-8")).get("board", {}).get("cache", {})

            # 1. Warm initial snapshot -> generation 1
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and poll_status().get("generation", 0) < 1:
                time.sleep(0.05)
            self.assertEqual(poll_status().get("generation"), 1)

            # 2. Trigger abnormal thread exit on next refresh
            time.sleep(0.15)
            fail_with_exit.set()
            poll_status()

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                meta = poll_status()
                if "SystemExit" in meta.get("last_error", "") and not meta.get("refresh_in_progress"):
                    break
                time.sleep(0.05)
            self.assertIn("SystemExit", poll_status().get("last_error", ""))

            # 3. Wait for backoff to expire, retry, and generation to advance to 2
            time.sleep(0.15)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and poll_status().get("generation", 0) < 2:
                time.sleep(0.05)
            self.assertEqual(poll_status().get("generation"), 2)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
