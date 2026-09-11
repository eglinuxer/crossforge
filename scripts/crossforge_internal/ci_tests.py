"""Complete unittest coverage with isolated processes; timings only schedule work."""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from .identity import content_sha256, exact_fields, load_json, relative_path, require


ROOT = Path(__file__).resolve().parents[2]
MODULE = re.compile(r"test_[A-Za-z0-9_]+\Z")


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def source_files(source):
    top = subprocess.check_output(["git", "-C", str(source), "rev-parse", "--show-toplevel"])
    require(Path(os.fsdecode(top).strip()).resolve() == source, "source must be a Git worktree root")
    names = subprocess.check_output(["git", "-C", str(source), "ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    result = []
    for name in sorted(set(os.fsdecode(names).rstrip("\0").split("\0"))):
        relative_path(name)
        path = source / name
        if not path.exists() and not path.is_symlink():
            continue  # A tracked file deliberately deleted in the worktree.
        require(path.resolve() == path and stat.S_ISREG(path.lstat().st_mode),
                "test snapshot requires regular source files: " + name)
        result.append({"path": name, "mode": stat.S_IMODE(path.stat().st_mode),
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    require(result, "test source is empty")
    return result


def check_files(source, records):
    for record in records:
        path = source / record["path"]
        require(path.resolve() == path and path.is_file() and not path.is_symlink() and
                stat.S_IMODE(path.stat().st_mode) == record["mode"] and
                hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"],
                "test source changed: " + record["path"])


def copy_files(source, destination, records):
    destination.mkdir()
    for record in records:
        target = destination / record["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(source / record["path"]), str(target))
        target.chmod(record["mode"])
    check_files(destination, records)


def test_ids(suite):
    result = []
    for test in suite:
        result.extend(test_ids(test) if isinstance(test, unittest.TestSuite) else [test.id()])
    return result


def bootstrap(source):
    os.chdir(str(source))
    sys.path[:0] = [str(source), str(source / "scripts"), str(source / "tests/config")]


def discover(source):
    bootstrap(source)
    directory = source / "tests/config"
    paths = sorted(directory.rglob("test_*.py"))
    require(paths and all(path.parent == directory and MODULE.fullmatch(path.stem) for path in paths),
            "configuration runner requires flat test_*.py modules")
    loader = unittest.TestLoader()
    suite = loader.discover(str(directory), pattern="test_*.py")
    require(not loader.errors, "test discovery failed:\n" + "\n".join(loader.errors))
    modules = {path.stem: [] for path in paths}
    for identity in test_ids(suite):
        module = identity.split(".", 1)[0]
        require(module in modules, "discovery returned a test outside its configuration module: " + identity)
        modules[module].append(identity)
    require(any(modules.values()), "configuration discovery found no tests")
    return modules


def timing_hints(source):
    path = source / "tests/config/timing-hints.json"
    if not path.exists():
        return {}
    value = load_json(path)
    exact_fields(value, ("schema_version", "module_seconds"), "test timing hints")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1,
            "unsupported test timing hints")
    hints = value["module_seconds"]
    require(type(hints) is dict and all(MODULE.fullmatch(key) and type(weight) is int and weight > 0
            for key, weight in hints.items()), "test timing hints must contain positive integer module weights")
    return hints


def partition(modules, jobs, hints):
    require(type(jobs) is int and 1 <= jobs <= 4, "test workers must be between 1 and 4")
    groups = [{"modules": {}, "weight": 0} for _ in range(min(jobs, len(modules)))]
    for name in sorted(modules, key=lambda name: (-hints.get(name, 1), name)):
        group = min(groups, key=lambda group: group["weight"])
        group["modules"][name] = modules[name]
        group["weight"] += hints.get(name, 1)
    return [group["modules"] for group in groups]


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executed = []
        self.fixture_skips = []

    def startTest(self, test):
        self.executed.append(test.id())
        super().startTest(test)

    def addSkip(self, test, reason):
        # unittest does not call startTest when a module/class fixture skips.
        # Record its scope separately instead of pretending those tests ran.
        match = re.fullmatch(r"setUp(?:Class|Module) \(([^)]+)\)", test.id())
        if match:
            self.fixture_skips.append({"scope": match.group(1), "reason": reason})
        super().addSkip(test, reason)


def complete_coverage(expected, executed, fixture_skips):
    require(type(executed) is list and all(type(name) is str for name in executed),
            "worker test identities are invalid")
    require(type(fixture_skips) is list, "worker fixture skips are invalid")
    accounted = Counter(executed)
    for skipped in fixture_skips:
        exact_fields(skipped, ("scope", "reason"), "skipped test fixture")
        require(type(skipped["scope"]) is str and skipped["scope"] and type(skipped["reason"]) is str,
                "skipped test fixture identity is invalid")
        matching = {name: count for name, count in expected.items()
                    if name.startswith(skipped["scope"] + ".")}
        require(matching, "skipped fixture is outside the test inventory")
        accounted.update(matching)
    return accounted == expected


def run_worker(source, plan):
    bootstrap(source)
    loader = unittest.TestLoader()
    started = time.monotonic()
    suite = loader.loadTestsFromNames(sorted(plan))
    expected = Counter(identity for names in plan.values() for identity in names)
    require(not loader.errors and Counter(test_ids(suite)) == expected,
            "worker discovery differs from the complete test inventory:\n" + "\n".join(loader.errors))
    loaded = time.monotonic()
    result = unittest.TextTestRunner(verbosity=1, resultclass=RecordedResult).run(suite)
    complete = complete_coverage(expected, result.executed, result.fixture_skips)
    return {"status": "passed" if result.wasSuccessful() and complete else "failed",
            "tests_run": result.testsRun, "executed": result.executed,
            "fixture_skips": result.fixture_skips, "complete": complete,
            "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
            "errors": [test.id() for test, _ in result.errors],
            "failures": [test.id() for test, _ in result.failures],
            "expected_failures": [test.id() for test, _ in result.expectedFailures],
            "unexpected_successes": [test.id() for test in result.unexpectedSuccesses],
            "loading_seconds": loaded - started, "execution_seconds": time.monotonic() - loaded}


def stop(process):
    # Also stop children left in this worker's own process group after its
    # interpreter exits. No worker subprocess may survive a completed run.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def command(source, mode, output, plan=None):
    result = [sys.executable, str(source / "scripts/ci-tests.py"), mode,
              "--source", str(source), "--output", str(output)]
    return result + (["--plan", str(plan)] if plan is not None else [])


def execute(commands, deadline):
    """Keep all worker results; interruption and timeout also stop their children."""
    workers = []
    started = time.monotonic()
    try:
        require(started < deadline, "configuration tests exceeded their timeout")
        for index, (args, cwd, log_path) in enumerate(commands):
            log = log_path.open("xb")
            try:
                process = subprocess.Popen(args, cwd=str(cwd), stdout=log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
            finally:
                log.close()
            workers.append(process)
        pending = set(range(len(workers)))
        heartbeat = started
        while pending:
            for index in list(pending):
                process = workers[index]
                if process.poll() is not None:
                    pending.remove(index)
                    print("configuration %s: exit=%s" % (commands[index][2].stem, process.returncode), flush=True)
                    print(commands[index][2].read_text(encoding="utf-8", errors="replace"), flush=True)
            if pending and time.monotonic() >= deadline:
                raise RuntimeError("configuration tests exceeded their timeout")
            if pending and time.monotonic() - heartbeat >= 30:
                print("configuration workers running: " + ", ".join(map(str, sorted(pending))), flush=True)
                heartbeat = time.monotonic()
            if pending:
                time.sleep(0.1)
        return [process.returncode for process in workers]
    finally:
        for process in workers:
            stop(process)


def run(source, output, jobs, timeout):
    source, output = source.resolve(), output.resolve()
    require(source != output and source not in output.parents, "test diagnostics must be outside the source worktree")
    require(not output.exists(), "test diagnostics directory must be new")
    output.mkdir(parents=True)
    started = time.monotonic()
    deadline = started + timeout
    records = source_files(source)
    write_json(output / "source.json", records)
    summary = {"schema_version": 1, "kind": "crossforge-configuration-test-run",
               "status": "failed", "source_sha256": content_sha256(records), "workers": []}
    try:
        with tempfile.TemporaryDirectory(prefix="crossforge-config-tests-") as temporary:
            temporary = Path(temporary)
            frozen = temporary / "source"
            copy_files(source, frozen, records)
            inventory = output / "inventory.json"
            codes = execute([(command(frozen, "_discover", inventory), frozen, output / "discovery.log")], deadline)
            require(codes == [0] and inventory.is_file(), "complete configuration discovery failed")
            modules = load_json(inventory)
            groups = partition(modules, jobs, timing_hints(frozen))
            commands = []
            for index, group in enumerate(groups):
                work = temporary / ("worker-%d" % index)
                copy_files(frozen, work, records)
                plan = output / ("worker-%d-plan.json" % index)
                write_json(plan, group)
                result = output / ("worker-%d.json" % index)
                commands.append((command(work, "_worker", result, plan), work,
                                 output / ("worker-%d.log" % index)))
            codes = execute(commands, deadline)
            for index, code in enumerate(codes):
                path = output / ("worker-%d.json" % index)
                result = load_json(path) if path.is_file() else {"status": "missing-result"}
                summary["workers"].append({"worker": index, "exit_code": code, "result": result})
                if result["status"] == "passed":
                    expected = Counter(name for names in groups[index].values() for name in names)
                    require(result["complete"] is True and complete_coverage(
                            expected, result["executed"], result["fixture_skips"]),
                            "worker result omits or duplicates selected tests")
                check_files(commands[index][1], records)
            check_files(frozen, records)
            require(source_files(source) == records, "source worktree changed while tests ran")
            summary["discovered_tests"] = sum(len(names) for names in modules.values())
            summary["tests_run"] = sum(worker["result"].get("tests_run", 0) for worker in summary["workers"])
            summary["source_unchanged"] = True
            if all(worker["exit_code"] == 0 and worker["result"]["status"] == "passed" for worker in summary["workers"]):
                summary["status"] = "passed"
    except KeyboardInterrupt:
        summary["error"] = "configuration tests interrupted"
        raise
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        summary["error"] = str(error)
        print("configuration tests failed: " + str(error), file=sys.stderr)
    finally:
        summary["elapsed_seconds"] = time.monotonic() - started
        write_json(output / "result.json", summary)
    print("configuration tests: %s; discovered=%s; elapsed=%.3fs" %
          (summary["status"], summary.get("discovered_tests", "unknown"), summary["elapsed_seconds"]), flush=True)
    return 0 if summary["status"] == "passed" else 1


def interrupted(_signal, _frame):
    raise KeyboardInterrupt()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "_discover", "_worker"))
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, choices=range(1, 5), default=4)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    require(args.timeout > 0, "test timeout must be positive")
    if args.mode == "run":
        previous = signal.signal(signal.SIGTERM, interrupted)
        try:
            return run(args.source, args.output, args.jobs, args.timeout)
        finally:
            signal.signal(signal.SIGTERM, previous)
    if args.mode == "_discover":
        write_json(args.output, discover(args.source.resolve()))
        return 0
    require(args.plan is not None, "worker plan is required")
    result = run_worker(args.source.resolve(), load_json(args.plan))
    write_json(args.output, result)
    return 0 if result["status"] == "passed" else 1
