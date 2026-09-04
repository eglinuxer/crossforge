#!/usr/bin/env python3
"""Derive domain-neutral release component identities from release.json.

The human-maintained release document remains the only version source.  The
files produced here are deterministic projections for BuildKit cache and
qualification boundaries.  A component projection deliberately does not carry
the global release digest; the upward-only release binding records that digest
and the digest of every component instead.  Shared implementation policy which
is intentionally outside release.json is projected under ``/@implementation``
and is bound by the same component graph.

The complete CLI injects domain extensions. Qualification stages deliberately
load this stable core without vcpkg policy so an unrelated vcpkg change cannot
invalidate Python or toolchain evidence layers.
"""

import argparse
import copy
import hashlib
import json
import os
import runpy
import sys
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
COMPONENT_SCHEMA_ID = (
    "https://crossforge.dev/schemas/release-component.schema.json"
)
BINDING_SCHEMA_ID = (
    "https://crossforge.dev/schemas/release-binding.schema.json"
)
COMPONENT_ROOT = Path("config/generated/components")
BINDING_PATH = Path("config/generated/release-binding.json")
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ReleaseValidationError = STRICT["ValidationError"]
ROW_CONTRACT = runpy.run_path(
    str(REPOSITORY / "scripts/python_row_contract.py")
)
IMPLEMENTED_ROWS = ROW_CONTRACT["IMPLEMENTED_ROWS"]
RECORD_FIELDS = ROW_CONTRACT["RECORD_FIELDS"]
POLICY_FIELD_SCOPES = {
    "minor": ("build", "qualification"),
    "row": ("build", "qualification"),
    "adapter": ("build", "qualification"),
    "gil_policy": ("qualification",),
    "sysconfig_isolation": ("build", "qualification"),
    "zstd": ("qualification",),
    "hash_algorithm": ("qualification",),
    "introduced_phase": ("qualification",),
}
BUILD_POLICY_FIELDS = tuple(
    field for field in RECORD_FIELDS if "build" in POLICY_FIELD_SCOPES[field]
)
QUALIFICATION_POLICY_FIELDS = tuple(RECORD_FIELDS)
COMPONENT_KEYS = {
    "$schema",
    "schema_version",
    "kind",
    "component",
    "scope",
    "dependencies",
    "materials",
}
COMPONENT_SCOPES = {"build", "qualification", "supply", "future"}
ZSTD_BUILD_POLICY = {
    "linkage": "static",
    "private": True,
    "position_independent_code": True,
    "multithread": True,
    "visibility": "hidden",
    "no_trace": True,
    "exclude_archive_symbols": True,
    "selected_license": "BSD-3-Clause",
}
CANDIDATE_MANIFEST_POLICY = {
    "schema_version": 1,
    "schema": "https://crossforge.dev/schemas/candidate.schema.json",
    "kind": "crossforge-candidate",
    "identity": "canonical-json-sha256",
    "source_commit": "full-lowercase-git-sha1",
    "image_identity": "oci-index-and-platform-manifest-digests",
    "tag_trust": "none-digest-only",
    "platform": "linux/amd64",
    "registry_resolution": "rehash-index-exact-one-linux-amd64-manifest",
}
if "COMPONENT_EXTENSIONS" not in globals():
    COMPONENT_EXTENSIONS = ()
PYTHON_QUALIFICATION_COMPONENTS = {
    "policy": "implementation/python-qualification-policy",
    "aggregate": "python/qualification",
}
TOOLCHAIN_QUALIFICATION_COMPONENTS = {
    "x86_64": "toolchain/x86_64-qualification",
    "aarch64": "toolchain/aarch64-qualification",
}
ABI_IDENTITY_PREFIXES = (
    ("abi", "provider_manifest"),
    ("abi", "targets", "x86_64", "baseline"),
    ("abi", "targets", "x86_64", "sysroot_inventory"),
    ("abi", "targets", "aarch64", "baseline"),
    ("abi", "targets", "aarch64", "sysroot_inventory"),
    ("abi", "python", "runtime_provider_policy"),
    ("abi", "python", "provider_catalogs", "x86_64"),
    ("abi", "python", "provider_catalogs", "aarch64"),
)


class ProjectionError(RuntimeError):
    pass


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def serialized(value):
    return json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"


def require(condition, message):
    if not condition:
        raise ProjectionError(message)


def validate_policy_registry(implemented_rows):
    require(
        len(POLICY_FIELD_SCOPES) == len(RECORD_FIELDS)
        and set(POLICY_FIELD_SCOPES) == set(RECORD_FIELDS),
        "Python policy field registry differs from RECORD_FIELDS",
    )
    require(
        QUALIFICATION_POLICY_FIELDS == tuple(RECORD_FIELDS),
        "qualification policy does not bind every RECORD_FIELDS field",
    )
    seen_rows = set()
    seen_minors = set()
    phases = []
    for record in implemented_rows:
        require(
            isinstance(record, dict) and set(record) == set(RECORD_FIELDS),
            "implemented Python policy record fields differ from RECORD_FIELDS",
        )
        require(record["row"] not in seen_rows, "implemented Python row is duplicated")
        require(
            record["minor"] not in seen_minors,
            "implemented Python minor is duplicated",
        )
        require(
            record["row"] == "cp" + record["minor"].replace(".", ""),
            "implemented Python row/minor mismatch",
        )
        require(
            record["adapter"] in ("legacy", "transition", "modern"),
            "implemented Python adapter is invalid",
        )
        require(
            record["gil_policy"] in ("absent", "zero"),
            "implemented Python GIL policy is invalid",
        )
        require(
            record["sysconfig_isolation"] is True,
            "implemented Python sysconfig isolation is not enabled",
        )
        require(
            type(record["zstd"]) is bool,
            "implemented Python zstd policy is invalid",
        )
        require(
            record["hash_algorithm"] in ("siphash13", "siphash24"),
            "implemented Python hash algorithm is invalid",
        )
        require(
            type(record["introduced_phase"]) is int
            and record["introduced_phase"] > 0,
            "implemented Python phase is invalid",
        )
        seen_rows.add(record["row"])
        seen_minors.add(record["minor"])
        phases.append(record["introduced_phase"])
    require(phases == sorted(phases), "implemented Python phases are not ordered")


def json_pointer(path):
    return "/" + "/".join(
        str(part).replace("~", "~0").replace("/", "~1") for part in path
    )


def leaf_items(value, path=()):
    """Return scalar and empty-container leaves as ``(path, value)`` pairs."""
    if isinstance(value, dict) and value:
        result = []
        for key in sorted(value):
            result.extend(leaf_items(value[key], path + (key,)))
        return result
    if isinstance(value, list) and value:
        result = []
        for index, child in enumerate(value):
            result.extend(leaf_items(child, path + (index,)))
        return result
    return [(path, value)]


def is_under(path, prefix):
    return path[: len(prefix)] == prefix


def selector(*prefixes):
    def selected(path):
        return any(is_under(path, prefix) for prefix in prefixes)

    return selected


def materials_for(leaves, selected):
    records = [
        {"path": json_pointer(path), "value": copy.deepcopy(value)}
        for path, value in leaves
        if selected(path)
    ]
    records.sort(key=lambda record: record["path"])
    require(records, "component projection has no release materials")
    paths = [record["path"] for record in records]
    require(len(paths) == len(set(paths)), "component projection repeats a path")
    return records


def python_policy_materials(implemented_rows, fields):
    records = []
    for row in implemented_rows:
        for field in fields:
            records.append(
                {
                    "path": "/@implementation/python_rows/%s/%s"
                    % (row["row"], field),
                    "value": copy.deepcopy(row[field]),
                }
            )
    records.sort(key=lambda record: record["path"])
    return records


def zstd_policy_materials():
    return [
        {
            "path": "/@implementation/zstd/%s" % field,
            "value": copy.deepcopy(ZSTD_BUILD_POLICY[field]),
        }
        for field in sorted(ZSTD_BUILD_POLICY)
    ]


def policy_materials(prefix, policy):
    """Flatten a nested implementation policy into canonical material records."""
    require(prefix.startswith("/@implementation/"), "unsafe policy material prefix")
    records = []

    def visit(path, value):
        if isinstance(value, dict) and value:
            for key in sorted(value):
                visit(path + (key,), value[key])
            return
        if isinstance(value, list) and value:
            for index, item in enumerate(value):
                visit(path + (str(index),), item)
            return
        records.append(
            {
                "path": prefix + "/".join(path),
                "value": copy.deepcopy(value),
            }
        )

    visit((), policy)
    require(records, "implementation policy has no materials")
    return records


def component_path(component):
    parts = component.split("/")
    require(
        parts
        and all(
            part
            and part not in (".", "..")
            and all(
                character.islower()
                or character.isdigit()
                or character in "-_"
                for character in part
            )
            for part in parts
        ),
        "unsafe component name: %s" % component,
    )
    return COMPONENT_ROOT.joinpath(*parts).with_suffix(".json")


def python_row_name(entry):
    version = entry["version"]
    minor = version.rsplit(".", 1)[0]
    require(
        minor.startswith("3.")
        and minor[2:].isdigit()
        and version.count(".") == 2,
        "invalid CPython version in release projection: %s" % version,
    )
    return "cp" + minor.replace(".", "")


def host_lock_scope(release, role):
    """Promote the independent runtime component only after it is locked."""
    if role != "host-runtime":
        return "build"
    status = release["host_locks"][role]["status"]
    require(
        status in ("pending", "locked"),
        "host runtime lock status is invalid",
    )
    return "build" if status == "locked" else "future"


def classify_release_leaves(release, implemented_rows=IMPLEMENTED_ROWS):
    """Assign every release leaf one primary semantic cache class.

    This deliberately enumerates the accepted leaf shapes instead of using a
    catch-all top-level projection.  Extending release.schema.json therefore
    also requires an explicit ownership decision here.
    """
    validate_policy_registry(implemented_rows)
    implemented_row_names = {record["row"] for record in implemented_rows}
    classifications = {}
    source_fields = {
        "status",
        "repository_nevra",
        "header_arch",
        "url",
        "sha256",
        "size",
        "spec_sha256",
    }
    python_source_fields = {"status", "url", "sha256", "size"}
    sigstore_fields = {
        "verification",
        "bundle_url",
        "bundle_sha256",
        "bundle_size",
        "bundle_evidence",
        "identity",
        "oidc_issuer",
    }
    qemu_executor_fields = {
        "status",
        "repository",
        "tag",
        "index_digest",
        "index_evidence",
        "manifest_digest",
        "manifest_evidence",
        "binary_path",
        "binary_sha256",
        "cpu",
        "uname_release",
    }
    qemu_provenance_fields = {
        "attestation_manifest_digest",
        "attestation_evidence",
        "predicate_digest",
        "predicate_evidence",
        "predicate_size",
        "builder_repository",
        "builder_commit",
    }
    qemu_source_fields = {
        "repository",
        "tag",
        "tag_object",
        "tag_evidence",
        "commit",
        "commit_evidence",
    }
    abi_identity_leaves = {
        prefix + (field,)
        for prefix in ABI_IDENTITY_PREFIXES
        for field in ("file", "canonical_sha256")
    }

    for path, _value in leaf_items(release):
        category = None
        if path in (("$schema",), ("schema_version",)):
            category = "supply"
        elif path == ("baseline",):
            category = "build"
        elif path in abi_identity_leaves:
            category = "qualification"
        elif len(path) == 2 and path[0] == "product" and path[1] in {
            "name",
            "version",
        }:
            # Product identity must rebind the final SDK qualification without
            # recompiling any toolchain, Python, or dependency component.
            category = "qualification"
        elif len(path) == 2 and path[0] == "product" and path[1] in {
            "image_repository",
            "stable_channel",
        }:
            category = "supply"
        elif path in {
            ("base_image", "repository"),
            ("base_image", "tag"),
            ("base_image", "digest"),
            ("base_image", "manifests", "amd64"),
            ("base_image", "manifests", "arm64"),
        }:
            category = "build"
        elif path == ("base_image", "index_evidence"):
            category = "supply"
        elif (
            len(path) == 3
            and path[:2] == ("trust", "rocky_rpm_key")
            and path[2] in {"file", "sha256", "fingerprint"}
        ):
            category = "build"
        elif len(path) == 2 and path[0] == "platforms" and path[1] in {
            "build",
            "tool_host",
            "image",
        }:
            category = "build"
        elif (
            len(path) == 3
            and path[0] == "host_locks"
            and path[1]
            in {
                "host-build-common",
                "host-gcc-build",
                "host-gcc-test",
                "host-python-build",
                "host-runtime",
            }
            and path[2] in {"status", "lock_file", "canonical_sha256"}
        ):
            category = host_lock_scope(release, path[1])
        elif path in (("gts", "major"), ("gts", "gcc_version")):
            category = "build"
        elif (
            len(path) == 3
            and path[:2] == ("gts", "source")
            and path[2] in source_fields
        ):
            category = "build"
        elif path and path[0] == "gcc_testsuite":
            category = "qualification"
        elif path == ("binutils", "version"):
            category = "build"
        elif (
            len(path) == 3
            and path[:2] == ("binutils", "source")
            and path[2] in source_fields
        ):
            category = "build"
        elif path == ("qemu", "version"):
            category = "qualification"
        elif (
            len(path) == 3
            and path[:2] == ("qemu", "executor")
            and path[2] in qemu_executor_fields
        ):
            category = (
                "supply" if path[2].endswith("_evidence") else "qualification"
            )
        elif (
            len(path) == 4
            and path[:3] == ("qemu", "executor", "provenance")
            and path[3] in qemu_provenance_fields
        ):
            category = "supply"
        elif (
            len(path) == 4
            and path[:3] == ("qemu", "executor", "source")
            and path[3] in qemu_source_fields
        ):
            category = (
                "supply" if path[3].endswith("_evidence") else "qualification"
            )
        elif (
            len(path) in (3, 4)
            and path[0] == "targets"
            and isinstance(path[1], int)
            and 0 <= path[1] < len(release["targets"])
        ):
            if len(path) == 3 and path[2] in {"arch", "triple"}:
                category = "build"
            elif len(path) == 4 and path[2] == "sysroot" and path[3] in {
                "status",
                "lock_file",
                "canonical_sha256",
            }:
                category = "build"
        elif (
            len(path) >= 4
            and path[:2] == ("python", "versions")
            and isinstance(path[2], int)
            and 0 <= path[2] < len(release["python"]["versions"])
        ):
            entry = release["python"]["versions"][path[2]]
            row = python_row_name(entry)
            relative = path[3:]
            if row not in implemented_row_names:
                category = "future"
            elif relative in (("version",), ("adapter",)):
                category = "build"
            elif relative == ("support",):
                category = "qualification"
            elif relative == ("patches",):
                category = "build"
            elif (
                len(relative) == 3
                and relative[0] == "patches"
                and isinstance(relative[1], int)
                and relative[2] in {"file", "sha256"}
            ):
                category = "build"
            elif (
                len(relative) == 2
                and relative[0] == "source"
                and relative[1] in python_source_fields
            ):
                category = "build"
            elif (
                len(relative) == 3
                and relative[:2] == ("source", "sigstore")
                and relative[2] in sigstore_fields
            ):
                category = "supply"
        elif len(path) >= 3 and path[:2] == ("python", "zstd"):
            # The source preparation boundary authenticates archive, detached
            # signature, Git provenance, key and selected license together.
            # Any change must therefore invalidate the one source identity.
            category = "build"
        elif len(path) >= 2 and path[0] in {"host_tools", "vcpkg"}:
            category = "build"
        elif len(path) >= 2 and path[0] == "nfpm":
            # The locked binary, its checksum manifest, archived Sigstore
            # bundle and selected license are all revalidated together before
            # nFPM enters the SDK.  Treat the complete identity as one build
            # input so no authentication-policy change can reuse that layer.
            category = "build"

        require(
            category is not None,
            "release leaf has no explicit semantic classification: %s"
            % json_pointer(path),
        )
        classifications[json_pointer(path)] = category
    return classifications


def classification_selector(classifications, category, excluded_prefixes=()):
    def selected(path):
        pointer = json_pointer(path)
        return (
            classifications.get(pointer) == category
            and not any(is_under(path, prefix) for prefix in excluded_prefixes)
        )

    return selected


def validate_release(repository, release, schema_path=None):
    if schema_path is None:
        schema_path = repository / "config/schemas/release.schema.json"
    schema = STRICT["load_json"](schema_path)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](release, schema, schema, "$")


def _render_expected_components(release, implemented_rows):
    """Purely derive the canonical component graph from its two authorities."""
    validate_policy_registry(implemented_rows)
    leaves = leaf_items(release)
    classifications = classify_release_leaves(release, implemented_rows)
    documents = {}
    digests = {}

    def add(
        component,
        scope,
        selected=None,
        dependencies=(),
        explicit_materials=None,
    ):
        require(component not in documents, "duplicate component: %s" % component)
        require(
            (selected is None) != (explicit_materials is None),
            "%s must select release or implementation materials" % component,
        )
        dependency_records = []
        for dependency in dependencies:
            require(
                dependency in documents,
                "%s depends on unknown or later component %s"
                % (component, dependency),
            )
            dependency_records.append(
                {
                    "component": dependency,
                    "canonical_sha256": digests[dependency],
                }
            )
        dependency_records.sort(key=lambda record: record["component"])
        document = {
            "$schema": COMPONENT_SCHEMA_ID,
            "schema_version": 1,
            "kind": "crossforge-release-component",
            "component": component,
            "scope": scope,
            "dependencies": dependency_records,
            "materials": (
                materials_for(leaves, selected)
                if selected is not None
                else copy.deepcopy(explicit_materials)
            ),
        }
        documents[component] = document
        digests[component] = canonical_sha256(document)

    rpm_common = (
        ("baseline",),
        ("base_image", "repository"),
        ("base_image", "tag"),
        ("base_image", "digest"),
        ("base_image", "manifests"),
        ("trust",),
        ("platforms",),
    )
    for role in (
        "host-build-common",
        "host-gcc-build",
        "host-gcc-test",
        "host-python-build",
        "host-runtime",
    ):
        add(
            "rpm/%s" % role,
            host_lock_scope(release, role),
            selector(*(rpm_common + (("host_locks", role),))),
        )

    targets = release["targets"]
    target_indices = {target["arch"]: index for index, target in enumerate(targets)}
    require(
        set(target_indices) == {"x86_64", "aarch64"}
        and len(target_indices) == len(targets),
        "release target matrix differs from the component contract",
    )
    target_components = {}
    for index, target in enumerate(targets):
        arch = target["arch"]
        component = "rpm/sysroot-%s" % arch
        target_components[arch] = component
        add(
            component,
            "build",
            selector(*(rpm_common + (("targets", index),))),
        )

    abi_baseline_components = {}
    for arch in ("x86_64", "aarch64"):
        component = "abi/%s-baseline" % arch
        abi_baseline_components[arch] = component
        add(
            component,
            "qualification",
            selector(("abi", "targets", arch, "baseline")),
        )
    add(
        "abi/python-providers",
        "qualification",
        selector(
            ("abi", "provider_manifest"),
            ("abi", "targets", "x86_64", "sysroot_inventory"),
            ("abi", "targets", "aarch64", "sysroot_inventory"),
            ("abi", "python"),
        ),
    )

    # SRPM preparation authenticates the Rocky signing key as well as the
    # downloaded bytes.  Trust rotation must therefore invalidate both source
    # preparation identities even when the SRPM URL and digest are unchanged.
    add("sources/gcc", "build", selector(("gts",), ("trust",)))
    add(
        "sources/binutils", "build", selector(("binutils",), ("trust",))
    )
    add("sources/zstd", "build", selector(("python", "zstd")))
    add("sources/vcpkg", "build", selector(("vcpkg",)))
    add("sources/nfpm", "build", selector(("nfpm",)))
    for tool in sorted(release["host_tools"]):
        add(
            "sources/%s" % tool,
            "build",
            selector(("host_tools", tool)),
        )

    toolchain_builds = {}
    toolchain_qualifications = {}
    for index, target in enumerate(targets):
        arch = target["arch"]
        build_component = "toolchain/%s-build" % arch
        toolchain_builds[arch] = build_component
        add(
            build_component,
            "build",
            selector(("baseline",), ("platforms",), ("targets", index)),
            (
                "rpm/host-build-common",
                "rpm/host-gcc-build",
                target_components[arch],
                "sources/gcc",
                "sources/binutils",
            ),
        )

        qualification_component = "toolchain/%s-qualification" % arch
        toolchain_qualifications[arch] = qualification_component
        qualification_prefixes = [
            ("baseline",),
            ("base_image",),
            ("targets", index),
        ]
        if arch == "aarch64":
            qualification_prefixes.append(("qemu",))
        add(
            qualification_component,
            "qualification",
            selector(*qualification_prefixes),
            (build_component, abi_baseline_components[arch]),
        )

    add(
        "toolchain/gcc-testsuite-qualification",
        "qualification",
        selector(("gcc_testsuite",)),
        (
            "rpm/host-gcc-test",
            "sources/gcc",
            toolchain_qualifications["x86_64"],
            toolchain_qualifications["aarch64"],
        ),
    )

    build_policy_components = []
    for record in implemented_rows:
        component = "implementation/python-%s-build-policy" % record["row"]
        build_policy_components.append(component)
        add(
            component,
            "build",
            explicit_materials=python_policy_materials(
                (record,), BUILD_POLICY_FIELDS
            ),
        )
    add(
        "implementation/python-qualification-policy",
        "qualification",
        dependencies=tuple(build_policy_components),
        explicit_materials=python_policy_materials(
            implemented_rows, QUALIFICATION_POLICY_FIELDS
        ),
    )
    add(
        "implementation/zstd-build-policy",
        "build",
        explicit_materials=zstd_policy_materials(),
    )
    add(
        "zstd/host-build",
        "build",
        selector(("baseline",), ("platforms",)),
        (
            "rpm/host-build-common",
            "sources/zstd",
            "implementation/zstd-build-policy",
        ),
    )
    for arch in ("x86_64", "aarch64"):
        add(
            "zstd/%s-build" % arch,
            "build",
            selector(("targets", target_indices[arch])),
            (
                "rpm/host-build-common",
                toolchain_builds[arch],
                "sources/zstd",
                "implementation/zstd-build-policy",
            ),
        )
    release_entries = {}
    release_entry_indices = {}
    for index, entry in enumerate(release["python"]["versions"]):
        row = python_row_name(entry)
        require(row not in release_entries, "release Python row is duplicated: %s" % row)
        release_entries[row] = entry
        release_entry_indices[row] = index

    python_target_builds = []
    qualification_material_prefixes = [
        ("baseline",),
        ("base_image",),
        ("qemu",),
        ("targets",),
    ]
    implemented_names = {record["row"] for record in implemented_rows}
    for record in implemented_rows:
        row = record["row"]
        require(row in release_entries, "implemented Python row is absent: %s" % row)
        entry = release_entries[row]
        require(
            entry["adapter"] == record["adapter"],
            "release Python adapter differs from implementation: %s" % row,
        )
        require(
            entry["source"]["status"] == "locked",
            "implemented Python source is not locked: %s" % row,
        )
        entry_prefix = ("python", "versions", release_entry_indices[row])
        source_component = "python/%s-source" % row
        row_policy = "implementation/python-%s-build-policy" % row
        add(
            source_component,
            "build",
            selector(
                entry_prefix + ("version",),
                entry_prefix + ("adapter",),
                entry_prefix + ("patches",),
                entry_prefix + ("source", "status"),
                entry_prefix + ("source", "url"),
                entry_prefix + ("source", "sha256"),
                entry_prefix + ("source", "size"),
            ),
            (row_policy,),
        )
        native_component = "python/%s-native-build" % row
        native_dependencies = [
            "rpm/host-build-common",
            "rpm/host-python-build",
            source_component,
        ]
        if record["zstd"]:
            native_dependencies.append("zstd/host-build")
        add(
            native_component,
            "build",
            selector(("baseline",), ("platforms",)),
            tuple(native_dependencies),
        )
        for arch in ("x86_64", "aarch64"):
            target_component = "python/%s-%s-build" % (row, arch)
            python_target_builds.append(target_component)
            target_dependencies = [native_component, toolchain_builds[arch]]
            if record["zstd"]:
                target_dependencies.append("zstd/%s-build" % arch)
            add(
                target_component,
                "build",
                selector(("targets", target_indices[arch])),
                tuple(target_dependencies),
            )
        qualification_material_prefixes.extend(
            [
                entry_prefix + ("support",),
                entry_prefix + ("source", "sigstore"),
            ]
        )

    for row, entry in sorted(release_entries.items()):
        if row in implemented_names:
            continue
        add(
            "future/python-%s" % row,
            "future",
            selector(("python", "versions", release_entry_indices[row])),
        )

    python_qualification_dependencies = tuple(
        python_target_builds
        + [
            abi_baseline_components["x86_64"],
            abi_baseline_components["aarch64"],
            "abi/python-providers",
            toolchain_qualifications["x86_64"],
            toolchain_qualifications["aarch64"],
            "implementation/python-qualification-policy",
        ]
    )
    add(
        "python/qualification",
        "qualification",
        selector(*qualification_material_prefixes),
        python_qualification_dependencies,
    )

    add(
        "product/identity",
        "qualification",
        selector(("product", "name"), ("product", "version")),
    )

    add(
        "implementation/candidate-manifest",
        "supply",
        explicit_materials=policy_materials(
            "/@implementation/candidate-manifest/", CANDIDATE_MANIFEST_POLICY
        ),
    )

    for extension in COMPONENT_EXTENSIONS:
        require(callable(extension), "component extension must be callable")
        extension(
            {
                "add": add,
                "release": release,
                "require": require,
                "selector": selector,
                "toolchain_builds": copy.deepcopy(toolchain_builds),
                "toolchain_qualifications": copy.deepcopy(
                    toolchain_qualifications
                ),
            }
        )

    add(
        "product/release",
        "supply",
        selector(
            ("$schema",),
            ("schema_version",),
            ("product",),
        ),
        ("implementation/candidate-manifest",),
    )

    add(
        "supply/evidence",
        "supply",
        classification_selector(
            classifications,
            "supply",
            (("$schema",), ("schema_version",), ("product",)),
        ),
    )

    future_selector = classification_selector(
        classifications, "future", (("python", "versions"),)
    )
    if any(future_selector(path) for path, _value in leaves):
        add("future/product", "future", future_selector)

    return documents


def render_component_documents(release, implemented_rows=IMPLEMENTED_ROWS):
    """Render and semantically validate the canonical component graph."""
    documents = _render_expected_components(release, implemented_rows)
    validate_component_set(release, documents, implemented_rows)
    return documents


def python_qualification_components(
    release, implemented_rows=IMPLEMENTED_ROWS
):
    """Return the two canonical identities governing Python qualification."""
    documents = render_component_documents(release, implemented_rows)
    return {
        role: {
            "component": component,
            "canonical_sha256": canonical_sha256(documents[component]),
        }
        for role, component in PYTHON_QUALIFICATION_COMPONENTS.items()
    }


def validate_python_qualification_components(
    value, release, implemented_rows=IMPLEMENTED_ROWS
):
    """Validate an untrusted policy/aggregate qualification identity pair."""
    require(
        isinstance(value, dict)
        and set(value) == set(PYTHON_QUALIFICATION_COMPONENTS),
        "Python qualification component roles differ",
    )
    for role, component in PYTHON_QUALIFICATION_COMPONENTS.items():
        record = value[role]
        require(
            isinstance(record, dict)
            and set(record) == {"component", "canonical_sha256"},
            "Python qualification %s component fields differ" % role,
        )
        require(
            record["component"] == component,
            "Python qualification %s component name differs" % role,
        )
        require(
            isinstance(record["canonical_sha256"], str)
            and len(record["canonical_sha256"]) == 64
            and all(
                character in "0123456789abcdef"
                for character in record["canonical_sha256"]
            ),
            "Python qualification %s component digest is invalid" % role,
        )
    expected = python_qualification_components(release, implemented_rows)
    require(
        value == expected,
        "Python qualification component identities differ from release/policy",
    )
    return copy.deepcopy(expected)


def bind_python_qualification_components(
    release,
    policy_sha256,
    aggregate_sha256,
    implemented_rows=IMPLEMENTED_ROWS,
):
    """Bind CLI-provided digests to their exact canonical component names."""
    value = {
        "policy": {
            "component": PYTHON_QUALIFICATION_COMPONENTS["policy"],
            "canonical_sha256": policy_sha256,
        },
        "aggregate": {
            "component": PYTHON_QUALIFICATION_COMPONENTS["aggregate"],
            "canonical_sha256": aggregate_sha256,
        },
    }
    return validate_python_qualification_components(
        value, release, implemented_rows
    )


def toolchain_qualification_component(
    release, arch, implemented_rows=IMPLEMENTED_ROWS
):
    """Return the canonical qualification identity for one toolchain."""
    require(
        arch in TOOLCHAIN_QUALIFICATION_COMPONENTS,
        "unsupported toolchain qualification architecture",
    )
    component = TOOLCHAIN_QUALIFICATION_COMPONENTS[arch]
    documents = render_component_documents(release, implemented_rows)
    return {
        "component": component,
        "canonical_sha256": canonical_sha256(documents[component]),
    }


def bind_toolchain_qualification_component(
    release, arch, canonical_sha256_value, implemented_rows=IMPLEMENTED_ROWS
):
    """Bind a CLI-provided digest to one exact toolchain component."""
    expected = toolchain_qualification_component(
        release, arch, implemented_rows
    )
    require(
        isinstance(canonical_sha256_value, str)
        and len(canonical_sha256_value) == 64
        and all(
            character in "0123456789abcdef"
            for character in canonical_sha256_value
        ),
        "toolchain qualification component digest is invalid",
    )
    require(
        canonical_sha256_value == expected["canonical_sha256"],
        "toolchain qualification component identity differs from release/ABI",
    )
    return expected


def validate_component_set(
    release, documents, implemented_rows=IMPLEMENTED_ROWS
):
    """Validate an untrusted component set against canonical pure rendering."""
    validate_policy_registry(implemented_rows)
    require(isinstance(documents, dict), "component set must be an object")
    expected = _render_expected_components(release, implemented_rows)
    require(
        set(documents) == set(expected),
        "component set differs from canonical release/policy projection",
    )
    classifications = classify_release_leaves(release, implemented_rows)
    release_leaves = {
        json_pointer(path): value for path, value in leaf_items(release)
    }
    owners = {path: [] for path in release_leaves}
    digests = {
        component: canonical_sha256(document)
        for component, document in documents.items()
    }
    edges = {}
    for component, document in documents.items():
        require(
            isinstance(component, str), "component mapping key must be text"
        )
        component_path(component)
        require(
            set(document) == COMPONENT_KEYS,
            "%s document fields differ" % component,
        )
        require(
            document.get("component") == component,
            "component document name mismatch: %s" % component,
        )
        require(
            document.get("$schema") == COMPONENT_SCHEMA_ID
            and document.get("schema_version") == 1
            and document.get("kind") == "crossforge-release-component",
            "%s component kind/schema identity differs" % component,
        )
        require(
            document.get("scope") in COMPONENT_SCOPES,
            "%s component scope is invalid" % component,
        )
        require(
            isinstance(document.get("materials"), list)
            and document["materials"],
            "%s materials must be a non-empty array" % component,
        )
        for record in document["materials"]:
            require(
                isinstance(record, dict) and set(record) == {"path", "value"},
                "%s material fields differ" % component,
            )
        paths = [record["path"] for record in document["materials"]]
        require(paths == sorted(paths), "%s materials are not sorted" % component)
        require(
            len(paths) == len(set(paths)),
            "%s materials repeat a path" % component,
        )
        if component.startswith("implementation/"):
            require(
                all(path.startswith("/@implementation/") for path in paths),
                "%s mixes release and implementation materials" % component,
            )
        for record in document["materials"]:
            path = record["path"]
            if path.startswith("/@implementation/"):
                require(
                    component.startswith("implementation/"),
                    "%s improperly owns implementation policy %s"
                    % (component, path),
                )
                continue
            require(
                path in release_leaves,
                "%s owns unknown release path %s" % (component, path),
            )
            require(
                record["value"] == release_leaves[path],
                "%s material differs from release path %s" % (component, path),
            )
            owners[path].append(component)
        dependency_names = []
        require(
            isinstance(document.get("dependencies"), list),
            "%s dependencies must be an array" % component,
        )
        for dependency in document["dependencies"]:
            require(
                isinstance(dependency, dict)
                and set(dependency) == {"component", "canonical_sha256"},
                "%s dependency fields differ" % component,
            )
            name = dependency["component"]
            dependency_names.append(name)
            require(name in documents, "%s has unknown dependency %s" % (component, name))
        require(
            dependency_names == sorted(dependency_names),
            "%s dependencies are not sorted" % component,
        )
        require(
            len(dependency_names) == len(set(dependency_names)),
            "%s repeats a dependency" % component,
        )
        edges[component] = dependency_names
        if component.startswith(("rpm/", "sources/", "toolchain/")) and component != "sources/zstd":
            require(
                not any(path.startswith("/python/") for path in paths),
                "%s improperly owns Python release material" % component,
            )
            require(
                not any(name.startswith("python/") for name in dependency_names),
                "%s improperly depends on Python" % component,
            )

    states = {}

    def visit(component):
        state = states.get(component, 0)
        require(state != 1, "component dependency graph contains a cycle")
        if state == 2:
            return
        states[component] = 1
        for dependency in edges[component]:
            visit(dependency)
        states[component] = 2

    for component in sorted(documents):
        visit(component)

    for component, document in documents.items():
        for dependency in document["dependencies"]:
            name = dependency["component"]
            require(
                dependency["canonical_sha256"] == digests[name],
                "%s dependency digest differs for %s" % (component, name),
            )

    missing = sorted(path for path, path_owners in owners.items() if not path_owners)
    require(
        not missing,
        "release leaf paths have no component owner: %s" % ", ".join(missing),
    )
    wrong_scope = []
    for path, category in classifications.items():
        if not any(documents[owner]["scope"] == category for owner in owners[path]):
            wrong_scope.append("%s:%s" % (path, category))
    require(
        not wrong_scope,
        "release leaves lack their classified owner scope: %s"
        % ", ".join(sorted(wrong_scope)),
    )
    for component in sorted(expected):
        require(
            canonical_bytes(documents[component])
            == canonical_bytes(expected[component]),
            "%s differs from its canonical release/policy projection"
            % component,
        )
    return owners


def _material_index_unchecked(document):
    result = {}
    for record in document.get("materials", []):
        require(
            isinstance(record, dict) and set(record) == {"path", "value"},
            "component material fields differ",
        )
        path = record["path"]
        require(path not in result, "component repeats material path %s" % path)
        result[path] = copy.deepcopy(record["value"])
    require(result, "component projection has no materials")
    return result


def material_index(
    release, documents, component, implemented_rows=IMPLEMENTED_ROWS
):
    """Validate the complete graph before exposing one component's values."""
    validate_component_set(release, documents, implemented_rows)
    require(component in documents, "unknown component: %s" % component)
    return _material_index_unchecked(documents[component])


def material_value(
    release, documents, component, path, implemented_rows=IMPLEMENTED_ROWS
):
    values = material_index(release, documents, component, implemented_rows)
    require(path in values, "component does not own material path %s" % path)
    return values[path]


def render_binding(release, documents):
    records = []
    for component in sorted(documents):
        document = documents[component]
        records.append(
            {
                "component": component,
                "scope": document["scope"],
                "path": component_path(component).as_posix(),
                "canonical_sha256": canonical_sha256(document),
            }
        )
    return {
        "$schema": BINDING_SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-release-binding",
        "release": {
            "schema": release["$schema"],
            "schema_version": release["schema_version"],
            "canonical_sha256": canonical_sha256(release),
        },
        "components": records,
    }


def validate_binding(
    release, documents, binding, implemented_rows=IMPLEMENTED_ROWS
):
    validate_component_set(release, documents, implemented_rows)
    expected_documents = _render_expected_components(release, implemented_rows)
    expected_binding = render_binding(release, expected_documents)
    require(
        isinstance(binding, dict)
        and set(binding)
        == {"$schema", "schema_version", "kind", "release", "components"},
        "release binding fields differ",
    )
    require(
        binding["$schema"] == BINDING_SCHEMA_ID
        and binding["schema_version"] == 1
        and binding["kind"] == "crossforge-release-binding",
        "release binding kind/schema identity differs",
    )
    expected_release = {
        "schema": release["$schema"],
        "schema_version": release["schema_version"],
        "canonical_sha256": canonical_sha256(release),
    }
    require(
        binding.get("release") == expected_release,
        "release binding canonical identity differs",
    )
    records = binding.get("components")
    require(isinstance(records, list) and records, "release binding has no components")
    names = []
    paths = []
    for record in records:
        require(
            isinstance(record, dict)
            and set(record)
            == {"component", "scope", "path", "canonical_sha256"},
            "release binding component fields differ",
        )
        component = record["component"]
        names.append(component)
        paths.append(record["path"])
        require(component in documents, "release binding names unknown component")
        document = documents[component]
        require(
            record["scope"] == document["scope"],
            "release binding component scope differs: %s" % component,
        )
        require(
            record["path"] == component_path(component).as_posix(),
            "release binding component path differs: %s" % component,
        )
        require(
            record["canonical_sha256"] == canonical_sha256(document),
            "release binding component digest differs: %s" % component,
        )
    require(names == sorted(documents), "release binding component order/set differs")
    require(len(paths) == len(set(paths)), "release binding repeats a component path")
    require(
        canonical_bytes(binding) == canonical_bytes(expected_binding),
        "release binding differs from canonical release/component projection",
    )


def validate_generated_schemas(repository, documents, binding):
    component_schema = STRICT["load_json"](
        repository / "config/schemas/release-component.schema.json"
    )
    binding_schema = STRICT["load_json"](
        repository / "config/schemas/release-binding.schema.json"
    )
    for schema in (component_schema, binding_schema):
        STRICT["validate_schema_subset"](schema)
    for component, document in documents.items():
        STRICT["validate"](
            document, component_schema, component_schema, "$component[%s]" % component
        )
    STRICT["validate"](
        binding, binding_schema, binding_schema, "$release_binding"
    )


def render_documents(
    repository=REPOSITORY,
    release=None,
    schema_path=None,
    implemented_rows=IMPLEMENTED_ROWS,
):
    if release is None:
        release = STRICT["load_json"](repository / "config/release.json")
    validate_release(repository, release, schema_path)
    components = render_component_documents(release, implemented_rows)
    binding = render_binding(release, components)
    validate_binding(release, components, binding, implemented_rows)
    validate_generated_schemas(repository, components, binding)
    result = {
        component_path(component): document
        for component, document in components.items()
    }
    result[BINDING_PATH] = binding
    return result


def output_drift(repository, documents):
    expected_paths = {repository / path for path in documents}
    problems = []
    for relative, document in sorted(
        documents.items(), key=lambda item: item[0].as_posix()
    ):
        path = repository / relative
        try:
            actual = path.read_text(encoding="utf-8")
        except OSError:
            problems.append("missing %s" % relative.as_posix())
            continue
        if actual != serialized(document):
            problems.append("stale %s" % relative.as_posix())
    component_directory = repository / COMPONENT_ROOT
    if component_directory.is_dir():
        for path in component_directory.rglob("*.json"):
            if path not in expected_paths:
                problems.append(
                    "unexpected %s" % path.relative_to(repository).as_posix()
                )
    generated_directory = repository / "config/generated"
    if generated_directory.is_dir():
        for path in generated_directory.rglob(".*.tmp"):
            problems.append(
                "abandoned %s" % path.relative_to(repository).as_posix()
            )
    return sorted(problems)


def _fsync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_temporary(path, payload):
    encoded = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name,
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(str(temporary), 0o644)
        return temporary
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise


def _replace_staged_raw(temporary, path):
    os.replace(str(temporary), str(path))
    _fsync_directory(path.parent)


def _replace_staged(temporary, path):
    _replace_staged_raw(temporary, path)


def _atomic_write_if_changed(path, payload):
    encoded = payload.encode("utf-8")
    try:
        if path.read_bytes() == encoded:
            return False
    except OSError:
        pass
    temporary = _stage_temporary(path, payload)
    try:
        _replace_staged(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def _snapshot_paths(paths):
    snapshots = {}
    for path in paths:
        if os.path.lexists(str(path)):
            require(
                path.is_file() and not path.is_symlink(),
                "generated output snapshot is not a regular file: %s" % path,
            )
            snapshots[path] = path.read_bytes()
        else:
            snapshots[path] = None
    return snapshots


def _missing_parent_directories(repository, paths):
    repository = Path(repository)
    missing = set()
    for path in paths:
        current = path.parent
        while current != repository and not current.exists():
            missing.add(current)
            current = current.parent
    return sorted(missing, key=lambda path: len(path.parts), reverse=True)


def _prune_directories(paths):
    for path in paths:
        try:
            path.rmdir()
        except OSError:
            pass


def _rollback_snapshots(snapshots, binding_path):
    errors = []
    ordered = sorted(
        snapshots,
        key=lambda path: (path == binding_path, path.as_posix()),
    )
    for path in ordered:
        previous = snapshots[path]
        try:
            if previous is None:
                if os.path.lexists(str(path)):
                    require(
                        path.is_file() and not path.is_symlink(),
                        "rollback target is not a regular file: %s" % path,
                    )
                    path.unlink()
                    _fsync_directory(path.parent)
                continue
            try:
                if path.read_bytes() == previous:
                    continue
            except OSError:
                pass
            temporary = _stage_temporary(path, previous)
            try:
                _replace_staged_raw(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        except BaseException as error:
            errors.append("%s: %s" % (path, error))
    require(
        not errors,
        "generated output rollback failed: %s" % "; ".join(errors),
    )


def _checked_output_path(repository, relative):
    require(isinstance(relative, Path), "generated output path must be a Path")
    require(
        not relative.is_absolute()
        and relative.parts
        and ".." not in relative.parts,
        "generated output path escapes repository: %s" % relative,
    )
    repository = Path(repository)
    require(
        repository.is_dir() and not repository.is_symlink(),
        "repository output root must be a real directory",
    )
    root = Path(os.path.realpath(str(repository)))
    candidate = repository / relative
    current = repository
    for part in relative.parts:
        current = current / part
        require(
            not (os.path.lexists(str(current)) and current.is_symlink()),
            "generated output path contains a symlink: %s" % current,
        )
    resolved = Path(os.path.realpath(str(candidate)))
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ProjectionError(
            "generated output resolves outside repository: %s" % relative
        )
    return candidate


def _planned_cleanup(repository, expected_paths):
    cleanup = []
    component_directory = _checked_output_path(repository, COMPONENT_ROOT)
    if component_directory.is_dir():
        for path in component_directory.rglob("*.json"):
            relative = path.relative_to(repository)
            checked = _checked_output_path(repository, relative)
            if checked not in expected_paths:
                cleanup.append(checked)
    generated_directory = _checked_output_path(
        repository, Path("config/generated")
    )
    if generated_directory.is_dir():
        for path in generated_directory.rglob(".*.tmp"):
            relative = path.relative_to(repository)
            cleanup.append(_checked_output_path(repository, relative))
    return cleanup


def _validate_write_inputs(
    repository, release, documents, implemented_rows
):
    require(isinstance(documents, dict), "generated documents must be an object")
    for relative in documents:
        _checked_output_path(repository, relative)
    require(BINDING_PATH in documents, "release binding commit marker is missing")

    components = {}
    for relative, document in documents.items():
        if relative == BINDING_PATH:
            continue
        require(
            isinstance(document, dict)
            and isinstance(document.get("component"), str),
            "generated component document is invalid",
        )
        component = document["component"]
        require(component not in components, "generated component is duplicated")
        components[component] = document

    validate_component_set(release, components, implemented_rows)
    validate_binding(
        release, components, documents[BINDING_PATH], implemented_rows
    )
    expected_mapping = {
        component_path(component): document
        for component, document in components.items()
    }
    expected_mapping[BINDING_PATH] = documents[BINDING_PATH]
    require(
        set(documents) == set(expected_mapping),
        "generated document paths differ from component identities",
    )
    for relative in expected_mapping:
        require(
            documents[relative] is expected_mapping[relative]
            or canonical_bytes(documents[relative])
            == canonical_bytes(expected_mapping[relative]),
            "generated document path/component mapping differs",
        )

    expected_paths = {
        _checked_output_path(repository, relative) for relative in documents
    }
    cleanup = _planned_cleanup(repository, expected_paths)
    return components, expected_paths, cleanup


def write_documents(
    repository,
    release,
    documents,
    implemented_rows=IMPLEMENTED_ROWS,
):
    """Commit per-file atomically, with ordinary-error rollback.

    All replacement payloads are durable before the first replace.  An ordinary
    in-process I/O exception restores the prior bytes/nonexistence state.  This
    is not a power-loss or process-kill transactional filesystem snapshot: the
    binding remains the fail-closed, binding-last commit marker for those cases.
    """
    repository = Path(repository)
    _components, _expected_paths, cleanup = _validate_write_inputs(
        repository, release, documents, implemented_rows
    )
    component_items = sorted(
        (
            (relative, document)
            for relative, document in documents.items()
            if relative != BINDING_PATH
        ),
        key=lambda item: item[0].as_posix(),
    )
    ordered_items = component_items + [
        (BINDING_PATH, documents[BINDING_PATH])
    ]
    write_plan = []
    for relative, document in ordered_items:
        path = repository / relative
        payload = serialized(document)
        encoded = payload.encode("utf-8")
        try:
            unchanged = path.read_bytes() == encoded
        except OSError:
            unchanged = False
        if not unchanged:
            write_plan.append((relative, path, payload))

    affected = {path for _relative, path, _payload in write_plan}
    affected.update(cleanup)
    snapshots = _snapshot_paths(affected)
    created_directories = _missing_parent_directories(
        Path(repository), [path for _relative, path, _payload in write_plan]
    )
    staged = []
    binding_path = repository / BINDING_PATH
    try:
        # Stage every payload before changing the visible component set.
        for relative, path, payload in write_plan:
            staged.append(
                (relative, path, _stage_temporary(path, payload))
            )

        written = []
        for relative, path, temporary in staged:
            _replace_staged(temporary, path)
            written.append(relative)

        # Stale projections and abandoned temporaries are removed only after
        # the binding-last replacement completed.
        for path in cleanup:
            if path.is_file() or path.is_symlink():
                path.unlink()
                _fsync_directory(path.parent)
        return written
    except BaseException:
        for _relative, _path, temporary in staged:
            if temporary.exists():
                temporary.unlink()
        try:
            _rollback_snapshots(snapshots, binding_path)
        finally:
            _prune_directories(created_directories)
        raise
    finally:
        for _relative, _path, temporary in staged:
            if temporary.exists():
                temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--config", type=Path, default=REPOSITORY / "config/release.json"
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release.schema.json",
    )
    arguments = parser.parse_args()
    try:
        release = STRICT["load_json"](arguments.config)
        documents = render_documents(REPOSITORY, release, arguments.schema)
        if arguments.check:
            problems = output_drift(REPOSITORY, documents)
            if problems:
                raise ProjectionError("; ".join(problems))
            print(
                "valid: %d release component projections are current"
                % (len(documents) - 1)
            )
        else:
            write_documents(REPOSITORY, release, documents)
            print(
                "wrote: %d release component projections and binding"
                % (len(documents) - 1)
            )
    except (OSError, ProjectionError, ReleaseValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    print(
        "error: release-components-core.py is internal; "
        "use render-release-components.py",
        file=sys.stderr,
    )
    raise SystemExit(2)
