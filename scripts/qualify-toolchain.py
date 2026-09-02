#!/usr/bin/env python3
"""Run cross-compiler, hybrid-runtime, and ABI smoke qualification."""

import argparse
import hashlib
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import abi_contract
from loader_evidence import normalize_loader_listing


class QualificationError(RuntimeError):
    pass


TARGET_PROFILES = {
    "x86_64-unknown-linux-gnu": {
        "arch": "x86_64",
    },
    "aarch64-unknown-linux-gnu": {
        "arch": "aarch64",
    },
}

QUALIFIED_ELF_PROFILE = "crossforge-qualified-v1"
COMPILER_DEFAULT_ELF_PROFILE = "compiler-default-observation"
HARDENED_LINKER_FLAG = "-Wl,-z,relro,-z,now"
RELEASE_COMPONENTS = runpy.run_path(
    str(Path(__file__).with_name("release-components-core.py"))
)
ProjectionError = RELEASE_COMPONENTS["ProjectionError"]


def run(arguments, cwd=None, env=None):
    process = subprocess.run(
        [str(argument) for argument in arguments],
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if process.returncode != 0:
        raise QualificationError(
            "command failed (%s):\n%s"
            % (" ".join(str(argument) for argument in arguments), process.stdout + process.stderr)
        )
    return process.stdout, process.stderr


def require(condition, message):
    if not condition:
        raise QualificationError(message)


def load_abi_baseline(path, arch, triple):
    """Load the exact canonical EL8 baseline for one trusted target input."""
    baseline = abi_contract.load_baseline(
        path,
        expected_arch=arch,
        expected_triple=triple,
    )
    require(baseline["baseline"] == "el8", "unsupported ABI baseline identity")
    canonical = abi_contract.canonical_bytes(baseline) + b"\n"
    try:
        serialized = path.read_bytes()
    except OSError as error:
        raise QualificationError("%s: %s" % (path, error)) from error
    require(serialized == canonical, "ABI baseline is not canonical JSON")
    return baseline, {
        "baseline": baseline["baseline"],
        "canonical_sha256": abi_contract.canonical_sha256(baseline),
        "schema": baseline["$schema"],
        "target": dict(baseline["target"]),
    }


def audit_artifact(readelf, binary, baseline, artifact, profile_name):
    """Collect GNU readelf evidence once and apply the shared ABI contract."""
    dynamic_symbols, _ = run([readelf, "--wide", "--dyn-syms", binary])
    version_info, _ = run([readelf, "--wide", "--version-info", binary])
    dynamic_section, _ = run([readelf, "--wide", "-d", binary])
    program_headers, _ = run([readelf, "--wide", "-l", binary])
    elf_header, _ = run([readelf, "--wide", "-h", binary])
    return abi_contract.audit_readelf(
        baseline,
        artifact,
        dynamic_symbols,
        version_info,
        dynamic_section,
        program_headers,
        elf_header,
        profile_name=profile_name,
    )


def main():
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--sysroot", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--abi-baseline", type=Path, required=True)
    parser.add_argument(
        "--qualification-component-sha256", required=True
    )
    parser.add_argument("--skip-sysroot-execution", action="store_true")
    arguments = parser.parse_args()

    profile = TARGET_PROFILES.get(arguments.target)
    require(profile is not None, "unsupported qualification target")
    if arguments.target == "x86_64-unknown-linux-gnu":
        require(
            not arguments.skip_sysroot_execution,
            "x86_64 qualification must execute in the locked sysroot",
        )
    else:
        require(
            arguments.skip_sysroot_execution,
            "aarch64 sysroot execution requires the explicit QEMU stage",
        )

    gcc = arguments.prefix / "bin" / (arguments.target + "-gcc")
    gxx = arguments.prefix / "bin" / (arguments.target + "-g++")
    gcc_ar = arguments.prefix / "bin" / (arguments.target + "-gcc-ar")
    gcc_ranlib = arguments.prefix / "bin" / (arguments.target + "-gcc-ranlib")
    nm = arguments.prefix / "bin" / (arguments.target + "-nm")
    readelf = arguments.prefix / "bin" / (arguments.target + "-readelf")
    require(
        all(path.is_file() for path in (gcc, gxx, gcc_ar, gcc_ranlib, nm, readelf)),
        "toolchain is incomplete",
    )
    arguments.work.mkdir(parents=True, exist_ok=True)

    release = json.loads(arguments.release.read_text(encoding="utf-8"))
    release_canonical = json.dumps(
        release, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    sysroot_lock_path = arguments.sysroot / "usr/share/crossforge/sysroot-lock.json"
    sysroot_lock = json.loads(sysroot_lock_path.read_text(encoding="utf-8"))
    sysroot_canonical = json.dumps(
        sysroot_lock, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    sysroot_digest = hashlib.sha256(sysroot_canonical.encode("utf-8")).hexdigest()
    release_targets = [
        target for target in release["targets"] if target["triple"] == arguments.target
    ]
    require(len(release_targets) == 1, "target is not unique in release.json")
    require(
        release_targets[0]["sysroot"]["canonical_sha256"] == sysroot_digest,
        "qualified sysroot differs from release.json",
    )
    abi_baseline, abi_baseline_identity = load_abi_baseline(
        arguments.abi_baseline,
        profile["arch"],
        arguments.target,
    )
    try:
        release_abi = abi_contract.validate_release_abi_identities(release)
        qualification_component = RELEASE_COMPONENTS[
            "bind_toolchain_qualification_component"
        ](
            release,
            profile["arch"],
            arguments.qualification_component_sha256,
        )
    except (abi_contract.AbiContractError, ProjectionError) as error:
        raise QualificationError(str(error)) from error
    require(
        release_abi["targets"][profile["arch"]]["baseline"]
        == {
            "file": "abi/el8/%s.json" % profile["arch"],
            "canonical_sha256": abi_baseline_identity[
                "canonical_sha256"
            ],
        },
        "qualified ABI baseline differs from release.json",
    )
    target_interpreter = abi_contract.TARGETS[profile["arch"]]["interpreter"]
    report = {
        "target": arguments.target,
        "binaries": {},
        "abi_baseline": abi_baseline_identity,
        "qualification_component": qualification_component,
        "release_sha256": hashlib.sha256(release_canonical.encode("utf-8")).hexdigest(),
        "sysroot_sha256": sysroot_digest,
        "sources": {
            "gcc": release["gts"]["source"],
            "binutils": release["binutils"]["source"],
        },
    }
    if arguments.target == "aarch64-unknown-linux-gnu":
        report["runtime_executor"] = release["qemu"]["executor"]
        report["runtime_base"] = {
            "index_digest": release["base_image"]["digest"],
            "manifest_digest": release["base_image"]["manifests"]["arm64"],
        }
    else:
        report["runtime_executor"] = {"kind": "native"}
    machine, _ = run([gcc, "-dumpmachine"])
    require(machine.strip() == arguments.target, "compiler target mismatch")
    full_version, _ = run([gcc, "-dumpfullversion"])
    report["compiler_version"] = full_version.strip()
    require(
        report["compiler_version"] == release["gts"]["gcc_version"],
        "compiler version differs from release.json",
    )
    printed_sysroot, _ = run([gcc, "-print-sysroot"])
    require(printed_sysroot.strip() == str(arguments.sysroot), "compiler sysroot mismatch")
    program_paths = {}
    for program in ("as", "ld"):
        path, _ = run([gcc, "-print-prog-name=%s" % program])
        program_paths[program] = path.strip()
        require(
            program_paths[program].startswith(str(arguments.prefix) + "/"),
            "%s escaped the Crossforge prefix" % program,
        )
    ld_version, _ = run([program_paths["ld"], "--version"])
    report["binutils_version"] = ld_version.splitlines()[0]
    require(
        re.search(
            r"(?<![0-9.])%s(?![0-9.])" % re.escape(release["binutils"]["version"]),
            report["binutils_version"],
        ),
        "binutils version differs from release.json",
    )

    libstdcxx, _ = run([gxx, "-print-file-name=libstdc++.so"])
    libgcc_s, _ = run([gcc, "-print-file-name=libgcc_s.so"])
    libgcc_a, _ = run([gcc, "-print-file-name=libgcc.a"])
    for name, path in (("libstdc++", libstdcxx.strip()), ("libgcc_s", libgcc_s.strip())):
        require(path.startswith(str(arguments.prefix) + "/"), "%s linker script escaped prefix" % name)
        require(Path(path).is_file(), "%s linker script is missing" % name)
    libgcc_archive = Path(libgcc_a.strip())
    require(
        str(libgcc_archive).startswith(str(arguments.prefix) + "/")
        and libgcc_archive.is_file(),
        "libgcc.a escaped the Crossforge prefix",
    )

    _stdout, include_stderr = run([gcc, "-E", "-Wp,-v", "-xc", "/dev/null"])
    include_paths = [line.strip() for line in include_stderr.splitlines() if line.startswith(" ")]
    require(str(arguments.sysroot / "usr/include") in include_paths, "target include path is missing")
    allowed_include_roots = [
        os.path.realpath(str(arguments.prefix)),
        os.path.realpath(str(arguments.sysroot)),
    ]
    leaked_includes = []
    for path in include_paths:
        normalized = os.path.realpath(path)
        if not any(
            normalized == root or normalized.startswith(root + os.sep)
            for root in allowed_include_roots
        ):
            leaked_includes.append(path)
    require(
        not leaked_includes,
        "host include paths leaked into search paths: %s" % ", ".join(leaked_includes),
    )
    report["include_paths"] = include_paths

    smoke = repository / "tests/smoke"
    hello = arguments.work / "hello"
    modern = arguments.work / "modern"
    lto = arguments.work / "lto"
    throw_library = arguments.work / "libthrow.so"
    catch = arguments.work / "catch"
    compiler_default_canary = arguments.work / "compiler-default-canary"
    link_map = arguments.work / "modern.map"
    nonshared_audit = arguments.work / "libstdc++-nonshared-audit.so"
    libgcc_helper_object = arguments.work / "libgcc-helper.o"
    libgcc_helper = arguments.work / "libgcc-helper"
    libgcc_map = arguments.work / "libgcc-helper.map"
    lto_archive_object = arguments.work / "lto-archive.o"
    lto_archive = arguments.work / "liblto-archive.a"
    lto_archive_executable = arguments.work / "lto-archive"
    run([gcc, "-O2", smoke / "hello.c", HARDENED_LINKER_FLAG, "-o", hello])
    # This one binary deliberately observes the unmodified compiler defaults.
    # It is compile-only evidence and is not a qualified runtime smoke binary.
    run([gcc, "-O2", smoke / "hello.c", "-o", compiler_default_canary])
    trace_stdout, trace_stderr = run(
        [
            gxx,
            "-std=c++20",
            "-O2",
            smoke / "modern.cc",
            "-Wl,-t",
            "-Wl,-Map=%s" % link_map,
            HARDENED_LINKER_FLAG,
            "-o",
            modern,
        ]
    )
    trace = trace_stdout + trace_stderr
    require(str(arguments.sysroot / "usr/lib64/libstdc++.so.6") in trace, "EL8 libstdc++ was not linked")
    require("libstdc++_nonshared.a" in trace, "vendor nonshared archive was not linked")
    nonshared_members = sorted(
        set(
            re.findall(
                r"libstdc\+\+_nonshared\.a\(([^)]+)\)",
                link_map.read_text(encoding="utf-8"),
            )
        )
    )
    require(nonshared_members, "modern C++ link extracted no nonshared archive member")
    report["nonshared_members"] = nonshared_members
    run(
        [
            gcc,
            "-O2",
            "-flto",
            smoke / "lto.c",
            HARDENED_LINKER_FLAG,
            "-o",
            lto,
        ]
    )
    run([gcc, "-O2", "-flto", "-c", smoke / "lto_archive.c", "-o", lto_archive_object])
    run([gcc_ar, "rcs", lto_archive, lto_archive_object])
    run([gcc_ranlib, lto_archive])
    run(
        [
            gcc,
            "-O2",
            "-flto",
            smoke / "lto_archive_main.c",
            lto_archive,
            HARDENED_LINKER_FLAG,
            "-o",
            lto_archive_executable,
        ]
    )
    run([gcc, "-O0", "-c", smoke / "libgcc_helper.c", "-o", libgcc_helper_object])
    undefined_helpers, _ = run([nm, "-u", libgcc_helper_object])
    helper_symbols = sorted(set(re.findall(r"\bU\s+(__udivti3)\b", undefined_helpers)))
    require(helper_symbols == ["__udivti3"], "wide division emitted no __udivti3 reference")
    run(
        [
            gcc,
            libgcc_helper_object,
            "-Wl,-Map=%s" % libgcc_map,
            HARDENED_LINKER_FLAG,
            "-o",
            libgcc_helper,
        ]
    )
    libgcc_map_text = libgcc_map.read_text(encoding="utf-8")
    require(
        str(libgcc_archive) + "(" in libgcc_map_text,
        "libgcc helper link did not use the Crossforge libgcc.a",
    )
    libgcc_members = sorted(
        set(
            re.findall(
                r"libgcc\.a\(([^)]+)\)",
                libgcc_map_text,
            )
        )
    )
    require(libgcc_members, "libgcc helper link extracted no cross-built archive member")
    report["libgcc_helper_symbols"] = helper_symbols
    report["libgcc_members"] = libgcc_members
    run(
        [
            gxx,
            "-std=c++20",
            "-O2",
            "-fPIC",
            "-shared",
            smoke / "throw.cc",
            HARDENED_LINKER_FLAG,
            "-o",
            throw_library,
        ]
    )
    run(
        [
            gxx,
            "-std=c++20",
            "-O2",
            smoke / "catch.cc",
            "-L" + str(arguments.work),
            "-lthrow",
            "-Wl,-z,origin",
            "-Wl,-rpath,$ORIGIN",
            HARDENED_LINKER_FLAG,
            "-o",
            catch,
        ]
    )
    nonshared_archive = Path(libstdcxx.strip()).parent / "libstdc++_nonshared.a"
    require(nonshared_archive.is_file(), "vendor nonshared archive is missing")
    run(
        [
            gxx,
            "-shared",
            "-nodefaultlibs",
            "-Wl,-z,defs",
            "-Wl,--whole-archive",
            nonshared_archive,
            "-Wl,--no-whole-archive",
            arguments.sysroot / "usr/lib64/libstdc++.so.6",
            "-lgcc_s",
            "-lc",
            "-lm",
            "-lpthread",
            "-ldl",
            HARDENED_LINKER_FLAG,
            "-o",
            nonshared_audit,
        ]
    )

    for binary, artifact, profile_name in (
        (hello, "toolchain/hello", QUALIFIED_ELF_PROFILE),
        (modern, "toolchain/modern", QUALIFIED_ELF_PROFILE),
        (lto, "toolchain/lto", QUALIFIED_ELF_PROFILE),
        (lto_archive_executable, "toolchain/lto-archive", QUALIFIED_ELF_PROFILE),
        (libgcc_helper, "toolchain/libgcc-helper", QUALIFIED_ELF_PROFILE),
        (throw_library, "toolchain/libthrow.so", QUALIFIED_ELF_PROFILE),
        (catch, "toolchain/catch", QUALIFIED_ELF_PROFILE),
        (
            nonshared_audit,
            "toolchain/libstdc++-nonshared-audit.so",
            QUALIFIED_ELF_PROFILE,
        ),
        (
            compiler_default_canary,
            "toolchain/compiler-default-canary",
            COMPILER_DEFAULT_ELF_PROFILE,
        ),
    ):
        report["binaries"][binary.name] = audit_artifact(
            readelf,
            binary,
            abi_baseline,
            artifact,
            profile_name,
        )

    catch_dynamic, _ = run([readelf, "-d", catch])
    for needed in ("libthrow.so", "libstdc++.so.6", "libgcc_s.so.1", "libc.so.6"):
        require("[%s]" % needed in catch_dynamic, "catch binary does not need %s" % needed)
    require(
        "$ORIGIN" in catch_dynamic,
        "catch binary does not carry an origin-relative runtime search path",
    )

    if arguments.skip_sysroot_execution:
        report["locked_sysroot_execution"] = {
            "status": "not_run",
            "required_executor": "explicit-qemu",
        }
        report["clean_runtime_execution"] = {
            "status": "not_run",
            "required_executor": "explicit-qemu",
        }
        report["native_release_execution"] = {
            "status": "required",
            "executor": "native-el8-aarch64",
        }
    else:
        require(os.geteuid() == 0, "locked-runtime execution requires chroot privileges")
        runtime_directory = arguments.sysroot / "opt/crossforge-qualification"
        require(not runtime_directory.exists(), "runtime qualification directory is not clean")
        runtime_directory.mkdir(parents=True)
        sysroot_resolved = str(arguments.sysroot.resolve())
        runtime_resolved = str(runtime_directory.resolve())
        require(
            runtime_resolved.startswith(sysroot_resolved + os.sep),
            "runtime qualification directory escaped the sysroot",
        )
        for artifact in (
            hello,
            modern,
            lto,
            lto_archive_executable,
            libgcc_helper,
            throw_library,
            catch,
        ):
            shutil.copy2(str(artifact), str(runtime_directory / artifact.name))
        require(
            (runtime_directory / "libthrow.so").is_file(),
            "cross-DSO runtime library was not copied into the sysroot",
        )
        loader_stdout, loader_stderr = run(
            [
                "chroot",
                arguments.sysroot,
                target_interpreter,
                "--list",
                "/opt/crossforge-qualification/catch",
            ]
        )
        loader_listing = loader_stdout + loader_stderr
        require(
            "not found" not in loader_listing,
            "dynamic loader could not resolve catch dependencies",
        )
        report["catch_loader"] = normalize_loader_listing(loader_listing)
        report["runtime_root"] = str(arguments.sysroot)
        for executable in (
            hello,
            modern,
            lto,
            lto_archive_executable,
            libgcc_helper,
            catch,
        ):
            target_path = "/opt/crossforge-qualification/%s" % executable.name
            command = ["chroot", arguments.sysroot, target_path]
            if executable == catch:
                # A bare chroot has no procfs for EL8 glibc's origin discovery.
                # The loader preflight above audits $ORIGIN; this execution leg
                # supplies the same directory explicitly. The clean Rocky stage
                # later executes catch directly and therefore exercises $ORIGIN.
                command = [
                    "chroot",
                    arguments.sysroot,
                    "/usr/bin/env",
                    "LD_LIBRARY_PATH=/opt/crossforge-qualification",
                    target_path,
                ]
            stdout, _ = run(command)
            if executable == hello:
                require(stdout.strip() == "crossforge-c-ok", "C smoke output mismatch")
            if executable == modern:
                require(stdout.strip() == "crossforge-cxx-ok", "C++ smoke output mismatch")
        report["locked_sysroot_execution"] = {
            "status": "passed",
            "executor": "native-chroot",
        }

    report_path = arguments.report or arguments.work / "qualification.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("qualified: %s" % arguments.target)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QualificationError, abi_contract.AbiContractError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
