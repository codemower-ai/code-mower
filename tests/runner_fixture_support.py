"""Offline Codex sandbox adapter for runner wiring fixtures.

The separate explicit installed-Codex rehearsal verifies actual OS enforcement.
This adapter acknowledges only the runner's disposable capability probe, never
executes a model or the supplied Python command.
"""
from pathlib import Path
import shlex
import sys


def enable_fake_codex_sandbox(path: Path) -> None:
    program = """
import ast
from pathlib import Path
import sys
script = ast.parse(sys.argv[-1])
first = script.body[1].value.func.value.args[0]
marker = Path(ast.literal_eval(first))
assert marker.parent.name == '.git' and marker.name.startswith('code-mower-capability-')
marker.write_text('probe')
"""
    original = path.read_text()
    first, rest = original.split('\n', 1)
    path.write_text(first + '\nif [ "${1:-}" = "sandbox" ]; then\n  ' + shlex.quote(sys.executable)
                    + ' -c ' + shlex.quote(program) + ' "$@"\n  exit $?\nfi\n' + rest)
    path.chmod(0o755)


def fake_handoff_source(root: Path) -> Path:
    """Create a real private lifecycle binding backed by the offline adapter."""
    import json
    from code_mower.remote_session import FakeProvider, RemoteSessions
    state = root.resolve() / "remote-sessions"
    engine = RemoteSessions(state, FakeProvider(state / "fake-provider"))
    engine.run("dispatch", "source", prose="Implement bounded work", repo="owner/repo", apply=True)
    path = root / "handoff-source.json"
    path.write_text(json.dumps({"transport": "remote_session", "provider": "fake",
                                "session": "source", "state_dir": str(state)}))
    path.chmod(0o600)
    return path
