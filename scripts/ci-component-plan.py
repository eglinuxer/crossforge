#!/usr/bin/env python3
"""Select CI roots by comparing immutable Git source inventories in Docker/Bake."""

import argparse
import io
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile

from crossforge_internal import ci_execution, incremental_plan
from crossforge_internal.identity import (IdentityError, file_record, parse_json, relative_path, require)


ROOT = Path(__file__).resolve().parents[1]


def stage_catalog():
    # Keep the existing build CLI authoritative; importing the selection domain
    # itself does not load workflow orchestration or SDK/packaging policy.
    return runpy.run_path(str(ROOT / "scripts/ci-build.py"))["STAGES"]


def compiler_targets():
    return sorted([target for arch in ("x86_64", "aarch64") for target in (
        "toolchain-%s-build-export" % arch, "gcc-%s-test-context-export" % arch)] +
        ["cpython-build-%s" % row for row in ("cp39", "cp310", "cp311", "cp312", "cp313", "cp314")] +
        ["cpython-cross-%s-%s" % (row, arch) for row in ("cp39", "cp310", "cp311", "cp312", "cp313", "cp314")
         for arch in ("x86_64", "aarch64")])


def _git(repository, *arguments):
    return subprocess.check_output(["git", *arguments], cwd=str(repository), stderr=subprocess.PIPE)


def export_source(repository, commit, destination):
    """Read a commit without altering the checkout or following archive links."""
    require(type(commit) is str and re.fullmatch(r"[0-9a-f]{40}", commit), "expected a complete source commit")
    require(not destination.exists(), "source snapshot destination must be new")
    data = _git(repository, "archive", "--format=tar", commit)
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive:
            name = relative_path(member.name.rstrip("/"))
            path = destination / name
            require(member.isfile() or member.isdir(), "source snapshot contains unsupported links or special files")
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as output:
                    shutil.copyfileobj(archive.extractfile(member), output)
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
    for directory in [destination] + [path for path in destination.rglob("*") if path.is_dir()]:
        directory.chmod(0o755)


def capture_source(source, stages):
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for script in ("render-release-components.py", "render-vcpkg-integration.py", "render-bake.py"):
        subprocess.run([sys.executable, str(source / "scripts" / script), "--check"], cwd=str(source),
                       env=environment, stdout=sys.stderr, check=True)
    requested = sorted({target for values in stages.values() for target in values} | set(compiler_targets()))
    graph = parse_json(subprocess.check_output(["docker", "buildx", "bake", "-f", "docker-bake.hcl", "-f",
        "docker-bake.override.json", "--print"] + requested, cwd=str(source), env=environment))
    policy = file_record(source, ".github/actions/setup-locked-buildx/action.yml")
    return incremental_plan.snapshot(source, graph, stages, compiler_targets(), {"ci_build_policy": policy})


def plan(repository, base, head, directory):
    stages = stage_catalog()
    require(type(head) is str and re.fullmatch(r"[0-9a-f]{40}", head), "head must be a complete source commit")
    require(_git(repository, "rev-parse", head + "^{commit}").decode().strip() == head, "head commit is unavailable")
    if base is None:
        return incremental_plan.full(stages, "comparison baseline is unavailable")
    paths = []
    try:
        require(type(base) is str and re.fullmatch(r"[0-9a-f]{40}", base), "base must be a complete source commit")
        raw = _git(repository, "diff", "--no-renames", "--name-only", "-z", base, head)
        paths = [value.decode("utf-8") for value in raw.split(b"\0") if value]
        for path in paths:
            relative_path(path)
        if all(incremental_plan.quick_only(path) for path in paths):
            return incremental_plan._plan({}, {}, [], paths)
        snapshots = []
        for name, commit in (("base", base), ("head", head)):
            source = directory / name
            export_source(repository, commit, source)
            snapshots.append(capture_source(source, stages))
        return incremental_plan.select(snapshots[0], snapshots[1], paths)
    except (IdentityError, OSError, UnicodeError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as error:
        # A failed inventory cannot turn into an empty selected matrix. The
        # existing complete CI graph remains the executable conservative path.
        return incremental_plan.full(stages, "cannot prove incremental scope: %s" % error, paths)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command")
    select = commands.add_parser("select", allow_abbrev=False)
    select.add_argument("--base")
    select.add_argument("--head", required=True)
    select.add_argument("--output", type=Path, required=True)
    execution = commands.add_parser("execution", allow_abbrev=False)
    execution.add_argument("--profile", required=True)
    execution.add_argument("--selection", default="")
    check = commands.add_parser("check", allow_abbrev=False)
    check.add_argument("results")
    args = parser.parse_args(argv)
    try:
        if args.command == "execution":
            for key, value in ci_execution.prepare(args.selection, args.profile, stage_catalog()).items():
                print(key + "=" + value)
            return 0
        if args.command == "check":
            return 0 if ci_execution.check_results(parse_json(args.results), stage_catalog()) else 1
        require(args.command == "select", "a CI planner command is required")
        with tempfile.TemporaryDirectory(prefix="crossforge-ci-plan-") as directory:
            result = plan(ROOT, args.base, args.head, Path(directory))
        result.update(source_commit=args.head, base_commit=args.base)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, sort_keys=True, indent=2)
            stream.write("\n")
        print("plan=" + json.dumps(incremental_plan.compact(result), sort_keys=True, separators=(",", ":")))
        return 0
    except (IdentityError, OSError, subprocess.CalledProcessError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
