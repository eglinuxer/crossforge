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
    add(
        "implementation/crosspack",
        "build",
        explicit_materials=policy_materials(
            "/@implementation/crosspack/", CROSSPACK_POLICY
        ),
    )
