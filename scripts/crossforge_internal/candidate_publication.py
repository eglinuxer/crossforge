"""Immutable source/SDK publication checkpoints, before consumer qualification."""

import copy
import hashlib
from pathlib import Path
import re
import runpy
import shutil

from .identity import content_sha256, digest_value, exact_fields, load_json, require
from .candidate_recovery import number, positive, REPOSITORY, WORKFLOW


SOURCE_FILES = ("source-bundle.json", "source-binding.json", "source-index.json", "source-build-metadata.json", "sbom-generator-image.json")
SDK_FILES = SOURCE_FILES + ("candidate.json", "candidate-index.json", "build-metadata.json")


def files_for(phase):
    require(phase in ("source", "sdk"), "unsupported publication checkpoint phase")
    return SOURCE_FILES if phase == "source" else SDK_FILES


def file_hash(path):
    require(path.is_file() and not path.is_symlink(), "publication payload must be a regular file: " + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def producer(environment, phase):
    files_for(phase)
    require(environment.get("GITHUB_SERVER_URL") == "https://github.com" and environment.get("GITHUB_REPOSITORY") == REPOSITORY and
            environment.get("GITHUB_REF") == "refs/heads/main" and environment.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and
            environment.get("GITHUB_WORKFLOW_REF") == REPOSITORY + "/" + WORKFLOW + "@refs/heads/main" and
            environment.get("GITHUB_WORKFLOW_SHA") == environment.get("GITHUB_SHA"), "publication checkpoint requires the exact trusted candidate workflow")
    return validate_producer({"source_commit": environment.get("GITHUB_SHA"), "run_id": number(environment.get("GITHUB_RUN_ID"), "run ID"),
        "attempt": number(environment.get("GITHUB_RUN_ATTEMPT"), "attempt"), "phase": phase})


def validate_producer(value):
    exact_fields(value, ("source_commit", "run_id", "attempt", "phase"), "publication producer")
    require(type(value["source_commit"]) is str and re.fullmatch(r"[0-9a-f]{40}", value["source_commit"]), "publication source commit is invalid")
    positive(value["run_id"], "publication run ID")
    positive(value["attempt"], "publication attempt")
    files_for(value["phase"])
    return value


def validate(value):
    exact_fields(value, ("schema_version", "kind", "repository", "workflow", "producer", "release_sha256", "image", "files", "source"),
                 "publication checkpoint")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1 and
            value["kind"] == "crossforge-candidate-publication", "unsupported publication checkpoint schema")
    require(value["repository"] == REPOSITORY and value["workflow"] == WORKFLOW, "publication checkpoint repository or workflow differs")
    original = validate_producer(value["producer"])
    digest_value(value["release_sha256"], "publication release SHA256")
    exact_fields(value["files"], files_for(original["phase"]), "publication file set")
    for digest in value["files"].values():
        digest_value(digest, "publication file SHA256")
    exact_fields(value["image"], ("repository", "digest", "platform_manifest_digest", "reference"), "published image")
    require(value["image"]["repository"] == "ghcr.io/eglinuxer/crossforge", "publication image repository differs")
    for field in ("digest", "platform_manifest_digest"):
        digest_value(value["image"][field], "publication " + field, oci=True)
    require(type(value["image"]["reference"]) is str and value["image"]["reference"].startswith(value["image"]["repository"] + ":"),
            "publication tag is invalid")
    if original["phase"] == "source":
        require(value["source"] is None, "source publication cannot have a parent")
    else:
        require(type(value["source"]) is dict and type(value["source"].get("producer")) is dict and
                value["source"]["producer"].get("phase") == "source", "SDK publication requires one source checkpoint")
        validate(value["source"])
        parent = value["source"]["producer"]
        require(parent["phase"] == "source" and parent["run_id"] == original["run_id"] and parent["source_commit"] == original["source_commit"] and
                parent["attempt"] <= original["attempt"] and value["source"]["release_sha256"] == value["release_sha256"],
                "SDK publication source producer differs")
        require(all(value["files"][name] == value["source"]["files"][name] for name in SOURCE_FILES),
                "SDK publication changed its original source payloads")
    return value


def semantics(source, directory, value):
    """Revalidate stored metadata and identity; public byte retrieval is separate."""
    source, directory = Path(source), Path(directory)
    candidate = runpy.run_path(str(source / "scripts/candidate_manifest.py"))
    binding = runpy.run_path(str(source / "scripts/source_binding.py"))
    image = runpy.run_path(str(source / "scripts/resolve_candidate_image.py"))
    release = candidate["load_release"](source / "config/release.json", source / "config/schemas/release.schema.json")
    require(content_sha256(release) == value["release_sha256"], "publication release inputs differ")
    original = value["producer"]
    source_binding = load_json(directory / "source-binding.json")
    binding["validate_binding"](source_binding, release, load_json(source / "config/schemas/source-binding.schema.json"), original["source_commit"])
    identity = candidate["load_source_bundle_identity"](directory / "source-bundle.json",
        source / "config/schemas/source-bundle-identity.schema.json", release, original["source_commit"])
    require(identity["archive"] == source_binding["archive"], "publication source archive identity differs")
    source_value = value if original["phase"] == "source" else value["source"]
    require({key: source_binding[key] for key in ("repository", "digest", "platform_manifest_digest")} ==
            {key: source_value["image"][key] for key in ("repository", "digest", "platform_manifest_digest")}, "publication source OCI identity differs")
    def check_image(record, metadata, index, target):
        require(image["buildx_digest"](load_json(directory / metadata), target) == record["image"]["digest"], "publication Buildx metadata differs")
        require(image["platform_manifest_digest"]((directory / index).read_bytes(), record["image"]["digest"], "linux/amd64") ==
                record["image"]["platform_manifest_digest"], "publication platform manifest differs")
        owner = record["producer"]
        tag = candidate["candidate_tag"](release, owner["source_commit"], str(owner["run_id"]), str(owner["attempt"]))
        require(record["image"]["reference"] == record["image"]["repository"] + ":" + ("source-" if owner["phase"] == "source" else "") + tag,
                "publication tag differs from its original producer")
    check_image(source_value, "source-build-metadata.json", "source-index.json", "source-bundle")
    if original["phase"] == "sdk":
        current = load_json(directory / "candidate.json")
        candidate["validate_candidate"](current, release, candidate["load_candidate_schema"](source / "config/schemas/candidate.schema.json"), original["source_commit"])
        require(current["source_bundle"] == {key: source_binding[key] for key in ("repository", "digest", "platform", "platform_manifest_digest", "archive")},
                "SDK candidate source binding differs")
        require(all(current[key] == value["image"][key] for key in ("repository", "digest", "platform_manifest_digest")), "SDK candidate identity differs")
        check_image(value, "build-metadata.json", "candidate-index.json", "sdk-candidate")
    report = load_json(directory / "sbom-generator-image.json")
    candidate["STRICT"]["validate"](report, load_json(source / "config/schemas/sbom-generator-image.schema.json"),
        load_json(source / "config/schemas/sbom-generator-image.schema.json"), "$")
    require(report["index_digest"] == release["sbom"]["generator"]["digest"] and report["manifest_digest"] == release["sbom"]["generator"]["manifest_digest"],
            "publication SBOM generator differs")
    return source_binding


def seal(source, directory, original, parent=None):
    directory = Path(directory)
    phase = original["phase"]
    image_module = runpy.run_path(str(Path(source) / "scripts/resolve_candidate_image.py"))
    candidate_module = runpy.run_path(str(Path(source) / "scripts/candidate_manifest.py"))
    release = load_json(Path(source) / "config/release.json")
    metadata, index, target = ("source-build-metadata.json", "source-index.json", "source-bundle") if phase == "source" else ("build-metadata.json", "candidate-index.json", "sdk-candidate")
    digest = image_module["buildx_digest"](load_json(directory / metadata), target)
    platform = image_module["platform_manifest_digest"]((directory / index).read_bytes(), digest, "linux/amd64")
    tag = candidate_module["candidate_tag"](release, original["source_commit"], str(original["run_id"]), str(original["attempt"]))
    repository = release["product"]["image_repository"]
    value = validate({"schema_version": 1, "kind": "crossforge-candidate-publication", "repository": REPOSITORY, "workflow": WORKFLOW,
        "producer": copy.deepcopy(original), "release_sha256": content_sha256(release),
        "image": {"repository": repository, "digest": digest, "platform_manifest_digest": platform,
                  "reference": repository + ":" + ("source-" if phase == "source" else "") + tag},
        "files": {name: file_hash(directory / name) for name in files_for(phase)}, "source": copy.deepcopy(parent)})
    semantics(source, directory, value)
    candidate_module["write_json_once"](directory / "checkpoint.json", value)
    return value


def verify(source, directory, sha256, current, phase):
    directory = Path(directory)
    digest_value(sha256, "independent publication checkpoint SHA256")
    file_hash(directory / "checkpoint.json")
    value = validate(load_json(directory / "checkpoint.json"))
    require(content_sha256(value) == sha256, "publication checkpoint SHA256 differs")
    original = value["producer"]
    require(original["phase"] == phase and original["run_id"] == current["run_id"] and original["source_commit"] == current["source_commit"] and
            original["attempt"] <= current["attempt"], "publication checkpoint belongs to another producer or source")
    require({path.name for path in directory.iterdir()} == set(files_for(phase)) | {"checkpoint.json"}, "publication checkpoint payload set differs")
    require(value["files"] == {name: file_hash(directory / name) for name in files_for(phase)}, "publication payload bytes differ")
    semantics(source, directory, value)
    return value


def restore(source, directory, sha256, current, phase, output):
    value = verify(source, directory, sha256, current, phase)
    output = Path(output)
    mapping = {name: name for name in files_for(phase)}
    mapping["source-bundle.json"] = "source-bundle-identity/source-bundle.json"
    require(all(not (output / name).exists() and not (output / name).is_symlink() for name in mapping.values()), "publication restore would replace existing inputs")
    for name, destination in mapping.items():
        path = output / destination
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(Path(directory) / name), str(path))
    return value
