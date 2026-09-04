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


def prepare_tool_links(output, prefix, compiler):
    directory = output / "tool-bin"
    directory.mkdir()
    records = []
    for name in ("as", "ld"):
        source = Path(command([compiler, "-print-prog-name=%s" % name]))
        if not str(source).startswith(str(prefix) + "/"):
            raise ValidationError("final target %s escaped the compiler prefix" % name)
        require_file(source, "final target %s" % name)
        destination = directory / name
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--mode", choices=("qualification", "observation"), default="qualification"
    )
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
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
        release_contract = CONTRACT["validate_release_contract"](arguments.release)
        release = release_contract["release"]
        if arguments.mode == "qualification":
            if arguments.plan is not None or arguments.candidate_baseline is not None:
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

        arguments.output.mkdir(parents=True, exist_ok=True)
        tool_prefix, target_tools = prepare_tool_links(
            arguments.output, arguments.prefix, compiler
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
                "CC_UNDER_TEST": str(compiler),
                "PATH": str(arguments.prefix / "bin") + ":" + environment["PATH"],
            }
        )
        summaries = {}
        make_results = []
        for suite in plan["suites"]:
            source_sum = arguments.build / Path(
                *CONTRACT["resolve_summary_path"](
                    suite["sum_file"], arguments.target
                ).parts
            )
            source_log = source_sum.with_suffix(".log")
            for stale in (source_sum, source_log):
                if stale.exists() or stale.is_symlink():
                    stale.unlink()
            flags = " ".join(
                ["--target_board=%s" % tier["board"]["name"]]
                + suite["runtestflags"]
            )
            process = subprocess.run(
                [
                    "make",
                    "-k",
                    "-C",
                    str(arguments.build),
                    suite["make_target"],
                    "RUNTESTFLAGS=%s" % flags,
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
            make_log = arguments.output / (suite["id"] + ".make.log")
            make_log.write_text(process.stdout, encoding="utf-8")
            require_file(source_sum, "%s summary" % suite["id"])
            copied_sum = arguments.output / (suite["id"] + ".sum")
            shutil.copyfile(str(source_sum), str(copied_sum))
            if source_log.is_file() and not source_log.is_symlink():
                shutil.copyfile(
                    str(source_log),
                    str(arguments.output / (suite["id"] + ".log")),
                )
                test_log = source_log.read_text(
                    encoding="utf-8", errors="replace"
                )
                if "/gcc/xgcc" in test_log or "/gcc/xg++" in test_log:
                    raise ValidationError(
                        "GCC testsuite invoked a build-tree compiler"
                    )
                if str(compiler) not in test_log:
                    raise ValidationError(
                        "GCC testsuite did not invoke the final installed compiler"
                    )
            summaries[suite["id"]] = copied_sum
            make_results.append(
                {
                    "suite": suite["id"],
                    "target": suite["make_target"],
                    "runtestflags": flags,
                    "returncode": process.returncode,
                    "log_sha256": file_sha256(make_log),
                }
            )
            if process.returncode != 0 and arguments.mode == "qualification":
                raise ValidationError(
                    "GCC testsuite make target failed: %s" % suite["make_target"]
                )
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
