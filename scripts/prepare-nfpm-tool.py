#!/usr/bin/env python3
"""Authenticate and assemble the locked nFPM host tool offline."""

import argparse
import base64
import binascii
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
COMPONENT_READER = runpy.run_path(
    str(SCRIPT_DIRECTORY / "release_component.py")
)
ComponentError = COMPONENT_READER["ComponentError"]
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
SHA512_RE = re.compile(r"^[0-9a-f]{128}\Z")
GIT_RE = re.compile(r"^[0-9a-f]{40}\Z")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\Z")


class PreparationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise PreparationError(message)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def load_json(path):
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            result = json.load(stream, object_pairs_hook=reject_duplicate_keys)
    except (OSError, ValueError) as error:
        raise PreparationError("cannot load %s: %s" % (path, error)) from error
    require(isinstance(result, dict), "%s must contain an object" % path)
    return result


def file_digest(path, algorithm):
    digest = hashlib.new(algorithm)
    try:
        with Path(path).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise PreparationError("cannot hash %s: %s" % (path, error)) from error
    return digest.hexdigest()


def sha256_file(path):
    return file_digest(path, "sha256")


def verify_file(path, sha256, size, label, sha512=None):
    path = Path(path)
    require(
        path.is_file() and not path.is_symlink(),
        "%s must be a regular file" % label,
    )
    require(path.stat().st_size == size, "%s size differs" % label)
    require(sha256_file(path) == sha256, "%s SHA256 differs" % label)
    if sha512 is not None:
        require(file_digest(path, "sha512") == sha512, "%s SHA512 differs" % label)


def run(arguments, input_bytes=None):
    process = subprocess.run(
        [str(argument) for argument in arguments],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    require(
        process.returncode == 0,
        "command failed (%s):\n%s"
        % (
            " ".join(str(argument) for argument in arguments),
            (process.stdout + process.stderr).decode("utf-8", "replace"),
        ),
    )
    return process.stdout, process.stderr


def material_map(document):
    return {record["path"]: record["value"] for record in document["materials"]}


def expected_fields():
    fields = {
        "/nfpm/version": str,
        "/nfpm/source/status": str,
        "/nfpm/source/repository": str,
        "/nfpm/source/tag": str,
        "/nfpm/source/tag_object": str,
        "/nfpm/source/commit": str,
        "/nfpm/source/archive/url": str,
        "/nfpm/source/archive/sha256": str,
        "/nfpm/source/archive/sha512": str,
        "/nfpm/source/archive/size": int,
        "/nfpm/binary/status": str,
        "/nfpm/binary/url": str,
        "/nfpm/binary/sha256": str,
        "/nfpm/binary/sha512": str,
        "/nfpm/binary/size": int,
        "/nfpm/binary/extracted_sha256": str,
        "/nfpm/binary/extracted_size": int,
        "/nfpm/checksums/url": str,
        "/nfpm/checksums/sha256": str,
        "/nfpm/checksums/size": int,
        "/nfpm/sigstore/url": str,
        "/nfpm/sigstore/sha256": str,
        "/nfpm/sigstore/size": int,
        "/nfpm/sigstore/status": str,
        "/nfpm/sigstore/signed_asset_sha256": str,
        "/nfpm/sigstore/expected_identity": str,
        "/nfpm/sigstore/expected_issuer": str,
        "/nfpm/license/expression": str,
        "/nfpm/license/file": str,
        "/nfpm/license/sha256": str,
    }
    return fields


def load_identity(component_path, component_sha256):
    try:
        component = COMPONENT_READER["load_component"](
            component_path, "sources/nfpm", "build", component_sha256
        )
    except ComponentError as error:
        raise PreparationError("invalid nFPM source component: %s" % error) from error
    require(component["dependencies"] == [], "nFPM source must have no dependencies")
    values = material_map(component)
    fields = expected_fields()
    require(set(values) == set(fields), "nFPM source material set differs")
    for path, expected_type in fields.items():
        require(
            type(values[path]) is expected_type,
            "nFPM material has wrong type: %s" % path,
        )

    version = values["/nfpm/version"]
    tag = values["/nfpm/source/tag"]
    commit = values["/nfpm/source/commit"]
    binary_filename = "nfpm_%s_Linux_x86_64.tar.gz" % version
    release_root = "https://github.com/goreleaser/nfpm/releases/download/%s/" % tag
    require(
        VERSION_RE.match(version)
        and tag == "v" + version
        and values["/nfpm/source/status"] == "locked"
        and values["/nfpm/binary/status"] == "locked"
        and values["/nfpm/source/repository"]
        == "https://github.com/goreleaser/nfpm.git"
        and GIT_RE.match(values["/nfpm/source/tag_object"])
        and GIT_RE.match(commit),
        "nFPM version/source identity differs",
    )
    require(
        values["/nfpm/binary/url"] == release_root + binary_filename
        and values["/nfpm/checksums/url"] == release_root + "checksums.txt"
        and values["/nfpm/sigstore/url"]
        == release_root + "checksums.txt.sigstore.json"
        and values["/nfpm/source/archive/url"]
        == "https://github.com/goreleaser/nfpm/archive/refs/tags/%s.tar.gz"
        % tag,
        "nFPM artifact URLs differ",
    )
    for path in (
        "/nfpm/source/archive/sha256",
        "/nfpm/binary/sha256",
        "/nfpm/binary/extracted_sha256",
        "/nfpm/checksums/sha256",
        "/nfpm/sigstore/sha256",
        "/nfpm/sigstore/signed_asset_sha256",
        "/nfpm/license/sha256",
    ):
        require(SHA256_RE.match(values[path]), "invalid SHA256: %s" % path)
    for path in (
        "/nfpm/source/archive/sha512",
        "/nfpm/binary/sha512",
    ):
        require(SHA512_RE.match(values[path]), "invalid SHA512: %s" % path)
    for path in (
        "/nfpm/source/archive/size",
        "/nfpm/binary/size",
        "/nfpm/binary/extracted_size",
        "/nfpm/checksums/size",
        "/nfpm/sigstore/size",
    ):
        require(values[path] > 0, "invalid size: %s" % path)
    require(
        values["/nfpm/sigstore/status"] == "archived-unverified"
        and values["/nfpm/sigstore/signed_asset_sha256"]
        == values["/nfpm/checksums/sha256"]
        and values["/nfpm/sigstore/expected_identity"]
        == "https://github.com/goreleaser/nfpm/.github/workflows/release.yml@refs/tags/%s"
        % tag
        and values["/nfpm/sigstore/expected_issuer"]
        == "https://token.actions.githubusercontent.com"
        and values["/nfpm/license/expression"] == "MIT"
        and values["/nfpm/license/file"] == "licenses/nfpm/LICENSE.md",
        "nFPM verification/license policy differs",
    )
    return component, values


def safe_member_name(member, root):
    name = member.name
    path = PurePosixPath(name)
    require(
        not path.is_absolute()
        and path.parts
        and path.parts[0] == root
        and all(part not in ("", ".", "..") for part in path.parts),
        "nFPM archive contains an unsafe path: %s" % name,
    )


def read_binary_archive(archive_path, identity):
    expected_names = {
        "LICENSE.md",
        "README.md",
        "completions/nfpm.bash",
        "completions/nfpm.fish",
        "completions/nfpm.zsh",
        "manpages/nfpm.1.gz",
        "nfpm",
    }
    try:
        with tarfile.open(str(archive_path), mode="r:gz") as archive:
            members = archive.getmembers()
            require(
                {member.name for member in members} == expected_names,
                "nFPM binary archive inventory differs",
            )
            require(
                all(member.isfile() for member in members),
                "nFPM binary archive contains a non-file",
            )
            binary_stream = archive.extractfile("nfpm")
            license_stream = archive.extractfile("LICENSE.md")
            require(
                binary_stream is not None and license_stream is not None,
                "nFPM archive payload is missing",
            )
            binary = binary_stream.read()
            license_payload = license_stream.read()
    except (OSError, tarfile.TarError) as error:
        raise PreparationError("cannot read nFPM binary archive: %s" % error) from error
    require(
        len(binary) == identity["/nfpm/binary/extracted_size"]
        and hashlib.sha256(binary).hexdigest()
        == identity["/nfpm/binary/extracted_sha256"],
        "extracted nFPM binary identity differs",
    )
    return binary, license_payload


def verify_source_archive(archive_path, identity, selected_license):
    root = "nfpm-%s" % identity["/nfpm/version"]
    go_mod_name = root + "/go.mod"
    license_name = root + "/LICENSE.md"
    try:
        with tarfile.open(str(archive_path), mode="r:gz") as archive:
            members = archive.getmembers()
            require(members, "nFPM source archive is empty")
            for member in members:
                safe_member_name(member, root)
                require(
                    member.isfile() or member.isdir() or member.issym(),
                    "nFPM source archive contains a special file",
                )
            names = {member.name for member in members}
            require(
                go_mod_name in names and license_name in names,
                "nFPM source archive lacks go.mod or LICENSE.md",
            )
            go_mod_stream = archive.extractfile(go_mod_name)
            license_stream = archive.extractfile(license_name)
            require(
                go_mod_stream is not None and license_stream is not None,
                "nFPM source archive metadata is unreadable",
            )
            go_mod = go_mod_stream.read()
            source_license = license_stream.read()
    except (OSError, tarfile.TarError) as error:
        raise PreparationError("cannot read nFPM source archive: %s" % error) from error
    require(
        go_mod.startswith(b"module github.com/goreleaser/nfpm/v2\n")
        and source_license == selected_license,
        "nFPM source module or license differs",
    )
    return len(members)


def verify_checksums(checksums_path, identity):
    try:
        lines = Path(checksums_path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise PreparationError("cannot read nFPM checksums: %s" % error) from error
    records = {}
    for line in lines:
        match = re.match(r"^([0-9a-f]{64})  ([A-Za-z0-9._+-]+)\Z", line)
        require(match is not None, "invalid nFPM checksum line")
        digest, filename = match.groups()
        require(filename not in records, "duplicate nFPM checksum filename")
        records[filename] = digest
    filename = "nfpm_%s_Linux_x86_64.tar.gz" % identity["/nfpm/version"]
    require(
        records.get(filename) == identity["/nfpm/binary/sha256"],
        "nFPM archive is not bound by checksums.txt",
    )
    return len(records)


def decode_base64(value, label):
    require(isinstance(value, str) and value, "%s is missing" % label)
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as error:
        raise PreparationError("invalid %s: %s" % (label, error)) from error


def verify_sigstore(bundle_path, identity):
    bundle = load_json(bundle_path)
    require(
        set(bundle) == {"mediaType", "verificationMaterial", "messageSignature"}
        and bundle["mediaType"]
        == "application/vnd.dev.sigstore.bundle.v0.3+json",
        "nFPM Sigstore bundle envelope differs",
    )
    signature = bundle["messageSignature"]
    require(
        isinstance(signature, dict)
        and set(signature) == {"messageDigest", "signature"},
        "nFPM Sigstore message signature differs",
    )
    digest = signature["messageDigest"]
    require(
        isinstance(digest, dict)
        and digest.get("algorithm") == "SHA2_256"
        and decode_base64(digest.get("digest"), "Sigstore message digest").hex()
        == identity["/nfpm/checksums/sha256"],
        "nFPM Sigstore bundle does not bind checksums.txt",
    )
    material = bundle["verificationMaterial"]
    require(isinstance(material, dict), "nFPM Sigstore verification material differs")
    certificate = material.get("certificate")
    require(
        isinstance(certificate, dict) and set(certificate) == {"rawBytes"},
        "nFPM Sigstore certificate differs",
    )
    certificate_der = decode_base64(
        certificate["rawBytes"], "Sigstore certificate"
    )
    stdout, _stderr = run(
        ["openssl", "x509", "-inform", "DER", "-noout", "-text"],
        certificate_der,
    )
    text = stdout.decode("utf-8", "replace")
    for expected in (
        "URI:" + identity["/nfpm/sigstore/expected_identity"],
        identity["/nfpm/sigstore/expected_issuer"],
        identity["/nfpm/source/commit"],
        "refs/tags/" + identity["/nfpm/source/tag"],
    ):
        require(expected in text, "nFPM Sigstore certificate metadata differs")
    return {
        "status": identity["/nfpm/sigstore/status"],
        "certificate_sha256": hashlib.sha256(certificate_der).hexdigest(),
        "tlog_entries": len(material.get("tlogEntries", [])),
    }


def verify_binary(binary_path, identity):
    header = Path(binary_path).read_bytes()[:20]
    require(
        header.startswith(b"\x7fELF")
        and len(header) == 20
        and header[4] == 2
        and header[5] == 1
        and int.from_bytes(header[18:20], byteorder="little") == 62,
        "nFPM executable is not an x86_64 little-endian ELF64",
    )
    stdout, stderr = run([binary_path, "--version"])
    version_text = (stdout + stderr).decode("utf-8", "replace")
    require(
        re.search(
            r"^GitVersion:\s+%s\s*$" % re.escape(identity["/nfpm/version"]),
            version_text,
            re.MULTILINE,
        )
        and re.search(
            r"^GitCommit:\s+%s\s*$" % identity["/nfpm/source/commit"],
            version_text,
            re.MULTILINE,
        )
        and re.search(r"^Platform:\s+linux/amd64\s*$", version_text, re.MULTILINE),
        "nFPM executable build identity differs",
    )
    return hashlib.sha256(stdout + stderr).hexdigest()


def write_json(path, document):
    Path(path).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def prepare(
    component_path,
    component_sha256,
    binary_archive,
    source_archive,
    checksums,
    sigstore,
    license_path,
    output_root,
):
    component, identity = load_identity(component_path, component_sha256)
    verify_file(
        binary_archive,
        identity["/nfpm/binary/sha256"],
        identity["/nfpm/binary/size"],
        "nFPM binary archive",
        identity["/nfpm/binary/sha512"],
    )
    verify_file(
        source_archive,
        identity["/nfpm/source/archive/sha256"],
        identity["/nfpm/source/archive/size"],
        "nFPM source archive",
        identity["/nfpm/source/archive/sha512"],
    )
    verify_file(
        checksums,
        identity["/nfpm/checksums/sha256"],
        identity["/nfpm/checksums/size"],
        "nFPM checksums",
    )
    verify_file(
        sigstore,
        identity["/nfpm/sigstore/sha256"],
        identity["/nfpm/sigstore/size"],
        "nFPM Sigstore bundle",
    )
    selected_license = Path(license_path).read_bytes()
    require(
        hashlib.sha256(selected_license).hexdigest()
        == identity["/nfpm/license/sha256"],
        "selected nFPM license differs",
    )
    binary, binary_license = read_binary_archive(binary_archive, identity)
    require(binary_license == selected_license, "nFPM binary archive license differs")
    source_members = verify_source_archive(
        source_archive, identity, selected_license
    )
    checksum_records = verify_checksums(checksums, identity)
    sigstore_result = verify_sigstore(sigstore, identity)

    output_root = Path(output_root)
    require(
        not output_root.exists() and not output_root.is_symlink(),
        "nFPM output root already exists",
    )
    output_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".nfpm-tool.", dir=str(output_root.parent))
    )
    try:
        version = identity["/nfpm/version"]
        install_root = (
            temporary / "root/opt/crossforge/host-tools/nfpm" / version
        )
        installed_binary = install_root / "bin/nfpm"
        installed_license = install_root / "share/licenses/nfpm/LICENSE.md"
        installed_binary.parent.mkdir(parents=True)
        installed_license.parent.mkdir(parents=True)
        installed_binary.write_bytes(binary)
        installed_license.write_bytes(selected_license)
        os.chmod(str(installed_binary), 0o755)
        os.chmod(str(installed_license), 0o644)
        source_output = temporary / "source" / (
            "nfpm-%s.tar.gz" % version
        )
        source_output.parent.mkdir(parents=True)
        shutil.copyfile(str(source_archive), str(source_output))
        version_output_sha256 = verify_binary(installed_binary, identity)
        report = {
            "schema_version": 1,
            "kind": "crossforge-nfpm-source",
            "component": {
                "name": "sources/nfpm",
                "canonical_sha256": COMPONENT_READER["canonical_sha256"](
                    component
                ),
            },
            "version": version,
            "commit": identity["/nfpm/source/commit"],
            "binary": {
                "sha256": identity["/nfpm/binary/extracted_sha256"],
                "size": identity["/nfpm/binary/extracted_size"],
                "version_output_sha256": version_output_sha256,
            },
            "source": {
                "sha256": identity["/nfpm/source/archive/sha256"],
                "size": identity["/nfpm/source/archive/size"],
                "members": source_members,
            },
            "checksums": {
                "sha256": identity["/nfpm/checksums/sha256"],
                "records": checksum_records,
            },
            "sigstore": sigstore_result,
            "license": {
                "expression": identity["/nfpm/license/expression"],
                "sha256": identity["/nfpm/license/sha256"],
            },
        }
        write_json(temporary / "source.json", report)
        temporary.rename(output_root)
    except BaseException:
        shutil.rmtree(str(temporary), ignore_errors=True)
        raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--component", type=Path, required=True)
    parser.add_argument("--component-sha256", required=True)
    parser.add_argument("--binary-archive", type=Path, required=True)
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--checksums", type=Path, required=True)
    parser.add_argument("--sigstore", type=Path, required=True)
    parser.add_argument("--license", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    arguments = parser.parse_args()
    prepare(
        arguments.component,
        arguments.component_sha256,
        arguments.binary_archive,
        arguments.source_archive,
        arguments.checksums,
        arguments.sigstore,
        arguments.license,
        arguments.output_root,
    )
    print("prepared locked nFPM host tool: %s" % arguments.output_root)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, PreparationError, TypeError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
