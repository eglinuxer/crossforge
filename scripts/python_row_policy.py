"""Authenticate one complete Python row's policy without a full release graph.

The row root pins both target policies, which pin the shared source/build policy.
This is an input contract, not an execution receipt or permission to reuse one.
"""

import copy
from pathlib import Path
import runpy

TARGET_POLICY = runpy.run_path(str(Path(__file__).with_name("python_qualification_policy.py")))
READER = TARGET_POLICY["READER"]
ROWS = TARGET_POLICY["ROWS"]
PREPARER = runpy.run_path(str(Path(__file__).with_name("prepare-cpython-source.py")))
ARCHES = ("x86_64", "aarch64")


class RowPolicyError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise RowPolicyError(message)


def row_contract(row, version, adapter):
    try:
        contract = ROWS["contract_for_version"](version)
    except ROWS["ContractError"] as error:
        raise RowPolicyError(str(error)) from error
    require(contract["row"] == row and contract["adapter"] == adapter, "row policy row/version/adapter differs")
    return contract


def component_name(row, version, adapter):
    row_contract(row, version, adapter)
    return "python/%s-qualification" % row


def root_dependencies(document, row, version, adapter, trusted_sha256):
    name = component_name(row, version, adapter)
    try:
        READER["validate_canonical_sha256"](trusted_sha256)
        READER["validate_component"](document, name, "qualification")
        require(READER["canonical_sha256"](document) == trusted_sha256, "row qualification root digest differs")
        prefix = TARGET_POLICY["_one_prefix"](document, "/python/versions")
        require(document["materials"] == [{"path": prefix + "/adapter", "value": adapter},
                                          {"path": prefix + "/version", "value": version}],
                "row qualification root material set differs")
        dependencies = {item["component"]: item["canonical_sha256"] for item in document["dependencies"]}
        require(set(dependencies) == {"python/%s-%s-qualification" % (row, arch) for arch in ARCHES},
                "row qualification root must bind both target policies")
        return dependencies
    except (READER["ComponentError"], TARGET_POLICY["PolicyError"]) as error:
        raise RowPolicyError(str(error)) from error


def assemble(root, targets, source_manifest, row, version, adapter, trusted_sha256):
    dependencies = root_dependencies(root, row, version, adapter, trusted_sha256)
    require(set(targets) == set(ARCHES), "row qualification target inventory differs")
    common = targets["x86_64"]
    for arch in ARCHES:
        target = targets[arch]
        name = "python/%s-%s-qualification" % (row, arch)
        require(target["component"] == {"component": name, "canonical_sha256": dependencies[name]} and
                target["version"] == version and target["target"]["arch"] == arch,
                "row target policy differs from its authenticated root")
        for key in ("contract", "version", "support", "source", "source_components"):
            require(READER["canonical_sha256"](target[key]) == READER["canonical_sha256"](common[key]),
                    "row targets disagree on " + key)
    require(source_manifest["source"] == {key: common["source"][key] for key in ("url", "size", "sha256")},
            "row prepared source differs from qualification source")
    return {"schema_version": 1, "kind": "crossforge-python-row-policy",
            "component": {"component": component_name(row, version, adapter), "canonical_sha256": trusted_sha256},
            "row": row, "version": version, "adapter": adapter, "support": common["support"],
            "targets": copy.deepcopy(targets), "source_manifest": copy.deepcopy(source_manifest)}


def load(directory, row, version, adapter, trusted_sha256):
    directory = Path(directory)
    name = component_name(row, version, adapter)
    try:
        root = READER["load_component"](directory / (name + ".json"), name, "qualification", trusted_sha256)
        dependencies = root_dependencies(root, row, version, adapter, trusted_sha256)
        targets = {arch: TARGET_POLICY["load"](directory, version, arch,
                    dependencies["python/%s-%s-qualification" % (row, arch)]) for arch in ARCHES}
        source = targets["x86_64"]["source_components"]
        entry, identities = PREPARER["row_from_components"](row, version, adapter,
            directory / (source["source"]["component"] + ".json"), source["source"]["canonical_sha256"],
            directory / (source["policy"]["component"] + ".json"), source["policy"]["canonical_sha256"])
        manifest = PREPARER["_source_manifest"](entry, row, entry["patches"], dict(identities, mode="component"))
        return assemble(root, targets, manifest, row, version, adapter, trusted_sha256)
    except (READER["ComponentError"], TARGET_POLICY["PolicyError"], PREPARER["PreparationError"]) as error:
        raise RowPolicyError(str(error)) from error


def from_release(release, row, version, adapter, render_components):
    """Derive all expectations independently from the consumer's full release."""
    contract = row_contract(row, version, adapter)
    try:
        entry = ROWS["bind_release"](release, version=version, adapter=adapter)["entry"]
        documents = render_components(release)
        root = documents[component_name(row, version, adapter)]
        digest = READER["canonical_sha256"](root)
        dependencies = root_dependencies(root, row, version, adapter, digest)
        targets = {arch: TARGET_POLICY["from_documents"](
            documents["python/%s-%s-qualification" % (row, arch)],
            documents["implementation/python-%s-qualification-policy" % row], version, arch,
            dependencies["python/%s-%s-qualification" % (row, arch)]) for arch in ARCHES}
        source = targets["x86_64"]["source_components"]
        build_policy = dict(source["policy"], **{key: contract[key] for key in ("minor", "row", "adapter", "sysconfig_isolation")})
        manifest = PREPARER["_source_manifest"](entry, row, entry["patches"],
            {"mode": "component", "source_component": source["source"], "build_policy": build_policy})
        return assemble(root, targets, manifest, row, version, adapter, digest)
    except (ROWS["ContractError"], READER["ComponentError"], TARGET_POLICY["PolicyError"], PREPARER["PreparationError"]) as error:
        raise RowPolicyError(str(error)) from error


def binding(policy):
    return {"schema_version": 1, "kind": "crossforge-python-row-input-binding",
            "policy_sha256": READER["canonical_sha256"](policy)}


def require_source(manifest, policy):
    require(READER["canonical_sha256"](manifest) == READER["canonical_sha256"](policy["source_manifest"]),
            "prepared source manifest differs from authenticated row inputs")


def require_binding(manifest, policy):
    require(type(manifest.get("schema_version")) is int and manifest["schema_version"] == 3,
            "scoped row manifest schema differs")
    require("release_sha256" not in manifest and "qualification_components" not in manifest,
            "scoped row manifest contains legacy release claims")
    require(READER["canonical_sha256"](manifest.get("input_binding")) == READER["canonical_sha256"](binding(policy)),
            "row manifest input binding differs")
    for key in ("row", "version", "adapter", "support"):
        require(manifest.get(key) == policy[key], "row manifest %s differs from policy" % key)
    for key in ("source", "patches"):
        require(READER["canonical_sha256"](manifest.get(key)) == READER["canonical_sha256"](policy["source_manifest"][key]),
                "row manifest %s differs from policy" % key)
