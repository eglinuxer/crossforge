"""Assemble the complete Python matrix from independently verified row receipts."""

import copy
from datetime import datetime
import json
from pathlib import Path
import runpy
import subprocess

from . import bake_materials, component_build, component_inputs, component_qualification
from . import python_components, python_qualification, qualification_execution
from .identity import exact_fields, file_record, load_json, require


ROOTS = {"python-dev": ("docker/python.Dockerfile", "python-sdk-final"),
         "sdk-complete-dev": ("docker/packaging.Dockerfile", "sdk-complete-dev")}


def matrix(source):
    table = runpy.run_path(str(Path(source) / "scripts/python_row_contract.py"))
    return [record["row"] for record in table["IMPLEMENTED_ROWS"]]


def _stage(graph, target, dockerfile, stage):
    definition = graph.get("target", {}).get(target, {})
    require(definition.get("dockerfile") == dockerfile and definition.get("target") == stage,
            "SDK component consumer must use its canonical stage: " + target)
    return definition


def bind(source, graph, root, execution, components, builder, docker_config=None, temporary_parent=None):
    require(root in ROOTS, "unsupported component SDK root")
    _stage(graph, root, *ROOTS[root])
    rows = matrix(source)
    final = _stage(graph, "python-dev", *ROOTS["python-dev"])
    require(final.get("args", {}).get("CROSSFORGE_PYTHON_ROWS") == " ".join(rows), "SDK row order or coverage differs")
    exact_fields(components, ("toolchains", "rows"), "SDK components")
    exact_fields(components["toolchains"], python_components.ARCHES, "SDK toolchains")
    exact_fields(components["rows"], rows, "SDK Python rows")
    previous = "sdk-toolchains-dev"
    for row in rows:
        value = components["rows"][row]
        exact_fields(value, ("subjects", "qualification"), "SDK row inputs")
        exact_fields(value["qualification"], ("receipt", "receipt_sha256", "layout"), "SDK row qualification")
        for arch in python_components.ARCHES:
            require(value["subjects"].get(arch + "-toolchain", {}).get("receipt_sha256") ==
                    components["toolchains"][arch].get("receipt_sha256"),
                    "SDK and Python row use different toolchain receipts: " + row + ":" + arch)
        target = "python-dev-append-" + row
        definition = _stage(graph, target, "docker/python.Dockerfile", "python-sdk-append")
        settings = python_qualification.spec(source, row)
        require(all(definition.get("args", {}).get(key) == settings[field] for key, field in
                    (("CPYTHON_ROW", "row"), ("CPYTHON_VERSION", "version"), ("CPYTHON_ADAPTER", "adapter"))),
                "SDK append row arguments differ: " + row)
        require(definition.get("contexts", {}).get("crossforge_sdk_base") == "target:" + previous,
                "SDK append chain differs: " + row)
        require(definition.get("contexts", {}).get("crossforge_python_row") == "target:python-row-" + row,
                "SDK row source boundary differs: " + row)
        previous = target
    require(final.get("contexts", {}).get("crossforge_sdk_base") == "target:" + previous,
            "SDK final stage does not consume the complete row chain")
    resolved, bindings, reused = copy.deepcopy(graph), {}, {}
    for arch in python_components.ARCHES:
        resolved, selected = component_qualification.bind_subjects(source, resolved,
            component_qualification.spec(arch, "toolchain"), execution,
            {"toolchain-install": components["toolchains"][arch]}, builder, docker_config, temporary_parent)
        bindings.update(selected)
    for row in rows:
        value = components["rows"][row]
        row_graph, row_bindings = python_components.bind_row(source, graph, row, execution["build"],
            value["subjects"], builder, docker_config, temporary_parent)
        settings = python_qualification.spec(source, row)
        expected = python_qualification.inputs(source, row_graph, settings, execution, row_bindings)
        subject = value["qualification"]
        receipt = load_json(subject["receipt"])
        verified = python_qualification.verify_local(receipt, subject["receipt_sha256"], expected, source,
            subject["layout"], builder, docker_config, temporary_parent)
        target = "python-dev-append-" + row
        resolved["target"][target]["contexts"]["crossforge_python_row"] = verified["reference"]
        bindings[target + ":crossforge_python_row"] = {"component": expected["component"],
            "inputs_sha256": component_inputs.identity(expected), "artifact_digest": receipt["artifact"]["platform_digest"]}
        reused[row] = verified
    return resolved, bindings, reused


def inputs(source, graph, root, execution, bindings):
    value = bake_materials.capture(source, graph, root, "qualification/" + root, "qualification",
        sorted(arch + "-unknown-linux-gnu" for arch in python_components.ARCHES), execution, artifacts=bindings)
    files = {record["path"]: record for record in value["files"]}
    require(not {"scripts/build-gcc.sh", "scripts/build-cpython-native.sh", "scripts/build-cpython-cross.sh"} & set(files),
            "SDK component consumer still includes GCC or CPython source compilation")
    path = "scripts/crossforge_internal/python_sdk.py"
    files[path] = file_record(source, path)
    value["files"] = [files[path] for path in sorted(files)]
    replay = {"python-dev-append-" + row: "python-sdk-append" for row in matrix(source)}
    replay["python-dev"] = "python-sdk-final"
    if root == "sdk-complete-dev":
        replay[root] = root
    runs = {}
    for target, stage in replay.items():
        instructions = value["parameters"]["recipes"].get(target, {}).get("stages", {}).get(stage, [])
        count = sum(line.startswith("RUN ") for line in instructions)
        require(count > 0, "SDK integration replay stage has no RUN: " + target)
        runs[stage] = runs.get(stage, 0) + count
    value["parameters"].update(replay=replay, required_runs=runs)
    return component_inputs.validate(value)


def execute(source, graph, root, execution, components, directory, builder, docker_config=None):
    """Reverify every row, then freshly execute SDK integration without publishing."""
    source, directory = Path(source).resolve(), Path(directory).resolve()
    require(not directory.exists(), "component SDK output directory must be new")
    require(qualification_execution.execution_identity(builder, docker_config) == execution, "SDK execution environment differs")
    resolved, bindings, reused = bind(source, graph, root, execution, components, builder, docker_config, directory.parent)
    expected = inputs(source, resolved, root, execution, bindings)
    component_build.write_json(directory / "inputs.json", expected)
    component_build.write_json(directory / "reused-rows.json", reused)
    build = copy.deepcopy(resolved)
    build.pop("group", None)
    build["target"] = {name: value for name, value in build["target"].items() if name in expected["parameters"]["bake_targets"]}
    for definition in build["target"].values():
        for field in ("cache-to", "tags", "attest", "no-cache", "no-cache-filter"):
            definition.pop(field, None)
        definition["output"] = [{"type": "cacheonly"}]
    for target, stage in expected["parameters"]["replay"].items():
        build["target"][target]["no-cache-filter"] = [stage]
    reports = {"/opt/crossforge/qualification/%s.json" % ("final-sdk" if root == "python-dev" else "python-final-sdk"):
               "reports/python-final-sdk.json"}
    if root == "sdk-complete-dev":
        reports["/opt/crossforge/qualification/complete-sdk.json"] = "reports/complete-sdk.json"
    reports.update({"/opt/crossforge/qualification/python/%s/row.json" % row: "reports/%s.json" % row for row in matrix(source)})
    frontend = expected["parameters"]["recipes"][root]["frontend"]
    lines = ["# syntax=" + frontend, "FROM scratch"]
    lines += ["COPY --from=sdk " + json.dumps([src, "/" + dst]) for src, dst in sorted(reports.items())]
    payload = directory / "payload"
    payload.mkdir()
    build["target"]["sdk-component-reports"] = {"context": ".", "dockerfile-inline": "\n".join(lines) + "\n",
        "platforms": ["linux/amd64"], "contexts": {"sdk": "target:" + root},
        "output": [{"type": "local", "dest": str(payload)}]}
    component_build.write_json(directory / "sdk.bake.json", build)
    started = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with (directory / "execution.jsonl").open("x", encoding="utf-8") as progress:
        subprocess.run(component_build.docker_command(docker_config) + ["buildx", "bake", "--builder", builder,
            "--allow=fs.write=" + str(payload), "-f", str(directory / "sdk.bake.json"), "sdk-component-reports",
            "--progress=rawjson"], cwd=str(source), stderr=progress, check=True)
    completed = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    require(qualification_execution.execution_identity(builder, docker_config) == execution, "SDK execution environment changed")
    component_inputs.require_match(expected, inputs(source, resolved, root, execution, bindings))
    vertices = qualification_execution.fresh_vertices(directory / "execution.jsonl",
        expected["parameters"]["required_runs"], started, completed)
    for row, prior in reused.items():
        require(file_record(payload, "reports/%s.json" % row)["sha256"] == prior["qualification"]["coverage"]["manifest_sha256"],
                "assembled SDK row manifest differs from its qualified artifact: " + row)
    result = {"kind": "crossforge-component-sdk-integration", "schema_version": 1, "mode": "executed", "target": root,
              "inputs_sha256": component_inputs.identity(expected), "started_at": started, "completed_at": completed,
              "vertices": vertices, "reports": [file_record(payload, path) for path in sorted(reports.values())],
              "reused_rows": reused, "scope": "local SDK integration; not candidate or native ARM qualification"}
    component_build.write_json(directory / "result.json", result)
    return result
