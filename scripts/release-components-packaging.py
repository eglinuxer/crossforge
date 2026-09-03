#!/usr/bin/env python3
"""Packaging extension for the canonical release-component graph."""

import copy


CROSSPACK_POLICY = {
    "schema_version": 1,
    "config_schema": "https://crossforge.dev/schemas/crosspack.schema.json",
    "plan_schema": "https://crossforge.dev/schemas/crosspack-plan.schema.json",
    "result_schema": "https://crossforge.dev/schemas/crosspack-result.schema.json",
    "formats": ["deb", "rpm"],
    "targets": {
        "x86_64": {"deb": "amd64", "rpm": "x86_64", "elf_machine": 62},
        "aarch64": {"deb": "arm64", "rpm": "aarch64", "elf_machine": 183},
    },
    "ownership": "complete-exclusive-staged-tree",
    "paths": "canonical-no-escape-no-overlap",
    "external_dependencies": "explicit-per-format",
    "internal_dependencies": "exact-version-release",
    "reproducibility": {
        "identity": "canonical-json-sha256",
        "timestamp": "source-date-epoch",
        "compression": "gzip",
    },
    "security": {
        "special_files": "forbidden",
        "setuid_setgid": "forbidden",
        "world_writable": "forbidden",
        "symlink_target": "owned-with-declared-component-edge",
        "elf": "64-bit-little-endian-exact-target",
    },
}
CROSSPACK_QUALIFICATION_POLICY = {
    "schema_version": 1,
    "targets": ["x86_64", "aarch64"],
    "formats": ["deb", "rpm"],
    "installers": {
        "deb": "dpkg",
        "rpm": "rpm",
    },
    "deb_test_image": {
        "repository": "docker.io/library/debian",
        "tag": "bookworm-slim",
        "amd64_manifest": "sha256:5ae3c39ebd15e229dcedd5cee596b2497182493d41ff162e824ba13fc1b2b867",
    },
    "requirements": {
        "byte_reproducible": True,
        "exact_metadata": True,
        "isolated_install_root": True,
        "installed_payload_hashes": True,
    },
}
CROSSFORGE_LAUNCHER_POLICY = {
    "schema_version": 1,
    "commands": ["info", "package", "run", "shell"],
    "target_selection": "explicit-no-project-guessing",
    "default_environment": "native-gts15-host",
    "subprocess_environment": "copy-and-overlay",
    "targets": ["x86_64", "aarch64"],
    "python_minors": ["3.9", "3.10", "3.11", "3.12", "3.13", "3.14"],
    "vcpkg_linkage": ["static", "dynamic"],
    "package_backend": "internal-locked-nfpm",
}
COMPLETE_SDK_QUALIFICATION_POLICY = {
    "schema_version": 1,
    "host": "x86_64",
    "targets": ["x86_64", "aarch64"],
    "python_minors": ["3.9", "3.10", "3.11", "3.12", "3.13", "3.14"],
    "vcpkg_linkage": ["static", "dynamic"],
    "matrix_size": 24,
    "composition": "qualified-artifacts-only",
    "publishable": False,
}


def policy_materials(prefix, value):
    records = []

    def visit(path, current):
        if isinstance(current, dict) and current:
            for key in sorted(current):
                visit(path + (key,), current[key])
            return
        if isinstance(current, list) and current:
            for index, item in enumerate(current):
                visit(path + (str(index),), item)
            return
        records.append(
            {"path": prefix + "/".join(path), "value": copy.deepcopy(current)}
        )

    visit((), value)
    return records


def extend_component_graph(context):
    """Add only the crosspack implementation identity to a prepared core graph."""
    add = context["add"]
    selector = context["selector"]
    add(
        "implementation/crosspack",
        "build",
        explicit_materials=policy_materials(
            "/@implementation/crosspack/", CROSSPACK_POLICY
        ),
    )
    add(
        "implementation/crosspack-qualification",
        "qualification",
        explicit_materials=policy_materials(
            "/@implementation/crosspack-qualification/",
            CROSSPACK_QUALIFICATION_POLICY,
        ),
    )
    add(
        "implementation/launcher",
        "build",
        explicit_materials=policy_materials(
            "/@implementation/launcher/", CROSSFORGE_LAUNCHER_POLICY
        ),
    )
    add(
        "implementation/complete-sdk-qualification",
        "qualification",
        explicit_materials=policy_materials(
            "/@implementation/complete-sdk-qualification/",
            COMPLETE_SDK_QUALIFICATION_POLICY,
        ),
    )
    add(
        "packaging/sdk-build",
        "build",
        selector(("baseline",), ("platforms",)),
        (
            "implementation/crosspack",
            "implementation/launcher",
            "sources/nfpm",
            "vcpkg/sdk-build",
        ),
    )
    add(
        "packaging/qualification",
        "qualification",
        selector(("baseline",), ("platforms",)),
        (
            "implementation/crosspack-qualification",
            "packaging/sdk-build",
            context["toolchain_qualifications"]["x86_64"],
            context["toolchain_qualifications"]["aarch64"],
        ),
    )
    add(
        "product/sdk-qualification",
        "qualification",
        selector(("baseline",), ("platforms",)),
        (
            "implementation/complete-sdk-qualification",
            "implementation/launcher",
            "packaging/qualification",
            "python/qualification",
        ),
    )
