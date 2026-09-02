#!/usr/bin/env python3
"""Assemble a locked, qualification-only CPython runtime overlay offline.

The input bundle is the complete bundle for a release-bound target-sysroot
lock.  It is deliberately verified in full even though only the seven CPython
runtime library RPMs are installed.  Evidence has exactly six root fields:
``schema_version``, ``kind``, ``qualification_only``, ``identity``,
``identity_sha256``, and ``runtime_inventory``.  ``identity_sha256`` is the
canonical JSON SHA256 of the ``identity`` object.
"""

import argparse
import json
import os
import re
import runpy
import shlex
import sys
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
MATERIALIZER = runpy.run_path(str(REPOSITORY / "scripts/materialize-sysroot.py"))
ValidationError = MATERIALIZER["ValidationError"]
canonical_sha256 = MATERIALIZER["canonical_sha256"]
load_json = MATERIALIZER["load_json"]
path_is_within = MATERIALIZER["path_is_within"]
reject_symlink_components = MATERIALIZER["reject_symlink_components"]
sha256_file = MATERIALIZER["sha256_file"]

RUNTIME_PACKAGE_NAMES = (
    "bzip2-libs",
    "libffi",
    "libuuid",
    "openssl-libs",
    "sqlite-libs",
    "xz-libs",
    "zlib",
)
ARCH_TO_OCI = {"x86_64": "amd64", "aarch64": "arm64"}
MACHINE_ALIASES = {"amd64": "x86_64", "arm64": "aarch64"}
OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")

EVIDENCE_KEYS = {
    "schema_version",
    "kind",
    "qualification_only",
    "identity",
    "identity_sha256",
    "runtime_inventory",
}
IDENTITY_KEYS = {
    "base_image",
    "release_sha256",
    "target",
    "sysroot",
    "selected_packages",
    "selected_packages_sha256",
}
BASE_IMAGE_KEYS = {"index_digest", "manifest_digest"}
TARGET_KEYS = {"arch", "triple"}
SYSROOT_KEYS = {"lock_sha256", "transaction_sha256"}
PACKAGE_KEYS = {"name", "nevra", "received_sha256"}
INVENTORY_KEYS = {
    "before_sha256",
    "before_item_count",
    "after_sha256",
    "after_item_count",
    "installed_nevras",
    "os_release_sha256",
}


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def require_exact_keys(value, keys, label):
    require(isinstance(value, dict), "%s must be an object" % label)
    actual = set(value)
    require(
        actual == keys,
        "%s has unexpected fields (missing=%s; extra=%s)"
        % (
            label,
            ",".join(sorted(keys - actual)),
            ",".join(sorted(actual - keys)),
        ),
    )


def normalized_machine():
    machine = os.uname().machine
    return MACHINE_ALIASES.get(machine, machine)


def require_runtime_root(path):
    reject_symlink_components(path, "runtime root")
    absolute = Path(os.path.abspath(str(path)))
    require(str(absolute) != "/", "refusing filesystem root as runtime root")
    require(
        absolute.is_dir() and not absolute.is_symlink(),
        "runtime root must be an existing, non-symlink directory: %s" % absolute,
    )
    resolved = Path(os.path.realpath(str(absolute)))
    require(any(resolved.iterdir()), "runtime root must not be empty")
    return resolved


def contained_existing_path(root, relative, label, kind):
    path = root / relative
    require(os.path.lexists(str(path)), "%s is missing: %s" % (label, path))
    resolved = Path(os.path.realpath(str(path)))
    require(path_is_within(resolved, root), "%s escapes the runtime root" % label)
    if kind == "file":
        require(resolved.is_file(), "%s is not a regular file" % label)
    else:
        require(resolved.is_dir(), "%s is not a directory" % label)
    return resolved


def parse_os_release(path, expected_version):
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ValidationError("cannot read Rocky os-release: %s" % error)
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, raw = line.partition("=")
        require(separator and key and key not in values, "invalid os-release entry")
        try:
            parsed = shlex.split(raw, posix=True)
        except ValueError as error:
            raise ValidationError("invalid os-release value for %s: %s" % (key, error))
        require(len(parsed) == 1, "invalid os-release value for %s" % key)
        values[key] = parsed[0]
    require(values.get("ID") == "rocky", "runtime root is not Rocky Linux")
    require(
        values.get("VERSION_ID") == expected_version,
        "runtime Rocky version differs from target-sysroot release",
    )
    return values


def validate_runtime_root(root, expected_version):
    os_release = contained_existing_path(
        root, "usr/lib/os-release", "Rocky os-release", "file"
    )
    parse_os_release(os_release, expected_version)
    rpmdb_candidates = ("var/lib/rpm", "usr/lib/sysimage/rpm")
    rpmdb = None
    for relative in rpmdb_candidates:
        candidate = root / relative
        if os.path.lexists(str(candidate)):
            rpmdb = contained_existing_path(
                root, relative, "runtime RPM database", "dir"
            )
            break
    require(rpmdb is not None, "runtime root does not contain an RPM database")
    require(any(rpmdb.iterdir()), "runtime RPM database is empty")
    return sha256_file(os_release)


def rpm_inventory(root):
    query = "%{NAME}\\t%{ARCH}\\t%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\\n"
    stdout, _stderr = MATERIALIZER["command"](
        ["rpm", "--root", root, "-qa", "--qf", query],
        "runtime RPM inventory query",
    )
    rows = []
    for line in stdout.splitlines():
        fields = line.split("\t")
        require(len(fields) == 3 and all(fields), "invalid runtime RPM inventory row")
        name, arch, nevra = fields
        require(
            not any(character.isspace() for character in nevra),
            "runtime RPM inventory contains whitespace",
        )
        rows.append((name, arch, nevra))
    rows.sort(key=lambda row: row[2])
    nevras = [row[2] for row in rows]
    require(rows, "runtime RPM inventory is empty")
    require(
        len(nevras) == len(set(nevras)),
        "runtime RPM inventory has duplicate NEVRAs",
    )
    return rows


def validate_inventory_arch(rows, target_arch):
    payload_arches = {
        arch for _name, arch, _nevra in rows if arch not in ("noarch", "(none)")
    }
    require(
        payload_arches == {target_arch},
        "runtime RPM inventory architecture differs from target (found=%s)"
        % ",".join(sorted(payload_arches)),
    )


def select_runtime_packages(context, verified):
    require(
        context["role"] == "target-sysroot",
        "runtime overlay requires a target-sysroot lock",
    )
    by_name = {}
    for package in verified:
        name = package["item"]["name"]
        if name in RUNTIME_PACKAGE_NAMES:
            require(
                name not in by_name,
                "target-sysroot lock contains duplicate %s" % name,
            )
            require(
                package["item"]["arch"] == context["arch"],
                "%s is not locked for target architecture" % name,
            )
            by_name[name] = package
    missing = sorted(set(RUNTIME_PACKAGE_NAMES) - set(by_name))
    require(
        not missing,
        "target-sysroot lock is missing runtime RPMs: %s" % ", ".join(missing),
    )
    return [by_name[name] for name in RUNTIME_PACKAGE_NAMES]


def installation_arguments(root, target_arch, paths, test):
    arguments = [
        "rpm",
        "--root",
        root,
        "-U",
        "--replacepkgs",
        # This is a qualification overlay, not a deployable RPM transaction.
        # Rocky's minimal OCI root pins exact versions across the util-linux
        # family, while the independently refreshed sysroot can contain a
        # newer libuuid erratum. The seven payloads are fully signature/header
        # verified and runtime-tested below; dependency solving remains the
        # downstream distribution's responsibility.
        "--nodeps",
        "--noscripts",
        "--notriggers",
        "--excludedocs",
        "--nocaps",
        "--nocontexts",
    ]
    if test:
        arguments.append("--test")
    if target_arch != normalized_machine():
        arguments.append("--ignorearch")
    arguments.extend(paths)
    return arguments


def verify_inventory_transition(before, after, selected):
    selected_names = set(RUNTIME_PACKAGE_NAMES)
    before_other = [row for row in before if row[0] not in selected_names]
    after_other = [row for row in after if row[0] not in selected_names]
    require(
        before_other == after_other,
        "RPMs outside the qualification runtime set changed during installation",
    )
    expected = sorted(
        (package["item"]["name"], package["item"]["arch"], package["item"]["nevra"])
        for package in selected
    )
    actual = sorted(row for row in after if row[0] in selected_names)
    if actual != expected:
        expected_nevras = {row[2] for row in expected}
        actual_nevras = {row[2] for row in actual}
        raise ValidationError(
            "installed runtime NEVRAs differ from lock (missing=%s; extra=%s)"
            % (
                ",".join(sorted(expected_nevras - actual_nevras)),
                ",".join(sorted(actual_nevras - expected_nevras)),
            )
        )
    return sorted(row[2] for row in actual)


def build_evidence(
    context, release, manifest_digest, selected, before, after, os_release_sha256
):
    selected_packages = [
        {
            "name": package["item"]["name"],
            "nevra": package["item"]["nevra"],
            "received_sha256": package["lock"]["received_sha256"],
        }
        for package in selected
    ]
    installed_nevras = verify_inventory_transition(before, after, selected)
    identity = {
        "base_image": {
            "index_digest": release["base_image"]["digest"],
            "manifest_digest": manifest_digest,
        },
        "release_sha256": canonical_sha256(release),
        "target": {
            "arch": context["arch"],
            "triple": context["transaction"]["identity"]["target_triple"],
        },
        "sysroot": {
            "lock_sha256": canonical_sha256(context["lock"]),
            "transaction_sha256": canonical_sha256(context["transaction"]),
        },
        "selected_packages": selected_packages,
        "selected_packages_sha256": canonical_sha256(selected_packages),
    }
    before_nevras = [row[2] for row in before]
    after_nevras = [row[2] for row in after]
    return {
        "schema_version": 1,
        "kind": "crossforge-python-runtime-overlay",
        "qualification_only": True,
        "identity": identity,
        "identity_sha256": canonical_sha256(identity),
        "runtime_inventory": {
            "before_sha256": canonical_sha256(before_nevras),
            "before_item_count": len(before_nevras),
            "after_sha256": canonical_sha256(after_nevras),
            "after_item_count": len(after_nevras),
            "installed_nevras": installed_nevras,
            "os_release_sha256": os_release_sha256,
        },
    }


def validate_evidence(evidence):
    require_exact_keys(evidence, EVIDENCE_KEYS, "evidence")
    require(evidence["schema_version"] == 1, "unsupported evidence schema")
    require(
        evidence["kind"] == "crossforge-python-runtime-overlay",
        "invalid evidence kind",
    )
    require(
        evidence["qualification_only"] is True,
        "runtime overlay is not qualification-only",
    )
    identity = evidence["identity"]
    require_exact_keys(identity, IDENTITY_KEYS, "evidence.identity")
    require_exact_keys(
        identity["base_image"], BASE_IMAGE_KEYS, "evidence.identity.base_image"
    )
    require_exact_keys(identity["target"], TARGET_KEYS, "evidence.identity.target")
    require_exact_keys(identity["sysroot"], SYSROOT_KEYS, "evidence.identity.sysroot")
    for digest in identity["base_image"].values():
        require(
            isinstance(digest, str) and OCI_DIGEST.fullmatch(digest),
            "invalid OCI digest",
        )
    require(
        isinstance(identity["release_sha256"], str)
        and HEX_SHA256.fullmatch(identity["release_sha256"]),
        "invalid release SHA256",
    )
    require(identity["target"]["arch"] in ARCH_TO_OCI, "invalid target architecture")
    require(
        isinstance(identity["target"]["triple"], str)
        and identity["target"]["triple"],
        "invalid target triple",
    )
    for digest in identity["sysroot"].values():
        require(
            isinstance(digest, str) and HEX_SHA256.fullmatch(digest),
            "invalid sysroot SHA256",
        )
    packages = identity["selected_packages"]
    require(
        isinstance(packages, list) and len(packages) == len(RUNTIME_PACKAGE_NAMES),
        "invalid selected package count",
    )
    names = []
    for index, package in enumerate(packages):
        require_exact_keys(
            package,
            PACKAGE_KEYS,
            "evidence.identity.selected_packages[%d]" % index,
        )
        require(
            all(
                isinstance(package[key], str) and package[key]
                for key in PACKAGE_KEYS
            ),
            "invalid selected package field",
        )
        require(
            HEX_SHA256.fullmatch(package["received_sha256"]),
            "invalid selected RPM SHA256",
        )
        names.append(package["name"])
    require(
        names == list(RUNTIME_PACKAGE_NAMES),
        "selected runtime package order or names differ",
    )
    require(
        identity["selected_packages_sha256"] == canonical_sha256(packages),
        "selected package canonical SHA256 differs",
    )
    require(
        evidence["identity_sha256"] == canonical_sha256(identity),
        "runtime overlay identity SHA256 differs",
    )
    inventory = evidence["runtime_inventory"]
    require_exact_keys(inventory, INVENTORY_KEYS, "evidence.runtime_inventory")
    for key in ("before_sha256", "after_sha256", "os_release_sha256"):
        require(
            isinstance(inventory[key], str) and HEX_SHA256.fullmatch(inventory[key]),
            "invalid %s" % key,
        )
    for key in ("before_item_count", "after_item_count"):
        require(
            type(inventory[key]) is int and inventory[key] > 0,
            "invalid %s" % key,
        )
    installed = inventory["installed_nevras"]
    require(
        isinstance(installed, list) and installed == sorted(installed),
        "installed NEVRAs must be sorted",
    )
    require(
        installed == sorted(package["nevra"] for package in packages),
        "installed NEVRAs differ from selected packages",
    )


def safe_evidence_path(path, bundle, runtime_root):
    absolute = Path(os.path.abspath(str(path)))
    reject_symlink_components(absolute, "runtime overlay evidence")
    require(str(absolute) != "/", "invalid runtime overlay evidence path")
    require(not absolute.is_dir(), "runtime overlay evidence path is a directory")
    require(
        not path_is_within(absolute, bundle),
        "evidence must not modify the RPM bundle",
    )
    absolute.parent.mkdir(parents=True, exist_ok=True)
    reject_symlink_components(absolute.parent, "runtime overlay evidence parent")
    require(absolute.parent.is_dir(), "runtime overlay evidence parent is not a directory")
    if path_is_within(absolute, runtime_root):
        require(path_is_within(absolute.parent, runtime_root), "evidence escapes runtime root")
    return absolute


def write_evidence(path, evidence):
    validate_evidence(evidence)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(evidence, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(str(temporary), 0o644)
        os.replace(str(temporary), str(path))
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def assemble(lock_path, bundle, key, runtime_root, manifest_digest, evidence_path):
    require(OCI_DIGEST.fullmatch(manifest_digest), "invalid base image manifest digest")
    context = MATERIALIZER["load_lock"](lock_path)
    require(
        context["role"] == "target-sysroot",
        "runtime overlay requires a target-sysroot lock",
    )
    release = load_json(REPOSITORY / "config/release.json")
    oci_arch = ARCH_TO_OCI.get(context["arch"])
    require(oci_arch is not None, "unsupported runtime target architecture")
    expected_manifest = release["base_image"]["manifests"][oci_arch]
    require(
        manifest_digest == expected_manifest,
        "base image manifest digest differs from release",
    )

    runtime_root = require_runtime_root(runtime_root)
    os_release_sha256 = validate_runtime_root(
        runtime_root, context["transaction"]["identity"]["release"]
    )
    MATERIALIZER["verify_bundle"](context, bundle)
    verified = MATERIALIZER["verify_key_and_headers"](context, bundle, key)
    selected = select_runtime_packages(context, verified)
    bundle = Path(os.path.realpath(str(bundle)))
    require(
        not path_is_within(runtime_root, bundle),
        "runtime root must not be inside the RPM bundle",
    )
    require(
        not path_is_within(bundle, runtime_root),
        "RPM bundle must not be inside the runtime root",
    )
    evidence_path = safe_evidence_path(evidence_path, bundle, runtime_root)

    before = rpm_inventory(runtime_root)
    validate_inventory_arch(before, context["arch"])
    paths = [bundle / MATERIALIZER["package_filename"](package) for package in selected]
    MATERIALIZER["command"](
        installation_arguments(runtime_root, context["arch"], paths, test=True),
        "qualification runtime RPM transaction test",
    )
    require(
        rpm_inventory(runtime_root) == before,
        "RPM inventory changed during transaction test",
    )
    MATERIALIZER["command"](
        installation_arguments(runtime_root, context["arch"], paths, test=False),
        "qualification runtime RPM transaction",
    )
    after = rpm_inventory(runtime_root)
    validate_inventory_arch(after, context["arch"])
    require(
        sha256_file(
            contained_existing_path(
                runtime_root, "usr/lib/os-release", "Rocky os-release", "file"
            )
        )
        == os_release_sha256,
        "Rocky os-release changed during runtime overlay installation",
    )
    evidence = build_evidence(
        context, release, manifest_digest, selected, before, after, os_release_sha256
    )
    write_evidence(evidence_path, evidence)
    print(
        "installed %d locked runtime RPMs into %s (identity sha256:%s)"
        % (len(selected), runtime_root, evidence["identity_sha256"])
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lock", type=Path)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--base-image-manifest-digest", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        assemble(
            arguments.lock,
            arguments.bundle,
            arguments.key,
            arguments.runtime_root,
            arguments.base_image_manifest_digest,
            arguments.evidence,
        )
    except (OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
