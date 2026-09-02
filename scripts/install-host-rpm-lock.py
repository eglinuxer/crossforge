#!/usr/bin/env python3
"""Verify and install an exact, content-locked host RPM transaction."""

import argparse
import hashlib
import json
import os
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


REPOSITORY = Path(__file__).resolve().parents[1]
ALLOWED_ROLES = {"host-build-common", "host-gcc-build", "host-python-build"}
ALLOWED_ACTIONS = {"install", "upgrade"}
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
HEX_FINGERPRINT = re.compile(r"^[0-9a-f]{40}$")
INVENTORY_NEVRA = re.compile(r"^.+-[0-9]+:.+-.+\.(?:[A-Za-z0-9_]+|\(none\))$")
LOCK_PACKAGE_FIELDS = {"nevra", "received_sha256", "header", "signature"}
HEADER_FIELDS = {"name", "epoch", "version", "release", "arch", "nevra", "source_rpm"}
SIGNATURE_FIELDS = {"status", "fingerprint"}
ITEM_FIELDS = {
    "name", "epoch", "version", "release", "arch", "nevra", "repo_id",
    "action", "reason", "location", "url", "repository_checksum", "size",
    "install_size", "source_rpm",
}


class ValidationError(RuntimeError):
    pass


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("duplicate JSON key: %r" % key)
        result[key] = value
    return result


def reject_symlink_components(path, label):
    absolute = Path(os.path.abspath(str(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if os.path.lexists(str(current)) and current.is_symlink():
            raise ValidationError("%s contains a symlink component: %s" % (label, current))


def require_regular_file(path, label):
    reject_symlink_components(path, label)
    if path.is_symlink() or not path.is_file():
        raise ValidationError("%s must be a regular, non-symlink file: %s" % (label, path))


def load_json(path, label):
    require_regular_file(path, label)
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream, object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError("invalid JSON in %s: %s" % (path, error))


def canonical_digest(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_schema(document, schema_name, label):
    validator_path = REPOSITORY / "scripts/validate-release.py"
    schema_path = REPOSITORY / "config/schemas" / schema_name
    require_regular_file(validator_path, "strict JSON validator")
    require_regular_file(schema_path, label + " schema")
    tools = runpy.run_path(str(validator_path))
    try:
        schema = tools["load_json"](schema_path)
        tools["validate_schema_subset"](schema)
        tools["validate"](document, schema, schema, "$")
    except tools["ValidationError"] as error:
        raise ValidationError("%s schema validation failed: %s" % (label, error))


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_object(value, label):
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % label)
    return value


def require_string(value, label):
    if not isinstance(value, str) or not value:
        raise ValidationError("%s must be a non-empty string" % label)
    return value


def require_integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValidationError("%s must be an integer >= %d" % (label, minimum))
    return value


def require_fields(value, required, label):
    missing = sorted(required - set(value))
    if missing:
        raise ValidationError("%s is missing fields: %s" % (label, ", ".join(missing)))


def safe_relative_path(value, label):
    value = require_string(value, label)
    if "\\" in value:
        raise ValidationError("%s must use POSIX path separators" % label)
    path = PurePosixPath(value)
    if path.is_absolute() or value in (".", "..") or ".." in path.parts or value != str(path):
        raise ValidationError("%s is unsafe or non-canonical: %s" % (label, value))
    return path


def validate_inventory(values, label):
    if not isinstance(values, list):
        raise ValidationError("%s must be an array" % label)
    for index, value in enumerate(values):
        require_string(value, "%s[%d]" % (label, index))
        if not INVENTORY_NEVRA.match(value) or any(character.isspace() for character in value):
            raise ValidationError("%s contains an invalid canonical NEVRA: %r" % (label, value))
    if values != sorted(values):
        raise ValidationError("%s must be sorted" % label)
    if len(values) != len(set(values)):
        raise ValidationError("%s contains duplicate NEVRAs" % label)
    return values


def nevra_name_arch(nevra):
    try:
        name_epoch, _remainder = nevra.split(":", 1)
        name, _epoch = name_epoch.rsplit("-", 1)
        arch = nevra.rsplit(".", 1)[1]
    except (IndexError, ValueError):
        raise ValidationError("invalid canonical NEVRA: %s" % nevra)
    return name, arch


def expected_nevra(header):
    return "%s-%d:%s-%s.%s" % (
        header["name"], header["epoch"], header["version"],
        header["release"], header["arch"],
    )


def validate_lock(lock):
    lock = require_object(lock, "$")
    require_fields(lock, {"schema_version", "kind", "transaction", "packages"}, "$")
    if require_integer(lock["schema_version"], "$.schema_version", 1) != 1:
        raise ValidationError("$.schema_version must be 1")
    if lock["kind"] != "rpm-lock":
        raise ValidationError("$.kind must be 'rpm-lock'")
    reference = require_object(lock["transaction"], "$.transaction")
    require_fields(reference, {"file", "canonical_sha256"}, "$.transaction")
    transaction_file = safe_relative_path(reference["file"], "$.transaction.file")
    transaction_sha256 = require_string(
        reference["canonical_sha256"], "$.transaction.canonical_sha256"
    )
    if not HEX_SHA256.match(transaction_sha256):
        raise ValidationError("$.transaction.canonical_sha256 is invalid")

    packages = lock["packages"]
    if not isinstance(packages, list) or not packages:
        raise ValidationError("$.packages must be a non-empty array")
    nevras = []
    for index, package in enumerate(packages):
        label = "$.packages[%d]" % index
        package = require_object(package, label)
        require_fields(package, LOCK_PACKAGE_FIELDS, label)
        nevra = require_string(package["nevra"], label + ".nevra")
        received_sha256 = require_string(package["received_sha256"], label + ".received_sha256")
        if not HEX_SHA256.match(received_sha256):
            raise ValidationError("%s.received_sha256 is invalid" % label)
        header = require_object(package["header"], label + ".header")
        require_fields(header, HEADER_FIELDS, label + ".header")
        require_string(header["name"], label + ".header.name")
        require_integer(header["epoch"], label + ".header.epoch")
        require_string(header["version"], label + ".header.version")
        require_string(header["release"], label + ".header.release")
        if header["arch"] not in ("x86_64", "noarch"):
            raise ValidationError("%s.header.arch is not a host architecture" % label)
        require_string(header["source_rpm"], label + ".header.source_rpm")
        if nevra != header["nevra"] or nevra != expected_nevra(header):
            raise ValidationError("%s header does not encode %s" % (label, nevra))
        signature = require_object(package["signature"], label + ".signature")
        require_fields(signature, SIGNATURE_FIELDS, label + ".signature")
        if signature["status"] != "verified":
            raise ValidationError("%s signature is not verified" % label)
        fingerprint = require_string(signature["fingerprint"], label + ".signature.fingerprint")
        if not HEX_FINGERPRINT.match(fingerprint):
            raise ValidationError("%s signature fingerprint is invalid" % label)
        nevras.append(nevra)
    if nevras != sorted(nevras):
        raise ValidationError("lock packages must be sorted by NEVRA")
    if len(nevras) != len(set(nevras)):
        raise ValidationError("lock packages contain duplicate NEVRAs")
    return {
        "document": lock,
        "packages": packages,
        "transaction_file": transaction_file,
        "transaction_sha256": transaction_sha256,
    }


def transaction_candidates(lock_path, bundle, reference, explicit):
    if explicit is not None:
        return [explicit]
    candidates = [
        (REPOSITORY.joinpath(*reference.parts), REPOSITORY),
        (bundle.joinpath(*reference.parts), bundle),
        (lock_path.parent.joinpath(*reference.parts), lock_path.parent),
    ]
    unique = []
    seen = set()
    for candidate, root in candidates:
        if not path_is_within(candidate, root):
            raise ValidationError("transaction reference escapes %s" % root)
        normalized = os.path.abspath(str(candidate))
        if normalized not in seen:
            seen.add(normalized)
            unique.append(Path(normalized))
    return unique


def load_transaction(validated_lock, lock_path, bundle, explicit):
    matches = []
    errors = []
    for candidate in transaction_candidates(
        lock_path, bundle, validated_lock["transaction_file"], explicit
    ):
        if not os.path.lexists(str(candidate)):
            continue
        try:
            transaction = load_json(candidate, "transaction")
        except ValidationError as error:
            errors.append(str(error))
            continue
        digest = canonical_digest(transaction)
        if digest == validated_lock["transaction_sha256"]:
            matches.append((candidate, transaction))
        else:
            errors.append("transaction digest mismatch: %s" % candidate)
    if not matches:
        detail = "; ".join(errors) if errors else "referenced transaction was not found"
        raise ValidationError(detail)
    canonical_paths = {os.path.realpath(str(path)) for path, _transaction in matches}
    if len(canonical_paths) > 1:
        raise ValidationError("referenced transaction resolves to multiple files; use --transaction")
    return matches[0]


def validate_repository(repository, index):
    label = "transaction.repositories[%d]" % index
    repository = require_object(repository, label)
    require_fields(repository, {"id", "gpg_key", "repomd"}, label)
    repository_id = require_string(repository["id"], label + ".id")
    gpg_key = require_object(repository["gpg_key"], label + ".gpg_key")
    require_fields(gpg_key, {"sha256", "fingerprint"}, label + ".gpg_key")
    key_sha256 = require_string(gpg_key["sha256"], label + ".gpg_key.sha256")
    key_fingerprint = require_string(
        gpg_key["fingerprint"], label + ".gpg_key.fingerprint"
    )
    if not HEX_SHA256.match(key_sha256) or not HEX_FINGERPRINT.match(key_fingerprint):
        raise ValidationError("%s has invalid GPG key identity" % label)
    repomd = require_object(repository["repomd"], label + ".repomd")
    require_fields(repomd, {"signature"}, label + ".repomd")
    signature = require_object(repomd["signature"], label + ".repomd.signature")
    require_fields(signature, {"fingerprint"}, label + ".repomd.signature")
    fingerprint = require_string(signature["fingerprint"], label + ".repomd.signature.fingerprint")
    if not HEX_FINGERPRINT.match(fingerprint):
        raise ValidationError("%s has invalid repository fingerprint" % label)
    if fingerprint != key_fingerprint:
        raise ValidationError("%s repomd signature differs from its GPG key" % label)
    return repository_id, (key_sha256, fingerprint)


def validate_manifest(manifest, label):
    manifest = require_object(manifest, label)
    require_fields(manifest, {"packages", "canonical_sha256"}, label)
    packages = validate_inventory(manifest["packages"], label + ".packages")
    digest = require_string(manifest["canonical_sha256"], label + ".canonical_sha256")
    if not HEX_SHA256.match(digest) or digest != canonical_digest(packages):
        raise ValidationError("%s canonical SHA256 differs from package list" % label)
    return packages


def validate_item(item, index, repositories):
    label = "transaction.items[%d]" % index
    item = require_object(item, label)
    require_fields(item, ITEM_FIELDS, label)
    require_string(item["name"], label + ".name")
    require_integer(item["epoch"], label + ".epoch")
    require_string(item["version"], label + ".version")
    require_string(item["release"], label + ".release")
    if item["arch"] not in ("x86_64", "noarch"):
        raise ValidationError("%s has unsupported host architecture" % label)
    if item["nevra"] != expected_nevra(item):
        raise ValidationError("%s NEVRA fields are inconsistent" % label)
    if item["action"] not in ALLOWED_ACTIONS | {"remove"}:
        raise ValidationError("%s has unsupported action %r" % (label, item["action"]))
    require_string(item["reason"], label + ".reason")
    if item["action"] == "remove":
        for field in (
            "location", "url", "repository_checksum", "size",
            "install_size", "source_rpm",
        ):
            if item[field] is not None:
                raise ValidationError("%s.%s must be null for removal" % (label, field))
        return item
    repo_id = require_string(item["repo_id"], label + ".repo_id")
    if repo_id not in repositories:
        raise ValidationError("%s refers to unknown repository %s" % (label, repo_id))
    location = safe_relative_path(item["location"], label + ".location")
    if not location.name.endswith(".rpm"):
        raise ValidationError("%s location is not an RPM" % label)
    require_string(item["url"], label + ".url")
    checksum = require_object(item["repository_checksum"], label + ".repository_checksum")
    require_fields(checksum, {"algorithm", "value"}, label + ".repository_checksum")
    if checksum["algorithm"] != "sha256" or not HEX_SHA256.match(checksum["value"]):
        raise ValidationError("%s does not have a SHA256 repository checksum" % label)
    require_integer(item["size"], label + ".size", minimum=1)
    require_integer(item["install_size"], label + ".install_size")
    require_string(item["source_rpm"], label + ".source_rpm")
    return item


def normalize(validated_lock, transaction):
    transaction = require_object(transaction, "transaction")
    require_fields(
        transaction,
        {"schema_version", "kind", "identity", "repositories", "items", "manifests"},
        "transaction",
    )
    if require_integer(transaction["schema_version"], "transaction.schema_version", 1) != 1:
        raise ValidationError("transaction.schema_version must be 1")
    if transaction["kind"] != "rpm-transaction":
        raise ValidationError("referenced document is not an rpm-transaction")
    identity = require_object(transaction["identity"], "transaction.identity")
    require_fields(identity, {"role", "arch"}, "transaction.identity")
    role = require_string(identity["role"], "transaction.identity.role")
    if role not in ALLOWED_ROLES:
        raise ValidationError("unsupported host RPM lock role: %s" % role)
    if identity["arch"] != "x86_64":
        raise ValidationError("host RPM transactions must use x86_64")

    repository_values = transaction["repositories"]
    if not isinstance(repository_values, list) or not repository_values:
        raise ValidationError("transaction.repositories must be a non-empty array")
    repositories = {}
    for index, repository in enumerate(repository_values):
        repository_id, key_identity = validate_repository(repository, index)
        if repository_id in repositories:
            raise ValidationError("duplicate repository id: %s" % repository_id)
        repositories[repository_id] = key_identity

    item_values = transaction["items"]
    if not isinstance(item_values, list) or not item_values:
        raise ValidationError("transaction.items must be a non-empty array")
    items = [validate_item(item, index, repositories) for index, item in enumerate(item_values)]
    item_keys = [(item["nevra"], item["action"]) for item in items]
    if item_keys != sorted(item_keys) or len(item_keys) != len(set(item_keys)):
        raise ValidationError("transaction items must have unique, sorted NEVRA/action keys")
    forward_items = [item for item in items if item["action"] in ALLOWED_ACTIONS]
    remove_items = [item for item in items if item["action"] == "remove"]

    manifests = require_object(transaction["manifests"], "transaction.manifests")
    require_fields(manifests, {"base", "remove", "result"}, "transaction.manifests")
    base = validate_manifest(manifests["base"], "transaction.manifests.base")
    removed = validate_manifest(manifests["remove"], "transaction.manifests.remove")
    result = validate_manifest(manifests["result"], "transaction.manifests.result")
    base_set = set(base)
    removed_set = set(removed)
    result_set = set(result)
    forward_set = {item["nevra"] for item in forward_items}
    remove_set = {item["nevra"] for item in remove_items}
    if removed_set != base_set - result_set:
        raise ValidationError("remove manifest must equal base minus result")
    if forward_set != result_set - base_set:
        raise ValidationError("forward transaction items must equal result minus base")
    if remove_set != removed_set:
        raise ValidationError("remove transaction items must equal the remove manifest")

    removed_by_name_arch = {}
    for nevra in removed:
        removed_by_name_arch.setdefault(nevra_name_arch(nevra), []).append(nevra)
    for item in forward_items:
        prior = removed_by_name_arch.get((item["name"], item["arch"]), [])
        if item["action"] == "upgrade" and len(prior) != 1:
            raise ValidationError("upgrade %s must replace exactly one base package" % item["nevra"])
        if item["action"] == "install" and prior:
            raise ValidationError("install %s unexpectedly replaces a base package" % item["nevra"])

    locked = {package["nevra"]: package for package in validated_lock["packages"]}
    if set(locked) != forward_set:
        raise ValidationError("rpm-lock packages do not exactly match forward transaction items")
    normalized = []
    for item in forward_items:
        package = locked[item["nevra"]]
        expected_header = {
            "name": item["name"], "epoch": item["epoch"],
            "version": item["version"], "release": item["release"],
            "arch": item["arch"], "nevra": item["nevra"],
            "source_rpm": item["source_rpm"],
        }
        if package["header"] != expected_header:
            raise ValidationError("verified header differs from transaction for %s" % item["nevra"])
        if package["received_sha256"] != item["repository_checksum"]["value"]:
            raise ValidationError("received bytes differ from repository checksum for %s" % item["nevra"])
        fingerprint = package["signature"]["fingerprint"]
        if fingerprint != repositories[item["repo_id"]][1]:
            raise ValidationError("package signature differs from repository trust for %s" % item["nevra"])
        normalized.append({
            "item": item,
            "lock": package,
            "filename": PurePosixPath(item["location"]).name,
            "fingerprint": fingerprint,
        })
    key_identities = {repositories[package["item"]["repo_id"]] for package in normalized}
    if len(key_identities) != 1:
        raise ValidationError("one --key cannot satisfy multiple package trust roots")
    key_sha256, fingerprint = next(iter(key_identities))
    filenames = [package["filename"] for package in normalized]
    if len(filenames) != len(set(filenames)):
        raise ValidationError("transaction contains duplicate RPM filenames")
    return {
        "role": role,
        "base": base,
        "result": result,
        "packages": normalized,
        "key_sha256": key_sha256,
        "fingerprint": fingerprint,
    }


def scan_bundle(bundle):
    reject_symlink_components(bundle, "bundle")
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValidationError("bundle must be a regular, non-symlink directory: %s" % bundle)
    resolved = Path(os.path.realpath(str(bundle)))
    if str(resolved) == "/":
        raise ValidationError("refusing filesystem root as RPM bundle")
    files = {}
    for root, directories, filenames in os.walk(str(resolved), followlinks=False):
        for directory in directories:
            path = Path(root) / directory
            if path.is_symlink():
                raise ValidationError("bundle contains symlink directory: %s" % path)
        for filename in filenames:
            path = Path(root) / filename
            if not stat.S_ISREG(os.lstat(str(path)).st_mode):
                raise ValidationError("bundle contains non-regular file: %s" % path)
            files[path.relative_to(resolved).as_posix()] = path
    return resolved, files


def path_is_within(path, directory):
    path = os.path.realpath(str(path))
    directory = os.path.realpath(str(directory))
    return path == directory or path.startswith(directory.rstrip(os.sep) + os.sep)


def verify_bundle(
    normalized, bundle, transaction_path, transaction_reference, transaction_sha256
):
    bundle, actual = scan_bundle(bundle)
    expected = {package["filename"] for package in normalized["packages"]}
    reference_text = str(transaction_reference)
    if path_is_within(transaction_path, bundle):
        transaction_relative = Path(os.path.realpath(str(transaction_path))).relative_to(bundle).as_posix()
        if transaction_relative != reference_text:
            raise ValidationError("bundle transaction path differs from lock reference")
        expected.add(transaction_relative)
    elif reference_text in actual:
        bundled_transaction = load_json(actual[reference_text], "bundled transaction")
        if canonical_digest(bundled_transaction) != transaction_sha256:
            raise ValidationError("bundled transaction digest differs from rpm-lock")
        expected.add(reference_text)
    missing = sorted(expected - set(actual))
    extra = sorted(set(actual) - expected)
    if missing:
        raise ValidationError("bundle is missing files: %s" % ", ".join(missing))
    if extra:
        raise ValidationError("bundle contains unlocked files: %s" % ", ".join(extra))
    for package in normalized["packages"]:
        path = actual[package["filename"]]
        if path.stat().st_size != package["item"]["size"]:
            raise ValidationError("size mismatch for %s" % path.name)
        if file_sha256(path) != package["lock"]["received_sha256"]:
            raise ValidationError("SHA256 mismatch for %s" % path.name)
        package["path"] = path
    return bundle


def run_command(arguments, label):
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    process = subprocess.run(
        [str(argument) for argument in arguments], env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
    )
    if process.returncode != 0:
        detail = (process.stdout + process.stderr).strip()
        raise ValidationError("%s failed: %s" % (label, detail))
    return process.stdout, process.stderr


def verify_key_and_headers(normalized, key):
    require_regular_file(key, "RPM signing key")
    if file_sha256(key) != normalized["key_sha256"]:
        raise ValidationError("RPM signing key SHA256 differs from transaction")
    temporary = Path(tempfile.mkdtemp(prefix="crossforge-host-rpmdb.", dir="/tmp"))
    try:
        run_command(["rpm", "--dbpath", temporary, "--initdb"], "temporary RPM database init")
        run_command(["rpm", "--dbpath", temporary, "--import", key], "locked RPM key import")
        fingerprint = normalized["fingerprint"]
        for package in normalized["packages"]:
            path = package["path"]
            stdout, stderr = run_command(
                ["rpmkeys", "--dbpath", temporary, "--checksig", "--verbose", path],
                "signature verification for %s" % path.name,
            )
            output = (stdout + stderr).lower()
            signature_lines = [line for line in output.splitlines() if "signature" in line]
            if (
                not signature_lines
                or (fingerprint not in output and fingerprint[-8:] not in output)
                or any(not line.rstrip().endswith(": ok") for line in signature_lines)
            ):
                raise ValidationError("invalid locked signature on %s" % path.name)
            query = (
                "%{NAME}\\t%{EPOCHNUM}\\t%{VERSION}\\t%{RELEASE}\\t"
                "%{ARCH}\\t%{SOURCERPM}\\t%{SIZE}\\n"
            )
            stdout, _stderr = run_command(
                ["rpm", "--dbpath", temporary, "-qp", "--qf", query, path],
                "header query for %s" % path.name,
            )
            lines = stdout.rstrip("\n").splitlines()
            if len(lines) != 1:
                raise ValidationError("unexpected RPM header output for %s" % path.name)
            header = package["lock"]["header"]
            expected = [
                header["name"], str(header["epoch"]), header["version"],
                header["release"], header["arch"], header["source_rpm"],
                str(package["item"]["install_size"]),
            ]
            if lines[0].split("\t") != expected:
                raise ValidationError("RPM header differs from lock for %s" % path.name)
    finally:
        shutil.rmtree(str(temporary))


def rpm_inventory():
    query = "%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\\n"
    stdout, _stderr = run_command(["rpm", "-qa", "--qf", query], "RPM inventory query")
    values = sorted(line for line in stdout.splitlines() if line)
    if len(values) != len(set(values)):
        raise ValidationError("current RPM inventory contains duplicate NEVRAs")
    for value in values:
        if not INVENTORY_NEVRA.match(value) or any(character.isspace() for character in value):
            raise ValidationError("current RPM inventory contains invalid NEVRA: %r" % value)
    return values


def require_inventory(actual, expected, label):
    if actual == expected:
        return
    actual_set = set(actual)
    expected_set = set(expected)
    missing = sorted(expected_set - actual_set)
    extra = sorted(actual_set - expected_set)
    details = []
    if missing:
        details.append("missing=" + ",".join(missing[:10]))
    if extra:
        details.append("extra=" + ",".join(extra[:10]))
    raise ValidationError("%s differs from lock (%s)" % (label, "; ".join(details)))


def validate_marker_path(marker, bundle):
    marker = Path(os.path.abspath(str(marker)))
    if str(marker) == "/" or os.path.lexists(str(marker)):
        raise ValidationError("marker already exists or is unsafe: %s" % marker)
    if path_is_within(marker, bundle):
        raise ValidationError("marker must not be written inside the RPM bundle")
    current = marker.parent
    while True:
        if os.path.lexists(str(current)) and current.is_symlink():
            raise ValidationError("marker parent contains a symlink: %s" % current)
        if current.parent == current:
            break
        current = current.parent
    marker.parent.mkdir(parents=True, exist_ok=True)
    if marker.parent.is_symlink() or not marker.parent.is_dir():
        raise ValidationError("marker parent is unsafe: %s" % marker.parent)
    return marker


def write_marker(marker, value):
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % marker.name, suffix=".tmp", dir=str(marker.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(str(temporary), 0o644)
        os.replace(str(temporary), str(marker))
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def install(normalized, lock, transaction, marker):
    require_inventory(rpm_inventory(), normalized["base"], "current RPM inventory")
    rpm_paths = [package["path"] for package in normalized["packages"]]
    run_command(["rpm", "--test", "-U"] + rpm_paths, "locked RPM transaction test")
    require_inventory(rpm_inventory(), normalized["base"], "RPM inventory after --test")
    stdout, stderr = run_command(["rpm", "-U"] + rpm_paths, "locked RPM transaction")
    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)
    result = rpm_inventory()
    require_inventory(result, normalized["result"], "installed RPM inventory")
    result_digest = canonical_digest(result)
    marker_value = {
        "schema_version": 1,
        "kind": "host-rpm-install-marker",
        "role": normalized["role"],
        "lock_sha256": canonical_digest(lock),
        "transaction_sha256": canonical_digest(transaction),
        "result_sha256": result_digest,
        "result_item_count": len(result),
    }
    write_marker(marker, marker_value)
    print(
        "installed %d locked host RPMs for %s (result sha256:%s)"
        % (len(normalized["packages"]), normalized["role"], result_digest)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lock", type=Path)
    parser.add_argument("--transaction", type=Path)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--marker", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        lock = load_json(arguments.lock, "lock")
        validate_schema(lock, "rpm-lock.schema.json", "rpm-lock")
        validated_lock = validate_lock(lock)
        transaction_path, transaction = load_transaction(
            validated_lock, arguments.lock, arguments.bundle, arguments.transaction
        )
        validate_schema(transaction, "rpm-transaction.schema.json", "rpm-transaction")
        normalized = normalize(validated_lock, transaction)
        bundle = verify_bundle(
            normalized, arguments.bundle, transaction_path,
            validated_lock["transaction_file"],
            validated_lock["transaction_sha256"],
        )
        marker = validate_marker_path(arguments.marker, bundle)
        verify_key_and_headers(normalized, arguments.key)
        install(normalized, lock, transaction, marker)
    except (OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
