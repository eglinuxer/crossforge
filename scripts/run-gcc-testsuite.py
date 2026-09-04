#!/usr/bin/env python3
"""Run a locked GCC DejaGNU profile against the final installed compiler."""

import argparse
import hashlib
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
CONTRACT = runpy.run_path(
    str(Path(__file__).with_name("validate-gcc-testsuite.py"))
)
COMPONENT = runpy.run_path(str(Path(__file__).with_name("release_component.py")))
ValidationError = CONTRACT["ValidationError"]


def file_sha256(path):
    return CONTRACT["file_sha256"](path)


def command(arguments, cwd=None, env=None):
    process = subprocess.run(
        [str(argument) for argument in arguments],
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if process.returncode != 0:
        raise ValidationError(
            "command failed (%s):\n%s"
            % (" ".join(str(argument) for argument in arguments), process.stdout + process.stderr)
        )
    return process.stdout.strip()


def write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, str(path))
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def selected_target(plan, triple, runtime_tier):
    matches = [target for target in plan["targets"] if target["triple"] == triple]
    if len(matches) != 1:
        raise ValidationError("GCC testsuite target is not unique")
    tiers = [
        tier
        for tier in matches[0]["runtime_tiers"]
        if tier["name"] == runtime_tier
    ]
    if len(tiers) != 1:
        raise ValidationError("GCC testsuite runtime tier is not unique")
    return matches[0], tiers[0]


def require_file(path, label):
    if path.is_symlink() or not path.is_file():
        raise ValidationError("%s is missing or not a regular file: %s" % (label, path))
    return path


def require_runtime_file(root, relative, label):
    path = root / relative
    resolved = path.resolve()
    trusted_root = root.resolve()
    if trusted_root not in resolved.parents or not resolved.is_file():
        raise ValidationError("%s escapes or is missing: %s" % (label, path))
    return resolved


def parse_os_release(path):
    values = {}
    with path.open("r", encoding="utf-8") as stream:
        for raw in stream:
            line = raw.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values


def print_log_tail(path, lines=80):
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    print("--- diagnostic tail: %s ---" % path, file=sys.stderr)
    for line in content[-lines:]:
        print(line, file=sys.stderr)


def print_log_diagnostics(path, limit=160):
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    needles = (
        "Executing on ",
        "spawn -ignore",
        " error:",
        "fatal error:",
        "No such file",
        "collect2:",
        "ld:",
        "cannot find",
        "compiler exited",
        "Excess errors:",
    )
    selected = [line for line in content if any(item in line for item in needles)]
    print("--- diagnostic errors: %s ---" % path, file=sys.stderr)
    for line in selected[-limit:]:
        print(line, file=sys.stderr)


def prepare_tool_links(output, prefix, compiler, gxx, gcc_ar, gcov):
    directory = output / "tool-bin"
    directory.mkdir(exist_ok=True)
    records = []
    tools = {
        "as": Path(command([compiler, "-print-prog-name=as"])),
        "gcc": compiler,
        "g++": gxx,
        "gcc-ar": gcc_ar,
        "gcov": gcov,
        "ld": Path(command([compiler, "-print-prog-name=ld"])),
    }
    for name in sorted(tools):
        source = tools[name]
        if not str(source).startswith(str(prefix) + "/"):
            raise ValidationError("final target %s escaped the compiler prefix" % name)
        require_file(source, "final target %s" % name)
        destination = directory / name
        if destination.exists() or destination.is_symlink():
            destination.unlink()
        os.symlink(str(source), str(destination))
        records.append({"name": name, "path": str(source), "sha256": file_sha256(source)})
    return directory, records


def prepare_gcc_site(build, compiler, gxx, tool_prefix):
    gcc_object = build / "gcc"
    site = gcc_object / "site.exp"
    if site.exists() or site.is_symlink():
        site.unlink()
    command(["make", "-C", gcc_object, "site.exp"])
    require_file(site, "generated GCC site.exp")
    with site.open("a", encoding="utf-8") as stream:
        stream.write("\n## Crossforge final installed compiler override ##\n")
        stream.write(
            'set GCC_UNDER_TEST "%s -B%s/"\n' % (compiler, tool_prefix)
        )
        stream.write(
            'set GXX_UNDER_TEST "%s -B%s/"\n' % (gxx, tool_prefix)
        )
        stream.write('set TOOL_EXECUTABLE "%s"\n' % compiler)
        stream.write("catch {unset TEST_GCC_EXEC_PREFIX}\n")
    return site


def source_patch_record(patch):
    return {
        "id": patch["id"],
        "patch_sha256": patch["sha256"],
        "targets": patch["targets"],
    }


def apply_suite_patches(plan, source, suite_id):
    records = []
    for patch in plan.get("source_patches", []):
        if patch["suite"] != suite_id:
            continue
        patch_path = CONTRACT["repository_file"](
            patch["file"], "GCC testsuite source patch"
        )
        targets = []
        for target_record in patch["targets"]:
            target_path = require_file(
                source / target_record["file"], "source patch target"
            )
            if file_sha256(target_path) != target_record["before_sha256"]:
                raise ValidationError("GCC testsuite source patch input differs")
            targets.append((target_path, target_record))
        command(
            [
                "patch",
                "--batch",
                "--forward",
                "--fuzz=0",
                "-p%d" % patch["strip"],
                "-i",
                patch_path,
            ],
            cwd=source,
        )
        for target_path, target_record in targets:
            if file_sha256(target_path) != target_record["after_sha256"]:
                raise ValidationError("GCC testsuite source patch output differs")
        records.append(source_patch_record(patch))
    return records


def prepare_runtime_links(output, runtime_root):
    directory = output / "runtime-lib"
    directory.mkdir()
    records = []
    for name, relative in (
        ("libgcc_s.so.1", "lib64/libgcc_s.so.1"),
        ("libstdc++.so.6", "usr/lib64/libstdc++.so.6"),
    ):
        source = require_runtime_file(
            runtime_root, relative, "installed libstdc++ runtime %s" % name
        )
        os.symlink(str(source), str(directory / name))
        records.append(
            {"name": name, "path": str(source), "sha256": file_sha256(source)}
        )
    return directory, records


def file_size(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def stop_processes(processes):
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def wait_with_progress(suite_id, processes, progress_paths, timeout_seconds):
    started = time.monotonic()
    last_change = started
    last_report = started
    signature = tuple(file_size(path) for path in progress_paths)
    try:
        while any(process.poll() is None for process in processes):
            time.sleep(5)
            now = time.monotonic()
            current = tuple(file_size(path) for path in progress_paths)
            if current != signature:
                signature = current
                last_change = now
            if now - last_report >= 60:
                print(
                    "GCC testsuite progress: %s elapsed=%ds bytes=%d active=%d"
                    % (
                        suite_id,
                        int(now - started),
                        sum(current),
                        sum(process.poll() is None for process in processes),
                    ),
                    flush=True,
                )
                last_report = now
            if now - last_change > 600:
                raise ValidationError(
                    "GCC testsuite made no observable progress for 600 seconds: %s"
                    % suite_id
                )
            if now - started > timeout_seconds:
                raise ValidationError(
                    "GCC testsuite exceeded %d seconds: %s"
                    % (timeout_seconds, suite_id)
                )
    except Exception:
        stop_processes(processes)
        raise
    return [process.returncode for process in processes]


def merge_dejagnu_results(script, inputs, output, logs=False):
    arguments = ["/bin/sh", str(script)]
    if logs:
        arguments.append("-L")
    arguments.extend(str(path) for path in inputs)
    with output.open("w", encoding="utf-8") as stream:
        process = subprocess.run(
            arguments,
            stdout=stream,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    if process.returncode != 0:
        raise ValidationError(
            "DejaGNU result merge failed: %s" % process.stderr.strip()
        )


def run_installed_libstdcxx(
    suite,
    source,
    output,
    environment,
    flags_list,
    jobs,
    timeout_seconds,
):
    testsuite_source = source / "libstdc++-v3/testsuite"
    require_file(
        testsuite_source / "lib/libstdc++.exp",
        "installed libstdc++ testsuite driver",
    )
    merge_script = require_file(
        source / "contrib/dg-extract-results.sh",
        "DejaGNU result merge script",
    )
    parallel_directory = output / "libstdc++.full.parallel"
    parallel_directory.mkdir()
    worker_directories = []
    stdout_paths = []
    handles = []
    processes = []
    try:
        for index in range(jobs):
            worker = output / ("libstdc++.full.worker-%d" % (index + 1))
            worker.mkdir()
            worker_directories.append(worker)
            stdout_path = worker / "runtest.stdout"
            stdout_paths.append(stdout_path)
            handle = stdout_path.open("w", encoding="utf-8")
            handles.append(handle)
            worker_environment = dict(environment)
            worker_environment["GCC_RUNTEST_PARALLELIZE_DIR"] = str(
                parallel_directory
            )
            invocation = [
                "runtest",
                "--tool",
                suite["tool"],
                "--srcdir=%s" % testsuite_source,
            ] + flags_list
            processes.append(
                subprocess.Popen(
                    invocation,
                    cwd=str(worker),
                    env=worker_environment,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                )
            )
        progress_paths = list(stdout_paths)
        for worker in worker_directories:
            progress_paths.extend(
                (worker / "libstdc++.sum", worker / "libstdc++.log")
            )
        returncodes = wait_with_progress(
            suite["id"], processes, progress_paths, timeout_seconds
        )
        if any(returncode not in (0, 1) for returncode in returncodes):
            raise ValidationError(
                "installed libstdc++ worker failed: %s" % returncodes
            )
    except Exception:
        stop_processes(processes)
        for handle in handles:
            handle.flush()
        for worker, stdout_path in zip(worker_directories, stdout_paths):
            print_log_tail(stdout_path, lines=40)
            print_log_tail(worker / "libstdc++.log", lines=80)
        raise
    finally:
        for handle in handles:
            handle.close()
    worker_sums = [
        require_file(worker / "libstdc++.sum", "libstdc++ worker summary")
        for worker in worker_directories
    ]
    worker_logs = [
        require_file(worker / "libstdc++.log", "libstdc++ worker log")
        for worker in worker_directories
    ]
    source_sum = output / suite["sum_file"]
    source_log = source_sum.with_suffix(".log")
    merge_dejagnu_results(merge_script, worker_sums, source_sum)
    merge_dejagnu_results(merge_script, worker_logs, source_log, logs=True)
    stdout = []
    workers = []
    for index, (worker, stdout_path, returncode) in enumerate(
        zip(worker_directories, stdout_paths, returncodes), start=1
    ):
        stdout.append("=== worker %d ===\n" % index)
        stdout.append(stdout_path.read_text(encoding="utf-8", errors="replace"))
        workers.append(
            {
                "id": index,
                "returncode": returncode,
                "summary_sha256": file_sha256(worker / "libstdc++.sum"),
                "log_sha256": file_sha256(worker / "libstdc++.log"),
            }
        )
    return subprocess.CompletedProcess(
        ["runtest", "--tool", suite["tool"]], max(returncodes), "".join(stdout)
    ), source_sum, source_log, workers


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--mode", choices=("qualification", "observation"), default="qualification"
    )
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--suite")
    parser.add_argument("--defer-observation", action="store_true")
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--sysroot", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--runtime-tier", required=True)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--host-marker", type=Path, required=True)
    parser.add_argument("--qualification-component", type=Path)
    parser.add_argument("--qualification-component-sha256")
    parser.add_argument("--qemu", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--candidate-baseline", type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.mode == "qualification":
            if (
                arguments.plan is not None
                or arguments.candidate_baseline is not None
                or arguments.suite is not None
                or arguments.defer_observation
            ):
                raise ValidationError(
                    "qualification mode does not accept an external plan or candidate baseline"
                )
            if (
                arguments.qualification_component is None
                or arguments.qualification_component_sha256 is None
            ):
                raise ValidationError(
                    "qualification mode requires its release component identity"
                )
            release_contract = CONTRACT["validate_release_contract"](
                arguments.release
            )
            release = release_contract["release"]
            plan = release_contract["plan"]
        else:
            if arguments.plan is None or arguments.candidate_baseline is None:
                raise ValidationError(
                    "observation mode requires --plan and --candidate-baseline"
                )
            if (
                arguments.qualification_component is not None
                or arguments.qualification_component_sha256 is not None
            ):
                raise ValidationError(
                    "observation mode must not claim a qualification component"
                )
            if arguments.defer_observation and arguments.suite is None:
                raise ValidationError(
                    "deferred observation requires one explicit suite"
                )
            release = CONTRACT["load_json"](arguments.release)
            CONTRACT["validate_schema"](
                release, REPOSITORY / "config/schemas/release.schema.json"
            )
            plan = CONTRACT["validate_plan"](
                CONTRACT["load_json"](arguments.plan)
            )
            if plan["gcc_version"] != release["gts"]["gcc_version"]:
                raise ValidationError(
                    "GCC testsuite compiler version differs from release"
                )
        target, tier = selected_target(
            plan, arguments.target, arguments.runtime_tier
        )
        selected_suites = plan["suites"]
        if arguments.suite is not None:
            selected_suites = [
                suite for suite in plan["suites"]
                if suite["id"] == arguments.suite
            ]
            if len(selected_suites) != 1:
                raise ValidationError("GCC testsuite suite is not in the plan")
        if arguments.mode == "qualification":
            baseline_record = release_contract["baselines"][(
                arguments.target, arguments.runtime_tier
            )]
            baseline = baseline_record["document"]
            component = COMPONENT["load_component"](
                arguments.qualification_component,
                "toolchain/gcc-testsuite-qualification",
                "qualification",
                arguments.qualification_component_sha256,
            )
        compiler = require_file(
            arguments.prefix / "bin" / (arguments.target + "-gcc"),
            "final GCC",
        )
        gxx = require_file(
            arguments.prefix / "bin" / (arguments.target + "-g++"),
            "final G++",
        )
        gcc_ar = require_file(
            arguments.prefix / "bin" / (arguments.target + "-gcc-ar"),
            "final gcc-ar",
        )
        gcov = require_file(
            arguments.prefix / "bin" / (arguments.target + "-gcov"),
            "final gcov",
        )
        require_file(arguments.source / "configure", "prepared GCC source")
        preparation = require_file(
            arguments.source / ".crossforge/preparation.txt",
            "GCC source preparation evidence",
        )
        sysroot_lock = require_file(
            arguments.sysroot / "usr/share/crossforge/sysroot-lock.json",
            "embedded sysroot lock",
        )
        host_marker = require_file(arguments.host_marker, "GCC test host marker")
        site = require_file(arguments.site, "DejaGNU site file")
        planned_site = CONTRACT["repository_file"](
            plan["site"]["file"], "planned DejaGNU site file"
        )
        if site.resolve() != planned_site:
            raise ValidationError("DejaGNU site file differs from the plan")
        board = CONTRACT["repository_file"](
            tier["board"]["file"], "planned DejaGNU board file"
        )
        if target["arch"] == "aarch64":
            if arguments.qemu is None or arguments.runtime_root is None:
                raise ValidationError(
                    "aarch64 GCC testsuite requires QEMU and a runtime root"
                )
            qemu = require_file(arguments.qemu, "aarch64 QEMU executor")
            if file_sha256(qemu) != release["qemu"]["executor"][
                "binary_sha256"
            ]:
                raise ValidationError("aarch64 QEMU digest differs from release")
            if arguments.runtime_root.is_symlink() or not arguments.runtime_root.is_dir():
                raise ValidationError("aarch64 runtime root is missing or invalid")
            runtime_loader = require_runtime_file(
                arguments.runtime_root,
                "lib/ld-linux-aarch64.so.1",
                "aarch64 runtime loader",
            )
            runtime_material = {
                "qemu_sha256": file_sha256(qemu),
                "loader_sha256": file_sha256(runtime_loader),
            }
            if arguments.runtime_tier == "clean-rocky":
                if arguments.runtime_root != Path("/runtime-root"):
                    raise ValidationError("clean Rocky runtime root path differs")
                runtime_os = require_runtime_file(
                    arguments.runtime_root,
                    "etc/os-release",
                    "runtime os-release",
                )
                os_release = parse_os_release(runtime_os)
                if os_release.get("ID") != "rocky" or os_release.get(
                    "VERSION_ID"
                ) != "8.10":
                    raise ValidationError("clean runtime is not Rocky Linux 8.10")
                runtime_material["os_release_sha256"] = file_sha256(runtime_os)
            elif arguments.runtime_root != arguments.sysroot:
                raise ValidationError("locked runtime root must equal the sysroot")
        elif arguments.qemu is not None or arguments.runtime_root is not None:
            raise ValidationError("x86_64 GCC testsuite must not receive QEMU/runtime root")
        machine = command([compiler, "-dumpmachine"])
        version = command([compiler, "-dumpfullversion"])
        printed_sysroot = command([compiler, "-print-sysroot"])
        if machine != arguments.target or version != plan["gcc_version"]:
            raise ValidationError("final GCC identity differs from the test plan")
        if printed_sysroot != str(arguments.sysroot):
            raise ValidationError("final GCC sysroot differs from the test invocation")
        if command(["runtest", "--version"]).splitlines()[0].find("1.6.1") < 0:
            raise ValidationError("unexpected DejaGNU version")
        if command(["expect", "-v"]).find("5.45.4") < 0:
            raise ValidationError("unexpected Expect version")
        host_gcc_material = None
        if plan["profile"] == "full":
            host_gcc = require_file(Path("/usr/bin/gcc"), "EL8 base host GCC")
            host_gcc_version = command([host_gcc, "-dumpfullversion"])
            if host_gcc_version.split(".")[0] != plan["host_gcc_major"]:
                raise ValidationError("unexpected EL8 base host GCC version")
            host_gcc_material = {
                "path": str(host_gcc),
                "sha256": file_sha256(host_gcc),
                "version": host_gcc_version,
            }

        arguments.output.mkdir(parents=True, exist_ok=True)
        tool_prefix, target_tools = prepare_tool_links(
            arguments.output, arguments.prefix, compiler, gxx, gcc_ar, gcov
        )
        generated_site = prepare_gcc_site(
            arguments.build, compiler, gxx, tool_prefix
        )
        environment = dict(os.environ)
        environment.pop("GCC_EXEC_PREFIX", None)
        environment.update(
            {
                "LC_ALL": "C",
                "DEJAGNU": str(site),
                "GCC_UNDER_TEST": str(compiler),
                "GXX_UNDER_TEST": str(gxx),
                "GCC_AR_UNDER_TEST": str(gcc_ar),
                "GCOV_UNDER_TEST": str(gcov),
                "CC_UNDER_TEST": str(compiler),
                "CROSSFORGE_GCC_TOOL_PREFIX": str(tool_prefix),
                "PATH": str(arguments.prefix / "bin") + ":" + environment["PATH"],
            }
        )
        summaries = {}
        make_results = []
        source_patches = []
        for suite in selected_suites:
            suite_patches = apply_suite_patches(
                plan, arguments.source, suite["id"]
            )
            source_patches.extend(suite_patches)
            flags_list = ["--target_board=%s" % tier["board"]["name"]]
            flags_list.extend(suite["runtestflags"])
            flags = " ".join(flags_list)
            suite_environment = dict(environment)
            driver = suite.get("driver", "make")
            runtime_records = None
            if driver == "runtest-installed":
                suite_environment["PATH"] = (
                    str(tool_prefix) + ":" + suite_environment["PATH"]
                )
                runtime_root = arguments.runtime_root or arguments.sysroot
                runtime_directory, runtime_records = prepare_runtime_links(
                    arguments.output, runtime_root
                )
                inherited_library_path = suite_environment.get(
                    "LD_LIBRARY_PATH", ""
                )
                suite_environment["LD_LIBRARY_PATH"] = str(runtime_directory)
                if inherited_library_path:
                    suite_environment["LD_LIBRARY_PATH"] += (
                        ":" + inherited_library_path
                    )
                process, source_sum, source_log, workers = run_installed_libstdcxx(
                    suite,
                    arguments.source,
                    arguments.output,
                    suite_environment,
                    flags_list,
                    plan["jobs"],
                    suite["timeout_seconds"],
                )
                make_result = {
                    "suite": suite["id"],
                    "driver": driver,
                    "tool": suite["tool"],
                    "jobs": plan["jobs"],
                    "timeout_seconds": suite["timeout_seconds"],
                    "runtestflags": flags,
                    "returncode": process.returncode,
                    "runtime_libraries": runtime_records,
                    "workers": workers,
                }
            else:
                source_sum = arguments.build / Path(
                    *CONTRACT["resolve_summary_path"](
                        suite["sum_file"], arguments.target
                    ).parts
                )
                source_log = source_sum.with_suffix(".log")
                for stale in (source_sum, source_log):
                    if stale.exists() or stale.is_symlink():
                        stale.unlink()
                work_directory = arguments.build / suite.get("make_directory", "")
                invocation = [
                    "make",
                    "-k",
                    "-j%d" % plan.get("jobs", 1),
                    "-C",
                    str(work_directory),
                    suite["make_target"],
                    "RUNTESTFLAGS=%s" % flags,
                ]
                process = subprocess.run(
                    invocation,
                    env=suite_environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                )
                make_result = {
                    "suite": suite["id"],
                    "driver": driver,
                    "target": suite["make_target"],
                    "directory": suite.get("make_directory", "."),
                    "jobs": plan.get("jobs", 1),
                    "runtestflags": flags,
                    "returncode": process.returncode,
                }
            make_log = arguments.output / (suite["id"] + ".make.log")
            make_log.write_text(process.stdout, encoding="utf-8")
            make_result["log_sha256"] = file_sha256(make_log)
            try:
                require_file(source_sum, "%s summary" % suite["id"])
            except ValidationError:
                print_log_tail(make_log)
                raise
            copied_sum = arguments.output / (suite["id"] + ".sum")
            shutil.copyfile(str(source_sum), str(copied_sum))
            require_file(source_log, "%s log" % suite["id"])
            shutil.copyfile(
                str(source_log),
                str(arguments.output / (suite["id"] + ".log")),
            )
            test_log = source_log.read_text(
                encoding="utf-8", errors="replace"
            )
            if "/gcc/xgcc" in test_log or "/gcc/xg++" in test_log:
                print_log_diagnostics(source_log)
                print_log_tail(make_log)
                raise ValidationError(
                    "GCC testsuite invoked a build-tree compiler"
                )
            final_invoked = str(compiler) in test_log
            if driver == "runtest-installed":
                resolved_gxx = shutil.which(
                    "g++", path=suite_environment["PATH"]
                )
                if (
                    resolved_gxx is None
                    or Path(resolved_gxx).resolve() != gxx.resolve()
                ):
                    raise ValidationError(
                        "libstdc++ compiler alias did not resolve to final G++"
                    )
                final_invoked = (
                    final_invoked or "Executing on host: g++ " in test_log
                )
                forbidden_library = str(
                    arguments.build / arguments.target / "libstdc++-v3"
                )
                if forbidden_library in test_log:
                    print_log_diagnostics(source_log)
                    print_log_tail(make_log)
                    raise ValidationError(
                        "installed libstdc++ tests used a build-tree library"
                    )
                if str(runtime_directory) not in test_log:
                    raise ValidationError(
                        "installed libstdc++ runtime path is absent from the log"
                    )
            if not final_invoked:
                print_log_diagnostics(source_log)
                print_log_tail(make_log)
                raise ValidationError(
                    "GCC testsuite did not invoke the final installed compiler"
                )
            summaries[suite["id"]] = copied_sum
            make_results.append(make_result)
            if arguments.mode == "observation":
                CONTRACT["parse_summary"](
                    suite["id"],
                    copied_sum,
                    set(plan["unexpected_statuses"]),
                )
                write_json(
                    arguments.output / (suite["id"] + ".execution.json"),
                    {
                        "schema_version": 1,
                        "kind": "gcc-testsuite-execution",
                        "profile": plan["profile"],
                        "target": arguments.target,
                        "runtime_tier": arguments.runtime_tier,
                        "plan_sha256": CONTRACT["canonical_sha256"](plan),
                        "suite": suite["id"],
                        "make": make_result,
                        "source_patches": suite_patches,
                    },
                )
            if (
                process.returncode != 0
                and driver != "runtest-installed"
                and arguments.mode == "qualification"
            ):
                raise ValidationError(
                    "GCC testsuite driver failed: %s" % suite["id"]
                )
        if arguments.mode == "observation" and arguments.defer_observation:
            print(
                "observed GCC testsuite suite: %s (%s, %s, %s)"
                % (
                    arguments.target,
                    arguments.runtime_tier,
                    plan["profile"],
                    arguments.suite,
                )
            )
            return 0
        if arguments.mode == "observation":
            summaries = {}
            make_results = []
            source_patches = []
            for suite in plan["suites"]:
                suite_id = suite["id"]
                copied_sum = require_file(
                    arguments.output / (suite_id + ".sum"),
                    "%s captured summary" % suite_id,
                )
                execution = CONTRACT["load_json"](
                    require_file(
                        arguments.output / (suite_id + ".execution.json"),
                        "%s execution record" % suite_id,
                    )
                )
                expected_execution = {
                    "schema_version": 1,
                    "kind": "gcc-testsuite-execution",
                    "profile": plan["profile"],
                    "target": arguments.target,
                    "runtime_tier": arguments.runtime_tier,
                    "plan_sha256": CONTRACT["canonical_sha256"](plan),
                    "suite": suite_id,
                }
                for key, value in expected_execution.items():
                    if execution.get(key) != value:
                        raise ValidationError(
                            "%s execution record identity differs" % suite_id
                        )
                if set(execution) != set(expected_execution) | {
                    "make",
                    "source_patches",
                }:
                    raise ValidationError(
                        "%s execution record fields differ" % suite_id
                    )
                if execution["make"].get("suite") != suite_id:
                    raise ValidationError(
                        "%s make execution identity differs" % suite_id
                    )
                expected_patches = [
                    source_patch_record(patch)
                    for patch in plan.get("source_patches", [])
                    if patch["suite"] == suite_id
                ]
                if execution["source_patches"] != expected_patches:
                    raise ValidationError(
                        "%s source patch execution differs" % suite_id
                    )
                summaries[suite_id] = copied_sum
                make_results.append(execution["make"])
                source_patches.extend(execution["source_patches"])
        materials = {
            "gcc": {
                "path": str(compiler),
                "sha256": file_sha256(compiler),
                "version": version,
            },
            "gxx": {"path": str(gxx), "sha256": file_sha256(gxx)},
            "sysroot_lock_sha256": file_sha256(sysroot_lock),
            "source_preparation_sha256": file_sha256(preparation),
            "host_marker_sha256": file_sha256(host_marker),
            "site_sha256": file_sha256(site),
            "generated_site_sha256": file_sha256(generated_site),
            "target_tools": target_tools,
            "board": {
                "name": tier["board"]["name"],
                "sha256": file_sha256(board),
            },
            "make": make_results,
        }
        if arguments.mode == "qualification":
            materials["qualification_component"] = {
                "component": component["component"],
                "canonical_sha256": arguments.qualification_component_sha256,
            }
        else:
            materials["observation_plan"] = {
                "file": str(arguments.plan),
                "canonical_sha256": CONTRACT["canonical_sha256"](plan),
            }
        if target["arch"] == "aarch64":
            materials["runtime"] = runtime_material
        if host_gcc_material is not None:
            materials["host_gcc"] = host_gcc_material
        if source_patches:
            materials["source_patches"] = source_patches
        try:
            if arguments.mode == "qualification":
                report = CONTRACT["normalize_summaries"](
                    plan, baseline, summaries, materials
                )
            else:
                report, candidate = CONTRACT["observe_summaries"](
                    plan,
                    arguments.target,
                    arguments.runtime_tier,
                    summaries,
                    materials,
                )
        except ValidationError:
            for suite in plan["suites"]:
                print_log_diagnostics(arguments.output / (suite["id"] + ".log"))
                print_log_tail(arguments.output / (suite["id"] + ".make.log"))
            raise
        write_json(arguments.report, report)
        if arguments.mode == "observation":
            write_json(arguments.candidate_baseline, candidate)
    except (KeyError, OSError, UnicodeError, ValidationError, COMPONENT["ComponentError"]) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    verb = "qualified" if arguments.mode == "qualification" else "observed"
    print(
        "%s GCC testsuite: %s (%s, %s)"
        % (verb, arguments.target, arguments.runtime_tier, plan["profile"])
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
