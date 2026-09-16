from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import http.client
import itertools
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch
from io import StringIO

from code_mower import board, board_observation, board_store, lane_status, reviewer_spend


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


TRUTH_HELPERS_END = "// --- presentation truth helpers (END) ---"

# A minimal stand-in for the pieces of the browser the shipped renderer
# touches: one element bag keyed by id, and a pinned clock so observation and
# campaign-liveness output is deterministic.
BOARD_DOM_HARNESS = """
const NODES = {};
const document = {getElementById: (id) => (NODES[id] = NODES[id] || {innerHTML: "", textContent: ""})};
Date.now = () => __NOW_MS__;
__SCRIPT__
render(JSON.parse(process.argv[1]));
renderEvents(JSON.parse(process.argv[2]));
console.log(JSON.stringify(Object.fromEntries(Object.entries(NODES).map(([id, node]) => [id, node.innerHTML || node.textContent]))));
"""


def _board_truth_helpers() -> str:
    """Lift the shipped, DOM-free presentation helpers out of the rendered page.

    The tests execute the same JavaScript the browser gets rather than a Python
    restatement of it, so a helper that is edited without its test is caught.
    """

    html = board.render_board_html(board.BoardConfig(repo="owner/repo"))
    start = html.find("    const text =")
    end = html.find(TRUTH_HELPERS_END)
    if start < 0 or end < start:  # pragma: no cover - guards the extraction
        raise AssertionError("board HTML no longer exposes the presentation truth helpers")
    return html[start : end + len(TRUTH_HELPERS_END)]


def _eval_board_truth(expression: str, *args: object) -> object:
    """Evaluate one shipped helper expression against JSON arguments."""

    script = (
        _board_truth_helpers()
        + "\nconst ARGS = process.argv.slice(1).map(value => JSON.parse(value));\n"
        + f"console.log(JSON.stringify({expression}));\n"
    )
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, *(json.dumps(arg) for arg in args)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _render_board_dom(
    payload: object,
    history: object | None = None,
    *,
    now: datetime = NOW,
) -> dict[str, str]:
    """Run the shipped ``render()``/``renderEvents()`` against a stubbed DOM."""

    html = board.render_board_html(board.BoardConfig(repo="owner/repo"))
    body = html[html.index("  <script>\n") + len("  <script>\n") : html.index("\n  </script>")]
    # The page kicks itself off with load(); the harness supplies the payload
    # directly instead of a fetch.
    trimmed = body.rsplit("    load();", 1)
    if len(trimmed) != 2:  # pragma: no cover - guards the extraction
        raise AssertionError("board HTML no longer bootstraps with load()")
    script = (
        BOARD_DOM_HARNESS.replace("__NOW_MS__", str(int(now.timestamp() * 1000)))
        .replace("__SCRIPT__", "".join(trimmed))
    )
    completed = subprocess.run(
        [
            shutil.which("node") or "node",
            "-e",
            script,
            json.dumps(payload),
            json.dumps(history if history is not None else {"events": []}),
        ],
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
                "headRefOid": "abcdef01abcdef01abcdef01abcdef01abcdef01",
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
    if args[0] == "api" and "/comments?" in args[1]:
        return []
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


def _pr(number: int, **overrides: object) -> dict[str, object]:
    pr: dict[str, object] = {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/owner/repo/pull/{number}",
        "branch": f"claude/{number}",
        "head_sha": "abcdef0123456789",
        "author": "claude-bot",
        "is_draft": False,
        "merge_state": "CLEAN",
        "updated_at": NOW.isoformat().replace("+00:00", "Z"),
        "labels": {"builder": ["builder:claude"], "needs": [], "done": [], "blocked": []},
        "checks": [],
        "stale": False,
        "next_action": "wait for audit",
        "next_detail": "",
    }
    pr.update(overrides)
    return pr


def _status(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "generated_at": NOW.isoformat().replace("+00:00", "Z"),
        "next_action": "inspect",
        "next_detail": "",
        "remote": {
            "available": True,
            "errors": [],
            "pull_requests": [],
            "workflow_runs": [],
            "gate_health": {"status": "pass", "alerts": []},
        },
        "board": {"version": {"serving_version": "0.9.0"}},
        "owner_queue": {"available": True, "count": 0, "entries": [], "message": ""},
        "agent_adapters": {"available": True, "path_exists": False, "agents": [], "message": ""},
        "orchestrator_lease": {"state": "absent"},
        "release_campaigns": {"available": True, "campaigns": []},
        "supervised_pilot": {"enabled": False, "cycle_state": "unavailable", "message": "off"},
        "timelines": {"verdicts": {"entries": []}, "spend": {"groups": []}},
        "productivity": {"status": "warn", "current": {}, "metrics": {}, "quality": {}, "spend": {}},
        "local_boards": {"boards": []},
        "local_processes": {"processes": []},
    }
    payload.update(overrides)
    return payload


@skipUnless(shutil.which("node"), "node is required to execute the shipped board renderer")
class BoardPresentationTruthTests(TestCase):
    """Issue #947: the Board may not claim more than the payload records."""

    def test_absent_measurements_render_as_not_recorded_not_zero(self) -> None:
        # Number(null), Number("") and Number(false) are all a finite 0, so a
        # naive Number.isFinite check turns "never measured" into "measured
        # zero". A real recorded zero must still render as zero.
        self.assertEqual(
            _eval_board_truth(
                "[seconds(null), seconds(undefined), seconds(''), seconds('12'), seconds(false),"
                " money(null), money(''), display(null), display(''), display(undefined)]"
            ),
            ["not recorded"] * 5 + ["not recorded"] * 5,
        )
        self.assertEqual(
            _eval_board_truth("[seconds(0), money(0), display(0), countOf(true, 0)]"),
            ["0.0s", "$0.000", "0", "0"],
        )
        # A count that could not be observed at all is not a zero count.
        self.assertEqual(_eval_board_truth("countOf(false, 0)"), "not recorded")

    def test_unknown_state_never_renders_green_or_as_pass(self) -> None:
        classes = _eval_board_truth(
            "['', 'unknown', 'unavailable', 'none', 'not recorded', 'off',"
            " 'pending', 'stale', 'unverified', 'last reported running',"
            " 'failure', 'blocked', 'success', 'complete'].map(stateClass)"
        )
        self.assertEqual(
            classes,
            ["muted"] * 6 + ["warn"] * 4 + ["bad"] * 2 + ["ok"] * 2,
        )

    def test_gate_publisher_success_cannot_pass_the_gate_verdict(self) -> None:
        # The gate workflow's job publishes the `code-mower/gate` commit status.
        # Its own success only means the publisher ran.
        pending = _eval_board_truth(
            "gateVerdict(ARGS[0])",
            _pr(
                7,
                checks=[
                    {"name": "publish Code Mower gate status", "state": "success"},
                    {"name": "code-mower/gate", "state": "pending"},
                ],
            ),
        )
        self.assertEqual(pending, {"state": "pending", "recorded": True, "class": "warn"})
        # With no `code-mower/gate` status at all the verdict is unrecorded,
        # never inherited from the publisher run beside it.
        missing = _eval_board_truth(
            "gateVerdict(ARGS[0])",
            _pr(7, checks=[{"name": "publish Code Mower gate status", "state": "success"}]),
        )
        self.assertEqual(missing, {"state": "not recorded", "recorded": False, "class": "muted"})
        self.assertEqual(
            _eval_board_truth(
                "['code-mower/gate', 'Code Mower gate', 'publish Code Mower gate status', 'package']"
                ".map(isGatePublisher)"
            ),
            [False, True, True, False],
        )

    def test_only_the_canonical_publisher_names_are_treated_as_publishers(self) -> None:
        # The publisher is the workflow that posts `code-mower/gate` and its
        # publishing job, matched case- and whitespace-insensitively. A check
        # that merely contains "gate" belongs to somebody else.
        self.assertEqual(
            _eval_board_truth(
                "['  code mower GATE ', 'Publish  Code Mower Gate Status'].map(isGatePublisher)"
            ),
            [True, True],
        )
        unrelated = [
            "security-gate",
            "gatekeeper",
            "release gate",
            "quality-gate/sonar",
            "code-mower/gate",
        ]
        self.assertEqual(
            _eval_board_truth("ARGS[0].map(isGatePublisher)", unrelated),
            [False] * len(unrelated),
        )
        # `code-mower/gate` stays the verdict, whatever spacing or case it
        # arrives in.
        self.assertEqual(
            _eval_board_truth("['code-mower/gate', ' CODE-MOWER/GATE '].map(isGateContext)"),
            [True, True],
        )

    def test_unrelated_gate_shaped_names_render_as_ordinary_checks_and_runs(self) -> None:
        nodes = _render_board_dom(
            _status(
                remote={
                    "available": True,
                    "errors": [],
                    "pull_requests": [_pr(7, checks=[{"name": "security-gate", "state": "success"}])],
                    "workflow_runs": [
                        {
                            "workflow": "security-gate",
                            "title": "scan",
                            "status": "completed",
                            "conclusion": "success",
                            "branch": "claude/7",
                            "url": "https://github.com/owner/repo/actions/runs/79",
                        }
                    ],
                    "gate_health": {"status": "pass", "alerts": []},
                }
            )
        )

        self.assertIn("security-gate=success", nodes["prs"])
        self.assertNotIn("publisher job, not the verdict", nodes["prs"])
        self.assertNotIn("gate publisher", nodes["runs"])
        self.assertNotIn("Publisher execution only", nodes["runs"])

    def test_gate_publisher_run_is_labelled_in_the_rendered_page(self) -> None:
        nodes = _render_board_dom(
            _status(
                remote={
                    "available": True,
                    "errors": [],
                    "pull_requests": [
                        _pr(
                            7,
                            checks=[
                                {"name": "publish Code Mower gate status", "state": "success"},
                                {"name": "code-mower/gate", "state": "pending"},
                            ],
                        )
                    ],
                    "workflow_runs": [
                        {
                            "workflow": "Code Mower gate",
                            "title": "publish gate",
                            "status": "completed",
                            "conclusion": "success",
                            "branch": "claude/7",
                            "url": "https://github.com/owner/repo/actions/runs/77",
                        }
                    ],
                    "gate_health": {"status": "pass", "alerts": []},
                },
                owner_queue={
                    "available": True,
                    "count": 1,
                    "message": "",
                    "entries": [
                        {"kind": "stale-gate", "pr_number": 7, "next_action": "rerun gate or inspect stuck audit"}
                    ],
                },
            )
        )

        self.assertIn("gate publisher", nodes["runs"])
        self.assertIn("Publisher execution only", nodes["runs"])
        self.assertIn("publisher job, not the verdict", nodes["prs"])
        self.assertIn("code-mower/gate=pending", nodes["prs"])
        # The verdict pill on the work item follows the commit status, not the
        # green publisher run beside it.
        self.assertIn('<span class="pill warn">gate pending</span>', nodes["worknow"] + nodes["lanework"])

    def test_a_pr_titled_after_the_gate_is_not_a_publisher_run(self) -> None:
        nodes = _render_board_dom(
            _status(
                remote={
                    "available": True,
                    "errors": [],
                    "pull_requests": [],
                    "workflow_runs": [
                        {
                            "workflow": "audit labeler",
                            "title": "Harden the gate alert wording",
                            "status": "completed",
                            "conclusion": "success",
                            "branch": "claude/1",
                            "url": "https://github.com/owner/repo/actions/runs/78",
                        }
                    ],
                    "gate_health": {"status": "pass", "alerts": []},
                }
            )
        )

        self.assertNotIn("gate publisher", nodes["runs"])

    def test_one_pr_with_several_reasons_is_one_grouped_work_item(self) -> None:
        prs = [_pr(7, merge_state="BEHIND", stale=True)]
        entries = [
            {"kind": "failing-check", "pr_number": 7, "next_action": "fix failing check"},
            {"kind": "stale-gate", "pr_number": 7, "next_action": "rerun gate"},
            {"kind": "rebase-needed", "pr_number": 7, "next_action": "rebase/behind"},
        ]

        items = _eval_board_truth("attentionItems(ARGS[0], ARGS[1])", entries, prs)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["pr_number"], 7)
        self.assertEqual(
            [reason["kind"] for reason in items[0]["reasons"]],
            ["failing-check", "stale-gate", "rebase-needed"],
        )
        # One primary responsible role, and the highest-precedence reason wins
        # the single next action.
        self.assertEqual(items[0]["role"], "builder")
        self.assertEqual(items[0]["next_action"], "fix failing check")

    def test_routine_lane_reasons_never_become_owner_attention(self) -> None:
        # Rebase, CI repair, audit fixes and re-review are builder/orchestrator
        # work regardless of how many of them a single PR raises.
        entries = [
            {"kind": kind, "pr_number": number, "next_action": kind}
            for number, kind in enumerate(
                ("blocked-audit", "failing-check", "rebase-needed", "stale-gate", "draft"), start=1
            )
        ]

        items = _eval_board_truth(
            "attentionItems(ARGS[0], ARGS[1])", entries, [_pr(n) for n in range(1, 6)]
        )

        self.assertEqual(
            {item["reasons"][0]["kind"]: item["role"] for item in items},
            {
                "blocked-audit": "builder",
                "failing-check": "builder",
                "rebase-needed": "builder",
                "stale-gate": "orchestrator",
                "draft": "builder",
            },
        )
        self.assertEqual([item for item in items if item["role"] == "owner"], [])

    def test_owner_attention_requires_explicit_evidence(self) -> None:
        labelled = _pr(7, labels={"needs": ["needs-owner"], "blocked": ["codex-audit-blocked"]})
        items = _eval_board_truth(
            "attentionItems(ARGS[0], ARGS[1])",
            [
                {
                    "kind": "needs-owner",
                    "pr_number": 7,
                    "next_action": "owner decision",
                    "labels": ["needs-owner"],
                },
                {"kind": "blocked-audit", "pr_number": 7, "next_action": "fix BLOCKED audit"},
            ],
            [labelled],
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["role"], "owner")
        self.assertEqual(items[0]["evidence"], ["needs-owner"])
        self.assertEqual(items[0]["next_action"], "owner decision")

        # A product decision is owner evidence too.
        product = _eval_board_truth(
            "attentionItems(ARGS[0], ARGS[1])",
            [{"kind": "needs-owner", "pr_number": 8, "next_action": "owner decision"}],
            [_pr(8, labels={"needs": ["product-decision"]})],
        )
        self.assertEqual(product[0]["role"], "owner")

        # A row that claims owner attention with no permission, budget, policy,
        # product-decision or owner-request signal anywhere in the payload is
        # orchestrator triage, not an owner decision.
        unbacked = _eval_board_truth(
            "attentionItems(ARGS[0], ARGS[1])",
            [{"kind": "needs-owner", "pr_number": 9, "next_action": "owner decision"}],
            [_pr(9, labels={"needs": ["needs-codex-audit"]})],
        )
        self.assertEqual(unbacked[0]["role"], "orchestrator")

    def test_grouped_work_items_split_owner_queue_from_lane_work(self) -> None:
        nodes = _render_board_dom(
            _status(
                remote={
                    "available": True,
                    "errors": [],
                    "pull_requests": [
                        _pr(7, merge_state="BEHIND", stale=True),
                        _pr(8, labels={"needs": ["needs-owner"]}),
                    ],
                    "workflow_runs": [],
                    "gate_health": {"status": "pass", "alerts": []},
                },
                owner_queue={
                    "available": True,
                    "count": 4,
                    "message": "",
                    "entries": [
                        {"kind": "needs-owner", "pr_number": 8, "next_action": "owner decision", "labels": ["needs-owner"]},
                        {"kind": "failing-check", "pr_number": 7, "next_action": "fix failing check"},
                        {"kind": "stale-gate", "pr_number": 7, "next_action": "rerun gate"},
                        {"kind": "rebase-needed", "pr_number": 7, "next_action": "rebase/behind"},
                    ],
                },
            )
        )

        # Three reasons for PR #7 are one row in Lane Work, not three rows in
        # the owner queue.
        self.assertEqual(nodes["lanework"].count('class="row"'), 1)
        self.assertIn("reasons (3):", nodes["lanework"])
        self.assertNotIn("#8", nodes["lanework"])
        self.assertEqual(nodes["owner"].count('class="row"'), 1)
        self.assertIn("#8", nodes["owner"])
        self.assertIn("owner evidence: needs-owner", nodes["owner"])
        # The summary counts work items, so one PR cannot inflate the owner
        # count through several reasons.
        self.assertIn("Owner decisions</span><b class=\"warn\">1</b>", nodes["summary"])
        self.assertIn("Lane work</span><b class=\"warn\">1</b>", nodes["summary"])
        # The deterministic next step is the owner decision, stated first.
        self.assertIn("Do next:", nodes["worknow"])
        self.assertIn("owner decision", nodes["worknow"])

    def test_current_work_precedes_aggregates_and_release_history(self) -> None:
        html = board.render_board_html(board.BoardConfig(repo="owner/repo"))

        order = [html.index(f">{title}<") for title in ("Work Now", "Owner Queue", "Lane Work")]
        self.assertEqual(order, sorted(order))
        for later in ("Release Campaigns", "Productivity", "Recent Local History", "Spend And Latency"):
            self.assertLess(html.index(">Work Now<"), html.index(f">{later}<"))
            self.assertLess(html.index(">Lane Work<"), html.index(f">{later}<"))

    def test_stale_snapshot_reports_age_and_suppresses_running_claims(self) -> None:
        observed = (NOW - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        stale = _eval_board_truth(
            "observation(ARGS[0], ARGS[1])",
            _status(
                productivity={
                    "status": "pass",
                    "current": {"source": "historical_board_snapshot", "observed_at": observed, "historical": True},
                }
            ),
            int(NOW.timestamp() * 1000),
        )
        self.assertTrue(stale["historical"])
        self.assertFalse(stale["live"])
        self.assertEqual(stale["age_text"], "3.0h")
        self.assertEqual(stale["label"], "last observed 3.0h ago")
        self.assertEqual(stale["class"], "warn")
        self.assertIn("nothing here is evidence of work running now", stale["detail"])

        # A live-sourced snapshot that simply stopped refreshing is stale by
        # age alone, whatever the payload calls its source.
        aged = _eval_board_truth(
            "observation(ARGS[0], ARGS[1])",
            _status(productivity={"status": "pass", "current": {"source": "live_remote", "observed_at": observed}}),
            int(NOW.timestamp() * 1000),
        )
        self.assertTrue(aged["aged"])
        self.assertFalse(aged["live"])
        self.assertEqual(aged["label"], "last observed 3.0h ago")

        fresh_at = (NOW - timedelta(seconds=9)).isoformat().replace("+00:00", "Z")
        live = _eval_board_truth(
            "observation(ARGS[0], ARGS[1])",
            _status(productivity={"status": "pass", "current": {"source": "live_remote", "observed_at": fresh_at}}),
            int(NOW.timestamp() * 1000),
        )
        self.assertTrue(live["live"])
        self.assertEqual(live["label"], "live, observed 9s ago")

    def test_unconfirmed_server_cache_is_never_labelled_live(self) -> None:
        # The server answers a cold cache with metadata only and a stale one
        # with the previous snapshot. A recent observation time embedded in
        # that unconfirmed snapshot is not evidence that it is current.
        fresh_at = (NOW - timedelta(seconds=9)).isoformat().replace("+00:00", "Z")
        payload = _status(
            generated_at=fresh_at,
            board={
                "version": {"serving_version": "0.9.0"},
                "cache": {"state": "stale", "age_seconds": 42.0, "refresh_in_progress": True},
            },
            productivity={"status": "pass", "current": {"source": "live_remote", "observed_at": fresh_at}},
        )

        stale_cache = _eval_board_truth("observation(ARGS[0], ARGS[1])", payload, int(NOW.timestamp() * 1000))

        self.assertTrue(stale_cache["unconfirmed"])
        self.assertFalse(stale_cache["live"])
        self.assertFalse(stale_cache["historical"])
        self.assertFalse(stale_cache["aged"])
        # The older of the two recorded ages is shown, so the cache age is not
        # understated by the fresher embedded observation time.
        self.assertEqual(stale_cache["age_text"], "42s")
        self.assertEqual(stale_cache["label"], "last observed 42s ago")
        self.assertEqual(stale_cache["class"], "warn")
        self.assertIn("nothing here is evidence of work running now", stale_cache["detail"])

        cold_cache = _eval_board_truth(
            "observation(ARGS[0], ARGS[1])",
            _status(
                generated_at=fresh_at,
                board={"version": {}, "cache": {"state": "cold", "age_seconds": None}},
                productivity={"status": "pass", "current": {"source": "live_remote", "observed_at": fresh_at}},
            ),
            int(NOW.timestamp() * 1000),
        )
        self.assertTrue(cold_cache["unconfirmed"])
        self.assertFalse(cold_cache["live"])

        # Only `fresh` confirms the snapshot the server is serving.
        confirmed = _eval_board_truth(
            "observation(ARGS[0], ARGS[1])",
            _status(
                generated_at=fresh_at,
                board={"version": {}, "cache": {"state": "fresh", "age_seconds": 9.0}},
                productivity={"status": "pass", "current": {"source": "live_remote", "observed_at": fresh_at}},
            ),
            int(NOW.timestamp() * 1000),
        )
        self.assertTrue(confirmed["live"])
        self.assertEqual(confirmed["label"], "live, observed 9s ago")

        nodes = _render_board_dom(payload)
        self.assertIn("last observed 42s ago", nodes["summary"])
        self.assertNotIn("live, observed", nodes["summary"])
        self.assertNotIn("live, observed", nodes["worknow"])
        self.assertIn("nothing here is evidence of work running now", nodes["worknow"])

    def test_missing_observation_time_is_neutral_and_never_live(self) -> None:
        # With no parseable observation time anywhere there is nothing to date
        # the snapshot by, so the page may claim neither freshness nor an age.
        payload = _status(generated_at="", productivity={"status": "pass", "current": {"source": "live_remote"}})

        unknown = _eval_board_truth("observation(ARGS[0], ARGS[1])", payload, int(NOW.timestamp() * 1000))

        self.assertFalse(unknown["live"])
        self.assertFalse(unknown["aged"])
        self.assertFalse(unknown["unconfirmed"])
        self.assertEqual(unknown["label"], "observation time not recorded")
        self.assertEqual(unknown["age_text"], "not recorded")
        self.assertEqual(unknown["class"], "muted")
        self.assertNotIn("live", unknown["label"])
        self.assertNotIn("ago", unknown["label"])
        self.assertIn("cannot be shown as current", unknown["detail"])

        # An unparseable timestamp is the same case as an absent one.
        garbled = _eval_board_truth(
            "observation(ARGS[0], ARGS[1])",
            _status(generated_at="not-a-timestamp", productivity={"current": {"observed_at": "soon"}}),
            int(NOW.timestamp() * 1000),
        )
        self.assertEqual(garbled["label"], "observation time not recorded")
        self.assertFalse(garbled["live"])

        nodes = _render_board_dom(payload)
        self.assertIn('<b class="muted">observation time not recorded</b>', nodes["summary"])
        self.assertNotIn("live, observed", nodes["summary"])
        self.assertNotIn("last observed not recorded", nodes["summary"] + nodes["worknow"])
        self.assertIn("cannot be shown as current", nodes["worknow"])

    def test_github_unavailable_renders_counts_as_not_recorded(self) -> None:
        nodes = _render_board_dom(
            _status(
                remote={
                    "available": False,
                    "errors": ["pull_requests: gh unavailable"],
                    "pull_requests": [],
                    "workflow_runs": [],
                    "gate_health": {"status": "pass", "alerts": []},
                }
            )
        )

        # No observation means no count, and an unobserved gate is not a clean
        # gate.
        self.assertIn("Open PRs</span><b class=\"muted\">not recorded</b>", nodes["summary"])
        self.assertIn("Gate alerts</span><b class=\"muted\">not recorded</b>", nodes["summary"])
        self.assertIn("gate alerts not recorded", nodes["alerts"])
        self.assertIn("last observed", nodes["summary"])

    def test_fresh_github_keeps_working_when_local_data_is_absent(self) -> None:
        sources = _eval_board_truth("localSources(ARGS[0])", _status())

        self.assertFalse(sources["adapters_available"])
        self.assertEqual(
            sources["missing"],
            ["agent adapter cards", "orchestrator lease", "reviewer verdict history", "reviewer spend rows"],
        )
        self.assertIn("Local session data unavailable", sources["message"])
        self.assertIn("GitHub data above is unaffected", sources["message"])

        nodes = _render_board_dom(
            _status(
                remote={
                    "available": True,
                    "errors": [],
                    "pull_requests": [_pr(7)],
                    "workflow_runs": [],
                    "gate_health": {"status": "pass", "alerts": []},
                }
            )
        )
        self.assertIn("Open PRs</span><b class=\"\">1</b>", nodes["summary"])
        self.assertIn("Agent cards</span><b class=\"muted\">not recorded</b>", nodes["summary"])
        self.assertIn("Local session data unavailable", nodes["worknow"])
        self.assertIn("#7", nodes["prs"])

    def test_old_running_campaign_is_reported_as_last_reported(self) -> None:
        past = (NOW - timedelta(hours=6)).isoformat().replace("+00:00", "Z")
        future = (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        now_ms = int(NOW.timestamp() * 1000)

        overdue = _eval_board_truth(
            "campaignLiveness(ARGS[0], ARGS[1])",
            {
                "status": "running",
                "elapsed_seconds": 12.0,
                "cards": [{"provider": "devin", "state": "running", "response_deadline_at": past}],
            },
            now_ms,
        )
        self.assertTrue(overdue["unverified"])
        self.assertEqual(overdue["label"], "last reported running")
        self.assertEqual(overdue["class"], "warn")
        self.assertEqual(overdue["cards"][0]["label"], "last reported running")
        self.assertEqual(overdue["cards"][0]["overdue_for"], "6.0h")

        # An unexpired response deadline is live evidence, so the present-tense
        # claim stands.
        current = _eval_board_truth(
            "campaignLiveness(ARGS[0], ARGS[1])",
            {
                "status": "running",
                "elapsed_seconds": 12.0,
                "cards": [{"provider": "devin", "state": "running", "response_deadline_at": future}],
            },
            now_ms,
        )
        self.assertFalse(current["unverified"])
        self.assertEqual(current["label"], "running")

        # No deadline at all is no liveness evidence either.
        silent = _eval_board_truth(
            "campaignLiveness(ARGS[0], ARGS[1])",
            {"status": "running", "elapsed_seconds": 0.0, "cards": [{"provider": "devin", "state": "running"}]},
            now_ms,
        )
        self.assertTrue(silent["unverified"])
        self.assertEqual(silent["label"], "last reported running")
        # A terminal campaign is never rewritten.
        complete = _eval_board_truth(
            "campaignLiveness(ARGS[0], ARGS[1])",
            {"status": "complete", "elapsed_seconds": 90.0, "cards": []},
            now_ms,
        )
        self.assertFalse(complete["unverified"])
        self.assertEqual(complete["label"], "complete")

    def test_terminal_provider_cards_keep_their_styling_with_expired_deadlines(self) -> None:
        # `complete` and `blocked` are release_campaigns' terminal evidence
        # states and `unavailable` never dispatched. All three can retain the
        # response deadline they were given, and that stale timestamp is not
        # evidence of a late provider.
        past = (NOW - timedelta(hours=6)).isoformat().replace("+00:00", "Z")
        now_ms = int(NOW.timestamp() * 1000)
        cards = [
            {"provider": "devin", "state": state, "response_deadline_at": past}
            for state in ("complete", "blocked", "unavailable", "running")
        ]

        liveness = _eval_board_truth(
            "ARGS[0].map(card => cardLiveness(card, ARGS[1]))", cards, now_ms
        )
        done, blocked, unavailable, control = liveness

        # A passed qualification stays green, not yellow.
        self.assertEqual((done["label"], done["class"]), ("complete", "ok"))
        self.assertFalse(done["awaiting"])
        self.assertFalse(done["overdue"])
        self.assertEqual(done["overdue_for"], "")
        # A failed qualification stays red; it is never downgraded to yellow.
        self.assertEqual((blocked["label"], blocked["class"]), ("blocked", "bad"))
        self.assertFalse(blocked["overdue"])
        # A provider that never dispatched is neutral, not overdue.
        self.assertEqual((unavailable["label"], unavailable["class"]), ("unavailable", "muted"))
        self.assertFalse(unavailable["overdue"])
        # Nonterminal control: a card still awaiting a response past its
        # deadline is still reported as overdue and last reported.
        self.assertTrue(control["awaiting"])
        self.assertTrue(control["overdue"])
        self.assertEqual((control["label"], control["class"]), ("last reported running", "warn"))
        self.assertEqual(control["overdue_for"], "6.0h")

        # A `queued` card is awaiting a response too, so its expired deadline
        # still counts -- but queued is not a running claim, so its own label
        # and state styling are untouched.
        queued = _eval_board_truth(
            "cardLiveness(ARGS[0], ARGS[1])",
            {"provider": "devin", "state": "queued", "response_deadline_at": past},
            now_ms,
        )
        self.assertTrue(queued["awaiting"])
        self.assertTrue(queued["overdue"])
        self.assertEqual(queued["label"], "queued")

    def test_finished_card_deadline_cannot_verify_a_running_campaign(self) -> None:
        # The only unexpired deadline belongs to a card that already answered,
        # so no card is actually awaiting a response and the campaign-level
        # running claim stays unverified.
        future = (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        past = (NOW - timedelta(hours=6)).isoformat().replace("+00:00", "Z")

        liveness = _eval_board_truth(
            "campaignLiveness(ARGS[0], ARGS[1])",
            {
                "status": "running",
                "elapsed_seconds": 30.0,
                "cards": [
                    {"provider": "devin", "state": "complete", "response_deadline_at": future},
                    {"provider": "cursor_cloud_agent", "state": "running", "response_deadline_at": past},
                ],
            },
            int(NOW.timestamp() * 1000),
        )

        self.assertTrue(liveness["unverified"])
        self.assertEqual(liveness["label"], "last reported running")
        self.assertEqual(liveness["cards"][0]["class"], "ok")
        self.assertEqual(liveness["cards"][1]["label"], "last reported running")

    def test_terminal_card_renders_without_an_overdue_warning(self) -> None:
        past = (NOW - timedelta(hours=6)).isoformat().replace("+00:00", "Z")
        nodes = _render_board_dom(
            _status(
                release_campaigns={
                    "available": True,
                    "campaigns": [
                        {
                            "release_tag": "v0.9.0",
                            "status": "complete",
                            "dry_run": False,
                            "qualification_context": "release",
                            "elapsed_seconds": 90.0,
                            "next_action": "campaign complete; all providers passed",
                            "cards": [
                                {
                                    "provider": "devin",
                                    "posture": "required",
                                    "state": "complete",
                                    "environment": "hosted",
                                    "elapsed_seconds": 90.0,
                                    "response_deadline_at": past,
                                    "next_action": "none",
                                }
                            ],
                        }
                    ],
                }
            )
        )

        self.assertIn('<span class="ok">complete</span>', nodes["campaigns"])
        self.assertNotIn("deadline passed", nodes["campaigns"])
        self.assertNotIn("last reported", nodes["campaigns"])

    def test_campaign_section_labels_elapsed_time_as_recorded_work(self) -> None:
        past = (NOW - timedelta(hours=6)).isoformat().replace("+00:00", "Z")
        nodes = _render_board_dom(
            _status(
                release_campaigns={
                    "available": True,
                    "campaigns": [
                        {
                            "release_tag": "v0.9.0",
                            "status": "running",
                            "dry_run": False,
                            "qualification_context": "release",
                            "elapsed_seconds": 12.0,
                            "next_action": "poll running providers",
                            "cards": [
                                {
                                    "provider": "devin",
                                    "posture": "required",
                                    "state": "running",
                                    "environment": "hosted",
                                    "elapsed_seconds": 12.0,
                                    "response_deadline_at": past,
                                    "next_action": "poll devin",
                                }
                            ],
                        }
                    ],
                }
            )
        )

        self.assertIn("last reported running", nodes["campaigns"])
        self.assertIn("recorded work 12.0s", nodes["campaigns"])
        self.assertIn("deadline passed 6.0h ago", nodes["campaigns"])
        self.assertIn("shown as last reported rather than currently running", nodes["campaigns"])

    def test_unmeasured_productivity_and_spend_render_as_not_recorded(self) -> None:
        nodes = _render_board_dom(
            _status(
                productivity={
                    "status": None,
                    "next_action": "record more board snapshots",
                    "current": {"source": "live_remote", "observed_at": NOW.isoformat().replace("+00:00", "Z")},
                    "metrics": {"cycle_time_seconds": None, "merged_pr_count": None},
                    "quality": {"audit_pass_count": None},
                    "spend": {"wall_seconds": None, "cost_usd": None, "total_tokens": None},
                },
                timelines={
                    "verdicts": {"entries": []},
                    "spend": {
                        "groups": [
                            {"lane": "codex", "verdict": None, "runs": 2, "wall_seconds_total": None,
                             "wall_seconds_avg": None, "cost_usd_total": None, "total_tokens": None}
                        ]
                    },
                },
            ),
            {"events": [{"created_at": NOW.isoformat().replace("+00:00", "Z"), "summary": {"next_action": "inspect"}}]},
        )

        self.assertIn("cycle not recorded", nodes["productivity"])
        self.assertIn("merged not recorded", nodes["productivity"])
        self.assertIn("not recorded tokens", nodes["productivity"])
        self.assertIn("Productivity</span><b class=\"muted\">not recorded</b>", nodes["summary"])
        self.assertIn("not recorded tokens", nodes["spend"])
        self.assertIn("PRs not recorded / alerts not recorded / local not recorded", nodes["history"])
        self.assertNotIn("$0.000", nodes["productivity"])


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

            # 1. Warm an initial snapshot. Fast hosts may refresh it again
            # before the following HTTP poll, so retain the observed generation
            # instead of assuming it is exactly one.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and poll_status().get("generation", 0) < 1:
                time.sleep(0.05)
            initial_generation = int(poll_status().get("generation", 0))
            self.assertGreaterEqual(initial_generation, 1)

            # 2. Trigger abnormal thread exit on next refresh
            time.sleep(0.15)
            fail_with_exit.set()
            poll_status()

            deadline = time.monotonic() + 5.0
            failed_meta: dict = {}
            while time.monotonic() < deadline:
                meta = poll_status()
                if "SystemExit" in meta.get("last_error", "") and not meta.get("refresh_in_progress"):
                    failed_meta = meta
                    break
                time.sleep(0.05)
            self.assertIn("SystemExit", failed_meta.get("last_error", ""))
            failed_generation = int(failed_meta.get("generation", 0))
            self.assertGreaterEqual(failed_generation, initial_generation)

            # 3. Wait for backoff to expire, retry, and advance beyond the
            # generation observed at failure.
            time.sleep(0.15)
            deadline = time.monotonic() + 5.0
            recovered_meta = poll_status()
            while (
                time.monotonic() < deadline
                and int(recovered_meta.get("generation", 0)) <= failed_generation
            ):
                time.sleep(0.05)
                recovered_meta = poll_status()
            self.assertGreater(int(recovered_meta.get("generation", 0)), failed_generation)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)


# --- Work-first Board views (#948) -----------------------------------------

WORK_MODEL_END = "// --- work view model (END) ---"

# The fixture clock plus 30s, so every fixture record is recent enough to be
# reported as current unless the record itself says otherwise.
OBSERVATION_NOW = datetime(2026, 9, 12, 20, 0, 30, tzinfo=UTC)

OBSERVATION_FIXTURES = Path(__file__).parent / "fixtures" / "board_observations.json"

# Render one payload after another through the shipped renderer in a single
# page lifetime, so selection, announcements and the change timeline are
# exercised the way a refresh actually exercises them. A step may select a work
# row by its opaque key before rendering the next payload.
BOARD_SEQUENCE_HARNESS = """
const NODES = {};
const document = {getElementById: (id) => (NODES[id] = NODES[id] || {innerHTML: "", textContent: ""})};
Date.now = () => __NOW_MS__;
__SCRIPT__
const frames = [];
for (const step of JSON.parse(process.argv[1])) {
  if (step.select !== null) selectWork(step.select);
  if (step.payload !== null) render(step.payload);
  frames.push(Object.fromEntries(Object.entries(NODES).map(([id, node]) => [id, node.innerHTML || node.textContent])));
}
console.log(JSON.stringify(frames));
"""


def _board_script() -> str:
    """The shipped page script, minus its own ``load()`` bootstrap."""

    html = board.render_board_html(board.BoardConfig(repo="codemower-ai/code-mower"))
    body = html[html.index("  <script>\n") + len("  <script>\n") : html.index("\n  </script>")]
    trimmed = body.rsplit("    load();", 1)
    if len(trimmed) != 2:  # pragma: no cover - guards the extraction
        raise AssertionError("board HTML no longer bootstraps with load()")
    return "".join(trimmed)


def _board_view_model() -> str:
    """Lift the shipped, DOM-free view-model transforms out of the page.

    The work view model is layered on the B1 truth helpers, so the extraction
    runs from the first helper through the end of the view-model block. As with
    the B1 helpers, the tests execute the JavaScript the browser gets.
    """

    html = board.render_board_html(board.BoardConfig(repo="codemower-ai/code-mower"))
    start = html.find("    const text =")
    end = html.find(WORK_MODEL_END)
    if start < 0 or end < start:  # pragma: no cover - guards the extraction
        raise AssertionError("board HTML no longer exposes the work view model")
    return html[start : end + len(WORK_MODEL_END)]


def _eval_board_view(expression: str, *args: object, mutate: tuple[str, str] | None = None) -> object:
    """Evaluate one shipped view-model expression against JSON arguments.

    ``mutate`` replaces one exact fragment of the shipped view model before it
    runs, so a test can execute the code this replaced and prove that the
    assertions it makes would actually catch its return.
    """

    model = _board_view_model()
    if mutate is not None:
        original, replacement = mutate
        if model.count(original) != 1:  # pragma: no cover - guards the mutation
            raise AssertionError(f"board view model no longer contains exactly one {original!r}")
        model = model.replace(original, replacement)
    script = (
        model
        + "\nconst ARGS = process.argv.slice(1).map(value => JSON.parse(value));\n"
        + f"console.log(JSON.stringify({expression}));\n"
    )
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, *(json.dumps(arg) for arg in args)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _render_board_sequence(
    steps: list[dict[str, object]],
    *,
    now: datetime = OBSERVATION_NOW,
) -> list[dict[str, str]]:
    """Render a sequence of payloads in one page lifetime."""

    script = BOARD_SEQUENCE_HARNESS.replace("__NOW_MS__", str(int(now.timestamp() * 1000))).replace(
        "__SCRIPT__", _board_script()
    )
    normalized = [{"select": step.get("select"), "payload": step.get("payload")} for step in steps]
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, json.dumps(normalized)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _observation_fixture(name: str) -> dict:
    """Build one accepted B0 fixture record, unchanged."""

    fixture = json.loads(OBSERVATION_FIXTURES.read_text(encoding="utf-8"))
    case = next(item for item in fixture["valid"] if item["name"] == name)
    record = copy.deepcopy(fixture["templates"][case["template"]])
    for pointer, value in case["set"].items():
        if pointer == "":
            record = copy.deepcopy(value)
            continue
        parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
        target: object = record
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        if isinstance(target, list):
            target[int(parts[-1])] = copy.deepcopy(value)
        else:
            target[parts[-1]] = copy.deepcopy(value)
    # Every fixture the views are tested against is a record the frozen
    # contract accepts, so no view is ever proved against a shape a producer
    # could not emit.
    return board_observation.validate(record)


def _record_with_reasons(fixture: str, reference: str, reasons: list[str]) -> dict:
    """One accepted record that carries several recorded states at once.

    The reasons are canonicalized and the primary route is derived exactly as
    the contract requires, so a record that is, say, both ready to merge and
    waiting for approval is a record a producer could really emit rather than
    a shape invented to make an ordering test pass.
    """

    record = copy.deepcopy(_observation_fixture(fixture))
    ordered = board_observation.ordered_reasons(reasons)
    record["work"]["id"] = reference
    record["work"]["reference"] = reference
    record["work"]["reasons"] = ordered
    record["work"]["primary"] = board_observation.derive_primary(ordered)
    for index, run in enumerate(record["work"]["runs"]):
        run["id"] = f"run{index}-{reference}"
        run["binding"]["work_id"] = reference
    return board_observation.validate(record)


def _referenced_record(reference: str) -> dict:
    """One accepted record distinguishable from every other by its reference."""

    fixture = _observation_fixture("observed_running")
    return _record_with_reasons("observed_running", reference, fixture["work"]["reasons"])


def _record_with_suspended_run(reference: str, *, reasons: list[str] | None = None) -> dict:
    """One accepted record whose run the provider suspended rather than failed.

    The contract allows the `suspended` lifecycle state only alongside the
    `failed` phase, so this is the shape a producer must emit for a paused
    session -- and the shape that reading the phase alone would misreport.
    """

    record = copy.deepcopy(_observation_fixture("failed"))
    ordered = board_observation.ordered_reasons(reasons or [])
    record["work"]["id"] = reference
    record["work"]["reference"] = reference
    record["work"]["reasons"] = ordered
    record["work"]["primary"] = board_observation.derive_primary(ordered)
    for index, run in enumerate(record["work"]["runs"]):
        run["id"] = f"run{index}-{reference}"
        run["binding"]["work_id"] = reference
        run["lifecycle"] = {
            **run["lifecycle"],
            "state": "suspended",
            "reason": "session_suspended",
            "next_action": "inspect_provider",
        }
    return board_observation.validate(record)


# One accepted fixture per run phase the frozen contract allows, so a record
# carrying that phase is always built from a shape the contract has already
# accepted rather than assembled by hand.
PHASE_FIXTURES = {
    "assigned": "assigned",
    "dispatched": "dispatched",
    "observed_running": "observed_running",
    "provider_progress": "provider_reported_progress",
    "waiting_for_user": "waiting_for_user",
    "waiting_for_approval": "waiting_for_approval",
    "implementation_complete": "implementation_complete",
    "failed": "failed",
    "cancelled": "cancelled",
}

# A reason and next action the remote-session projection accepts for each
# lifecycle state, so every combination below is a record a producer could
# really emit.
LIFECYCLE_ROUTES = {
    "pending": ("none", "none"),
    "running": ("none", "status"),
    "waiting_for_user": ("user_input_required", "none"),
    "waiting_for_approval": ("approval_required", "none"),
    "complete": ("none", "none"),
    "failed": ("session_failed", "inspect_provider"),
    "suspended": ("session_suspended", "inspect_provider"),
    "terminated": ("none", "none"),
    "archived": ("none", "none"),
    "uncertain": ("reconcile_dispatch", "status"),
}

# Every lifecycle state the frozen B0 contract accepts, against every phase it
# allows that state to carry, plus the no-lifecycle case for each phase. The
# expected label, class and cue are the operator meaning of the pair: the one
# state whose truthful meaning is not its phase is `suspended`, which the
# contract requires to carry the `failed` phase.
LIFECYCLE_DISPLAY_MATRIX = (
    (None, "assigned", "assigned", "muted", "?"),
    (None, "dispatched", "dispatched", "warn", "~"),
    (None, "observed_running", "observed running", "warn", "~"),
    (None, "provider_progress", "provider reported progress", "warn", "~"),
    (None, "waiting_for_user", "waiting for an answer", "warn", "~"),
    (None, "waiting_for_approval", "waiting for approval", "warn", "~"),
    (None, "implementation_complete", "implementation complete", "ok", "+"),
    (None, "failed", "failed", "bad", "!"),
    (None, "cancelled", "cancelled", "warn", "~"),
    ("pending", "dispatched", "dispatched", "warn", "~"),
    ("running", "observed_running", "observed running", "warn", "~"),
    ("running", "provider_progress", "provider reported progress", "warn", "~"),
    ("waiting_for_user", "waiting_for_user", "waiting for an answer", "warn", "~"),
    ("waiting_for_approval", "waiting_for_approval", "waiting for approval", "warn", "~"),
    ("complete", "implementation_complete", "implementation complete", "ok", "+"),
    ("failed", "failed", "failed", "bad", "!"),
    ("suspended", "failed", "suspended", "warn", "~"),
    ("terminated", "cancelled", "cancelled", "warn", "~"),
    ("archived", "implementation_complete", "implementation complete", "ok", "+"),
    ("archived", "cancelled", "cancelled", "warn", "~"),
    ("uncertain", "dispatched", "dispatched", "warn", "~"),
)


def _record_with_run_lifecycle(reference: str, phase: str, state: str | None) -> dict:
    """One accepted record whose single run carries ``phase`` under ``state``.

    The record is built from the accepted fixture for that phase and only its
    lifecycle is rewritten, so the contract's own phase/basis, liveness and
    lifecycle rules still decide whether the result is a record at all. No
    reason is recorded, so what the views say about the run is decided by the
    run rather than by a recorded blocker.
    """

    record = copy.deepcopy(_observation_fixture(PHASE_FIXTURES[phase]))
    record["work"]["id"] = reference
    record["work"]["reference"] = reference
    record["work"]["reasons"] = []
    record["work"]["primary"] = board_observation.derive_primary([])
    for index, run in enumerate(record["work"]["runs"]):
        run["id"] = f"run{index}-{reference}"
        run["binding"]["work_id"] = reference
        if state is None:
            run["lifecycle"] = None
            continue
        reason, next_action = LIFECYCLE_ROUTES[state]
        counts = (run["lifecycle"] or {}).get(
            "counts", {"dispatch": 0, "message": 0, "cancel": 0, "collect": 0}
        )
        run["lifecycle"] = {
            "schema": "code_mower.remote_session.v1",
            "state": state,
            "reason": reason,
            "next_action": next_action,
            "counts": counts,
        }
    return board_observation.validate(record)


# --- Session-scope reconciliation (#948) ------------------------------------

# Two more session identities the frozen contract accepts, so a scope can be
# moved without inventing a shape a producer could not record.
FIXTURE_SESSION = "a4ce901ecfb743609ed0b6504668aca7"
FIXTURE_WORKTREE = f"sha256:{'a' * 64}"
OTHER_SESSION = "b" * 32
OTHER_WORKTREE = f"sha256:{'c' * 64}"

_OBSERVATION_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _shift_instants(value: object, seconds: int) -> object:
    """Move every recorded instant in a decoded record by ``seconds``."""

    if isinstance(value, dict):
        return {key: _shift_instants(item, seconds) for key, item in value.items()}
    if isinstance(value, list):
        return [_shift_instants(item, seconds) for item in value]
    if isinstance(value, str) and _OBSERVATION_INSTANT.match(value):
        moved = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) + timedelta(
            seconds=seconds
        )
        return moved.strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


def _observed_later(record: dict, seconds: int) -> dict:
    """The same accepted record, recorded ``seconds`` later than it was.

    Every instant moves together, so each ordering rule the frozen contract
    enforces between them -- a source checked no later than the record, an
    event no later than the observation that caught it -- still holds. The
    result is re-validated rather than assumed, so a shifted record is still a
    record a producer could really emit.
    """

    return board_observation.validate(_shift_instants(copy.deepcopy(record), seconds))


def _in_session(record: dict, *, session: str, worktree: str) -> dict:
    """The same accepted record, observed in a different session scope."""

    moved = copy.deepcopy(record)
    moved["scope"]["session_id"] = session
    moved["scope"]["worktree_id"] = worktree
    for run in ((moved.get("work") or {}).get("runs") or []):
        run["binding"]["session_id"] = session
        run["binding"]["worktree_id"] = worktree
    return board_observation.validate(moved)


def _named_work(work_id: str, *, fixture: str = "observed_running") -> dict:
    """One accepted work observation carrying the given work identity."""

    record = copy.deepcopy(_observation_fixture(fixture))
    record["work"]["id"] = work_id
    record["work"]["reference"] = work_id
    for index, run in enumerate(record["work"]["runs"]):
        run["id"] = f"run{index}{work_id}"
        run["binding"]["work_id"] = work_id
    return board_observation.validate(record)


def _idle_key(session: str = FIXTURE_SESSION, worktree: str = FIXTURE_WORKTREE) -> str:
    return f"idle:{session}:{worktree}"


def _work_key(
    work_id: str, session: str = FIXTURE_SESSION, worktree: str = FIXTURE_WORKTREE
) -> str:
    return f"work:{session}:{worktree}:{work_id}"


def _observation_payload(records: list[dict], **overrides: object) -> dict:
    payload: dict[str, object] = {
        "generated_at": "2026-09-12T20:00:00Z",
        "next_action": "inspect",
        "board": {
            "cache": {
                "state": "fresh",
                "ttl_seconds": 15,
                "age_seconds": 1,
                "generation": 2,
                "refresh_in_progress": False,
                "retry_in_seconds": None,
            },
            "version": {
                "serving_version": "1.4.1",
                "installed_version": "1.4.1",
                "restart_recommended": False,
            },
        },
        "remote": {
            "available": True,
            "pull_requests": [],
            "workflow_runs": [],
            "gate_health": {"alerts": []},
        },
        "observations": {
            "available": True,
            "path_exists": True,
            "records": records,
            "warnings": [],
            "rejected": 0,
            "message": "",
        },
    }
    payload.update(overrides)
    return payload


# A DOM shim in which focus is a real question. Elements exist because the
# markup that was rendered declared an id; replacing a container's innerHTML
# destroys everything that was inside it, so a focused control that the refresh
# does not render again is genuinely gone and focus falls to the body exactly
# as a browser would drop it. Only the ids the shipped page ships in its static
# shell exist up front, so a lookup for a control that a render removed returns
# nothing rather than conjuring a phantom element to focus.
BOARD_FOCUS_HARNESS = """
class FakeElement {
  constructor(doc, id, attrs, parent) {
    this.doc = doc;
    this.id = id;
    this.attrs = attrs || {};
    this.parent = parent || null;
    this.dataset = {};
    this.classList = (this.attrs["class"] || "").split(/\\s+/).filter(Boolean);
    for (const [name, value] of Object.entries(this.attrs)) {
      if (!name.startsWith("data-")) continue;
      this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
    }
    this.hidden = false;
    this.textContent = "";
    this._html = "";
    // A freshly created element starts at the top, exactly as a replacement
    // node does in a browser: this is the reset the page has to undo. The
    // assignment itself is deliberately dumb -- nothing here clamps it -- so a
    // restored offset is only ever in range because the page put it in range.
    this.scrollTop = 0;
  }
  // Content inside a hidden panel has no box at all, so every layout metric
  // reads zero however tall the evidence is. That is the browser behaviour the
  // page has to tell apart from a genuine reading position of zero, so the
  // shim reproduces it rather than letting a test opt into it.
  get scrollHeight() { return this.doc.laidOut(this) ? (this.doc.metrics[this.id] || {}).scrollHeight || 0 : 0; }
  get clientHeight() { return this.doc.laidOut(this) ? (this.doc.metrics[this.id] || {}).clientHeight || 0 : 0; }
  get innerHTML() { return this._html; }
  set innerHTML(value) {
    this.doc.replaceChildren(this, value);
    this._html = value;
  }
  focus() { this.doc.activeElement = this; }
  matches(selector) {
    if (selector.startsWith(".")) return this.classList.includes(selector.slice(1));
    const attribute = /^\\[([A-Za-z-]+)(?:=([^\\]]*))?\\]$/.exec(selector);
    if (!attribute) return false;
    const value = this.attrs[attribute[1]];
    if (value === undefined) return false;
    return attribute[2] === undefined || value === attribute[2];
  }
  closest(selector) {
    for (let node = this; node; node = node.parent) if (node.matches(selector)) return node;
    return null;
  }
  querySelectorAll(selector) {
    return [...(this.doc.owned.get(this.id) || new Map()).values()]
      .filter(node => node.matches(selector));
  }
}
const document = {
  activeElement: null,
  body: null,
  // Content geometry the shim cannot compute, declared per element id by the
  // step that needs it, so a test can shrink the detail region between two
  // refreshes the way changed evidence really would.
  metrics: {},
  roots: new Map(),
  owned: new Map(),
  index: new Map(),
  // Which view panel each static container belongs to, read off the shipped
  // markup rather than assumed here.
  panelOf: __PANEL_OF__,
  getElementById(id) {
    if (this.index.has(id)) return this.index.get(id);
    return this.roots.get(id) || null;
  },
  laidOut(node) {
    for (let current = node; current; current = current.parent) {
      if (current.hidden === true) return false;
      const panel = this.panelOf[current.id];
      if (panel && (this.roots.get(panel) || {}).hidden === true) return false;
    }
    return true;
  },
  replaceChildren(root, html) {
    for (const [id, node] of this.owned.get(root.id) || new Map()) {
      if (this.index.get(id) === node) this.index.delete(id);
      if (this.activeElement === node) this.activeElement = this.body;
    }
    const created = new Map();
    for (const tag of html.match(/<[a-zA-Z][^>]*>/g) || []) {
      const attrs = {};
      for (const [, name, value] of tag.matchAll(/([A-Za-z-]+)="([^"]*)"/g)) attrs[name] = value;
      if (attrs.id === undefined) continue;
      const node = new FakeElement(this, attrs.id, attrs, root);
      created.set(attrs.id, node);
      this.index.set(attrs.id, node);
    }
    this.owned.set(root.id, created);
  }
};
document.body = new FakeElement(document, "", {});
document.activeElement = document.body;
for (const id of __SHELL_IDS__) document.roots.set(id, new FakeElement(document, id, {}));
Date.now = () => __NOW_MS__;
__SCRIPT__
const element = (id) => {
  const node = document.getElementById(id);
  if (!node) throw new Error("no element: " + id);
  return node;
};
const frames = [];
for (const step of JSON.parse(process.argv[1])) {
  if (step.metrics) Object.assign(document.metrics, step.metrics);
  if (step.focus) element(step.focus).focus();
  if (step.click) element(step.on || "worklist").onclick({target: element(step.click)});
  if (step.key) {
    element(step.on || "worklist").onkeydown({
      key: step.key,
      target: element(step.from),
      preventDefault: () => {},
    });
  }
  if (step.select) selectWork(step.select);
  if (step.scroll) {
    // A browser moves the element and then tells the page it moved. Both
    // halves are modelled: a page that only ever reads the offset out of the
    // element when it is about to be replaced has nothing to read once the
    // panel is hidden.
    const scrolled = element(step.scroll.id || "workdetail");
    scrolled.scrollTop = step.scroll.top;
    if (typeof scrolled.onscroll === "function") scrolled.onscroll();
  }
  if (step.payload) render(step.payload);
  const detail = document.getElementById("workdetail");
  frames.push({
    active: document.activeElement === document.body ? "" : document.activeElement.id,
    detail: detail === null ? null : {key: detail.attrs["data-key"] || "", top: detail.scrollTop},
    // What the page remembers outside the DOM, so a test can see that a
    // hidden poll left it alone and that an identity the Board stopped
    // showing was dropped rather than kept for ever.
    remembered: Object.fromEntries(detailOffsets),
    worklist: document.getElementById("worklist").innerHTML,
    tabs: document.getElementById("tabs").innerHTML,
    hidden: Object.fromEntries(["now", "timeline", "releases", "health"]
      .map(view => [view, element("panel-" + view).hidden])),
  });
}
console.log(JSON.stringify(frames));
"""


def _board_shell_ids() -> list[str]:
    """Every id the shipped page's static markup declares, script excluded."""

    html = board.render_board_html(board.BoardConfig(repo="codemower-ai/code-mower"))
    shell = html[: html.index("  <script>\n")]
    return sorted(set(re.findall(r'id="([^"]+)"', shell)))


def _board_panel_of() -> dict[str, str]:
    """Which view panel each static container in the shipped shell sits inside."""

    html = board.render_board_html(board.BoardConfig(repo="codemower-ai/code-mower"))
    shell = html[: html.index("  <script>\n")]
    panels: dict[str, str] = {}
    sections = list(re.finditer(r'<section class="view" id="(panel-[a-z]+)"', shell))
    for index, section in enumerate(sections):
        end = sections[index + 1].start() if index + 1 < len(sections) else len(shell)
        for found in re.findall(r'id="([^"]+)"', shell[section.start() : end]):
            if found != section.group(1):
                panels[found] = section.group(1)
    return panels


def _render_board_focus(
    steps: list[dict[str, object]],
    *,
    now: datetime = OBSERVATION_NOW,
) -> list[dict[str, str]]:
    """Drive focus, selection and refresh through the shipped page in one lifetime."""

    script = (
        BOARD_FOCUS_HARNESS.replace("__NOW_MS__", str(int(now.timestamp() * 1000)))
        .replace("__SHELL_IDS__", json.dumps(_board_shell_ids()))
        .replace("__PANEL_OF__", json.dumps(_board_panel_of()))
        .replace("__SCRIPT__", _board_script())
    )
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, json.dumps(steps)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


# Evaluate one expression against the whole shipped page script, so the id
# encoding the rows, the detail region and the actions all share can be
# exercised directly on keys the renderer would have to survive.
BOARD_PAGE_HARNESS = """
const NODES = {};
const document = {getElementById: (id) => (NODES[id] = NODES[id] || {innerHTML: "", textContent: ""})};
Date.now = () => __NOW_MS__;
__SCRIPT__
const ARGS = process.argv.slice(1).map(value => JSON.parse(value));
console.log(JSON.stringify(__EXPRESSION__));
"""


def _eval_board_page(expression: str, *args: object) -> object:
    """Evaluate one shipped page expression against JSON arguments."""

    script = (
        BOARD_PAGE_HARNESS.replace("__NOW_MS__", str(int(OBSERVATION_NOW.timestamp() * 1000)))
        .replace("__SCRIPT__", _board_script())
        .replace("__EXPRESSION__", expression)
    )
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, *(json.dumps(arg) for arg in args)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


# The desktop width at which the work list gains its second column.
DESKTOP_MEDIA = "@media (min-width: 900px)"

# The rule this page shipped before the detail region was put into normal
# flow. It is kept here as the regression the layout model has to catch: an
# out-of-flow detail contributes no height, so a short list leaves it hanging
# over whatever follows.
PREVIOUS_DESKTOP_CSS = """
.workrows { display:grid; gap:8px; }
.workrow { border:1px solid var(--line); }
.workdetail { border-top:1px solid var(--line); padding:12px; }
@media (min-width: 900px) {
  .worklayout { position:relative; padding-right:372px; min-height:180px; }
  .workdetail { position:absolute; top:0; right:0; width:356px; max-height:70vh; overflow:auto; }
}
"""


def _css_rules(css: str) -> list[tuple[str, str, dict[str, str]]]:
    """Every ``(at-rule, selector, declarations)`` triple a stylesheet declares."""

    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules: list[tuple[str, str, dict[str, str]]] = []
    stack: list[str] = []
    prelude = ""
    index = 0
    while index < len(css):
        char = css[index]
        if char == "{":
            head, prelude = prelude.strip(), ""
            if head.startswith("@"):
                stack.append(" ".join(head.split()))
                index += 1
                continue
            end = css.index("}", index)
            declarations: dict[str, str] = {}
            for item in css[index + 1 : end].split(";"):
                name, separator, value = item.partition(":")
                if separator:
                    declarations[name.strip()] = value.strip()
            for selector in head.split(","):
                rules.append((" ".join(stack), " ".join(selector.split()), declarations))
            index = end + 1
            continue
        if char == "}":
            if stack:
                stack.pop()
            prelude = ""
            index += 1
            continue
        prelude += char
        index += 1
    return rules


def _board_css() -> str:
    """The stylesheet the shipped page serves."""

    html = board.render_board_html(board.BoardConfig(repo="owner/repo"))
    return html[html.index("<style>") + len("<style>") : html.index("</style>")]


def _matches(selector: str, node: dict, ancestors: list[dict]) -> bool:
    """Match the descendant-and-class selectors this stylesheet is written in."""

    compounds = selector.split()
    subject, rest = compounds[-1], compounds[:-1]
    if not _matches_compound(subject, node):
        return False
    remaining = list(ancestors)
    for compound in reversed(rest):
        while remaining and not _matches_compound(compound, remaining[-1]):
            remaining.pop()
        if not remaining:
            return False
        remaining.pop()
    return True


def _matches_compound(compound: str, node: dict) -> bool:
    if compound.startswith("#"):
        return node.get("id") == compound[1:]
    classes = set(node.get("classes", ()))
    parts = [part for part in compound.split(".") if part]
    if not compound.startswith("."):
        tag, parts = parts[0], parts[1:]
        if tag != node.get("tag"):
            return False
    return all(part in classes for part in parts)


def _computed(css: str, node: dict, ancestors: list[dict], *, desktop: bool) -> dict[str, str]:
    """Cascade the stylesheet onto one node in source order."""

    style: dict[str, str] = {}
    for at_rule, selector, declarations in _css_rules(css):
        if at_rule and not (desktop and at_rule == DESKTOP_MEDIA):
            continue
        if _matches(selector, node, ancestors):
            style.update(declarations)
    return style


def _px(value: str, *, viewport: int) -> float:
    if value.endswith("px"):
        return float(value[:-2])
    if value.endswith("vh"):
        return float(value[:-2]) * viewport / 100
    return 0.0


def _track_count(value: str) -> int:
    """Count the tracks a ``grid-template-columns`` value declares."""

    tracks, depth, token = 0, 0, ""
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char.isspace() and depth == 0:
            tracks += 1 if token else 0
            token = ""
            continue
        token += char
    return tracks + (1 if token else 0)


def _work_list_layout(
    css: str,
    *,
    container_classes: tuple[str, ...] = (),
    rows: int,
    selected: int,
    row_height: float,
    detail_height: float,
    viewport: int = 900,
) -> dict[str, float]:
    """Model the desktop height the work list reserves and where the detail ends.

    A deliberately small box model: enough to tell an out-of-flow detail, which
    reserves nothing, from a detail that is a real item of the row's grid and
    therefore makes the row -- and so the list, and so the section -- at least
    as tall as itself.
    """

    container = {"tag": "div", "id": "worklist", "classes": list(container_classes)}
    list_node = {"tag": "ul", "classes": ["workrows"]}
    row_nodes = [
        {"tag": "li", "classes": ["workrow"] + (["selected"] if index == selected else [])}
        for index in range(rows)
    ]
    detail_node = {"tag": "div", "classes": ["workdetail"]}
    ancestors = [container, list_node, row_nodes[selected]]

    container_style = _computed(css, container, [], desktop=True)
    list_style = _computed(css, list_node, [container], desktop=True)
    row_style = _computed(css, row_nodes[selected], [container, list_node], desktop=True)
    detail_style = _computed(css, detail_node, ancestors, desktop=True)

    height = detail_height
    if "max-height" in detail_style:
        height = min(height, _px(detail_style["max-height"], viewport=viewport))
    gap = _px(list_style.get("gap", "0px"), viewport=viewport)

    out_of_flow = detail_style.get("position") in {"absolute", "fixed"}
    side_by_side = (
        row_style.get("display") == "grid"
        and _track_count(row_style.get("grid-template-columns", "")) > 1
        and detail_style.get("grid-column") not in {None, "1"}
    )
    if out_of_flow:
        row_heights = [row_height] * rows
    elif side_by_side:
        row_heights = [
            max(row_height, height) if index == selected else row_height for index in range(rows)
        ]
    else:  # normal flow, single column: the detail sits under its own row
        row_heights = [
            row_height + height if index == selected else row_height for index in range(rows)
        ]

    reserved = sum(row_heights) + gap * max(rows - 1, 0)
    reserved = max(reserved, _px(container_style.get("min-height", "0px"), viewport=viewport))
    if out_of_flow:
        # Positioned against the list box, so it starts at the list's own top.
        detail_bottom = _px(detail_style.get("top", "0px"), viewport=viewport) + height
    else:
        above = sum(row_heights[:selected]) + gap * selected
        detail_bottom = above + height
    return {"reserved": reserved, "detail_bottom": detail_bottom}


def _work_keys(worklist: str) -> list[str]:
    return re.findall(r'class="rowbtn" id="[^"]+" data-key="([^"]+)"', worklist)


def _row_element_id(worklist: str, key: str) -> str:
    match = re.search(rf'class="rowbtn" id="([^"]+)" data-key="{re.escape(key)}"', worklist)
    return match.group(1) if match else ""


def _selected_key(worklist: str) -> str:
    match = re.search(r'data-key="([^"]+)" aria-expanded="true"', worklist)
    return match.group(1) if match else ""


class _RecordingHandle:
    """A file handle that records the size of every read it is asked for."""

    def __init__(self, handle: object, reads: list[int]) -> None:
        self._handle = handle
        self._reads = reads

    def __enter__(self) -> "_RecordingHandle":
        self._handle.__enter__()
        return self

    def __exit__(self, *exc: object) -> object:
        return self._handle.__exit__(*exc)

    def read(self, size: int = -1) -> bytes:
        self._reads.append(size)
        return self._handle.read(size)


class BoardObservationReaderTests(TestCase):
    """The Board consumes the frozen observation contract; it never writes one."""

    def _write(self, directory: Path, name: str, record: object) -> None:
        (directory / name).write_text(json.dumps(record), encoding="utf-8")

    def test_missing_directory_is_nothing_recorded_not_no_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = board.observations_payload(
                board.BoardConfig(repo="owner/repo", observations_path=str(Path(tmp) / "absent"))
            )
        self.assertTrue(payload["available"])
        self.assertFalse(payload["path_exists"])
        self.assertEqual(payload["records"], [])
        self.assertEqual(payload["message"], "no local Board observations recorded yet")
        self.assertEqual(payload["path"], lane_status.LOCAL_PATH_REDACTION)

    def test_valid_records_are_returned_and_invalid_ones_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._write(directory, "a-work.json", _observation_fixture("observed_running"))
            self._write(directory, "b-idle.json", _observation_fixture("no_work"))
            # A record that fails the contract is dropped, not repaired.
            broken = _observation_fixture("observed_running")
            broken["work"]["reasons"] = ["review_requested", "approval_required"]
            self._write(directory, "c-broken.json", broken)
            (directory / "d-garbage.json").write_text("{not json", encoding="utf-8")
            payload = board.observations_payload(
                board.BoardConfig(repo="owner/repo", observations_path=str(directory))
            )

        self.assertEqual(len(payload["records"]), 2)
        self.assertEqual([record["kind"] for record in payload["records"]], ["work", "no_work"])
        self.assertEqual(payload["rejected"], 2)
        messages = {warning["message"] for warning in payload["warnings"]}
        # Contract diagnostics are a fixed vocabulary with no values or paths.
        self.assertTrue(messages <= {"invalid_route", "invalid_contract"})
        self.assertEqual(payload["record_schema"], board_observation.SCHEMA)

    def test_reading_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            record = _observation_fixture("no_work")
            for index in range(board.MAX_OBSERVATION_FILES + 5):
                self._write(directory, f"obs-{index:03d}.json", record)
            payload = board.observations_payload(
                board.BoardConfig(repo="owner/repo", observations_path=str(directory))
            )
        self.assertEqual(len(payload["records"]), board.MAX_OBSERVATION_FILES)

    def _fill(self, directory: Path, count: int, *, ages: bool = False) -> list[str]:
        """Write ``count`` distinguishable accepted records, newest name last."""

        names = []
        for index in range(count):
            name = f"obs-{index:03d}.json"
            self._write(directory, name, _referenced_record(f"work-{index:03d}"))
            if ages:
                # Modification time rises with the name, so the alphabetically
                # first files are the oldest ones -- exactly the arrangement in
                # which an alphabetical cap would keep the stalest records and
                # drop every current one.
                stamp = 1_600_000_000 + index
                os.utime(directory / name, (stamp, stamp))
            names.append(name)
        return names

    def _references(self, payload: dict) -> list[str]:
        return [record["work"]["reference"] for record in payload["records"]]

    def test_file_coverage_is_reported_at_every_cap_boundary(self) -> None:
        """Exactly at the cap is complete; one file past it is not.

        A directory larger than the cap is still read bounded, but the shortfall
        is counted and stated rather than disappearing into a snapshot that
        looks whole.
        """

        cap = board.MAX_OBSERVATION_FILES
        for count in (0, cap - 1, cap, cap + 1, cap + 9):
            with self.subTest(files=count), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                self._fill(directory, count)
                payload = board.observations_payload(
                    board.BoardConfig(repo="owner/repo", observations_path=str(directory))
                )
                read = min(count, cap)
                self.assertTrue(payload["available"])
                self.assertEqual(payload["file_cap"], cap)
                self.assertEqual(payload["candidate_files"], count)
                self.assertEqual(payload["read_files"], read)
                self.assertEqual(payload["omitted_files"], count - read)
                self.assertEqual(len(payload["records"]), read)
                self.assertEqual(payload["rejected"], 0)
                self.assertEqual(payload["truncated"], count > cap)
                self.assertEqual(payload["coverage"], "partial" if count > cap else "complete")
                self.assertEqual(payload["selection"], board.OBSERVATION_SELECTION)
                if count > cap:
                    self.assertIn(f"{read} of {count}", payload["message"])
                    self.assertIn("incomplete", payload["message"])
                elif count:
                    # A directory inside the cap reads exactly as it did
                    # before: every record, in file-name order, no message.
                    self.assertEqual(payload["message"], "")
                    self.assertEqual(
                        self._references(payload), [f"work-{index:03d}" for index in range(count)]
                    )

    def test_an_overflowing_directory_stays_bounded_in_files_and_in_bytes(self) -> None:
        cap = board_observation.MAX_BYTES
        board_observation.schema()
        reads: list[int] = []
        real_open = Path.open

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._fill(directory, board.MAX_OBSERVATION_FILES * 3 + 4)

            def recording_open(self: Path, *args: object, **kwargs: object) -> object:
                handle = real_open(self, *args, **kwargs)
                return _RecordingHandle(handle, reads) if self.parent == directory else handle

            with patch.object(Path, "open", recording_open):
                payload = board.observations_payload(
                    board.BoardConfig(repo="owner/repo", observations_path=str(directory))
                )

        # Counting the whole candidate set never widens the read: at most the
        # cap many files are opened, each with one bounded request.
        self.assertEqual(reads, [cap + 1] * board.MAX_OBSERVATION_FILES)
        self.assertEqual(payload["candidate_files"], board.MAX_OBSERVATION_FILES * 3 + 4)
        self.assertEqual(payload["read_files"], board.MAX_OBSERVATION_FILES)
        self.assertTrue(payload["truncated"])

    def test_selection_is_deterministic_under_reversed_directory_iteration(self) -> None:
        """Directory order is not an input to which files are read."""

        real_scandir = os.scandir

        class _ReversedScan:
            def __init__(self, entries: list[object]) -> None:
                self._entries = entries

            def __enter__(self) -> object:
                return iter(self._entries)

            def __exit__(self, *exc: object) -> bool:
                return False

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._fill(directory, board.MAX_OBSERVATION_FILES + 7, ages=True)
            config = board.BoardConfig(repo="owner/repo", observations_path=str(directory))
            forward = board.observations_payload(config)

            def reversed_scandir(path: object) -> object:
                with real_scandir(path) as entries:
                    return _ReversedScan(list(entries)[::-1])

            with patch.object(board.os, "scandir", reversed_scandir):
                backward = board.observations_payload(config)

        self.assertEqual(self._references(forward), self._references(backward))
        self.assertEqual(forward["candidate_files"], backward["candidate_files"])
        self.assertEqual(forward["omitted_files"], backward["omitted_files"])

    def test_the_bounded_set_does_not_systematically_starve_current_records(self) -> None:
        """The cap keeps the most recently written files, not the first names.

        File times are not part of the frozen record contract, so this is a
        best-effort preference rather than evidence -- which is why the read is
        reported as incomplete either way.
        """

        cap = board.MAX_OBSERVATION_FILES
        extra = 8
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._fill(directory, cap + extra, ages=True)
            payload = board.observations_payload(
                board.BoardConfig(repo="owner/repo", observations_path=str(directory))
            )

        self.assertEqual(
            self._references(payload),
            [f"work-{index:03d}" for index in range(extra, cap + extra)],
        )
        # Alphabetically first is exactly what is dropped here, so the newest
        # records survive the cap instead of being starved by it.
        self.assertNotIn("work-000", self._references(payload))
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["omitted_files"], extra)

    def test_truncation_metadata_names_no_file_and_no_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for index in range(board.MAX_OBSERVATION_FILES + 3):
                self._write(
                    directory,
                    f"private-session-{index:03d}.json",
                    _referenced_record(f"work-{index:03d}"),
                )
            payload = board.observations_payload(
                board.BoardConfig(repo="owner/repo", observations_path=str(directory))
            )

        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["warnings"], [])
        encoded = json.dumps(payload)
        self.assertNotIn("private-session", encoded)
        self.assertNotIn(str(directory), encoded)
        self.assertEqual(payload["path"], lane_status.LOCAL_PATH_REDACTION)
        # Truncation is counted metadata; the contract's record diagnostics stay
        # their own closed vocabulary and say nothing about unread files.
        self.assertNotIn("truncat", json.dumps(payload["warnings"]))

    def test_an_unlistable_directory_reports_unavailable_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with patch.object(board.os, "scandir", side_effect=OSError("denied")):
                payload = board.observations_payload(
                    board.BoardConfig(repo="owner/repo", observations_path=str(directory))
                )

        self.assertFalse(payload["available"])
        self.assertEqual(payload["coverage"], "unavailable")
        self.assertFalse(payload["truncated"])
        # Nothing was counted, so no total is invented for it.
        self.assertIsNone(payload["candidate_files"])
        self.assertIsNone(payload["omitted_files"])
        self.assertEqual(payload["read_files"], 0)

    def test_an_oversize_file_is_read_bounded_and_rejected_before_it_is_decoded(self) -> None:
        cap = board_observation.MAX_BYTES
        # Warm the contract's own schema read so it cannot be mistaken for one
        # of the observation reads being measured here.
        board_observation.schema()
        reads: list[int] = []
        decoded: list[int] = []
        real_open = Path.open
        real_decode = board_observation.decode

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._write(directory, "a-small.json", _observation_fixture("no_work"))
            # Far past the contract's cap, so there is a real remainder that
            # must never be pulled into memory.
            (directory / "b-huge.json").write_bytes(b'{"padding":"' + b"x" * (cap * 4) + b'"}')

            def recording_open(self: Path, *args: object, **kwargs: object) -> object:
                handle = real_open(self, *args, **kwargs)
                return _RecordingHandle(handle, reads) if self.parent == directory else handle

            def recording_decode(raw: bytes) -> object:
                decoded.append(len(raw))
                return real_decode(raw)

            with patch.object(Path, "open", recording_open), patch.object(
                board.board_observation, "decode", recording_decode
            ):
                payload = board.observations_payload(
                    board.BoardConfig(repo="owner/repo", observations_path=str(directory))
                )

        # Every observation file is read with the same bounded request: one
        # byte past the cap, which is all it takes to know the file is too big.
        self.assertEqual(reads, [cap + 1, cap + 1])
        # Only the record inside the cap ever reached the decoder, and it was
        # never handed more bytes than the contract allows.
        self.assertEqual(len(decoded), 1)
        self.assertLessEqual(decoded[0], cap)

        self.assertEqual(len(payload["records"]), 1)
        self.assertEqual(payload["records"][0]["kind"], "no_work")
        self.assertEqual(payload["rejected"], 1)
        self.assertEqual(
            payload["warnings"], [{"file": "b-huge.json", "message": "invalid_contract"}]
        )
        # The diagnostic carries no path and no content from the file.
        self.assertEqual(payload["path"], lane_status.LOCAL_PATH_REDACTION)
        self.assertNotIn("x" * 32, json.dumps(payload))
        self.assertNotIn(str(directory), json.dumps(payload))

    def test_observations_are_not_written_into_local_history(self) -> None:
        snapshot = {"schema": "code_mower.laneStatus.v1", "observations": {"records": [1]}}
        self.assertNotIn("observations", board._recordable_payload(snapshot))
        self.assertIn("observations", snapshot)

    def test_resolved_metadata_paths_bind_the_observation_directory(self) -> None:
        paths = board.resolved_metadata_paths(board.BoardConfig(repo="owner/repo", repo_path="/repo"))
        self.assertTrue(paths["observations_path"].endswith("/.code-mower/board/observations"))


@skipUnless(shutil.which("node"), "node is required to execute the shipped board renderer")
class BoardWorkFirstViewTests(TestCase):
    """The Now, Timeline, Releases and Health views over B0 fixtures."""

    def test_views_are_semantic_tabs_with_visible_focus_and_expanded_state(self) -> None:
        html = board.render_board_html(board.BoardConfig(repo="owner/repo"))
        for view in ("now", "timeline", "releases", "health"):
            self.assertIn(f'id="panel-{view}" role="tabpanel" aria-labelledby="tab-{view}"', html)
        self.assertIn('role="tablist"', html)
        self.assertIn(":focus-visible { outline:", html)

        nodes = _render_board_sequence(
            [{"payload": _observation_payload([_observation_fixture("observed_running")])}]
        )[0]
        tabs = nodes["tabs"]
        self.assertEqual(tabs.count('role="tab"'), 4)
        self.assertEqual(tabs.count('aria-selected="true"'), 1)
        self.assertIn('id="tab-now" data-view="now" aria-selected="true"', tabs)
        # Roving tabindex: exactly one tab is in the tab order.
        self.assertEqual(tabs.count('tabindex="0"'), 1)
        self.assertEqual(tabs.count('tabindex="-1"'), 3)
        # One row is expanded and it is the one carrying the detail region.
        self.assertEqual(nodes["worklist"].count('aria-expanded="true"'), 1)
        self.assertIn('aria-controls="workdetail"', nodes["worklist"])
        # The detail region is labelled by the row it belongs to, and the row's
        # element id is derived from its opaque identity rather than its index.
        row_id = re.search(r'class="rowbtn" id="([^"]+)"', nodes["worklist"]).group(1)
        detail_key = re.search(r'id="workdetail" data-key="([^"]+)"', nodes["worklist"]).group(1)
        self.assertIn(
            f'id="workdetail" data-key="{detail_key}" role="region" aria-labelledby="{row_id}"',
            nodes["worklist"],
        )
        # The identity the detail is rendered for is the selected row's own
        # opaque key, which is what a refresh matches a preserved scroll
        # offset against.
        self.assertEqual(
            detail_key,
            re.search(r'class="rowbtn" id="[^"]+" data-key="([^"]+)"', nodes["worklist"]).group(1),
        )
        self.assertIn("runningwork", row_id)

    def test_keyboard_movement_rules_wrap_for_tabs_and_clamp_for_rows(self) -> None:
        moves = _eval_board_view(
            "["
            "nextTabIndex('ArrowRight', 3, 4), nextTabIndex('ArrowLeft', 0, 4),"
            "nextTabIndex('Home', 2, 4), nextTabIndex('End', 0, 4), nextTabIndex('a', 0, 4),"
            "nextRowIndex('ArrowDown', 2, 3), nextRowIndex('ArrowUp', 0, 3),"
            "nextRowIndex('Home', 2, 3), nextRowIndex('End', 0, 3), nextRowIndex('ArrowLeft', 0, 3),"
            "nextRowIndex('ArrowDown', 0, 0)"
            "]"
        )
        self.assertEqual(moves, [0, 3, 0, 3, -1, 2, 0, 0, 2, -1, -1])

    def test_row_order_is_urgency_and_never_headline_precedence(self) -> None:
        # Headline precedence is untouched: "merged" is still the truth that
        # describes a merged item best, and it still wins that contest outright.
        rules = _eval_board_view("STATE_RULES.map(rule => [rule.label, rule.rank])")
        self.assertEqual(rules[0], ["merged", 0])

        # Row order is a separate, explicit ranking. Every state the views
        # can produce is ranked in it -- the recorded states, plus the two the
        # idle and unlinked rows synthesise -- so no state orders by accident.
        ranked = _eval_board_view("[...ROW_URGENCY_ORDER, ...TERMINAL_ROW_HEADLINES]")
        for label in [rule[0] for rule in rules] + [
            "state not recorded",
            "idle with complete coverage",
        ]:
            self.assertIn(label, ranked)
        self.assertEqual(len(ranked), len(set(ranked)))

        # Terminal placement is explicit, not a consequence of falling off the
        # end: every named non-terminal state ranks below an unranked one,
        # which ranks below every terminal state, and no two share a rank.
        ranks = _eval_board_view(
            "[...ROW_URGENCY_ORDER, 'a state nobody ranked', ...TERMINAL_ROW_HEADLINES]"
            ".map(label => stateUrgency(label))"
        )
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(len(set(ranks)), len(ranks))
        # And the two rankings genuinely disagree about merged work.
        self.assertGreater(
            _eval_board_view("stateUrgency('merged')"),
            _eval_board_view("stateUrgency('CI pending')"),
        )

    def test_finished_work_never_outranks_actionable_or_blocked_work(self) -> None:
        records = [
            _observation_fixture("merged"),
            _observation_fixture("waiting_for_approval"),
            _observation_fixture("failed"),
            # The idle row belongs to a different session. A session-level
            # "nothing to do" snapshot is only ever truthful about a session
            # that has no work, and reconciliation drops one that sits beside
            # work in its own scope, so the terminal band is exercised here
            # with a board a producer could really record.
            _in_session(
                _observation_fixture("no_work"),
                session=OTHER_SESSION,
                worktree=OTHER_WORKTREE,
            ),
        ]
        payload = _observation_payload(records)
        worklist = _render_board_sequence([{"payload": payload}])[0]["worklist"]
        headlines = re.findall(
            r'aria-hidden="true">[^<]*</span> ([^<]+)</span><span class="pill">stage', worklist
        )
        # Blocked work first, then work waiting on a person, and the two
        # terminal rows last in their declared order.
        self.assertEqual(
            headlines,
            [
                "provider run failed",
                "waiting for approval",
                "merged",
                "idle with complete coverage",
            ],
        )

        keys = _work_keys(worklist)
        # An operator who has chosen nothing is shown blocked work, never the
        # merged item that used to win on headline precedence.
        self.assertEqual(_selected_key(worklist), keys[0])
        self.assertTrue(keys[0].endswith("failedwork"))

        # The order is a property of what the rows record, not of the order the
        # observation directory happened to list them in.
        reversed_payload = _observation_payload(list(reversed(records)))
        self.assertEqual(
            _render_board_sequence([{"payload": reversed_payload}])[0]["worklist"], worklist
        )

        # With only merged work on the board, the merged row is still selected:
        # terminal placement orders rows, it never hides them.
        merged_only = _render_board_sequence(
            [{"payload": _observation_payload([_observation_fixture("merged")])}]
        )[0]["worklist"]
        self.assertTrue(_selected_key(merged_only).endswith("mergedwork"))

        # Two rows that share a headline still order deterministically on the
        # reference and then the opaque identity.
        first = _observation_fixture("waiting_for_approval")
        second = copy.deepcopy(first)
        second["work"]["id"] = "approvaltwo"
        second["work"]["reference"] = "issue-947"
        for run in second["work"]["runs"]:
            run["binding"]["work_id"] = "approvaltwo"
        second = board_observation.validate(second)
        tied = _render_board_sequence([{"payload": _observation_payload([second, first])}])[0]
        self.assertEqual(
            re.findall(r'<span class="ref">([^<]+)</span>', tied["worklist"]),
            ["issue-946", "issue-947"],
        )

    # Every ordering case the row ranking has to get right, as the accepted
    # fixture the record is built from, the reference it is given, the reasons
    # the same record states alongside it, the headline the display rules must
    # still pick, and the recorded state the row must actually be ordered by.
    # The last two columns differ wherever a record states more than one thing
    # at once, which is exactly what ordering by the headline alone loses.
    URGENCY_MATRIX = (
        # A record can be ready to merge and still be waiting on a person, a
        # failure or an unreadable source. The headline reports the first of
        # those; the order has to report the rest.
        (
            "ready",
            "ready-unavailable",
            ["ready_to_merge", "source_unavailable"],
            "ready to merge",
            "source unavailable",
        ),
        (
            "ready",
            "ready-failed",
            ["ready_to_merge", "provider_failed"],
            "ready to merge",
            "provider run failed",
        ),
        (
            "ready",
            "ready-approval",
            ["ready_to_merge", "approval_required"],
            "ready to merge",
            "waiting for approval",
        ),
        # Weaker evidence never demotes the stronger action the same record
        # records: a stale observation of work that is ready to merge is still
        # ordered as work that is ready to merge.
        (
            "ready",
            "ready-stale",
            ["ready_to_merge", "stale_observation"],
            "ready to merge",
            "ready to merge",
        ),
        ("ready", "ready-plain", ["ready_to_merge"], "ready to merge", "ready to merge"),
        # Terminal work that owes nothing stays terminal, even though the same
        # record also states that the implementation is complete and the review
        # passed. Terminal work that owes something is ordered by what it owes.
        ("merged", "merged-alone", [], "merged", "merged"),
        (
            "merged",
            "merged-unavailable",
            ["source_unavailable"],
            "merged",
            "source unavailable",
        ),
        ("merged", "merged-failed", ["provider_failed"], "merged", "provider run failed"),
        (
            "merged",
            "merged-approval",
            ["approval_required"],
            "merged",
            "waiting for approval",
        ),
        # Work recorded as running owes nothing right now, so it sits between
        # everything that does and everything that is finished.
        ("observed_running", "running-plain", [], "provider run observed", "provider run observed"),
    )

    def _urgency_matrix_records(self) -> list[dict]:
        return [
            _record_with_reasons(fixture, reference, list(reasons))
            for fixture, reference, reasons, _, _ in self.URGENCY_MATRIX
        ]

    def test_row_urgency_is_computed_from_every_recorded_state(self) -> None:
        # The idle row is a different session's: a session-level snapshot is
        # only truthful about a session with no work of its own, so the one
        # that exercises the terminal band here is recorded against a session
        # the work rows do not belong to.
        records = self._urgency_matrix_records() + [
            _in_session(
                _observation_fixture("no_work"),
                session=OTHER_SESSION,
                worktree=OTHER_WORKTREE,
            )
        ]
        # The same board, read in three different input orders. Nothing about
        # the answer may depend on which order the directory was listed in.
        payloads = [
            _observation_payload(records),
            _observation_payload(list(reversed(records))),
            _observation_payload(records[3:] + records[:3]),
        ]
        # The order the states themselves were recorded in must not matter
        # either, so every permutation of a recorded state set is asserted.
        permutations = [
            ["merged", "waiting for approval", "review passed"],
            ["ready to merge", "source unavailable", "implementation complete"],
            ["merged", "implementation complete", "review passed"],
            ["merged", "stale observation"],
        ]
        permuted = [
            {"case": index, "states": list(order)}
            for index, labels in enumerate(permutations)
            for order in itertools.permutations(labels)
        ]
        result = _eval_board_view(
            "(() => {"
            " const [payloads, nowMs, permuted] = ARGS;"
            " return {"
            "  ranking: Object.fromEntries("
            "    [...ROW_URGENCY_ORDER, ...TERMINAL_ROW_HEADLINES].map(l => [l, stateUrgency(l)])),"
            "  boards: payloads.map(payload => {"
            "    const rows = workRows(payload, nowMs);"
            "    return {"
            "      references: rows.map(row => row.reference),"
            "      headlines: rows.map(row => row.headline),"
            "      urgency: rows.map(row => rowUrgency(row)),"
            "      selected: resolveSelection(rows, null),"
            "      keys: rows.map(row => row.key)"
            "    };"
            "  }),"
            "  permuted: permuted.map(item =>"
            "    rowUrgency({states: item.states.map(label => ({label}))}))"
            " };"
            "})()",
            payloads,
            int(OBSERVATION_NOW.timestamp() * 1000),
            permuted,
        )

        ranking = result["ranking"]
        expected_urgency = {
            reference: ranking[ordering_state]
            for _, reference, _, _, ordering_state in self.URGENCY_MATRIX
        }
        expected_headline = {
            reference: headline for _, reference, _, headline, _ in self.URGENCY_MATRIX
        }
        # Most urgent first, then the declared tiebreak on the reference. Every
        # row that states an outstanding blocker or action sorts above the
        # merged row that states nothing but its own completion, and the idle
        # row is last because it is genuinely terminal.
        expected_order = [
            "merged-unavailable",
            "ready-unavailable",
            "merged-failed",
            "ready-failed",
            "merged-approval",
            "ready-approval",
            "ready-plain",
            "ready-stale",
            "running-plain",
            "merged-alone",
            "Board contract delivery",
        ]
        for board_index, board_rows in enumerate(result["boards"]):
            with self.subTest(input_order=board_index):
                self.assertEqual(board_rows["references"], expected_order)
                # The display headline rules are untouched: a merged record
                # still reads as merged and a ready one still reads as ready to
                # merge, however they are ordered.
                headlines = dict(zip(board_rows["references"], board_rows["headlines"], strict=True))
                for reference, headline in expected_headline.items():
                    self.assertEqual(headlines[reference], headline)
                # And each row is ordered by the state it owes, not by the one
                # it reads as.
                urgency = dict(zip(board_rows["references"], board_rows["urgency"], strict=True))
                for reference, value in expected_urgency.items():
                    self.assertEqual(urgency[reference], value, reference)
                self.assertEqual(
                    board_rows["urgency"], sorted(board_rows["urgency"]), "rows are not sorted"
                )
                # An operator who has chosen nothing is shown the most urgent
                # row, so the default selection follows the same ranking.
                self.assertEqual(board_rows["selected"], board_rows["keys"][0])
                self.assertIn("merged-unavailable", board_rows["selected"])

        # Urgency is a function of the set of recorded states, not of the order
        # they arrived in: every permutation of one set answers identically.
        by_case: dict[int, set[int]] = {}
        for item, value in zip(permuted, result["permuted"], strict=True):
            by_case.setdefault(item["case"], set()).add(value)
        self.assertEqual(
            [sorted(values) for _, values in sorted(by_case.items())],
            [
                [ranking["waiting for approval"]],
                [ranking["source unavailable"]],
                [ranking["merged"]],
                [ranking["merged"]],
            ],
        )

    def test_the_default_selection_opens_on_work_that_still_owes_something(self) -> None:
        # The same ranking, proved through the rendered page rather than
        # through the model: the row the browser marks as selected is the
        # merged record that is also recorded as unreadable, not the merged
        # record that owes nothing.
        records = self._urgency_matrix_records()
        worklist = _render_board_sequence([{"payload": _observation_payload(records)}])[0][
            "worklist"
        ]
        references = re.findall(r'<span class="ref">([^<]+)</span>', worklist)
        self.assertEqual(references[0], "merged-unavailable")
        self.assertEqual(references[-1], "merged-alone")
        self.assertTrue(_selected_key(worklist).endswith("merged-unavailable"))
        self.assertEqual(_selected_key(worklist), _work_keys(worklist)[0])
        # The selected row still reads as merged: ordering it by what it owes
        # did not rewrite what it says.
        headlines = re.findall(
            r'aria-hidden="true">[^<]*</span> ([^<]+)</span><span class="pill">stage', worklist
        )
        self.assertEqual(
            dict(zip(references, headlines, strict=True)),
            {reference: headline for _, reference, _, headline, _ in self.URGENCY_MATRIX},
        )

    def test_headline_only_urgency_would_bury_work_that_still_owes_something(self) -> None:
        # The mutation this test exists to catch: ranking a row by its headline
        # alone, which is what the sort used to do. It is executed here against
        # the shipped model so the difference is demonstrated, not asserted.
        records = self._urgency_matrix_records() + [
            # A different session's idle snapshot, for the reason given above.
            _in_session(
                _observation_fixture("no_work"),
                session=OTHER_SESSION,
                worktree=OTHER_WORKTREE,
            )
        ]
        payload = _observation_payload(records)
        result = _eval_board_view(
            "(() => {"
            " const [payload, nowMs] = ARGS;"
            " const rows = workRows(payload, nowMs);"
            " const headlineOnly = [...rows].sort((a, b) =>"
            "   stateUrgency(a.headline) - stateUrgency(b.headline)"
            "   || a.reference.localeCompare(b.reference)"
            "   || a.key.localeCompare(b.key));"
            " return {"
            "  shipped: rows.map(row => row.reference),"
            "  headlineOnly: headlineOnly.map(row => row.reference),"
            "  shippedSelected: resolveSelection(rows, null),"
            "  headlineOnlySelected: resolveSelection(headlineOnly, null)"
            " };"
            "})()",
            payload,
            int(OBSERVATION_NOW.timestamp() * 1000),
        )
        self.assertNotEqual(result["shipped"], result["headlineOnly"])
        # Headline-only ordering buries every merged record -- including the
        # three that are still blocked or waiting on a person -- beneath work
        # that is merely in flight, and opens the Board on a ready-to-merge
        # item while an unreadable source goes unseen.
        self.assertEqual(
            result["headlineOnly"][:5],
            [
                "ready-approval",
                "ready-failed",
                "ready-plain",
                "ready-stale",
                "ready-unavailable",
            ],
        )
        self.assertEqual(
            result["headlineOnly"][-5:],
            [
                "merged-alone",
                "merged-approval",
                "merged-failed",
                "merged-unavailable",
                "Board contract delivery",
            ],
        )
        self.assertEqual(result["shipped"][0], "merged-unavailable")
        self.assertNotEqual(result["shippedSelected"], result["headlineOnlySelected"])

    # Every reason the frozen B0 contract accepts, with the state the Board
    # must display for it and the urgency band that state is ranked in. The
    # vocabulary is not restated here: the test asserts this table covers
    # exactly ``board_observation.REASON_ROUTES``, so a reason added to the
    # contract without a Board classification fails rather than silently
    # falling through to the neutral "state not recorded".
    REASON_CLASSIFICATION = {
        "approval_required": ("waiting for approval", "actionable"),
        "user_input_required": ("waiting for an answer", "actionable"),
        "source_unavailable": ("source unavailable", "blocked"),
        "identity_unlinked": ("identity unlinked", "untrusted"),
        "stale_observation": ("stale observation", "untrusted"),
        "provider_failed": ("provider run failed", "blocked"),
        "provider_suspended": ("provider run suspended", "blocked"),
        "cancelled": ("provider run cancelled", "blocked"),
        "changes_requested": ("changes requested", "blocked"),
        "update_required": ("branch update required", "blocked"),
        "ci_failed": ("CI failed", "blocked"),
        "gate_failed": ("gate failed", "blocked"),
        "review_stale": ("stale review", "untrusted"),
        "review_requested": ("review requested", "actionable"),
        "review_in_progress": ("review observed running", "in_flight"),
        "ci_pending": ("CI pending", "in_flight"),
        "gate_pending": ("gate pending", "in_flight"),
        "human_review_required": ("ready for human review", "actionable"),
        "ready_to_merge": ("ready to merge", "actionable"),
    }

    def test_every_reason_the_contract_accepts_is_displayed_and_ranked(self) -> None:
        # The vocabulary comes from the frozen contract, not from a subset
        # chosen by hand.
        reasons = sorted(board_observation.REASON_ROUTES)
        self.assertEqual(set(self.REASON_CLASSIFICATION), set(reasons))

        result = _eval_board_view(
            "(() => {"
            " const [reasons] = ARGS;"
            " const band = new Map(ROW_URGENCY_BANDS.flatMap("
            "   b => b.labels.map(label => [label, b.name])));"
            " const demanding = new Map(ROW_URGENCY_BANDS.flatMap("
            "   b => b.labels.map(label => [label, b.demanding])));"
            " return {"
            "  neutral: workStates({}).map(state => state.label),"
            "  unranked: UNRANKED_ROW_URGENCY,"
            "  reasons: Object.fromEntries(reasons.map(reason => {"
            "    const states = workStates({reasons: [reason]});"
            "    const labels = states.map(state => state.label);"
            "    return [reason, {"
            "      labels,"
            "      classes: states.map(state => state.class),"
            "      band: band.get(labels[0]) ?? null,"
            "      declaredDemanding: demanding.get(labels[0]) ?? null,"
            "      urgency: stateUrgency(labels[0]),"
            "      demanding: isDemandingState(labels[0])"
            "    }];"
            "  }))"
            " };"
            "})()",
            reasons,
        )

        # The neutral classification exists and is reachable: a record that
        # states nothing reads as nothing.
        self.assertEqual(result["neutral"], ["state not recorded"])
        for reason in reasons:
            with self.subTest(reason=reason):
                label, band = self.REASON_CLASSIFICATION[reason]
                observed = result["reasons"][reason]
                # One reason on its own produces exactly the one state it
                # names -- never the neutral fallback, and never a second
                # state the record did not record.
                self.assertEqual(observed["labels"], [label])
                self.assertIn(observed["classes"][0], {"ok", "warn", "bad", "muted"})
                # And that state is explicitly ranked, in the band this table
                # declares, rather than sorting as something nobody ranked.
                self.assertEqual(observed["band"], band)
                self.assertLess(observed["urgency"], result["unranked"])
                self.assertEqual(observed["demanding"], observed["declaredDemanding"])

        # Every reason whose contract route names an actor who has to act on a
        # blocked change is ranked as demanding, so none of them can sort below
        # work that is merely progressing.
        for reason in reasons:
            band = self.REASON_CLASSIFICATION[reason][1]
            self.assertEqual(
                result["reasons"][reason]["demanding"], band in {"blocked", "actionable"}, reason
            )

        # The rest of the closed vocabulary the same routes carry is covered
        # too, and again against the contract rather than a hand-picked list:
        # every actor and every next action a route can name has its own
        # phrasing, so no route can be displayed as an unlabelled token.
        labels = _eval_board_view("[Object.keys(ACTION_LABELS), Object.keys(ACTOR_LABELS)]")
        self.assertEqual(set(labels[0]), set(board_observation.ACTIONS))
        self.assertEqual(set(labels[1]), set(board_observation.ACTORS))

        # The two reasons this pass added are ranked with the blockers, and
        # both outrank ordinary progressing, CI and ready-to-merge work.
        ranking = {
            reason: result["reasons"][reason]["urgency"]
            for reason in ("provider_suspended", "update_required")
        }
        for ordinary in ("ci_pending", "gate_pending", "review_in_progress", "ready_to_merge"):
            for reason, urgency in ranking.items():
                self.assertLess(urgency, result["reasons"][ordinary]["urgency"], reason)

    def test_a_suspended_session_is_never_reported_as_a_failed_one(self) -> None:
        # The contract records a suspended session as the `suspended`
        # lifecycle state and only ever alongside the `failed` phase, so
        # reading the phase alone reports a paused session as a failure.
        suspended = _eval_board_view(
            "workStates(ARGS[0]).map(state => state.label)",
            {"runs": [{"phase": "failed", "lifecycle": {"state": "suspended"}}]},
        )
        self.assertEqual(suspended, ["provider run suspended"])
        # A run that really failed is still reported as one.
        failed = _eval_board_view(
            "workStates(ARGS[0]).map(state => state.label)",
            {"runs": [{"phase": "failed", "lifecycle": {"state": "failed"}}]},
        )
        self.assertEqual(failed, ["provider run failed"])
        # A run with no lifecycle recorded at all is read from its phase.
        bare = _eval_board_view(
            "workStates(ARGS[0]).map(state => state.label)", {"runs": [{"phase": "failed"}]}
        )
        self.assertEqual(bare, ["provider run failed"])
        # One work item with both records both, and reads as the failure --
        # which is also the precedence the contract's own route table gives
        # `provider_failed` over `provider_suspended`.
        both = _eval_board_view(
            "workStates(ARGS[0]).map(state => state.label)",
            {
                "runs": [
                    {"phase": "failed", "lifecycle": {"state": "failed"}},
                    {"phase": "failed", "lifecycle": {"state": "suspended"}},
                ]
            },
        )
        self.assertEqual(both, ["provider run failed", "provider run suspended"])
        self.assertLess(
            board_observation.REASON_ROUTES["provider_failed"][0],
            board_observation.REASON_ROUTES["provider_suspended"][0],
        )

        # Proved once more through a record the frozen contract accepts and
        # the page the browser is served.
        record = _record_with_suspended_run("suspended-session")
        worklist = _render_board_sequence(
            [{"payload": _observation_payload([record])}]
        )[0]["worklist"]
        self.assertIn("provider run suspended", worklist)
        self.assertNotIn("provider run failed", worklist)
        # And when the reason is recorded alongside the suspended lifecycle,
        # the row reports the contract's own route for it rather than the
        # failure route.
        routed = _record_with_suspended_run("suspended-routed", reasons=["provider_suspended"])
        rows = _eval_board_view(
            "workRows(ARGS[0], ARGS[1]).map(row => [row.headline, row.action_label, row.actor_label])",
            _observation_payload([routed]),
            int(OBSERVATION_NOW.timestamp() * 1000),
        )
        self.assertEqual(rows, [["provider run suspended", "inspect the provider", "orchestrator"]])

    # The two reasons that had no state rules at all, stated on their own and
    # alongside the states they used to be invisible next to.
    BLOCKER_MATRIX = (
        ("ready", "a-suspended-ready", ["ready_to_merge", "provider_suspended"],
         "ready to merge", "provider run suspended"),
        ("ready", "b-update-ready", ["ready_to_merge", "update_required"],
         "ready to merge", "branch update required"),
        ("merged", "c-suspended-merged", ["provider_suspended"],
         "merged", "provider run suspended"),
        ("merged", "d-update-merged", ["update_required"],
         "merged", "branch update required"),
        ("observed_running", "e-suspended-running", ["provider_suspended"],
         "provider run suspended", "provider run suspended"),
        ("observed_running", "f-update-running", ["update_required"],
         "branch update required", "branch update required"),
        # Ordinary work, which every row above has to outrank.
        ("observed_running", "g-running", [], "provider run observed", "provider run observed"),
        ("ready", "h-ci-pending", ["ci_pending"], "ready to merge", "ready to merge"),
        ("ready", "i-ready", ["ready_to_merge"], "ready to merge", "ready to merge"),
    )

    def test_the_two_unranked_blockers_outrank_ordinary_work_in_every_state_order(self) -> None:
        records = [
            _record_with_reasons(fixture, reference, list(reasons))
            for fixture, reference, reasons, _, _ in self.BLOCKER_MATRIX
        ]
        # The state order inside one record must not matter either, so every
        # permutation of each recorded state set is ranked.
        permutations = [
            ["ready to merge", "provider run suspended"],
            ["ready to merge", "branch update required"],
            ["merged", "provider run suspended"],
            ["merged", "branch update required", "review passed"],
            ["provider run suspended", "branch update required", "CI pending"],
        ]
        permuted = [
            {"case": index, "states": list(order)}
            for index, labels in enumerate(permutations)
            for order in itertools.permutations(labels)
        ]
        payloads = [
            _observation_payload(records),
            _observation_payload(list(reversed(records))),
            _observation_payload(records[4:] + records[:4]),
        ]
        result = _eval_board_view(
            "(() => {"
            " const [payloads, nowMs, permuted] = ARGS;"
            " return {"
            "  ranking: Object.fromEntries("
            "    [...ROW_URGENCY_ORDER, ...TERMINAL_ROW_HEADLINES].map(l => [l, stateUrgency(l)])),"
            "  boards: payloads.map(payload => {"
            "    const rows = workRows(payload, nowMs);"
            "    return {"
            "      references: rows.map(row => row.reference),"
            "      headlines: rows.map(row => row.headline),"
            "      urgency: rows.map(row => rowUrgency(row)),"
            "      selected: resolveSelection(rows, null),"
            "      keys: rows.map(row => row.key)"
            "    };"
            "  }),"
            "  permuted: permuted.map(item =>"
            "    rowUrgency({states: item.states.map(label => ({label}))}))"
            " };"
            "})()",
            payloads,
            int(OBSERVATION_NOW.timestamp() * 1000),
            permuted,
        )

        ranking = result["ranking"]
        expected = {
            reference: (ranking[ordering], headline)
            for _, reference, _, headline, ordering in self.BLOCKER_MATRIX
        }
        blockers = [reference for reference in expected if reference[0] in "abcdef"]
        ordinary = [reference for reference in expected if reference[0] in "ghi"]
        for index, board_rows in enumerate(result["boards"]):
            with self.subTest(input_order=index):
                urgency = dict(zip(board_rows["references"], board_rows["urgency"], strict=True))
                headlines = dict(
                    zip(board_rows["references"], board_rows["headlines"], strict=True)
                )
                for reference, (rank, headline) in expected.items():
                    # Ordering reports what the row owes; the headline keeps
                    # reporting the truth that describes it best. The two
                    # responsibilities stay distinct.
                    self.assertEqual(urgency[reference], rank, reference)
                    self.assertEqual(headlines[reference], headline, reference)
                self.assertEqual(board_rows["urgency"], sorted(board_rows["urgency"]))
                # Every row carrying one of the two blockers sorts above every
                # ordinary progressing, CI-pending or ready-to-merge row.
                self.assertLess(
                    max(urgency[reference] for reference in blockers),
                    min(urgency[reference] for reference in ordinary),
                )
                # An operator who has chosen nothing opens on a blocker.
                self.assertEqual(board_rows["selected"], board_rows["keys"][0])
                # "branch update required" is the most urgent state on this
                # board, so its row is the one the Board opens on.
                self.assertEqual(board_rows["references"][0], "b-update-ready")

        # The ranking of one recorded state set never depends on the order the
        # states were recorded in.
        by_case: dict[int, set[int]] = {}
        for item, value in zip(permuted, result["permuted"], strict=True):
            by_case.setdefault(item["case"], set()).add(value)
        self.assertEqual(
            [sorted(values) for _, values in sorted(by_case.items())],
            [
                [ranking["provider run suspended"]],
                [ranking["branch update required"]],
                [ranking["provider run suspended"]],
                [ranking["branch update required"]],
                [ranking["branch update required"]],
            ],
        )

    def test_the_blocked_rows_are_selected_by_the_rendered_page(self) -> None:
        # The same ranking, through the page the browser is served rather than
        # through the model.
        records = [
            _record_with_reasons(fixture, reference, list(reasons))
            for fixture, reference, reasons, _, _ in self.BLOCKER_MATRIX
        ]
        worklist = _render_board_sequence([{"payload": _observation_payload(records)}])[0][
            "worklist"
        ]
        references = re.findall(r'<span class="ref">([^<]+)</span>', worklist)
        headlines = re.findall(
            r'aria-hidden="true">[^<]*</span> ([^<]+)</span><span class="pill">stage', worklist
        )
        # The six rows carrying a blocker come first, in any order among
        # themselves, and the three ordinary rows follow.
        self.assertEqual(
            sorted(references[:6]), sorted(r for r in references if r[0] in "abcdef")
        )
        self.assertEqual(sorted(references[6:]), sorted(r for r in references if r[0] in "ghi"))
        self.assertTrue(_selected_key(worklist).endswith("b-update-ready"))
        self.assertEqual(
            dict(zip(references, headlines, strict=True)),
            {reference: headline for _, reference, _, headline, _ in self.BLOCKER_MATRIX},
        )
        # Both new states are shown with a text cue as well as a colour, like
        # every other state the Board reports.
        for label in ("provider run suspended", "branch update required"):
            self.assertIn(f'aria-hidden="true">~</span> {label}</span>', worklist)

    # Room for the detail region to scroll: 900px of evidence in a 300px
    # panel, so 600px of travel.
    DETAIL_METRICS = {"workdetail": {"scrollHeight": 900, "clientHeight": 300}}

    def _scroll_case(self) -> tuple[dict, dict, str]:
        """One payload, the same payload with changed evidence, and a key."""

        first = _record_with_reasons("ready", "alpha", ["ready_to_merge"])
        second = _record_with_reasons("observed_running", "beta", [])
        changed = _record_with_reasons("ready", "alpha", ["ready_to_merge", "ci_pending"])
        payload = _observation_payload([first, second])
        refreshed = _observation_payload([changed, second])
        worklist = _render_board_focus([{"payload": payload}])[0]["worklist"]
        key = next(item for item in _work_keys(worklist) if item.endswith("alpha"))
        return payload, refreshed, key

    def test_the_selected_detail_keeps_its_reading_position_across_a_refresh(self) -> None:
        payload, refreshed, key = self._scroll_case()
        steps = [
            {"payload": payload},
            {"select": key, "metrics": self.DETAIL_METRICS},
            {"scroll": {"top": 240}},
        ]
        # A poll that observed nothing new must not move the panel at all.
        unchanged = _render_board_focus([*steps, {"payload": payload}])
        self.assertEqual(unchanged[-2]["detail"], {"key": key, "top": 240})
        self.assertEqual(unchanged[-1]["detail"], {"key": key, "top": 240})

        # Neither must a poll that changed the evidence of the very work item
        # being read: the identity survived, so the reading position does too.
        changed = _render_board_focus([*steps, {"payload": refreshed}])
        self.assertEqual(changed[-1]["detail"], {"key": key, "top": 240})
        self.assertIn("CI pending", changed[-1]["worklist"])
        self.assertNotIn("CI pending", unchanged[-1]["worklist"])

        # Without the offset being carried across, the replacement starts at
        # the top -- which is the reset this test exists to catch.
        self.assertEqual(
            _render_board_focus(
                [{"payload": payload}, {"select": key, "metrics": self.DETAIL_METRICS}]
            )[-1]["detail"],
            {"key": key, "top": 0},
        )

    def test_a_refresh_that_shortens_the_detail_clamps_the_restored_position(self) -> None:
        payload, refreshed, key = self._scroll_case()
        opened = [
            {"payload": payload},
            {"select": key, "metrics": self.DETAIL_METRICS},
            {"scroll": {"top": 560}},
        ]
        # Evidence that shrinks to 400px in the same 300px panel can only
        # scroll 100px, so the restored position is the end of what is now
        # there rather than an offset that no longer exists.
        shrunk = _render_board_focus(
            [
                *opened,
                {
                    "metrics": {"workdetail": {"scrollHeight": 400, "clientHeight": 300}},
                    "payload": refreshed,
                },
            ]
        )
        self.assertEqual(shrunk[-2]["detail"], {"key": key, "top": 560})
        self.assertEqual(shrunk[-1]["detail"], {"key": key, "top": 100})

        # Evidence that no longer overflows at all cannot scroll, and the
        # panel is left at the top rather than at a negative offset.
        flattened = _render_board_focus(
            [
                *opened,
                {
                    "metrics": {"workdetail": {"scrollHeight": 200, "clientHeight": 300}},
                    "payload": refreshed,
                },
            ]
        )
        self.assertEqual(flattened[-1]["detail"], {"key": key, "top": 0})

    def test_a_scroll_position_is_never_inherited_by_a_different_work_item(self) -> None:
        payload, refreshed, key = self._scroll_case()
        worklist = _render_board_focus([{"payload": payload}])[0]["worklist"]
        other = next(item for item in _work_keys(worklist) if item != key)
        opened = [
            {"payload": payload},
            {"select": key, "metrics": self.DETAIL_METRICS},
            {"scroll": {"top": 240}},
        ]

        # Choosing another work item opens its evidence at the top. Its
        # detail is a different identity, so it inherits nothing.
        moved = _render_board_focus([*opened, {"select": other}])
        self.assertEqual(moved[-1]["detail"], {"key": other, "top": 0})
        # Coming back returns to where this identity was being read. The
        # offsets are kept per identity, so the two never mix: the second work
        # item's own position is its own, and it is the one restored when it
        # is the one on screen.
        returned = _render_board_focus(
            [
                *opened,
                {"select": other},
                {"scroll": {"top": 90}},
                {"select": key},
                {"select": other},
            ]
        )
        self.assertEqual(returned[-3]["detail"], {"key": other, "top": 90})
        self.assertEqual(returned[-2]["detail"], {"key": key, "top": 240})
        self.assertEqual(returned[-1]["detail"], {"key": other, "top": 90})

        # A refresh that drops the selected record entirely selects another
        # row, which also starts at the top.
        without = _observation_payload([_record_with_reasons("observed_running", "beta", [])])
        dropped = _render_board_focus([*opened, {"payload": without}])
        self.assertEqual(dropped[-1]["detail"]["top"], 0)
        self.assertNotEqual(dropped[-1]["detail"]["key"], key)

        # A refresh with nothing to show at all removes the detail region.
        # Restoring has nowhere to land and does not fail trying.
        emptied = _render_board_focus([*opened, {"payload": _observation_payload([])}])
        self.assertIsNone(emptied[-1]["detail"])
        self.assertIn("No local Board observation", emptied[-1]["worklist"])

    # Reading evidence, then going to look at something else, is the ordinary
    # thing to do with a tabbed page. While another view is open the Now panel
    # is hidden: its content has no box, so every layout metric reads zero.
    # These are the cases where a position read off -- or clamped against -- a
    # hidden panel silently becomes a return to the top.
    def _reading(self, key: str) -> list[dict[str, object]]:
        """An operator part way down one work item's evidence, on Now."""

        return [
            {"select": key, "metrics": self.DETAIL_METRICS},
            {"scroll": {"top": 240}},
        ]

    def test_a_poll_that_lands_while_now_is_hidden_keeps_the_reading_position(self) -> None:
        payload, refreshed, key = self._scroll_case()
        frames = _render_board_focus(
            [
                {"payload": payload},
                *self._reading(key),
                {"click": "tab-timeline", "on": "tabs"},
                {"payload": payload},
                {"click": "tab-now", "on": "tabs"},
            ]
        )
        # Now really is hidden while the poll lands, and the replacement the
        # poll rendered into the hidden panel starts at the top with no
        # measurable height at all -- there is nothing there to read.
        self.assertTrue(frames[-2]["hidden"]["now"])
        self.assertFalse(frames[-1]["hidden"]["now"])
        self.assertEqual(frames[-2]["detail"], {"key": key, "top": 0})
        # What the operator was reading was never in that element: it is kept
        # against the identity, outside everything the poll replaced.
        self.assertEqual(frames[-2]["remembered"], {key: 240})
        # Returning to Now is the first moment the panel can be measured
        # again, and it is where the reading position comes back.
        self.assertEqual(frames[-1]["detail"], {"key": key, "top": 240})

        # The keyboard takes the same route through the tab strip, and the two
        # pieces of state compose: the keyboard is left on the tab the
        # operator moved to, and the evidence is where they left it.
        keyboard = _render_board_focus(
            [
                {"payload": payload},
                *self._reading(key),
                {"focus": "tab-now"},
                {"key": "ArrowRight", "on": "tabs", "from": "tab-now"},
                {"payload": refreshed},
                {"key": "ArrowLeft", "on": "tabs", "from": "tab-timeline"},
            ]
        )
        self.assertEqual(keyboard[-3]["active"], "tab-timeline")
        self.assertTrue(keyboard[-2]["hidden"]["now"])
        self.assertEqual(keyboard[-1]["active"], "tab-now")
        self.assertEqual(keyboard[-1]["detail"], {"key": key, "top": 240})

    def test_many_hidden_polls_and_changed_evidence_lose_nothing(self) -> None:
        payload, refreshed, key = self._scroll_case()
        away = [
            {"payload": payload},
            *self._reading(key),
            {"click": "tab-health", "on": "tabs"},
        ]
        # Eight polls land while the operator is on another view, two of them
        # changing the evidence of the very work item being read. Each one
        # replaces the hidden detail region; none of them may touch what is
        # remembered for it.
        polls: list[dict[str, object]] = []
        for index in range(8):
            polls.append({"payload": refreshed if index % 4 == 3 else payload})
        frames = _render_board_focus([*away, *polls, {"click": "tab-now", "on": "tabs"}])
        for frame in frames[len(away) : -1]:
            self.assertTrue(frame["hidden"]["now"])
            self.assertEqual(frame["remembered"], {key: 240})
        self.assertEqual(frames[-1]["detail"], {"key": key, "top": 240})
        self.assertIn("CI pending", frames[-1]["worklist"])

        # Switching view twice over, with polls on both sides, is the same
        # story: the position belongs to the identity, not to a visit.
        returning = _render_board_focus(
            [
                *away,
                {"payload": refreshed},
                {"click": "tab-now", "on": "tabs"},
                {"payload": payload},
                {"click": "tab-releases", "on": "tabs"},
                {"payload": refreshed},
                {"click": "tab-now", "on": "tabs"},
            ]
        )
        self.assertEqual(returning[-1]["detail"], {"key": key, "top": 240})

    def test_a_hidden_panel_is_never_read_as_a_reading_position_of_zero(self) -> None:
        payload, refreshed, key = self._scroll_case()
        # Hidden content has no box: the shim reports what a browser reports,
        # so a page that reads the offset out of the element, or clamps
        # against its travel, sees zero and throws the reading away.
        hidden = _render_board_focus(
            [
                {"payload": payload},
                *self._reading(key),
                {"click": "tab-timeline", "on": "tabs"},
                {"payload": refreshed},
            ]
        )[-1]
        self.assertEqual(hidden["detail"], {"key": key, "top": 0})
        self.assertEqual(hidden["remembered"], {key: 240})

        # Zero metrics declared outright -- a panel the browser has not laid
        # out yet -- are read the same way: not a position, so nothing is
        # captured from it and nothing is clamped against it.
        unlaid = _render_board_focus(
            [
                {"payload": payload},
                *self._reading(key),
                {"metrics": {"workdetail": {"scrollHeight": 0, "clientHeight": 0}}},
                {"payload": refreshed},
                {"metrics": self.DETAIL_METRICS},
                {"payload": refreshed},
            ]
        )
        self.assertEqual(unlaid[-2]["remembered"], {key: 240})
        self.assertEqual(unlaid[-1]["detail"], {"key": key, "top": 240})

        # All of which rests on one property of the shipped stylesheet: a
        # hidden view is taken out of layout rather than merely made
        # invisible, so its content genuinely has no box to measure. The rule
        # is read here rather than assumed, because a stylesheet that hid a
        # panel some other way would leave these metrics reporting a box for
        # evidence nobody can see.
        hiding = [
            declarations
            for at_rule, selector, declarations in _css_rules(_board_css())
            if selector == "[hidden]" and not at_rule
        ]
        self.assertEqual([entry.get("display") for entry in hiding], ["none !important"])

    def test_returning_to_now_clamps_against_what_is_there_on_return(self) -> None:
        payload, refreshed, key = self._scroll_case()
        away = [
            {"payload": payload},
            {"select": key, "metrics": self.DETAIL_METRICS},
            {"scroll": {"top": 560}},
            {"click": "tab-timeline", "on": "tabs"},
            {"payload": refreshed},
        ]
        # Evidence that shrank while the operator was away can only scroll
        # 100px, and the clamp happens on return -- the one moment the panel
        # can be measured -- rather than against the zeros it reported while
        # hidden. What is remembered is then what is on screen.
        shrunk = _render_board_focus(
            [
                *away,
                {"metrics": {"workdetail": {"scrollHeight": 400, "clientHeight": 300}}},
                {"click": "tab-now", "on": "tabs"},
            ]
        )
        self.assertEqual(shrunk[-1]["detail"], {"key": key, "top": 100})
        self.assertEqual(shrunk[-1]["remembered"], {key: 100})

        # Evidence that grew keeps the place that was being read: there is
        # more below it, not less.
        grown = _render_board_focus(
            [
                *away,
                {"metrics": {"workdetail": {"scrollHeight": 1500, "clientHeight": 300}}},
                {"click": "tab-now", "on": "tabs"},
            ]
        )
        self.assertEqual(grown[-1]["detail"], {"key": key, "top": 560})

        # And growth while Now is open keeps it too, poll after poll.
        visible = _render_board_focus(
            [
                {"payload": payload},
                *self._reading(key),
                {"metrics": {"workdetail": {"scrollHeight": 1500, "clientHeight": 300}}},
                {"payload": refreshed},
                {"payload": payload},
            ]
        )
        self.assertEqual(visible[-1]["detail"], {"key": key, "top": 240})

    def test_remembered_reading_positions_are_dropped_with_the_work_they_belong_to(self) -> None:
        payload, refreshed, key = self._scroll_case()
        worklist = _render_board_focus([{"payload": payload}])[0]["worklist"]
        other = next(item for item in _work_keys(worklist) if item != key)
        without = _observation_payload([_record_with_reasons("observed_running", "beta", [])])

        # A work item the Board stops showing has no evidence to come back to,
        # so what was remembered for it goes with it -- while the offset of
        # the item still on screen is left exactly where it was.
        frames = _render_board_focus(
            [
                {"payload": payload},
                *self._reading(key),
                {"select": other},
                {"scroll": {"top": 90}},
                {"payload": without},
                {"payload": payload},
                {"select": key},
            ]
        )
        self.assertEqual(frames[-3]["remembered"], {other: 90})
        # The identity is rendered again later, but it comes back as new work
        # rather than resuming a position from before it disappeared.
        self.assertEqual(frames[-1]["detail"], {"key": key, "top": 0})

        # Polls alone never accumulate anything: an unchanged poll remembers
        # what the operator did, and nothing else.
        repeated = _render_board_focus(
            [{"payload": payload}, *self._reading(key), *([{"payload": payload}] * 6)]
        )
        self.assertEqual(repeated[-1]["remembered"], {key: 240})

    def test_the_remembered_reading_positions_are_bounded(self) -> None:
        # The page is left open for days. Even a stream of identities that
        # were never on screen together cannot grow the map without limit:
        # it is bounded, and the least recently touched entry is the one that
        # goes. The bound is read off the page rather than assumed here.
        limit = _eval_board_page("DETAIL_OFFSET_LIMIT")
        self.assertIsInstance(limit, int)
        self.assertGreater(limit, 1)
        measured = _eval_board_page(
            "(() => {"
            "  for (let index = 0; index < ARGS[0]; index += 1) rememberDetailOffset('k' + index, index);"
            "  const keys = [...detailOffsets.keys()];"
            "  rememberDetailOffset(keys[0], 7);"
            "  rememberDetailOffset('fresh', 9);"
            "  return {"
            "    size: detailOffsets.size,"
            "    first: keys[0],"
            "    kept: detailOffsets.get(keys[0]),"
            "    oldest: [...detailOffsets.keys()][0],"
            "    newest: detailOffsets.get('fresh'),"
            "    dropped: detailOffsets.has('k0')"
            "  };"
            "})()",
            2000,
        )
        self.assertEqual(measured["size"], limit)
        # Every identity beyond the bound displaced an older one, touching an
        # entry keeps it, and the entry evicted for the newest arrival is the
        # one that had gone longest without being touched.
        self.assertEqual(measured["kept"], 7)
        self.assertNotEqual(measured["oldest"], measured["first"])
        self.assertEqual(measured["newest"], 9)
        self.assertFalse(measured["dropped"])

    def test_keyboard_focus_and_the_reading_position_survive_one_refresh_together(self) -> None:
        payload, refreshed, key = self._scroll_case()
        worklist = _render_board_focus([{"payload": payload}])[0]["worklist"]
        row_id = _row_element_id(worklist, key)
        opened = _render_board_focus(
            [{"payload": payload}, {"select": key, "metrics": self.DETAIL_METRICS}]
        )[-1]["worklist"]
        action = re.search(r'id="(workaction-inspect-[^"]+)"', opened).group(1)

        # The keyboard is on an action inside the panel and the panel is
        # scrolled. One refresh has to carry both.
        both = _render_board_focus(
            [
                {"payload": payload},
                {"select": key, "metrics": self.DETAIL_METRICS},
                {"focus": action},
                {"scroll": {"top": 240}},
                {"payload": refreshed},
            ]
        )
        self.assertEqual(both[-1]["active"], action)
        self.assertEqual(both[-1]["detail"], {"key": key, "top": 240})

        # When the action the keyboard was on stops being offered, focus falls
        # back to the row it belonged to -- and the reading position is still
        # kept, because the identity being read did not change.
        self.assertEqual(
            _render_board_focus(
                [
                    {"payload": payload},
                    {"select": key, "metrics": self.DETAIL_METRICS},
                    {"focus": row_id},
                    {"scroll": {"top": 240}},
                    {"payload": refreshed},
                ]
            )[-1],
            {**both[-1], "active": row_id, "worklist": both[-1]["worklist"]},
        )

    # Every piece of ephemeral UI state that lives inside the subtree
    # `put("worklist", ...)` replaces on every poll: state the operator
    # created that the payload does not contain and a re-render therefore
    # cannot reconstruct. Each is named with where the page keeps it and what
    # is proved about it, so nothing in this subtree is handled by accident.
    #
    #   selection             kept outside the subtree, in `selectedWorkKey`,
    #                         as the opaque identity, so a refresh that
    #                         reorders, adds or drops rows keeps the choice;
    #   keyboard focus        read off the element about to be destroyed and
    #                         restored by id, with the row named as the
    #                         fallback when the control is not offered again;
    #   detail scroll offset  kept outside the subtree too, in `detailOffsets`,
    #                         against the same opaque identity; captured when
    #                         the operator scrolls and before the panel is
    #                         replaced or hidden, and restored -- clamped to
    #                         what is there to scroll -- only while that
    #                         identity's detail is visible and measurable.

    def test_every_ephemeral_state_the_work_list_replaces_is_accounted_for(self) -> None:
        payload, refreshed, key = self._scroll_case()
        opened = _render_board_focus(
            [{"payload": payload}, {"select": key, "metrics": self.DETAIL_METRICS}]
        )[-1]["worklist"]

        # The subtree holds nothing that carries state of its own beyond the
        # three named above: no field with a value, no disclosure with an open
        # state, no editable region. Anything the payload does not describe is
        # therefore one of the three.
        for tag in ("<input", "<textarea", "<select", "<details", "<summary", "contenteditable"):
            self.assertNotIn(tag, opened)

        # And exactly one element inside it scrolls independently of the page,
        # which is the one the offset is kept for. The stylesheet is read for
        # this rather than assumed.
        scrollers = sorted(
            {
                selector
                for _, selector, declarations in _css_rules(_board_css())
                if declarations.get("overflow") in {"auto", "scroll"}
            }
        )
        self.assertEqual(scrollers, [".workdetail"])
        self.assertEqual(opened.count('class="workdetail"'), 1)

        # Each of the three, through one lifecycle: preserved when the
        # identity survives, and reset deliberately when it does not.
        row_id = _row_element_id(opened, key)
        survived = _render_board_focus(
            [
                {"payload": payload},
                {"select": key, "metrics": self.DETAIL_METRICS},
                {"focus": row_id},
                {"scroll": {"top": 240}},
                {"payload": refreshed},
            ]
        )[-1]
        self.assertEqual(_selected_key(survived["worklist"]), key)
        self.assertEqual(survived["active"], row_id)
        self.assertEqual(survived["detail"], {"key": key, "top": 240})

        gone = _render_board_focus(
            [
                {"payload": payload},
                {"select": key, "metrics": self.DETAIL_METRICS},
                {"focus": row_id},
                {"scroll": {"top": 240}},
                {"payload": _observation_payload([_record_with_reasons("observed_running", "beta", [])])},
            ]
        )[-1]
        self.assertNotEqual(_selected_key(gone["worklist"]), key)
        # The row the keyboard was on is gone, so focus is left where the
        # browser put it rather than moved to an unrelated control, and the
        # reading position starts again with the work item now shown.
        self.assertEqual(gone["active"], "")
        self.assertEqual(gone["detail"]["top"], 0)

    def test_detail_actions_keep_keyboard_focus_across_a_refresh(self) -> None:
        payload = _observation_payload([_observation_fixture("ready")])
        payload["remote"]["pull_requests"] = [
            {
                "number": 946,
                "url": "https://github.example/codemower-ai/code-mower/pull/946",
                "title": "t",
                "labels": {},
                "checks": [],
            }
        ]
        opened = _render_board_focus([{"payload": payload}])[0]
        worklist = opened["worklist"]
        key = _work_keys(worklist)[0]
        row_id = _row_element_id(worklist, key)
        actions = re.findall(r'id="(workaction-[^"]+)"', worklist)
        # Every focusable action in the detail region carries an identity, and
        # no identity is shared with another action or with the row button.
        self.assertEqual(len(actions), 3)
        self.assertEqual(sorted(name.split("-")[1] for name in actions), ["changes", "inspect", "openpr"])
        self.assertEqual(len(set(actions + [row_id])), 4)
        self.assertNotIn(row_id, actions)

        # Focus each action in turn, then let an unchanged poll replace the row
        # list under it. Focus must come back to the same action, not to the
        # document body.
        steps: list[dict[str, object]] = [{"payload": payload}]
        for action in actions + [row_id, "tab-now"]:
            steps.append({"focus": action})
            steps.append({"payload": payload})
        frames = _render_board_focus(steps)
        restored = [frames[index]["active"] for index in range(2, len(frames), 2)]
        self.assertEqual(restored, actions + [row_id, "tab-now"])
        self.assertNotIn("", restored)

        # A poll that changes the row still keeps the keyboard on the action,
        # because the action's identity is the work's, not the render's.
        moved = copy.deepcopy(payload)
        moved["observations"]["records"][0]["work"]["stage"] = "in_review"
        after_change = _render_board_focus(
            [{"payload": payload}, {"focus": actions[0]}, {"payload": moved}]
        )
        self.assertIn("stage: in review", after_change[-1]["worklist"])
        self.assertEqual(after_change[-1]["active"], actions[0])

    def test_a_detail_action_that_disappears_never_hands_focus_to_another_control(self) -> None:
        payload = _observation_payload([_observation_fixture("ready")])
        payload["remote"]["pull_requests"] = [
            {
                "number": 946,
                "url": "https://github.example/codemower-ai/code-mower/pull/946",
                "title": "t",
                "labels": {},
                "checks": [],
            }
        ]
        worklist = _render_board_focus([{"payload": payload}])[0]["worklist"]
        row_id = _row_element_id(worklist, _work_keys(worklist)[0])
        open_pr = next(name for name in re.findall(r'id="(workaction-[^"]+)"', worklist) if "openpr" in name)

        # The recorded PR link stops being recorded, so the action it backed is
        # no longer offered. Focus lands on the row that action belonged to --
        # the one control it named -- and never on whichever action now happens
        # to sit in its place.
        without_link = copy.deepcopy(payload)
        without_link["remote"]["pull_requests"] = []
        gone = _render_board_focus(
            [{"payload": payload}, {"focus": open_pr}, {"payload": without_link}]
        )[-1]
        self.assertNotIn(open_pr, gone["worklist"])
        self.assertEqual(gone["active"], row_id)

        # The whole work item disappears and a different one takes the first
        # row. Nothing is focused at all: the Board does not move the keyboard
        # onto an unrelated work item's controls.
        replaced = _observation_payload([_observation_fixture("observed_running")])
        dropped = _render_board_focus(
            [{"payload": payload}, {"focus": open_pr}, {"payload": replaced}]
        )[-1]
        self.assertIn("runningwork", dropped["worklist"])
        self.assertNotIn(row_id, dropped["worklist"])
        self.assertEqual(dropped["active"], "")

        # Same when nothing at all is left to render.
        emptied = _render_board_focus(
            [{"payload": payload}, {"focus": open_pr}, {"payload": _observation_payload([])}]
        )[-1]
        self.assertEqual(emptied["active"], "")

    def test_opening_a_view_from_a_detail_action_moves_focus_out_of_the_hidden_panel(self) -> None:
        payload = _observation_payload([_observation_fixture("ready")])
        worklist = _render_board_focus([{"payload": payload}])[0]["worklist"]
        actions = {
            name.split("-")[1]: name for name in re.findall(r'id="(workaction-[^"]+)"', worklist)
        }
        # Both view-switching actions live in the Now panel, which the switch
        # itself hides. Focus must end on the tab for the view that was opened
        # rather than inside hidden content or back at the document body.
        for name, view in (("inspect", "health"), ("changes", "timeline")):
            frame = _render_board_focus(
                [{"payload": payload}, {"focus": actions[name]}, {"click": actions[name]}]
            )[-1]
            self.assertTrue(frame["hidden"]["now"])
            self.assertFalse(frame["hidden"][view])
            self.assertEqual(frame["active"], f"tab-{view}")
            self.assertIn(f'id="tab-{view}" data-view="{view}" aria-selected="true"', frame["tabs"])

        # Choosing a row is not a view switch, so it leaves the view alone and
        # the keyboard on the row.
        row_id = _row_element_id(worklist, _work_keys(worklist)[0])
        chosen = _render_board_focus(
            [{"payload": payload}, {"focus": row_id}, {"click": row_id}]
        )[-1]
        self.assertFalse(chosen["hidden"]["now"])
        self.assertEqual(chosen["active"], row_id)

    def test_keyboard_navigation_moves_selection_and_focus_through_rows_and_tabs(self) -> None:
        payload = _observation_payload(
            [_observation_fixture("failed"), _observation_fixture("merged")]
        )
        worklist = _render_board_focus([{"payload": payload}])[0]["worklist"]
        keys = _work_keys(worklist)
        rows = [_row_element_id(worklist, key) for key in keys]
        self.assertEqual(len(rows), 2)

        def press(key: str, start: str, **extra: object) -> dict[str, str]:
            steps: list[dict[str, object]] = [
                {"payload": payload},
                {"focus": start},
                {"key": key, "from": start, **extra},
            ]
            return _render_board_focus(steps)[-1]

        # Down selects and focuses the next row; Up comes back; End and Home
        # jump to the ends; Down on the last row clamps rather than wrapping.
        moved = press("ArrowDown", rows[0])
        self.assertEqual(moved["active"], rows[1])
        self.assertEqual(_selected_key(moved["worklist"]), keys[1])
        self.assertEqual(press("ArrowUp", rows[1])["active"], rows[0])
        self.assertEqual(press("End", rows[0])["active"], rows[1])
        self.assertEqual(press("Home", rows[1])["active"], rows[0])
        clamped = press("ArrowDown", rows[1])
        self.assertEqual(clamped["active"], rows[1])
        self.assertEqual(_selected_key(clamped["worklist"]), keys[1])

        # A key the row list does not handle leaves selection and focus alone.
        ignored = press("ArrowLeft", rows[0])
        self.assertEqual(ignored["active"], rows[0])
        self.assertEqual(_selected_key(ignored["worklist"]), keys[0])

        # Arrow keys pressed on a detail action are not row movement: the
        # action keeps the keyboard and the selection does not move.
        action = next(
            name for name in re.findall(r'id="(workaction-[^"]+)"', worklist) if "inspect" in name
        )
        on_action = press("ArrowDown", action)
        self.assertEqual(on_action["active"], action)
        self.assertEqual(_selected_key(on_action["worklist"]), keys[0])

        # Tabs wrap, and the roving tabindex follows the focused tab.
        forward = press("ArrowRight", "tab-now", on="tabs")
        self.assertEqual(forward["active"], "tab-timeline")
        self.assertFalse(forward["hidden"]["timeline"])
        self.assertTrue(forward["hidden"]["now"])
        self.assertIn('id="tab-timeline" data-view="timeline" aria-selected="true"', forward["tabs"])
        self.assertEqual(forward["tabs"].count('tabindex="0"'), 1)
        wrapped = press("ArrowLeft", "tab-now", on="tabs")
        self.assertEqual(wrapped["active"], "tab-health")
        self.assertFalse(wrapped["hidden"]["health"])

    def test_work_row_carries_reference_stage_assignment_update_action_and_role(self) -> None:
        nodes = _render_board_sequence(
            [{"payload": _observation_payload([_observation_fixture("observed_running")])}]
        )[0]
        row = nodes["worklist"]
        self.assertIn('<span class="ref">issue-946</span>', row)
        self.assertIn("stage: building", row)
        self.assertIn("assignments: codex builder observed running", row)
        self.assertIn("last update: 50s ago", row)
        self.assertIn("responsible: no responsible role recorded", row)
        self.assertIn("next: <b>no next action recorded</b>", row)

        reviewed = _render_board_sequence(
            [{"payload": _observation_payload([_observation_fixture("reviewed")])}]
        )[0]["worklist"]
        self.assertIn("next: <b>review the change</b>", reviewed)
        self.assertIn("responsible: owner", reviewed)

    def test_lifecycle_states_remain_distinct(self) -> None:
        expected = {
            "review_requested": ("implementation_complete", "review requested"),
            "review_running": ("observed_running", "provider run observed"),
            "stale_review": ("stale", "stale observation"),
            "changes_requested": ("cancelled", "provider run cancelled"),
            "implementation_complete": ("implementation_complete", "implementation complete"),
            "human_review": ("reviewed", "ready for human review"),
            "ready": ("ready", "ready to merge"),
            "merged": ("merged", "merged"),
        }
        headlines = {}
        for label, (fixture_name, _state) in expected.items():
            states = _eval_board_view("workStates(ARGS[0].work)", _observation_fixture(fixture_name))
            headlines[label] = [state["label"] for state in states]

        self.assertEqual(headlines["ready"][0], "ready to merge")
        self.assertEqual(headlines["merged"][0], "merged")
        self.assertEqual(headlines["human_review"][0], "ready for human review")
        self.assertEqual(headlines["implementation_complete"][0], "implementation complete")
        self.assertIn("review requested", headlines["implementation_complete"])
        self.assertEqual(headlines["stale_review"][0], "stale observation")
        self.assertEqual(headlines["review_running"][0], "provider run observed")

        # The eight named lifecycle states never share a label.
        distinct = [
            "review requested",
            "review observed running",
            "stale review",
            "changes requested",
            "implementation complete",
            "ready for human review",
            "ready to merge",
            "merged",
        ]
        self.assertEqual(len(set(distinct)), len(distinct))
        rules = _eval_board_view("STATE_RULES.map(rule => rule.label)")
        for label in distinct:
            self.assertIn(label, rules)
        self.assertEqual(len(rules), len(set(rules)))

    def test_gate_publisher_never_stands_in_for_the_gate_verdict(self) -> None:
        nodes = _render_board_sequence(
            [{"payload": _observation_payload([_observation_fixture("publisher_pass_gate_pending")])}]
        )[0]
        detail = nodes["worklist"]
        self.assertIn("code-mower/gate verdict", detail)
        self.assertIn("gate publisher run", detail)
        self.assertIn("Publisher execution only; it is not the gate verdict.", detail)
        self.assertIn("gate pending", detail)

    def test_selected_row_exposes_independent_evidence(self) -> None:
        nodes = _render_board_sequence(
            [{"payload": _observation_payload([_observation_fixture("ready")])}]
        )[0]
        detail = nodes["worklist"]
        for group in ("Builder", "Review", "CI", "Gate", "Merge", "Human policy"):
            self.assertIn(f"<h4>{group}</h4>", detail)
        self.assertIn("orchestrator lease", detail)
        self.assertIn("An assignment is a record of intent, not of execution.", detail)
        self.assertIn("github, fresh, complete coverage", detail)
        self.assertIn("remote_session, fresh, complete coverage", detail)

    def test_selection_is_kept_by_opaque_work_identity_across_refresh(self) -> None:
        first = _observation_fixture("observed_running")
        second = _observation_fixture("ready")
        payload = _observation_payload([first, second])
        keys = _work_keys(_render_board_sequence([{"payload": payload}])[0]["worklist"])
        self.assertEqual(len(keys), 2)
        running_key = next(key for key in keys if key.endswith("runningwork"))

        # Select the row that is not the default, then refresh twice: once with
        # the same payload and once with the rows in the opposite order.
        reordered = _observation_payload([second, first])
        frames = _render_board_sequence(
            [
                {"payload": payload},
                {"select": running_key, "payload": payload},
                {"payload": reordered},
            ]
        )
        self.assertNotEqual(_selected_key(frames[0]["worklist"]), running_key)
        self.assertEqual(_selected_key(frames[1]["worklist"]), running_key)
        self.assertEqual(_selected_key(frames[2]["worklist"]), running_key)

        # The identity is built only from session, worktree and work id.
        key = _eval_board_view("workKey(ARGS[0])", first)
        self.assertTrue(key.startswith("work:"))
        self.assertIn(first["work"]["id"], key)
        moved = copy.deepcopy(first)
        moved["created_at"] = "2026-09-12T20:00:01Z"
        self.assertEqual(_eval_board_view("workKey(ARGS[0])", moved), key)

    def test_unchanged_polls_announce_nothing_and_stay_out_of_the_timeline(self) -> None:
        record = _observation_fixture("observed_running")
        payload = _observation_payload([record])
        # A poll that only advances observation and heartbeat times is an
        # unchanged snapshot, not news.
        polled = copy.deepcopy(payload)
        for source in polled["observations"]["records"][0]["sources"]:
            source["checked_at"] = "2026-09-12T20:00:20Z"
        changed = copy.deepcopy(payload)
        changed["observations"]["records"][0]["work"]["stage"] = "in_review"

        frames = _render_board_sequence(
            [
                {"payload": payload},
                {"payload": polled},
                {"payload": changed},
                {"payload": changed},
            ]
        )
        # The live region is only ever touched by a meaningful change, so the
        # first render and the unchanged poll after it leave it untouched.
        self.assertEqual(frames[0].get("announce", ""), "")
        self.assertEqual(frames[1].get("announce", ""), "")
        self.assertIn("not listed here", frames[1]["changes"])
        self.assertIn("issue-946", frames[2]["announce"])
        # A recorded change that does not move the headline is still reported
        # as a change rather than as a new state.
        self.assertIn("changed while staying provider run observed", frames[2]["announce"])
        self.assertEqual(
            _eval_board_view(
                "changeSentence({kind: 'changed', reference: 'issue-946',"
                " headline: 'ready to merge', from: 'in review'})"
            ),
            "issue-946 moved from in review to ready to merge",
        )
        self.assertEqual(frames[2]["changes"].count('class="row"'), 1)
        # The fourth poll repeats the third, so nothing new is announced or logged.
        self.assertEqual(frames[3]["announce"], frames[2]["announce"])
        self.assertEqual(frames[3]["changes"].count('class="row"'), 1)

    def test_signature_ignores_poll_timestamps_and_tracks_recorded_change(self) -> None:
        record = _observation_fixture("observed_running")
        polled = copy.deepcopy(record)
        for source in polled["sources"]:
            source["checked_at"] = "2026-09-12T20:00:20Z"
            source["observed_at"] = "2026-09-12T20:00:10Z"
            if source["heartbeat_at"] is not None:
                source["heartbeat_at"] = "2026-09-12T20:00:10Z"
        polled["created_at"] = "2026-09-12T20:00:20Z"
        self.assertEqual(
            _eval_board_view("workSignature(ARGS[0])", record),
            _eval_board_view("workSignature(ARGS[0])", polled),
        )
        moved = copy.deepcopy(record)
        moved["work"]["runs"][0]["phase"] = "implementation_complete"
        moved["work"]["runs"][0]["basis"] = "provider_reported"
        moved["work"]["runs"][0]["lifecycle"]["state"] = "complete"
        self.assertNotEqual(
            _eval_board_view("workSignature(ARGS[0])", record),
            _eval_board_view("workSignature(ARGS[0])", moved),
        )

    def test_fixture_scenarios_produce_honest_summaries(self) -> None:
        # No session: an unlinked observation claims no stage and no route.
        unlinked = _render_board_sequence(
            [{"payload": _observation_payload([_observation_fixture("unlinked")])}]
        )[0]["worklist"]
        self.assertIn("identity unlinked", unlinked)
        self.assertIn("stage: not linked to a session", unlinked)
        self.assertIn("next: <b>no next action recorded</b>", unlinked)
        self.assertIn("Nothing binds this run to Code Mower work", unlinked)

        # Idle with complete coverage is idle because it was looked at.
        idle = _render_board_sequence(
            [{"payload": _observation_payload([_observation_fixture("no_work")])}]
        )[0]["worklist"]
        self.assertIn("idle with complete coverage", idle)
        self.assertIn("session, work_queue, run_registry", idle)
        self.assertNotIn("no work found", idle)

        # An unavailable source preserves the last observation without a live claim.
        unavailable = _render_board_sequence(
            [
                {
                    "payload": _observation_payload(
                        [_observation_fixture("source_unavailable_preserves_last_observation")]
                    )
                }
            ]
        )[0]["worklist"]
        self.assertIn("source unavailable", unavailable)
        self.assertIn("last observed", unavailable)
        self.assertIn("is not evidence of work running now", unavailable)

        # A stale source is reported as stale, with its partial coverage named.
        stale = _render_board_sequence(
            [{"payload": _observation_payload([_observation_fixture("stale")])}]
        )[0]["worklist"]
        self.assertIn("stale observation", stale)
        self.assertIn("Source stale: remote_session", stale)
        self.assertIn("Partial coverage: remote_session", stale)
        self.assertNotIn("live, observed", stale)

    def test_partial_coverage_reports_counts_and_never_a_percentage_or_eta(self) -> None:
        record = _observation_fixture("observed_running")
        record["work"]["measurements"]["elapsed_seconds"] = {
            "value": 120.0,
            "coverage": "partial",
            "observed": 2,
            "total": 5,
        }
        record = board_observation.validate(record)
        nodes = _render_board_sequence([{"payload": _observation_payload([record])}])[0]
        self.assertIn("elapsed 120.0s from 2 of 5 recorded", nodes["worklist"])
        self.assertIn("cost not recorded", nodes["worklist"])

        # Nothing the operator is shown states a share, a percentage, or a
        # projection of work that has not been observed.
        rendered = "\n".join(nodes.values())
        self.assertNotIn("%", rendered)
        for invented in ("percent", "estimat", "remaining", "projected", "eta "):
            self.assertNotIn(invented, rendered.lower())
        # A partial measurement is reported as counted evidence, never scaled.
        self.assertEqual(
            _eval_board_view(
                "measurementText({value: 2, coverage: 'partial', observed: 2, total: 5}, 'count')"
            ),
            "2 from 2 of 5 recorded",
        )

    def test_unknown_states_stay_neutral_and_colour_always_carries_text(self) -> None:
        classes = _eval_board_view(
            "['unknown', 'not_started', 'absent', 'unverifiable', 'none', 'unassigned']"
            ".map(state => EVIDENCE_STATE_CLASSES[state])"
        )
        self.assertEqual(set(classes), {"muted"})
        nodes = _render_board_sequence(
            [{"payload": _observation_payload([_observation_fixture("observed_running")])}]
        )[0]
        detail = nodes["worklist"]
        # Every coloured pill carries a text cue and a text label beside it.
        for coloured in re.findall(r'<span class="pill (ok|warn|bad)">(.*?)</span>\s*</span>', detail):
            self.assertIn('class="cue"', coloured[1] + "</span>")
        self.assertEqual(detail.count('<span class="pill ok">'), detail.count('<span class="pill ok"><span class="cue"'))
        self.assertEqual(detail.count('<span class="pill bad">'), detail.count('<span class="pill bad"><span class="cue"'))

    def test_actions_are_read_only_and_never_invent_a_remote_link(self) -> None:
        record = _observation_fixture("ready")
        # No open PR is recorded locally, so no PR link may be offered.
        without_link = _render_board_sequence([{"payload": _observation_payload([record])}])[0]
        self.assertIn("PR #946, no local link recorded", without_link["worklist"])
        self.assertNotIn("https://github.com/codemower-ai/code-mower/pull/946", without_link["worklist"])

        payload = _observation_payload([record])
        payload["remote"]["pull_requests"] = [
            {
                "number": 946,
                "url": "https://github.example/owner/repo/pull/946",
                "title": "t",
                "labels": {},
                "checks": [],
            }
        ]
        with_link = _render_board_sequence([{"payload": payload}])[0]["worklist"]
        self.assertIn('href="https://github.example/owner/repo/pull/946">Open PR #946', with_link)
        self.assertIn("This Board never merges, requeues, cancels, retries", with_link)
        # The page has no form, no non-GET request, and reaches only the two
        # read-only local endpoints.
        page = board.render_board_html(board.BoardConfig(repo="owner/repo"))
        self.assertNotIn("<form", page)
        self.assertNotIn("POST", page)
        self.assertNotIn("method=", page)
        self.assertEqual(
            sorted(set(re.findall(r'fetch\("([^"]+)"', page))),
            ["/api/events", "/api/status"],
        )

    def test_a_same_numbered_pull_request_in_another_repository_is_never_linked(self) -> None:
        # A custom observations directory can hold a record another repository
        # produced. Its PR number is not this repository's PR number, so this
        # repository's link may not be attached to it.
        foreign = _observation_fixture("ready")
        foreign["scope"]["repository"] = "other-org/other-repo"
        for run in foreign["work"]["runs"]:
            run["binding"]["repository"] = "other-org/other-repo"
        # The foreign record is entirely self-consistent: the contract accepts
        # it, and it names a repository that is not this one.
        foreign = board_observation.validate(foreign)
        payload = _observation_payload([foreign])
        payload["remote"]["pull_requests"] = [
            {
                "number": 946,
                "url": "https://github.example/codemower-ai/code-mower/pull/946",
                "title": "t",
                "labels": {},
                "checks": [],
            }
        ]
        worklist = _render_board_sequence([{"payload": payload}])[0]["worklist"]
        self.assertNotIn("Open PR #946", worklist)
        self.assertNotIn("https://github.example/codemower-ai/code-mower/pull/946", worklist)
        # The foreign record is still shown for exactly what it is.
        self.assertIn(
            "PR #946 in other-org/other-repo, not this repository; no local link recorded",
            worklist,
        )

        # The identical payload for this repository does get the recorded link,
        # so the suppression above is the repository check and nothing else.
        local = copy.deepcopy(payload)
        local["observations"]["records"] = [_observation_fixture("ready")]
        self.assertEqual(
            local["observations"]["records"][0]["scope"]["repository"], "codemower-ai/code-mower"
        )
        local_worklist = _render_board_sequence([{"payload": local}])[0]["worklist"]
        self.assertIn(
            'href="https://github.example/codemower-ai/code-mower/pull/946">Open PR #946',
            local_worklist,
        )

    def test_duplicate_observations_of_one_identity_render_the_newest_once(self) -> None:
        # Two files in the directory observe the same work item: an older one
        # and the one that replaced it.
        older = _observation_fixture("ready")
        newer = _observation_fixture("merged")
        for record in (older, newer):
            record["work"]["id"] = "readywork"
            record["work"]["runs"][0]["binding"]["work_id"] = "readywork"
        older["created_at"] = "2026-09-12T20:00:00Z"
        newer["created_at"] = "2026-09-12T20:00:20Z"
        # Both remain records the frozen contract accepts.
        older = board_observation.validate(older)
        newer = board_observation.validate(newer)
        scope = older["scope"]
        identity = f"work:{scope['session_id']}:{scope['worktree_id']}:readywork"

        frames = _render_board_sequence(
            [
                {"payload": _observation_payload([older, newer])},
                # The same two files listed the other way round.
                {"payload": _observation_payload([newer, older])},
            ]
        )
        worklist = frames[0]["worklist"]
        # One identity is one row and one detail region, not two competing ones.
        self.assertEqual(_work_keys(worklist), [identity])
        self.assertEqual(worklist.count('class="rowbtn" id='), 1)
        self.assertEqual(worklist.count('id="workdetail"'), 1)
        # The newest observation is the one rendered.
        self.assertIn("stage: merged", worklist)
        self.assertNotIn("stage: ready to merge", worklist)
        self.assertEqual(_selected_key(worklist), identity)

        # Which file the directory happened to list first cannot change the row.
        self.assertEqual(frames[1]["worklist"], worklist)
        # Change tracking reads the same deduplicated set, so reordering the
        # duplicates is not news.
        self.assertEqual(frames[1].get("announce", ""), "")
        self.assertIn("not listed here", frames[1]["changes"])

        # A genuinely newer observation of the same identity is still a change.
        newest = copy.deepcopy(newer)
        newest["created_at"] = "2026-09-12T20:00:25Z"
        newest["work"]["stage"] = "in_review"
        newest = board_observation.validate(newest)
        moved = _render_board_sequence(
            [
                {"payload": _observation_payload([older, newer])},
                {"payload": _observation_payload([older, newest])},
            ]
        )[1]
        self.assertEqual(_work_keys(moved["worklist"]), [identity])
        self.assertIn("stage: in review", moved["worklist"])
        self.assertIn("issue-946", moved["announce"])

    def test_a_run_that_changes_phase_across_observations_counts_once_and_newest(self) -> None:
        # One run, observed twice: the file that caught it dispatched, and the
        # file that replaced it once the run was seen running.
        def phased(fixture_name: str, created_at: str) -> dict:
            record = _observation_fixture(fixture_name)
            record["created_at"] = created_at
            record["work"]["id"] = "phasework"
            record["work"]["runs"][0]["id"] = "phaserun"
            record["work"]["runs"][0]["binding"]["work_id"] = "phasework"
            return board_observation.validate(record)

        older = phased("dispatched", "2026-09-12T20:00:00Z")
        newer = phased("observed_running", "2026-09-12T20:00:20Z")
        frames = _render_board_sequence(
            [
                {"payload": _observation_payload([older, newer])},
                # The same two files listed the other way round.
                {"payload": _observation_payload([newer, older])},
            ]
        )
        participants = frames[0]["participants"]
        # One run, counted once, in the phase the newest observation records.
        self.assertIn("1 recorded run;", participants)
        self.assertNotIn("2 recorded runs", participants)
        self.assertIn('<span class="pill">observed running 1</span>', participants)
        # The phase the run has already moved past is not still reported.
        self.assertNotIn("dispatched 1", participants)
        self.assertEqual(participants.count('class="row"'), 1)
        # The participant summary reads the same deduplicated set the work list
        # does, so file order cannot change either of them.
        self.assertEqual(frames[1]["participants"], participants)
        self.assertEqual(frames[1]["worklist"], frames[0]["worklist"])
        self.assertIn("assignments: codex builder observed running", frames[0]["worklist"])

    def test_consolidated_unlinked_observations_recompute_freshness_and_update(self) -> None:
        # One unlinked run in one repository, observed twice: a first file whose
        # source was fresh, and a later file whose source has gone unavailable
        # while preserving a later recorded event.
        fresh = _observation_fixture("unlinked")
        gone = copy.deepcopy(fresh)
        gone["created_at"] = "2026-09-12T20:00:20Z"
        gone["sources"] = [
            {
                "id": "registryobs",
                "kind": "run_registry",
                "freshness": "unavailable",
                "coverage": "unavailable",
                "event_at": "2026-09-12T20:00:05Z",
                "observed_at": "2026-09-12T20:00:10Z",
                "checked_at": "2026-09-12T20:00:20Z",
                "heartbeat_at": None,
            }
        ]
        gone["unlinked"][0]["source_id"] = "registryobs"
        gone["unlinked"][0]["observed_at"] = "2026-09-12T20:00:10Z"
        # Both remain records the frozen contract accepts.
        gone = board_observation.validate(gone)

        frames = _render_board_sequence(
            [
                {"payload": _observation_payload([fresh, gone])},
                # The same two files listed the other way round.
                {"payload": _observation_payload([gone, fresh])},
            ]
        )
        worklist = frames[0]["worklist"]
        # Reversing the files cannot change one byte of the consolidated row.
        self.assertEqual(frames[1]["worklist"], worklist)
        self.assertEqual(frames[1]["participants"], frames[0]["participants"])

        # A fresh first record cannot hide the unavailable source behind the
        # evidence that is being shown next to it.
        self.assertIn(
            '<span class="pill bad"><span class="cue" aria-hidden="true">!</span>'
            " last observed 30s ago</span>",
            worklist,
        )
        self.assertNotIn(
            '<span class="cue" aria-hidden="true">+</span> observed 30s ago', worklist
        )
        self.assertIn("Source unavailable: run_registry.", worklist)
        # The later meaningful update is the one retained, not the earlier one
        # the first file happened to record.
        self.assertIn("last update: 25s ago", worklist)
        self.assertNotIn("last update: 50s ago", worklist)
        self.assertIn("last meaningful update 25s ago", worklist)

        # One run observed in two files is one run, attested by the worst
        # source that observed it.
        self.assertIn("assignments: claude unknown", worklist)
        self.assertNotIn("claude unknown; claude unknown", worklist)
        self.assertIn("run_registry, unavailable, unavailable coverage", worklist)
        participants = frames[0]["participants"]
        self.assertIn("1 recorded run;", participants)
        self.assertNotIn("2 recorded runs", participants)
        self.assertIn("worst source unavailable", participants)

    def test_element_ids_encode_every_work_key_injectively(self) -> None:
        keys = [
            # The frozen contract admits all three of these repositories, and
            # an unlinked row is identified by its repository alone.
            "unlinked:owner/re.po",
            "unlinked:owner/re-po",
            "unlinked:owner/re_po",
            # Work and idle identities differing only in their punctuation.
            "work:" + "a" * 32 + ":sha256:" + "b" * 64 + ":work_one",
            "work:" + "a" * 32 + ":sha256:" + "b" * 64 + ":work-one",
            "idle:" + "a" * 32 + ":sha256:" + "b" * 64,
            # Opaque keys past anything the contract admits today: non-ASCII, an
            # astral character, separators alone, and a long one.
            "unlinked:ówner/répo",
            "unlinked:owner/repo\U0001f600",
            ":::",
            "unlinked:owner/" + "a.b-c_" * 60,
        ]
        rows = _eval_board_page("ARGS[0].map(rowElementId)", keys)
        # Distinct work is distinct DOM identity: no two keys share a row id.
        self.assertEqual(len(set(rows)), len(keys))
        # The rule this replaced did collapse them, which is the defect: it is
        # not a collision this set merely happens to avoid.
        self.assertLess(len({re.sub(r"[^A-Za-z0-9_-]", "-", key) for key in keys}), len(keys))
        for element_id in rows:
            self.assertRegex(element_id, r"^workrow-[A-Za-z0-9_-]+$")
        # Every id decodes back to exactly the key it came from, which is what
        # makes the encoding injective rather than merely unlikely to collide.
        decoded = _eval_board_page(
            "ARGS[0].map(key => rowElementId(key).slice('workrow-'.length)"
            ".replace(/_([0-9a-f]+)_/g, (whole, hex) => String.fromCharCode(parseInt(hex, 16))))",
            keys,
        )
        self.assertEqual(decoded, keys)
        # The detail region's actions are built from the same encoding, so they
        # are unique per work item too, and never collide with a row button.
        actions = _eval_board_page(
            "ARGS[0].flatMap(key => ARGS[1].map(name => actionElementId(key, name)))",
            keys,
            ["openpr", "inspect", "changes"],
        )
        self.assertEqual(len(set(actions)), len(keys) * 3)
        self.assertFalse(set(actions) & set(rows))

    def test_keys_that_differ_only_in_punctuation_keep_separate_rows_and_focus(self) -> None:
        def unlinked_for(repository: str) -> dict:
            record = _observation_fixture("unlinked")
            record["scope"]["repository"] = repository
            # Still a record the frozen contract accepts.
            return board_observation.validate(record)

        payload = _observation_payload([unlinked_for("owner/re.po"), unlinked_for("owner/re-po")])
        worklist = _render_board_focus([{"payload": payload}])[0]["worklist"]
        keys = _work_keys(worklist)
        self.assertEqual(sorted(keys), ["unlinked:owner/re-po", "unlinked:owner/re.po"])
        # Two work items, two row ids. Under the punctuation-to-hyphen rule
        # both rows claimed one id, so the document carried a duplicate.
        row_ids = [_row_element_id(worklist, key) for key in keys]
        self.assertEqual(len(set(row_ids)), 2)

        # The one detail region is labelled by the row that is actually
        # selected, and the row that is not selected is not expanded.
        selected_key = _selected_key(worklist)
        selected_id = _row_element_id(worklist, selected_key)
        self.assertEqual(worklist.count('id="workdetail"'), 1)
        # The detail region also carries the opaque identity it is rendered
        # for, which is what a refresh matches its preserved scroll offset
        # against, so two keys that differ only in punctuation cannot inherit
        # one another's reading position either.
        self.assertIn(
            f'id="workdetail" data-key="{selected_key}" role="region" aria-labelledby="{selected_id}"',
            worklist,
        )
        self.assertEqual(worklist.count('aria-expanded="true"'), 1)

        # Selecting its neighbour moves the detail, and the label with it.
        other_key = next(key for key in keys if key != selected_key)
        moved = _render_board_focus([{"payload": payload}, {"select": other_key}])[-1]["worklist"]
        other_id = _row_element_id(moved, other_key)
        self.assertNotEqual(other_id, selected_id)
        self.assertEqual(_selected_key(moved), other_key)
        self.assertEqual(moved.count('id="workdetail"'), 1)
        self.assertIn(
            f'id="workdetail" data-key="{other_key}" role="region" aria-labelledby="{other_id}"',
            moved,
        )
        # The detail's actions belong to the work that is selected, so no
        # action id is shared between the two rows' detail regions.
        first_actions = set(re.findall(r'id="(workaction-[^"]+)"', worklist))
        second_actions = set(re.findall(r'id="(workaction-[^"]+)"', moved))
        self.assertTrue(first_actions)
        self.assertFalse(first_actions & second_actions)

        # A refresh under the keyboard restores focus to the same row, not to
        # the neighbour that used to answer to the same id.
        restored = _render_board_focus(
            [
                {"payload": payload},
                {"select": other_key},
                {"focus": other_id},
                {"payload": payload},
            ]
        )[-1]
        self.assertEqual(restored["active"], other_id)
        self.assertEqual(_selected_key(restored["worklist"]), other_key)

    def test_mobile_detail_follows_the_row_and_desktop_places_it_adjacent(self) -> None:
        html = board.render_board_html(board.BoardConfig(repo="owner/repo"))
        # One detail node, rendered inside the selected row, so single-column
        # source order already puts it under the row it belongs to.
        nodes = _render_board_sequence(
            [
                {
                    "payload": _observation_payload(
                        [_observation_fixture("observed_running"), _observation_fixture("ready")]
                    )
                }
            ]
        )[0]
        self.assertEqual(nodes["worklist"].count('id="workdetail"'), 1)
        selected = nodes["worklist"].split('<li class="workrow selected">')[1]
        self.assertLess(selected.index("</button>"), selected.index('id="workdetail"'))
        self.assertIn("@media (min-width: 900px) {", html)
        # Desktop moves the detail into a second column of the row's own grid.
        # It is still one region, still rendered inside the selected row, and
        # still in normal flow rather than painted over the page.
        style = _computed(
            _board_css(),
            {"tag": "div", "classes": ["workdetail"]},
            [
                {"tag": "div", "id": "worklist", "classes": []},
                {"tag": "ul", "classes": ["workrows"]},
                {"tag": "li", "classes": ["workrow", "selected"]},
            ],
            desktop=True,
        )
        self.assertNotIn("position", style)
        self.assertEqual(style["grid-column"], "2")
        row = _computed(
            _board_css(),
            {"tag": "li", "classes": ["workrow", "selected"]},
            [{"tag": "div", "id": "worklist", "classes": []}, {"tag": "ul", "classes": ["workrows"]}],
            desktop=True,
        )
        self.assertEqual(row["display"], "grid")
        self.assertEqual(_track_count(row["grid-template-columns"]), 2)

    def test_desktop_detail_reserves_its_height_so_a_short_list_cannot_overlap(self) -> None:
        # A detail region far taller than the one or two rows beside it: the
        # case where an out-of-flow panel used to hang over the Work Now and
        # Participants sections that follow the list.
        boxes = {"row_height": 90, "detail_height": 420}
        for rows, selected in ((1, 0), (2, 0), (2, 1)):
            with self.subTest(rows=rows, selected=selected):
                layout = _work_list_layout(
                    _board_css(), rows=rows, selected=selected, **boxes
                )
                # The list reserves real height for the detail, so everything
                # after it starts below the detail rather than under it.
                self.assertGreaterEqual(layout["reserved"], layout["detail_bottom"])
                self.assertGreaterEqual(layout["reserved"], boxes["detail_height"])

        # The same model, given the rule this page shipped before, reports the
        # overlap it was blocked for: a one-row list reserved its 180px minimum
        # while the absolutely positioned detail ran on to 420px.
        previous = _work_list_layout(
            PREVIOUS_DESKTOP_CSS, container_classes=("worklayout",), rows=1, selected=0, **boxes
        )
        self.assertEqual(previous["reserved"], 180)
        self.assertEqual(previous["detail_bottom"], 420)
        self.assertGreater(previous["detail_bottom"], previous["reserved"])

    def test_no_rule_takes_dynamic_content_out_of_flow_without_pinning_its_box(self) -> None:
        # Out-of-flow content contributes no layout height, so anything the
        # payload can grow must stay in flow. The one exception is the
        # visually hidden live region, which pins its own box to a clipped
        # pixel and so can never overlap anything.
        for at_rule, selector, declarations in _css_rules(_board_css()):
            if declarations.get("position") not in {"absolute", "fixed"}:
                continue
            with self.subTest(rule=f"{at_rule} {selector}".strip()):
                self.assertEqual(declarations.get("width"), "1px")
                self.assertEqual(declarations.get("height"), "1px")
                self.assertEqual(declarations.get("overflow"), "hidden")

    def test_participants_report_recorded_phases_without_claiming_liveness(self) -> None:
        nodes = _render_board_sequence(
            [
                {
                    "payload": _observation_payload(
                        [_observation_fixture("observed_running"), _observation_fixture("unlinked")]
                    )
                }
            ]
        )[0]
        participants = nodes["participants"]
        self.assertIn("<b>codex</b>", participants)
        self.assertIn("observed running 1", participants)
        self.assertIn("<b>claude</b>", participants)
        self.assertIn("not linked to a session 1", participants)
        self.assertIn("not a claim that anything is running now", participants)

    def test_health_view_reports_connections_version_and_process_state(self) -> None:
        payload = _observation_payload(
            [_observation_fixture("stale"), _observation_fixture("observed_running")]
        )
        payload["board"]["version"]["installed_version"] = "1.5.0"
        payload["board"]["version"]["restart_recommended"] = True
        payload["board"]["cache"]["state"] = "stale"
        payload["local_boards"] = {"boards": [{"port": 5332, "pid": 42, "cwd": ""}]}
        nodes = _render_board_sequence([{"payload": payload}])[0]
        self.assertIn("restart recommended", nodes["diagnostics"])
        self.assertIn("installed 1.5.0", nodes["diagnostics"])
        self.assertIn("Snapshot cache", nodes["diagnostics"])
        self.assertIn("2 recorded", nodes["diagnostics"])
        self.assertIn("<b>remote_session</b>", nodes["sources"])
        self.assertIn("coverage partial", nodes["sources"])
        self.assertIn("board localhost:5332", nodes["local"])

    def test_empty_observation_directory_is_reported_as_nothing_recorded(self) -> None:
        payload = _observation_payload([])
        payload["observations"]["path_exists"] = False
        nodes = _render_board_sequence([{"payload": payload}])[0]
        self.assertIn("No local Board observation is recorded yet", nodes["worklist"])
        self.assertNotIn("idle", nodes["worklist"])
        rejected = _observation_payload([])
        rejected["observations"]["rejected"] = 1
        rejected["observations"]["message"] = "no local Board observation passed the observation contract"
        rejected_nodes = _render_board_sequence([{"payload": rejected}])[0]
        self.assertIn("passed the observation contract", rejected_nodes["worklist"])
        self.assertIn("1 rejected by the observation contract", rejected_nodes["diagnostics"])


# One expression over the shipped view model returning every run-level display
# at once -- the row's own states, the assignments line, the selected-work
# evidence panel and the participant summary -- so the displays are compared
# against each other rather than each against its own expectation.
RUN_DISPLAY_EXPRESSION = """(() => {
  const rows = workRows(ARGS[0], ARGS[1]);
  const summary = participantSummary(rows);
  return {
    rows: rows.map(row => ({
      reference: row.reference,
      states: row.states.map(state => [state.label, state.class, state.cue]),
      assignments: row.assignments,
      builder: (row.groups.find(group => group.name === "builder") || {items: []}).items
        .map(item => [item.state, item.class, item.cue])
    })),
    participants: summary.map(entry => [
      entry.provider,
      entry.role,
      entry.states.map(state => [state.label, state.count])
    ])
  };
})()"""


class BoardRunLifecycleDisplayTests(TestCase):
    """Every run-level display names one run the same lifecycle-aware way.

    The frozen contract requires the `suspended` lifecycle state to carry the
    `failed` phase, so any display built from the phase alone reports a paused
    provider session as a failed one. These tests execute the shipped page
    JavaScript over every lifecycle state the contract accepts, against every
    phase it allows that state to carry.
    """

    NOW_MS = int(OBSERVATION_NOW.timestamp() * 1000)

    def _run_displays(self, records: list[dict], **kwargs: object) -> dict:
        return _eval_board_view(
            RUN_DISPLAY_EXPRESSION,
            _observation_payload(records),
            self.NOW_MS,
            **kwargs,
        )

    def test_every_lifecycle_state_displays_one_run_the_same_way_everywhere(self) -> None:
        vocabulary = {label for _, _, label, _, _ in LIFECYCLE_DISPLAY_MATRIX}
        for state, phase, label, cls, cue in LIFECYCLE_DISPLAY_MATRIX:
            with self.subTest(lifecycle=state, phase=phase):
                reference = f"lifecycle-{state or 'none'}-{phase}"
                record = _record_with_run_lifecycle(reference, phase, state)
                view = self._run_displays([record])
                row = view["rows"][0]
                self.assertEqual(row["reference"], reference)
                # The selected-work evidence panel, the assignments line and
                # the participant summary all name and colour the one recorded
                # run identically, and all three count it exactly once.
                self.assertEqual(row["builder"], [[label, cls, cue]])
                self.assertEqual(row["assignments"], [f"codex builder {label}"])
                self.assertEqual(
                    view["participants"], [["codex", "builder", [[label, 1]]]]
                )
                # No other run state in the contract's vocabulary is named by
                # any of those displays, so a suspended run never reads as
                # failed, a cancelled one never reads as failed, and an actual
                # lifecycle failure never reads as either.
                rendered = json.dumps([row["builder"], row["assignments"], view["participants"]])
                for other in vocabulary - {label}:
                    self.assertNotIn(other, rendered)

    def test_a_suspended_run_never_carries_a_failed_row_state(self) -> None:
        for state, phase, label, _, _ in LIFECYCLE_DISPLAY_MATRIX:
            with self.subTest(lifecycle=state, phase=phase):
                record = _record_with_run_lifecycle(f"states-{state or 'none'}-{phase}", phase, state)
                states = [
                    entry[0] for entry in self._run_displays([record])["rows"][0]["states"]
                ]
                if label == "suspended":
                    self.assertIn("provider run suspended", states)
                    self.assertNotIn("provider run failed", states)
                    self.assertNotIn("provider run cancelled", states)
                elif label == "failed":
                    self.assertIn("provider run failed", states)
                    self.assertNotIn("provider run suspended", states)
                elif label == "cancelled":
                    self.assertIn("provider run cancelled", states)
                    self.assertNotIn("provider run failed", states)
                else:
                    self.assertNotIn("provider run failed", states)
                    self.assertNotIn("provider run suspended", states)

    def test_the_suspended_fixture_renders_no_failure_anywhere_on_the_page(self) -> None:
        # The fixture the audit reproduced with: an accepted record whose run
        # the provider suspended, which the contract records as the `failed`
        # phase under the `suspended` lifecycle state.
        record = _record_with_suspended_run("suspended-session")
        frame = _render_board_sequence([{"payload": _observation_payload([record])}])[0]
        worklist = frame["worklist"]
        self.assertIn("provider run suspended", worklist)
        self.assertIn("assignments: codex builder suspended", worklist)
        # The detail is open on the first row, so this is the evidence panel.
        self.assertIn("suspended", worklist)
        self.assertIn("suspended 1", frame["participants"])
        # Not one label, class or count on the whole page reports a failure.
        for node, html in frame.items():
            self.assertNotIn("failed", html, f"{node} reports a failure for a suspended run")

    def test_one_suspended_run_and_one_failed_run_are_one_of_each(self) -> None:
        suspended = _record_with_run_lifecycle("mixed-suspended", "failed", "suspended")
        failed = _record_with_run_lifecycle("mixed-failed", "failed", "failed")
        cancelled = _record_with_run_lifecycle("mixed-cancelled", "cancelled", "terminated")
        view = self._run_displays([suspended, failed, cancelled])
        # One entry for the one provider and role, counting each run under the
        # state its own lifecycle records: one suspended, one failed and one
        # cancelled, never three failures.
        self.assertEqual(
            view["participants"],
            [["codex", "builder", [["cancelled", 1], ["failed", 1], ["suspended", 1]]]],
        )
        by_reference = {row["reference"]: row for row in view["rows"]}
        self.assertEqual(by_reference["mixed-suspended"]["builder"], [["suspended", "warn", "~"]])
        self.assertEqual(by_reference["mixed-failed"]["builder"], [["failed", "bad", "!"]])
        self.assertEqual(by_reference["mixed-cancelled"]["builder"], [["cancelled", "warn", "~"]])

    def test_the_timeline_reports_a_run_moving_from_suspended_to_failure(self) -> None:
        suspended = _record_with_run_lifecycle("moving-run", "failed", "suspended")
        failed = _record_with_run_lifecycle("moving-run", "failed", "failed")
        frames = _render_board_sequence(
            [
                {"payload": _observation_payload([suspended])},
                {"payload": _observation_payload([failed])},
            ]
        )
        self.assertNotIn("failed", frames[0]["worklist"])
        self.assertIn(
            "moving-run moved from provider run suspended to provider run failed",
            frames[1]["changes"],
        )
        self.assertIn("provider run failed", frames[1]["worklist"])
        self.assertNotIn("suspended", frames[1]["worklist"])

    def test_labelling_a_run_from_its_raw_phase_would_report_suspended_as_failed(self) -> None:
        """The mutation the fix replaced, executed, so these checks have teeth."""

        record = _record_with_run_lifecycle("mutation-suspended", "failed", "suspended")
        raw_phase = self._run_displays(
            [record],
            # Emptying the lifecycle mapping is exactly raw-phase labelling:
            # every run state becomes the phase the contract recorded.
            mutate=('const LIFECYCLE_RUN_STATES = {suspended: "suspended"};',
                    "const LIFECYCLE_RUN_STATES = {};"),
        )
        row = raw_phase["rows"][0]
        self.assertEqual(row["builder"], [["failed", "bad", "!"]])
        self.assertEqual(row["assignments"], ["codex builder failed"])
        self.assertEqual(raw_phase["participants"], [["codex", "builder", [["failed", 1]]]])
        self.assertIn("provider run failed", [entry[0] for entry in row["states"]])
        # And the shipped view model, unmutated, says none of that.
        shipped = self._run_displays([record])
        self.assertEqual(shipped["rows"][0]["builder"], [["suspended", "warn", "~"]])
        self.assertEqual(shipped["participants"], [["codex", "builder", [["suspended", 1]]]])


# Everything a reconciled reading decides, read off the shipped view model in
# one call: which rows survive, what each of them says, which row an operator
# who has made no choice is shown, and what the participant summary counts.
RECONCILED_EXPRESSION = """(() => {
  const rows = workRows(ARGS[0], ARGS[1]);
  return {
    keys: rows.map(row => row.key),
    headlines: rows.map(row => row.headline),
    actions: rows.map(row => row.action_label),
    selection: resolveSelection(rows, null),
    participants: participantSummary(rows).map(entry => [entry.provider, entry.role, entry.count])
  };
})()"""


class BoardSessionScopeReconciliationTests(TestCase):
    """One session scope never states two incompatible things at once.

    A session-level `no_work` snapshot and a work-specific observation of the
    same session and worktree are different work identities, so identity
    deduplication -- which only ever compares like with like -- retains both.
    Reconciliation is the explicit step that decides which of the two describes
    the session now, using the recorded observation order rather than the order
    the directory happened to list the files in.
    """

    NOW_MS = int(OBSERVATION_NOW.timestamp() * 1000)

    def _reconciled(self, records: list[dict], **kwargs: object) -> dict:
        return _eval_board_view(
            RECONCILED_EXPRESSION,
            _observation_payload(records),
            self.NOW_MS,
            **kwargs,
        )

    # Every transition one session scope can record, with the rows that survive
    # it. Offsets are seconds after the fixture clock, so each case fixes the
    # recorded order outright; the reversed-order test below proves the result
    # does not depend on the order the records were handed over in.
    TRANSITIONS = (
        (
            "idle then active: the session picked work up",
            lambda idle, work: [idle(0), work("alpha", 20)],
            [_work_key("alpha")],
        ),
        (
            "idle then two distinct work ids: both survive",
            lambda idle, work: [idle(0), work("alpha", 20), work("beta", 25)],
            [_work_key("alpha"), _work_key("beta")],
        ),
        (
            "active then idle: the session went quiet",
            lambda idle, work: [work("alpha", 0), idle(20)],
            [_idle_key()],
        ),
        (
            "terminal then idle: finished work is not current work either",
            lambda idle, work: [work("alpha", 0, fixture="merged"), idle(20)],
            [_idle_key()],
        ),
        (
            "active, idle, then new active: only the newest work survives",
            lambda idle, work: [work("alpha", 0), idle(20), work("beta", 40)],
            [_work_key("beta")],
        ),
        (
            "equal timestamps: the work-specific observation is the current one",
            lambda idle, work: [idle(0), work("alpha", 0)],
            [_work_key("alpha")],
        ),
    )

    def _transition_records(self, build) -> list[dict]:
        idle = _observation_fixture("no_work")

        def at_idle(offset: int) -> dict:
            return _observed_later(idle, offset)

        def at_work(work_id: str, offset: int, *, fixture: str = "observed_running") -> dict:
            return _observed_later(_named_work(work_id, fixture=fixture), offset)

        return build(at_idle, at_work)

    def test_every_session_scope_transition_keeps_one_truthful_reading(self) -> None:
        for name, build, expected in self.TRANSITIONS:
            with self.subTest(name):
                records = self._transition_records(build)
                self.assertEqual(self._reconciled(records)["keys"], expected)

    def test_reconciliation_never_depends_on_the_order_the_records_arrive_in(self) -> None:
        # Directory listing order, input order and file order are all the same
        # accident, and none of them may decide what the Board says.
        for name, build, expected in self.TRANSITIONS:
            with self.subTest(name):
                records = self._transition_records(build)
                forward = self._reconciled(records)
                self.assertEqual(forward["keys"], expected)
                self.assertEqual(self._reconciled(list(reversed(records))), forward)

    def test_equal_timestamps_are_a_real_tie_broken_by_specificity(self) -> None:
        # The tie is proved rather than assumed: both records are compared on
        # the trusted order itself, and only then on what the tie-break did.
        idle = _observation_fixture("no_work")
        work = _named_work("alpha")
        orders = _eval_board_view(
            "[observationOrder(ARGS[0]), observationOrder(ARGS[1])]", idle, work
        )
        self.assertEqual(orders[0], orders[1])
        # A session-level snapshot summarizes the whole session; a work-specific
        # observation names one item inside it. On an exact tie the specific
        # reading wins, because "nothing to do" over a work item observed at the
        # same instant is the contradiction being removed.
        self.assertEqual(self._reconciled([idle, work])["keys"], [_work_key("alpha")])
        self.assertEqual(self._reconciled([work, idle])["keys"], [_work_key("alpha")])

    def test_a_different_worktree_in_the_same_session_never_supersedes(self) -> None:
        idle = _observed_later(_observation_fixture("no_work"), 0)
        elsewhere = _observed_later(
            _in_session(_named_work("alpha"), session=FIXTURE_SESSION, worktree=OTHER_WORKTREE),
            20,
        )
        # One session can hold several worktrees, and one of them being busy
        # says nothing about another being idle.
        expected = sorted([_idle_key(), _work_key("alpha", FIXTURE_SESSION, OTHER_WORKTREE)])
        self.assertEqual(sorted(self._reconciled([idle, elsewhere])["keys"]), expected)
        self.assertEqual(sorted(self._reconciled([elsewhere, idle])["keys"]), expected)

    def test_a_different_session_in_the_same_worktree_never_supersedes(self) -> None:
        idle = _observed_later(_observation_fixture("no_work"), 0)
        other = _observed_later(
            _in_session(_named_work("alpha"), session=OTHER_SESSION, worktree=FIXTURE_WORKTREE),
            20,
        )
        # One worktree is reused by session after session, so a later session
        # working in it is not evidence that an earlier one is not idle.
        expected = sorted([_idle_key(), _work_key("alpha", OTHER_SESSION, FIXTURE_WORKTREE)])
        self.assertEqual(sorted(self._reconciled([idle, other])["keys"]), expected)
        self.assertEqual(sorted(self._reconciled([other, idle])["keys"]), expected)

    def test_a_record_without_a_session_identity_is_never_correlated(self) -> None:
        # The frozen contract gives an unlinked record neither half of a session
        # identity, so nothing ties it to the session beside it. It keeps the
        # unlinked consolidation semantics it already had, in both directions.
        idle = _observed_later(_observation_fixture("no_work"), 0)
        unlinked = _observed_later(_observation_fixture("unlinked"), 20)
        keys = [_idle_key(), "unlinked:codemower-ai/code-mower"]
        self.assertEqual(sorted(self._reconciled([idle, unlinked])["keys"]), sorted(keys))
        older_unlinked = _observed_later(_observation_fixture("unlinked"), -20)
        self.assertEqual(sorted(self._reconciled([idle, older_unlinked])["keys"]), sorted(keys))
        work = _observed_later(_named_work("alpha"), 20)
        self.assertEqual(
            sorted(self._reconciled([work, unlinked])["keys"]),
            sorted([_work_key("alpha"), "unlinked:codemower-ai/code-mower"]),
        )

    def test_a_scope_missing_either_half_of_its_identity_is_not_a_scope(self) -> None:
        # Half an identity is not an identity: correlating on it would be a
        # guess about which session a record belonged to. The view model is
        # asked directly, because the contract does not let a producer record
        # any of these shapes in the first place.
        scopes = [
            {"session_id": FIXTURE_SESSION, "worktree_id": FIXTURE_WORKTREE},
            {"session_id": FIXTURE_SESSION, "worktree_id": None},
            {"session_id": None, "worktree_id": FIXTURE_WORKTREE},
            {"session_id": "", "worktree_id": FIXTURE_WORKTREE},
            {"session_id": None, "worktree_id": None},
            {},
        ]
        self.assertEqual(
            _eval_board_view("ARGS[0].map(scope => sessionScope({scope}))", scopes),
            [f"{FIXTURE_SESSION} {FIXTURE_WORKTREE}", "", "", "", "", ""],
        )

    def test_the_reconciled_set_is_what_every_work_first_consumer_reads(self) -> None:
        # One reconciliation, consumed everywhere: the work list, the default
        # selection and the participant summary all descend from it, so a
        # superseded observation cannot keep counting a run or keep offering
        # itself as the row an operator lands on.
        idle = _observed_later(_observation_fixture("no_work"), 0)
        work = _observed_later(_named_work("alpha"), 20)
        reconciled = self._reconciled([idle, work])
        self.assertEqual(reconciled["keys"], [_work_key("alpha")])
        self.assertEqual(reconciled["selection"], _work_key("alpha"))
        self.assertEqual(reconciled["participants"], [["codex", "builder", 1]])

        # And in the other direction the idle snapshot is what is left, so the
        # superseded run is no longer counted as a participant at all.
        quiet = self._reconciled([_observed_later(_named_work("alpha"), 0), _observed_later(idle, 20)])
        self.assertEqual(quiet["keys"], [_idle_key()])
        self.assertEqual(quiet["selection"], _idle_key())
        self.assertEqual(quiet["participants"], [])

    def test_no_idle_claim_is_rendered_beside_active_work_in_one_scope(self) -> None:
        idle = _observed_later(_observation_fixture("no_work"), 0)
        work = _observed_later(_named_work("alpha"), 20)
        nodes = _render_board_sequence([{"payload": _observation_payload([idle, work])}])[0]
        worklist = nodes["worklist"]
        self.assertEqual(_work_keys(worklist), [_work_key("alpha")])
        # The two sentences that would contradict the work beside them.
        self.assertNotIn("idle with complete coverage", worklist)
        self.assertNotIn("nothing to do in this session", worklist)
        self.assertNotIn("This session is idle because", worklist)
        # The page counts what it renders, everywhere it reports a count.
        self.assertIn("1 observed work item", nodes["chrome"])
        self.assertIn("1 recorded run;", nodes["participants"])
        # The superseded snapshot's sources were still really contacted, so the
        # Health view still inspects them on their own terms.
        self.assertIn("<b>session</b>", nodes["sources"])

    def test_change_tracking_reports_the_reconciled_set_and_nothing_else(self) -> None:
        idle = _observed_later(_observation_fixture("no_work"), 0)
        work = _observed_later(_named_work("alpha"), 20)
        frames = _render_board_sequence(
            [
                # An idle session, then the same session with work observed
                # after the snapshot, then the session quiet again.
                {"payload": _observation_payload([idle])},
                {"payload": _observation_payload([idle, work])},
                {"payload": _observation_payload([idle, work, _observed_later(idle, 40)])},
            ]
        )
        # Picking work up is the idle row going and the work row appearing.
        self.assertIn("alpha appeared as provider run observed", frames[1]["announce"])
        self.assertIn("is no longer recorded", frames[1]["announce"])
        self.assertEqual(_work_keys(frames[1]["worklist"]), [_work_key("alpha")])
        # Going quiet again is the reverse, and the work that is no longer
        # current is reported as exactly that rather than restated as running.
        self.assertEqual(_work_keys(frames[2]["worklist"]), [_idle_key()])
        self.assertIn("alpha is no longer recorded", frames[2]["announce"])
        self.assertIn("alpha is no longer recorded", frames[2]["changes"])
        self.assertNotIn("alpha", frames[2]["worklist"])

    def test_without_reconciliation_one_scope_claims_idle_and_active_at_once(self) -> None:
        # The mutation is exactly the missing step: identity deduplication with
        # no reconciliation after it, which is what the Board did before.
        records = [
            _observed_later(_observation_fixture("no_work"), 0),
            _observed_later(_named_work("alpha"), 20),
        ]
        unreconciled = self._reconciled(
            records,
            mutate=(
                "return reconcileSessionScopes(observationGroups(data), nowMs, coverage);",
                "return observationGroups(data);",
            ),
        )
        # Both readings survive, and the Board states both at once.
        self.assertEqual(
            sorted(unreconciled["keys"]), sorted([_idle_key(), _work_key("alpha")])
        )
        self.assertIn("idle with complete coverage", unreconciled["headlines"])
        self.assertIn("provider run observed", unreconciled["headlines"])
        self.assertIn("nothing to do in this session", unreconciled["actions"])
        # And the shipped view model, unmutated, states one of them.
        shipped = self._reconciled(records)
        self.assertEqual(shipped["keys"], [_work_key("alpha")])
        self.assertEqual(shipped["headlines"], ["provider run observed"])
        self.assertNotIn("nothing to do in this session", shipped["actions"])


@skipUnless(shutil.which("node"), "node is required to execute the shipped board renderer")
class BoardObservationCoverageViewTests(TestCase):
    """A bounded read that left files unread is stated, not quietly rendered.

    The Board reads at most `MAX_OBSERVATION_FILES` observation files per
    refresh. Before this, a larger directory produced a page that looked
    exactly like a complete one: the API reported the records as available, the
    work list rendered them as the local record set, and an idle snapshot among
    them claimed the session had no work. These tests hold the opposite: the
    shortfall is counted in `/api/status`, warned about in the work-first views,
    and stated with its cap semantics in Health.
    """

    def _block(self, records: list[dict], *, candidates: int, cap: int | None = None) -> dict:
        """The observations block the reader emits for `candidates` files."""

        cap = board.MAX_OBSERVATION_FILES if cap is None else cap
        read = min(candidates, cap)
        omitted = candidates - read
        return {
            "schema": board.BOARD_OBSERVATIONS_SCHEMA,
            "record_schema": board_observation.SCHEMA,
            "available": True,
            "path": lane_status.LOCAL_PATH_REDACTION,
            "path_redacted": True,
            "path_exists": True,
            "records": records,
            "warnings": [],
            "rejected": 0,
            "coverage": "partial" if omitted else "complete",
            "truncated": bool(omitted),
            "file_cap": cap,
            "candidate_files": candidates,
            "read_files": read,
            "omitted_files": omitted,
            "selection": board.OBSERVATION_SELECTION,
            "message": (
                f"{read} of {candidates} local Board observation files were read "
                f"(cap {cap}), so this snapshot is incomplete"
            )
            if omitted
            else "",
        }

    def test_api_truth_and_every_board_claim_come_from_one_read(self) -> None:
        cap = board.MAX_OBSERVATION_FILES
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for index in range(cap + 9):
                (directory / f"obs-{index:03d}.json").write_text(
                    json.dumps(_referenced_record(f"work-{index:03d}")), encoding="utf-8"
                )
            block = board.observations_payload(
                board.BoardConfig(repo="owner/repo", observations_path=str(directory))
            )
            local_path = str(directory)

        # /api/status states the shortfall rather than reporting availability
        # alone.
        self.assertTrue(block["truncated"])
        self.assertEqual(block["coverage"], "partial")
        self.assertEqual(
            [block["candidate_files"], block["read_files"], block["omitted_files"]],
            [cap + 9, cap, 9],
        )

        nodes = _render_board_dom(_observation_payload([], observations=block))
        # The work list warns above the rows, so nothing a row says can be read
        # as the whole local record set.
        self.assertIn("Incomplete snapshot", nodes["worklist"])
        self.assertIn(f"{cap} of {cap + 9} observation files read (cap {cap})", nodes["worklist"])
        self.assertIn("not the whole local record set", nodes["worklist"])
        # Now says it beside "Do next", and the chrome carries it into every
        # other view.
        self.assertIn("Incomplete snapshot", nodes["worknow"])
        self.assertIn("Observation files", nodes["summary"])
        self.assertIn("incomplete snapshot", nodes["chrome"])
        self.assertIn("in the files read", nodes["chrome"])
        # Health states the cap, the counts and how the read set was chosen.
        self.assertIn(f"{cap} of {cap + 9} observation files read (cap {cap})", nodes["diagnostics"])
        self.assertIn("9 not read", nodes["diagnostics"])
        self.assertIn(board.OBSERVATION_SELECTION, nodes["diagnostics"])
        # None of that names a file or a local path.
        rendered = json.dumps(nodes)
        self.assertNotIn(local_path, rendered)
        self.assertNotIn("obs-0", rendered)

    def test_a_directory_inside_the_cap_renders_exactly_as_before(self) -> None:
        cap = board.MAX_OBSERVATION_FILES
        for candidates in (1, cap):
            with self.subTest(files=candidates):
                nodes = _render_board_dom(
                    _observation_payload(
                        [],
                        observations=self._block(
                            [_observation_fixture("no_work")], candidates=candidates
                        ),
                    )
                )
                self.assertNotIn("Incomplete snapshot", nodes["worklist"])
                self.assertNotIn("Incomplete snapshot", nodes["worknow"])
                self.assertNotIn("incomplete snapshot", nodes["chrome"])
                self.assertIn("idle with complete coverage", nodes["worklist"])
                self.assertIn("nothing to do in this session", nodes["worklist"])

    def test_idle_is_not_claimed_when_candidate_files_went_unread(self) -> None:
        """Exactly at the cap the session is idle; one file past it, it is not.

        An unread file can record work in this very scope, so an idle snapshot
        stops being authoritative about the session the moment the read is
        incomplete. This is the mutation the P2 describes: the records are
        identical in both readings, and only the file coverage differs.
        """

        cap = board.MAX_OBSERVATION_FILES
        record = _observation_fixture("no_work")
        complete = _render_board_dom(
            _observation_payload([], observations=self._block([record], candidates=cap))
        )["worklist"]
        truncated = _render_board_dom(
            _observation_payload([], observations=self._block([record], candidates=cap + 1))
        )["worklist"]

        self.assertIn("idle with complete coverage", complete)
        self.assertNotIn("idle with complete coverage", truncated)
        self.assertNotIn("nothing to do in this session", truncated)
        self.assertIn("idle in the files read", truncated)
        self.assertIn("before treating this session as idle", truncated)
        # The record is still shown for what it is, with the reason its claim
        # is not being repeated.
        self.assertIn("recorded complete, not confirmed", truncated)
        self.assertIn("went unread this refresh", truncated)

    def test_an_empty_truncated_read_is_not_reported_as_nothing_recorded(self) -> None:
        nodes = _render_board_dom(
            _observation_payload(
                [], observations=self._block([], candidates=board.MAX_OBSERVATION_FILES + 4)
            )
        )
        worklist = nodes["worklist"]
        self.assertIn("incomplete", worklist)
        self.assertIn("evidence that there is no work", worklist)
        self.assertNotIn("No local Board observation is recorded yet", worklist)
        self.assertNotIn("No local Board observation passed", worklist)
        # Health's own empty states stop reading as measured absences too.
        self.assertIn("this snapshot is incomplete", nodes["participants"])
        self.assertIn("this snapshot is incomplete", nodes["sources"])

    def test_the_coverage_reading_states_cap_and_counts(self) -> None:
        cap = board.MAX_OBSERVATION_FILES
        at_cap, past_cap, unavailable = (
            _eval_board_truth(
                "observationCoverage(ARGS[0])",
                {"observations": self._block([], candidates=cap)},
            ),
            _eval_board_truth(
                "observationCoverage(ARGS[0])",
                {"observations": self._block([], candidates=cap + 5)},
            ),
            _eval_board_truth(
                "observationCoverage(ARGS[0])",
                {
                    "observations": {
                        "coverage": "unavailable",
                        "truncated": False,
                        "file_cap": cap,
                        "candidate_files": None,
                        "read_files": 0,
                        "omitted_files": None,
                    }
                },
            ),
        )

        self.assertFalse(at_cap["truncated"])
        self.assertEqual(at_cap["read"], cap)
        self.assertEqual(at_cap["omitted"], 0)
        self.assertEqual(at_cap["label"], f"{cap} of {cap} observation files read (cap {cap})")
        self.assertEqual(at_cap["note"], "")

        self.assertTrue(past_cap["truncated"])
        self.assertEqual(past_cap["omitted"], 5)
        self.assertEqual(past_cap["class"], "warn")
        self.assertIn("incomplete", past_cap["note"])
        self.assertIn("idle session", past_cap["note"])

        # An unreadable directory is not a complete one: no total is invented,
        # and nothing downstream may read it as coverage.
        self.assertFalse(unavailable["truncated"])
        self.assertEqual(unavailable["class"], "bad")
        self.assertIsNone(unavailable["candidates"])
        self.assertEqual(unavailable["label"], "observation file coverage unavailable")

        # A payload carrying no coverage at all is neutral rather than good
        # news: nothing here may render as a complete read.
        unknown = _eval_board_truth("observationCoverage(ARGS[0])", {})
        self.assertFalse(unknown["truncated"])
        self.assertEqual(unknown["state"], "unknown")
        self.assertEqual(unknown["class"], "muted")
        self.assertEqual(unknown["label"], "observation file coverage unknown")


# The instant the `no_work` fixture records itself at, so a test can name an
# age directly rather than by an offset from an unrelated clock.
FIXTURE_CREATED = datetime(2026, 9, 12, 20, 0, tzinfo=UTC)


def _with_extra_source(record: dict, *, freshness: str, coverage: str) -> dict:
    """The same accepted record, plus one source with the given reading.

    The frozen contract requires a `no_work` record's session, work queue and
    run registry to have been fresh and complete when it was written, so those
    three are never weakened here. A producer may name further sources beside
    them, and this adds exactly one -- which is how a real record comes to
    carry partial or unreachable evidence at all.
    """

    extended = copy.deepcopy(record)
    # Every instant is taken from the record itself, so a source added to a
    # record recorded a day ago is still a source a producer could have
    # written beside it.
    checked = datetime.strptime(extended["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

    def stamp(seconds: int) -> str:
        return (checked - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

    unreachable = freshness == "unavailable"
    extended["sources"].append(
        {
            "id": "extraobs",
            "kind": "remote_session",
            "freshness": freshness,
            "coverage": coverage,
            "event_at": None if unreachable else stamp(20),
            "observed_at": None if unreachable else stamp(10),
            "checked_at": stamp(0),
            "heartbeat_at": None,
        }
    )
    return board_observation.validate(extended)


def _undated(record: dict) -> dict:
    """The same record with an observation time nothing can read.

    The frozen contract requires `created_at`, so no producer can emit this.
    The view is handed it anyway, because a record whose age cannot be
    computed is exactly the case in which a view must not guess: the classifier
    has to be at least as conservative here as it is for evidence it can date.
    """

    undated_record = copy.deepcopy(record)
    undated_record["created_at"] = ""
    return undated_record


def _truncated_observations(records: list[dict], *, omitted: int = 1) -> dict:
    """The observations block a bounded read emits when it left files unread."""

    cap = board.MAX_OBSERVATION_FILES
    return {
        "schema": board.BOARD_OBSERVATIONS_SCHEMA,
        "record_schema": board_observation.SCHEMA,
        "available": True,
        "path": lane_status.LOCAL_PATH_REDACTION,
        "path_redacted": True,
        "path_exists": True,
        "records": records,
        "warnings": [],
        "rejected": 0,
        "coverage": "partial",
        "truncated": True,
        "file_cap": cap,
        "candidate_files": cap + omitted,
        "read_files": cap,
        "omitted_files": omitted,
        "selection": board.OBSERVATION_SELECTION,
        "message": f"{cap} of {cap + omitted} local Board observation files were read",
    }


@skipUnless(shutil.which("node"), "node is required to execute the shipped board renderer")
class BoardIdleFreshnessTests(TestCase):
    """An idle claim is a claim about now, so it needs current, whole evidence.

    A `no_work` record states that when it was written, the session, work queue
    and run registry were all observed complete and held no work. That is a
    fact about an instant in the past. Repeating it as "nothing to do in this
    session" turns it into a claim about the present, and that claim holds only
    while two things are true together: the evidence behind the record is still
    current, and the coverage behind it is whole -- every source covering all
    of what it covers, and every candidate observation file read this refresh.

    Before this, only the second half was checked. A record that was valid when
    written went on rendering green, as `idle with complete coverage` with
    `nothing to do in this session` beside it, for as long as the page was left
    open -- a day later, a week later, with `recordFreshness().current` false
    the whole time and the row's own age pill saying so. These tests hold the
    two halves together, one reading at a time.
    """

    NOW_MS = int(OBSERVATION_NOW.timestamp() * 1000)
    # The shipped text cues, read off the page rather than restated here.
    CUES = _eval_board_view("CUES")

    # freshness x coverage for one `no_work` record, and what the shared
    # classifier is allowed to say about each cell. Exactly one of the nine --
    # current evidence under whole coverage -- may speak in the present tense
    # or render as good news. Every other cell reports a prior observation.
    #
    # The third freshness reading covers both ways evidence stops being
    # datable or reachable: a source the record could not reach, and an
    # observation carrying no readable time at all. The contract ties an
    # unreachable source to unavailable coverage, so an undated record is the
    # only shape that reaches that row's complete-coverage column.
    MATRIX = (
        ("current", "complete", "current", "idle with complete coverage", "ok"),
        ("current", "partial", "partial", "last observed idle, coverage incomplete", "warn"),
        ("current", "truncated", "truncated", "idle in the files read", "warn"),
        ("stale", "complete", "stale", "last observed idle", "warn"),
        ("stale", "partial", "partial", "last observed idle, coverage incomplete", "warn"),
        ("stale", "truncated", "truncated", "idle in the files read", "warn"),
        (
            "unavailable/unknown",
            "complete",
            "unknown",
            "last observed idle at an unrecorded time",
            "muted",
        ),
        (
            "unavailable/unknown",
            "partial",
            "unavailable",
            "last observed idle, source unavailable",
            "bad",
        ),
        ("unavailable/unknown", "truncated", "truncated", "idle in the files read", "warn"),
    )

    # The two sentences that claim the session needs nothing right now, and the
    # cue that renders such a claim as good news.
    PRESENT_TENSE = ("idle with complete coverage", "nothing to do in this session")

    @staticmethod
    def _record(freshness: str, coverage: str) -> dict:
        record = _observation_fixture("no_work")
        if freshness == "stale":
            # A day later: every source still says it was fresh when the record
            # was written, and the record is far past the staleness threshold.
            record = _observed_later(record, -86400)
        if freshness == "unavailable/unknown":
            record = (
                _undated(record)
                if coverage == "complete"
                else _with_extra_source(record, freshness="unavailable", coverage="unavailable")
            )
        elif coverage == "partial":
            record = _with_extra_source(record, freshness="fresh", coverage="partial")
        return record

    @classmethod
    def _payload(cls, freshness: str, coverage: str) -> dict:
        record = cls._record(freshness, coverage)
        if coverage == "truncated":
            return _observation_payload([], observations=_truncated_observations([record]))
        return _observation_payload([record])

    def _rows(self, payload: dict, now_ms: int | None = None) -> list[dict]:
        return _eval_board_view(
            "workRows(ARGS[0], ARGS[1]).map(row => ({"
            "key: row.key, headline: row.headline, headline_class: row.headline_class,"
            "action: row.action_label, states: row.states, idle: row.idle || null,"
            "coverage: row.groups[0].items[0], freshness: row.freshness}))",
            payload,
            self.NOW_MS if now_ms is None else now_ms,
        )

    def test_only_current_evidence_under_whole_coverage_claims_idle_now(self) -> None:
        for freshness, coverage, reason, label, cls in self.MATRIX:
            with self.subTest(freshness=freshness, coverage=coverage):
                row = self._rows(self._payload(freshness, coverage))[0]
                affirmative = reason == "current"
                self.assertEqual(row["idle"]["reason"], reason)
                self.assertEqual(row["idle"]["affirmative"], affirmative)
                self.assertEqual(row["idle"]["coverage_state"], coverage)

                # One reading, read by the headline, the state cues, the next
                # action and the coverage evidence alike.
                self.assertEqual(row["headline"], label)
                self.assertEqual(row["headline_class"], cls)
                self.assertEqual(row["states"], [{"label": label, "class": cls, "cue": self.CUES[cls]}])

                if affirmative:
                    self.assertEqual(row["action"], "nothing to do in this session")
                    self.assertEqual(row["coverage"]["class"], "ok")
                    self.assertIn("This session is idle because", row["coverage"]["note"])
                    continue

                # Everything else reports a prior observation, says what is
                # missing, and is never styled as good news.
                self.assertIn("before treating", row["action"])
                self.assertNotIn("nothing to do", row["action"])
                self.assertNotEqual(row["coverage"]["class"], "ok")
                self.assertIn("not shown as idle", row["coverage"]["note"])
                self.assertIn("when it was written", row["coverage"]["note"])
                # The idle surfaces themselves: the headline, the cues, the
                # action and the coverage evidence. The age pill beside them
                # reports the record's own age and stays what it is -- a
                # recent record whose coverage is incomplete is recent.
                claimed = json.dumps(
                    [row["idle"], row["states"], row["coverage"], row["headline"], row["action"]]
                )
                for sentence in self.PRESENT_TENSE:
                    self.assertNotIn(sentence, claimed)
                self.assertNotIn('"ok"', claimed)

    def test_every_withheld_reading_carries_its_age_or_source_caveat(self) -> None:
        # A row that declines to repeat an idle claim has to say why, or an
        # operator is left with a bare label and no way to judge it.
        caveats = {
            ("current", "partial"): "reported part of what it covers",
            ("current", "truncated"): "went unread this refresh",
            ("stale", "complete"): "old and nothing has confirmed it since",
            ("stale", "partial"): "old and nothing has confirmed it since",
            ("stale", "truncated"): "old and nothing has confirmed it since",
            ("unavailable/unknown", "complete"): "No observation time is recorded",
            ("unavailable/unknown", "partial"): "Source unavailable: remote_session",
            ("unavailable/unknown", "truncated"): "Source unavailable: remote_session",
        }
        for (freshness, coverage), caveat in caveats.items():
            with self.subTest(freshness=freshness, coverage=coverage):
                row = self._rows(self._payload(freshness, coverage))[0]
                self.assertIn(caveat, row["coverage"]["note"])

    def test_the_freshness_threshold_decides_at_its_own_boundary(self) -> None:
        # The record is one record; only the clock moves. An observation is
        # current up to and including the threshold, and reports itself as a
        # past observation the first second after it.
        threshold = _eval_board_view("OBSERVATION_STALE_SECONDS")
        payload = _observation_payload([_observation_fixture("no_work")])
        for age, affirmative in (
            (threshold - 1, True),
            (threshold, True),
            (threshold + 1, False),
            (86400, False),
        ):
            with self.subTest(age=age):
                now_ms = int((FIXTURE_CREATED + timedelta(seconds=age)).timestamp() * 1000)
                row = self._rows(payload, now_ms)[0]
                self.assertEqual(row["idle"]["affirmative"], affirmative)
                self.assertEqual(
                    row["headline"],
                    "idle with complete coverage" if affirmative else "last observed idle",
                )
                self.assertEqual(row["freshness"]["current"], affirmative)

    def test_a_day_later_the_same_valid_record_states_no_present_tense_claim(self) -> None:
        # The P2 exactly: the page is left open, the producer stops writing,
        # and the record that was valid a day ago is still on screen. What it
        # may still say is that this session was observed idle, a day ago.
        payload = _observation_payload([_observation_fixture("no_work")])
        nodes = _render_board_dom(payload, now=OBSERVATION_NOW + timedelta(days=1))
        rendered = json.dumps(nodes)
        for sentence in self.PRESENT_TENSE:
            self.assertNotIn(sentence, rendered)
        worklist = nodes["worklist"]
        self.assertIn("last observed idle", worklist)
        self.assertIn("last observed 24.0h ago", worklist)
        self.assertIn("re-observe this session before treating it as idle", worklist)
        self.assertIn("so it is shown as last observed rather than current", worklist)
        self.assertIn("This reading is 24.0h old and nothing has confirmed it since", worklist)

        # And the same record read while it is current still says both.
        current = _render_board_dom(payload, now=OBSERVATION_NOW)["worklist"]
        for sentence in self.PRESENT_TENSE:
            self.assertIn(sentence, current)

    def test_a_prior_observation_never_outranks_work_that_is_moving(self) -> None:
        # Two sessions: one observed idle a day ago, one with a run observed
        # now. An operator who has chosen nothing is shown the work.
        stale_idle = _in_session(
            _observed_later(_observation_fixture("no_work"), -86400),
            session=OTHER_SESSION,
            worktree=OTHER_WORKTREE,
        )
        work = _named_work("alpha")
        rows = self._rows(_observation_payload([stale_idle, work]))
        self.assertEqual([row["key"] for row in rows], [_work_key("alpha"), _idle_key(OTHER_SESSION, OTHER_WORKTREE)])

        # Order is a property of what the rows record, never of the order the
        # directory happened to list them in.
        reversed_rows = self._rows(_observation_payload([work, stale_idle]))
        self.assertEqual(reversed_rows, rows)

        # A current idle snapshot is finished business and sorts below the
        # work; a prior observation sorts below the work too, and above the
        # terminal band, because its caveat still has to be read.
        ranks = _eval_board_view(
            "[stateUrgency('provider run observed'), stateUrgency('last observed idle'),"
            "stateUrgency('idle with complete coverage')]"
        )
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(len(set(ranks)), 3)

    def test_a_snapshot_that_cannot_claim_the_present_retires_no_work(self) -> None:
        # Reconciliation retires a work row by asserting that the session has
        # since gone quiet. A snapshot that may not make a present-tense claim
        # may not make that one either, however recently it was written.
        work = _observed_later(_named_work("alpha"), 0)
        unreachable = _with_extra_source(
            _observed_later(_observation_fixture("no_work"), 20),
            freshness="unavailable",
            coverage="unavailable",
        )
        rows = self._rows(_observation_payload([work, unreachable]))
        # Both readings stay, and they do not contradict each other: the
        # snapshot's row claims only a past observation.
        self.assertEqual([row["key"] for row in rows], [_work_key("alpha"), _idle_key()])
        self.assertEqual(rows[1]["headline"], "last observed idle, source unavailable")
        self.assertEqual(self._rows(_observation_payload([unreachable, work])), rows)

        # The prior fix is untouched: a snapshot that can claim the present
        # still retires the work it supersedes, in either input order.
        quiet = _observed_later(_observation_fixture("no_work"), 20)
        for records in ([work, quiet], [quiet, work]):
            reconciled = self._rows(_observation_payload(records))
            self.assertEqual([row["key"] for row in reconciled], [_idle_key()])
            self.assertEqual(reconciled[0]["action"], "nothing to do in this session")

    def test_without_the_freshness_gate_a_day_old_record_still_claims_idle(self) -> None:
        # The mutation is exactly the missing condition: coverage alone decides
        # whether the record may speak in the present tense, which is what the
        # Board did before.
        payload = _observation_payload([_observed_later(_observation_fixture("no_work"), -86400)])
        ungated = _eval_board_view(
            "workRows(ARGS[0], ARGS[1]).map(row => [row.headline, row.headline_class, row.action_label])",
            payload,
            self.NOW_MS,
            mutate=(
                'const affirmative = coverageState === "complete" && freshnessState === "current";',
                'const affirmative = coverageState === "complete";',
            ),
        )
        self.assertEqual(
            ungated,
            [["idle with complete coverage", "ok", "nothing to do in this session"]],
        )
        # And the shipped classifier, unmutated, says none of that about the
        # very same record.
        shipped = self._rows(payload)[0]
        self.assertEqual(shipped["headline"], "last observed idle")
        self.assertEqual(shipped["headline_class"], "warn")
        self.assertEqual(shipped["action"], "re-observe this session before treating it as idle")
