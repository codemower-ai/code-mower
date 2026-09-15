"""Finite external-I/O fixtures for the staged producer's real contracts."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from code_mower.builder_lineage import Authorities, Chain, Episode, History, Identity, Target, render
from code_mower.builder_lineage_producer import Snapshot, Transport

REPO = "owner/repo"
BRANCH = "codex/Topic"
AUTHORITY = Authorities(["lineage-publisher[bot]"])
TRANSPORT = Transport("claude", "claude", "claude_cli", "local_cli")
POLICY = Identity.from_mapping({"enabled": True,
    "authors": {"source-bot": "codex"},
    "labels": {"builder:codex": "codex", "builder:claude": "claude", "builder:devin": "devin"},
    "branch_prefixes": {"codex/": "codex"}, "require_verified_lineage": True})


def sha(n):
    return f"{n:040x}"


def target(n=1, **changes):
    return Target(**(dict(repo=REPO, pr_number=42, branch=BRANCH, head_sha=sha(n)) | changes))


def episode(n=1, **changes):
    return Episode(**(dict(sequence=n, repo=REPO, pr_number=42, branch=BRANCH,
        source_lane="codex" if n == 1 else "claude", destination_lane="claude",
        expected_head=sha(n-1), resulting_head=sha(n), writer_state="terminated" if n == 1 else "same_writer",
        kind="handoff" if n == 1 else "continuation") | changes))


def comments(episodes, *, author="lineage-publisher[bot]"):
    episodes = list(episodes)
    bound = Chain.from_arrivals(target(episodes[-1].sequence), episodes)
    return [{"user": {"login": author}, "body": render(bound)}]


def observation_args(n=1):
    return dict(target=target(n), identity=POLICY, authorities=AUTHORITY,
                history=History([]), private=[episode(i) for i in range(1, n+1)],
                author="source-bot", labels=["builder:codex"])


class MemoryStore:
    """Stub only private storage I/O; all producer validation remains real."""
    records = None
    effects = None

    def __init__(self, root):
        self.root = str(root)

    @contextmanager
    def locked(self, name):
        store = self
        key = (self.root, name)
        class Locked:
            def read(self):
                return deepcopy(store.records.get(key))
            def write(self, value):
                store.effects.append("write")
                store.records[key] = deepcopy(value)
        yield Locked()


class GitHubIO:
    def __init__(self, n=1):
        self.target = target(n)
        self.current_labels = ("builder:codex", "keep")
        self.public = []
        self.effects = []
        self.snapshots = 0
        self.reads = 0
        self.fail_snapshot = None
        self.readback = None
        self.fail_post = False

    def snapshot(self, requested):
        self.snapshots += 1
        self.effects.append("snapshot")
        if self.snapshots == self.fail_snapshot:
            raise OSError("unreadable")
        return Snapshot(self.target, "source-bot", self.current_labels)

    def history(self, requested):
        self.reads += 1
        self.effects.append("history")
        value = self.public if self.reads == 1 or self.readback is None else self.readback
        return History(deepcopy(value))

    def post(self, requested, body):
        self.effects.append("post")
        if self.fail_post:
            raise OSError("unavailable")
        self.public.append({"user": {"login": "lineage-publisher[bot]"}, "body": body})

    def labels(self, requested, desired, remove, add):
        self.effects.append("labels")
        self.current_labels = tuple(s for s in self.current_labels if s not in remove)
        if add:
            self.current_labels += (desired,)


def round_fixture(root, n=1, writer="destination-writer", config=None, runtime="ready"):
    from code_mower.lane_delivery import LineageRound
    return LineageRound(Path(root), f"round-{n}", writer, target(n-1), TRANSPORT,
        Path(root) / "checkout", config=config or {}, runtime_observation=lambda: runtime)
