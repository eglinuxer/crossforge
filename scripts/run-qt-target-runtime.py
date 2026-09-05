#!/usr/bin/env python3
"""Execute one Qt target consumer inside an explicit clean Rocky root."""

import argparse
import hashlib
import json
import os
import platform
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
LOADER = runpy.run_path(str(REPOSITORY / "scripts/loader_evidence.py"))
ValidationError = STRICT["ValidationError"]
SCHEMA_ID = "https://crossforge.dev/schemas/qt-target-runtime.schema.json"
TARGETS = {
    "x86_64": {
        "triple": "x86_64-unknown-linux-gnu",
        "tier": "clean-rocky",
        "machine": "Advanced Micro Devices X86-64",
    },
    "aarch64": {
        "triple": "aarch64-unknown-linux-gnu",
        "tier": "clean-rocky-qemu",
        "machine": "AArch64",
    },
}
ARTIFACT_PATTERNS = (
    "usr/lib/libQt6*.so*",
    "usr/lib/libavcodec.so*",
    "usr/lib/libavformat.so*",
    "usr/lib/libavutil.so*",
    "usr/lib/libswresample.so*",
    "usr/lib/libswscale.so*",
    "usr/lib64/libxcb-cursor.so*",
)
ARTIFACT_DIRECTORIES = (
    "usr/plugins",
    "usr/qml",
    "usr/resources",
    "usr/translations",
)
CONSUMER = "opt/crossforge-qualification/qt/qt-target-consumer"
PLUGIN_CONSUMER = "opt/crossforge-qualification/qt/qt-plugin-probe"
PLUGIN = "/usr/plugins/platforms/libqoffscreen.so"
LOADER_PROBES = (
    "/" + CONSUMER,
    PLUGIN,
    "/usr/plugins/platforms/libqxcb.so",
    "/usr/plugins/platforms/libqwayland-generic.so",
    "/usr/plugins/multimedia/libffmpegmediaplugin.so",
    "/usr/libexec/QtWebEngineProcess",
)
PLUGIN_PROBES = tuple(
    artifact for artifact in LOADER_PROBES if artifact.endswith(".so")
)


def require(condition, message):
    if not condition:
        raise ValidationError(message)


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
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ValidationError("%s: %s" % (path, error)) from error


def canonical_sha256(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def bounded_diagnostics(value, limit=120):
    lines = value.splitlines()
    selected = lines[-limit:]
    return "\n".join(line[:2000] for line in selected)


def load_schema(path, schema_name):
    document = load_json(path)
    schema = STRICT["load_json"](REPOSITORY / "config/schemas" / schema_name)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    return document


def require_root(path, allow_root=False):
    absolute = Path(os.path.abspath(str(path)))
    require(
        str(absolute) != "/" or allow_root,
        "refusing filesystem root as Qt runtime root",
    )
    require(absolute.is_dir() and not absolute.is_symlink(), "Qt runtime root is invalid")
    return absolute.resolve()


def record_path(root, path):
    relative = path.relative_to(root).as_posix()
    metadata = os.lstat(str(path))
    record = {
        "path": relative,
        "mode": stat.S_IMODE(metadata.st_mode),
        "type": "file",
    }
    if stat.S_ISLNK(metadata.st_mode):
        target = os.readlink(str(path))
        resolved = Path(os.path.realpath(str(path)))
        require(root == resolved or root in resolved.parents, "runtime artifact symlink escapes the root")
        record.update({"type": "symlink", "target": target})
    elif stat.S_ISREG(metadata.st_mode):
        record.update({"size": metadata.st_size, "sha256": sha256_file(path)})
    elif stat.S_ISDIR(metadata.st_mode):
        record["type"] = "directory"
    else:
        raise ValidationError("runtime artifact has an unsupported type: %s" % path)
    return record


def artifact_tree(root):
    roots = []
    for pattern in ARTIFACT_PATTERNS:
        roots.extend(root.glob(pattern))
    for relative in ARTIFACT_DIRECTORIES:
        path = root / relative
        require(path.is_dir() and not path.is_symlink(), "Qt runtime artifact directory is missing: %s" % relative)
        roots.append(path)
    for relative in (
        CONSUMER,
        PLUGIN_CONSUMER,
        "usr/libexec/QtWebEngineProcess",
    ):
        path = root / relative
        require(path.is_file() and not path.is_symlink(), "Qt runtime artifact is missing: %s" % relative)
        roots.append(path)
    records = {}

    def visit(path):
        record = record_path(root, path)
        records[record["path"]] = record
        if record["type"] == "directory":
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                visit(child)

    for path in sorted(set(roots), key=lambda item: item.as_posix()):
        visit(path)
    values = [records[name] for name in sorted(records)]
    require(values, "Qt runtime artifact tree is empty")
    return {"entries": len(values), "sha256": canonical_sha256(values)}


def runtime_environment():
    return {
        "HOME": "/tmp/crossforge-qt-home",
        "LANG": "C",
        "LC_ALL": "C",
        "LD_LIBRARY_PATH": "/usr/lib:/usr/lib64",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "QT_DEBUG_PLUGINS": "1",
        "QT_PLUGIN_PATH": "/usr/plugins",
        "QT_QPA_PLATFORM": "offscreen",
        "QT_QPA_PLATFORM_PLUGIN_PATH": "/usr/plugins/platforms",
        "QTWEBENGINEPROCESS_PATH": "/usr/libexec/QtWebEngineProcess",
        "QTWEBENGINE_DISABLE_SANDBOX": "1",
        "XDG_RUNTIME_DIR": "/tmp/crossforge-qt-runtime",
    }


def validate_rocky_release(os_release):
    require(
        re.search(r'^ID="?rocky"?$', os_release, re.MULTILINE),
        "runtime root is not Rocky",
    )
    require(
        re.search(r'^VERSION_ID="?8\.10"?$', os_release, re.MULTILINE),
        "runtime root is not Rocky 8.10",
    )


def runtime_command_prefix(arguments, environment):
    values = dict(environment)
    if arguments.native_release:
        command = ["timeout", "180s", "/usr/bin/env", "-i"]
        command.extend("%s=%s" % item for item in sorted(values.items()))
        return command
    if arguments.arch == "x86_64":
        command = [
            "timeout",
            "90s",
            "chroot",
            str(arguments.runtime_root),
            "/usr/bin/env",
            "-i",
        ]
        command.extend("%s=%s" % item for item in sorted(values.items()))
        return command
    command = [
        "timeout",
        "180s",
        "chroot",
        str(arguments.runtime_root),
        "/.crossforge/qemu-aarch64",
        "-L",
        "/",
        "-cpu",
        arguments.qemu_cpu,
        "-r",
        arguments.qemu_uname_release,
    ]
    for key, value in sorted(values.items()):
        command.extend(["-E", "%s=%s" % (key, value)])
    require(
        TARGETS[arguments.arch]["tier"] == "clean-rocky-qemu",
        "invalid AArch64 runtime tier",
    )
    return command


def runtime_command(arguments, environment):
    return runtime_command_prefix(arguments, environment) + ["/" + CONSUMER]


def loader_command(arguments, environment, artifact):
    loader = (
        "/lib64/ld-linux-x86-64.so.2"
        if arguments.arch == "x86_64"
        else "/lib/ld-linux-aarch64.so.1"
    )
    return runtime_command_prefix(arguments, environment) + [
        loader,
        "--list",
        artifact,
    ]


def plugin_command(arguments, environment, artifact):
    values = dict(environment)
    values["CROSSFORGE_QT_PLUGIN"] = artifact
    values["LD_DEBUG"] = "libs"
    return runtime_command_prefix(arguments, values) + [
        "/" + PLUGIN_CONSUMER,
    ]


def run(command, label):
    process = subprocess.run(
        [str(value) for value in command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    require(
        process.returncode == 0,
        "%s failed (%d): %s"
        % (
            label,
            process.returncode,
            bounded_diagnostics(process.stdout + process.stderr),
        ),
    )
    return process


def validate_executor(arguments, release):
    if arguments.native_release:
        require(arguments.arch == "aarch64", "native release requires AArch64")
        require(arguments.qemu is None, "native release must not receive QEMU")
        require(
            platform.machine() == "aarch64" and os.uname().machine == "aarch64",
            "native release is not executing on AArch64",
        )
        require(
            shutil.which("qemu-aarch64") is None
            and not Path("/usr/bin/qemu-aarch64").exists()
            and not Path("/usr/local/bin/qemu-aarch64").exists(),
            "native release unexpectedly contains QEMU",
        )
        return {"kind": "native"}
    if arguments.arch == "x86_64":
        require(arguments.qemu is None, "x86_64 runtime must not receive QEMU")
        return {"kind": "native"}
    executor = release["qemu"]["executor"]
    require(arguments.qemu is not None and arguments.qemu.is_file(), "AArch64 runtime requires QEMU")
    require(
        sha256_file(arguments.qemu) == executor["binary_sha256"],
        "QEMU binary digest differs from release",
    )
    version = run([arguments.qemu, "--version"], "QEMU version").stdout.splitlines()[0]
    require(
        version.startswith("qemu-aarch64 version " + release["qemu"]["version"]),
        "QEMU version differs from release",
    )
    require(
        arguments.qemu_cpu == executor["cpu"]
        and arguments.qemu_uname_release == executor["uname_release"],
        "QEMU CPU or uname contract differs",
    )
    return {
        "kind": "explicit-qemu",
        "binary_sha256": executor["binary_sha256"],
        "version": release["qemu"]["version"],
        "cpu": executor["cpu"],
        "uname_release": executor["uname_release"],
    }


def write_json(path, document):
    schema = STRICT["load_json"](
        REPOSITORY / "config/schemas/qt-target-runtime.schema.json"
    )
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    target = document["identity"]["target"]
    profile = TARGETS.get(target["arch"])
    require(profile is not None, "runtime evidence architecture differs")
    require(target["triple"] == profile["triple"], "runtime evidence target differs")
    tier = document["identity"]["tier"]
    require(
        tier == profile["tier"]
        or (target["arch"] == "aarch64" and tier == "native-release"),
        "runtime evidence tier differs",
    )
    executor = document["executor"]
    expected_executor_keys = (
        {"kind"}
        if tier in ("clean-rocky", "native-release")
        else {"kind", "binary_sha256", "version", "cpu", "uname_release"}
    )
    require(set(executor) == expected_executor_keys, "runtime executor fields differ")
    require(
        executor["kind"]
        == (
            "explicit-qemu"
            if tier == "clean-rocky-qemu"
            else "native"
        ),
        "runtime executor kind differs",
    )
    probes = document["execution"]["loader_probes"]
    require(
        [record["artifact"] for record in probes] == list(LOADER_PROBES),
        "runtime loader probe order differs",
    )
    for record in probes:
        dependencies = record["dependencies"]
        require(
            dependencies == sorted(set(dependencies))
            and record["sha256"] == canonical_sha256(dependencies),
            "runtime loader probe evidence differs: %s" % record["artifact"],
        )
    require(
        document["execution"]["loader"] == probes[0]["dependencies"]
        and document["execution"]["loader_sha256"] == probes[0]["sha256"],
        "runtime consumer loader evidence differs",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(str(temporary), 0o644)
        os.replace(str(temporary), str(path))
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def qualify(arguments):
    target = TARGETS.get(arguments.arch)
    require(target is not None and arguments.triple == target["triple"], "Qt runtime target differs")
    require(
        not arguments.native_release or arguments.runtime_root == Path("/"),
        "native release runtime root must be /",
    )
    arguments.runtime_root = require_root(
        arguments.runtime_root, allow_root=arguments.native_release
    )
    release = load_schema(arguments.release, "release.schema.json")
    build = load_schema(arguments.build_evidence, "qt-target-build.schema.json")
    overlay = load_schema(arguments.overlay_evidence, "qt-runtime-overlay.schema.json")
    expected_target = {"arch": arguments.arch, "triple": arguments.triple}
    require(build["identity"] == expected_target, "Qt build evidence target differs")
    require(overlay["identity"]["target"] == expected_target, "Qt overlay target differs")
    require(
        overlay["identity"]["release_sha256"] == canonical_sha256(release),
        "Qt overlay release binding differs",
    )
    executor = validate_executor(arguments, release)
    tier = "native-release" if arguments.native_release else target["tier"]
    require(
        not (arguments.runtime_root / "usr/share/crossforge/sysroot-lock.json").exists(),
        "clean Rocky root contains a Crossforge sysroot marker",
    )
    os_release = (arguments.runtime_root / "etc/os-release").read_text(encoding="utf-8")
    validate_rocky_release(os_release)
    for relative in ("tmp/crossforge-qt-home", "tmp/crossforge-qt-runtime"):
        path = arguments.runtime_root / relative
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(str(path), 0o700)
    environment = runtime_environment()
    loader_probes = []
    for artifact in LOADER_PROBES:
        require(
            (arguments.runtime_root / artifact.lstrip("/")).is_file(),
            "Qt loader probe artifact is missing: %s" % artifact,
        )
        if artifact in PLUGIN_PROBES:
            loader_process = run(
                plugin_command(arguments, environment, artifact),
                "Qt clean-Rocky plugin load probe %s" % artifact,
            )
            loader_text = loader_process.stdout + loader_process.stderr
            sentinel = "crossforge-dlopen-ok:%s" % artifact
            require(
                loader_text.count(sentinel) == 1,
                "Qt clean-Rocky plugin load sentinel differs for %s: %s"
                % (artifact, bounded_diagnostics(loader_text)),
            )
            dependencies = LOADER["normalize_loader_debug_listing"](
                loader_process.stderr
            )
            dependencies.append("dlopen:%s" % artifact)
            dependencies = sorted(set(dependencies))
        else:
            loader_process = run(
                loader_command(arguments, environment, artifact),
                "Qt clean-Rocky loader probe %s" % artifact,
            )
            dependencies = LOADER["normalize_loader_listing"](
                loader_process.stdout + loader_process.stderr
            )
            loader_text = loader_process.stdout + loader_process.stderr
        require(
            "not found" not in loader_text,
            "Qt clean-Rocky dependency is unresolved for %s" % artifact,
        )
        require(
            "/work/" not in loader_text,
            "Qt runtime loader leaked the build tree for %s" % artifact,
        )
        require(dependencies, "Qt clean-Rocky loader trace is empty")
        loader_probes.append(
            {
                "artifact": artifact,
                "dependencies": dependencies,
                "sha256": canonical_sha256(dependencies),
            }
        )
    loader = loader_probes[0]["dependencies"]
    process = run(
        runtime_command(arguments, environment),
        "Qt clean-Rocky offscreen consumer",
    )
    require(
        "libqoffscreen.so" in process.stderr
        and not any(
            marker in process.stderr
            for marker in ("Cannot load library", "not found", "could not connect")
        ),
        "Qt offscreen platform plugin was not loaded cleanly: %s"
        % bounded_diagnostics(process.stderr),
    )
    require(
        "/work/" not in process.stdout + process.stderr,
        "Qt runtime execution leaked the build tree",
    )
    consumer = arguments.runtime_root / CONSUMER
    document = {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-qt-target-runtime",
        "qt_version": "6.8.4",
        "identity": {
            "target": expected_target,
            "tier": tier,
            "base_image": overlay["identity"]["base_image"],
            "release_sha256": canonical_sha256(release),
            "runtime_qualification": overlay["identity"]["runtime_qualification"],
            "build_evidence_sha256": canonical_sha256(build),
            "overlay_evidence_sha256": canonical_sha256(overlay),
            "artifact_tree": artifact_tree(arguments.runtime_root),
        },
        "executor": executor,
        "execution": {
            "consumer_sha256": sha256_file(consumer),
            "exit_code": process.returncode,
            "stdout_sha256": sha256_text(process.stdout),
            "stderr_sha256": sha256_text(process.stderr),
            "loader": loader,
            "loader_sha256": canonical_sha256(loader),
            "loader_probes": loader_probes,
            "platform_plugin": PLUGIN,
        },
        "checks": {
            "clean_rocky": True,
            "dependency_closure": True,
            "plugin_dependency_closure": True,
            "offscreen_widget": True,
            "platform_plugin_loaded": True,
            "no_build_tree": True,
            "no_sysroot_marker": True,
        },
    }
    write_json(arguments.output, document)
    print("qualified Qt target runtime: %s" % arguments.output)


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--triple", required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--build-evidence", type=Path, required=True)
    parser.add_argument("--overlay-evidence", type=Path, required=True)
    parser.add_argument("--qemu", type=Path)
    parser.add_argument("--qemu-cpu")
    parser.add_argument("--qemu-uname-release")
    parser.add_argument("--native-release", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        qualify(arguments)
    except (OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
