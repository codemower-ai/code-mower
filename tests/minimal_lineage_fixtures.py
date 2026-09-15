"""Isolated metadata examples for the declared lineage contract."""
from code_mower.builder_lineage import CONTINUATION_WRITER_STATE, Episode, Target

REPO = "owner/repo"
PR = 42
BRANCH = "Devin/42-work"
AUTHORITY = "owner"
OPENED = "a" * 40
TAKEN = "b" * 40


def head(index):
    return f"{index:040x}"


def target(**changes):
    return Target(**(dict(repo=REPO, pr_number=PR, branch=BRANCH, head_sha=TAKEN) | changes))


def episode(**changes):
    return Episode(**(dict(sequence=1, repo=REPO, pr_number=PR, branch=BRANCH,
                          source_lane="devin", destination_lane="codex", expected_head=OPENED,
                          resulting_head=TAKEN, writer_state="terminated") | changes))


def episodes(length):
    return [episode(resulting_head=head(1)), *[
        episode(sequence=i, kind="continuation", source_lane="codex",
                expected_head=head(i - 1), resulting_head=head(i),
                writer_state=CONTINUATION_WRITER_STATE) for i in range(2, length + 1)
    ]]


def identity_mapping(**changes):
    return dict(enabled=True,
                labels={"builder:devin": "devin", "builder:codex": "codex",
                        "builder:claude": "claude"},
                authors={"devin-bot[bot]": "devin", "codex-bot[bot]": "codex",
                         "claude-bot[bot]": "claude"},
                branch_prefixes={"devin/": "devin", "codex/": "codex", "claude/": "claude",
                                 "feature/cx-": "codex"},
                require_verified_lineage=True) | changes


def comment(body="ordinary comment", account=AUTHORITY, field="user"):
    return {field: {"login": account}, "body": body}


# Supported REST and GraphQL shapes, including deleted accounts and optional body.
VALID_COMMENTS = [
    {"user": None, "body": "deleted account"},
    {"user": {"login": AUTHORITY}},
    {"user": {}, "body": "no login"},
    {"body": "no author"},
    {"author": None, "body": "deleted account"},
    {"author": {"login": AUTHORITY}, "body": "ordinary comment"},
]


class BoundedArrivals:
    """The 562nd read is a bug, even when every earlier arrival is identical."""
    def __init__(self, value):
        self.value = value
        self.reads = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.reads += 1
        if self.reads > 561:
            raise AssertionError("raw arrival iterator overconsumed")
        return self.value
