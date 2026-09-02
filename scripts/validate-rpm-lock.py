#!/usr/bin/env python3
"""Validate canonical RPM plans, DNF transactions, and content locks."""

import argparse
import hashlib
import json
import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ValidationError = STRICT["ValidationError"]
load_json = STRICT["load_json"]
SCHEMAS = {
    "rpm-plan": REPOSITORY / "config/schemas/rpm-plan.schema.json",
    "rpm-transaction": REPOSITORY / "config/schemas/rpm-transaction.schema.json",
    "rpm-lock": REPOSITORY / "config/schemas/rpm-lock.schema.json",
}
TARGET_TRIPLES = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
}
SYSROOT_ROOTS = {
    "glibc",
    "glibc-devel",
    "glibc-headers",
    "glibc-minimal-langpack",
    "kernel-headers",
    "libgcc",
    "libstdc++",
    "bzip2-devel",
    "libffi-devel",
    "libuuid-devel",
    "openssl-devel",
    "sqlite-devel",
    "xz-devel",
    "zlib-devel",
}
SYSROOT_FORBIDDEN = {
    "binutils",
    "gcc",
    "glibc-static",
    "libstdc++-devel",
    "libstdc++-static",
}
HOST_COMMON_ROOTS = {
    "bzip2",
    "diffutils",
    "file",
    "findutils",
    "gcc-toolset-15-binutils",
    "gcc-toolset-15-gcc",
    "gcc-toolset-15-gcc-c++",
    "glibc-devel",
    "gmp-devel",
    "gzip",
    "libmpc-devel",
    "make",
    "mpfr-devel",
    "patch",
    "perl-interpreter",
    "redhat-rpm-config",
    "rpm-build",
    "scl-utils-build",
    "sed",
    "tar",
    "which",
    "xz",
    "zlib-devel",
}
HOST_GCC_ROOTS = {"bison", "flex", "libzstd-devel"}
HOST_PYTHON_ROOTS = {
    "bzip2-devel",
    "libffi-devel",
    "libuuid-devel",
    "openssl-devel",
    "sqlite-devel",
    "xz-devel",
}
HOST_MODULES = [
    "perl:5.26",
    "perl-IO-Socket-SSL:2.066",
    "perl-libwww-perl:6.34",
]


def canonical_sha256(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def safe_posix_location(value, label):
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "?" in value
        or "#" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError("unsafe %s: %s" % (label, value))
    relative = PurePosixPath(value)
    if (
        not relative.parts
        or relative.is_absolute()
        or ".." in relative.parts
        or ":" in relative.parts[0]
        or str(relative) != value
    ):
        raise ValidationError("unsafe %s: %s" % (label, value))
    return relative


def checked_metadata_path(metadata_root, location, trusted_anchor):
    relative = safe_posix_location(location, "repository metadata location")
    if trusted_anchor.is_symlink():
        raise ValidationError("trusted metadata anchor is a symlink")
    try:
        root_relative = metadata_root.relative_to(trusted_anchor)
    except ValueError:
        raise ValidationError("repository metadata root escapes its trusted anchor")
    current = trusted_anchor
    for part in root_relative.parts + relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValidationError("repository metadata path contains a symlink")
    anchor = trusted_anchor.resolve()
    root = metadata_root.resolve()
    path = current.resolve()
    if anchor != root and anchor not in root.parents:
        raise ValidationError("repository metadata root escaped its trusted anchor")
    if root != path and root not in path.parents:
        raise ValidationError("repository metadata escaped its directory")
    return current


def parse_repomd(path):
    namespace = {"repo": "http://linux.duke.edu/metadata/repo"}
    try:
        root = ElementTree.parse(str(path)).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise ValidationError("cannot parse checked repomd.xml: %s" % error)
    revision = root.find("repo:revision", namespace)
    records = []
    seen_types = set()
    seen_locations = set()
    for element in root.findall("repo:data", namespace):
        metadata_type = element.get("type")
        checksum = element.find("repo:checksum", namespace)
        open_checksum = element.find("repo:open-checksum", namespace)
        location = element.find("repo:location", namespace)
        size = element.find("repo:size", namespace)
        open_size = element.find("repo:open-size", namespace)
        if (
            not metadata_type
            or metadata_type in seen_types
            or checksum is None
            or checksum.get("type") != "sha256"
            or not is_sha256(checksum.text)
            or location is None
            or not location.get("href")
            or size is None
            or not size.text
        ):
            raise ValidationError("invalid or duplicate checked repomd record")
        relative = str(
            safe_posix_location(location.get("href"), "repomd metadata location")
        )
        if relative in seen_locations:
            raise ValidationError("duplicate checked repomd metadata location")
        try:
            compressed_size = int(size.text)
            expanded_size = int(open_size.text) if open_size is not None else None
        except (TypeError, ValueError):
            raise ValidationError("invalid checked repomd metadata size")
        if compressed_size <= 0 or (expanded_size is not None and expanded_size < 0):
            raise ValidationError("invalid checked repomd metadata size")
        expanded_checksum = None
        if open_checksum is not None:
            if open_checksum.get("type") != "sha256" or not is_sha256(open_checksum.text):
                raise ValidationError("invalid checked repomd open checksum")
            expanded_checksum = {
                "algorithm": "sha256",
                "value": open_checksum.text,
            }
        if (expanded_checksum is None) != (expanded_size is None):
            raise ValidationError("checked repomd open checksum/size must be paired")
        records.append(
            {
                "type": metadata_type,
                "location": relative,
                "checksum": {"algorithm": "sha256", "value": checksum.text},
                "size": compressed_size,
                "open_checksum": expanded_checksum,
                "open_size": expanded_size,
            }
        )
        seen_types.add(metadata_type)
        seen_locations.add(relative)
    missing = sorted({"primary", "filelists", "primary_db"} - seen_types)
    if missing:
        raise ValidationError("checked repomd is missing metadata: %s" % ", ".join(missing))
    return (
        revision.text if revision is not None and revision.text else None,
        sorted(records, key=lambda item: (item["type"], item["location"])),
    )


def validate_repomd_claim(repository, repomd_path):
    revision, records = parse_repomd(repomd_path)
    if revision != repository["repomd"]["revision"]:
        raise ValidationError("transaction revision differs from signed repomd")
    if records != repository["metadata"]:
        raise ValidationError("transaction metadata differs from signed repomd")


def validate_repository_trust(repository, trust):
    if repository["gpg_key"] != {
        "sha256": trust["sha256"],
        "fingerprint": trust["fingerprint"],
    }:
        raise ValidationError("repository key differs from release.json")
    if repository["repomd"]["signature"]["fingerprint"] != trust["fingerprint"]:
        raise ValidationError("repomd signature claim differs from release.json")


def verify_detached_signature(key, fingerprint, signature, content):
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    with tempfile.TemporaryDirectory(prefix="crossforge-repomd-gpg-") as temporary:
        os.chmod(temporary, 0o700)
        imported = subprocess.run(
            [
                "gpg",
                "--batch",
                "--no-autostart",
                "--homedir",
                temporary,
                "--import",
                str(key),
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        if imported.returncode != 0:
            raise ValidationError("cannot import locked Rocky key for repomd verification")
        verified = subprocess.run(
            [
                "gpg",
                "--batch",
                "--no-autostart",
                "--homedir",
                temporary,
                "--status-fd",
                "1",
                "--verify",
                str(signature),
                str(content),
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        if verified.returncode != 0:
            raise ValidationError("repomd detached signature verification failed")
        valid = [
            line.split()[2].lower()
            for line in verified.stdout.splitlines()
            if line.startswith("[GNUPG:] VALIDSIG ") and len(line.split()) >= 3
        ]
        if valid != [fingerprint]:
            raise ValidationError("repomd signature uses an unexpected key")


def schema_for(document):
    if not isinstance(document, dict) or document.get("kind") not in SCHEMAS:
        raise ValidationError("unsupported RPM document kind")
    return load_json(SCHEMAS[document["kind"]])


def validate_schema(document):
    schema = schema_for(document)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")


def reject_duplicates(values, label):
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValidationError("duplicate %s: %s" % (label, ", ".join(duplicates)))


def sorted_unique(values, label):
    if values != sorted(values) or len(values) != len(set(values)):
        raise ValidationError("%s must be sorted and unique" % label)


def nevra_name_arch(nevra):
    try:
        name_epoch, _version_release = nevra.split(":", 1)
        name, epoch = name_epoch.rsplit("-", 1)
        arch = nevra.rsplit(".", 1)[1]
    except (IndexError, ValueError):
        raise ValidationError("invalid canonical NEVRA: %s" % nevra)
    if not name or not epoch.isdigit() or not arch:
        raise ValidationError("invalid canonical NEVRA: %s" % nevra)
    return name, arch


def repository_file(reference, label):
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError("unsafe %s: %s" % (label, reference))
    path = (REPOSITORY / relative).resolve()
    if REPOSITORY not in path.parents:
        raise ValidationError("%s escapes the repository" % label)
    return path


def expected_role_roots(role):
    return {
        "target-sysroot": SYSROOT_ROOTS,
        "host-build-common": HOST_COMMON_ROOTS,
        "host-gcc-build": HOST_GCC_ROOTS,
        "host-python-build": HOST_PYTHON_ROOTS,
    }[role]


def validate_plan_semantics(plan):
    identity = plan["identity"]
    role = identity["role"]
    arch = identity["arch"]
    if role == "target-sysroot":
        expected_name = "sysroot-el8-%s" % arch
        if identity["target_triple"] != TARGET_TRIPLES[arch]:
            raise ValidationError("target sysroot triple and arch disagree")
        if plan["base"]["mode"] != "empty":
            raise ValidationError("target sysroot must use an empty base")
        expected_repositories = ["baseos"]
        expected_repository_urls = [
            "https://download.rockylinux.org/pub/rocky/8.10/BaseOS/%s/os/" % arch
        ]
        expected_modules = []
    else:
        if arch != "x86_64":
            raise ValidationError("host RPM plans must use x86_64")
        expected_name = "%s-el8-x86_64" % role
        if identity["target_triple"] is not None:
            raise ValidationError("host plan target_triple must be null")
        expected_repositories = ["baseos", "appstream"]
        expected_repository_urls = [
            "https://download.rockylinux.org/pub/rocky/8.10/BaseOS/x86_64/os/",
            "https://download.rockylinux.org/pub/rocky/8.10/AppStream/x86_64/os/",
        ]
        expected_modules = HOST_MODULES
        expected_mode = "image" if role == "host-build-common" else "lock"
        if plan["base"]["mode"] != expected_mode:
            raise ValidationError("%s must use a %s base" % (role, expected_mode))
    if identity["name"] != expected_name:
        raise ValidationError("RPM plan identity name differs from its role/architecture")
    if [repo["id"] for repo in plan["repositories"]] != expected_repositories:
        raise ValidationError("repository order/set differs from the role contract")
    if [repo["baseurl"] for repo in plan["repositories"]] != expected_repository_urls:
        raise ValidationError("repository URLs differ from the Rocky role contract")
    if plan["solver_policy"]["allowed_arches"] != [arch, "noarch"]:
        raise ValidationError("allowed_arches must be target arch followed by noarch")
    if plan["solver_policy"]["module_platform_id"] != "platform:el8":
        raise ValidationError("module platform must be platform:el8")
    if plan["solver_policy"]["enabled_modules"] != expected_modules:
        raise ValidationError("enabled module streams differ from the role contract")
    root_names = [root["name"] for root in plan["roots"]]
    reject_duplicates(root_names, "root package")
    if set(root_names) != expected_role_roots(role):
        raise ValidationError("root set differs from the %s contract" % role)
    if role == "target-sysroot":
        if any(root["arch"] != "target" for root in plan["roots"]):
            raise ValidationError("sysroot roots must select the target arch")
    elif any(root["arch"] != "any" for root in plan["roots"]):
        raise ValidationError("host roots must use DNF's best target/noarch choice")


def load_referenced_plan(transaction):
    reference = transaction["plan"]
    path = repository_file(reference["file"], "plan reference")
    plan = load_json(path)
    validate_schema(plan)
    validate_plan_semantics(plan)
    if canonical_sha256(plan) != reference["canonical_sha256"]:
        raise ValidationError("transaction plan digest mismatch")
    if transaction["identity"] != plan["identity"]:
        raise ValidationError("transaction identity differs from plan")
    if transaction["base"] != plan["base"]:
        raise ValidationError("transaction base differs from plan")
    transaction_policy = dict(transaction["solver_policy"])
    plan_policy = dict(plan["solver_policy"])
    transaction_policy["enabled_modules"] = sorted(
        transaction_policy["enabled_modules"]
    )
    plan_policy["enabled_modules"] = sorted(plan_policy["enabled_modules"])
    if transaction_policy != plan_policy:
        raise ValidationError("transaction policy differs from plan")
    transaction_repositories = transaction["repositories"]
    plan_repositories = {item["id"]: item for item in plan["repositories"]}
    if [item["id"] for item in transaction_repositories] != sorted(plan_repositories):
        raise ValidationError("transaction repositories differ from plan")
    for actual in transaction_repositories:
        if actual["baseurl"] != plan_repositories[actual["id"]]["baseurl"]:
            raise ValidationError("transaction repository URL differs from plan")
    return plan


def validate_manifest(manifest, label):
    packages = manifest["packages"]
    sorted_unique(packages, label)
    if canonical_sha256(packages) != manifest["canonical_sha256"]:
        raise ValidationError("%s canonical digest is invalid" % label)
    return packages


def validate_nevra_fields(item):
    expected = "%s-%d:%s-%s.%s" % (
        item["name"],
        item["epoch"],
        item["version"],
        item["release"],
        item["arch"],
    )
    if item["nevra"] != expected:
        raise ValidationError("transaction NEVRA fields encode %s" % expected)


def load_parent_transaction(plan):
    if plan["base"]["mode"] != "lock":
        return None
    lock_path = repository_file(plan["base"]["parent_lock"], "parent lock")
    parent_lock = load_json(lock_path)
    validate_schema(parent_lock)
    if canonical_sha256(parent_lock) != plan["base"]["parent_sha256"]:
        raise ValidationError("parent lock digest differs from plan")
    return load_referenced_transaction(parent_lock)


def validate_transaction_semantics(transaction):
    plan = load_referenced_plan(transaction)
    role = transaction["identity"]["role"]
    resolver = transaction["resolver"]
    expected_system_repo = plan["base"]["mode"] != "empty"
    if resolver["load_system_repo"] is not expected_system_repo:
        raise ValidationError("resolver system-repo mode differs from plan base")
    component_names = [item["name"] for item in resolver["components"]]
    sorted_unique(component_names, "resolver components")
    repositories = transaction["repositories"]
    for repository in repositories:
        metadata_types = [item["type"] for item in repository["metadata"]]
        reject_duplicates(metadata_types, "repository metadata type")
        if "primary" not in metadata_types:
            raise ValidationError("repository does not bind primary metadata")
        if not repository["gpgcheck"] or not repository["repo_gpgcheck"]:
            raise ValidationError("repository and payload GPG checks are mandatory")
    requests = transaction["requests"]
    if [
        (item["name"], item["arch"], item["purpose"])
        for item in requests
    ] != [
        (item["name"], item["arch"], item["purpose"])
        for item in sorted(
            plan["roots"], key=lambda item: (item["name"], item["arch"], item["purpose"])
        )
    ]:
        raise ValidationError("transaction requests differ from sorted plan roots")
    items = transaction["items"]
    item_keys = [(item["nevra"], item["action"]) for item in items]
    if item_keys != sorted(item_keys) or len(item_keys) != len(set(item_keys)):
        raise ValidationError("transaction items must be sorted and unique")
    allowed_arches = set(plan["solver_policy"]["allowed_arches"])
    repositories_by_id = {item["id"]: item for item in repositories}
    repo_ids = set(repositories_by_id)
    forward = []
    removed = []
    for item in items:
        validate_nevra_fields(item)
        if item["arch"] not in allowed_arches:
            raise ValidationError("transaction selected a forbidden architecture")
        if item["action"] in ("install", "upgrade"):
            forward.append(item)
            allowed_reasons = {"user", "dependency"}
            if role != "target-sysroot" and item["action"] == "upgrade":
                # DNF 4.7 reports some explicitly requested upgrades as
                # unknown; preserve that fact instead of inventing user.
                allowed_reasons.add("unknown")
            if item["reason"] not in allowed_reasons:
                raise ValidationError("forward item has non-canonical DNF reason")
            if item["repo_id"] not in repo_ids:
                raise ValidationError("forward item uses an undeclared repository")
            if any(
                item[field] is None
                for field in (
                    "location", "url", "repository_checksum", "size", "install_size", "source_rpm"
                )
            ):
                raise ValidationError("forward item lacks repository identity")
            if item["repository_checksum"]["algorithm"] != "sha256":
                raise ValidationError("RPM repository checksum must be SHA256")
            location = str(safe_posix_location(item["location"], "RPM location"))
            expected_url = repositories_by_id[item["repo_id"]]["baseurl"] + location
            if item["url"] != expected_url:
                raise ValidationError("RPM URL differs from repository baseurl/location")
            if role == "target-sysroot" and item["name"] in SYSROOT_FORBIDDEN:
                raise ValidationError("forbidden package entered target sysroot")
        elif item["action"] == "remove":
            removed.append(item)
            if any(
                item[field] is not None
                for field in (
                    "location", "url", "repository_checksum", "size", "install_size", "source_rpm"
                )
            ):
                raise ValidationError("remove item carries repository payload identity")
        else:
            raise ValidationError("unsupported transaction action: %s" % item["action"])
    base_manifest = validate_manifest(transaction["manifests"]["base"], "base manifest")
    remove_manifest = validate_manifest(transaction["manifests"]["remove"], "remove manifest")
    result_manifest = validate_manifest(transaction["manifests"]["result"], "result manifest")
    forward_nevras = {item["nevra"] for item in forward}
    removed_nevras = {item["nevra"] for item in removed}
    if removed_nevras != set(remove_manifest):
        raise ValidationError("remove items differ from remove manifest")
    expected_result = (set(base_manifest) - removed_nevras) | forward_nevras
    if expected_result != set(result_manifest):
        raise ValidationError("transaction items do not encode the result manifest")
    request_nevras = {item["resolved_nevra"] for item in requests}
    if len(request_nevras) != len(requests):
        raise ValidationError("multiple roots resolve to the same RPM")
    if not request_nevras.issubset(set(result_manifest)):
        raise ValidationError("a root request is absent from result manifest")
    for request in requests:
        resolved_name, resolved_arch = nevra_name_arch(request["resolved_nevra"])
        if resolved_name != request["name"]:
            raise ValidationError("root request resolves to a different package name")
        selector = request["arch"]
        target_arch = transaction["identity"]["arch"]
        if selector == "target" and resolved_arch != target_arch:
            raise ValidationError("target root resolves to the wrong architecture")
        if selector == "noarch" and resolved_arch != "noarch":
            raise ValidationError("noarch root resolves to the wrong architecture")
        if selector == "any" and resolved_arch not in (target_arch, "noarch"):
            raise ValidationError("host root resolves outside target/noarch")
        expected = "transaction" if request["resolved_nevra"] in forward_nevras else "base"
        if request["disposition"] != expected:
            raise ValidationError("root request disposition is incorrect")
        if expected == "transaction":
            matches = [item for item in forward if item["nevra"] == request["resolved_nevra"]]
            if len(matches) != 1:
                raise ValidationError("transaction root is not a unique forward item")
            accepted = {"user"} if role == "target-sysroot" else {"user", "unknown"}
            if matches[0]["reason"] not in accepted:
                raise ValidationError("transaction root has an invalid DNF reason")
    if role == "target-sysroot":
        if base_manifest or removed_nevras:
            raise ValidationError("target sysroot transaction must start empty")
        if any(item["action"] != "install" for item in forward):
            raise ValidationError("target sysroot transaction may only install")
        user_nevras = {item["nevra"] for item in forward if item["reason"] == "user"}
        if user_nevras != request_nevras:
            raise ValidationError("sysroot DNF user set differs from roots")
    parent = load_parent_transaction(plan)
    if parent is not None:
        if parent["manifests"]["result"] != transaction["manifests"]["base"]:
            raise ValidationError("delta base differs from parent result manifest")
    return plan


def load_referenced_transaction(lock):
    reference = lock["transaction"]
    path = repository_file(reference["file"], "transaction reference")
    transaction = load_json(path)
    validate_schema(transaction)
    if canonical_sha256(transaction) != reference["canonical_sha256"]:
        raise ValidationError("transaction canonical SHA256 differs from lock")
    validate_transaction_semantics(transaction)
    return transaction


def validate_lock_semantics(lock):
    transaction = load_referenced_transaction(lock)
    forward = sorted(
        [item for item in transaction["items"] if item["action"] in ("install", "upgrade")],
        key=lambda item: item["nevra"],
    )
    packages = lock["packages"]
    if [item["nevra"] for item in packages] != [item["nevra"] for item in forward]:
        raise ValidationError("content lock package set differs from DNF transaction")
    fingerprints = {repo["gpg_key"]["fingerprint"] for repo in transaction["repositories"]}
    if len(fingerprints) != 1:
        raise ValidationError("RPM transaction uses multiple signing trust roots")
    fingerprint = next(iter(fingerprints))
    for package, item in zip(packages, forward):
        header = package["header"]
        if header != {
            "name": item["name"],
            "epoch": item["epoch"],
            "version": item["version"],
            "release": item["release"],
            "arch": item["arch"],
            "nevra": item["nevra"],
            "source_rpm": item["source_rpm"],
        }:
            raise ValidationError("verified RPM header differs from transaction")
        if package["received_sha256"] != item["repository_checksum"]["value"]:
            raise ValidationError("received RPM differs from repository checksum")
        if package["signature"]["fingerprint"] != fingerprint:
            raise ValidationError("RPM signature differs from repository trust root")
    return transaction


def validate_release_binding(lock, lock_path, release_path):
    transaction = validate_lock_semantics(lock)
    release = load_json(release_path)
    release_schema = load_json(REPOSITORY / "config/schemas/release.schema.json")
    STRICT["validate_schema_subset"](release_schema)
    STRICT["validate"](release, release_schema, release_schema, "$")
    resolver = transaction["resolver"]
    base = release["base_image"]
    if resolver["image"] != "%s:%s" % (base["repository"], base["tag"]):
        raise ValidationError("resolver image differs from release.json")
    if resolver["image_digest"] != base["digest"]:
        raise ValidationError("resolver digest differs from release.json")
    trust = release["trust"]["rocky_rpm_key"]
    for repository in transaction["repositories"]:
        validate_repository_trust(repository, trust)
        metadata_root = REPOSITORY / "locks/metadata" / transaction["identity"]["name"] / repository["id"]
        for record in (repository["repomd"], repository["repomd"]["signature"]):
            path = checked_metadata_path(metadata_root, record["location"], REPOSITORY)
            if not path.is_file():
                raise ValidationError("checked repository identity file is missing: %s" % path)
            if path.stat().st_size != record["size"] or file_sha256(path) != record["sha256"]:
                raise ValidationError("checked repository identity file differs: %s" % path)
    key_path = REPOSITORY / trust["file"]
    if file_sha256(key_path) != trust["sha256"]:
        raise ValidationError("Rocky key file differs from release.json")
    for repository in transaction["repositories"]:
        metadata_root = (
            REPOSITORY
            / "locks/metadata"
            / transaction["identity"]["name"]
            / repository["id"]
        )
        verify_detached_signature(
            key_path,
            trust["fingerprint"],
            checked_metadata_path(
                metadata_root,
                repository["repomd"]["signature"]["location"],
                REPOSITORY,
            ),
            checked_metadata_path(
                metadata_root, repository["repomd"]["location"], REPOSITORY
            ),
        )
        validate_repomd_claim(
            repository,
            checked_metadata_path(
                metadata_root, repository["repomd"]["location"], REPOSITORY
            ),
        )
    role = transaction["identity"]["role"]
    if role == "target-sysroot":
        matches = [
            target for target in release["targets"]
            if target["arch"] == transaction["identity"]["arch"]
        ]
        if len(matches) != 1:
            raise ValidationError("release target is not unique")
        pin = matches[0]["sysroot"]
    else:
        pin = release["host_locks"][role]
    try:
        relative = lock_path.resolve().relative_to(REPOSITORY).as_posix()
    except ValueError:
        raise ValidationError("lock path is outside the repository")
    if pin["status"] != "locked" or pin["lock_file"] != relative:
        raise ValidationError("release lock path binding differs")
    if pin["canonical_sha256"] != canonical_sha256(lock):
        raise ValidationError("release lock digest binding differs")
    return transaction


def validate_document(document):
    validate_schema(document)
    if document["kind"] == "rpm-plan":
        validate_plan_semantics(document)
    elif document["kind"] == "rpm-transaction":
        validate_transaction_semantics(document)
    else:
        validate_lock_semantics(document)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path)
    parser.add_argument("--require-lock", action="store_true")
    parser.add_argument("--release-config", type=Path, default=REPOSITORY / "config/release.json")
    arguments = parser.parse_args()
    try:
        document = load_json(arguments.document)
        validate_document(document)
        if arguments.require_lock and document["kind"] != "rpm-lock":
            raise ValidationError("document is not a verified RPM content lock")
        if document["kind"] == "rpm-lock":
            validate_release_binding(document, arguments.document, arguments.release_config)
    except (OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(
        "valid %s: %s (canonical sha256:%s)"
        % (document["kind"], arguments.document, canonical_sha256(document))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
