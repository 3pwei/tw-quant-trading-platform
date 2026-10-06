"""P9 market-only approval, exact-image and secret-free offline regressions."""
import copy
import zipfile
from urllib.error import HTTPError
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/production'))
import cutover
import evidence

PINS = json.loads((ROOT / 'deploy/production/approved-p8.json').read_text())
CODE = '^accepted-runtime-market-capability$'
LABELS = {'io.tw-quant.capability.market.shioaji': 'true', 'io.tw-quant.shioaji.version': '1.7.4'}


class ApprovalTests(unittest.TestCase):
    def test_accepted_shioaji_is_bound_to_actual_approved_artifact_checksums(self):
        self.assertEqual(PINS['platform_sha'], 'fac88646fe2f40680c60f8d61c7da385ae7f91b0')
        self.assertEqual(PINS['candidate_run_id'], '37459015384')
        self.assertEqual(PINS['staging_run_id'], '37462725158')
        self.assertEqual(PINS['candidate_artifact']['id'], 11410933865)
        self.assertEqual(PINS['candidate_artifact']['digest'], 'sha256:0481499d37dc6e208e71e63edc12f4182a4e6f6f31ee4e4b6988cd8f55d4cb15')
        self.assertEqual(PINS['acceptance_artifact']['id'], 11417625183)
        self.assertEqual(PINS['acceptance_artifact']['digest'], 'sha256:e28ab8c6c4608f9eee309d40b95b64e71d705a66a5de11a015609e7536c716c6')
        self.assertEqual(PINS['acceptance_files']['ledger.jsonl'], '46d426306ed6014d236ab0649252164cda0e907aa6f11c9666fb25ffb391cfbd')
        payload = (json.dumps(PINS['manifest'], indent=2, sort_keys=True) + '\n').encode()
        evidence.verify_manifest(payload, PINS)
        self.assertEqual(PINS['manifest_sha256'], PINS['acceptance_files']['candidate-manifest.json'])
        self.assertNotIn('market_capabilities', PINS['manifest'])
        self.assertEqual(cutover.market_compatibility({'MARKET_DATA_PROVIDER': 'shioaji'}, PINS), 'shioaji')

    def test_old_p8_identities_missing_capability_wrong_version_or_binding_cannot_authorize(self):
        bad_records = []
        for key, value in [('platform_sha', 'f70c1ba2fbfd6f5deff3178361d18f576af94b24'),
                           ('candidate_run_id', '37306446381'), ('staging_run_id', '37307300334'),
                           ('manifest_sha256', '0' * 64), ('release', 'candidate')]:
            bad = copy.deepcopy(PINS); bad[key] = value; bad_records.append(bad)
        bad = copy.deepcopy(PINS); bad.pop('market_capabilities'); bad_records.append(bad)
        for key, value in [('enabled', False), ('enabled', 'true'), ('version', '1.7.3'),
                           ('verified_candidate_run_id', '37306446381'), ('verified_staging_run_id', '37307300334'),
                           ('binding', {})]:
            bad = copy.deepcopy(PINS); bad['market_capabilities']['shioaji'][key] = value; bad_records.append(bad)
        for artifact in ('candidate_artifact', 'acceptance_artifact'):
            for key in ('id', 'digest', 'name'):
                bad = copy.deepcopy(PINS); bad[artifact][key] = 'wrong'; bad_records.append(bad)
        for name in PINS['acceptance_files']:
            bad = copy.deepcopy(PINS); bad['acceptance_files'][name] = '0' * 64; bad_records.append(bad)
        for bad in bad_records:
            with self.subTest(pins=bad), self.assertRaisesRegex(ValueError, CODE):
                cutover.market_compatibility({'MARKET_DATA_PROVIDER': 'shioaji'}, bad)

    def test_wrong_runtime_and_labels_fail_closed_and_replay_remains_compatible(self):
        runtime = PINS['manifest']['images']['releases']['known_good']['runtime']
        cutover.shioaji_capability(PINS, runtime, LABELS)
        for labels in ({}, {**LABELS, 'io.tw-quant.capability.market.shioaji': 'false'},
                       {**LABELS, 'io.tw-quant.shioaji.version': '1.7.3'}):
            with self.assertRaisesRegex(ValueError, CODE): cutover.shioaji_capability(PINS, runtime, labels)
        for runtime in (PINS['manifest']['images']['releases']['candidate']['runtime'],
                        {**runtime, 'config_digest': 'sha256:' + '0' * 64}):
            with self.assertRaisesRegex(ValueError, CODE): cutover.shioaji_capability(PINS, runtime, LABELS)
        for provider in ('unknown', 'broker', 'disabled'):
            with self.assertRaisesRegex(ValueError, CODE):
                cutover.market_compatibility({'MARKET_DATA_PROVIDER': provider}, PINS)
        for provider in ('mock', 'replay'):
            self.assertEqual(cutover.market_compatibility({'MARKET_DATA_PROVIDER': provider}), provider)


class ArtifactTests(unittest.TestCase):
    def fixture(self):
        pins = copy.deepcopy(PINS)
        files = {'candidate-manifest.json': (json.dumps(pins['manifest'], indent=2, sort_keys=True) + '\n').encode(),
                 'candidate-manifest.sha256': ('manifest_sha256=' + pins['manifest_sha256'] + '\n').encode()}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            for name, payload in files.items(): archive.writestr(name, payload)
        payload = buffer.getvalue(); pins['candidate_artifact']['digest'] = 'sha256:' + evidence.sha(payload)
        run = {'id': int(pins['candidate_run_id']), 'repository': {'full_name': pins['repository']},
               'head_sha': pins['platform_sha'], 'head_branch': 'master', 'path': '.github/workflows/staging-candidate.yml',
               'name': 'P8 Staging Candidate', 'event': 'workflow_dispatch', 'run_attempt': 1,
               'status': 'completed', 'conclusion': 'success'}
        artifact = {**pins['candidate_artifact'], 'expired': False,
                    'workflow_run': {'id': run['id'], 'head_sha': pins['platform_sha']},
                    'archive_download_url': 'https://api.github.com/repos/' + pins['repository'] + '/actions/artifacts/' + str(pins['candidate_artifact']['id']) + '/zip'}
        return pins, files, payload, run, artifact

    def archive(self, pins, payload, run, artifact):
        from unittest.mock import Mock
        opener = Mock(); opener.open.side_effect = HTTPError('fixture', 302, 'redirect',
            {'Location': 'https://fixture.blob.core.windows.net/archive'}, None)
        with patch.object(evidence, 'api', side_effect=[run, {'total_count': 1, 'artifacts': [artifact]}]), \
             patch.dict(evidence.os.environ, {'GITHUB_TOKEN': 'synthetic-token'}), \
             patch.object(evidence.urllib.request, 'build_opener', return_value=opener), \
             patch.object(evidence.urllib.request, 'urlopen', return_value=io.BytesIO(payload)) as download:
            result = evidence.archive(pins, 'candidate', pins['candidate_run_id'],
                '.github/workflows/staging-candidate.yml', 'P8 Staging Candidate')
            # Storage request must never inherit GitHub authorization headers.
            self.assertIsInstance(download.call_args.args[0], str)
            return result

    def test_exact_run_artifact_metadata_zip_checksum_and_manifest_bytes(self):
        pins, files, payload, run, artifact = self.fixture()
        self.assertEqual(self.archive(pins, payload, run, artifact), files)
        evidence.verify_manifest(files['candidate-manifest.json'], pins)
        for changes in ({'run_attempt': 2}, {'head_sha': '0' * 40}, {'conclusion': 'failure'}, {'id': 1}):
            with self.subTest(run=changes), self.assertRaisesRegex(ValueError, 'p8-run-mismatch'):
                self.archive(pins, payload, {**run, **changes}, artifact)
        for changes in ({'id': 1}, {'digest': 'sha256:' + '0' * 64}, {'expired': True},
                        {'workflow_run': {'id': 1, 'head_sha': pins['platform_sha']}}):
            with self.subTest(artifact=changes), self.assertRaisesRegex(ValueError, 'artifact-identity-mismatch'):
                self.archive(pins, payload, run, {**artifact, **changes})
        with self.assertRaisesRegex(ValueError, 'archive-checksum'):
            self.archive(pins, payload + b'corrupt', run, artifact)


class ConfigTests(unittest.TestCase):
    def config(self, root, *, provider='shioaji', execution=None, market_changes=None):
        market = {'MARKET_DATA_PROVIDER': provider, 'PLATFORM_ENVIRONMENT': 'production',
                  'PLATFORM_AUTHORIZATION_MODE': 'enforced', 'MARKET_ACCESS_MODE': 'cloudflare',
                  'MARKET_DB_PATH': '/data/platform.sqlite3',
                  'PRIVATE_PROVIDER_WHEEL_SHA256': PINS['manifest']['private_provider']['wheel_sha256']}
        if provider == 'shioaji':
            market.update(MARKET_SJ_API_KEY='synthetic-market-key', MARKET_SJ_SECRET_KEY='synthetic-market-secret',
                          MARKET_SJ_PRODUCTION='true')
        else: market['MARKET_REPLAY_CSV'] = '/run/production-market/replay.csv'
        market.update(market_changes or {})
        worker = {**{k: 'false' for k in cutover.DISABLED}, 'BROKER_PROVIDER': 'disabled',
                  'LIVE_EXECUTION_DB_PATH': '/data/platform.sqlite3',
                  'LIVE_EXECUTION_HEALTH_PATH': '/run/tw-quant-execution/health.json', **(execution or {})}
        (root / 'config').mkdir(exist_ok=True); (root / 'provider').mkdir(exist_ok=True)
        for name, env in [('market.env', market), ('execution.env', worker), ('gateway.env', {'MARKET_DOMAIN': 'production.example'})]:
            (root / 'config' / name).write_text(''.join(k + '=' + v + '\n' for k, v in env.items()))
        (root / 'config/replay.csv').write_text('sealed inactive fallback fixture\n')
        (root / 'provider/factory').write_text('fixture_provider:factory\n')
        for p in root.rglob('*'):
            if p.is_file(): p.chmod(0o600)
        return {name: cutover.file_sha(root / ('provider/factory' if name == 'factory' else 'config/' + name))
                for name in ('market.env', 'execution.env', 'gateway.env', 'factory', 'replay.csv')}

    def check(self, root, hashes):
        protected = cutover.protected
        with patch.object(cutover, 'ROOT', root), patch.object(cutover, 'protected',
                side_effect=lambda p, digest=None, uid=0: protected(p, digest, uid=p.stat().st_uid)):
            return cutover.config_check(PINS, hashes)

    def test_shioaji_market_credentials_and_disabled_execution_allowed_without_active_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); hashes = self.config(root)
            envs = self.check(root, hashes)
            self.assertNotIn('MARKET_REPLAY_CSV', envs['market.env'])
            self.assertEqual(envs['execution.env']['BROKER_PROVIDER'], 'disabled')
            self.assertEqual(set(hashes), {'market.env', 'execution.env', 'gateway.env', 'factory', 'replay.csv'})
            # Inactive fallback remains sealed, not a relaxed checksum check.
            (root / 'config/replay.csv').write_text('changed inventory')
            with self.assertRaisesRegex(ValueError, 'config-digest'): self.check(root, hashes)

    def test_missing_credentials_or_auth_fails_without_values_in_errors_or_logs(self):
        for key, value in [('MARKET_SJ_API_KEY', ''), ('MARKET_SJ_SECRET_KEY', ''),
                           ('MARKET_SJ_PRODUCTION', 'false'), ('MARKET_SJ_API_KEY', '   '),
                           ('PLATFORM_ENVIRONMENT', 'staging'), ('PLATFORM_AUTHORIZATION_MODE', 'disabled'),
                           ('MARKET_ACCESS_MODE', 'direct')]:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); hashes = self.config(root, market_changes={key: value})
                with patch('sys.stdout', new_callable=io.StringIO) as out, patch('sys.stderr', new_callable=io.StringIO) as err:
                    with self.assertRaises(ValueError) as raised: self.check(root, hashes)
                    output = str(raised.exception) + out.getvalue() + err.getvalue()
                    self.assertNotIn('synthetic-market', output)

    def test_shioaji_execution_or_leaked_market_credentials_hard_fail(self):
        changes = [{'BROKER_PROVIDER': 'shioaji'}, {'LIVE_TRADING_CONFIRMATION': 'LIVE'},
                   {'MARKET_SJ_API_KEY': 'synthetic-market-key'}]
        changes += [{k: 'true'} for k in cutover.DISABLED]
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); hashes = self.config(root, execution=change)
                with self.assertRaises(ValueError): self.check(root, hashes)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); hashes = self.config(root, market_changes={'BROKER_PROVIDER': 'shioaji'})
            with self.assertRaisesRegex(ValueError, 'execution-not-disabled'): self.check(root, hashes)

    def test_every_required_execution_flag_must_be_explicitly_false(self):
        for flag in cutover.DISABLED[:5]:
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); hashes = self.config(root)
                path = root / 'config/execution.env'
                path.write_text(path.read_text().replace(flag + '=false\n', ''))
                hashes['execution.env'] = cutover.file_sha(path)
                with self.assertRaisesRegex(ValueError, 'execution-not-disabled'): self.check(root, hashes)

    def test_replay_mock_sources_remain_valid_and_require_active_csv(self):
        for provider in ('mock', 'replay'):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); hashes = self.config(root, provider=provider)
                self.check(root, hashes)
                hashes = self.config(root, provider=provider, market_changes={'MARKET_REPLAY_CSV': ''})
                with self.assertRaisesRegex(ValueError, CODE): self.check(root, hashes)


class ExactImageTests(unittest.TestCase):
    def test_registry_config_provenance_and_capability_labels_checked_together(self):
        runtime = PINS['manifest']['images']['releases']['known_good']['runtime']
        m = PINS['manifest']
        labels = {**LABELS, 'org.opencontainers.image.revision': PINS['platform_sha'],
                  'io.tw-quant.pipeline.revision': PINS['platform_sha'],
                  'io.tw-quant.configuration.identity': m['images']['releases']['known_good']['configuration_identity'],
                  'io.tw-quant.core.sha256': m['core']['wheel_sha256'],
                  'io.tw-quant.private-provider.sha256': m['private_provider']['wheel_sha256'],
                  'io.tw-quant.p7.acceptance': m['private_provider']['p7_acceptance_sha']}
        d = {'Id': 'sha256:' + 'a' * 64, 'RepoDigests': [runtime['ref'].split(':', 1)[0] + '@' + runtime['digest']],
             'Config': {'Labels': labels}}
        class Process:
            def __init__(self): self.stdout = io.BytesIO()
            def wait(self, timeout): return 0
            def poll(self): return 0
        import image_config_digest
        with patch.object(cutover.subprocess, 'Popen', side_effect=lambda *a, **kw: Process()), \
             patch.object(image_config_digest, 'config_digest', return_value=runtime['config_digest']):
            with patch.object(cutover, 'run', return_value=json.dumps([d]).encode()):
                self.assertEqual(cutover.local_image(runtime, PINS), d['Id'])
            for key in labels:
                bad = copy.deepcopy(d); bad['Config']['Labels'][key] = 'wrong'
                with self.subTest(key=key), patch.object(cutover, 'run', return_value=json.dumps([bad]).encode()):
                    with self.assertRaisesRegex(ValueError, CODE if key in LABELS else 'image-provenance-mismatch'):
                        cutover.local_image(runtime, PINS)

    def test_offline_probe_exact_version_no_credentials_network_or_broker_calls(self):
        expected = {'P8_SHIOAJI_CAPABILITY': 'PASS', 'shioaji_version': '1.7.4',
                    'execution_locked': True, 'external_order_calls': 0, 'external_cancel_calls': 0}
        source = (ROOT / 'deploy/staging/shioaji_capability.py').read_bytes()
        with patch.object(Path, 'read_bytes', return_value=source), patch.object(cutover, 'run', return_value=json.dumps(expected).encode()) as run:
            cutover.offline_market_capability('sha256:' + 'a' * 64)
            argv, probe = run.call_args.args
            self.assertEqual(argv[argv.index('--network') + 1], 'none')
            self.assertEqual(argv[-3:], ['sha256:' + 'a' * 64, '-', 'probe'])
            for value in ('--env', '--env-file', '--mount', 'login'): self.assertNotIn(value, argv)
            self.assertIn(b'clear=True', probe); self.assertIn(b'forbidden_call', probe)
            for bad in ({**expected, 'shioaji_version': '1.7.3'}, {**expected, 'external_order_calls': 1},
                        {**expected, 'execution_locked': False}):
                with patch.object(cutover, 'run', return_value=json.dumps(bad).encode()), self.assertRaisesRegex(ValueError, CODE):
                    cutover.offline_market_capability('sha256:' + 'a' * 64)
            with patch.object(cutover, 'run', side_effect=RuntimeError('synthetic-private-value')), self.assertRaisesRegex(ValueError, CODE):
                cutover.offline_market_capability('sha256:' + 'a' * 64)

    def test_failed_capability_precedes_any_forward_directory_data_or_compose_deploy(self):
        host = cutover.Host(PINS, '', {}, b'', {})
        with patch.object(cutover, 'run') as run, patch.object(cutover, 'local_image', return_value='sha256:' + 'a' * 64), \
             patch.object(cutover, 'offline_market_capability', side_effect=ValueError('accepted-runtime-market-capability')), \
             patch.object(Path, 'mkdir') as mkdir, patch.object(cutover, 'compose') as compose:
            with self.assertRaisesRegex(ValueError, CODE): host.deploy()
            mkdir.assert_not_called(); compose.assert_not_called()
            run.assert_called_once_with(['docker', 'pull', PINS['manifest']['images']['releases']['known_good']['runtime']['ref']], timeout=600)


if __name__ == '__main__':
    unittest.main()
