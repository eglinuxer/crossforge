#!/usr/bin/env python3
"""Bind AArch64 compile and QEMU runtime evidence into one report."""

import argparse
import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path


class FinalizationError(RuntimeError):
    pass


RELEASE_COMPONENTS = runpy.run_path(
    str(Path(__file__).with_name("render-release-components.py"))
)
ProjectionError = RELEASE_COMPONENTS["ProjectionError"]


RESULT_KEYS = {
    "schema_version",
    "tier",
    "status",
    "target",
    "cpu",
    "uname_release",
    "qemu_binary_sha256",
    "qemu_version",
    "runtime_os_release_sha256",
    "loader_sha256",
    "loader_evidence_sha256",
    "hello_stdout_sha256",
    "modern_stdout_sha256",
}
ARTIFACTS = (
    "hello",
    "modern",
    "lto",
    "lto-archive",
    "libgcc-helper",
    "libthrow.so",
    "libstdc++-nonshared-audit.so",
    "catch",
)


def require(condition, message):
    if not condition:
        raise FinalizationError(message)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path):
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise FinalizationError("%s: %s" % (path, error)) from error


def parse_runtime_result(path, expected_tier):
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise FinalizationError("%s: %s" % (path, error)) from error
    for line in lines:
        require("=" in line, "%s: malformed result line" % path)
        key, value = line.split("=", 1)
        require(key and value, "%s: empty result key or value" % path)
        require(key not in values, "%s: duplicate result key %s" % (path, key))
        values[key] = value
    require(set(values) == RESULT_KEYS, "%s: unexpected result fields" % path)
    require(values["schema_version"] == "1", "%s: unsupported result schema" % path)
    require(values["tier"] == expected_tier, "%s: runtime tier mismatch" % path)
    require(values["status"] == "passed", "%s: runtime did not pass" % path)
    require(
        values["target"] == "aarch64-unknown-linux-gnu",
        "%s: target mismatch" % path,
    )
    for key in (
        "qemu_binary_sha256",
        "runtime_os_release_sha256",
        "loader_sha256",
        "loader_evidence_sha256",
        "hello_stdout_sha256",
        "modern_stdout_sha256",
    ):
        require(
            len(values[key]) == 64
            and all(character in "0123456789abcdef" for character in values[key]),
            "%s: invalid %s" % (path, key),
        )
    loader_path = Path(str(path) + ".loader")
    require(loader_path.is_file(), "%s: loader evidence is missing" % path)
    require(
        sha256_file(loader_path) == values["loader_evidence_sha256"],
        "%s: loader evidence digest mismatch" % path,
    )
    loader_lines = loader_path.read_text(encoding="utf-8").splitlines()
    require(loader_lines, "%s: loader evidence is empty" % path)
    require(
        loader_lines == sorted(set(loader_lines)),
        "%s: loader evidence is not canonical" % path,
    )
    require(
        not any("not found" in line for line in loader_lines),
        "%s: loader evidence contains an unresolved dependency" % path,
    )
    require(
        not any("(0x" in line for line in loader_lines),
        "%s: loader evidence contains a runtime address" % path,
    )
    return {
        "status": "passed",
        "executor": "explicit-qemu",
        "tier": expected_tier,
        "result_sha256": sha256_file(path),
        "runtime_os_release_sha256": values["runtime_os_release_sha256"],
        "loader_sha256": values["loader_sha256"],
        "loader_evidence_sha256": values["loader_evidence_sha256"],
        "loader_dependencies": loader_lines,
        "stdout_sha256": {
            "hello": values["hello_stdout_sha256"],
            "modern": values["modern_stdout_sha256"],
        },
        "qemu": {
            "binary_sha256": values["qemu_binary_sha256"],
            "version": values["qemu_version"],
            "cpu": values["cpu"],
            "uname_release": values["uname_release"],
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-report", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--locked-sysroot-result", type=Path, required=True)
    parser.add_argument("--clean-runtime-result", type=Path, required=True)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    compile_report = load_json(arguments.compile_report)
    release = load_json(arguments.release)
    try:
        qualification_component = RELEASE_COMPONENTS[
            "toolchain_qualification_component"
        ](release, "aarch64")
    except ProjectionError as error:
        raise FinalizationError(str(error)) from error
    executor = release["qemu"]["executor"]
    require(
        compile_report.get("target") == "aarch64-unknown-linux-gnu",
        "compile report target mismatch",
    )
    release_canonical = json.dumps(
        release, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    require(
        compile_report.get("release_sha256")
        == hashlib.sha256(release_canonical.encode("utf-8")).hexdigest(),
        "compile report release digest mismatch",
    )
    require(
        compile_report.get("qualification_component")
        == qualification_component,
        "compile report qualification component mismatch",
    )
    require(
        release["abi"]["targets"]["aarch64"]["baseline"]
        == {
            "file": "abi/el8/aarch64.json",
            "canonical_sha256": compile_report.get("abi_baseline", {}).get(
                "canonical_sha256"
            ),
        },
        "compile report ABI baseline differs from release.json",
    )
    require(
        compile_report.get("runtime_executor") == executor,
        "compile report QEMU identity mismatch",
    )
    require(
        compile_report.get("runtime_base")
        == {
            "index_digest": release["base_image"]["digest"],
            "manifest_digest": release["base_image"]["manifests"]["arm64"],
        },
        "compile report runtime base mismatch",
    )
    for field in ("locked_sysroot_execution", "clean_runtime_execution"):
        require(
            compile_report.get(field, {}).get("status") == "not_run",
            "compile report has an unexpected %s state" % field,
        )
    require(
        compile_report.get("native_release_execution", {}).get("status") == "required",
        "native release gate is not explicit",
    )

    locked = parse_runtime_result(arguments.locked_sysroot_result, "locked-sysroot")
    clean = parse_runtime_result(arguments.clean_runtime_result, "clean-rocky")
    for result in (locked, clean):
        require(
            result["qemu"]
            == {
                "binary_sha256": executor["binary_sha256"],
                "version": release["qemu"]["version"],
                "cpu": executor["cpu"],
                "uname_release": executor["uname_release"],
            },
            "%s QEMU observation differs from release.json" % result["tier"],
        )

    require(arguments.qemu.is_file(), "QEMU executor is missing")
    observed_qemu_sha256 = sha256_file(arguments.qemu)
    require(
        observed_qemu_sha256 == executor["binary_sha256"],
        "mounted QEMU executor digest mismatch",
    )
    process = subprocess.run(
        [str(arguments.qemu), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    require(process.returncode == 0, "mounted QEMU executor did not run")
    version_line = process.stdout.splitlines()[0] if process.stdout.splitlines() else ""
    require(
        version_line.startswith("qemu-aarch64 version %s" % release["qemu"]["version"]),
        "mounted QEMU executor version mismatch",
    )

    artifact_sha256 = {}
    for name in ARTIFACTS:
        path = arguments.artifacts / name
        require(path.is_file(), "qualification artifact is missing: %s" % name)
        artifact_sha256[name] = sha256_file(path)

    final_report = dict(compile_report)
    final_report.update(
        {
            "report_kind": "crossforge-toolchain-qualification",
            "qualification_schema_version": 1,
            "compile_report_sha256": sha256_file(arguments.compile_report),
            "artifact_sha256": artifact_sha256,
            "locked_sysroot_execution": locked,
            "clean_runtime_execution": clean,
            "runtime_executor_observation": {
                "binary_sha256": observed_qemu_sha256,
                "version_line": version_line,
            },
        }
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_name(arguments.output.name + ".tmp")
    temporary.write_text(
        json.dumps(final_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(arguments.output)
    print("finalized: %s" % arguments.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FinalizationError, KeyError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
