"""Real subprocess coverage, isolation and failure checks for the config runner."""

from collections import Counter
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from crossforge_internal import ci_tests


class ConfigurationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "repository"
        self.source.mkdir()
        for name in (".gitignore", "scripts/ci-tests.py", "scripts/crossforge_internal/__init__.py",
                     "scripts/crossforge_internal/identity.py", "scripts/crossforge_internal/ci_tests.py"):
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(ROOT / name), str(target))
        (self.source / "tests/config").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        self.output = self.directory / "diagnostics"

    def module(self, name, body):
        path = self.source / "tests/config" / (name + ".py")
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    def command(self, jobs=4, timeout=30):
        return [sys.executable, str(self.source / "scripts/ci-tests.py"), "run", "--source", str(self.source),
                "--output", str(self.output), "--jobs", str(jobs), "--timeout", str(timeout)]

    def run_cli(self, jobs=4, timeout=30):
        process = subprocess.run(self.command(jobs, timeout), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 universal_newlines=True, timeout=timeout + 15)
        path = self.output / "result.json"
        return process, json.loads(path.read_text()) if path.exists() else None

    def test_every_module_runs_in_isolated_source_and_python_state(self):
        for name in ("test_first", "test_second", "test_new_without_a_timing_hint"):
            self.module(name, '''
                import builtins
                from pathlib import Path
                import unittest
                class Tests(unittest.TestCase):
                    def test_isolated(self):
                        self.assertFalse(hasattr(builtins, "other_test_worker"))
                        builtins.other_test_worker = True
                        path = Path("worker-owned-file")
                        self.assertFalse(path.exists())
                        path.write_text("isolated")
            ''')
        process, result = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual((result["discovered_tests"], result["tests_run"]), (3, 3))
        self.assertEqual(len(result["workers"]), 3)
        self.assertFalse((self.source / "worker-owned-file").exists())
        executed = [name for worker in result["workers"] for name in worker["result"]["executed"]]
        self.assertEqual(len(set(executed)), 3)
        self.assertTrue(result["source_unchanged"])

    def test_failure_in_one_worker_does_not_hide_other_selected_results(self):
        self.module("test_failure", '''
            import unittest
            class Tests(unittest.TestCase):
                def test_rejected(self): self.fail("deliberate regression")
        ''')
        self.module("test_success", '''
            import unittest
            class Tests(unittest.TestCase):
                def test_checked(self): self.assertEqual(2 + 2, 4)
        ''')
        process, result = self.run_cli()
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["tests_run"], 2)
        self.assertEqual({worker["result"]["status"] for worker in result["workers"]}, {"passed", "failed"})
        self.assertIn("deliberate regression", process.stdout)

    def test_import_error_prevents_partial_suite_success(self):
        self.module("test_broken", "raise RuntimeError('broken test import')\n")
        self.module("test_valid", "import unittest\nclass T(unittest.TestCase):\n def test_ok(self): pass\n")
        process, result = self.run_cli()
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result["status"], "failed")
        self.assertIn("discovery failed", result["error"])
        self.assertIn("broken test import", (self.output / "discovery.log").read_text())
        self.assertEqual(result["workers"], [])

    def test_worker_exit_without_result_cannot_succeed(self):
        self.module("test_exits", '''
            import os, unittest
            class Tests(unittest.TestCase):
                def test_exits(self): os._exit(0)
        ''')
        process, result = self.run_cli()
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result["workers"][0]["exit_code"], 0)
        self.assertEqual(result["workers"][0]["result"]["status"], "missing-result")

    def test_worker_discovery_cannot_silently_drop_a_selected_test(self):
        self.module("test_changes_inventory", '''
            from pathlib import Path
            import unittest
            class Tests(unittest.TestCase):
                def test_first(self): pass
                def test_second(self): pass
            def load_tests(loader, suite, pattern):
                if Path.cwd().name.startswith("worker-"):
                    return unittest.TestSuite([Tests("test_first")])
                return suite
        ''')
        process, result = self.run_cli()
        self.assertEqual(process.returncode, 1)
        self.assertIn("worker discovery differs", (self.output / "worker-0.log").read_text())
        self.assertEqual(result["status"], "failed")

    def test_class_fixture_failure_and_unexpected_success_remain_failures(self):
        for body in (
            "import unittest\nclass T(unittest.TestCase):\n @classmethod\n def setUpClass(cls): raise RuntimeError('fixture failed')\n def test_first(self): pass\n def test_second(self): pass\n",
            "import unittest\nclass T(unittest.TestCase):\n @unittest.expectedFailure\n def test_unexpected(self): pass\n",
        ):
            with self.subTest(body=body):
                self.module("test_boundary", body)
                self.output = self.directory / ("diagnostics-%d" % len(list(self.directory.glob("diagnostics-*"))))
                process, result = self.run_cli()
                self.assertEqual(process.returncode, 1, process.stdout)
                self.assertEqual(result["status"], "failed")

    def test_intentional_test_and_fixture_skips_are_reported_without_fabricated_execution(self):
        self.module("test_skips", '''
            import unittest
            class ClassSkipped(unittest.TestCase):
                @classmethod
                def setUpClass(cls): raise unittest.SkipTest("fixture unavailable")
                def test_first(self): pass
                def test_second(self): pass
            class FunctionSkipped(unittest.TestCase):
                @unittest.skip("optional tool unavailable")
                def test_optional(self): pass
        ''')
        process, result = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(result["discovered_tests"], 3)
        self.assertEqual(result["tests_run"], 1)
        worker = result["workers"][0]["result"]
        self.assertEqual(len(worker["executed"]), 1)
        self.assertEqual(worker["fixture_skips"][0]["scope"], "test_skips.ClassSkipped")
        self.assertEqual(len(worker["skipped"]), 2)

    def test_missing_or_duplicate_coverage_is_rejected(self):
        expected = Counter({"test_sample.T.test_one": 1, "test_sample.T.test_two": 1})
        self.assertFalse(ci_tests.complete_coverage(expected, ["test_sample.T.test_one"], []))
        self.assertFalse(ci_tests.complete_coverage(expected, ["test_sample.T.test_one"] * 2, []))
        self.assertTrue(ci_tests.complete_coverage(expected, list(expected), []))
        self.assertFalse(ci_tests.complete_coverage(expected, list(expected), [{"scope": "test_sample.T", "reason": "skip"}]))

    def test_stale_timing_hints_never_filter_new_or_existing_tests(self):
        modules = {name: [name + ".T.test_one"] for name in ("test_first", "test_second", "test_new")}
        groups = ci_tests.partition(modules, 2, {"test_removed": 1000, "test_first": 10})
        self.assertEqual(Counter(name for group in groups for name in group), Counter(modules.keys()))
        self.assertEqual(Counter(test for group in groups for names in group.values() for test in names),
                         Counter(test for names in modules.values() for test in names))

    def test_invalid_timing_hints_fail_explicitly(self):
        path = self.source / "tests/config/timing-hints.json"
        for value in ('{"schema_version":1,"schema_version":1,"module_seconds":{}}',
                      '{"schema_version":2,"module_seconds":{}}',
                      '{"schema_version":1,"module_seconds":{},"unknown":true}',
                      '{"schema_version":1,"module_seconds":{"test_one":true}}'):
            with self.subTest(value=value):
                path.write_text(value)
                with self.assertRaises(ValueError):
                    ci_tests.timing_hints(self.source)

    def test_source_file_mutation_prevents_success(self):
        self.module("test_mutates", '''
            from pathlib import Path
            import unittest
            class Tests(unittest.TestCase):
                def test_changes_source(self): Path(__file__).write_text("changed")
        ''')
        process, result = self.run_cli()
        self.assertEqual(process.returncode, 1)
        self.assertIn("test source changed", result["error"])
        self.assertIn("test_changes_source", (self.source / "tests/config/test_mutates.py").read_text())

    def test_empty_suite_and_nested_test_modules_fail_explicitly(self):
        process, result = self.run_cli()
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result["workers"], [])
        self.output = self.directory / "nested-diagnostics"
        self.module("test_top", "import unittest\nclass T(unittest.TestCase):\n def test_one(self): pass\n")
        nested = self.source / "tests/config/nested"
        nested.mkdir()
        (nested / "test_extra.py").write_text("raise RuntimeError('must not be ignored')\n")
        process, result = self.run_cli()
        self.assertEqual(process.returncode, 1)
        self.assertIn("flat test_*.py", (self.output / "discovery.log").read_text())

    def test_timeout_stops_a_worker_and_its_term_ignoring_child(self):
        marker = self.directory / "child.pid"
        body = '''
            from pathlib import Path
            import subprocess, sys, time, unittest
            class Tests(unittest.TestCase):
                def test_wait(self):
                    child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(90)"])
                    Path(MARKER).write_text(str(child.pid))
                    time.sleep(90)
        '''.replace("MARKER", repr(str(marker)))
        self.module("test_waits", body)
        process, result = self.run_cli(timeout=2)
        self.assertEqual(process.returncode, 1)
        self.assertIn("timeout", result["error"])
        self.assertTrue(marker.is_file(), process.stdout)
        pid = int(marker.read_text())
        path = Path("/proc/%d/stat" % pid)
        deadline = time.monotonic() + 3
        while path.exists() and path.read_text().split()[2] != "Z" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(not path.exists() or path.read_text().split()[2] == "Z", "test child must not keep running")

    def test_cancellation_stops_workers_and_preserves_failure_diagnostics(self):
        marker = self.directory / "started.pid"
        self.module("test_cancel", '''
            import os, time, unittest
            from pathlib import Path
            class Tests(unittest.TestCase):
                def test_wait(self):
                    Path(MARKER).write_text(str(os.getpid()))
                    time.sleep(90)
        '''.replace("MARKER", repr(str(marker))))
        process = subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.is_file())
            pid = int(marker.read_text())
            process.terminate()
            process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            result = json.loads((self.output / "result.json").read_text())
            self.assertEqual(result["status"], "failed")
            self.assertIn("interrupted", result["error"])
            self.assertFalse(Path("/proc/%d" % pid).exists())
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)

    def test_snapshot_includes_uncommitted_files_and_omits_tracked_deletions(self):
        path = self.module("test_deleted", "fixture\n")
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        path.unlink()
        added = self.module("test_untracked", "new fixture\n")
        records = ci_tests.source_files(self.source.resolve())
        names = {record["path"] for record in records}
        self.assertNotIn("tests/config/test_deleted.py", names)
        self.assertIn(added.relative_to(self.source).as_posix(), names)
        link = self.source / "outside"
        link.symlink_to("/etc/hostname")
        with self.assertRaisesRegex(ValueError, "regular source files"):
            ci_tests.source_files(self.source.resolve())
        link.unlink()
        records = ci_tests.source_files(self.source.resolve())
        directory = self.source / "tests/config"
        directory.rename(self.source / "tests/renamed")
        directory.symlink_to("renamed", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "test source changed"):
            ci_tests.check_files(self.source.resolve(), records)

    def test_existing_output_and_output_inside_source_are_rejected(self):
        self.output.mkdir()
        marker = self.output / "original"
        marker.write_text("preserve")
        process, _ = self.run_cli()
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(marker.read_text(), "preserve")
        self.output = self.source / "diagnostics"
        process, _ = self.run_cli()
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse(self.output.exists())
