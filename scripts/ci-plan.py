#!/usr/bin/env python3
"""Conservatively select hosted build profiles from a Git diff."""

import argparse
import json
import re
import subprocess

def select_profile(paths):
    profiles = set()
    for path in paths:
        if path.startswith("docs/") or path in {
            "README.md", "SUPPORT.md", "SECURITY.md", "AGENTS.md",
            "LICENSE-MIT", "LICENSE-APACHE",
        }:
            continue
        if path.startswith("tests/config/"):
            continue  # Always executed by the required fast check.
        if path.startswith(("tools/crossforge/", "tests/packaging/", "tests/consumer/",
                            "integration/")) or path == "docker/packaging.Dockerfile":
            profiles.add("sdk")
        elif path.startswith("tests/qt6/") or re.match(
            r"(?:scripts/[^/]*qt[^/]*|docker/qt[^/]*)$", path
        ):
            continue  # Qt compatibility builds are explicitly requested locally or manually.
        elif path.startswith("tests/python/") or re.match(
            r"(?:scripts/[^/]*(?:python|cpython)[^/]*|docker/python[^/]*)$", path
        ):
            profiles.add("python")
        else:
            return "full"  # Locks, shared scripts, workflows and unknown paths.
    if not profiles:
        return "none"
    if profiles <= {"sdk", "python"}:
        return "python" if "python" in profiles else "sdk"
    return profiles.pop() if len(profiles) == 1 else "full"


def required_results(results):
    return set(results) == {"quick", "builds"} and all(
        value.get("result") == "success" for value in results.values())


def stage_results(results):
    plan = results.get("plan", {})
    if plan.get("result") != "success":
        return False
    flags = plan.get("outputs", {})
    groups = {"inputs": "active", "toolchains": "active", "python": "sdk",
              "vcpkg": "sdk", "sdk": "sdk", "gcc": "gcc",
              "qt-inputs": "qt", "qt-host-webengine": "qt", "qt-host": "qt", "qt": "qt"}
    if set(results) != {"plan", *groups} or set(flags) != {"active", "sdk", "gcc", "qt"}:
        return False
    if any(flags.get(flag) not in ("true", "false") for flag in groups.values()):
        return False
    return all(results.get(job, {}).get("result") == (
        "success" if flags[flag] == "true" else "skipped") for job, flag in groups.items())


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--base", required=True)
    plan.add_argument("--head", required=True)
    check = commands.add_parser("check")
    check.add_argument("results")
    stages = commands.add_parser("check-stages")
    stages.add_argument("results")
    args = parser.parse_args()
    if args.command == "check-stages":
        return 0 if stage_results(json.loads(args.results)) else 1
    if args.command == "check":
        return 0 if required_results(json.loads(args.results)) else 1
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (args.base, args.head)):
        parser.error("base/head must be complete Git commit IDs")
    # Include both paths of renames, so moving build code into docs cannot skip it.
    raw = subprocess.check_output([
        "git", "diff", "--no-renames", "--name-only", "-z", args.base, args.head,
    ])
    paths = [value.decode("utf-8") for value in raw.split(b"\0") if value]
    print("profile=" + select_profile(paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
