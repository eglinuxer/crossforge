#!/usr/bin/env python3
"""Authenticate and safely prepare the locked Zstandard source subset."""

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
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
REPOSITORY = SCRIPT_DIRECTORY.parent
COMPONENT_NAME = "sources/zstd"
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\Z")
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
GIT_OID_RE = re.compile(r"^[0-9a-f]{40}\Z")
FINGERPRINT_RE = re.compile(r"^[0-9A-F]{40}\Z")
COMPONENT_READER = None


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


def sha256_file(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def canonical_sha256(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def safe_repository_file(repository, relative, label):
    require(type(relative) is str, "%s path must be text" % label)
    path = PurePosixPath(relative)
    require(
        not path.is_absolute()
        and path.parts
        and all(part not in ("", ".", "..") for part in path.parts)
        and str(path) == relative,
        "unsafe %s path: %r" % (label, relative),
    )
    root = repository.resolve()
    candidate = root.joinpath(*path.parts)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise PreparationError("%s escapes repository: %s" % (label, relative)) from error
    require(resolved == candidate and resolved.is_file(), "missing %s: %s" % (label, relative))
    return resolved


def evidence_bytes(repository, relative, label):
    path = safe_repository_file(repository, relative, label)
    payload = path.read_bytes()
    if path.suffix == ".b64":
        try:
            payload = base64.b64decode(b"".join(payload.split()), validate=True)
        except (ValueError, TypeError) as error:
            raise PreparationError("invalid base64 %s" % label) from error
    return payload


def git_object_sha1(kind, payload):
    header = ("%s %d\0" % (kind, len(payload))).encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()


def material_values(document):
    values = {}
    for record in document["materials"]:
        path = record["path"]
        require(path not in values, "zstd component repeats a material path")
        values[path] = record["value"]
    return values


def required_material(values, path, expected_type):
    require(path in values, "zstd component is missing %s" % path)
    value = values[path]
    require(type(value) is expected_type, "zstd material has wrong type: %s" % path)
    return value


def load_identity(component, component_sha256, repository):
    reader = component_reader()
    try:
        document = reader["load_component"](
            component, COMPONENT_NAME, "build", component_sha256
        )
    except reader["ComponentError"] as error:
        raise PreparationError("invalid zstd source component: %s" % error) from error
    require(document["dependencies"] == [], "zstd source component has dependencies")
    values = material_values(document)
    fields = {
        "/python/zstd/version": str,
        "/python/zstd/source/status": str,
        "/python/zstd/source/url": str,
        "/python/zstd/source/size": int,
        "/python/zstd/source/sha256": str,
        "/python/zstd/source/signature/url": str,
        "/python/zstd/source/signature/size": int,
        "/python/zstd/source/signature/sha256": str,
        "/python/zstd/source/signature/evidence": str,
        "/python/zstd/source/signature/key/file": str,
        "/python/zstd/source/signature/key/sha256": str,
        "/python/zstd/source/signature/key/fingerprint": str,
        "/python/zstd/source/git/repository": str,
        "/python/zstd/source/git/tag": str,
        "/python/zstd/source/git/tag_object": str,
        "/python/zstd/source/git/tag_evidence": str,
        "/python/zstd/source/git/commit": str,
        "/python/zstd/source/git/commit_evidence": str,
        "/python/zstd/license/expression": str,
        "/python/zstd/license/license_file": str,
        "/python/zstd/license/license_sha256": str,
        "/python/zstd/license/copying_file": str,
        "/python/zstd/license/copying_sha256": str,
    }
    require(set(values) == set(fields), "zstd source component material set differs")
    result = {
        path: required_material(values, path, value_type)
        for path, value_type in fields.items()
    }
    version = result["/python/zstd/version"]
    source_url = result["/python/zstd/source/url"]
    require(VERSION_RE.match(version), "invalid zstd version")
    require(result["/python/zstd/source/status"] == "locked", "zstd source is not locked")
    require(source_url == "https://github.com/facebook/zstd/releases/download/v%s/zstd-%s.tar.gz" % (version, version), "zstd source URL differs")
    require(result["/python/zstd/source/signature/url"] == source_url + ".sig", "zstd signature URL differs")
    for path in (
        "/python/zstd/source/sha256",
        "/python/zstd/source/signature/sha256",
        "/python/zstd/source/signature/key/sha256",
        "/python/zstd/license/license_sha256",
        "/python/zstd/license/copying_sha256",
    ):
        require(SHA256_RE.match(result[path]), "invalid SHA256 material: %s" % path)
    for path in ("/python/zstd/source/size", "/python/zstd/source/signature/size"):
        require(type(result[path]) is int and result[path] > 0, "invalid size material: %s" % path)
    require(re.fullmatch(r"[0-9a-f]{40}", result["/python/zstd/source/signature/key/fingerprint"]), "invalid zstd signing fingerprint")
    require(result["/python/zstd/source/git/repository"] == "https://github.com/facebook/zstd.git", "zstd git repository differs")
    require(result["/python/zstd/source/git/tag"] == "v" + version, "zstd git tag differs")
    require(GIT_OID_RE.match(result["/python/zstd/source/git/tag_object"]), "invalid zstd tag object")
    require(GIT_OID_RE.match(result["/python/zstd/source/git/commit"]), "invalid zstd commit")
    require(result["/python/zstd/license/expression"] == "BSD-3-Clause", "zstd license expression differs")
    require(result["/python/zstd/license/license_file"] == "LICENSE", "zstd license filename differs")
    require(result["/python/zstd/license/copying_file"] == "COPYING", "zstd COPYING filename differs")
    return result


def verify_git_evidence(identity, repository):
    tag = evidence_bytes(repository, identity["/python/zstd/source/git/tag_evidence"], "zstd tag evidence")
    commit = evidence_bytes(repository, identity["/python/zstd/source/git/commit_evidence"], "zstd commit evidence")
    require(git_object_sha1("tag", tag) == identity["/python/zstd/source/git/tag_object"], "zstd tag evidence digest differs")
    require(git_object_sha1("commit", commit) == identity["/python/zstd/source/git/commit"], "zstd commit evidence digest differs")
    require(tag.startswith(("object %s\n" % identity["/python/zstd/source/git/commit"]).encode("ascii")), "zstd tag does not reference locked commit")
    require(("tag %s\n" % identity["/python/zstd/source/git/tag"]).encode("ascii") in tag, "zstd tag evidence name differs")


def verify_signature(archive, signature, identity, repository):
    source_sha, source_size = sha256_file(archive)
    signature_sha, signature_size = sha256_file(signature)
    require((source_sha, source_size) == (identity["/python/zstd/source/sha256"], identity["/python/zstd/source/size"]), "zstd source archive identity differs")
    require((signature_sha, signature_size) == (identity["/python/zstd/source/signature/sha256"], identity["/python/zstd/source/signature/size"]), "zstd detached signature identity differs")
    require(evidence_bytes(repository, identity["/python/zstd/source/signature/evidence"], "zstd signature evidence") == signature.read_bytes(), "zstd signature evidence differs")
    key = safe_repository_file(repository, identity["/python/zstd/source/signature/key/file"], "zstd signing key")
    require(sha256_file(key)[0] == identity["/python/zstd/source/signature/key/sha256"], "zstd signing key digest differs")
    with tempfile.TemporaryDirectory(prefix="crossforge-zstd-gpg-") as temporary:
        home = Path(temporary)
        os.chmod(str(home), 0o700)
        imported = subprocess.run(["gpg", "--batch", "--homedir", str(home), "--import", str(key)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        shown = subprocess.run(["gpg", "--batch", "--homedir", str(home), "--with-colons", "--fingerprint"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        fingerprints = [line.split(":")[9] for line in shown.stdout.splitlines() if line.startswith("fpr:")]
        expected = identity["/python/zstd/source/signature/key/fingerprint"]
        require([item.lower() for item in fingerprints] == [expected], "cannot import zstd signing key or fingerprint differs: %s" % imported.stderr.strip())
        verified = subprocess.run(["gpg", "--batch", "--no-auto-key-retrieve", "--homedir", str(home), "--status-fd", "1", "--verify", str(signature), str(archive)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        valid = [line.split()[2] for line in verified.stdout.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
        require(verified.returncode == 0 and [item.lower() for item in valid] == [expected], "zstd detached signature is not valid")


def safe_members(archive, version):
    root = "zstd-%s" % version
    selected = []
    seen = set()
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        require(not path.is_absolute() and path.parts and path.parts[0] == root and all(part not in ("", ".", "..") for part in path.parts), "unsafe zstd archive member: %s" % member.name)
        relative = PurePosixPath(*path.parts[1:])
        wanted = relative in (PurePosixPath("LICENSE"), PurePosixPath("COPYING")) or (relative.parts and relative.parts[0] == "lib")
        if not wanted:
            continue
        key = str(relative)
        require(key not in seen, "duplicate zstd archive member: %s" % key)
        seen.add(key)
        require(member.isdir() or member.isfile(), "unsupported selected zstd member: %s" % member.name)
        selected.append((member, relative))
    require("LICENSE" in seen and "COPYING" in seen and "lib/zstd.h" in seen and "lib/Makefile" in seen, "zstd archive selected subset is incomplete")
    return selected


def extract_source(archive_path, destination, version):
    require(not destination.exists() and not destination.is_symlink(), "zstd destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".%s." % destination.name, dir=str(destination.parent)))
    try:
        with tarfile.open(str(archive_path), "r:gz") as archive:
            for member, relative in safe_members(archive, version):
                output = temporary.joinpath(*relative.parts)
                if member.isdir():
                    output.mkdir(parents=True, exist_ok=True)
                    os.chmod(str(output), 0o755)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                require(source is not None, "cannot read zstd archive member")
                with source, output.open("xb") as stream:
                    shutil.copyfileobj(source, stream, 1024 * 1024)
                os.chmod(str(output), 0o644)
        temporary.rename(destination)
    except BaseException:
        shutil.rmtree(str(temporary), ignore_errors=True)
        raise


def tree_manifest(destination):
    records = []
    for path in sorted(item for item in destination.rglob("*") if item.is_file()):
        digest, size = sha256_file(path)
        records.append({"path": path.relative_to(destination).as_posix(), "sha256": digest, "size": size})
    return records


def prepare(component, component_sha256, archive, signature, destination, manifest, repository):
    identity = load_identity(component, component_sha256, repository)
    verify_git_evidence(identity, repository)
    verify_signature(archive, signature, identity, repository)
    version = identity["/python/zstd/version"]
    require(not manifest.exists() and not manifest.is_symlink(), "zstd manifest already exists")
    extract_source(archive, destination, version)
    temporary = manifest.with_name(".%s.tmp" % manifest.name)
    try:
        license_sha = sha256_file(destination / "LICENSE")[0]
        copying_sha = sha256_file(destination / "COPYING")[0]
        require(license_sha == identity["/python/zstd/license/license_sha256"], "zstd LICENSE hash differs")
        require(copying_sha == identity["/python/zstd/license/copying_sha256"], "zstd COPYING hash differs")
        result = {
            "schema_version": 1,
            "kind": "crossforge-zstd-source",
            "component": {"name": COMPONENT_NAME, "canonical_sha256": component_sha256},
            "version": version,
            "source": {"url": identity["/python/zstd/source/url"], "size": identity["/python/zstd/source/size"], "sha256": identity["/python/zstd/source/sha256"]},
            "signature": {"sha256": identity["/python/zstd/source/signature/sha256"], "fingerprint": identity["/python/zstd/source/signature/key/fingerprint"], "key_sha256": identity["/python/zstd/source/signature/key/sha256"]},
            "git": {"repository": identity["/python/zstd/source/git/repository"], "tag": identity["/python/zstd/source/git/tag"], "tag_object": identity["/python/zstd/source/git/tag_object"], "commit": identity["/python/zstd/source/git/commit"]},
            "license": {"expression": identity["/python/zstd/license/expression"], "file": "LICENSE", "sha256": license_sha, "copying_file": "COPYING", "copying_sha256": copying_sha},
            "files": tree_manifest(destination),
        }
        manifest.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(str(temporary), str(manifest))
        return result
    except BaseException:
        shutil.rmtree(str(destination), ignore_errors=True)
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--component", type=Path, required=True)
    parser.add_argument("--component-sha256", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--repository", type=Path, default=REPOSITORY)
    arguments = parser.parse_args()
    try:
        result = prepare(arguments.component, arguments.component_sha256, arguments.archive, arguments.signature, arguments.destination, arguments.manifest, arguments.repository)
    except (OSError, PreparationError, tarfile.TarError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print("prepared: zstd %s" % result["version"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
