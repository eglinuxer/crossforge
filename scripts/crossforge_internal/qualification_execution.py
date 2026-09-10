"""Observe the supported runner boundary and verify fresh BuildKit RUN events."""

from datetime import datetime
import json
import re
import subprocess

from . import component_build
from .identity import content_sha256, digest_value, parse_json, require


def execution_identity(builder, docker_config=None):
    build = component_build.execution_identity(builder, docker_config)
    command = component_build.docker_command(docker_config)
    description = subprocess.check_output(command + ["buildx", "inspect", builder]).decode("utf-8")
    names = re.findall(r"^Name:\s+(\S+)\s*$", description, re.M)
    require(len(names) == 2, "qualification requires one BuildKit node")
    container = "buildx_buildkit_" + names[1]
    observed = json.loads(subprocess.check_output(command + ["inspect", container]))[0]
    info = json.loads(subprocess.check_output(command + ["info", "--format", "{{json .}}"] ))
    require(info["OSType"] == "linux" and info["Architecture"] in ("x86_64", "amd64"),
            "qualification host must be Linux x86_64")
    cpuinfo = subprocess.check_output(command + ["exec", container, "cat", "/proc/cpuinfo"]).decode("utf-8")
    cpu_keys = {"vendor_id", "cpu family", "model", "model name", "stepping", "microcode", "flags", "bugs"}
    cpus = []
    for block in cpuinfo.split("\n\n"):
        cpu = {key.strip(): " ".join(value.split()) for line in block.splitlines() if ":" in line
               for key, value in [line.split(":", 1)] if key.strip() in cpu_keys}
        if cpu:
            require(cpu_keys <= set(cpu), "qualification CPU identity is incomplete")
            cpus.append(cpu)
    require(cpus, "qualification CPU identity is missing")
    limits = ("NanoCpus", "CpuQuota", "CpuPeriod", "CpuShares", "CpusetCpus", "CpusetMems", "Memory",
              "MemorySwap", "MemoryReservation", "PidsLimit", "Ulimits", "ShmSize", "Privileged",
              "SecurityOpt", "CapAdd", "CapDrop", "CgroupnsMode", "UsernsMode", "ReadonlyRootfs")
    host = {key: info[key] for key in ("ServerVersion", "KernelVersion", "OperatingSystem", "OSType",
                                      "Architecture", "NCPU", "MemTotal", "CgroupDriver", "CgroupVersion", "Driver")}
    host["cpu_identity_sha256"] = content_sha256(cpus)
    host["buildkit_host_config"] = {key: observed["HostConfig"][key] for key in limits}
    return {"build": build, "host": host}


def timestamp(value):
    require(type(value) is str and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z", value),
            "execution event timestamp must be UTC")
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        require(False, "invalid execution event timestamp")


def fresh_vertices(path, required_runs, started_at, completed_at):
    """Inspect rawjson from the pinned BuildKit client; logs are still trust-bound."""
    start, end = timestamp(started_at), timestamp(completed_at)
    require(start <= end, "qualification execution interval is reversed")
    vertices = {}
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            event = parse_json(line)
            require(type(event) is dict, "BuildKit progress event must be an object")
            for vertex in event.get("vertexes", []):
                require(type(vertex) is dict, "BuildKit vertex must be an object")
                digest = digest_value(vertex.get("digest"), "BuildKit vertex digest", oci=True)
                previous = vertices.get(digest, {})
                # A later update must never hide a cached or failed observation.
                vertex = dict(vertex, cached=bool(vertex.get("cached") or previous.get("cached")),
                              error=vertex.get("error") or previous.get("error", ""))
                vertices[digest] = vertex
    result = []
    for stage, count in sorted(required_runs.items()):
        matched = []
        pattern = re.compile(r"^\[(?:\S+ )?" + re.escape(stage) + r" \d+/\d+\] RUN ")
        for digest, vertex in vertices.items():
            if not pattern.match(vertex.get("name", "")):
                continue
            require(not vertex["cached"] and not vertex["error"], "qualification RUN was cached or failed: " + stage)
            require(start <= timestamp(vertex.get("started")) <= timestamp(vertex.get("completed")) <= end,
                    "qualification RUN did not complete within this execution: " + stage)
            matched.append({"stage": stage, "digest": digest, "name": vertex["name"],
                            "started": vertex["started"], "completed": vertex["completed"], "cached": False})
        require(len(matched) == count and count > 0, "qualification RUN coverage differs: " + stage)
        result.extend(sorted(matched, key=lambda vertex: vertex["digest"]))
    require(result, "qualification execution has no RUN evidence")
    return result
