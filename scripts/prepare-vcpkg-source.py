#!/usr/bin/env python3
"""Authenticate and assemble the locked vcpkg registry and host tool."""

import argparse
import base64
import hashlib
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
COMPONENT_NAME = "sources/vcpkg"
COMPONENT_READER = None
HISTORY = runpy.run_path(str(SCRIPT_DIRECTORY / "fetch-vcpkg-history.py"))
HistoryError = HISTORY["FetchError"]
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
SHA512_RE = re.compile(r"^[0-9a-f]{128}\Z")
GIT_OID_RE = re.compile(r"^[0-9a-f]{40}\Z")


class PreparationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise PreparationError(message)


def component_reader():
    global COMPONENT_READER
    if COMPONENT_READER is None:
        COMPONENT_READER = runpy.run_path(
            str(SCRIPT_DIRECTORY / "release_component.py")
        )
    return COMPONENT_READER


def run(arguments, cwd=None, env=None):
    process = subprocess.run(
        [str(argument) for argument in arguments],
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    require(
        process.returncode == 0,
        "command failed (%s):\n%s"
        % (
            " ".join(str(argument) for argument in arguments),
            process.stdout + process.stderr,
        ),
    )
    return process.stdout, process.stderr


def run_bytes(arguments, cwd=None):
    process = subprocess.run(
        [str(argument) for argument in arguments],
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    require(
        process.returncode == 0,
        "binary command failed: %s" % " ".join(str(item) for item in arguments),
    )
    return process.stdout


def file_hash(path, algorithm):
    digest = hashlib.new(algorithm)
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def safe_input(root, relative, label):
    require(type(relative) is str, "%s path must be text" % label)
    logical = PurePosixPath(relative)
    require(
        not logical.is_absolute()
        and logical.parts
        and all(part not in ("", ".", "..") for part in logical.parts)
        and str(logical) == relative,
        "unsafe %s path" % label,
    )
    root = root.resolve()
    candidate = root.joinpath(*logical.parts)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise PreparationError("%s escaped its input root" % label) from error
    require(
        candidate == resolved and candidate.is_file(),
        "%s is missing or non-canonical" % label,
    )
    return candidate


def evidence_bytes(root, relative, label):
    path = safe_input(root, relative, label)
    encoded = b"".join(path.read_bytes().split())
    try:
        return base64.b64decode(encoded, validate=True)
    except (TypeError, ValueError) as error:
        raise PreparationError("invalid base64 %s" % label) from error


def material_values(document):
    result = {}
    for record in document["materials"]:
        path = record["path"]
        require(path not in result, "vcpkg component repeats a material")
        result[path] = record["value"]
    return result


def load_identity(component_path, component_sha256):
    reader = component_reader()
    try:
        document = reader["load_component"](
            component_path,
            COMPONENT_NAME,
            "build",
            component_sha256,
        )
    except reader["ComponentError"] as error:
        raise PreparationError("invalid vcpkg source component: %s" % error) from error
    require(document["dependencies"] == [], "vcpkg source has dependencies")
    values = material_values(document)
    fields = {
        "/vcpkg/repository": str,
        "/vcpkg/release/status": str,
        "/vcpkg/release/tag": str,
        "/vcpkg/release/tag_object": str,
        "/vcpkg/release/tag_evidence": str,
        "/vcpkg/release/commit": str,
        "/vcpkg/release/commit_evidence": str,
        "/vcpkg/registry_license/expression": str,
        "/vcpkg/registry_license/license_file": str,
        "/vcpkg/registry_license/license_sha256": str,
        "/vcpkg/registry_license/notice_file": str,
        "/vcpkg/registry_license/notice_sha256": str,
        "/vcpkg/tool/status": str,
        "/vcpkg/tool/repository": str,
        "/vcpkg/tool/tag": str,
        "/vcpkg/tool/commit": str,
        "/vcpkg/tool/commit_evidence": str,
        "/vcpkg/tool/url": str,
        "/vcpkg/tool/sha256": str,
        "/vcpkg/tool/sha512": str,
        "/vcpkg/tool/size": int,
        "/vcpkg/tool/signature/url": str,
        "/vcpkg/tool/signature/sha256": str,
        "/vcpkg/tool/signature/size": int,
        "/vcpkg/tool/signature/evidence": str,
        "/vcpkg/tool/signature/key/file": str,
        "/vcpkg/tool/signature/key/sha256": str,
        "/vcpkg/tool/signature/key/fingerprint": str,
        "/vcpkg/tool/license/expression": str,
        "/vcpkg/tool/license/license_file": str,
        "/vcpkg/tool/license/license_sha256": str,
        "/vcpkg/tool/license/notice_file": str,
        "/vcpkg/tool/license/notice_sha256": str,
    }
    require(set(values) == set(fields), "vcpkg source material set differs")
    for path, expected_type in fields.items():
        require(
            type(values[path]) is expected_type,
            "vcpkg material has wrong type: %s" % path,
        )
    for path in (
        "/vcpkg/registry_license/license_sha256",
        "/vcpkg/registry_license/notice_sha256",
        "/vcpkg/tool/sha256",
        "/vcpkg/tool/signature/sha256",
        "/vcpkg/tool/signature/key/sha256",
        "/vcpkg/tool/license/license_sha256",
        "/vcpkg/tool/license/notice_sha256",
    ):
        require(SHA256_RE.match(values[path]), "invalid SHA256: %s" % path)
    require(
        SHA512_RE.match(values["/vcpkg/tool/sha512"]),
        "invalid vcpkg-tool SHA512",
    )
    for path in (
        "/vcpkg/release/tag_object",
        "/vcpkg/release/commit",
        "/vcpkg/tool/commit",
        "/vcpkg/tool/signature/key/fingerprint",
    ):
        require(GIT_OID_RE.match(values[path]), "invalid Git identity: %s" % path)
    require(
        values["/vcpkg/release/status"] == "locked"
        and values["/vcpkg/tool/status"] == "locked"
        and values["/vcpkg/tool/signature/url"]
        == values["/vcpkg/tool/url"] + ".sig"
        and values["/vcpkg/registry_license/expression"] == "MIT"
        and values["/vcpkg/tool/license/expression"] == "MIT"
        and values["/vcpkg/tool/size"] > 0
        and values["/vcpkg/tool/signature/size"] > 0,
        "vcpkg source relationships differ",
    )
    return values


def verify_version_trees(repository):
    try:
        trees = HISTORY["version_trees"](repository)
        missing = HISTORY["missing_trees"](repository, trees)
    except HistoryError as error:
        raise PreparationError(str(error)) from error
    require(
        not missing,
        "vcpkg version database references missing tree objects",
    )
    return {
        "files": HISTORY["EXPECTED_VERSION_FILES"],
        "tree_set_sha256": hashlib.sha256(
            ("\n".join(trees) + "\n").encode("ascii")
        ).hexdigest(),
        "unique_trees": len(trees),
    }


def verify_git_repository(repository, identity, input_root):
    require(
        repository.is_dir()
        and not repository.is_symlink()
        and (repository / ".git").is_dir(),
        "vcpkg registry clone is invalid",
    )
    shallow, _stderr = run(
        ["git", "rev-parse", "--is-shallow-repository"], cwd=repository
    )
    require(shallow.strip() == "false", "vcpkg registry clone is shallow")
    remote, _stderr = run(["git", "remote", "get-url", "origin"], cwd=repository)
    refs, _stderr = run(
        ["git", "for-each-ref", "--format=%(refname)"], cwd=repository
    )
    tag = identity["/vcpkg/release/tag"]
    tag_object = identity["/vcpkg/release/tag_object"]
    commit = identity["/vcpkg/release/commit"]
    observed_tag, _stderr = run(["git", "rev-parse", tag], cwd=repository)
    observed_commit, _stderr = run(
        ["git", "rev-parse", tag + "^{}"], cwd=repository
    )
    require(
        remote.strip() == identity["/vcpkg/repository"]
        and refs.splitlines() == ["refs/tags/" + tag]
        and observed_tag.strip() == tag_object
        and observed_commit.strip() == commit,
        "vcpkg release tag moved",
    )
    tag_payload = run_bytes(["git", "cat-file", "tag", tag_object], cwd=repository)
    commit_payload = run_bytes(
        ["git", "cat-file", "commit", commit], cwd=repository
    )
    require(
        tag_payload
        == evidence_bytes(
            input_root,
            identity["/vcpkg/release/tag_evidence"],
            "vcpkg tag evidence",
        )
        and commit_payload
        == evidence_bytes(
            input_root,
            identity["/vcpkg/release/commit_evidence"],
            "vcpkg commit evidence",
        ),
        "vcpkg Git object evidence differs",
    )
    missing, _stderr = run(
        ["git", "rev-list", "--objects", "--missing=print", commit],
        cwd=repository,
    )
    require(
        not any(line.startswith("?") for line in missing.splitlines()),
        "vcpkg registry history has missing objects",
    )
    history, _stderr = run(["git", "rev-list", "--count", commit], cwd=repository)
    history_count = int(history.strip())
    require(history_count == 30001, "vcpkg registry history is incomplete")
    run(["git", "fsck", "--full", "--no-dangling"], cwd=repository)
    run(["git", "checkout", "--detach", "--force", commit], cwd=repository)
    status, _stderr = run(["git", "status", "--porcelain"], cwd=repository)
    require(not status, "vcpkg registry checkout is dirty")
    version_database = verify_version_trees(repository)

    for name, digest_path in (
        (
            identity["/vcpkg/registry_license/license_file"],
            "/vcpkg/registry_license/license_sha256",
        ),
        (
            identity["/vcpkg/registry_license/notice_file"],
            "/vcpkg/registry_license/notice_sha256",
        ),
    ):
        path = repository / name
        require(
            path.is_file()
            and not path.is_symlink()
            and file_hash(path, "sha256")[0] == identity[digest_path],
            "vcpkg registry license material differs: %s" % name,
        )

    metadata = {}
    metadata_path = repository / "scripts/vcpkg-tool-metadata.txt"
    for line in metadata_path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        key, separator, value = line.partition("=")
        require(separator and key not in metadata, "invalid vcpkg-tool metadata")
        metadata[key] = value
    require(
        metadata.get("VCPKG_TOOL_RELEASE_TAG")
        == identity["/vcpkg/tool/tag"]
        and metadata.get("VCPKG_GLIBC_SHA")
        == identity["/vcpkg/tool/sha512"],
        "vcpkg registry does not bind the selected tool",
    )
    tree, _stderr = run(["git", "rev-parse", "HEAD^{tree}"], cwd=repository)
    return {
        "commit": commit,
        "history_commit_count": history_count,
        "tag": tag,
        "tag_object": tag_object,
        "tree": tree.strip(),
        "version_database": version_database,
    }


def verify_signature(tool, signature, identity, input_root):
    tool_sha256, tool_size = file_hash(tool, "sha256")
    tool_sha512, _size = file_hash(tool, "sha512")
    signature_sha256, signature_size = file_hash(signature, "sha256")
    require(
        (tool_sha256, tool_sha512, tool_size)
        == (
            identity["/vcpkg/tool/sha256"],
            identity["/vcpkg/tool/sha512"],
            identity["/vcpkg/tool/size"],
        ),
        "vcpkg-tool payload identity differs",
    )
    require(
        (signature_sha256, signature_size)
        == (
            identity["/vcpkg/tool/signature/sha256"],
            identity["/vcpkg/tool/signature/size"],
        )
        and signature.read_bytes()
        == evidence_bytes(
            input_root,
            identity["/vcpkg/tool/signature/evidence"],
            "vcpkg-tool signature evidence",
        ),
        "vcpkg-tool signature identity differs",
    )
    key = safe_input(
        input_root,
        identity["/vcpkg/tool/signature/key/file"],
        "Microsoft release key",
    )
    require(
        file_hash(key, "sha256")[0]
        == identity["/vcpkg/tool/signature/key/sha256"],
        "Microsoft release key digest differs",
    )
    expected_fingerprint = identity[
        "/vcpkg/tool/signature/key/fingerprint"
    ]
    with tempfile.TemporaryDirectory(prefix="crossforge-vcpkg-gpg-") as temporary:
        home = Path(temporary)
        os.chmod(str(home), 0o700)
        run(["gpg", "--batch", "--homedir", home, "--import", key])
        shown, _stderr = run(
            ["gpg", "--batch", "--homedir", home, "--with-colons", "--fingerprint"]
        )
        fingerprints = [
            line.split(":")[9].lower()
            for line in shown.splitlines()
            if line.startswith("fpr:")
        ]
        require(
            fingerprints == [expected_fingerprint],
            "Microsoft release key fingerprint differs",
        )
        verified, _stderr = run(
            [
                "gpg",
                "--batch",
                "--no-auto-key-retrieve",
                "--homedir",
                home,
                "--status-fd",
                "1",
                "--verify",
                signature,
                tool,
            ]
        )
        valid = [
            line.split()[2].lower()
            for line in verified.splitlines()
            if line.startswith("[GNUPG:] VALIDSIG ")
        ]
        require(
            valid == [expected_fingerprint],
            "vcpkg-tool signature fingerprint differs",
        )
    os.chmod(str(tool), 0o755)
    version, _stderr = run([tool, "version", "--disable-metrics"])
    expected_version = "vcpkg package management program version %s-%s" % (
        identity["/vcpkg/tool/tag"],
        identity["/vcpkg/tool/commit"],
    )
    require(
        version.splitlines() and version.splitlines()[0] == expected_version,
        "vcpkg-tool version differs",
    )
    dynamic, _stderr = run(["readelf", "--wide", "-d", tool])
    headers, _stderr = run(["readelf", "--wide", "-l", tool])
    require(
        "[Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]"
        in headers
        and all(tag not in dynamic for tag in ("RPATH", "RUNPATH", "TEXTREL")),
        "vcpkg-tool ELF policy differs",
    )
    tool_commit = evidence_bytes(
        input_root,
        identity["/vcpkg/tool/commit_evidence"],
        "vcpkg-tool commit evidence",
    )
    object_id = hashlib.sha1(
        ("commit %d\0" % len(tool_commit)).encode("ascii") + tool_commit
    ).hexdigest()
    require(
        object_id == identity["/vcpkg/tool/commit"],
        "vcpkg-tool commit evidence differs",
    )
    return {
        "commit": object_id,
        "sha256": tool_sha256,
        "sha512": tool_sha512,
        "signature_sha256": signature_sha256,
        "version": expected_version,
    }


def prepare(
    component_path,
    component_sha256,
    repository,
    tool,
    signature,
    input_root,
    output,
    manifest_path,
):
    identity = load_identity(component_path, component_sha256)
    require(not output.exists(), "vcpkg output already exists")
    repository_evidence = verify_git_repository(
        repository, identity, input_root
    )
    tool_evidence = verify_signature(tool, signature, identity, input_root)
    for name in (
        "downloads",
        "buildtrees",
        "packages",
        "installed",
        "vcpkg_installed",
    ):
        require(not (repository / name).exists(), "vcpkg clone contains build output")
    for name in ("logs",):
        shutil.rmtree(str(repository / ".git" / name), ignore_errors=True)
    for name in ("FETCH_HEAD", "ORIG_HEAD"):
        path = repository / ".git" / name
        if path.exists():
            path.unlink()
    shutil.copy2(str(tool), str(repository / "vcpkg"))
    os.chmod(str(repository / "vcpkg"), 0o755)
    (repository / "vcpkg.disable-metrics").touch()
    license_directory = repository / "licenses/vcpkg-tool"
    license_directory.mkdir(parents=True)
    license_evidence = {}
    for role in ("license", "notice"):
        relative = identity["/vcpkg/tool/license/%s_file" % role]
        source = safe_input(input_root, relative, "vcpkg-tool %s" % role)
        expected = identity["/vcpkg/tool/license/%s_sha256" % role]
        require(
            file_hash(source, "sha256")[0] == expected,
            "vcpkg-tool %s digest differs" % role,
        )
        destination = license_directory / source.name
        shutil.copy2(str(source), str(destination))
        license_evidence[role + "_sha256"] = expected

    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(repository), str(output))
    manifest = {
        "schema_version": 1,
        "kind": "crossforge-vcpkg-source",
        "component": {
            "name": COMPONENT_NAME,
            "canonical_sha256": component_sha256,
        },
        "registry": repository_evidence,
        "tool": tool_evidence,
        "licenses": license_evidence,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--component", type=Path, required=True)
    parser.add_argument("--component-sha256", required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--tool", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    arguments = parser.parse_args()
    prepare(
        arguments.component,
        arguments.component_sha256,
        arguments.repository,
        arguments.tool,
        arguments.signature,
        arguments.input_root,
        arguments.output,
        arguments.manifest,
    )
    print("prepared locked vcpkg registry: %s" % arguments.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        KeyError,
        OSError,
        PreparationError,
        TypeError,
        ValueError,
    ) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
