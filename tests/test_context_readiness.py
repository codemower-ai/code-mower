"""Optional setup must be useful without exposing account or organization data."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from code_mower import cli, config, context_connections, context_readiness, init, session
from code_mower.context_contract import ContextError
from code_mower.context_store import ContextStore, NativeCredentialVault
from test_context_connections import FakeBackend, MemoryVault

ROOT = Path(__file__).resolve().parents[1]
STARTER = ROOT / 'src/code_mower/templates/code-mower.example.yml'


@unittest.skipUnless(os.name == 'posix', 'private storage needs POSIX')
class ContextReadinessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / 'private'
        self.vault = MemoryVault()
        self.backend = FakeBackend()
        self.store = ContextStore(self.root, vault=self.vault)
        self.policy = {'schema': 'code_mower.contextPolicy.v1', 'connection': 'private-alias',
                       'policy_version': 'v1', 'required': False}
        self.spec = {'principal': 'one@example.invalid', 'workspace': 'example-workspace',
                     'repositories': ['owner/repo'], 'recipients': ['codex:builder', 'claude:reviewer']}

    def create(self):
        context_connections.connect(self.store, 'private-alias', self.spec, backend=self.backend)

    def inspect(self, **kwargs):
        return context_readiness.inspect_connection(self.policy, store=self.store, backend=self.backend, **kwargs)

    def mutate(self, **kwargs):
        with self.store.locked('private-alias') as locked:
            state = locked.read()
            state.update(kwargs)
            locked.write(state)

    def assert_private(self, result):
        encoded = json.dumps(result)
        for value in ('one@example.invalid', 'example-workspace', 'private-alias', 'owner/repo',
                      'codex:builder', str(self.root), 'private provider', 'credentials', 'packet_sha256'):
            self.assertNotIn(value, encoded)

    def test_no_provider_needs_no_store_sdk_or_authorization(self):
        with mock.patch.object(context_readiness, 'ContextStore', side_effect=AssertionError), \
             mock.patch.object(context_readiness, 'authorize', side_effect=AssertionError), \
             mock.patch.object(context_readiness.importlib.util, 'find_spec', side_effect=AssertionError):
            self.assertEqual(context_readiness.inspect_connection()['readiness'], 'not_configured')
        self.assertFalse(self.root.exists())

    def test_offline_inspection_never_reads_credentials_or_refreshes(self):
        self.create()
        self.vault.unavailable = True
        self.assertEqual(self.inspect()['readiness'], 'unchecked')
        self.mutate(expires_at='2000-01-01T00:00:00+00:00')
        result = self.inspect()
        self.assertEqual(result['readiness'], 'stale')
        self.assertEqual(result['dependent_work'], 'usable')
        self.assertEqual(self.backend.calls, [])
        self.assert_private(result)

    def test_missing_and_malformed_required_connection_pause_only_dependent_work(self):
        self.policy['required'] = True
        result = self.inspect()
        self.assertEqual(result['readiness'], 'identity_unverified')
        self.assertEqual(result['dependent_work'], 'paused')
        self.assertTrue(result['owner_action'])
        self.assert_private(result)
        result = context_readiness.inspect_connection({'required': 'invalid', 'identity': 'one@example.invalid'})
        self.assertEqual(result['readiness'], 'unavailable')
        self.assertTrue(result['required'])
        self.assert_private(result)

    def test_explicit_online_verification_refreshes_once_and_never_searches(self):
        self.create()
        result = self.inspect(online=True, repository='owner/repo', recipients=['claude:reviewer'])
        self.assertEqual(result['readiness'], 'incomplete')
        self.assertEqual(result['authorization'], 'verified_online')
        self.assertEqual(self.backend.calls, ['refresh'])
        self.mutate(capability_status={'search': 'available', 'memory': 'available'})
        self.assertEqual(self.inspect(online=True)['readiness'], 'ready')
        self.assertEqual(self.backend.calls, ['refresh', 'refresh'])
        self.assert_private(result)

    def test_wrong_destination_is_denied_before_network_and_never_uses_host_account(self):
        self.create()
        for kwargs in ({'repository': 'owner/other-repo'}, {'recipients': ['codex:orchestrator']}):
            self.assertEqual(self.inspect(online=True, **kwargs)['readiness'], 'unauthorized')
        self.assertEqual(self.backend.calls, [])

    def test_revocation_and_wrong_identity_are_redacted_and_invalidate_saved_access(self):
        for kind in ('revoked', 'wrong_identity'):
            with self.subTest(kind=kind):
                if not self.root.exists():
                    self.create()
                setattr(self.backend, kind, True)
                result = self.inspect(online=True)
                self.assertEqual(result['readiness'], 'unauthorized')
                self.assert_private(result)
                setattr(self.backend, kind, False)
        with self.store.locked('private-alias') as locked:
            self.assertEqual(locked.read()['state'], 'needs_auth')

    def test_cli_json_text_and_status_share_redacted_readiness(self):
        self.create()
        with mock.patch.object(context_readiness, 'ContextStore', return_value=self.store):
            for format_args in ([], ['--json']):
                out = io.StringIO()
                with redirect_stdout(out):
                    result = cli.main(['context', 'doctor', '--connection', 'private-alias', *format_args])
                self.assertEqual(result, 0)
                self.assertIn('unchecked', out.getvalue())
                self.assert_private(out.getvalue())
        self.assertEqual(context_connections.status(self.store, 'private-alias')['readiness'], 'unchecked')

    def test_identity_requires_explicit_local_terminal_and_rejects_json(self):
        self.create()
        args = ['context', 'identity', '--connection', 'private-alias', '--local-only']
        with mock.patch.object(context_connections, 'ContextStore', return_value=self.store):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(cli.main(args), 1)
            self.assertEqual(out.getvalue(), '')
            self.assert_private(err.getvalue())
            with redirect_stdout(out), mock.patch.object(out, 'isatty', return_value=True):
                self.assertEqual(cli.main(args), 0)
            self.assertIn(self.spec['principal'], out.getvalue())
            self.assertIn(self.spec['workspace'], out.getvalue())
            self.assertNotIn('credential', out.getvalue())
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main([*args, '--json'])
        self.assertEqual(self.backend.calls, [])

    def test_general_doctor_board_and_explicit_report_bundle_do_not_collect_private_context(self):
        from code_mower import board, lane_status
        from code_mower.cloud_client import build_cloud_bundle, CloudBundleError
        from code_mower.doctor_checks.runner import run_doctor
        from test_board import _command_runner
        self.create()
        source = config.load_config(STARTER)
        plan = init.render_init_plan(source, config_path=str(STARTER), package_mode=True,
                                     context_connection='private-alias')
        generated = self.root.parent / 'generated'
        init.apply_init_plan(plan, generated)
        with mock.patch.object(context_readiness, 'ContextStore', return_value=self.store):
            report = run_doctor(config_path=generated / 'code-mower.yml',
                provider_templates_path=ROOT / 'src/code_mower/templates/providers.yml', profile='recommended')
        check = next(check for check in report.checks if check.name == 'context.readiness')
        self.assertEqual(check.status, 'warn')
        self.assertEqual(check.detail['readiness'], 'unchecked')
        self.assert_private(check.as_dict())
        shared = self.root.parent / 'context-readiness.json'
        shared.write_text(json.dumps(check.as_dict()))
        bundle = self.root.parent / 'bundle'
        with self.assertRaises(CloudBundleError):
            build_cloud_bundle(reports=[(shared, 'context-readiness')], output_dir=bundle, anonymous=True)
        bundle = self.root.parent / 'empty-bundle'
        build_cloud_bundle(reports=[], output_dir=bundle, anonymous=True)
        for file in bundle.rglob('*'):
            if file.is_file():
                self.assert_private(file.read_text())
        def offline(_args):
            raise lane_status.LaneStatusUnavailable('offline')
        board_result = board.doctor_payload(board.BoardConfig(repo='owner/repo', repo_path=str(generated)),
            gh_json_runner=offline, command_runner=_command_runner)
        for private_value in (self.spec['principal'], self.spec['workspace'], 'private-alias', str(self.root)):
            self.assertNotIn(private_value, json.dumps(board_result))
        self.assertEqual(self.backend.calls, [])

    def test_unsupported_os_vault_has_specific_safe_diagnostic_for_all_operations(self):
        with mock.patch('code_mower.context_store.sys.platform', 'unsupported'):
            for method, args in (('get', ('a' * 32,)), ('put', ('a' * 32, {})), ('delete', ('a' * 32,))):
                with self.assertRaisesRegex(ContextError, 'macOS Keychain or Linux Secret Service'):
                    getattr(NativeCredentialVault(), method)(*args)
        vault = NativeCredentialVault()
        vault.__dict__['_backend'] = mock.Mock()
        vault._backend.get_password.side_effect = ContextError('one@example.invalid')
        with self.assertRaises(ContextError) as error:
            vault.get('a' * 32)
        self.assertNotIn('one@example.invalid', str(error.exception))


class ContextSetupTests(unittest.TestCase):
    def test_init_round_trip_select_require_and_remove_preserves_participants(self):
        source = config.load_config(STARTER)
        original = init.render_init_plan(source, config_path=str(STARTER), package_mode=True)
        selected = init.render_init_plan(source, config_path=str(STARTER), package_mode=True,
                                        context_connection='example-context')
        self.assertNotIn('context', source)
        self.assertEqual(selected.data['profile'], original.data['profile'])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'generated'
            init.apply_init_plan(selected, out)
            saved = config.load_config(out / 'code-mower.yml')
            self.assertEqual(saved['context']['connection'], 'example-context')
            self.assertFalse(saved['context']['required'])
            self.assertEqual(config.validate_config(saved), [])
            for kwargs, expected in (({'context_required': True}, True), ({'without_context': True}, None)):
                plan = init.render_init_plan(saved, config_path=str(out / 'code-mower.yml'), **kwargs)
                proposed = next(item['config_data'] for item in plan.data['generated_files'] if item['path'] == 'code-mower.yml')
                self.assertEqual(proposed.get('context', {}).get('required'), expected)
            session_value = session.build_session(repo='owner/repo', host='codex', selected=('claude', 'codex'), config=saved)
            self.assertEqual(session_value['context']['readiness'], 'unchecked')
            self.assertNotIn('example-context', json.dumps(session_value))

    def test_default_init_and_session_do_not_create_optional_context_requirements(self):
        source = config.load_config(STARTER)
        with mock.patch.object(context_readiness, 'ContextStore', side_effect=AssertionError):
            result = session.build_session(repo='owner/repo', host='codex', selected=('claude', 'codex'), config=source)
            self.assertNotIn('context', result)
        with self.assertRaises(config.ConfigError):
            init.render_init_plan(source, context_connection='example', without_context=True)
        with self.assertRaises(config.ConfigError):
            init.render_init_plan(source, context_required=True)
