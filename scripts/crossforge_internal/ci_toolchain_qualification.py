"""Acquire authenticated raw subjects, then reuse or freshly qualify one gate."""

import os
from pathlib import Path
import shutil

from . import component_build, component_catalog, component_ci, component_inputs, component_resolution
from . import component_qualification, qualification_execution, registry_transfer
from . import toolchain_qualification_handoff, toolchain_qualification_resolution
from .identity import content_sha256, file_record, load_json, require


def unchanged(source, producer, execution, builder, docker_config):
    require(qualification_execution.execution_identity(builder, docker_config) == execution,
            "toolchain qualification producer execution environment changed")
    current = component_ci.checked_source(source, "main")
    require(all(current[key] == producer[key] for key in ("source_commit", "source_dirty", "invocation")),
            "toolchain qualification producer source or invocation changed")


def preserve_qualification(source, directory, destination, settings):
    """Preserve exact reports and progress on failure without copying raw OCI data."""
    paths = ["inputs.json", "qualification.bake.json", "receipt.json", "buildx-metadata.json"]
    paths += ["payload/" + name for name in [component_qualification.RECORD_PATH,
        component_qualification.PROGRESS_PATH] + list(component_qualification.report_copies(source, settings).values())]
    paths += ["extracted/" + name for name in ("extract.bake.json", "extract-attempt2.bake.json",
              "export-timeout-1.json", "export-timeout-2.json")]
    for name in paths:
        path = directory / name
        if path.exists() or path.is_symlink():
            file_record(directory, name)
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(path), str(target), follow_symlinks=False)


def ensure(source, arch, profile, directory, builder, oras, cosign, docker_config=None):
    directory = Path(directory).absolute()
    require(not directory.exists() and not directory.is_symlink(), "qualification producer output directory must be new")
    settings = component_qualification.spec(arch, profile)
    producer = component_ci.checked_source(source, "main")
    graph = component_ci.source_graph(source, [settings["target"]], directory / "source", builder, docker_config)
    execution = qualification_execution.execution_identity(builder, docker_config)
    report = directory / "report"
    subjects, raw = {}, {}
    for role in sorted(set(settings["contexts"].values())):
        output = directory / "subjects" / role
        try:
            result = component_resolution.toolchain(source, graph, arch, role, execution["build"],
                cosign, output, builder, oras, docker_config)
        finally:
            if output.exists():
                component_resolution.preserve_evidence(output, report / "subjects" / role)
        require(result["status"] == "verified-build-component", "qualification requires prepared raw role: " + role)
        subjects[role], raw[role] = result["subject"], result
    output = directory / "resolution"
    try:
        resolution = toolchain_qualification_resolution.resolve(source, graph, arch, profile, execution,
            subjects, cosign, output, builder, oras, docker_config)
    finally:
        if output.exists():
            component_resolution.preserve_evidence(output, report / "qualification-reference")
    result = {"schema_version": 1, "kind": "crossforge-ci-toolchain-qualification-production",
        "arch": arch, "profile": profile, "subjects": subjects, "raw_resolutions": raw,
        "resolution": resolution, "produced": False}
    if resolution["status"] == "qualification-required":
        output = directory / "qualification"
        try:
            built = component_qualification.produce(source, graph, arch, profile, execution, producer,
                subjects, output, builder, docker_config)
        finally:
            preserve_qualification(source, output, report / "qualification", settings)
        receipt = load_json(output / "receipt.json")
        require(component_inputs.identity(receipt["contract"]["inputs"]) == resolution["inputs_sha256"],
                "qualification inputs changed after the missing index was selected")
        require(built["artifact"] == receipt["artifact"], "qualification producer artifact differs from receipt")
        entry = {"reference": component_catalog.REPOSITORY + "@" + receipt["artifact"]["root_digest"],
                 "receipt_sha256": built["receipt_sha256"], "receipt": receipt}
        handoff = toolchain_qualification_handoff.document(source, arch, profile, producer, execution, entry)
        unchanged(source, producer, execution, builder, docker_config)
        policy = registry_transfer.validate_tool(load_json(Path(source) / ".github/locked-tools/oras.json"))
        config = Path(docker_config or os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker") / "config.json"
        published = registry_transfer.publish(output / "oci", built["artifact"]["root_digest"],
            component_catalog.REPOSITORY, oras, policy, config)
        require(published["reference"] == entry["reference"], "published qualification reference differs")
        path = directory / "handoff.json"
        component_build.write_json(path, handoff)
        result.update(produced=True, handoff=str(path), handoff_sha256=content_sha256(handoff),
                      producer_invocation=producer["invocation"], qualification=built)
    else:
        require(resolution["status"] == "verified-qualification", "unsupported toolchain qualification resolution")
    unchanged(source, producer, execution, builder, docker_config)
    component_build.write_json(report / "result.json", result)
    return result
