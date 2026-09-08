#!/usr/bin/env python3
"""Resolve version tags to exact successful candidates; never start a build."""

import argparse
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PROMOTION = runpy.run_path(str(ROOT / "scripts/release_promotion.py"))
TAG = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def check_tag(tag, sha):
    if not TAG.fullmatch(tag or "") or not SHA.fullmatch(sha or ""):
        raise ValueError("release requires a stable vX.Y.Z tag and full commit SHA")
    if git("rev-parse", "refs/tags/" + tag + "^{commit}") != sha:
        raise ValueError("release tag does not point to the candidate commit")
    subprocess.run(["git", "merge-base", "--is-ancestor", sha, "origin/main"], check=True)
    release = json.loads(git("show", sha + ":config/release.json"),
                         object_pairs_hook=PROMOTION["STRICT"]["reject_duplicate_keys"])
    if tag != PROMOTION["version_tag"](release["product"]["version"]):
        raise ValueError("release tag differs from config/release.json at that commit")


def select_candidate(runs, repository, sha):
    matching = [run for run in runs if (
        run.get("head_sha") == sha
        and run.get("head_branch") == "main"
        and run.get("path") == ".github/workflows/candidate.yml"
        and run.get("event") in ("push", "workflow_dispatch")
        and run.get("repository", {}).get("full_name") == repository
        and run.get("head_repository", {}).get("full_name") == repository
    )]
    # Pin the newest attempt/run, including failures; never silently fall back
    # to an older success when a later qualification is failing or incomplete.
    if not matching:
        raise ValueError("no trusted main candidate exists for the exact tag commit")
    run = max(matching, key=lambda item: (item["id"], item["run_attempt"]))
    if run.get("status") != "completed":
        return None
    PROMOTION["validate_candidate_run"](run, repository, run["id"])
    return run


def gh_json(*args):
    return json.loads(subprocess.check_output(["gh", *args], text=True))


def request_release(event_name, event, repository, ref):
    if repository != "eglinuxer/crossforge":
        raise ValueError("release requests require the upstream repository")
    if event_name == "push":
        if event.get("deleted"):
            return
        if not ref.startswith("refs/tags/"):
            raise ValueError("release push must name a tag")
        tags = [ref[len("refs/tags/"):]]
        sha = git("rev-parse", ref + "^{commit}")
        # A moved/deleted tag must not release a different commit on rerun.
        if git("rev-parse", event["after"] + "^{commit}") != sha:
            raise ValueError("release tag moved since the triggering push")
    elif event_name == "workflow_run":
        run_id = PROMOTION["positive_integer"](event["workflow_run"]["id"], "candidate run ID")
        run = gh_json("api", "/repos/" + repository + "/actions/runs/" + str(run_id))
        PROMOTION["validate_candidate_run"](run, repository, run["id"])
        sha = run["head_sha"]
        tags = [tag for tag in git("tag", "--points-at", sha).splitlines()
                if TAG.fullmatch(tag)]
    else:
        raise ValueError("unsupported release request event")
    for tag in tags:
        check_tag(tag, sha)
        pages = gh_json(
            "api", "--paginate", "--slurp", "--method", "GET",
            "/repos/" + repository + "/actions/workflows/candidate.yml/runs",
            "-f", "head_sha=" + sha, "-f", "branch=main", "-f", "per_page=100",
        )
        runs = [run for page in pages for run in page["workflow_runs"]]
        candidate = select_candidate(runs, repository, sha)
        if candidate is None:
            print(tag + ": candidate still running; its completion event will resume release")
            continue
        # Dispatch from main so existing main-only production protection and
        # trusted promotion code still apply. The existing tag is revalidated
        # after environment approval and again by the release preflight.
        payload = {"ref": "main", "inputs": {
            "candidate_run_id": str(candidate["id"]),
            "release_tag": tag,
            "confirmation": "PROMOTE-" + tag,
            "immutable_releases_enabled": True,
            "private_vulnerability_reporting_enabled": True,
        }}
        subprocess.run([
            "gh", "api", "--method", "POST",
            "/repos/" + repository + "/actions/workflows/promote.yml/dispatches",
            "--input", "-",
        ], input=json.dumps(payload), text=True, check=True)
        print(tag + ": requested digest-only promotion of candidate " + str(candidate["id"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-tag")
    check.add_argument("--tag", required=True)
    check.add_argument("--sha", required=True)
    commands.add_parser("request")
    args = parser.parse_args()
    try:
        if args.command == "check-tag":
            check_tag(args.tag, args.sha)
        else:
            event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
            request_release(os.environ["GITHUB_EVENT_NAME"], event,
                            os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_REF"])
    except (ValueError, KeyError, subprocess.CalledProcessError) as error:
        print("release request failed: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
