"""Exercise row selection with actual rendered source trees and canonical Bake."""

import hashlib
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from crossforge_internal import incremental_plan

CLI = runpy.run_path(str(ROOT / 'scripts/ci-component-plan.py'))


class PythonAppendScopeTests(unittest.TestCase):
    def test_real_cp39_patch_change_preserves_other_independent_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'source'
            source.mkdir()
            for path in ROOT.iterdir():
                if path.name in ('.git', 'build', '.codex', '.agents', '__pycache__'):
                    continue
                if path.is_dir():
                    shutil.copytree(path, source / path.name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                elif path.is_file():
                    shutil.copy2(path, source / path.name)
            stages = CLI['stage_catalog']()
            before = CLI['capture_source'](source, stages)
            originals = {str(path.relative_to(source)): path.read_bytes()
                         for path in source.rglob('*') if path.is_file()}
            patch_name = 'patches/cpython/3.9/0001-gh-115382-isolate-target-sysconfig.patch'
            patch = source / patch_name
            patch.write_bytes(b'Local material-selection regression; patch hunks unchanged.\n' + patch.read_bytes())
            release = source / 'config/release.json'
            value = json.loads(release.read_text())
            entry = next(item for item in value['python']['versions'] if item['version'].startswith('3.9.'))
            self.assertEqual(entry['patches'][0]['file'], patch_name)
            entry['patches'][0]['sha256'] = hashlib.sha256(patch.read_bytes()).hexdigest()
            release.write_text(json.dumps(value, indent=2) + '\n')
            for script in ('render-release-components.py', 'render-vcpkg-integration.py', 'render-bake.py'):
                subprocess.run([sys.executable, str(source / 'scripts' / script)], cwd=str(source),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            after = CLI['capture_source'](source, stages)
            changed = sorted(name for name, data in originals.items() if (source / name).read_bytes() != data)
            plan = incremental_plan.select(before, after, changed)
            self.assertEqual(plan['mode'], 'incremental')
            self.assertFalse(plan['fallback_reasons'])
            self.assertEqual(set(plan['targets']), {'inputs', 'python-cp39', 'sdk'})
            self.assertEqual(plan['targets']['python-cp39'], ['python-cp39-dev'])
            self.assertEqual(plan['compiler_inputs_changed'],
                             ['cpython-build-cp39', 'cpython-cross-cp39-aarch64', 'cpython-cross-cp39-x86_64'])
            for row in ('cp310', 'cp311', 'cp312', 'cp313', 'cp314'):
                self.assertEqual(before['nodes']['python-' + row + '-dev'], after['nodes']['python-' + row + '-dev'])
            self.assertIn('config/release.json', {item['path'] for item in after['nodes']['python-dev']['files']})


if __name__ == '__main__':
    unittest.main()
