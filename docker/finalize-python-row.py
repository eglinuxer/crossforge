#!/usr/bin/env python3
"""Bind two qualified target SDKs into one append-only CPython row artifact."""

import argparse
import hashlib
import json
import re
import runpy
import sys
from pathlib import Path


class FinalizationError(RuntimeError):
    pass


def support_script(name):
    sibling = Path(__file__).with_name(name)
    if sibling.is_file():
        return sibling
    repository_script = Path(__file__).resolve().parents[1] / "scripts" / name
    if repository_script.is_file():
        return repository_script
    raise FinalizationError("missing row support script: %s" % name)


QUALIFICATION_VALIDATOR = runpy.run_path(
    str(support_script("finalize-cpython-qualification.py"))
)
QualificationError = QUALIFICATION_VALIDATOR["FinalizationError"]
SDK_IDENTITY = runpy.run_path(str(support_script("python_sdk_identity.py")))
SDKIdentityError = SDK_IDENTITY["IdentityError"]
sdk_tree_identity = SDK_IDENTITY["sdk_tree_identity"]


TARGETS = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
}
SOURCE_MANIFEST_KEYS = {
    "schema_version",
    "kind",
    "row",
    "version",
    "minor",
    "compact",
    "adapter",
    "support",
    "release_sha256",
    "source",
    "patches",
}
SOURCE_KEYS = {"url", "size", "sha256"}
PATCH_KEYS = {"file", "sha256"}
QUALIFICATION_SOURCE_KEYS = {
    "url",
    "size",
    "sha256",
    "sigstore_bundle_sha256",
    "sigstore_verification",
}
QUALIFICATION_KEYS = {
    "qualification_schema_version",
    "report_kind",
    "status",
    "target",
    "version",
    "adapter",
    "release_sha256",
    "source",
    "sysroot_sha256",
    "python_sha256",
    "extension_sha256",
    "probe_sha256",
    "compile_report_sha256",
    "compile",
    "runtime_result_sha256",
    "executions",
}


def require(condition, message):
    if not condition:
        raise FinalizationError(message)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def load_json(path):
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FinalizationError("%s: %s" % (path, error)) from error


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--row", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    require(re.fullmatch(r"cp[0-9]+", arguments.row), "invalid CPython row")
    minor = arguments.version.rsplit(".", 1)[0]
    require(arguments.row == "cp" + minor.replace(".", ""), "row/version mismatch")
    release = load_json(arguments.release)
    release_sha256 = canonical_sha256(release)
    source_manifest = load_json(arguments.source_manifest)
    require(
        set(source_manifest) == SOURCE_MANIFEST_KEYS,
        "prepared source manifest fields differ from row contract",
    )
    require(
        source_manifest.get("kind") == "crossforge-cpython-source-row"
        and source_manifest.get("row") == arguments.row
        and source_manifest.get("version") == arguments.version
        and source_manifest.get("adapter") == arguments.adapter
        and source_manifest.get("release_sha256") == release_sha256,
        "prepared source manifest differs from row contract",
    )
    source = source_manifest.get("source")
    require(
        isinstance(source, dict) and set(source) == SOURCE_KEYS,
        "prepared source identity is invalid",
    )
    patches = source_manifest.get("patches")
    require(
        isinstance(patches, list)
        and all(
            isinstance(patch, dict) and set(patch) == PATCH_KEYS
            for patch in patches
        ),
        "prepared patch identity is invalid",
    )
    build_python = (
        arguments.root
        / "opt/crossforge/python"
        / arguments.row
        / "build/bin"
        / ("python" + minor)
    )
    require(build_python.is_file(), "row export is missing the build Python")
    build_python_sha256 = sha256_file(build_python)
    build_prefix = build_python.parents[1]
    build_tree = sdk_tree_identity(build_prefix)

    reports = {}
    for arch, target in TARGETS.items():
        report_path = (
            arguments.root
            / "opt/crossforge/qualification/python"
            / arguments.row
            / (arch + ".json")
        )
        report = load_json(report_path)
        try:
            QUALIFICATION_VALIDATOR["validate_final_report"](
                report, release, target, arguments.version
            )
        except QualificationError as error:
            raise FinalizationError(
                "%s qualification report is invalid: %s" % (arch, error)
            ) from error
        require(
            set(report) == QUALIFICATION_KEYS
            and report.get("qualification_schema_version") == 2
            and report.get("report_kind") == "crossforge-cpython-qualification"
            and report.get("status") == "passed",
            "%s qualification did not pass" % arch,
        )
        require(report.get("target") == target, "%s target mismatch" % arch)
        require(report.get("version") == arguments.version, "%s version mismatch" % arch)
        require(
            report.get("adapter") == arguments.adapter,
            "%s adapter mismatch" % arch,
        )
        embedded_compile = report.get("compile")
        require(
            isinstance(embedded_compile, dict)
            and embedded_compile.get("adapter") == arguments.adapter,
            "%s embedded compile adapter mismatch" % arch,
        )
        require(
            report.get("release_sha256") == release_sha256,
            "%s release identity mismatch" % arch,
        )
        report_source = report.get("source")
        require(
            isinstance(report_source, dict)
            and set(report_source) == QUALIFICATION_SOURCE_KEYS
            and all(
                report_source.get(name) == source.get(name)
                for name in ("url", "size", "sha256")
            ),
            "%s source identity mismatch" % arch,
        )
        target_python = (
            arguments.root
            / "opt/crossforge/python"
            / arguments.row
            / "targets"
            / target
            / "bin"
            / ("python" + minor)
        )
        require(target_python.is_file(), "%s target Python is missing" % arch)
        require(
            sha256_file(target_python) == report.get("python_sha256"),
            "%s target Python differs from qualification" % arch,
        )
        target_prefix = target_python.parents[1]
        target_tree = sdk_tree_identity(target_prefix)
        require(
            target_tree == embedded_compile.get("sdk_tree"),
            "%s target SDK tree differs from qualification" % arch,
        )
        lib_dynload = target_prefix / "lib" / ("python" + minor) / "lib-dynload"
        actual_elf_paths = [target_python]
        if lib_dynload.is_dir():
            actual_elf_paths.extend(sorted(lib_dynload.glob("*.so")))
        actual_elf_audit = {
            path.relative_to(target_prefix).as_posix(): sha256_file(path)
            for path in actual_elf_paths
        }
        compile_elf_audit = embedded_compile.get("elf_audit")
        require(
            isinstance(compile_elf_audit, dict)
            and set(actual_elf_audit)
            == {
                name
                for name in compile_elf_audit
                if not name.startswith("qualification/")
            },
            "%s SDK ELF inventory differs from qualification" % arch,
        )
        for name, digest in actual_elf_audit.items():
            audit = compile_elf_audit[name]
            require(
                isinstance(audit, dict) and audit.get("sha256") == digest,
                "%s SDK ELF digest differs from qualification: %s"
                % (arch, name),
            )
        compile_build = embedded_compile.get("build_python")
        require(
            isinstance(compile_build, dict)
            and compile_build.get("sha256") == build_python_sha256
            and compile_build.get("sdk_tree") == build_tree,
            "%s build Python tree differs from qualification" % arch,
        )
        reports[arch] = {
            "target": target,
            "report_sha256": sha256_file(report_path),
            "python_sha256": report["python_sha256"],
            "sdk_tree": target_tree,
        }

    manifest = {
        "schema_version": 1,
        "kind": "crossforge-cpython-row",
        "row": arguments.row,
        "version": arguments.version,
        "adapter": arguments.adapter,
        "support": source_manifest.get("support"),
        "release_sha256": release_sha256,
        "source": source,
        "source_manifest_sha256": sha256_file(arguments.source_manifest),
        "patches": patches,
        "build_python_sha256": build_python_sha256,
        "build_python_sdk_tree": build_tree,
        "qualifications": reports,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_name(arguments.output.name + ".tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(arguments.output)
    print("finalized CPython row: %s" % arguments.row)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        FinalizationError,
        SDKIdentityError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
