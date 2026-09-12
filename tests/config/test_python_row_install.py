"""Original independent append graph with explicit OCI/BuildKit execution fixtures."""

import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import test_python_component_sdk as fixtures
from crossforge_internal import python_row_install as install
from crossforge_internal.identity import IdentityError, load_json

ROOT = fixtures.ROOT


class PythonRowInstallTests(unittest.TestCase):
    reference = fixtures.PythonComponentSdkTests.reference
    verified = fixtures.PythonComponentSdkTests.verified
    receipt = fixtures.PythonComponentSdkTests.receipt

    @classmethod
    def setUpClass(cls):
        cls.rows = fixtures.sdk.matrix(ROOT)
        cls.graph = json.loads(subprocess.check_output(
            ['docker', 'buildx', 'bake', '--print'] + ['python-' + row + '-dev' for row in cls.rows], cwd=ROOT))

    def patch(self, module, name, **kwargs):
        patcher = mock.patch.object(module, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def setUp(self):
        fixtures.PythonComponentSdkTests.setUp(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / 'installation'
        self.report = b'{"fixture":"independently verified row manifest"}\n'
        self.patch(install.python_components, 'verify', side_effect=self.verified)
        self.patch(install.component_build, 'verify_local', side_effect=lambda receipt, trusted, expected, *args:
                   self.reference(expected['component']))
        self.patch(install.component_qualification, 'load_json', side_effect=self.receipt)
        self.patch(install, 'load_json', side_effect=self.receipt)
        self.patch(install.python_qualification, 'inspection_identity', return_value={'fixture': 'inspection tools'})
        self.row_verifier = self.patch(install.python_qualification, 'verify_local', side_effect=lambda receipt, trusted, expected, *args:
            {'mode': 'verified-prior-execution', 'reference': self.reference(expected['component']),
             'qualification': {'producer': {'invocation': 'urn:crossforge:local:original'},
                              'coverage': {'manifest_sha256': hashlib.sha256(self.report).hexdigest()}}})
        self.environment = self.patch(install.qualification_execution, 'execution_identity', return_value=self.execution)
        self.build = self.patch(install.subprocess, 'run', side_effect=self.solve)
        self.progress_change = None
        self.report_change = False

    def solve(self, command, **kwargs):
        self.assertIn('--progress=rawjson', command)
        self.assertTrue(kwargs['check'])
        graph = load_json(self.output / 'installation.bake.json')
        exporter = graph['target']['independent-install-report']
        root = exporter['contexts']['sdk'][len('target:'):]
        row = root[len('python-'):-len('-dev')]
        stamp = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        vertices = [{'digest': 'sha256:' + str(index) * 64,
            'name': '[%s python-sdk-append %s/21] RUN fixture --row "%s"' % (root, index, row),
            'started': stamp, 'completed': stamp} for index in (1, 2)]
        if self.progress_change:
            vertices = self.progress_change(vertices)
        kwargs['stderr'].write(json.dumps({'vertexes': vertices}) + '\n')
        payload = Path(exporter['output'][0]['dest'])
        (payload / 'row.json').write_bytes(b'changed manifest' if self.report_change else self.report)

    def execute(self, row='cp39', graph=None):
        component = self.components['rows'][row]
        return install.execute(ROOT, graph or self.graph, row, self.execution, component['subjects'],
            component['qualification'], self.output, 'fixture-builder')

    def test_all_six_rows_keep_independent_base_two_fresh_runs_and_no_source_compilers(self):
        for row in self.rows:
            with self.subTest(row=row):
                self.output = self.output.parent / row
                result = self.execute(row)
                self.assertEqual(len(result['vertices']), 2)
                self.assertEqual(result['reused_row']['qualification']['producer']['invocation'], 'urn:crossforge:local:original')
                self.assertEqual(result['report']['sha256'], hashlib.sha256(self.report).hexdigest())
                captured = load_json(self.output / 'inputs.json')
                self.assertEqual({item['component'] for item in captured['dependencies']},
                    {'toolchain/x86_64-install', 'toolchain/aarch64-install', 'qualification/python-' + row})
                self.assertFalse({'scripts/build-gcc.sh', 'scripts/build-cpython-native.sh', 'scripts/build-cpython-cross.sh'} &
                                 {item['path'] for item in captured['files']})
                self.assertNotIn('config/release.json', {item['path'] for item in captured['files']})
                self.assertIn('config/generated/components/python/' + row + '-qualification.json',
                              {item['path'] for item in captured['files']})
                graph = load_json(self.output / 'installation.bake.json')
                self.assertEqual(graph['target']['python-' + row + '-dev']['contexts']['crossforge_sdk_base'],
                                 'target:sdk-toolchains-dev')
                for name, definition in graph['target'].items():
                    self.assertFalse(set(definition) & {'tags', 'attest', 'cache-to', 'no-cache'})
                    if name != 'independent-install-report':
                        self.assertEqual(definition['output'], [{'type': 'cacheonly'}])
                    self.assertEqual(definition.get('no-cache-filter'),
                                     ['python-sdk-append'] if name == 'python-' + row + '-dev' else None)

    def test_wrong_base_row_version_or_recipe_rejected_before_component_acceptance(self):
        for field, key, value in (('contexts', 'crossforge_sdk_base', 'target:python-dev-append-cp313'),
                ('contexts', 'crossforge_python_row', 'target:python-row-cp310'),
                ('args', 'CPYTHON_VERSION', '3.9.0'), ('args', 'CPYTHON_ADAPTER', 'modern')):
            graph = copy.deepcopy(self.graph)
            graph['target']['python-cp39-dev'][field][key] = value
            with self.subTest(key=key), self.assertRaises(IdentityError):
                self.execute(graph=graph)
        graph = copy.deepcopy(self.graph)
        graph['target']['python-cp39-dev']['dockerfile'] = 'docker/packaging.Dockerfile'
        with self.assertRaises(IdentityError):
            self.execute(graph=graph)
        self.row_verifier.assert_not_called()
        self.build.assert_not_called()

    def test_rejected_sealed_row_cannot_execute_installation(self):
        self.row_verifier.side_effect = IdentityError('fixture rejected original receipt or files')
        with self.assertRaises(IdentityError):
            self.execute()
        self.build.assert_not_called()
        self.assertFalse((self.output / 'result.json').exists())

    def test_missing_cached_failed_or_wrong_row_runs_never_count_as_success(self):
        changes = [lambda v: v[:1], lambda v: [dict(v[0], cached=True), v[1]],
                   lambda v: [dict(v[0], error='fixture failure'), v[1]],
                   lambda v: [dict(item, name=item['name'].replace('"cp39"', '"cp310"')) for item in v]]
        for index, change in enumerate(changes):
            self.output = self.output.parent / ('failed-' + str(index))
            self.progress_change = change
            with self.subTest(index=index), self.assertRaises(IdentityError):
                self.execute()
            self.assertTrue((self.output / 'execution.jsonl').exists())
            self.assertFalse((self.output / 'result.json').exists())

    def test_installed_manifest_change_keeps_failure_evidence_without_success(self):
        self.report_change = True
        with self.assertRaisesRegex(IdentityError, 'manifest differs'):
            self.execute()
        self.assertTrue((self.output / 'payload/row.json').exists())
        self.assertFalse((self.output / 'result.json').exists())

    def test_environment_change_before_or_after_solve_prevents_success(self):
        self.environment.return_value = dict(self.execution, host={'fixture': 'changed host'})
        with self.assertRaisesRegex(IdentityError, 'environment differs'):
            self.execute()
        self.build.assert_not_called()
        self.environment.side_effect = [self.execution, self.environment.return_value]
        with self.assertRaisesRegex(IdentityError, 'environment changed'):
            self.execute()
        self.assertFalse((self.output / 'result.json').exists())

    def test_material_change_after_solve_prevents_success(self):
        capture = install.inputs
        count = [0]
        def changed(*args):
            value = capture(*args)
            count[0] += 1
            if count[0] == 2:
                value['parameters']['fixture'] = 'changed source inputs'
            return value
        with mock.patch.object(install, 'inputs', side_effect=changed):
            with self.assertRaises(IdentityError):
                self.execute()
        self.assertFalse((self.output / 'result.json').exists())

    def test_existing_and_symlink_output_roots_are_not_overwritten(self):
        self.output.symlink_to(self.output.parent / 'missing', target_is_directory=True)
        with self.assertRaisesRegex(IdentityError, 'must be new'):
            self.execute()
        self.output.unlink()
        self.output.mkdir()
        with self.assertRaisesRegex(IdentityError, 'must be new'):
            self.execute()
        self.build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
