"""Bind a public SDK to authenticated raw components, retaining every gate."""

from pathlib import Path
import runpy
import subprocess
import tempfile

from . import bake_materials, component_build, component_ci, component_inputs, component_recovery, component_resolution
from . import candidate_qualification, qualification_execution
from .identity import content_sha256, digest_value, exact_fields, file_record, load_json, require


TARGET = "sdk-candidate"
STAGE = "candidate-sdk"
COMPILERS = {"scripts/build-gcc.sh", "scripts/build-cpython-native.sh", "scripts/build-cpython-cross.sh"}


def source_binding(source, path, commit):
    source = Path(source)
    policy = runpy.run_path(str(source / "scripts/source_binding.py"))
    release = load_json(source / "config/release.json")
    value = load_json(path)
    policy["validate_binding"](value, release, load_json(source / "config/schemas/source-binding.schema.json"), commit)
    return value


def publication_graph(source, binding, commit, directory, builder, docker_config=None):
    graph = component_ci.source_graph(source, [TARGET], directory, builder, docker_config)
    definition = graph.get("target", {}).get(TARGET, {})
    require(definition.get("dockerfile") == "docker/packaging.Dockerfile" and definition.get("target") == TARGET,
            "candidate must retain the canonical SDK publication stage")
    definition.setdefault("args", {}).update(CROSSFORGE_SOURCE_COMMIT=commit,
        CROSSFORGE_SOURCE_BUNDLE_DIGEST=binding["digest"],
        CROSSFORGE_SOURCE_BUNDLE_PLATFORM_MANIFEST_DIGEST=binding["platform_manifest_digest"],
        CROSSFORGE_SOURCE_ARCHIVE_SHA256=binding["archive"]["sha256"],
        CROSSFORGE_SOURCE_ARCHIVE_SIZE=str(binding["archive"]["size"]))
    for item in graph["target"].values():
        require(not item.get("tags") and not item.get("cache-to") and
                all(value.get("type") == "cacheonly" for value in item.get("output", [])),
                "component preparation must not contain an image or cache exporter")
    return graph


def reparse(source, path, builder, docker_config=None):
    command = component_build.docker_command(docker_config) + ["buildx", "bake", "--builder", builder,
        "-f", str(path), "--print", TARGET]
    from .identity import parse_json
    return parse_json(subprocess.check_output(command, cwd=str(source)))


def identities(graph, results):
    """Describe only contexts that remain reachable after producer replacement."""
    contexts = {}
    for result in results.values():
        require(result["status"] == "verified-build-component", "candidate component is not verified")
        receipt = load_json(result["subject"]["receipt"])
        require(content_sha256(receipt) == result["subject"]["receipt_sha256"], "verified candidate receipt changed")
        record = {"component": result["component"], "inputs_sha256": result["inputs_sha256"],
                  "artifact_digest": receipt["artifact"]["platform_digest"]}
        reference = result["context"]
        require(reference not in contexts or contexts[reference] == record, "ambiguous candidate component context")
        contexts[reference] = record
    return {target + ":" + context: contexts[reference]
        for target, definition in graph["target"].items()
        for context, reference in definition.get("contexts", {}).items() if reference in contexts}


def capture(source, graph, execution, bindings):
    value = bake_materials.capture(source, graph, TARGET, "qualification/candidate-sdk", "qualification",
        ["aarch64-unknown-linux-gnu", "x86_64-unknown-linux-gnu"], execution, artifacts=bindings)
    require(not COMPILERS & {item["path"] for item in value["files"]},
            "candidate component graph still includes GCC or CPython source compilation")
    require(graph["target"][TARGET].get("target") == TARGET, "candidate publication stage changed")
    files = {item["path"]: item for item in value["files"]}
    for name in ("candidate_components", "candidate_qualification", "ci_replay", "qualification_execution"):
        path = "scripts/crossforge_internal/" + name + ".py"
        files[path] = file_record(source, path)
    value["files"] = [files[path] for path in sorted(files)]
    return value


def validate_selection(value, commit):
    component_recovery.validate(value)
    require(value["context"]["stage"] == STAGE and value["context"]["targets"] == [TARGET] and
            value["context"]["source_commit"] == commit and value["components"],
            "candidate component selection belongs to another source or stage")
    return value


def prepare(source, binding_path, directory, data, builder, oras, cosign, docker_config=None):
    directory, data = Path(directory).absolute(), Path(data).absolute()
    require(not directory.exists() and not directory.is_symlink(), "candidate component diagnostics must be new")
    require(not data.exists() and not data.is_symlink() and data != directory and
            data not in directory.parents and directory not in data.parents, "component OCI data must be outside candidate diagnostics")
    producer = component_ci.checked_source(source, "candidate")
    commit = producer["source_commit"]
    binding = source_binding(source, binding_path, commit)
    graph = publication_graph(source, binding, commit, directory / "source", builder, docker_config)
    execution = component_build.execution_identity(builder, docker_config)
    qualified_execution = qualification_execution.execution_identity(builder, docker_config)
    require(qualified_execution["build"] == execution, "candidate build and qualification environments differ")
    context = component_recovery.context(source, graph, STAGE, [TARGET], execution, commit)
    required = component_recovery.requirements(source, graph, True)
    resolved, toolchains = component_resolution.bind_toolchains(source, graph, execution, cosign,
        data, directory / "toolchains", builder, oras, docker_config)
    resolved, python = component_resolution.bind_python(source, graph, resolved, toolchains["components"], execution, cosign,
        data / "python", directory / "python", builder, oras, docker_config)
    require(not toolchains["required_producers"] and not python["required_producers"],
            "candidate requires all centrally prepared raw components; source fallback is disabled")
    results = dict(toolchains["components"], **python["components"])
    selection = validate_selection(component_recovery.document(context, results, required), commit)
    # Resolve linked targets again so removed compiler subgraphs and their
    # unused contexts cannot survive in the actual publication graph.
    intermediate = directory / "resolved.bake.json"
    component_build.write_json(intermediate, resolved)
    final = reparse(source, intermediate, builder, docker_config)
    bindings = identities(final, results)
    inputs = capture(source, final, qualified_execution, bindings)
    qualification = candidate_qualification.plan(source, inputs)
    final = candidate_qualification.override(final, qualification)
    ready = {"schema_version": 2, "kind": "crossforge-candidate-component-binding", "source_commit": commit,
        "source_binding_sha256": content_sha256(binding), "execution": execution, "bindings": bindings,
        "graph_sha256": content_sha256(final), "inputs": inputs, "selection": selection,
        "qualification_execution": qualified_execution, "qualification": qualification}
    component_build.write_json(directory / "components.bake.json", final)
    component_build.write_json(directory / "ready.json", ready)
    component_build.write_json(directory / "component-selection.json", selection)
    check(source, binding_path, directory, content_sha256(ready), builder, docker_config)
    return ready


def check(source, binding_path, directory, sha256, builder, docker_config=None):
    directory = Path(directory)
    digest_value(sha256, "independent candidate binding SHA256")
    ready = load_json(directory / "ready.json")
    exact_fields(ready, ("schema_version", "kind", "source_commit", "source_binding_sha256", "execution", "bindings",
                        "graph_sha256", "inputs", "selection", "qualification_execution", "qualification"), "candidate component binding")
    require(type(ready["schema_version"]) is int and ready["schema_version"] == 2 and
            ready["kind"] == "crossforge-candidate-component-binding" and content_sha256(ready) == sha256,
            "candidate binding document differs")
    producer = component_ci.checked_source(source, "candidate")
    commit = producer["source_commit"]
    binding = source_binding(source, binding_path, commit)
    execution = component_build.execution_identity(builder, docker_config)
    qualified_execution = qualification_execution.execution_identity(builder, docker_config)
    require(ready["source_commit"] == commit and ready["source_binding_sha256"] == content_sha256(binding) and
            ready["execution"] == execution and ready["qualification_execution"] == qualified_execution and
            qualified_execution["build"] == execution, "candidate source, source bundle or execution environment changed")
    with tempfile.TemporaryDirectory(prefix="candidate-source-", dir=str(directory.parent)) as temporary:
        graph = publication_graph(source, binding, commit, Path(temporary) / "source", builder, docker_config)
    context = component_recovery.context(source, graph, STAGE, [TARGET], execution, commit)
    require(ready["selection"]["context"] == context, "candidate source graph changed")
    component_recovery.validate(ready["selection"], component_recovery.requirements(source, graph, True))
    validate_selection(ready["selection"], commit)
    require(load_json(directory / "component-selection.json") == ready["selection"], "candidate component selection changed")
    resolved = load_json(directory / "components.bake.json")
    require(content_sha256(resolved) == ready["graph_sha256"], "candidate component graph changed")
    expected = capture(source, resolved, qualified_execution, ready["bindings"])
    component_inputs.require_match(ready["inputs"], expected)
    require(ready["qualification"] == candidate_qualification.plan(source, expected) and
            resolved == candidate_qualification.override(resolved, ready["qualification"]),
            "candidate qualification coverage or forced stages changed")
    return ready
