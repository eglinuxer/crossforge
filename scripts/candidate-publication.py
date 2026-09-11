#!/usr/bin/env python3
"""Seal and restore same-run immutable source/SDK publication checkpoints."""

import argparse
import os
from pathlib import Path
import shutil
import sys

from crossforge_internal import candidate_publication as publication
from crossforge_internal.identity import content_sha256, digest_value, exact_fields, load_json, parse_json, require
from crossforge_internal.candidate_recovery import number

ROOT = Path(__file__).resolve().parents[1]


def outputs(values):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        for key, value in sorted(values.items()):
            require("\n" not in str(value) and "\r" not in str(value), "invalid publication output")
            stream.write("%s=%s\n" % (key, value))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command")
    check = commands.add_parser("upstream", allow_abbrev=False)
    check.add_argument("--phase", choices=("source", "sdk"), required=True)
    check.add_argument("--needs-json", required=True)
    seal = commands.add_parser("seal", allow_abbrev=False)
    seal.add_argument("--phase", choices=("source", "sdk"), required=True)
    seal.add_argument("--root", type=Path, required=True)
    seal.add_argument("--output", type=Path, required=True)
    seal.add_argument("--source-checkpoint", type=Path)
    seal.add_argument("--source-sha256")
    restore = commands.add_parser("restore", allow_abbrev=False)
    restore.add_argument("--phase", choices=("source", "sdk"), required=True)
    restore.add_argument("--directory", type=Path, required=True)
    restore.add_argument("--sha256", required=True)
    restore.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        require(args.command in ("upstream", "seal", "restore"), "a publication command is required")
        current = publication.producer(os.environ, args.phase)
        if args.command == "upstream":
            needs = parse_json(args.needs_json)
            job = args.phase + "-publication"
            exact_fields(needs, (job,), "publication predecessor jobs")
            exact_fields(needs[job], ("result", "outputs"), "publication predecessor")
            require(needs[job]["result"] == "success", "publication predecessor did not succeed")
            exact_fields(needs[job]["outputs"], ("checkpoint_artifact_id", "checkpoint_sha256"), "publication predecessor outputs")
            number(needs[job]["outputs"]["checkpoint_artifact_id"], "publication artifact ID")
            digest_value(needs[job]["outputs"]["checkpoint_sha256"], "publication checkpoint SHA256")
            return 0
        if args.command == "seal":
            require((args.source_checkpoint is not None and args.source_sha256 is not None) if args.phase == "sdk" else
                    (args.source_checkpoint is None and args.source_sha256 is None), "publication source checkpoint options differ from phase")
            parent = None
            if args.phase == "sdk":
                parent = publication.verify(ROOT, args.source_checkpoint, args.source_sha256, current, "source")
            require(not args.output.exists() and not args.output.is_symlink(), "publication checkpoint output must be new")
            args.output.mkdir(parents=True)
            for name in publication.files_for(args.phase):
                if parent is not None and name in publication.SOURCE_FILES:
                    path = args.source_checkpoint / name
                else:
                    path = args.root / ("source-bundle-identity/source-bundle.json" if name == "source-bundle.json" else name)
                publication.file_hash(path)
                shutil.copyfile(str(path), str(args.output / name))
            value = publication.seal(ROOT, args.output, current, parent)
            outputs({"checkpoint_sha256": content_sha256(value)})
            return 0
        value = publication.restore(ROOT, args.directory, args.sha256, current, args.phase, args.output)
        release = load_json(ROOT / "config/release.json")
        source = value if args.phase == "source" else value["source"]
        binding = load_json(args.output / "source-binding.json")
        result = {"digest": value["image"]["digest"], "platform_digest": value["image"]["platform_manifest_digest"],
            "reference": value["image"]["reference"], "attempt": current["attempt"],
            "source_reference": source["image"]["reference"], "source_digest": source["image"]["digest"],
            "source_platform_digest": source["image"]["platform_manifest_digest"], "archive_sha256": binding["archive"]["sha256"],
            "archive_size": binding["archive"]["size"], "source_attempt": source["producer"]["attempt"],
            "source_checkpoint_sha256": content_sha256(source),
            "sbom_generator": release["sbom"]["generator"]["repository"] + "@" + release["sbom"]["generator"]["digest"]}
        if args.phase == "sdk":
            result.update(candidate_sha256=content_sha256(load_json(args.output / "candidate.json")),
                          sdk_attempt=value["producer"]["attempt"], sdk_checkpoint_sha256=content_sha256(value))
            if value["schema_version"] == 2:
                result["component_selection_sha256"] = content_sha256(load_json(args.output / "component-selection.json"))
        outputs(result)
        return 0
    except (ValueError, KeyError, OSError) as error:
        print("candidate publication failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
