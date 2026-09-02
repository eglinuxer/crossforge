#!/usr/bin/env python3
"""Qualify final host-runtime integration in the assembled Crossforge SDK."""

import argparse
import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path


ROW_CONTRACT = runpy.run_path(
    str(Path(__file__).with_name("python_row_contract.py"))
)
ContractError = ROW_CONTRACT["ContractError"]
SDK_IDENTITY = runpy.run_path(
    str(Path(__file__).with_name("python_sdk_identity.py"))
)
SDKIdentityError = SDK_IDENTITY["IdentityError"]
LOADER_EVIDENCE = runpy.run_path(
    str(Path(__file__).with_name("loader_evidence.py"))
)
RELEASE_COMPONENTS = runpy.run_path(
    str(Path(__file__).with_name("render-release-components.py"))
)
ProjectionError = RELEASE_COMPONENTS["ProjectionError"]


class QualificationError(RuntimeError):
    pass


TARGETS = {
    "x86_64": {
        "triple": "x86_64-unknown-linux-gnu",
        "machine": "Advanced Micro Devices X86-64",
        "interpreter": "/lib64/ld-linux-x86-64.so.2",
    },
    "aarch64": {
        "triple": "aarch64-unknown-linux-gnu",
        "machine": "AArch64",
        "interpreter": "/lib/ld-linux-aarch64.so.1",
    },
}
REQUIRED_IMPORTS = (
    "_bz2",
    "_ctypes",
    "_hashlib",
    "_lzma",
    "_multiprocessing",
    "_sqlite3",
    "_ssl",
    "_uuid",
    "bz2",
    "ctypes",
    "fcntl",
    "hashlib",
    "lzma",
    "pty",
    "select",
    "sqlite3",
    "ssl",
    "termios",
    "tty",
    "uuid",
    "zlib",
)
NEEDED_RE = re.compile(r"\(NEEDED\).*?\[([^\]]+)\]")
INTERPRETER_RE = re.compile(r"Requesting program interpreter:\s*([^\]]+)\]")
ROW_MANIFEST_KEYS = {
    "schema_version",
    "kind",
    "row",
    "version",
    "adapter",
    "support",
    "release_sha256",
    "qualification_components",
    "source",
    "source_manifest_sha256",
    "patches",
    "build_python_sha256",
    "build_python_sdk_tree",
    "zstd",
    "qualifications",
}


def fail(message):
    raise QualificationError(message)


def require(condition, message):
    if not condition:
        fail(message)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %r" % key)
        result[key] = value
    return result


def load_json(path):
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            value = json.load(stream, object_pairs_hook=reject_duplicate_keys)
    except (OSError, ValueError) as error:
        raise QualificationError("cannot load %s: %s" % (path, error)) from error
    require(isinstance(value, dict), "%s must contain an object" % path)
    return value


def canonical_sha256(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(arguments, env=None):
    process = subprocess.run(
        [str(argument) for argument in arguments],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    require(
        process.returncode == 0,
        "command failed (%s):\n%s"
        % (" ".join(str(item) for item in arguments), process.stdout + process.stderr),
    )
    return process.stdout, process.stderr


def rpm_inventory():
    stdout, _stderr = run(
        [
            "rpm",
            "-qa",
            "--qf",
            "%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\\n",
        ]
    )
    return sorted(line for line in stdout.splitlines() if line)


def clean_environment():
    environment = dict(os.environ)
    for name in ("PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH"):
        environment.pop(name, None)
    environment["LC_ALL"] = "C"
    return environment


def audit_dynamic_elf(readelf, path, expected_interpreter):
    dynamic, _stderr = run([readelf, "--wide", "-d", path])
    require(
        all(tag not in dynamic for tag in ("(RPATH)", "(RUNPATH)", "TEXTREL")),
        "%s has a forbidden dynamic tag" % path,
    )
    needed = sorted(set(NEEDED_RE.findall(dynamic)))
    require(
        all("/" not in name for name in needed),
        "%s has a path-qualified dependency" % path,
    )
    headers, _stderr = run([readelf, "--wide", "-l", path])
    interpreters = INTERPRETER_RE.findall(headers)
    expected = [] if expected_interpreter is None else [expected_interpreter]
    require(
        interpreters == expected,
        "%s has an unexpected program interpreter" % path,
    )
    return {"interpreter": expected_interpreter, "needed": needed}


def qualify_build_python(prefix, row, version, manifest, release_sha256):
    minor = version.rsplit(".", 1)[0]
    python = prefix / "bin" / ("python" + minor)
    require(python.is_file() and not python.is_symlink(), "%s build Python is missing" % row)
    python_sha256 = sha256_file(python)
    try:
        tree = SDK_IDENTITY["sdk_tree_identity"](prefix)
    except SDKIdentityError as error:
        raise QualificationError(str(error)) from error
    require(
        manifest.get("kind") == "crossforge-cpython-row"
        and manifest.get("row") == row
        and manifest.get("version") == version
        and manifest.get("release_sha256") == release_sha256
        and manifest.get("build_python_sha256") == python_sha256
        and manifest.get("build_python_sdk_tree") == tree,
        "%s build Python differs from its row manifest" % row,
    )
    imports = list(REQUIRED_IMPORTS)
    if minor == "3.14":
        imports.extend(("_zstd", "compression.zstd"))
    program = (
        "import importlib,json,platform,sys;"
        "mods=%r;"
        "loaded={name:importlib.import_module(name) for name in mods};"
        "print(json.dumps({'version':platform.python_version(),"
        "'prefix':sys.prefix,'imports':mods,"
        "'files':{name:getattr(module,'__file__',None) for name,module in loaded.items()},"
        "'zstd_version':list(loaded['compression.zstd'].zstd_version_info) "
        "if 'compression.zstd' in loaded else None},"
        "sort_keys=True,separators=(',',':')))"
        % imports
    )
    stdout, _stderr = run(
        [python, "-B", "-I", "-c", program], env=clean_environment()
    )
    lines = stdout.splitlines()
    require(len(lines) == 1, "%s build Python emitted unstable output" % row)
    try:
        probe = json.loads(lines[0], object_pairs_hook=reject_duplicate_keys)
    except ValueError as error:
        raise QualificationError("%s build Python probe is invalid" % row) from error
    require(
        probe.get("version") == version
        and probe.get("prefix") == str(prefix)
        and probe.get("imports") == imports
        and probe.get("zstd_version")
        == ([1, 5, 7] if minor == "3.14" else None)
        and isinstance(probe.get("files"), dict)
        and set(probe["files"]) == set(imports),
        "%s build Python probe differs" % row,
    )
    imported_files = {}
    resolved_prefix = prefix.resolve()
    for name in sorted(probe["files"]):
        value = probe["files"][name]
        if value is None:
            imported_files[name] = None
            continue
        path = Path(value)
        resolved = path.resolve()
        require(
            path.is_file() and resolved_prefix in resolved.parents,
            "%s imported %s outside its build prefix" % (row, name),
        )
        imported_files[name] = str(path)

    lib_dynload = prefix / "lib" / ("python" + minor) / "lib-dynload"
    elf_files = [python]
    if lib_dynload.is_dir():
        elf_files.extend(sorted(lib_dynload.glob("*.so")))
    require(len(elf_files) > 1, "%s build Python has no extension ELF inventory" % row)
    needed = set()
    elf_evidence = {}
    loader_dependencies = set()
    for index, path in enumerate(elf_files):
        relative = path.relative_to(prefix).as_posix()
        evidence = audit_dynamic_elf(
            "/usr/bin/readelf",
            path,
            TARGETS["x86_64"]["interpreter"] if index == 0 else None,
        )
        elf_evidence[relative] = evidence
        needed.update(evidence["needed"])
        loader_stdout, loader_stderr = run(["/usr/bin/ldd", path])
        loader = loader_stdout + loader_stderr
        require("not found" not in loader, "%s has an unresolved host DSO: %s" % (row, path))
        normalized = LOADER_EVIDENCE["normalize_loader_listing"](loader)
        loader_dependencies.update(normalized)
    for dependency in loader_dependencies:
        resolved = dependency.split(" => ", 1)[-1]
        if not resolved.startswith("/"):
            continue
        require(
            not resolved.startswith(("/src/", "/work/"))
            and "/sysroots/" not in resolved
            and not (
                "/opt/crossforge/python/" in resolved
                and not resolved.startswith(str(prefix) + "/")
            ),
            "%s loader escaped its host runtime: %s" % (row, dependency),
        )
    try:
        after_tree = SDK_IDENTITY["sdk_tree_identity"](prefix)
    except SDKIdentityError as error:
        raise QualificationError(str(error)) from error
    require(after_tree == tree, "%s build Python execution mutated its SDK" % row)
    return {
        "row": row,
        "version": version,
        "python_sha256": python_sha256,
        "sdk_tree": tree,
        "elf_count": len(elf_files),
        "elf_evidence": elf_evidence,
        "needed": sorted(needed),
        "imports": imports,
        "imported_files": imported_files,
        "loader_dependencies": sorted(loader_dependencies),
        "loader_evidence_sha256": canonical_sha256(
            sorted(loader_dependencies)
        ),
    }


def qualify_target_pythons(
    row_prefix,
    qualification_directory,
    row,
    version,
    manifest,
    release_sha256,
):
    minor = version.rsplit(".", 1)[0]
    qualifications = manifest.get("qualifications")
    require(
        isinstance(qualifications, dict)
        and set(qualifications) == set(TARGETS),
        "%s target qualification inventory differs" % row,
    )
    result = []
    for arch in ("x86_64", "aarch64"):
        profile = TARGETS[arch]
        target_prefix = row_prefix / "targets" / profile["triple"]
        target_python = target_prefix / "bin" / ("python" + minor)
        report_path = qualification_directory / (arch + ".json")
        require(
            target_python.is_file() and not target_python.is_symlink(),
            "%s %s target Python is missing or unsafe" % (row, arch),
        )
        require(
            report_path.is_file() and not report_path.is_symlink(),
            "%s %s qualification report is missing or unsafe" % (row, arch),
        )
        try:
            tree = SDK_IDENTITY["sdk_tree_identity"](target_prefix)
        except SDKIdentityError as error:
            raise QualificationError(str(error)) from error
        python_sha256 = sha256_file(target_python)
        report_sha256 = sha256_file(report_path)
        report = load_json(report_path)
        expected = {
            "target": profile["triple"],
            "report_sha256": report_sha256,
            "python_sha256": python_sha256,
            "sdk_tree": tree,
        }
        require(
            qualifications[arch] == expected,
            "%s %s target SDK differs from its row manifest" % (row, arch),
        )
        compile_report = report.get("compile")
        require(
            report.get("qualification_schema_version") == 4
            and report.get("report_kind") == "crossforge-cpython-qualification"
            and report.get("status") == "passed"
            and report.get("target") == profile["triple"]
            and report.get("version") == version
            and report.get("release_sha256") == release_sha256
            and report.get("python_sha256") == python_sha256
            and isinstance(compile_report, dict)
            and compile_report.get("sdk_tree") == tree,
            "%s %s qualification report identity differs" % (row, arch),
        )
        result.append(
            {
                "arch": arch,
                "target": profile["triple"],
                "python_sha256": python_sha256,
                "report_sha256": report_sha256,
                "sdk_tree": tree,
            }
        )
    return result


def release_target_map(release):
    targets = release.get("targets")
    require(isinstance(targets, list), "release target inventory is invalid")
    require(
        [
            (target.get("arch"), target.get("triple"))
            for target in targets
            if isinstance(target, dict)
        ]
        == [
            ("x86_64", TARGETS["x86_64"]["triple"]),
            ("aarch64", TARGETS["aarch64"]["triple"]),
        ],
        "release target inventory differs",
    )
    result = {}
    for target in targets:
        arch = target["arch"]
        sysroot = target.get("sysroot")
        require(
            set(target) == {"arch", "triple", "sysroot"}
            and isinstance(sysroot, dict)
            and set(sysroot)
            == {"status", "lock_file", "canonical_sha256"}
            and sysroot.get("status") == "locked"
            and sysroot.get("lock_file")
            == "locks/sysroot-el8-%s.json" % arch
            and re.fullmatch(r"[0-9a-f]{64}", sysroot.get("canonical_sha256", ""))
            is not None,
            "%s release sysroot identity differs" % arch,
        )
        result[arch] = target
    return result


def qualify_host_runtime(release, report_path, marker_path=None):
    require(
        report_path.is_file() and not report_path.is_symlink(),
        "host runtime qualification report is missing or unsafe",
    )
    report = load_json(report_path)
    try:
        components = RELEASE_COMPONENTS["render_component_documents"](release)
    except ProjectionError as error:
        raise QualificationError(str(error)) from error
    component = components["rpm/host-runtime"]
    expected_binding = {
        "kind": "release-component",
        "component": "rpm/host-runtime",
        "scope": "build",
        "canonical_sha256": canonical_sha256(component),
    }
    rpm = report.get("rpm")
    require(
        report.get("kind") == "crossforge-host-runtime-qualification"
        and report.get("status") == "passed"
        and isinstance(rpm, dict)
        and rpm.get("lock_sha256")
        == release["host_locks"]["host-runtime"]["canonical_sha256"]
        and rpm.get("release_binding") == expected_binding,
        "host runtime qualification report is not release-bound",
    )
    inventory = rpm_inventory()
    require(
        rpm.get("result_item_count") == len(inventory)
        and rpm.get("result_sha256") == canonical_sha256(inventory),
        "final SDK RPMDB differs from qualified host runtime",
    )
    if marker_path is None:
        marker_path = Path(
            "/usr/share/crossforge/rpm-locks/host-runtime.json"
        )
    require(
        marker_path.is_file()
        and not marker_path.is_symlink()
        and sha256_file(marker_path) == rpm.get("marker_sha256"),
        "final SDK host-runtime marker identity differs",
    )
    marker = load_json(marker_path)
    require(
        set(marker)
        == {
            "schema_version",
            "kind",
            "role",
            "lock_sha256",
            "transaction_sha256",
            "result_sha256",
            "result_item_count",
            "release_binding",
        }
        and marker.get("schema_version") == 2
        and marker.get("kind") == "host-rpm-install-marker"
        and marker.get("role") == "host-runtime"
        and marker.get("lock_sha256") == rpm.get("lock_sha256")
        and marker.get("transaction_sha256")
        == rpm.get("transaction_sha256")
        and marker.get("result_sha256") == rpm.get("result_sha256")
        and marker.get("result_item_count") == rpm.get("result_item_count")
        and marker.get("release_binding") == expected_binding,
        "final SDK host-runtime marker content differs",
    )
    marker_directory = marker_path.parent
    require(
        sorted(path.name for path in marker_directory.iterdir())
        == ["host-runtime.json"],
        "build host RPM markers leaked into final SDK",
    )
    return {
        "report_sha256": sha256_file(report_path),
        "marker_sha256": sha256_file(marker_path),
        "release_binding": expected_binding,
        "rpm_result_item_count": len(inventory),
        "rpm_result_sha256": canonical_sha256(inventory),
    }


def qualify_prior_toolchain_report(
    arch,
    target,
    report_path,
    release,
    release_sha256,
    sysroot_sha256,
):
    require(
        report_path.is_file() and not report_path.is_symlink(),
        "%s toolchain qualification report is missing or unsafe" % arch,
    )
    report = load_json(report_path)
    try:
        component = RELEASE_COMPONENTS["toolchain_qualification_component"](
            release, arch
        )
    except ProjectionError as error:
        raise QualificationError(str(error)) from error
    require(
        report.get("target") == target
        and report.get("release_sha256") == release_sha256
        and report.get("sysroot_sha256") == sysroot_sha256
        and report.get("compiler_version") == release["gts"]["gcc_version"]
        and re.search(
            r"(?<![0-9.])%s(?![0-9.])"
            % re.escape(release["binutils"]["version"]),
            report.get("binutils_version", ""),
        )
        is not None
        and report.get("sources")
        == {
            "gcc": release["gts"]["source"],
            "binutils": release["binutils"]["source"],
        }
        and report.get("qualification_component") == component,
        "%s prior toolchain qualification is not release-bound" % arch,
    )
    if arch == "aarch64":
        require(
            report.get("qualification_schema_version") == 1
            and report.get("report_kind")
            == "crossforge-toolchain-qualification"
            and report.get("locked_sysroot_execution", {}).get("status")
            == "passed"
            and report.get("clean_runtime_execution", {}).get("status")
            == "passed",
            "aarch64 prior runtime qualification did not pass",
        )
    else:
        clean_marker = report_path.with_name("x86_64-clean-runtime.ok")
        require(
            clean_marker.is_file()
            and not clean_marker.is_symlink()
            and clean_marker.read_bytes() == b"passed\n",
            "x86_64 clean-runtime qualification marker differs",
        )
    return {
        "component": component,
        "report_sha256": sha256_file(report_path),
    }


def qualify_target_compilers(work, qemu, release):
    c_source = work / "target.c"
    c_source.write_text(
        "#include <stdio.h>\n"
        "int main(void){puts(\"crossforge-final-c\");return 0;}\n",
        encoding="utf-8",
    )
    cxx_source = work / "target.cpp"
    cxx_source.write_text(
        "#include <iostream>\n"
        "int main(){std::cout<<\"crossforge-final-cxx\\n\";}\n",
        encoding="utf-8",
    )
    target_releases = release_target_map(release)
    release_sha256 = canonical_sha256(release)
    result = []
    for arch in ("x86_64", "aarch64"):
        profile = TARGETS[arch]
        release_target = target_releases[arch]
        prefix = Path("/opt/crossforge/targets") / profile["triple"]
        sysroot = Path("/opt/crossforge/sysroots/el8") / arch
        gcc = prefix / "bin" / (profile["triple"] + "-gcc")
        gxx = prefix / "bin" / (profile["triple"] + "-g++")
        readelf = prefix / "bin" / (profile["triple"] + "-readelf")
        resolved_prefix = prefix.resolve()
        require(
            all(
                path.is_file()
                and (
                    path.resolve() == resolved_prefix
                    or resolved_prefix in path.resolve().parents
                )
                for path in (gcc, gxx, readelf)
            ),
            "%s cross tools are missing" % arch,
        )
        sysroot_lock_path = sysroot / "usr/share/crossforge/sysroot-lock.json"
        require(
            sysroot_lock_path.is_file() and not sysroot_lock_path.is_symlink(),
            "%s sysroot lock is missing or unsafe" % arch,
        )
        sysroot_sha256 = canonical_sha256(load_json(sysroot_lock_path))
        require(
            sysroot_sha256
            == release_target["sysroot"]["canonical_sha256"],
            "%s sysroot differs from release" % arch,
        )
        machine, _stderr = run([gcc, "-dumpmachine"])
        require(machine.strip() == profile["triple"], "%s compiler target differs" % arch)
        version, _stderr = run([gcc, "-dumpfullversion"])
        require(
            version.strip() == release["gts"]["gcc_version"],
            "%s compiler version differs" % arch,
        )
        cxx_machine, _stderr = run([gxx, "-dumpmachine"])
        cxx_version, _stderr = run([gxx, "-dumpfullversion"])
        require(
            cxx_machine.strip() == profile["triple"]
            and cxx_version.strip() == release["gts"]["gcc_version"],
            "%s C++ compiler identity differs" % arch,
        )
        compiler_evidence = {
            "gcc": {"path": str(gcc), "sha256": sha256_file(gcc)},
            "g++": {"path": str(gxx), "sha256": sha256_file(gxx)},
        }
        printed_sysroot, _stderr = run([gcc, "-print-sysroot"])
        require(
            printed_sysroot.strip() == str(sysroot),
            "%s compiler sysroot differs" % arch,
        )
        program_evidence = {}
        for program in ("as", "ld"):
            printed, _stderr = run([gcc, "-print-prog-name=" + program])
            path = Path(printed.strip())
            resolved = path.resolve()
            require(
                path.is_file()
                and (resolved == prefix.resolve() or prefix.resolve() in resolved.parents),
                "%s %s escaped its target prefix" % (arch, program),
            )
            program_evidence[program] = {
                "path": str(path),
                "sha256": sha256_file(path),
            }
        ld_version, _stderr = run([program_evidence["ld"]["path"], "--version"])
        ld_version_line = ld_version.splitlines()[0] if ld_version.splitlines() else ""
        require(
            re.search(
                r"(?<![0-9.])%s(?![0-9.])"
                % re.escape(release["binutils"]["version"]),
                ld_version_line,
            )
            is not None,
            "%s linker version differs" % arch,
        )
        prior = qualify_prior_toolchain_report(
            arch,
            profile["triple"],
            Path("/opt/crossforge/qualification/toolchain")
            / (arch + ".json"),
            release,
            release_sha256,
            sysroot_sha256,
        )
        artifacts = []
        for name, compiler, source, extra, expected_stdout in (
            ("c", gcc, c_source, [], "crossforge-final-c\n"),
            ("cxx", gxx, cxx_source, [], "crossforge-final-cxx\n"),
            ("lto", gcc, c_source, ["-flto"], "crossforge-final-c\n"),
        ):
            output = work / (arch + "-" + name)
            run(
                [compiler, "-O2"]
                + extra
                + ["-Wl,-z,relro,-z,now", source, "-o", output]
            )
            header, _stderr = run([readelf, "-h", output])
            require(
                re.search(
                    r"^\s*Machine:\s+%s\s*$"
                    % re.escape(profile["machine"]),
                    header,
                    re.MULTILINE,
                )
                is not None,
                "%s target executable machine differs" % arch,
            )
            dynamic = audit_dynamic_elf(
                readelf, output, profile["interpreter"]
            )
            if arch == "x86_64":
                command = [output]
            else:
                executor = release["qemu"]["executor"]
                command = [
                    qemu,
                    "-cpu",
                    executor["cpu"],
                    "-r",
                    executor["uname_release"],
                    "-L",
                    "/opt/crossforge/sysroots/el8/aarch64",
                    output,
                ]
            stdout, _stderr = run(command, env=clean_environment())
            require(
                stdout == expected_stdout,
                "%s %s execution differs" % (arch, name),
            )
            artifacts.append(
                {
                    "kind": name,
                    "sha256": sha256_file(output),
                    "dynamic": dynamic,
                }
            )
        result.append(
            {
                "arch": arch,
                "triple": profile["triple"],
                "compiler_version": version.strip(),
                "compilers": compiler_evidence,
                "linker_version": ld_version_line,
                "programs": program_evidence,
                "prior_qualification": prior,
                "sysroot_sha256": sysroot_sha256,
                "artifacts": artifacts,
            }
        )
    return result


def qualify_qemu(path, expected_sha256, expected_version):
    require(path.is_file() and not path.is_symlink(), "QEMU executor is missing or unsafe")
    require(sha256_file(path) == expected_sha256, "QEMU executor digest differs")
    version, _stderr = run([path, "--version"])
    first = version.splitlines()[0] if version.splitlines() else ""
    require(
        first.startswith("qemu-aarch64 version " + expected_version),
        "QEMU executor version differs",
    )
    headers, _stderr = run(["/usr/bin/readelf", "-l", path])
    require(
        "Requesting program interpreter" not in headers,
        "QEMU executor is not static",
    )
    return {
        "path": str(path),
        "sha256": expected_sha256,
        "version": expected_version,
    }


def phase_for_rows(rows):
    phases = []
    for phase in range(1, ROW_CONTRACT["LATEST_PHASE"] + 1):
        try:
            expected_rows = list(ROW_CONTRACT["rows_for_phase"](phase))
        except ContractError:
            continue
        if rows == expected_rows:
            phases.append(phase)
    require(len(phases) == 1, "final SDK Python rows are not one exact phase")
    return phases[0]


def qualify(release_path, rows, qemu):
    require(
        release_path.is_file() and not release_path.is_symlink(),
        "final SDK release manifest is missing or unsafe",
    )
    release = load_json(release_path)
    forbidden_environment = (
        "CC",
        "CPATH",
        "CXX",
        "HOSTRUNNER",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "LIBRARY_PATH",
        "PYTHONHOME",
        "PYTHONPATH",
    )
    leaked_environment = [
        name for name in forbidden_environment if name in os.environ
    ]
    require(not leaked_environment, "build environment leaked into final SDK")
    require(Path.cwd() == Path("/workspace"), "final SDK workdir differs")
    require(
        os.environ.get("PATH", "").split(":", 1)[0]
        == "/opt/rh/gcc-toolset-15/root/usr/bin",
        "final SDK PATH does not prefer GTS15",
    )
    entries = {}
    for entry in release["python"]["versions"]:
        minor = entry["version"].rsplit(".", 1)[0]
        entries["cp" + minor.replace(".", "")] = entry
    phase = phase_for_rows(rows)
    require(set(rows).issubset(entries), "final SDK Python row is absent from release")
    release_sha256 = canonical_sha256(release)
    host_report_path = Path("/opt/crossforge/qualification/host-runtime.json")
    host_runtime = qualify_host_runtime(release, host_report_path)
    build_pythons = []
    target_pythons = []
    for row in rows:
        prefix = Path("/opt/crossforge/python") / row / "build"
        qualification_directory = (
            Path("/opt/crossforge/qualification/python") / row
        )
        manifest_path = qualification_directory / "row.json"
        require(
            manifest_path.is_file() and not manifest_path.is_symlink(),
            "%s row manifest is missing or unsafe" % row,
        )
        manifest = load_json(manifest_path)
        manifest_sha256 = sha256_file(manifest_path)
        version = entries[row]["version"]
        require(
            set(manifest) == ROW_MANIFEST_KEYS
            and manifest.get("schema_version") == 2
            and manifest.get("adapter") == entries[row]["adapter"],
            "%s row manifest shape or adapter differs" % row,
        )
        build_pythons.append(
            qualify_build_python(
                prefix,
                row,
                version,
                manifest,
                release_sha256,
            )
        )
        target_pythons.append(
            {
                "row": row,
                "version": version,
                "row_manifest_sha256": manifest_sha256,
                "targets": qualify_target_pythons(
                    prefix.parent,
                    qualification_directory,
                    row,
                    version,
                    manifest,
                    release_sha256,
                ),
            }
        )
    executor = release["qemu"]["executor"]
    qemu_evidence = qualify_qemu(
        qemu,
        executor["binary_sha256"],
        release["qemu"]["version"],
    )
    with tempfile.TemporaryDirectory(prefix="crossforge-final-sdk-") as temporary:
        compilers = qualify_target_compilers(
            Path(temporary), qemu, release
        )
    return {
        "schema_version": 1,
        "kind": "crossforge-final-sdk-qualification",
        "status": "passed",
        "phase": phase,
        "release_sha256": release_sha256,
        "environment": {
            "path_prefix": os.environ["PATH"].split(":", 1)[0],
            "workdir": str(Path.cwd()),
            "forbidden_variables": list(forbidden_environment),
        },
        "host_runtime": host_runtime,
        "build_pythons": build_pythons,
        "target_pythons": target_pythons,
        "target_compilers": compilers,
        "qemu": qemu_evidence,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--rows", nargs="+", required=True)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = qualify(
        arguments.release,
        arguments.rows,
        arguments.qemu,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_name(arguments.output.name + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(arguments.output)
    print("qualified final SDK host integration: %s" % arguments.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, QualificationError, TypeError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
