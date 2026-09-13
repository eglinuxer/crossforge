"""Resolve signed toolchain/GCC reports against current inputs and actual artifacts."""

import os
from pathlib import Path

from . import catalog_registry, component_build, component_catalog, component_inputs
from . import component_qualification, qualification_execution, registry_transfer
from .identity import load_json, require


def resolve(source, graph, arch, profile, execution, subjects, cosign, directory, builder, oras,
            docker_config=None, catalog_reference=None):
    """Only an absent input index permits new qualification; verification errors fail."""
    directory = Path(directory).absolute()
    require(not directory.exists() and not directory.is_symlink(),
            "toolchain qualification resolution directory must be new")
    settings = component_qualification.spec(arch, profile)
    require(qualification_execution.execution_identity(builder, docker_config) == execution,
            "toolchain qualification resolution execution environment differs")
    directory.parent.mkdir(parents=True, exist_ok=True)
    resolved, bindings = component_qualification.bind_subjects(source, graph, settings, execution,
        subjects, builder, docker_config, directory.parent)
    expected = component_qualification.qualification_inputs(source, resolved, settings, execution, bindings)
    component_build.write_json(directory / "inputs.json", expected)
    policy = registry_transfer.validate_tool(load_json(Path(source) / ".github/locked-tools/oras.json"))
    config = Path(docker_config or os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker") / "config.json"
    selected = catalog_registry.lookup(source, expected, "qualification", cosign, directory / "catalog",
        component_catalog.REPOSITORY, oras, policy, config, catalog_reference=catalog_reference)
    result = {"schema_version": 1, "kind": "crossforge-toolchain-qualification-resolution",
        "component": settings["component"], "role": "qualification", "inputs_sha256": component_inputs.identity(expected)}
    if selected["status"] == "missing":
        require(catalog_reference is None, "fixed toolchain qualification catalog cannot be replaced")
        result.update(status="qualification-required", reason=selected["reason"], input_tag=selected["input_tag"])
    else:
        require(selected["status"] == "authenticated-reference", "unsupported qualification catalog result")
        entry = selected["entry"]
        receipt = entry["receipt"]
        layout = directory / "oci"
        registry_transfer.fetch(entry["reference"], layout, oras, policy, config)
        verified = component_qualification.verify_local(receipt, entry["receipt_sha256"], expected,
            source, layout, builder, docker_config, directory)
        receipt_path = directory / "receipt.json"
        component_build.write_json(receipt_path, receipt)
        result.update(status="verified-qualification", reason="authenticated-catalog-and-verified-prior-execution",
            subject={"receipt": str(receipt_path), "receipt_sha256": entry["receipt_sha256"], "layout": str(layout)},
            verification=verified, catalog=selected["catalog"], authentication=selected["authentication"],
            producer=receipt["contract"]["producer"], reference=entry["reference"])
    require(qualification_execution.execution_identity(builder, docker_config) == execution,
            "toolchain qualification resolution execution environment changed")
    component_inputs.require_match(expected,
        component_qualification.qualification_inputs(source, resolved, settings, execution, bindings))
    component_build.write_json(directory / "resolution.json", result)
    return result
