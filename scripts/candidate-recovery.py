#!/usr/bin/env python3
"""Preserve exact candidate artifact producers when only failed jobs are retried."""

import argparse
import hashlib
import os
from pathlib import Path
import runpy
import sys

from crossforge_internal import candidate_recovery as recovery
from crossforge_internal.identity import content_sha256, load_json, parse_json, require

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command")
    upstream = commands.add_parser("upstream", allow_abbrev=False)
    upstream.add_argument("--stage", choices=("native", "sign"), required=True)
    upstream.add_argument("--needs-json", required=True)
    create = commands.add_parser("create", allow_abbrev=False)
    create.add_argument("--needs-json", required=True)
    create.add_argument("--candidate", type=Path, required=True)
    create.add_argument("--native-report", type=Path, required=True)
    create.add_argument("--component-selection", type=Path)
    create.add_argument("--output", type=Path, required=True)
    select = commands.add_parser("select", allow_abbrev=False)
    select.add_argument("--signature-directory", type=Path, required=True)
    select.add_argument("--candidate-run", type=Path, required=True)
    select.add_argument("--artifacts", type=Path, required=True)
    select.add_argument("--expected-run-id", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        require(args.command in ("upstream", "create", "select"), "a command is required")
        if args.command in ("upstream", "create"):
            require(os.environ.get("GITHUB_SERVER_URL") == "https://github.com" and
                os.environ.get("GITHUB_REPOSITORY") == recovery.REPOSITORY and
                os.environ.get("GITHUB_REF") == "refs/heads/main" and
                os.environ.get("GITHUB_EVENT_NAME") in ("push", "workflow_dispatch"), "candidate recovery requires the trusted main workflow")
            attempt = recovery.number(os.environ.get("GITHUB_RUN_ATTEMPT"), "current attempt")
            needs = parse_json(args.needs_json)
            recovery.upstream(needs, attempt, args.stage if args.command == "upstream" else "sign")
            if args.command == "upstream":
                return 0
            candidate = load_json(args.candidate)
            require(candidate["source_commit"] == os.environ.get("GITHUB_SHA"), "candidate source differs from current run")
            selection = load_json(args.component_selection) if args.component_selection is not None else None
            value = recovery.document(candidate, needs, recovery.number(os.environ.get("GITHUB_RUN_ID"), "run ID"), attempt, selection)
            require(hashlib.sha256(args.native_report.read_bytes()).hexdigest() == value["native_report_sha256"],
                    "native report differs from original successful producer")
            candidate_module = runpy.run_path(str(ROOT / "scripts/candidate_manifest.py"))
            candidate_module["write_json_once"](args.output, value)
            return 0
        promotion = runpy.run_path(str(ROOT / "scripts/release_promotion.py"))
        run = promotion["validate_candidate_run"](load_json(args.candidate_run), recovery.REPOSITORY, args.expected_run_id)
        path = args.signature_directory / "candidate-recovery.json"
        if path.exists() or path.is_symlink():
            require(path.is_file() and not path.is_symlink(), "candidate recovery must be a regular file")
            candidate = load_json(args.signature_directory / "candidate.json")
            value = recovery.bind(load_json(path), content_sha256(candidate), run)
            recovery.verify_artifacts(value, load_json(args.artifacts))
            selection = {role + "_id": str(item["id"]) for role, item in value["artifacts"].items()}
            selection.update({role + "_name": "" for role in recovery.PREFIXES})
        else:
            # Legacy successful candidates retain their exact single-attempt contract.
            selection = {role + "_name": "%s-%d-%d" % (prefix, run["id"], run["attempt"])
                         for role, prefix in recovery.PREFIXES.items()}
            selection.update({role + "_id": "" for role in recovery.PREFIXES})
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            for name, value in sorted(selection.items()):
                output.write(name + "=" + value + "\n")
        return 0
    except (ValueError, KeyError, OSError) as error:
        print("candidate recovery failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
