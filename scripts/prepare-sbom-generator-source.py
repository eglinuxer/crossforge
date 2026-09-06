#!/usr/bin/env python3
"""Authenticate and inspect the locked BuildKit Syft scanner source."""

import argparse
import hashlib
import json
import os
import runpy
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


REPOSITORY = Path(__file__).resolve().parents[1]
COMPONENT = runpy.run_path(str(REPOSITORY / "scripts/release_component.py"))
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ComponentError = COMPONENT["ComponentError"]
ValidationError = STRICT["ValidationError"]
EXPECTED_COMPONENT = "sources/sbom-generator"
EXPECTED_SCOPE = "supply"
MAX_MEMBER_SIZE = 64 * 1024 * 1024
MAX_ARCHIVE_SIZE = 64 * 1024 * 1024


class SourceError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise SourceError(message)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha512_file(path):
    digest = hashlib.sha512()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def regular_file(path, label, maximum=MAX_ARCHIVE_SIZE):
    path = Path(path)
    information = path.lstat()
    require(stat.S_ISREG(information.st_mode), "%s is not regular" % label)
    require(not path.is_symlink(), "%s is a symlink" % label)
    require(0 < information.st_size <= maximum, "%s size is invalid" % label)
    return path, information.st_size


def material(component, digest, pointer, expected_type):
    try:
        return COMPONENT["material_value"](
            component,
            EXPECTED_COMPONENT,
            EXPECTED_SCOPE,
            digest,
            pointer,
            expected_type,
        )
    except ComponentError as error:
        raise SourceError(str(error)) from error


def source_policy(component, digest):
    base = "/sbom/generator"
    source = base + "/source"
    return {
        "version": material(component, digest, base + "/version", "string"),
        "source": {
            field: material(component, digest, source + "/" + field, kind)
            for field, kind in (
                ("status", "string"),
                ("tag", "string"),
                ("tag_object", "string"),
                ("commit", "string"),
                ("url", "string"),
                ("sha256", "string"),
                ("sha512", "string"),
                ("size", "integer"),
                ("archive_root", "string"),
                ("member_count", "integer"),
            )
        },
        "license": {
            field: material(
                component, digest, source + "/license/" + field, kind
            )
            for field, kind in (
                ("expression", "string"),
                ("file", "string"),
                ("sha256", "string"),
            )
        },
        "tag_evidence": {
            field: material(
                component, digest, source + "/tag_evidence/" + field, kind
            )
            for field, kind in (
                ("url", "string"),
                ("sha256", "string"),
                ("size", "integer"),
            )
        },
        "key": {
            field: material(component, digest, source + "/key/" + field, kind)
            for field, kind in (
                ("file", "string"),
                ("source_url", "string"),
                ("source_sha256", "string"),
                ("derivation", "string"),
                ("sha256", "string"),
                ("fingerprint", "string"),
            )
        },
    }


def validate_archive(archive, policy):
    archive, size = regular_file(archive, "SBOM generator source")
    source = policy["source"]
    require(source["status"] == "locked", "SBOM generator source is not locked")
    require(size == source["size"], "SBOM generator source size differs")
    require(sha256_file(archive) == source["sha256"], "source SHA256 differs")
    require(sha512_file(archive) == source["sha512"], "source SHA512 differs")
    members = 0
    license_digest = None
    with tarfile.open(str(archive), "r:gz") as stream:
        for record in stream:
            members += 1
            path = PurePosixPath(record.name)
            require(
                not path.is_absolute()
                and path.parts
                and path.parts[0] == source["archive_root"]
                and ".." not in path.parts,
                "unsafe SBOM generator archive path",
            )
            require(
                record.isdir() or record.isreg() or record.issym(),
                "unsupported SBOM generator archive member",
            )
            require(
                0 <= record.size <= MAX_MEMBER_SIZE,
                "SBOM generator archive member is too large",
            )
            if record.name == "%s/%s" % (
                source["archive_root"], policy["license"]["file"]
            ):
                require(record.isreg(), "SBOM generator license is not regular")
                payload = stream.extractfile(record)
                require(payload is not None, "cannot read SBOM generator license")
                license_digest = hashlib.sha256(payload.read()).hexdigest()
    require(members == source["member_count"], "source member count differs")
    require(
        license_digest == policy["license"]["sha256"],
        "SBOM generator license differs",
    )


def validate_key(key_path, policy, gpg):
    key_path, _size = regular_file(key_path, "SBOM generator signing key")
    require(sha256_file(key_path) == policy["key"]["sha256"], "key digest differs")
    process = subprocess.run(
        [gpg, "--batch", "--with-colons", "--show-keys", str(key_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        universal_newlines=True,
    )
    require(process.returncode == 0, "cannot inspect SBOM generator signing key")
    fingerprints = [
        line.split(":")[9].lower()
        for line in process.stdout.splitlines()
        if line.startswith("fpr:")
    ]
    require(
        fingerprints and fingerprints[0] == policy["key"]["fingerprint"],
        "SBOM generator signing key fingerprint differs",
    )


def validate_tag(tag_path, policy, key_path, gpg):
    tag_path, size = regular_file(tag_path, "SBOM generator tag evidence")
    evidence = policy["tag_evidence"]
    source = policy["source"]
    require(size == evidence["size"], "tag evidence size differs")
    require(sha256_file(tag_path) == evidence["sha256"], "tag evidence digest differs")
    document = STRICT["load_json"](tag_path)
    verification = document.get("verification")
    require(
        document.get("sha") == source["tag_object"]
        and document.get("tag") == source["tag"]
        and document.get("object", {}).get("type") == "commit"
        and document.get("object", {}).get("sha") == source["commit"],
        "SBOM generator tag identity differs",
    )
    require(
        isinstance(verification, dict)
        and verification.get("verified") is True
        and verification.get("reason") == "valid"
        and isinstance(verification.get("signature"), str)
        and isinstance(verification.get("payload"), str),
        "GitHub tag verification differs",
    )
    expected_payload_prefix = (
        "object %s\ntype commit\ntag %s\n" % (source["commit"], source["tag"])
    )
    require(
        verification["payload"].startswith(expected_payload_prefix)
        and verification["payload"].endswith("\n\n%s\n" % source["tag"]),
        "signed tag payload differs",
    )
    with tempfile.TemporaryDirectory(prefix="crossforge-sbom-gpg-") as temporary:
        root = Path(temporary)
        os.chmod(str(root), 0o700)
        signature = root / "tag.asc"
        payload = root / "tag.payload"
        signature.write_text(verification["signature"], encoding="utf-8")
        payload.write_text(verification["payload"], encoding="utf-8")
        environment = dict(os.environ)
        environment["GNUPGHOME"] = str(root / "home")
        Path(environment["GNUPGHOME"]).mkdir(mode=0o700)
        imported = subprocess.run(
            [gpg, "--batch", "--import", str(key_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            universal_newlines=True,
            env=environment,
        )
        require(imported.returncode == 0, "cannot import SBOM generator key")
        verified = subprocess.run(
            [
                gpg,
                "--batch",
                "--status-fd",
                "1",
                "--verify",
                str(signature),
                str(payload),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            universal_newlines=True,
            env=environment,
        )
        expected = policy["key"]["fingerprint"].upper()
        require(verified.returncode == 0, "SBOM generator tag signature failed")
        require(
            any(
                line.startswith("[GNUPG:] VALIDSIG " + expected + " ")
                for line in verified.stdout.splitlines()
            ),
            "SBOM generator tag signer differs",
        )


def prepare(arguments):
    try:
        component = COMPONENT["load_component"](
            arguments.component,
            EXPECTED_COMPONENT,
            EXPECTED_SCOPE,
            arguments.component_sha256,
        )
    except ComponentError as error:
        raise SourceError(str(error)) from error
    policy = source_policy(component, arguments.component_sha256)
    require(policy["version"] == "1.12.0", "SBOM generator version differs")
    validate_archive(arguments.archive, policy)
    validate_key(arguments.key, policy, arguments.gpg)
    validate_tag(arguments.tag_evidence, policy, arguments.key, arguments.gpg)
    destination = Path(arguments.destination)
    require(
        not destination.exists() and not destination.is_symlink(),
        "SBOM generator output already exists",
    )
    source_root = destination / "source"
    materials = destination / "materials"
    source_root.mkdir(parents=True)
    materials.mkdir()
    archive_name = "buildkit-syft-scanner-%s.tar.gz" % policy["version"]
    tag_name = "buildkit-syft-scanner-%s.tag.json" % policy["source"]["tag"]
    shutil.copyfile(str(arguments.archive), str(source_root / archive_name))
    shutil.copyfile(str(arguments.tag_evidence), str(materials / tag_name))
    shutil.copyfile(str(arguments.key), str(materials / Path(arguments.key).name))
    for path in (source_root / archive_name, materials / tag_name, materials / Path(arguments.key).name):
        os.chmod(str(path), 0o644)
    manifest = {
        "schema_version": 1,
        "kind": "crossforge-sbom-generator-source",
        "component": {
            "name": EXPECTED_COMPONENT,
            "canonical_sha256": arguments.component_sha256,
        },
        "version": policy["version"],
        "source": {
            "tag": policy["source"]["tag"],
            "tag_object": policy["source"]["tag_object"],
            "commit": policy["source"]["commit"],
            "sha256": policy["source"]["sha256"],
            "sha512": policy["source"]["sha512"],
            "size": policy["source"]["size"],
            "member_count": policy["source"]["member_count"],
        },
        "tag_evidence": {
            "sha256": policy["tag_evidence"]["sha256"],
            "size": policy["tag_evidence"]["size"],
            "github_verified": True,
            "offline_gpg_verified": True,
            "signer_fingerprint": policy["key"]["fingerprint"],
        },
        "license": policy["license"],
    }
    (destination / "source.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("prepared SBOM generator source: %s" % destination)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--component", type=Path, required=True)
    parser.add_argument("--component-sha256", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--tag-evidence", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--gpg", default="gpg")
    parser.add_argument("--destination", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        prepare(arguments)
        return 0
    except (
        ComponentError,
        KeyError,
        OSError,
        SourceError,
        subprocess.SubprocessError,
        tarfile.TarError,
        TypeError,
        ValidationError,
    ) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
