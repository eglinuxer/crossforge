"""Freshly install a verified Python row on the canonical toolchain-only SDK."""

import copy
from datetime import datetime
import json
from pathlib import Path
import subprocess

from . import bake_materials, ci_replay, component_build, component_inputs, component_qualification
from . import python_components, python_qualification, qualification_execution
from .identity import exact_fields, file_record, load_json, require


def validate_graph(source, graph, row):
    settings = python_qualification.spec(source, row)
    root = "python-" + row + "-dev"
    definition = graph.get("target", {}).get(root, {})
    require(definition.get("dockerfile") == "docker/python.Dockerfile" and
            definition.get("target") == "python-sdk-append" and
            definition.get("contexts", {}).get("crossforge_sdk_base") == "target:sdk-toolchains-dev" and
            definition.get("contexts", {}).get("crossforge_python_row") == "target:python-row-" + row and
            all(definition.get("args", {}).get(key) == settings[field] for key, field in
                (("CPYTHON_ROW", "row"), ("CPYTHON_VERSION", "version"), ("CPYTHON_ADAPTER", "adapter"))),
            "independent row must retain its canonical installation base and arguments")
    return root, settings


def bind(source, graph, row, execution, subjects, qualification, builder, docker_config, directory):
    root, settings = validate_graph(source, graph, row)
    exact_fields(qualification, ("receipt", "receipt_sha256", "layout"), "independent row qualification")
    row_graph, row_bindings = python_components.bind_row(source, graph, row, execution["build"],
        subjects, builder, docker_config, directory)
    expected = python_qualification.inputs(source, row_graph, settings, execution, row_bindings)
    receipt = load_json(qualification["receipt"])
    verified = python_qualification.verify_local(receipt, qualification["receipt_sha256"], expected,
        source, qualification["layout"], builder, docker_config, directory)
    resolved, bindings = copy.deepcopy(graph), {}
    for arch in python_components.ARCHES:
        resolved, selected = component_qualification.bind_subjects(source, resolved,
            component_qualification.spec(arch, "toolchain"), execution,
            {"toolchain-install": subjects[arch + "-toolchain"]}, builder, docker_config, directory)
        bindings.update(selected)
    resolved["target"][root]["contexts"]["crossforge_python_row"] = verified["reference"]
    bindings[root + ":crossforge_python_row"] = {"component": expected["component"],
        "inputs_sha256": component_inputs.identity(expected), "artifact_digest": receipt["artifact"]["platform_digest"]}
    return resolved, bindings, verified


def inputs(source, graph, row, execution, bindings):
    root = "python-" + row + "-dev"
    value = bake_materials.capture(source, graph, root, "qualification/" + root, "qualification",
        sorted(arch + "-unknown-linux-gnu" for arch in python_components.ARCHES), execution, artifacts=bindings)
    require({item["component"] for item in value["dependencies"]} ==
            {"toolchain/x86_64-install", "toolchain/aarch64-install", "qualification/python-" + row},
            "independent append requires exactly two raw toolchains and one verified row")
    files = {item["path"]: item for item in value["files"]}
    require(not {"scripts/build-gcc.sh", "scripts/build-cpython-native.sh", "scripts/build-cpython-cross.sh"} & set(files),
            "independent append still includes source compilers")
    path = "scripts/crossforge_internal/python_row_install.py"
    files[path] = file_record(source, path)
    value["files"] = [files[path] for path in sorted(files)]
    instructions = value["parameters"]["recipes"][root]["stages"]["python-sdk-append"]
    count = sum(line.startswith("RUN ") for line in instructions)
    require(count == 2, "canonical independent append RUN count differs")
    value["parameters"]["replay"] = {root: {"python-sdk-append": {"runs": count, "marker": '--row "' + row + '"'}}}
    return component_inputs.validate(value)


def execute(source, graph, row, execution, subjects, qualification, directory, builder, docker_config=None):
    """Reverify the sealed row and execute both original independent append RUNs."""
    source, directory = Path(source).resolve(), Path(directory).absolute()
    require(not directory.exists() and not directory.is_symlink(), "independent installation output must be new")
    root, _ = validate_graph(source, graph, row)
    require(qualification_execution.execution_identity(builder, docker_config) == execution,
            "independent installation execution environment differs")
    directory.mkdir(parents=True)
    resolved, bindings, verified = bind(source, graph, row, execution, subjects, qualification,
        builder, docker_config, directory)
    expected = inputs(source, resolved, row, execution, bindings)
    component_build.write_json(directory / "inputs.json", expected)
    component_build.write_json(directory / "reused-row.json", verified)
    build = copy.deepcopy(resolved)
    build.pop("group", None)
    build["target"] = {name: value for name, value in build["target"].items()
                       if name in expected["parameters"]["bake_targets"]}
    for target in build["target"].values():
        for field in ("cache-to", "tags", "attest", "no-cache", "no-cache-filter"):
            target.pop(field, None)
        target["output"] = [{"type": "cacheonly"}]
    build["target"][root]["no-cache-filter"] = ["python-sdk-append"]
    payload = directory / "payload"
    payload.mkdir()
    frontend = expected["parameters"]["recipes"][root]["frontend"]
    report_path = "/opt/crossforge/qualification/python/" + row + "/row.json"
    build["target"]["independent-install-report"] = {"context": ".", "platforms": ["linux/amd64"],
        "dockerfile-inline": "# syntax=" + frontend + "\nFROM scratch\nCOPY --from=sdk " +
                            json.dumps([report_path, "/row.json"]) + "\n",
        "contexts": {"sdk": "target:" + root}, "output": [{"type": "local", "dest": str(payload)}]}
    component_build.write_json(directory / "installation.bake.json", build)
    started = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with (directory / "execution.jsonl").open("x", encoding="utf-8") as progress:
        subprocess.run(component_build.docker_command(docker_config) + ["buildx", "bake", "--builder", builder,
            "--allow=fs.write=" + str(payload), "-f", str(directory / "installation.bake.json"),
            "independent-install-report", "--progress=rawjson"], cwd=str(source), stderr=progress, check=True)
    completed = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    vertices = ci_replay.fresh_vertices(directory / "execution.jsonl", expected["parameters"]["replay"], started, completed)
    report = file_record(payload, "row.json")
    require(report["sha256"] == verified["qualification"]["coverage"]["manifest_sha256"],
            "installed row manifest differs from independently verified row")
    component_inputs.require_match(expected, inputs(source, resolved, row, execution, bindings))
    require(qualification_execution.execution_identity(builder, docker_config) == execution,
            "independent installation execution environment changed")
    result = {"schema_version": 1, "kind": "crossforge-component-python-installation", "mode": "executed",
        "row": row, "target": root, "row_receipt_sha256": qualification["receipt_sha256"],
        "inputs_sha256": component_inputs.identity(expected), "started_at": started, "completed_at": completed,
        "vertices": vertices, "report": report, "reused_row": verified,
        "scope": "independent installation on sdk-toolchains-dev; not candidate or native ARM qualification"}
    component_build.write_json(directory / "result.json", result)
    return result
