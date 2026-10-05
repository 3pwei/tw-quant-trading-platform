"""Reject old archived package bytes even under a new pipeline identity."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

STAGING = Path(__file__).resolve().parents[1] / 'deploy/staging'
spec = importlib.util.spec_from_file_location('runtime_source', STAGING / 'runtime_source.py')
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)
spec = importlib.util.spec_from_file_location('candidate_manifest', STAGING / 'candidate_manifest.py')
manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest)


class RuntimeSourceTests(unittest.TestCase):
    def test_archived_bytes_and_generation_fix_must_match_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / 'tw_quant/execution_service/health.py'
            file.parent.mkdir(parents=True)
            file.write_text('def new_generation(): return "current"\n')
            receipt = {'platform_source_sha': 'a' * 40, 'files': {
                str(file.relative_to(root)): hashlib.sha256(file.read_bytes()).hexdigest()}}
            source.verify(root, receipt, 'a' * 40)
            with self.assertRaises(ValueError): source.verify(root, receipt, 'b' * 40)
            file.write_text('# old runtime without generation\n')
            with self.assertRaises(ValueError): source.verify(root, receipt, 'a' * 40)
            file.unlink()
            with self.assertRaises(ValueError): source.verify(root, receipt, 'a' * 40)

    def test_receipt_create_and_container_import_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'tw_quant'; package.mkdir()
            (package / '__init__.py').write_text('')
            (root / 'public-candidate.json').write_text(json.dumps({'files': [
                {'path': 'tw_quant/__init__.py', 'sha256': hashlib.sha256(b'').hexdigest()}]}))
            script = root / 'runtime_source.py'
            script.write_bytes((STAGING / 'runtime_source.py').read_bytes())
            def run(command):
                return subprocess.run([sys.executable, str(script), command, '--root', str(root),
                    '--revision', 'a' * 40], capture_output=True, text=True)
            self.assertEqual(run('create').returncode, 0)
            self.assertEqual(run('verify').returncode, 0)
            (package / 'unexpected.py').write_text('')
            self.assertNotEqual(run('verify').returncode, 0)

    def test_manifest_rejects_old_source_under_new_pipeline(self):
        # Use the established complete image/provenance fixture.
        from test_p8_recovery_provenance import candidate
        good = candidate()
        manifest.validate(good)
        for key, value in (('platform_source_sha', manifest.IDENTITIES['P7_PLATFORM_SOURCE_SHA']),
                           ('p7_platform_source_sha', '2' * 40), ('schema_version', 1)):
            bad = copy.deepcopy(good); bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): manifest.validate(bad)


if __name__ == '__main__': unittest.main()
