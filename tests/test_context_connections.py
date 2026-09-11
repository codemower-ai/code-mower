"""Account isolation, local invalidation, and concurrent rotating credentials."""

import copy
import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from code_mower.context_connections import authorize, connect, disconnect, status
from code_mower.context_contract import ContextError
from code_mower.context_store import ContextStore, strict_json


class MemoryVault:
    def __init__(self):
        self.values = {}
        self.unavailable = False

    def get(self, key):
        if self.unavailable:
            raise RuntimeError("private vault diagnostic must not escape")
        return copy.deepcopy(self.values.get(key))

    def put(self, key, value):
        if self.unavailable:
            raise RuntimeError("private vault diagnostic must not escape")
        self.values[key] = copy.deepcopy(value)

    def delete(self, key):
        if self.unavailable:
            raise RuntimeError("private vault diagnostic must not escape")
        self.values.pop(key, None)


class FakeBackend:
    def __init__(self):
        self.sequence = {}
        self.wrong_identity = False
        self.revoked = False
        self.calls = []

    def proof(self, expected, sequence):
        return SimpleNamespace(
            principal="wrong@example.invalid" if self.wrong_identity else expected["principal"],
            workspace=expected["workspace"], subject=expected.get("subject", expected["principal"]),
            expires_at=int(time.time()) + 3600,
            credentials={"account": expected["principal"], "sequence": sequence},
        )

    def login(self, expected, open_url):
        self.sequence[expected["principal"]] = 0
        return self.proof(expected, 0)

    def refresh(self, expected, credentials):
        self.calls.append("refresh")
        if self.revoked:
            raise RuntimeError("private provider diagnostic must not escape")
        account = credentials["account"]
        if credentials["sequence"] != self.sequence[account]:
            raise RuntimeError("rotated credential was reused")
        time.sleep(0.01)
        self.sequence[account] += 1
        return self.proof(expected, self.sequence[account])

    def revoke(self, credentials):
        self.calls.append("revoke")
        if self.revoked:
            raise RuntimeError("private revoke failure")


@unittest.skipUnless(os.name == "posix", "private context store needs POSIX protections")
class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "private"
        self.vault = MemoryVault()
        self.store = ContextStore(self.root, vault=self.vault)
        self.backend = FakeBackend()
        self.spec = {"principal": "one@example.invalid", "workspace": "example-one",
                     "repositories": ["owner/repo"], "recipients": ["codex:builder", "claude:reviewer"]}

    def create(self, name="example", spec=None):
        return connect(self.store, name, spec or self.spec, backend=self.backend)

    def read(self, name="example"):
        with self.store.locked(name) as locked:
            return locked.read()

    def test_accounts_and_workspaces_do_not_share_credentials_or_disconnect(self):
        self.create()
        two = {**self.spec, "principal": "two@example.invalid", "workspace": "example-two"}
        self.create("second", two)
        first, second = self.read(), self.read("second")
        self.assertNotEqual(first["credential_id"], second["credential_id"])
        result = disconnect(self.store, "example", backend=self.backend)
        self.assertEqual(result["status"], "disconnected")
        self.assertEqual(result["remote_revocation"], "confirmed")
        self.assertNotIn(first["credential_id"], self.vault.values)
        self.assertIn(second["credential_id"], self.vault.values)
        self.assertEqual(authorize(self.store, "second", backend=self.backend)["identity"]["workspace"], "example-two")
        with self.assertRaises(ContextError):
            authorize(self.store, "example", backend=self.backend, explicit_retry=True)

    def test_wrong_identity_rejects_login_and_refresh(self):
        self.backend.wrong_identity = True
        with self.assertRaises(ContextError):
            self.create()
        self.assertFalse(self.vault.values)
        self.backend.wrong_identity = False
        self.create()
        generation = self.read()["generation"]
        self.backend.wrong_identity = True
        with self.assertRaises(ContextError):
            authorize(self.store, "example", backend=self.backend)
        self.assertEqual(self.read()["state"], "needs_auth")
        self.assertNotEqual(self.read()["generation"], generation)

    def test_rotating_refresh_is_serialized_and_does_not_rotate_packet_generation(self):
        self.create()
        generation = self.read()["generation"]
        failures = []
        def run():
            try:
                authorize(self.store, "example", backend=self.backend)
            except Exception as exc:
                failures.append(type(exc).__name__)
        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.backend.sequence[self.spec["principal"]], 4)
        self.assertEqual(self.read()["generation"], generation)

    def test_revoked_refresh_invalidates_old_evidence_and_requires_explicit_retry(self):
        self.create()
        before = self.read()["generation"]
        self.backend.revoked = True
        with self.assertRaisesRegex(ContextError, "cached evidence is invalid") as raised:
            authorize(self.store, "example", backend=self.backend)
        self.assertNotIn("private provider", str(raised.exception))
        self.assertNotEqual(before, self.read()["generation"])
        self.backend.revoked = False
        with self.assertRaises(ContextError):
            authorize(self.store, "example", backend=self.backend)
        restored = authorize(self.store, "example", backend=self.backend, explicit_retry=True)
        self.assertNotEqual(before, restored["generation"])

    def test_disconnect_commits_tombstone_even_when_vault_and_remote_are_unavailable(self):
        self.create()
        before = self.read()["generation"]
        self.vault.unavailable = True
        result = disconnect(self.store, "example", backend=self.backend)
        self.assertEqual(result["status"], "disconnected")
        self.assertEqual(result["credential_cleanup"], "needs_attention")
        self.assertNotEqual(before, self.read()["generation"])
        self.assertEqual(self.read()["state"], "disconnected")

    def test_offline_status_is_redacted_and_not_authorization(self):
        result = self.create()
        current = status(self.store, "example")
        self.assertEqual(current["authorization"], "unchecked")
        self.assertEqual(current["memory"], "unverified")
        encoded = json.dumps([result, current])
        for value in [self.spec["principal"], self.spec["workspace"], "owner/repo", "codex:builder", str(self.root)]:
            self.assertNotIn(value, encoded)
        self.assertEqual(self.backend.calls, [])

    def test_verified_connection_cannot_be_overwritten_with_new_scope(self):
        self.create()
        before = self.read()
        with self.assertRaises(ContextError):
            self.create(spec={**self.spec, "repositories": ["owner/other-repo"]})
        self.assertEqual(before, self.read())

    def test_failed_authorization_requires_disconnect_before_reconnecting(self):
        self.create()
        self.backend.revoked = True
        with self.assertRaises(ContextError):
            authorize(self.store, "example", backend=self.backend)
        before = self.read()
        self.backend.revoked = False
        other = {**self.spec, "principal": "two@example.invalid", "workspace": "example-two",
                 "repositories": ["owner/other-repo"], "recipients": ["claude:builder"]}
        for spec in (self.spec, other):
            with self.assertRaisesRegex(ContextError, "disconnect"):
                self.create(spec=spec)
            self.assertEqual(before, self.read())
        disconnect(self.store, "example", backend=self.backend)
        self.create(spec=other)
        self.assertEqual(self.read()["identity"]["principal"], other["principal"])
        self.assertNotEqual(before["generation"], self.read()["generation"])

    def test_top_level_cli_dispatches_private_connect_and_status(self):
        from code_mower.cli import main
        payload = SimpleNamespace(buffer=io.BytesIO(json.dumps(self.spec).encode()))
        out = io.StringIO()
        with patch("code_mower.context_connections.ContextStore", return_value=self.store), \
                patch("code_mower.context_connections._backend", return_value=self.backend), \
                patch("sys.stdin", payload), redirect_stdout(out):
            self.assertEqual(main(["context", "connect", "coworker", "--connection", "example", "--spec-stdin", "--json"]), 0)
            self.assertEqual(main(["context", "status", "--connection", "example", "--json"]), 0)
        states = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([item["status"] for item in states], ["verified", "verified"])
        self.assertEqual(states[1]["authorization"], "unchecked")

    def test_state_and_lock_symlinks_and_permissions_are_rejected(self):
        self.create()
        outside = self.root.parent / "outside"
        outside.write_text("private")
        for filename in ["example.json", "example.lock"]:
            original = self.root / filename
            saved = original.read_bytes()
            original.unlink()
            original.symlink_to(outside)
            with self.assertRaises(ContextError):
                status(self.store, "example")
            original.unlink()
            original.write_bytes(saved)
            original.chmod(0o600)
        self.root.chmod(0o755)
        with self.assertRaises(ContextError):
            status(self.store, "example")
        self.root.chmod(0o700)
        self.assertEqual(outside.read_text(), "private")

    def test_store_inside_repository_is_rejected(self):
        (self.root.parent / ".git").mkdir()
        with self.assertRaisesRegex(ContextError, "outside Git"):
            self.create()
        self.assertFalse(self.vault.values)

    def test_parser_rejects_duplicate_fields_and_nonfinite_numbers(self):
        for raw in ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '[]']:
            with self.assertRaises(ContextError):
                strict_json(raw)

    def test_hardlinked_state_and_symlinked_parent_are_rejected(self):
        self.create()
        os.link(self.root / "example.json", self.root.parent / "copied-state")
        with self.assertRaises(ContextError):
            status(self.store, "example")
        alias = self.root.parent / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ContextError):
            status(ContextStore(alias, vault=self.vault), "example")

    def test_cli_status_and_error_output_stay_redacted_without_loading_sdk(self):
        from code_mower.context_connections import main
        self.create()
        out, err = io.StringIO(), io.StringIO()
        with patch("code_mower.context_connections.ContextStore", return_value=self.store), \
                patch("code_mower.context_connections._backend", side_effect=AssertionError("offline")), \
                redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(main(["status", "--connection", "example", "--json"]), 0)
            self.assertEqual(main(["status", "--connection", "missing", "--json"]), 1)
        self.assertEqual(json.loads(out.getvalue())["authorization"], "unchecked")
        self.assertEqual(json.loads(err.getvalue())["status"], "unavailable")
        for private in [self.spec["principal"], self.spec["workspace"], str(self.root), "credential_id"]:
            self.assertNotIn(private, out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
