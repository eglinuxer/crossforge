"""Resolve build components through authenticated catalogs and verified OCI bytes.

An absent input index requests a producer. All other failures remain errors;
neither a catalog signature nor an acquisition result asserts qualification.
"""

from pathlib import Path

from . import catalog_registry, component_build, component_catalog, component_inputs
from . import registry_transfer
from .identity import load_json, require


def toolchain(source, graph, arch, role, execution, cosign, directory, builder, oras,
              docker_config=None, catalog_reference=None):
    directory = Path(directory).absolute()
    require(not directory.exists() and not directory.is_symlink(), "component resolution directory must be new")
    spec = component_build.toolchain_spec(arch, role)
    require(component_build.execution_identity(builder, docker_config) == execution,
            "component resolution build environment differs")
    expected = component_build.toolchain_inputs(source, graph, arch, role, execution)
    component_build.write_json(directory / "inputs.json", expected)
    policy = registry_transfer.validate_tool(load_json(Path(source) / ".github/locked-tools/oras.json"))
    registry_config = Path(docker_config or Path.home() / ".docker") / "config.json"
    selected = catalog_registry.lookup(source, expected, role, cosign, directory / "catalog",
        component_catalog.REPOSITORY, oras, policy, registry_config, catalog_reference=catalog_reference)
    result = {"schema_version": 1, "kind": "crossforge-component-resolution",
        "component": spec["component"], "role": role, "inputs_sha256": component_inputs.identity(expected)}
    if selected["status"] == "missing":
        require(catalog_reference is None, "fixed recovery catalog cannot be replaced by a producer")
        result.update(status="build-required", reason=selected["reason"], input_tag=selected["input_tag"])
    else:
        require(selected["status"] == "authenticated-reference", "unsupported catalog selection result")
        entry = selected["entry"]
        receipt = entry["receipt"]
        layout = directory / "oci"
        registry_transfer.fetch(entry["reference"], layout, oras, policy, registry_config)
        frontend = expected["parameters"]["recipes"][spec["target"]]["frontend"]
        context = component_build.verify_local(receipt, entry["receipt_sha256"], expected, role,
            layout, frontend, builder, docker_config, directory)
        receipt_path = directory / "receipt.json"
        component_build.write_json(receipt_path, receipt)
        result.update(status="verified-build-component", reason="authenticated-catalog-and-matching-artifact",
            context=context, subject={"receipt": str(receipt_path), "receipt_sha256": entry["receipt_sha256"],
                "layout": str(layout)}, catalog=selected["catalog"], authentication=selected["authentication"],
            producer=receipt["contract"]["producer"], reference=entry["reference"])
    require(component_build.execution_identity(builder, docker_config) == execution,
            "component resolution build environment changed")
    component_inputs.require_match(expected, component_build.toolchain_inputs(source, graph, arch, role, execution))
    component_build.write_json(directory / "resolution.json", result)
    return result
