#!/usr/bin/env python3
"""Run bounded hosted-runner Bake stages; caches are never release evidence."""

import argparse
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
HEARTBEAT = runpy.run_path(str(ROOT / "scripts/run-with-heartbeat.py"))
STAGES = {
    "inputs": [
        "validate", "platform-python-check", "sigstore-sources-qualified",
        "rpm-source-lock-validated", "host-build-common-locked",
        "host-gcc-build-locked", "host-gcc-test-locked", "host-python-build-locked",
        "qemu-source-qualified", "qemu-aarch64-validated",
        "host-runtime-qualified", "ninja-host-tool", "cmake-source",
        "cmake-host-tool", "vcpkg-source",
        "sysroot-x86_64", "sysroot-aarch64", "python-runtime-clean-x86_64",
        "python-runtime-clean-aarch64",
    ],
    "toolchain-x86_64": ["toolchain-x86_64-dev"],
    "toolchain-aarch64": ["toolchain-aarch64-dev"],
    **{"python-" + row: ["python-" + row + "-dev"]
       for row in ("cp39", "cp310", "cp311", "cp312", "cp313", "cp314")},
    "vcpkg": ["vcpkg-upstream-tier3-qualified"],
    "sdk": ["python-matrix", "sdk-complete-dev"],
    "gcc-smoke": ["gcc-testsuite-smoke"],
    "gcc-full": ["gcc-testsuite-full-qualified"],
    "qt-inputs": ["qt-rpm-locked", "xcb-util-cursor-host-build"],
    "qt-host-webengine": ["qt-host-webengine-build"],
    "qt-host": ["qt-host-qualified"],
    "qt-x86_64": ["qt-x86_64-runtime-qualified"],
    "qt-aarch64": ["qt-aarch64-runtime-qualified"],
}
BAKE = ["docker", "buildx", "bake", "-f", "docker-bake.hcl",
        "-f", "docker-bake.override.json"]
# Component jobs retain intermediate caches. Aggregate SDK roots must not
# compress every compiler and Python build tree again on one hosted disk.
FINAL_STAGE_CACHE_TARGETS = {"python-dev", "sdk-complete-dev"}


def require_writer(environment):
    if not (
        environment.get("GITHUB_REPOSITORY") == "eglinuxer/crossforge"
        and environment.get("GITHUB_REF") == "refs/heads/main"
        and environment.get("GITHUB_EVENT_NAME") in ("push", "schedule", "workflow_dispatch")
    ):
        raise ValueError("cache writes require a trusted main qualification run")


def read_graph(targets):
    return json.loads(subprocess.check_output(BAKE + ["--print", *targets], cwd=ROOT))


def graph_roots(graph):
    result = set()

    def visit(name, stack):
        if name in stack:
            raise ValueError("cyclic Bake group")
        if name in graph.get("group", {}):
            for child in graph["group"][name]["targets"]:
                visit(child, stack | {name})
        elif name in graph["target"]:
            result.add(name)
        else:
            raise ValueError("missing Bake target: " + name)

    visit("default", set())
    return sorted(result)


def cache_catalog():
    # Includes internal-Dockerfile dependencies and Python row exports which
    # do not appear as linked Bake targets in a downstream solve.
    graph = read_graph(sorted({target for targets in STAGES.values()
                               for target in targets}))
    return {name: graph["target"][name] for name in graph_roots(graph)}


def cache_override(graph, repository, write=False, cold=False, imports=None):
    if not re.fullmatch(r"ghcr\.io/[a-z0-9][a-z0-9._/-]*", repository):
        raise ValueError("invalid GHCR cache repository")
    if repository == "ghcr.io/eglinuxer/crossforge":
        raise ValueError("cache repository must be separate from public SDK images")
    roots = graph_roots(graph)
    # Bake resolves groups and linked targets; only explicit roots export.
    # Internal Dockerfile dependencies require the shared cache catalog too.
    names = sorted(graph["target"])
    if not all(re.fullmatch(r"[a-zA-Z0-9_-]+", name) for name in names):
        raise ValueError("invalid Bake target name")
    result = {}
    for name in names:
        candidates = imports if imports is not None else names
        if isinstance(candidates, dict):
            target = graph["target"][name]
            row = target.get("args", {}).get("CPYTHON_ROW")
            # Linked Bake targets import their own Dockerfile's caches. Internal
            # stages remain covered by exports from that same Dockerfile.
            candidates = [source for source, config in candidates.items()
                          if config.get("dockerfile") == target.get("dockerfile")
                          and (not row or not config.get("args", {}).get("CPYTHON_ROW")
                               or config["args"]["CPYTHON_ROW"] == row)]
        # Final aggregates export mode=min, so their cache cannot provide
        # the intermediate build stages of other targets. Keep only their
        # own direct import; max-mode component exports cover shared inputs.
        sources = [{"type": "registry", "ref": repository + ":main-" + source}
                   for source in candidates
                   if source not in FINAL_STAGE_CACHE_TARGETS or source == name] if not cold else []
        value = {"cache-from": sources, "cache-to": []}
        if write and name in roots:
            value["cache-to"] = [{
                "type": "registry", "ref": repository + ":main-" + name,
                "mode": "min" if name in FINAL_STAGE_CACHE_TARGETS else "max",
                "image-manifest": True, "oci-mediatypes": True,
            }]
        result[name] = value
    # A parent's max-mode export contains its linked inputs too. Those inputs
    # are separate Bake solves: they must receive that cache themselves, or a
    # rebuilt input can invalidate the expensive parent despite its cache hit.
    final_refs = {repository + ":main-" + name for name in FINAL_STAGE_CACHE_TARGETS}
    direct_sources = {name: [source for source in value["cache-from"]
                             if source["ref"] not in final_refs]
                      for name, value in result.items()}
    for parent in names:
        pending = [parent]
        visited = set()
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            # A consuming solve must import the linked dependency's own
            # caches too; loading them only in the dependency solve can miss
            # shared stages when remote-cache resolution races across solves.
            parent_sources = result[parent]["cache-from"]
            for source in direct_sources[name]:
                if source not in parent_sources:
                    parent_sources.append(source)
            sources = result[name]["cache-from"]
            for source in direct_sources[parent]:
                if source not in sources:
                    sources.append(source)
            for context in graph["target"][name].get("contexts", {}).values():
                if isinstance(context, str) and context.startswith("target:"):
                    dependency = context.removeprefix("target:")
                    if dependency not in result:
                        raise ValueError("missing linked Bake target: " + dependency)
                    pending.append(dependency)
    return {"target": result}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sample_resources(path):
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        name, value = line.split(":", 1)
        if name in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            memory[name] = int(value.split()[0]) * 1024
    disk = shutil.disk_usage(ROOT)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"time": time.time(), "load": os.getloadavg(),
                                 "memory_bytes": memory, "disk_free_bytes": disk.free,
                                 "disk_total_bytes": disk.total}) + "\n")


def monitor_resources(path, stop):
    while True:
        sample_resources(path)
        if stop.wait(30):
            return


def run_stage(stage, directory, repository, write=False, cold=False):
    directory.mkdir(parents=True, exist_ok=True)
    graph = read_graph(STAGES[stage])
    write_json(directory / "graph.json", graph)
    override = directory / "cache.json"
    cache = cache_override(graph, repository, write, cold, cache_catalog())
    write_json(override, cache)
    # Bound concurrent top-level solves. Linked dependencies still use Bake's
    # original graph (and require the BuildKit cache-session fix) and the
    # same builder, so completed prerequisites remain available locally.
    targets = graph_roots(graph)
    stopped = threading.Event()
    monitor = threading.Thread(target=monitor_resources,
                               args=(directory / "resources.jsonl", stopped), daemon=True)
    started = time.monotonic()
    status = 127
    monitor.start()
    try:
        with (directory / "build.log").open("xb") as log:
            for target in targets:
                # Linked roots can be solved again by a later consumer. Export
                # only the current root, preserving every dependency import.
                solve_override = directory / ("cache-" + target + ".json")
                write_json(solve_override, {"target": {
                    name: {**value, "cache-to": value["cache-to"] if name == target else []}
                    for name, value in cache["target"].items()
                }})
                # One budget for the whole stage, including all root solves.
                remaining = int(330 * 60 - (time.monotonic() - started))
                if remaining <= 0:
                    status = 124
                    break
                command = ["timeout", "--signal=TERM", "--kill-after=60s",
                           str(remaining) + "s", *BAKE, "-f", str(solve_override),
                           target, "--progress=plain", "--metadata-file",
                           str(directory / ("metadata-" + target + ".json"))]
                status = HEARTBEAT["execute"](
                    command, stage + "/" + target, 60, output=log,
                    log_path=directory / "build.log")
                if status:
                    break
        return status
    finally:
        stopped.set()
        monitor.join()
        sample_resources(directory / "resources.jsonl")
        elapsed = round(time.monotonic() - started, 1)
        write_json(directory / "result.json", {
            "stage": stage, "targets": STAGES[stage], "exit_code": status,
            "elapsed_seconds": elapsed, "cold": cold, "cache_write": write,
            "source_commit": os.environ.get("GITHUB_SHA", ""),
            "kind": "crossforge-ci-build-observation",
        })
        print("%s: exit=%d elapsed=%.1fs" % (stage, status, elapsed), flush=True)
        if status and (directory / "build.log").exists():
            subprocess.run(["tail", "-n", "100", str(directory / "build.log")], check=False)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as stream:
                stream.write("\n%s: exit `%d`, %.1f seconds, cold=%s. "
                             "See the build diagnostics artifact.\n" %
                             (stage, status, elapsed, cold))


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--repository", default="ghcr.io/eglinuxer/crossforge-buildcache")
    parser.add_argument("--write-cache", action="store_true")
    parser.add_argument("--cold", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("stage", choices=STAGES)
    run.add_argument("--directory", type=Path, required=True)
    cache = commands.add_parser("cache")
    cache.add_argument("--output", type=Path, required=True)
    cache.add_argument("targets", nargs="+")
    args = parser.parse_args()
    if args.write_cache:
        require_writer(os.environ)
    if args.command == "run":
        return run_stage(args.stage, args.directory.resolve(), args.repository,
                         args.write_cache, args.cold)
    write_json(args.output, cache_override(read_graph(args.targets), args.repository,
                                          args.write_cache, args.cold, cache_catalog()))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("error: %s" % error, file=sys.stderr)
        sys.exit(1)
