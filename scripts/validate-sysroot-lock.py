#!/usr/bin/env python3
"""Validate a sysroot plan or fully resolved offline RPM lock."""

import argparse
import hashlib
import json
import runpy
import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT_JSON = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ValidationError = STRICT_JSON["ValidationError"]
load_json = STRICT_JSON["load_json"]
validate = STRICT_JSON["validate"]
validate_schema_subset = STRICT_JSON["validate_schema_subset"]

SCHEMAS = {
    "sysroot-plan": REPOSITORY / "config/schemas/sysroot-plan.schema.json",
    "sysroot-lock": REPOSITORY / "config/schemas/sysroot-lock.schema.json",
}
TARGET_TRIPLES = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
}
MINIMUM_ROOTS = {
    "glibc": "c-runtime",
    "glibc-devel": "c-runtime",
    "glibc-headers": "c-runtime",
    "glibc-minimal-langpack": "c-runtime",
    "kernel-headers": "c-runtime",
    "libgcc": "c-runtime",
    "libstdc++": "c-runtime",
    "bzip2-devel": "python",
    "libffi-devel": "python",
    "libuuid-devel": "python",
    "openssl-devel": "python",
    "sqlite-devel": "python",
    "xz-devel": "python",
    "zlib-devel": "python",
}
FORBIDDEN_ROOTS = {
    "binutils",
    "gcc",
    "glibc-static",
    "libstdc++-devel",
    "libstdc++-static",
}


def _reject_duplicates(values, label):
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValidationError("duplicate %s: %s" % (label, ", ".join(duplicates)))


def validate_semantics(document):
    identity = document["identity"]
    arch = identity["arch"]
    expected_triple = TARGET_TRIPLES[arch]
    if identity["target_triple"] != expected_triple:
        raise ValidationError(
            "$.identity.target_triple: expected %r for %s"
            % (expected_triple, arch)
        )

    solver = document.get("solver_policy", document.get("solver"))
    expected_arches = [arch, "noarch"]
    if solver["allowed_arches"] != expected_arches:
        raise ValidationError(
            "solver allowed_arches must be exactly %r" % expected_arches
        )

    repository_ids = [repository["id"] for repository in document["repositories"]]
    _reject_duplicates(repository_ids, "repository id")
    for repository in document["repositories"]:
        if "/%s/" % arch not in repository["baseurl"]:
            raise ValidationError(
                "repository %s baseurl does not identify arch %s"
                % (repository["id"], arch)
            )

    roots = document["roots"]
    root_keys = ["%s.%s" % (root["name"], root["arch"]) for root in roots]
    _reject_duplicates(root_keys, "root package")
    for root in roots:
        if root["arch"] != arch:
            raise ValidationError(
                "root %s has arch %s, expected %s"
                % (root["name"], root["arch"], arch)
            )
        expected_purpose = MINIMUM_ROOTS.get(root["name"])
        if expected_purpose is not None and root["purpose"] != expected_purpose:
            raise ValidationError(
                "root %s must have purpose %s" % (root["name"], expected_purpose)
            )
        if root["name"] in FORBIDDEN_ROOTS:
            raise ValidationError("forbidden sysroot root: %s" % root["name"])

    root_names = {root["name"] for root in roots}
    missing_roots = sorted(set(MINIMUM_ROOTS) - root_names)
    if missing_roots:
        raise ValidationError("missing minimum roots: %s" % ", ".join(missing_roots))

    if document["kind"] == "sysroot-plan":
        return

    repositories = {repository["id"]: repository for repository in document["repositories"]}
    for repository in repositories.values():
        metadata_types = [item["type"] for item in repository["metadata"]]
        _reject_duplicates(metadata_types, "metadata type in %s" % repository["id"])
        if "primary" not in metadata_types:
            raise ValidationError(
                "repository %s does not lock primary metadata" % repository["id"]
            )
    packages = document["packages"]
    nevras = [package["nevra"] for package in packages]
    _reject_duplicates(nevras, "NEVRA")
    if nevras != sorted(nevras):
        raise ValidationError("packages must be sorted by NEVRA")

    name_arches = ["%s.%s" % (package["name"], package["arch"]) for package in packages]
    _reject_duplicates(name_arches, "package name/arch")
    _reject_duplicates(
        [package["location"] for package in packages], "repository location"
    )
    _reject_duplicates([package["url"] for package in packages], "package URL")
    _reject_duplicates(
        [Path(package["location"]).name for package in packages], "RPM filename"
    )

    for package in packages:
        if package["name"] in FORBIDDEN_ROOTS:
            raise ValidationError("forbidden sysroot package: %s" % package["name"])
        if package["epoch"] < 0:
            raise ValidationError("%s: epoch must not be negative" % package["nevra"])
        if package["size"] <= 0 or package["install_size"] < 0:
            raise ValidationError("%s: invalid package sizes" % package["nevra"])
        if package["arch"] not in expected_arches:
            raise ValidationError(
                "%s: package arch %s is outside %r"
                % (package["nevra"], package["arch"], expected_arches)
            )
        expected_nevra = "%s-%d:%s-%s.%s" % (
            package["name"],
            package["epoch"],
            package["version"],
            package["release"],
            package["arch"],
        )
        if package["nevra"] != expected_nevra:
            raise ValidationError(
                "%s: NEVRA fields encode %s" % (package["nevra"], expected_nevra)
            )
        if package["location"].startswith("/") or ".." in Path(package["location"]).parts:
            raise ValidationError("%s: unsafe repository location" % package["nevra"])
        if package["repo_id"] not in repositories:
            raise ValidationError(
                "%s: unknown repository %s" % (package["nevra"], package["repo_id"])
            )
        repository = repositories[package["repo_id"]]
        expected_url = repository["baseurl"] + package["location"]
        if package["url"] != expected_url:
            raise ValidationError(
                "%s: URL does not match repository baseurl and location"
                % package["nevra"]
            )
        if package["sha256"] != package["repository_checksum"]["value"]:
            raise ValidationError(
                "%s: downloaded SHA256 differs from repository metadata"
                % package["nevra"]
            )
        if package["signing_key_fingerprint"] != repository["gpg_key"]["fingerprint"]:
            raise ValidationError(
                "%s: signing key does not match repository key" % package["nevra"]
            )
        if package["reason"] == "root" and package["name"] not in root_names:
            raise ValidationError(
                "%s: package is marked root but was not requested" % package["nevra"]
            )

    packages_by_name = {package["name"]: package for package in packages}
    for root in roots:
        package = packages_by_name.get(root["name"])
        if package is None or package["arch"] != arch or package["reason"] != "root":
            raise ValidationError(
                "root %s is not represented by a target-arch root package"
                % root["name"]
            )


def validate_document(document, schema):
    if not isinstance(document, dict) or not isinstance(schema, dict):
        raise ValidationError("configuration and schema roots must be JSON objects")
    validate_schema_subset(schema)
    validate(document, schema, schema, "$")
    validate_semantics(document)


def schema_for(document):
    if not isinstance(document, dict):
        raise ValidationError("configuration root must be a JSON object")
    kind = document.get("kind")
    if kind not in SCHEMAS:
        raise ValidationError("unsupported sysroot document kind: %r" % kind)
    return load_json(SCHEMAS[kind])


def canonical_digest(document):
    canonical = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_release_binding(document, lock_path, release_path):
    release = load_json(release_path)
    release_schema = load_json(REPOSITORY / "config/schemas/release.schema.json")
    validate_schema_subset(release_schema)
    validate(release, release_schema, release_schema, "$")

    arch = document["identity"]["arch"]
    matches = [target for target in release["targets"] if target["arch"] == arch]
    if len(matches) != 1:
        raise ValidationError("release must contain exactly one %s target" % arch)
    target = matches[0]
    if target["triple"] != document["identity"]["target_triple"]:
        raise ValidationError("sysroot target triple differs from release.json")
    pin = target["sysroot"]
    if pin["status"] != "locked":
        raise ValidationError("release sysroot pin is not locked for %s" % arch)

    expected_path = (REPOSITORY / pin["lock_file"]).resolve()
    if lock_path.resolve() != expected_path:
        raise ValidationError("lock path differs from release.json: %s" % expected_path)
    digest = canonical_digest(document)
    if pin["canonical_sha256"] != digest:
        raise ValidationError("lock canonical SHA256 differs from release.json")

    base = release["base_image"]
    solver = document["solver"]
    expected_image = "%s:%s" % (base["repository"], base["tag"])
    if solver["image"] != expected_image or solver["image_digest"] != base["digest"]:
        raise ValidationError("lock resolver image differs from release.json")

    trust = release["trust"]["rocky_rpm_key"]
    repository_key = document["repositories"][0]["gpg_key"]
    if (
        repository_key["sha256"] != trust["sha256"]
        or repository_key["fingerprint"] != trust["fingerprint"]
    ):
        raise ValidationError("lock RPM key differs from release.json trust root")
    key_path = REPOSITORY / trust["file"]
    if file_sha256(key_path) != trust["sha256"]:
        raise ValidationError("repository RPM key file differs from release.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=REPOSITORY / "config/sysroots/el8-x86_64.plan.json",
    )
    parser.add_argument(
        "--require-lock",
        action="store_true",
        help="reject planning manifests in release gates",
    )
    parser.add_argument(
        "--release-config",
        type=Path,
        default=REPOSITORY / "config/release.json",
        help="release configuration that anchors lock identity and trust",
    )
    arguments = parser.parse_args()

    try:
        document = load_json(arguments.config)
        schema = schema_for(document)
        validate_document(document, schema)
        if arguments.require_lock and document["kind"] != "sysroot-lock":
            raise ValidationError("planning manifest is not a resolved release lock")
        if document["kind"] == "sysroot-lock":
            validate_release_binding(document, arguments.config, arguments.release_config)
    except ValidationError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1

    digest = canonical_digest(document)
    print(
        "valid %s: %s (canonical sha256:%s)"
        % (document["kind"], arguments.config, digest)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
