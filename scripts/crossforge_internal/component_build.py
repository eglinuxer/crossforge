"""Local toolchain artifact producer/consumer using the canonical Bake graph."""

import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from . import bake_materials, component_artifacts, component_inputs, oci_layout
from .identity import (content_sha256, digest_value, file_record, load_json, require)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")


def toolchain_spec(arch, role):
    require(arch in ("x86_64", "aarch64"), "unsupported toolchain architecture")
    require(role in ("toolchain-install", "gcc-test-context"), "unsupported toolchain artifact role")
    suffix = "install" if role == "toolchain-install" else "test-context"
    return {"component": "toolchain/%s-%s" % (arch, suffix),
            "target": "toolchain-%s-build-export" % arch if role == "toolchain-install" else "gcc-%s-test-context-export" % arch,
            "triple": arch + "-unknown-linux-gnu",
            "copies": ["/opt/crossforge/"] if role == "toolchain-install" else [
                "/work/prepared/gcc/", "/work/build/gcc-%s/" % arch]}


def toolchain_inputs(source, graph, arch, role, execution):
    spec = toolchain_spec(arch, role)
    require(type(execution) is dict and "buildkit_image" in execution, "declare pinned BuildKit execution image")
    reference = execution["buildkit_image"]
    require(type(reference) is str and reference.count("@") == 1, "BuildKit execution image must be pinned")
    digest_value(reference.rsplit("@", 1)[1], "BuildKit execution image digest", oci=True)
    require(type(graph) is dict and type(graph.get("target")) is dict and
            type(graph["target"].get(spec["target"])) is dict and
            graph["target"][spec["target"]].get("target") == spec["target"],
            "toolchain artifact must use its canonical Docker stage")
    value = bake_materials.capture(source, graph, spec["target"], spec["component"], role,
                                   [spec["triple"]], execution)
    paths = {record["path"] for record in value["files"]}
    for name in ("component_build.py", "component_artifacts.py", "oci_layout.py", "__init__.py"):
        paths.add("scripts/crossforge_internal/" + name)
    paths.add("scripts/component-artifact.py")
    value["files"] = [file_record(source, path) for path in sorted(paths)]
    value["parameters"]["artifact_copies"] = spec["copies"]
    return component_inputs.validate(value)


def plan_toolchain(source, graph, arch, role, execution, producer, directory):
    """Create an isolated local OCI export; no tags or registry outputs survive."""
    source, directory = Path(source).resolve(), Path(directory).resolve()
    require(not directory.exists(), "component output directory must be new")
    spec = toolchain_spec(arch, role)
    inputs = toolchain_inputs(source, graph, arch, role, execution)
    contract = component_artifacts.contract(role, inputs, producer)
    frontend = inputs["parameters"]["recipes"][spec["target"]]["frontend"]
    metadata = directory / "metadata"
    write_json(metadata / component_artifacts.CONTRACT_PATH, contract)
    write_json(directory / "inputs.json", inputs)
    # Freeze the resolved target graph used for both the build and revalidation.
    build_graph = copy.deepcopy(graph)
    build_graph.pop("group", None)
    for target in build_graph["target"].values():
        for field in ("cache-to", "tags", "attest"):
            target.pop(field, None)
        target["output"] = [{"type": "cacheonly"}]
    lines = ["# syntax=" + frontend, "FROM scratch"]
    lines.extend("COPY --from=payload " + json.dumps([path, path]) for path in spec["copies"])
    lines.append("COPY --from=metadata [\"/component/contract.json\", \"/component/contract.json\"]")
    build_graph["target"]["component-artifact"] = {
        "context": ".", "dockerfile-inline": "\n".join(lines) + "\n",
        "platforms": ["linux/amd64"],
        "contexts": {"payload": "target:" + spec["target"], "metadata": str(metadata)},
        "output": [{"type": "oci", "dest": str(directory / "oci"), "tar": False}]}
    write_json(directory / "source-graph.json", graph)
    write_json(directory / "producer.bake.json", build_graph)
    return contract


def docker_command(config=None):
    return ["docker"] + (["--config", str(config)] if config is not None else [])


def execution_identity(builder, docker_config=None):
    """Observe the supported single-node docker-container execution boundary."""
    command = docker_command(docker_config)
    description = subprocess.check_output(command + ["buildx", "inspect", builder]).decode("utf-8")
    require(re.search(r"^Driver:\s+docker-container\s*$", description, re.M),
            "local component producer requires the docker-container driver")
    names = re.findall(r"^Name:\s+(\S+)\s*$", description, re.M)
    require(len(names) == 2, "local component producer requires one BuildKit node")
    container = "buildx_buildkit_" + names[1]
    config = json.loads(subprocess.check_output(command + ["inspect", container]))[0]["Config"]
    reference = config["Image"]
    require(reference.count("@") == 1, "running BuildKit image must be digest-pinned")
    digest_value(reference.rsplit("@", 1)[1], "running BuildKit image digest", oci=True)
    environment = dict(item.split("=", 1) for item in config["Env"])
    daemon_config = subprocess.check_output(command + ["exec", container, "cat", "/etc/buildkit/buildkitd.toml"])
    return {"buildkit_image": reference,
            "buildx_version": subprocess.check_output(command + ["buildx", "version"]).decode("utf-8").strip(),
            "buildkit_config_sha256": hashlib.sha256(daemon_config).hexdigest(),
            "gomemlimit": environment.get("GOMEMLIMIT", "")}


def extract_metadata(layout, observation, paths, frontend, directory, builder, docker_config=None):
    directory = Path(directory).resolve()
    require(not directory.exists(), "metadata extraction directory must be new")
    directory.mkdir(parents=True)
    destination = directory / "files"
    destination.mkdir()
    reference = "oci-layout://%s@%s" % (Path(layout).resolve(), observation["root_digest"])
    graph = oci_layout.metadata_graph(reference, {path: path for path in paths}, destination, frontend)
    write_json(directory / "extract.bake.json", graph)
    subprocess.run(docker_command(docker_config) + ["buildx", "bake", "--builder", builder,
        "--allow=fs.write=" + str(destination), "-f", str(directory / "extract.bake.json"),
        "component-metadata", "--progress=plain"], cwd=str(directory), check=True)
    return destination


def _build_digest(path):
    metadata = load_json(path)
    require(type(metadata) is dict and type(metadata.get("component-artifact")) is dict,
            "component Bake metadata target is missing")
    return digest_value(metadata["component-artifact"].get("containerimage.digest"),
                        "component Bake image digest", oci=True)


def produce_toolchain(source, graph, arch, role, execution, producer, directory,
                      builder, docker_config=None):
    """Build, recheck materials, extract metadata with BuildKit, and seal locally."""
    directory, source = Path(directory).resolve(), Path(source).resolve()
    require(execution_identity(builder, docker_config) == execution,
            "running component execution environment differs from planned identity")
    contract = plan_toolchain(source, graph, arch, role, execution, producer, directory)
    metadata_file = directory / "buildx-metadata.json"
    subprocess.run(docker_command(docker_config) + ["buildx", "bake", "--builder", builder,
        "--allow=fs.read=" + str(directory / "metadata"), "--allow=fs.write=" + str(directory),
        "-f", str(directory / "producer.bake.json"), "component-artifact", "--progress=plain",
        "--metadata-file", str(metadata_file)], cwd=str(source), check=True)
    expected = toolchain_inputs(source, graph, arch, role, execution)
    component_inputs.require_match(contract["inputs"], expected)
    observation = oci_layout.inspect(directory / "oci", _build_digest(metadata_file))
    paths = [component_artifacts.CONTRACT_PATH]
    frontend = expected["parameters"]["recipes"][toolchain_spec(arch, role)["target"]]["frontend"]
    extracted = extract_metadata(directory / "oci", observation, paths, frontend,
                                 directory / "extracted", builder, docker_config)
    receipt = component_artifacts.receipt(contract, observation, extracted, paths)
    write_json(directory / "receipt.json", receipt)
    return {"receipt": str(directory / "receipt.json"), "receipt_sha256": content_sha256(receipt),
            "inputs_sha256": component_inputs.identity(expected), "artifact": receipt["artifact"],
            "qualification": "not asserted by an artifact receipt"}


def verify_local(receipt, trusted_sha256, expected_inputs, role, layout, frontend,
                 builder, docker_config=None, temporary_parent=None):
    digest_value(trusted_sha256, "trusted receipt SHA256")
    require(content_sha256(component_artifacts.validate_receipt(receipt)) == trusted_sha256,
            "receipt digest differs from trusted reference")
    require(receipt["contract"]["role"] == role, "component artifact role differs")
    component_inputs.require_match(receipt["contract"]["inputs"], expected_inputs)
    observation = oci_layout.inspect(layout, receipt["artifact"]["root_digest"])
    paths = [record["path"] for record in receipt["metadata"]]
    with tempfile.TemporaryDirectory(prefix="crossforge-component-verify-", dir=temporary_parent) as temporary:
        extracted = extract_metadata(layout, observation, paths, frontend, Path(temporary) / "extract",
                                     builder, docker_config)
        digest = component_artifacts.verify_receipt(receipt, trusted_sha256, expected_inputs,
            role, observation, extracted, [component_artifacts.CONTRACT_PATH])
    return "oci-layout://%s@%s" % (Path(layout).resolve(), digest)
