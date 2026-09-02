#!/usr/bin/env python3
"""Bridge a prepared CPython source manifest to one complete release row."""

import hashlib
import json
import runpy
from pathlib import Path


class BindingError(RuntimeError):
    pass


def support_script(name):
    sibling = Path(__file__).with_name(name)
    if sibling.is_file():
        return sibling
    repository_script = Path(__file__).resolve().parents[1] / "scripts" / name
    if repository_script.is_file():
        return repository_script
    raise BindingError("missing source binding support script: %s" % name)


ROW_CONTRACT = runpy.run_path(str(support_script("python_row_contract.py")))
ContractError = ROW_CONTRACT["ContractError"]
COMPONENT_RENDERER = None

SOURCE_MANIFEST_V1_KEYS = {
    "schema_version",
    "kind",
    "row",
    "version",
    "minor",
    "compact",
    "adapter",
    "support",
    "release_sha256",
    "source",
    "patches",
}
SOURCE_MANIFEST_V2_KEYS = {
    "schema_version",
    "kind",
    "row",
    "version",
    "minor",
    "compact",
    "adapter",
    "source_component",
    "build_policy",
    "source",
    "patches",
}
SOURCE_KEYS = {"url", "size", "sha256"}
PATCH_KEYS = {"file", "sha256"}
SOURCE_COMPONENT_KEYS = {"component", "canonical_sha256"}
BUILD_POLICY_KEYS = {
    "component",
    "canonical_sha256",
    "minor",
    "row",
    "adapter",
    "sysconfig_isolation",
}


def require(condition, message):
    if not condition:
        raise BindingError(message)


def canonical_sha256(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_equal(left, right):
    return canonical_sha256(left) == canonical_sha256(right)


def component_renderer():
    global COMPONENT_RENDERER
    if COMPONENT_RENDERER is None:
        COMPONENT_RENDERER = runpy.run_path(
            str(support_script("release-components-core.py"))
        )
    return COMPONENT_RENDERER


def release_row_context(release, row, version, adapter):
    try:
        binding = ROW_CONTRACT["bind_release"](
            release, version=version, adapter=adapter
        )
    except ContractError as error:
        raise BindingError(str(error)) from error
    contract = binding["contract"]
    entry = binding["entry"]
    require(contract["row"] == row, "release row differs from source row")
    source = entry.get("source")
    require(
        isinstance(source, dict) and source.get("status") == "locked",
        "release CPython source is not locked",
    )
    return contract, entry


def bind_source_manifest(manifest, release, row, version, adapter):
    """Return a release-bound identity from an already loaded v1/v2 manifest."""
    require(isinstance(manifest, dict), "prepared source manifest must be an object")
    require(isinstance(release, dict), "release must be an object")
    contract, entry = release_row_context(release, row, version, adapter)
    release_sha256 = canonical_sha256(release)
    minor = version.rsplit(".", 1)[0]
    require(
        manifest.get("kind") == "crossforge-cpython-source-row"
        and manifest.get("row") == row
        and manifest.get("version") == version
        and manifest.get("minor") == minor
        and manifest.get("compact") == minor.replace(".", "")
        and manifest.get("adapter") == adapter,
        "prepared source manifest row identity differs",
    )
    expected_source = {
        "url": entry["source"]["url"],
        "size": entry["source"]["size"],
        "sha256": entry["source"]["sha256"],
    }
    expected_patches = entry.get("patches", [])
    source = manifest.get("source")
    patches = manifest.get("patches")
    require(
        isinstance(source, dict)
        and set(source) == SOURCE_KEYS
        and canonical_equal(source, expected_source),
        "prepared source identity differs from release",
    )
    require(
        isinstance(patches, list)
        and all(
            isinstance(patch, dict) and set(patch) == PATCH_KEYS
            for patch in patches
        )
        and canonical_equal(patches, expected_patches),
        "prepared patch identity differs from release",
    )

    source_component = None
    build_policy = None
    schema_version = manifest.get("schema_version")
    if type(schema_version) is int and schema_version == 1:
        require(
            set(manifest) == SOURCE_MANIFEST_V1_KEYS,
            "prepared v1 source manifest fields differ",
        )
        require(
            manifest.get("release_sha256") == release_sha256
            and manifest.get("support") == entry["support"],
            "prepared v1 source manifest release binding differs",
        )
    elif type(schema_version) is int and schema_version == 2:
        require(
            set(manifest) == SOURCE_MANIFEST_V2_KEYS,
            "prepared v2 source manifest fields differ",
        )
        renderer = component_renderer()
        try:
            components = renderer["render_component_documents"](release)
        except renderer["ProjectionError"] as error:
            raise BindingError(
                "cannot derive release components: %s" % error
            ) from error
        source_name = "python/%s-source" % row
        policy_name = "implementation/python-%s-build-policy" % row
        require(
            source_name in components and policy_name in components,
            "release does not derive row source/policy components",
        )
        expected_source_component = {
            "component": source_name,
            "canonical_sha256": renderer["canonical_sha256"](
                components[source_name]
            ),
        }
        source_component = manifest.get("source_component")
        require(
            isinstance(source_component, dict)
            and set(source_component) == SOURCE_COMPONENT_KEYS
            and canonical_equal(source_component, expected_source_component),
            "prepared source component differs from release projection",
        )
        expected_policy = {
            "component": policy_name,
            "canonical_sha256": renderer["canonical_sha256"](
                components[policy_name]
            ),
            "minor": contract["minor"],
            "row": contract["row"],
            "adapter": contract["adapter"],
            "sysconfig_isolation": contract["sysconfig_isolation"],
        }
        build_policy = manifest.get("build_policy")
        require(
            isinstance(build_policy, dict)
            and set(build_policy) == BUILD_POLICY_KEYS
            and canonical_equal(build_policy, expected_policy),
            "prepared build policy differs from release projection",
        )
    else:
        raise BindingError("unsupported prepared source manifest schema")
    return {
        "schema_version": schema_version,
        "release_sha256": release_sha256,
        "support": entry["support"],
        "source": source,
        "patches": patches,
        "source_component": source_component,
        "build_policy": build_policy,
    }
