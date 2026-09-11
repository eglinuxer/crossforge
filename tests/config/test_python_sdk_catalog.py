"""Signed SDK acquisition uses explicit fixture trust boundaries and a real Bake graph."""

import contextlib
import copy
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import test_python_component_sdk as sdk_fixtures

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import component_build, component_resolution, python_components, python_handoff
    from crossforge_internal import python_row_resolution, python_sdk, python_sdk_catalog as catalog, qualification_execution
    from crossforge_internal.identity import IdentityError, load_json
    CLI = runpy.run_path(str(ROOT / "scripts/component-artifact.py"))
finally:
    sys.path.pop(0)


class PythonSdkCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = json.loads(subprocess.check_output(
            ['docker', 'buildx', 'bake', '--print', 'python-dev', 'sdk-complete-dev'], cwd=ROOT))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data, self.evidence = self.root / 'data', self.root / 'evidence'
        self.fixture = sdk_fixtures.PythonComponentSdkTests()
        self.fixture.graph = self.graph
        self.fixture.setUp()
        self.components, self.execution = self.fixture.components, self.fixture.execution
        self.toolchains = {'components': {arch + '-toolchain-install': {'status': 'verified-build-component',
            'subject': self.components['toolchains'][arch]} for arch in python_components.ARCHES}, 'required_producers': []}
        self.python = {'components': {row + '-' + name: {'status': 'verified-build-component',
            'subject': value['subjects'][name]} for row, value in self.components['rows'].items()
            for name, _, _ in python_handoff.PARTS}, 'required_producers': []}
        self.environment = self.patch(qualification_execution, 'execution_identity', return_value=self.execution)
        self.toolchain_resolver = self.patch(component_resolution, 'bind_toolchains', return_value=(self.graph, self.toolchains))
        self.python_resolver = self.patch(component_resolution, 'bind_python', return_value=(self.graph, self.python))
        self.row_resolver = self.patch(python_row_resolution, 'resolve', side_effect=lambda *args: {
            'status': 'verified-qualified-row', 'subject': self.components['rows'][args[2]]['qualification'],
            'producer': {'fixture': 'original producer'}})

    def patch(self, module, name, **kwargs):
        patcher = mock.patch.object(module, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def acquire(self, root='python-dev', graph=None):
        return catalog.acquire(ROOT, graph or self.graph, root, self.execution, self.data, self.evidence,
            'builder', self.root / 'oras', self.root / 'cosign', self.root / 'docker')

    def test_acquisition_connects_all_six_rows_to_existing_sdk_consumer_without_compilers(self):
        for root in ('python-dev', 'sdk-complete-dev'):
            self.data, self.evidence = self.root / (root + '-data'), self.root / (root + '-evidence')
            result = self.acquire(root)
            self.assertEqual(result['status'], 'ready')
            self.assertEqual(result['components'], self.components)
            self.assertEqual(load_json(self.evidence / 'components.json'), self.components)
            self.assertEqual(result['required_builds'], [])
            self.assertEqual(result['required_rows'], [])
            self.assertEqual(result['integration'], 'not executed by acquisition')
            resolved, bindings, reused = self.fixture.bind(root, components=result['components'])
            captured = python_sdk.inputs(ROOT, resolved, root, self.execution, bindings)
            self.assertEqual(len(captured['dependencies']), 8)
            self.assertEqual(set(reused), set(self.components['rows']))
            self.assertFalse({record['path'] for record in captured['files']} & {
                'scripts/build-gcc.sh', 'scripts/build-cpython-native.sh', 'scripts/build-cpython-cross.sh'})
            self.assertEqual(self.toolchain_resolver.call_args[0][2], self.execution['build'])
            self.assertEqual(self.python_resolver.call_args[0][3], self.toolchains['components'])
        self.assertEqual(self.toolchain_resolver.call_count, 2)
        self.assertEqual(self.python_resolver.call_count, 2)
        self.assertEqual(self.row_resolver.call_count, 12)
        for call in self.row_resolver.call_args_list:
            row = call[0][2]
            self.assertEqual(call[0][3], self.execution)
            self.assertEqual(call[0][4], self.components['rows'][row]['subjects'])
            self.assertEqual(len(call[0][4]), 7)

    def test_wrong_sdk_chain_is_rejected_before_registry_access(self):
        changes = [('python-dev', 'args', 'CROSSFORGE_PYTHON_ROWS', 'cp39'),
            ('python-dev-append-cp314', 'contexts', 'crossforge_python_row', 'target:python-row-cp313'),
            ('python-dev-append-cp39', 'args', 'CPYTHON_VERSION', '3.9.0')]
        for target, field, key, value in changes:
            graph = copy.deepcopy(self.graph)
            graph['target'][target][field][key] = value
            with self.subTest(target=target), self.assertRaises(IdentityError):
                self.acquire(graph=graph)
        with self.assertRaises(IdentityError):
            self.acquire(root='python-cp39-dev')
        self.toolchain_resolver.assert_not_called()
        self.python_resolver.assert_not_called()
        self.row_resolver.assert_not_called()

    def test_raw_part_miss_selects_its_producer_and_blocks_only_its_row_acquisition(self):
        label = 'cp39-x86_64-install'
        self.python['components'][label] = {'status': 'build-required'}
        target = python_components.spec(ROOT, 'cp39', 'x86_64', 'install')['target']
        self.python['required_producers'] = [target]
        result = self.acquire()
        self.assertEqual(result['status'], 'components-required')
        self.assertEqual(result['required_builds'], [target])
        self.assertEqual(result['required_rows'], ['cp39'])
        self.assertIsNone(result['components'])
        self.assertFalse((self.evidence / 'components.json').exists())
        self.assertEqual(self.row_resolver.call_count, 5)
        self.assertEqual(result['rows']['cp39']['status'], 'dependency-build-required')

    def test_shared_toolchain_miss_prevents_every_row_from_becoming_qualified(self):
        self.toolchains['components']['x86_64-toolchain-install'] = {'status': 'build-required'}
        target = component_build.toolchain_spec('x86_64', 'toolchain-install')['target']
        self.toolchains['required_producers'] = [target]
        result = self.acquire()
        self.assertEqual(result['required_rows'], sorted(self.components['rows']))
        self.assertEqual(result['required_builds'], [target])
        self.assertIsNone(result['components'])
        self.row_resolver.assert_not_called()

    def test_qualification_miss_is_not_replaced_by_raw_parts_or_implicit_execution(self):
        original = self.row_resolver.side_effect
        self.row_resolver.side_effect = lambda *args: {'status': 'qualification-required'} if args[2] == 'cp39' else original(*args)
        with mock.patch.object(python_sdk, 'execute') as execute:
            result = self.acquire()
        self.assertEqual(result['status'], 'components-required')
        self.assertEqual(result['required_rows'], ['cp39'])
        self.assertEqual(result['required_builds'], [])
        self.assertIsNone(result['components'])
        execute.assert_not_called()

    def test_signature_transport_or_row_verification_error_is_fatal_and_retains_available_evidence(self):
        def reject(*args):
            output = args[6]
            component_build.write_json(output / 'inputs.json', {'fixture': 'rejected row inputs'})
            raise IdentityError('fixture failed authentication or execution verification')
        self.row_resolver.side_effect = reject
        with self.assertRaises(IdentityError):
            self.acquire()
        self.assertFalse((self.evidence / 'components.json').exists())
        self.assertFalse((self.evidence / 'result.json').exists())
        row = self.row_resolver.call_args[0][2]
        self.assertEqual(load_json(self.evidence / 'rows' / row / 'inputs.json'), {'fixture': 'rejected row inputs'})

    def test_environment_changes_cannot_produce_a_ready_components_file(self):
        self.environment.return_value = dict(self.execution, host={'fixture': 'different host'})
        with self.assertRaisesRegex(IdentityError, 'environment differs'):
            self.acquire()
        self.toolchain_resolver.assert_not_called()
        self.environment.side_effect = [self.execution, dict(self.execution, host={'fixture': 'changed host'})]
        with self.assertRaisesRegex(IdentityError, 'environment changed'):
            self.acquire()
        self.assertFalse((self.evidence / 'components.json').exists())

    def test_incomplete_or_unplanned_resolver_results_cannot_hide_missing_producers(self):
        original = copy.deepcopy(self.python)
        for change in ('missing', 'extra', 'unplanned', 'unexpected-status'):
            self.python.clear()
            self.python.update(copy.deepcopy(original))
            if change == 'missing':
                self.python['components'].pop('cp39-build')
            elif change == 'extra':
                self.python['components']['cp315-build'] = {'status': 'build-required'}
            else:
                self.python['components']['cp39-build'] = {'status': 'build-required' if change == 'unplanned' else 'trusted'}
            with self.subTest(change=change), self.assertRaises(IdentityError):
                self.acquire()
            self.assertFalse((self.evidence / 'components.json').exists())

    def test_graph_raw_scope_must_have_exactly_both_toolchains_and_every_row_part(self):
        for function, result in ((component_resolution.toolchain_edges, {}),
                                 (component_resolution.python_requirements, {'cp39': ['build']})):
            with mock.patch.object(component_resolution, function.__name__, return_value=result), self.assertRaises(IdentityError):
                self.acquire()
        self.toolchain_resolver.assert_not_called()

    def test_directories_reject_overlap_existing_paths_and_symlink_parent_aliases(self):
        for data, evidence in ((self.root / 'same', self.root / 'same'), (self.data, self.data / 'nested'),
                               (self.evidence / 'nested', self.evidence), (self.root, self.evidence)):
            with self.subTest(data=data, evidence=evidence), self.assertRaises(IdentityError):
                catalog.directories(data, evidence)
        target = self.root / 'actual'
        target.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(target, target_is_directory=True)
        with self.assertRaises(IdentityError):
            catalog.directories(target / 'data', alias / 'data' / 'evidence')

    def test_execute_passes_exact_acquired_subjects_to_fresh_existing_integration(self):
        integrated = {'mode': 'executed', 'fixture': 'existing integration boundary'}
        with mock.patch.object(python_sdk, 'execute', return_value=integrated) as execute:
            result = catalog.execute(ROOT, self.graph, 'python-dev', self.execution, self.data, self.evidence,
                'builder', self.root / 'oras', self.root / 'cosign', self.root / 'docker')
        self.assertEqual(execute.call_args[0][:5], (ROOT, self.graph, 'python-dev', self.execution, self.components))
        self.assertEqual(execute.call_args[0][5], self.evidence / 'integration')
        self.assertEqual(result['integration'], integrated)
        self.assertEqual(load_json(self.evidence / 'result.json'), result)

    def test_incomplete_acquisition_or_integration_failure_never_produces_success(self):
        self.row_resolver.side_effect = lambda *args: {'status': 'qualification-required'}
        with mock.patch.object(python_sdk, 'execute') as execute, self.assertRaisesRegex(IdentityError, 'components are missing'):
            catalog.execute(ROOT, self.graph, 'python-dev', self.execution, self.data, self.evidence,
                'builder', self.root / 'oras', self.root / 'cosign')
        execute.assert_not_called()
        self.assertEqual(load_json(self.evidence / 'acquisition/result.json')['status'], 'components-required')
        self.assertFalse((self.evidence / 'result.json').exists())
        self.data, self.evidence = self.root / 'second-data', self.root / 'second-evidence'
        self.row_resolver.side_effect = lambda *args: {'status': 'verified-qualified-row',
            'subject': self.components['rows'][args[2]]['qualification']}
        with mock.patch.object(python_sdk, 'execute', side_effect=IdentityError('fixture failed integration')), self.assertRaises(IdentityError):
            catalog.execute(ROOT, self.graph, 'python-dev', self.execution, self.data, self.evidence,
                'builder', self.root / 'oras', self.root / 'cosign')
        self.assertFalse((self.evidence / 'result.json').exists())


class PythonSdkCatalogCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.graph, self.execution = {'fixture': 'graph'}, {'fixture': 'execution'}
        component_build.write_json(self.root / 'graph.json', self.graph)
        component_build.write_json(self.root / 'execution.json', self.execution)

    def args(self, command):
        return [command, '--source', str(ROOT), '--graph', str(self.root / 'graph.json'), '--root', 'python-dev',
                '--execution', str(self.root / 'execution.json'), '--builder', 'fixture',
                '--component-directory', str(self.root / 'data'), '--output', str(self.root / 'output'),
                '--oras', str(self.root / 'oras'), '--cosign', str(self.root / 'cosign')]

    def test_acquisition_cli_reports_missing_components_as_failure_with_explicit_result(self):
        for status, code in (('ready', 0), ('components-required', 1)):
            with mock.patch.object(catalog, 'acquire', return_value={'status': status}) as acquire, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(CLI['main'](self.args('acquire-python-sdk')), code)
            self.assertEqual(json.loads(output.getvalue()), {'status': status})
            self.assertEqual(acquire.call_args[0][:4], (ROOT, self.graph, 'python-dev', self.execution))
            self.assertEqual(acquire.call_args[0][4:6], (self.root / 'data', self.root / 'output'))

    def test_catalog_execution_cli_uses_acquisition_gate_and_propagates_failure(self):
        with mock.patch.object(catalog, 'execute', return_value={'fixture': 'integrated'}) as execute, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(CLI['main'](self.args('execute-python-sdk-catalog')), 0)
        self.assertEqual(execute.call_args[0][:4], (ROOT, self.graph, 'python-dev', self.execution))
        with mock.patch.object(catalog, 'execute', side_effect=IdentityError('fixture missing row')), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(CLI['main'](self.args('execute-python-sdk-catalog')), 1)


if __name__ == '__main__':
    unittest.main()
