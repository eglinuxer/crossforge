#!/usr/bin/env python3
"""Create and verify native AArch64 evidence for an immutable candidate."""

import argparse
import hashlib
import io
import json
import os
import platform
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from loader_evidence import normalize_loader_listing


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
CANDIDATE = runpy.run_path(str(REPOSITORY / "scripts/candidate_manifest.py"))
COMPONENT = runpy.run_path(str(REPOSITORY / "scripts/release_component.py"))
ValidationError = STRICT["ValidationError"]
CandidateError = CANDIDATE["CandidateError"]
ComponentError = COMPONENT["ComponentError"]
BUNDLE_SCHEMA_ID = (
    "https://crossforge.dev/schemas/native-aarch64-probe-bundle.schema.json"
)
REPORT_SCHEMA_ID = (
    "https://crossforge.dev/schemas/native-aarch64-qualification.schema.json"
)
BINDING_SCHEMA_ID = "https://crossforge.dev/schemas/release-binding.schema.json"
TARGET = "aarch64-unknown-linux-gnu"
ARTIFACTS = (
    "catch",
    "hello",
    "libgcc-helper",
    "libstdc++-nonshared-audit.so",
    "libthrow.so",
    "lto",
    "lto-archive",
    "modern",
)
EXECUTABLES = (
    "catch",
    "hello",
    "libgcc-helper",
    "lto",
    "lto-archive",
    "modern",
)
COMPILE_BINARIES = set(ARTIFACTS + ("compiler-default-canary",))
MAX_MEMBER_SIZE = 64 * 1024 * 1024
MAX_BUNDLE_SIZE = 256 * 1024 * 1024
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class NativeReleaseError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise NativeReleaseError(message)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def reject_nonfinite(value):
    raise NativeReleaseError("non-finite JSON number: %s" % value)


def load_json_bytes(payload, label):
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonfinite,
        )
    except (UnicodeError, ValueError) as error:
        raise NativeReleaseError("cannot load %s: %s" % (label, error)) from error
    require(isinstance(value, dict), "%s must contain an object" % label)
    return value


def load_json(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "invalid JSON file: %s" % path)
    try:
        require(
            0 < path.stat().st_size <= MAX_MEMBER_SIZE,
            "JSON file size is invalid: %s" % path,
        )
        return load_json_bytes(path.read_bytes(), str(path))
    except OSError as error:
        raise NativeReleaseError("cannot load %s: %s" % (path, error)) from error


def canonical_bytes(document):
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(document):
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_schema(document, schema_path, expected_id):
    schema = load_json(schema_path)
    require(schema.get("$id") == expected_id, "schema identity differs")
    try:
        STRICT["validate_schema_subset"](schema)
        STRICT["validate"](document, schema, schema, "$")
    except ValidationError as error:
        raise NativeReleaseError("schema validation failed: %s" % error) from error


def validated_release(path, schema_path):
    release = load_json(path)
    schema = load_json(schema_path)
    try:
        STRICT["validate_schema_subset"](schema)
        STRICT["validate"](release, schema, schema, "$")
    except ValidationError as error:
        raise NativeReleaseError("release validation failed: %s" % error) from error
    return release


def validated_candidate(path, release, schema_path):
    document = load_json(path)
    schema = CANDIDATE["load_candidate_schema"](schema_path)
    try:
        digest = CANDIDATE["validate_candidate"](document, release, schema)
    except (CandidateError, ValidationError) as error:
        raise NativeReleaseError(str(error)) from error
    return document, {
        "canonical_sha256": digest,
        "digest": document["digest"],
        "platform_manifest_digest": document["platform_manifest_digest"],
        "source_commit": document["source_commit"],
    }


def validated_binding(path, schema_path, release):
    binding = load_json(path)
    validate_schema(binding, schema_path, BINDING_SCHEMA_ID)
    require(
        binding.get("release")
        == {
            "schema": "./schemas/release.schema.json",
            "schema_version": 1,
            "canonical_sha256": canonical_sha256(release),
        },
        "release binding does not bind the selected release",
    )
    records = {}
    for record in binding["components"]:
        require(
            record["component"] not in records,
            "release binding component is duplicated",
        )
        records[record["component"]] = record
    return records


def component_identity(
    path, expected_component, expected_scope, expected_canonical_sha256
):
    require(
        HEX_SHA256.match(expected_canonical_sha256 or ""),
        "expected component digest is invalid",
    )
    try:
        COMPONENT["load_component"](
            path,
            expected_component,
            expected_scope,
            expected_canonical_sha256,
        )
    except ComponentError as error:
        raise NativeReleaseError(str(error)) from error
    return {
        "component": expected_component,
        "canonical_sha256": expected_canonical_sha256,
    }


def selected_component(
    records, path, expected_component, expected_scope
):
    record = records.get(expected_component)
    require(
        record is not None
        and record["scope"] == expected_scope
        and (REPOSITORY / record["path"]).resolve() == Path(path).resolve(),
        "release binding component selection differs: %s" % expected_component,
    )
    return component_identity(
        path,
        expected_component,
        expected_scope,
        record["canonical_sha256"],
    )


def artifact_identity(path, name):
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise NativeReleaseError("cannot inspect artifact %s: %s" % (name, error))
    require(stat.S_ISREG(metadata.st_mode), "artifact is not a regular file: %s" % name)
    require(not path.is_symlink(), "artifact is a symlink: %s" % name)
    require(metadata.st_size > 0, "artifact is empty: %s" % name)
    require(metadata.st_size <= MAX_MEMBER_SIZE, "artifact is too large: %s" % name)
    require(
        stat.S_IMODE(metadata.st_mode) == 0o755,
        "artifact mode differs from 0755: %s" % name,
    )
    return {
        "name": name,
        "sha256": sha256_file(path),
        "size": metadata.st_size,
        "mode": "0755",
    }


def verify_compile_report(report, release, qualification_component):
    require(report.get("target") == TARGET, "compile report target differs")
    require(
        report.get("release_sha256") == canonical_sha256(release),
        "compile report release digest differs",
    )
    require(
        report.get("qualification_component") == qualification_component,
        "compile report qualification component differs",
    )
    require(
        report.get("native_release_execution")
        == {"status": "required", "executor": "native-el8-aarch64"},
        "compile report native release requirement differs",
    )
    for field in ("locked_sysroot_execution", "clean_runtime_execution"):
        require(
            report.get(field, {}).get("status") == "not_run",
            "compile report has an unexpected %s" % field,
        )
    require(
        set(report.get("binaries", {})) == COMPILE_BINARIES,
        "compile report binary set differs",
    )


def json_payload(document):
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def add_tar_bytes(archive, name, payload, mode):
    record = tarfile.TarInfo(name)
    record.size = len(payload)
    record.mode = mode
    record.uid = 0
    record.gid = 0
    record.uname = "root"
    record.gname = "root"
    record.mtime = 0
    archive.addfile(record, io.BytesIO(payload))


def write_bundle(output, manifest, compile_payload, artifact_paths):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    require(not output.is_symlink(), "bundle output must not be a symlink")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % output.name, suffix=".tmp", dir=str(output.parent)
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(str(temporary), "w", format=tarfile.USTAR_FORMAT) as archive:
            add_tar_bytes(archive, "manifest.json", json_payload(manifest), 0o644)
            add_tar_bytes(archive, "compile-report.json", compile_payload, 0o644)
            for name in ARTIFACTS:
                add_tar_bytes(
                    archive,
                    "artifacts/%s" % name,
                    Path(artifact_paths[name]).read_bytes(),
                    0o755,
                )
        require(
            temporary.stat().st_size <= MAX_BUNDLE_SIZE,
            "native AArch64 probe bundle is too large",
        )
        if output.exists():
            require(
                output.is_file() and output.read_bytes() == temporary.read_bytes(),
                "refusing to replace a different probe bundle: %s" % output,
            )
            temporary.unlink()
            return False
        os.replace(str(temporary), str(output))
        return True
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def create_bundle(arguments):
    release = validated_release(arguments.release, arguments.release_schema)
    _candidate, candidate_identity = validated_candidate(
        arguments.candidate, release, arguments.candidate_schema
    )
    binding = validated_binding(
        arguments.release_binding,
        arguments.release_binding_schema,
        release,
    )
    qualification_component = selected_component(
        binding,
        arguments.qualification_component,
        "toolchain/aarch64-qualification",
        "qualification",
    )
    candidate_policy_component = selected_component(
        binding,
        arguments.candidate_policy_component,
        "implementation/candidate-manifest",
        "supply",
    )
    compile_payload = Path(arguments.compile_report).read_bytes()
    compile_report = load_json_bytes(compile_payload, str(arguments.compile_report))
    verify_compile_report(compile_report, release, qualification_component)
    artifact_paths = {}
    artifact_records = []
    for name in ARTIFACTS:
        path = arguments.artifacts / name
        artifact_paths[name] = path
        artifact_records.append(artifact_identity(path, name))
    manifest = {
        "$schema": BUNDLE_SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-native-aarch64-probe-bundle",
        "target": TARGET,
        "candidate": candidate_identity,
        "release_sha256": canonical_sha256(release),
        "qualification_component": qualification_component,
        "candidate_policy_component": candidate_policy_component,
        "compile_report": {
            "sha256": sha256_bytes(compile_payload),
            "size": len(compile_payload),
        },
        "artifacts": artifact_records,
    }
    validate_schema(manifest, arguments.bundle_schema, BUNDLE_SCHEMA_ID)
    state = "wrote" if write_bundle(
        arguments.output, manifest, compile_payload, artifact_paths
    ) else "current"
    print(
        "%s native AArch64 probe bundle: %s (sha256:%s)"
        % (state, arguments.output, sha256_file(arguments.output))
    )


def expected_tar_members():
    return ["manifest.json", "compile-report.json"] + [
        "artifacts/%s" % name for name in ARTIFACTS
    ]


def read_bundle(path, expected_sha256=None):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "probe bundle is invalid")
    require(path.stat().st_size <= MAX_BUNDLE_SIZE, "probe bundle is too large")
    observed_sha256 = sha256_file(path)
    if expected_sha256 is not None:
        require(
            HEX_SHA256.match(expected_sha256 or "")
            and observed_sha256 == expected_sha256,
            "probe bundle digest differs",
        )
    payloads = {}
    with tarfile.open(str(path), "r:") as archive:
        members = archive.getmembers()
        require(
            [member.name for member in members] == expected_tar_members(),
            "probe bundle member set or order differs",
        )
        total = 0
        for member in members:
            require(member.isreg(), "probe bundle contains a non-file member")
            require(
                member.uid == 0
                and member.gid == 0
                and member.uname == "root"
                and member.gname == "root"
                and member.mtime == 0,
                "probe bundle metadata differs: %s" % member.name,
            )
            expected_mode = 0o755 if member.name.startswith("artifacts/") else 0o644
            require(member.mode == expected_mode, "probe bundle mode differs")
            require(
                0 < member.size <= MAX_MEMBER_SIZE,
                "probe bundle member size is invalid",
            )
            total += member.size
            require(total <= MAX_BUNDLE_SIZE, "probe bundle payload is too large")
            stream = archive.extractfile(member)
            require(stream is not None, "cannot read probe bundle member")
            payloads[member.name] = stream.read()
    return observed_sha256, payloads


def validate_bundle(arguments, release, candidate_identity, components):
    bundle_sha256, payloads = read_bundle(
        arguments.bundle, arguments.expected_bundle_sha256
    )
    manifest = load_json_bytes(payloads["manifest.json"], "bundle manifest")
    validate_schema(manifest, arguments.bundle_schema, BUNDLE_SCHEMA_ID)
    require(manifest["candidate"] == candidate_identity, "bundle candidate differs")
    require(
        manifest["release_sha256"] == canonical_sha256(release),
        "bundle release digest differs",
    )
    require(
        manifest["qualification_component"] == components["qualification"],
        "bundle qualification component differs",
    )
    require(
        manifest["candidate_policy_component"] == components["candidate_policy"],
        "bundle candidate policy component differs",
    )
    compile_payload = payloads["compile-report.json"]
    require(
        manifest["compile_report"]
        == {"sha256": sha256_bytes(compile_payload), "size": len(compile_payload)},
        "bundle compile report identity differs",
    )
    compile_report = load_json_bytes(compile_payload, "bundle compile report")
    verify_compile_report(compile_report, release, components["qualification"])
    require(
        [record["name"] for record in manifest["artifacts"]] == list(ARTIFACTS),
        "bundle artifact order differs",
    )
    for record in manifest["artifacts"]:
        payload = payloads["artifacts/%s" % record["name"]]
        require(
            record["sha256"] == sha256_bytes(payload)
            and record["size"] == len(payload)
            and record["mode"] == "0755",
            "bundle artifact identity differs: %s" % record["name"],
        )
    return bundle_sha256, manifest, payloads


def command_bytes(arguments, environment):
    try:
        process = subprocess.run(
            [str(argument) for argument in arguments],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        raise NativeReleaseError(
            "native command exceeded 30 seconds: %s"
            % " ".join(str(argument) for argument in arguments)
        ) from error
    if process.returncode != 0:
        raise NativeReleaseError(
            "native command failed (%s):\n%s"
            % (
                " ".join(str(argument) for argument in arguments),
                (process.stdout + process.stderr).decode("utf-8", "replace"),
            )
        )
    return process.stdout, process.stderr


def parse_os_release(path):
    values = {}
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        require(key not in values, "duplicate os-release field: %s" % key)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def identity_context(arguments, release):
    _candidate, candidate_identity = validated_candidate(
        arguments.candidate, release, arguments.candidate_schema
    )
    binding = validated_binding(
        arguments.release_binding,
        arguments.release_binding_schema,
        release,
    )
    components = {
        "qualification": selected_component(
            binding,
            arguments.qualification_component,
            "toolchain/aarch64-qualification",
            "qualification",
        ),
        "candidate_policy": selected_component(
            binding,
            arguments.candidate_policy_component,
            "implementation/candidate-manifest",
            "supply",
        ),
    }
    return candidate_identity, components


def write_json_once(path, document):
    payload = json_payload(document)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), "report output must not be a symlink")
    if path.exists():
        require(
            path.is_file() and path.read_bytes() == payload,
            "refusing to replace a different native qualification report",
        )
        return False
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return True


def execute_bundle(arguments):
    release = validated_release(arguments.release, arguments.release_schema)
    candidate_identity, components = identity_context(arguments, release)
    bundle_sha256, manifest, payloads = validate_bundle(
        arguments, release, candidate_identity, components
    )
    expected_runtime = "%s:%s@%s" % (
        release["base_image"]["repository"],
        release["base_image"]["tag"],
        release["base_image"]["manifests"]["arm64"],
    )
    require(arguments.runtime_reference == expected_runtime, "runtime reference differs")
    require(arguments.runner_label == "ubuntu-24.04-arm", "runner label differs")
    require(arguments.runner_arch == "ARM64", "runner architecture differs")
    require(arguments.host_machine == "aarch64", "host machine differs")
    require(platform.machine() == "aarch64", "container is not native AArch64")
    require(os.uname().machine == "aarch64", "container uname is not AArch64")
    require(
        shutil.which("qemu-aarch64") is None
        and not Path("/usr/bin/qemu-aarch64").exists()
        and not Path("/usr/local/bin/qemu-aarch64").exists(),
        "native runtime unexpectedly contains a QEMU executor",
    )
    os_release_path = Path("/etc/os-release")
    os_release = parse_os_release(os_release_path)
    require(
        os_release.get("ID") == "rocky" and os_release.get("VERSION_ID") == "8.10",
        "native runtime is not Rocky Linux 8.10",
    )
    loader = Path("/lib/ld-linux-aarch64.so.1")
    require(loader.is_file(), "native AArch64 loader is missing")
    work = Path("/tmp/crossforge-native-aarch64-probes")
    require(not work.exists(), "native probe work directory is not clean")
    artifacts = work / "artifacts"
    artifacts.mkdir(parents=True)
    try:
        for name in ARTIFACTS:
            path = artifacts / name
            path.write_bytes(payloads["artifacts/%s" % name])
            path.chmod(0o755)
        environment = {
            "HOME": "/tmp",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        }
        loader_stdout, loader_stderr = command_bytes(
            [loader, "--list", artifacts / "catch"], environment
        )
        loader_text = (loader_stdout + loader_stderr).decode("utf-8", "replace")
        require("not found" not in loader_text, "native loader has an unresolved DSO")
        loader_dependencies = normalize_loader_listing(loader_text)
        require(
            loader_dependencies == sorted(set(loader_dependencies)),
            "native loader evidence is not canonical",
        )
        for dependency in ("libthrow.so", "libstdc++.so.6", "libgcc_s.so.1", "libc.so.6"):
            require(
                any(line.startswith(dependency + " ") for line in loader_dependencies),
                "native loader evidence is missing %s" % dependency,
            )
        executions = {}
        for name in EXECUTABLES:
            stdout, _stderr = command_bytes([artifacts / name], environment)
            if name == "hello":
                require(stdout == b"crossforge-c-ok\n", "native C output differs")
            elif name == "modern":
                require(stdout == b"crossforge-cxx-ok\n", "native C++ output differs")
            executions[name] = {
                "status": "passed",
                "stdout_sha256": sha256_bytes(stdout),
            }
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
    report = {
        "$schema": REPORT_SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-native-aarch64-release-qualification",
        "status": "passed",
        "executor": "native",
        "target": TARGET,
        "candidate": candidate_identity,
        "release_sha256": canonical_sha256(release),
        "qualification_component": components["qualification"],
        "candidate_policy_component": components["candidate_policy"],
        "bundle_sha256": bundle_sha256,
        "compile_report_sha256": manifest["compile_report"]["sha256"],
        "artifacts": manifest["artifacts"],
        "host": {
            "runner_label": arguments.runner_label,
            "runner_arch": arguments.runner_arch,
            "host_machine": arguments.host_machine,
            "container_machine": platform.machine(),
            "kernel_release": os.uname().release,
        },
        "runtime": {
            "reference": arguments.runtime_reference,
            "index_digest": release["base_image"]["digest"],
            "manifest_digest": release["base_image"]["manifests"]["arm64"],
            "os_release_sha256": sha256_file(os_release_path),
            "loader_sha256": sha256_file(loader),
        },
        "loader_dependencies": loader_dependencies,
        "executions": executions,
    }
    validate_report_document(
        report,
        release,
        candidate_identity,
        components,
        manifest,
        bundle_sha256,
    )
    validate_schema(report, arguments.report_schema, REPORT_SCHEMA_ID)
    state = "wrote" if write_json_once(arguments.output, report) else "current"
    print("%s native AArch64 qualification: %s" % (state, arguments.output))


def validate_report_document(
    report, release, candidate_identity, components, manifest, bundle_sha256
):
    require(report.get("status") == "passed", "native qualification did not pass")
    require(report.get("candidate") == candidate_identity, "report candidate differs")
    require(
        report.get("release_sha256") == canonical_sha256(release),
        "report release digest differs",
    )
    require(
        report.get("qualification_component") == components["qualification"],
        "report qualification component differs",
    )
    require(
        report.get("candidate_policy_component") == components["candidate_policy"],
        "report candidate policy component differs",
    )
    require(report.get("bundle_sha256") == bundle_sha256, "report bundle differs")
    require(
        report.get("compile_report_sha256") == manifest["compile_report"]["sha256"],
        "report compile evidence differs",
    )
    require(report.get("artifacts") == manifest["artifacts"], "report artifacts differ")
    expected_runtime = "%s:%s@%s" % (
        release["base_image"]["repository"],
        release["base_image"]["tag"],
        release["base_image"]["manifests"]["arm64"],
    )
    runtime = report.get("runtime", {})
    require(
        runtime.get("reference") == expected_runtime
        and runtime.get("index_digest") == release["base_image"]["digest"]
        and runtime.get("manifest_digest")
        == release["base_image"]["manifests"]["arm64"],
        "report runtime identity differs",
    )
    dependencies = report.get("loader_dependencies")
    require(
        isinstance(dependencies, list)
        and dependencies == sorted(set(dependencies)),
        "report loader evidence is not canonical",
    )
    for dependency in (
        "libthrow.so",
        "libstdc++.so.6",
        "libgcc_s.so.1",
        "libc.so.6",
    ):
        require(
            any(line.startswith(dependency + " ") for line in dependencies),
            "report loader evidence is missing %s" % dependency,
        )
    expected_stdout = {
        "catch": b"",
        "hello": b"crossforge-c-ok\n",
        "libgcc-helper": b"",
        "lto": b"",
        "lto-archive": b"",
        "modern": b"crossforge-cxx-ok\n",
    }
    require(
        report.get("executions")
        == {
            name: {
                "status": "passed",
                "stdout_sha256": sha256_bytes(expected_stdout[name]),
            }
            for name in EXECUTABLES
        },
        "report execution evidence differs",
    )


def validate_report(arguments):
    release = validated_release(arguments.release, arguments.release_schema)
    candidate_identity, components = identity_context(arguments, release)
    bundle_sha256, manifest, _payloads = validate_bundle(
        arguments, release, candidate_identity, components
    )
    report = load_json(arguments.report)
    validate_schema(report, arguments.report_schema, REPORT_SCHEMA_ID)
    validate_report_document(
        report, release, candidate_identity, components, manifest, bundle_sha256
    )
    print(
        "valid native AArch64 qualification: %s (candidate %s)"
        % (arguments.report, candidate_identity["digest"])
    )


def add_identity_arguments(parser):
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument(
        "--candidate-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/candidate.schema.json",
    )
    parser.add_argument(
        "--release", type=Path, default=REPOSITORY / "config/release.json"
    )
    parser.add_argument(
        "--release-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release.schema.json",
    )
    parser.add_argument(
        "--release-binding",
        type=Path,
        default=REPOSITORY / "config/generated/release-binding.json",
    )
    parser.add_argument(
        "--release-binding-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release-binding.schema.json",
    )
    parser.add_argument(
        "--qualification-component",
        type=Path,
        default=(
            REPOSITORY
            / "config/generated/components/toolchain/aarch64-qualification.json"
        ),
    )
    parser.add_argument(
        "--candidate-policy-component",
        type=Path,
        default=(
            REPOSITORY
            / "config/generated/components/implementation/candidate-manifest.json"
        ),
    )
    parser.add_argument(
        "--bundle-schema",
        type=Path,
        default=(
            REPOSITORY
            / "config/schemas/native-aarch64-probe-bundle.schema.json"
        ),
    )


def add_bundle_arguments(parser):
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--expected-bundle-sha256", required=True)


def parser():
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = result.add_subparsers(dest="command")
    bundle = commands.add_parser("bundle", allow_abbrev=False)
    add_identity_arguments(bundle)
    bundle.add_argument("--compile-report", type=Path, required=True)
    bundle.add_argument("--artifacts", type=Path, required=True)
    bundle.add_argument("--output", type=Path, required=True)
    execute = commands.add_parser("execute", allow_abbrev=False)
    add_identity_arguments(execute)
    add_bundle_arguments(execute)
    execute.add_argument(
        "--report-schema",
        type=Path,
        default=(
            REPOSITORY
            / "config/schemas/native-aarch64-qualification.schema.json"
        ),
    )
    execute.add_argument("--runner-label", required=True)
    execute.add_argument("--runner-arch", required=True)
    execute.add_argument("--host-machine", required=True)
    execute.add_argument("--runtime-reference", required=True)
    execute.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate", allow_abbrev=False)
    add_identity_arguments(validate)
    add_bundle_arguments(validate)
    validate.add_argument(
        "--report-schema",
        type=Path,
        default=(
            REPOSITORY
            / "config/schemas/native-aarch64-qualification.schema.json"
        ),
    )
    validate.add_argument("--report", type=Path, required=True)
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        require(arguments.command in ("bundle", "execute", "validate"), "command required")
        if arguments.command == "bundle":
            create_bundle(arguments)
        elif arguments.command == "execute":
            execute_bundle(arguments)
        else:
            validate_report(arguments)
        return 0
    except (
        CandidateError,
        ComponentError,
        NativeReleaseError,
        OSError,
        tarfile.TarError,
        ValidationError,
    ) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
