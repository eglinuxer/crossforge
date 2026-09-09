#!/usr/bin/env python3
"""Create and validate a deterministic durable release-evidence archive."""

import argparse
import hashlib
import io
import json
import os
import runpy
import shutil
import stat
import sys
import tarfile
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCHEMA_ID = (
    "https://crossforge.dev/schemas/"
    "release-evidence-bundle-manifest.schema.json"
)
MANIFEST_NAME = "MANIFEST.json"
MAX_FILE_SIZE = 256 * 1024 * 1024
MAX_ARCHIVE_SIZE = 512 * 1024 * 1024
PAYLOAD_ARGUMENTS = (
    ("candidate-signature.json", "candidate_signature"),
    ("candidate.json", "candidate"),
    ("native-aarch64-probes.tar", "native_probe_bundle"),
    ("native-aarch64.json", "native_report"),
    ("release-promotion.json", "promotion"),
    ("release.json", "release"),
    ("sdk-attestations.json", "sdk_attestations"),
    ("sbom-generator-image.json", "sbom_generator_image"),
    ("sigstore-trusted-root.json", "trusted_root"),
    ("sigstore-verification.json", "sigstore_report"),
    ("source-attestations.json", "source_attestations"),
    ("source-binding.json", "source_binding"),
    ("source-bundle-signature.json", "source_signature"),
    ("source-bundle.json", "source_bundle_identity"),
)
PAYLOAD_NAMES = tuple(name for name, _attribute in PAYLOAD_ARGUMENTS)

STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
CANDIDATE = runpy.run_path(str(REPOSITORY / "scripts/candidate_manifest.py"))
SOURCE = runpy.run_path(str(REPOSITORY / "scripts/source_binding.py"))
PROMOTION = runpy.run_path(str(REPOSITORY / "scripts/release_promotion.py"))
SIGSTORE = runpy.run_path(str(REPOSITORY / "scripts/validate-sigstore-report.py"))
IMAGE_ATTESTATIONS = runpy.run_path(
    str(REPOSITORY / "scripts/image_attestations.py")
)
if str(REPOSITORY / "scripts") not in sys.path:
    sys.path.insert(0, str(REPOSITORY / "scripts"))
NATIVE = runpy.run_path(str(REPOSITORY / "scripts/native-aarch64-release.py"))
ValidationError = STRICT["ValidationError"]
CandidateError = CANDIDATE["CandidateError"]
PromotionError = PROMOTION["PromotionError"]


class ReleaseEvidenceError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseEvidenceError(message)


def canonical_bytes(document):
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(document):
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def regular_file(path, label, maximum=MAX_FILE_SIZE):
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ReleaseEvidenceError("cannot inspect %s: %s" % (label, error))
    require(stat.S_ISREG(metadata.st_mode), "%s is not a regular file" % label)
    require(not path.is_symlink(), "%s is a symlink" % label)
    require(0 < metadata.st_size <= maximum, "%s size is invalid" % label)
    return path, metadata.st_size


def load_release(path, schema_path):
    return CANDIDATE["load_release"](path, schema_path)


def load_schema(path):
    schema = STRICT["load_json"](path)
    require(isinstance(schema, dict), "release evidence schema must be an object")
    STRICT["validate_schema_subset"](schema)
    require(schema.get("$id") == SCHEMA_ID, "release evidence schema differs")
    return schema


def payload_identities(paths):
    identities = []
    for name in PAYLOAD_NAMES:
        path, size = regular_file(paths[name], name)
        identities.append(
            {"path": name, "sha256": sha256_file(path), "size": size}
        )
    return identities


def validate_inputs(paths, release, schema):
    bundled_release = STRICT["load_json"](paths["release.json"])
    require(bundled_release == release, "bundled release differs from selected release")
    candidate = STRICT["load_json"](paths["candidate.json"])
    candidate_schema = CANDIDATE["load_candidate_schema"](
        REPOSITORY / "config/schemas/candidate.schema.json"
    )
    candidate_sha256 = CANDIDATE["validate_candidate"](
        candidate, release, candidate_schema
    )
    source_binding = STRICT["load_json"](paths["source-binding.json"])
    source_schema = STRICT["load_json"](
        REPOSITORY / "config/schemas/source-binding.schema.json"
    )
    SOURCE["validate_binding"](
        source_binding,
        release,
        source_schema,
        expected_source_commit=candidate["source_commit"],
    )
    expected_source = {
        "repository": source_binding["repository"],
        "digest": source_binding["digest"],
        "platform": source_binding["platform"],
        "platform_manifest_digest": source_binding["platform_manifest_digest"],
        "archive": source_binding["archive"],
    }
    require(candidate["source_bundle"] == expected_source, "source binding differs")
    source_identity = CANDIDATE["load_source_bundle_identity"](
        paths["source-bundle.json"],
        REPOSITORY / "config/schemas/source-bundle-identity.schema.json",
        release,
        candidate["source_commit"],
    )
    require(
        source_identity["archive"] == candidate["source_bundle"]["archive"],
        "source bundle file identity differs",
    )
    promotion = STRICT["load_json"](paths["release-promotion.json"])
    promotion_schema = PROMOTION["load_schema"](
        REPOSITORY / "config/schemas/release-promotion.schema.json"
    )
    PROMOTION["validate_document"](promotion, release, promotion_schema)
    require(
        promotion["candidate_manifest_sha256"] == candidate_sha256,
        "promotion candidate manifest digest differs",
    )
    for name, selected in (
        ("candidate", candidate),
        ("source_bundle", candidate["source_bundle"]),
    ):
        promoted = promotion[name]
        require(
            promoted["repository"] == selected["repository"]
            and promoted["digest"] == selected["digest"]
            and promoted["platform_manifest_digest"]
            == selected["platform_manifest_digest"],
            "%s promotion identity differs" % name,
        )
    sigstore_report = STRICT["load_json"](paths["sigstore-verification.json"])
    sigstore_schema = STRICT["load_json"](
        REPOSITORY / "config/schemas/sigstore-verification.schema.json"
    )
    SIGSTORE["validate_report_document"](
        sigstore_report, release, sigstore_schema
    )
    attestation_schema = REPOSITORY / "config/schemas/image-attestations.schema.json"
    attestation_expectations = {
        "sdk-attestations.json": (
            "sdk-candidate",
            candidate["repository"],
            candidate["digest"],
            candidate["platform_manifest_digest"],
        ),
        "source-attestations.json": (
            "source-bundle",
            candidate["source_bundle"]["repository"],
            candidate["source_bundle"]["digest"],
            candidate["source_bundle"]["platform_manifest_digest"],
        ),
    }
    for name, expected in attestation_expectations.items():
        report = STRICT["load_json"](paths[name])
        IMAGE_ATTESTATIONS["validate_schema"](report, attestation_schema)
        require(
            (
                report["image_kind"],
                report["repository"],
                report["index_digest"],
                report["platform_manifest_digest"],
            )
            == expected
            and report["source_commit"] == candidate["source_commit"],
            "%s image identity differs" % name,
        )
    generator_report = STRICT["load_json"](paths["sbom-generator-image.json"])
    generator_schema = STRICT["load_json"](
        REPOSITORY / "config/schemas/sbom-generator-image.schema.json"
    )
    STRICT["validate_schema_subset"](generator_schema)
    STRICT["validate"](
        generator_report, generator_schema, generator_schema, "$"
    )
    require(
        generator_report["index_digest"] == release["sbom"]["generator"]["digest"]
        and generator_report["manifest_digest"]
        == release["sbom"]["generator"]["manifest_digest"],
        "SBOM generator image report differs",
    )
    trusted_root, _size = regular_file(
        paths["sigstore-trusted-root.json"], "sigstore-trusted-root.json"
    )
    require(
        sha256_file(trusted_root) == release["sigstore"]["trust"]["trusted_root_sha256"],
        "Sigstore trusted root digest differs",
    )
    signature_identities = {
        "candidate-signature.json": (
            candidate["repository"],
            candidate["digest"],
        ),
        "source-bundle-signature.json": (
            candidate["source_bundle"]["repository"],
            candidate["source_bundle"]["digest"],
        ),
    }
    for name, (repository, digest) in signature_identities.items():
        signatures = STRICT["load_json"](paths[name])
        require(
            isinstance(signatures, list) and len(signatures) >= 1,
            "%s contains no signatures" % name,
        )
        require(
            all(
                isinstance(record, dict)
                and record.get("critical", {}).get("identity", {}).get(
                    "docker-reference"
                )
                == repository
                and record.get("critical", {}).get("image", {}).get(
                    "docker-manifest-digest"
                )
                == digest
                for record in signatures
            ),
            "%s identity differs" % name,
        )
    native_report = STRICT["load_json"](paths["native-aarch64.json"])
    native_arguments = NATIVE["parser"]().parse_args(
        [
            "validate",
            "--candidate",
            str(paths["candidate.json"]),
            "--bundle",
            str(paths["native-aarch64-probes.tar"]),
            "--expected-bundle-sha256",
            native_report["bundle_sha256"],
            "--report",
            str(paths["native-aarch64.json"]),
            "--release",
            str(paths["release.json"]),
        ]
    )
    NATIVE["validate_report"](native_arguments)
    manifest = {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-release-evidence-bundle",
        "status": "qualified-signed-promoted",
        "release": {
            "version": release["product"]["version"],
            "canonical_sha256": CANDIDATE["canonical_sha256"](release),
        },
        "candidate": {
            "source_commit": candidate["source_commit"],
            "repository": candidate["repository"],
            "digest": candidate["digest"],
            "platform_manifest_digest": candidate["platform_manifest_digest"],
            "canonical_sha256": candidate_sha256,
        },
        "source_bundle": {
            "repository": candidate["source_bundle"]["repository"],
            "digest": candidate["source_bundle"]["digest"],
            "platform_manifest_digest": candidate["source_bundle"]
            ["platform_manifest_digest"],
        },
        "promotion_sha256": canonical_sha256(promotion),
        "files": payload_identities(paths),
    }
    validate_manifest(manifest, release, candidate, promotion, schema)
    return manifest


def validate_manifest(manifest, release, candidate, promotion, schema):
    try:
        STRICT["validate"](manifest, schema, schema, "$")
    except ValidationError as error:
        raise ReleaseEvidenceError(
            "release evidence schema validation failed: %s" % error
        )
    require(
        [record["path"] for record in manifest["files"]] == list(PAYLOAD_NAMES),
        "release evidence file order or set differs",
    )
    require(
        manifest["release"]
        == {
            "version": release["product"]["version"],
            "canonical_sha256": CANDIDATE["canonical_sha256"](release),
        },
        "release evidence release identity differs",
    )
    require(
        manifest["candidate"]
        == {
            "source_commit": candidate["source_commit"],
            "repository": candidate["repository"],
            "digest": candidate["digest"],
            "platform_manifest_digest": candidate["platform_manifest_digest"],
            "canonical_sha256": CANDIDATE["canonical_sha256"](candidate),
        },
        "release evidence candidate identity differs",
    )
    require(
        manifest["source_bundle"]
        == {
            "repository": candidate["source_bundle"]["repository"],
            "digest": candidate["source_bundle"]["digest"],
            "platform_manifest_digest": candidate["source_bundle"]
            ["platform_manifest_digest"],
        },
        "release evidence source identity differs",
    )
    require(
        manifest["promotion_sha256"] == canonical_sha256(promotion),
        "release evidence promotion identity differs",
    )
    return manifest


def tar_record(name, size):
    record = tarfile.TarInfo(name)
    record.size = size
    record.mode = 0o644
    record.uid = 0
    record.gid = 0
    record.uname = ""
    record.gname = ""
    record.mtime = 0
    return record


def build_archive(path, paths, manifest):
    manifest_payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % Path(path).name, suffix=".tmp", dir=str(Path(path).parent)
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(str(temporary), "w", format=tarfile.USTAR_FORMAT) as archive:
            archive.addfile(
                tar_record(MANIFEST_NAME, len(manifest_payload)),
                io.BytesIO(manifest_payload),
            )
            for name in PAYLOAD_NAMES:
                source, size = regular_file(paths[name], name)
                with source.open("rb") as stream:
                    archive.addfile(tar_record(name, size), stream)
        require(
            temporary.stat().st_size <= MAX_ARCHIVE_SIZE,
            "release evidence archive is too large",
        )
        output = Path(path)
        if output.exists():
            regular_file(output, "existing release evidence archive", MAX_ARCHIVE_SIZE)
            require(
                output.stat().st_size == temporary.stat().st_size
                and sha256_file(output) == sha256_file(temporary),
                "refusing to replace a different release evidence archive",
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


def write_sidecar(path, archive):
    payload = "%s  %s\n" % (sha256_file(archive), Path(archive).name)
    path = Path(path)
    if path.exists():
        regular_file(path, "existing release evidence checksum")
        require(
            path.read_text(encoding="utf-8") == payload,
            "refusing to replace a different release evidence checksum",
        )
        return False
    require(not path.is_symlink(), "release evidence checksum is a symlink")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
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


def extract_archive(path, destination):
    archive_path, _size = regular_file(
        path, "release evidence archive", MAX_ARCHIVE_SIZE
    )
    destination = Path(destination)
    extracted = {}
    with tarfile.open(str(archive_path), "r:") as archive:
        members = archive.getmembers()
        expected = [MANIFEST_NAME] + list(PAYLOAD_NAMES)
        require([member.name for member in members] == expected, "archive member set differs")
        for member in members:
            require(member.isreg(), "archive contains a non-regular member")
            require(member.mode == 0o644, "archive member mode differs")
            require(
                member.uid == 0
                and member.gid == 0
                and member.uname == ""
                and member.gname == ""
                and member.mtime == 0,
                "archive member metadata differs",
            )
            require(0 < member.size <= MAX_FILE_SIZE, "archive member size is invalid")
            stream = archive.extractfile(member)
            require(stream is not None, "cannot read archive member")
            output = destination / member.name
            with output.open("wb") as target:
                shutil.copyfileobj(stream, target, 1024 * 1024)
            os.chmod(str(output), 0o644)
            extracted[member.name] = output
    return extracted


def validate_archive(arguments, release, schema):
    with tempfile.TemporaryDirectory(prefix="crossforge-release-evidence-") as temporary:
        extracted = extract_archive(arguments.archive, temporary)
        paths = {name: extracted[name] for name in PAYLOAD_NAMES}
        manifest = STRICT["load_json"](extracted[MANIFEST_NAME])
        expected = validate_inputs(paths, release, schema)
        require(manifest == expected, "archive manifest content differs")
    if arguments.sha256 is not None:
        checksum, separator, filename = Path(arguments.sha256).read_text(
            encoding="utf-8"
        ).strip().partition("  ")
        require(separator == "  ", "release evidence checksum format differs")
        require(filename == Path(arguments.archive).name, "checksum filename differs")
        require(checksum == sha256_file(arguments.archive), "archive checksum differs")
    return manifest


def add_common(parser):
    parser.add_argument(
        "--release-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release.schema.json",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY
        / "config/schemas/release-evidence-bundle-manifest.schema.json",
    )


def parser():
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = result.add_subparsers(dest="command")
    create = commands.add_parser("create", allow_abbrev=False)
    add_common(create)
    for _name, attribute in PAYLOAD_ARGUMENTS:
        create.add_argument("--" + attribute.replace("_", "-"), type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--sha256-output", type=Path, required=True)
    validate = commands.add_parser("validate", allow_abbrev=False)
    add_common(validate)
    validate.add_argument("archive", type=Path)
    validate.add_argument("--sha256", type=Path)
    validate.add_argument(
        "--release", type=Path, default=REPOSITORY / "config/release.json"
    )
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        require(arguments.command in ("create", "validate"), "a command is required")
        release_path = arguments.release
        release = load_release(release_path, arguments.release_schema)
        schema = load_schema(arguments.schema)
        if arguments.command == "validate":
            manifest = validate_archive(arguments, release, schema)
            print(
                "valid release evidence archive: %s (candidate %s)"
                % (arguments.archive, manifest["candidate"]["digest"])
            )
            return 0
        paths = {
            name: Path(getattr(arguments, attribute))
            for name, attribute in PAYLOAD_ARGUMENTS
        }
        expected_name = "crossforge-v%s-release-evidence.tar" % release["product"][
            "version"
        ].replace("+", "_")
        require(arguments.output.name == expected_name, "release evidence filename differs")
        require(
            arguments.sha256_output.name == expected_name + ".sha256",
            "release evidence checksum filename differs",
        )
        manifest = validate_inputs(paths, release, schema)
        state = "wrote" if build_archive(arguments.output, paths, manifest) else "current"
        write_sidecar(arguments.sha256_output, arguments.output)
        print(
            "%s release evidence archive: %s (sha256:%s)"
            % (state, arguments.output, sha256_file(arguments.output))
        )
        return 0
    except (
        CandidateError,
        KeyError,
        OSError,
        PromotionError,
        ReleaseEvidenceError,
        tarfile.TarError,
        TypeError,
        ValidationError,
    ) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
