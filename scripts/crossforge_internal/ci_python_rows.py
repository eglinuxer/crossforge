"""Acquire seven raw subjects, then verify or freshly qualify one complete row."""

import os
from pathlib import Path
import shutil

from . import component_build, component_catalog, component_ci, component_inputs, component_resolution
from . import python_components, python_handoff, python_qualification, python_row_handoff, python_row_resolution
from . import qualification_execution, registry_transfer
from .identity import content_sha256, file_record, load_json, require


def prepared_subjects(source, graph, row, execution, directory, report, builder, oras, cosign, docker_config):
    """Never compile a raw dependency implicitly in the qualification job."""
    subjects, resolutions = {}, {}
    for arch in python_components.ARCHES:
        name = arch + "-toolchain"
        output = directory / name
        try:
            result = component_resolution.toolchain(source, graph, arch, "toolchain-install", execution["build"],
                cosign, output, builder, oras, docker_config)
        finally:
            if output.exists():
                component_resolution.preserve_evidence(output, report / name)
        require(result["status"] == "verified-build-component", "Python row requires a prepared toolchain: " + arch)
        subjects[name], resolutions[name] = result["subject"], result
    for name, arch, kind in python_handoff.PARTS:
        dependencies = {} if arch == "build" else {
            "build-python": subjects["build"], "toolchain-install": subjects[arch + "-toolchain"]}
        output = directory / name
        try:
            result = component_resolution.python(source, graph, row, arch, kind, execution["build"], dependencies,
                cosign, output, builder, oras, docker_config)
        finally:
            if output.exists():
                component_resolution.preserve_evidence(output, report / name)
        require(result["status"] == "verified-build-component", "Python row requires a prepared raw part: " + name)
        subjects[name], resolutions[name] = result["subject"], result
    return subjects, resolutions


def preserve_qualification(directory, destination, row):
    """Retain execution diagnostics on failure without copying installed trees or OCI blobs."""
    paths = ["inputs.json", "qualification.bake.json", "receipt.json", "buildx-metadata.json",
             "payload/" + python_qualification.PROGRESS_PATH, "payload/" + python_qualification.RECORD_PATH]
    paths += ["extracted/" + name for name in ("extract.bake.json", "extract-attempt2.bake.json",
              "export-timeout-1.json", "export-timeout-2.json")]
    paths += ["payload/opt/crossforge/qualification/python/%s/%s" % (row, name) for name in python_qualification.REPORTS]
    for name in paths:
        path = directory / name
        if path.exists() or path.is_symlink():
            file_record(directory, name)
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(path), str(target), follow_symlinks=False)


def unchanged(source, producer, execution, builder, docker_config):
    require(qualification_execution.execution_identity(builder, docker_config) == execution,
            "Python row producer execution environment changed")
    current = component_ci.checked_source(source, "main")
    require(all(current[key] == producer[key] for key in ("source_commit", "source_dirty", "invocation")),
            "Python row producer source or invocation changed")


def ensure(source, row, directory, builder, oras, cosign, docker_config=None):
    directory = Path(directory).absolute()
    require(not directory.exists() and not directory.is_symlink(), "Python row producer output directory must be new")
    producer = component_ci.checked_source(source, "main")
    settings = python_qualification.spec(source, row)
    graph = component_ci.source_graph(source, [settings["target"]], directory / "source", builder, docker_config)
    execution = qualification_execution.execution_identity(builder, docker_config)
    report = directory / "report"
    subjects, raw = prepared_subjects(source, graph, row, execution, directory / "subjects", report / "subjects",
                                     builder, oras, cosign, docker_config)
    output = directory / "resolution"
    try:
        resolution = python_row_resolution.resolve(source, graph, row, execution, subjects, cosign,
            output, builder, oras, docker_config)
    finally:
        if output.exists():
            component_resolution.preserve_evidence(output, report / "row")
    result = {"schema_version": 1, "kind": "crossforge-ci-python-row-production", "row": row,
              "subjects": subjects, "raw_resolutions": raw, "resolution": resolution, "produced": False}
    if resolution["status"] == "qualification-required":
        output = directory / "qualification"
        try:
            built = python_qualification.produce(source, graph, row, execution, producer, subjects, output, builder, docker_config)
        finally:
            preserve_qualification(output, report / "qualification", row)
        receipt = load_json(output / "receipt.json")
        require(component_inputs.identity(receipt["contract"]["inputs"]) == resolution["inputs_sha256"],
                "Python row inputs changed after missing qualification was planned")
        require(built["artifact"] == receipt["artifact"], "Python row producer artifact differs from receipt")
        entry = {"reference": component_catalog.REPOSITORY + "@" + receipt["artifact"]["root_digest"],
                 "receipt_sha256": built["receipt_sha256"], "receipt": receipt}
        handoff = python_row_handoff.document(source, row, producer, execution, entry)
        unchanged(source, producer, execution, builder, docker_config)
        policy = registry_transfer.validate_tool(load_json(Path(source) / ".github/locked-tools/oras.json"))
        config = Path(docker_config or os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker") / "config.json"
        published = registry_transfer.publish(output / "oci", built["artifact"]["root_digest"],
            component_catalog.REPOSITORY, oras, policy, config)
        require(published["reference"] == entry["reference"], "published Python row reference differs")
        path = directory / "handoff.json"
        component_build.write_json(path, handoff)
        result.update(produced=True, handoff=str(path), handoff_sha256=content_sha256(handoff),
                      producer_invocation=producer["invocation"], qualification=built)
    else:
        require(resolution["status"] == "verified-qualified-row", "unsupported Python row resolution")
    unchanged(source, producer, execution, builder, docker_config)
    component_build.write_json(report / "result.json", result)
    return result
