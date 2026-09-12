"""Trusted CI source identity and canonical graph capture for component jobs."""

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

from . import ci_execution, component_artifacts, component_build
from .identity import require


MAIN_CALLER = "eglinuxer/crossforge/.github/workflows/ci.yml@refs/heads/main"
CANDIDATE_CALLER = "eglinuxer/crossforge/.github/workflows/candidate.yml@refs/heads/main"
RAW_CALLERS = {MAIN_CALLER: ("push", "workflow_dispatch"), CANDIDATE_CALLER: ("workflow_dispatch",)}


def github_producer(environment, commit, dirty, mode="pilot"):
    require(mode in ("pilot", "main", "candidate"), "unsupported component CI entry point")
    require(ci_execution.component_reader_allowed(environment), "component CI requires trusted main push or dispatch")
    if mode == "pilot":
        require(environment.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "component pilot requires trusted main dispatch")
    else:
        caller = environment.get("GITHUB_WORKFLOW_REF")
        require(environment.get("GITHUB_EVENT_NAME") in RAW_CALLERS.get(caller, ()) and
                environment.get("GITHUB_WORKFLOW_SHA") == commit, "component production requires an exact trusted main caller")
        if mode == "candidate":
            require(caller == CANDIDATE_CALLER, "candidate components require the candidate workflow")
    require(environment.get("GITHUB_SHA") == commit and not dirty, "component CI requires the exact clean source")
    value = {"kind": "github-actions", "source_commit": commit, "source_dirty": False,
             "invocation": "https://github.com/eglinuxer/crossforge/actions/runs/%s/attempts/%s" %
                (environment.get("GITHUB_RUN_ID", ""), environment.get("GITHUB_RUN_ATTEMPT", "")),
             "started_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}
    return component_artifacts.validate_producer(value)


def checked_source(source, mode="pilot"):
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(source)).decode().strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain", "-z"], cwd=str(source)))
    producer = github_producer(os.environ, commit, dirty, mode)
    for script in ("render-release-components.py", "render-vcpkg-integration.py", "render-bake.py"):
        subprocess.run([sys.executable, str(Path(source) / "scripts" / script), "--check"],
                       cwd=str(source), stdout=sys.stderr, check=True)
    return producer


def source_graph(source, targets, directory, builder, docker_config=None):
    require(type(targets) is list and targets, "component graph requires explicit roots")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    cache = directory / "cache.json"
    environment = dict(os.environ)
    if docker_config:
        environment["DOCKER_CONFIG"] = str(docker_config)
    subprocess.run([sys.executable, "scripts/ci-build.py", "cache", "--output", str(cache)] + targets,
                   cwd=str(source), env=environment, stdout=sys.stderr, check=True)
    command = component_build.docker_command(docker_config) + ["buildx", "bake", "--builder", builder,
        "-f", "docker-bake.hcl", "-f", "docker-bake.override.json", "-f", str(cache), "--print"]
    graph = json.loads(subprocess.check_output(command + targets, cwd=str(source)))
    component_build.write_json(directory / "graph.json", graph)
    return graph
