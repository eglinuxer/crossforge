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


def stream_build_command(command, log_path):
    # Pass paths and argv as positional arguments, never as shell source.
    # pipefail retains a failed build's status even when tee succeeds.
    return ["bash", "-o", "pipefail", "-c",
            'log_file=$1; shift; "$@" 2>&1 | tee -a "$log_file"',
            "crossforge-build-log", str(log_path), *command]


def selected_graph(stage, targets=None):
    graph = read_graph(STAGES[stage])
    if targets is None:
        return graph
    if not isinstance(targets, list) or not targets or any(not isinstance(target, str) for target in targets):
        raise ValueError("selected stage targets must be a nonempty array of strings")
    if len(set(targets)) != len(targets) or set(targets) - (set(graph_roots(graph)) | set(STAGES[stage])):
        raise ValueError("selected targets are outside this hosted stage")
    # Re-resolve through Bake so linked dependencies retain the canonical graph.
    return read_graph(targets)


def run_stage(stage, directory, repository, write=False, cold=False, selected_targets=None, components=None):
    graph = selected_graph(stage, selected_targets)
    directory.mkdir(parents=True, exist_ok=True)
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
        bake = list(BAKE)
        if components is not None:
            from crossforge_internal import component_build, component_resolution
            execution = component_build.execution_identity(components["builder"])
            resolved, binding = component_resolution.bind_toolchains(ROOT, graph, execution,
                components["cosign"], components["directory"], directory / "components",
                components["builder"], components["oras"])
            if components.get("required") and binding["required_producers"]:
                raise ValueError("centralized toolchain preparation is incomplete: " + ", ".join(binding["required_producers"]))
            resolved_path = directory / "components.bake.json"
            write_json(resolved_path, resolved)
            bake += ["--builder", components["builder"], "-f", str(resolved_path)]
            print("%s: resolved %d toolchain components; %d producer boundaries require build" % (
                stage, len(binding["components"]), len(binding["required_producers"])), flush=True)
        with (directory / "build.log").open("xb"):
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
                           str(remaining) + "s", *bake, "-f", str(solve_override),
                           target, "--progress=plain", "--metadata-file",
                           str(directory / ("metadata-" + target + ".json"))]
                status = HEARTBEAT["execute"](
                    command[:4] + stream_build_command(command[4:], directory / "build.log"),
                    stage + "/" + target, 60,
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
            "stage": stage, "targets": selected_targets if selected_targets is not None else STAGES[stage], "exit_code": status,
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
    run.add_argument("--targets-json")
    run.add_argument("--component-builder")
    run.add_argument("--component-oras", type=Path)
    run.add_argument("--component-cosign", type=Path)
    run.add_argument("--component-directory", type=Path)
    run.add_argument("--require-components", action="store_true")
    cache = commands.add_parser("cache")
    cache.add_argument("--output", type=Path, required=True)
    cache.add_argument("targets", nargs="+")
    args = parser.parse_args()
    if args.write_cache:
        require_writer(os.environ)
    if args.command == "run":
        from crossforge_internal.identity import parse_json
        components = None
        options = (args.component_builder, args.component_oras, args.component_cosign, args.component_directory)
        if args.require_components and not all(value is not None for value in options):
            raise ValueError("required component consumption needs all component options")
        if any(value is not None for value in options):
            if not all(value is not None for value in options):
                raise ValueError("component consumption requires builder, ORAS, Cosign and a separate OCI directory")
            if args.cold or args.write_cache:
                raise ValueError("incremental component consumption cannot be combined with cold or cache-writing qualification")
            components = {"builder": args.component_builder, "oras": args.component_oras,
                "cosign": args.component_cosign, "directory": args.component_directory, "required": args.require_components}
        return run_stage(args.stage, args.directory.resolve(), args.repository,
                         args.write_cache, args.cold,
                         parse_json(args.targets_json) if args.targets_json is not None else None, components)
    write_json(args.output, cache_override(read_graph(args.targets), args.repository,
                                          args.write_cache, args.cold, cache_catalog()))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("error: %s" % error, file=sys.stderr)
        sys.exit(1)
