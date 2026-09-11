"""Exact same-run candidate artifact lineage across partial job retries.

This records successful upstream job outputs, not independent qualification.
Consumers must still validate candidate, native execution and public signatures.
"""

import copy
import re

from .identity import content_sha256, digest_value, exact_fields, require


REPOSITORY = "eglinuxer/crossforge"
WORKFLOW = ".github/workflows/candidate.yml"
PREFIXES = {"identity": "candidate-identity", "probes": "native-aarch64-probes", "native": "native-aarch64-evidence"}
PUBLISH_OUTPUTS = ("candidate_digest", "platform_digest", "candidate_sha256", "probe_bundle_sha256",
                   "publish_attempt", "identity_artifact_id", "probe_artifact_id")
PUBLICATION_OUTPUTS = ("source_attempt", "source_checkpoint_sha256", "sdk_attempt", "sdk_checkpoint_sha256")
NATIVE_OUTPUTS = ("native_attempt", "native_artifact_id", "native_report_sha256")


def positive(value, label):
    require(type(value) is int and value > 0, label + " must be a positive integer")
    return value


def number(value, label):
    require(type(value) is str and re.fullmatch(r"[1-9][0-9]*", value), label + " must be a positive decimal")
    return int(value)


def upstream(needs, attempt, stage):
    require(stage in ("native", "sign"), "unsupported candidate recovery consumer")
    positive(attempt, "current attempt")
    exact_fields(needs, ("publish",) if stage == "native" else ("publish", "native-aarch64"), "candidate upstream jobs")
    for job, fields in (("publish", PUBLISH_OUTPUTS), ("native-aarch64", NATIVE_OUTPUTS)):
        if job not in needs:
            continue
        exact_fields(needs[job], ("result", "outputs"), "candidate upstream job")
        require(needs[job]["result"] == "success", "candidate upstream job did not succeed: " + job)
        if job == "publish" and set(needs[job]["outputs"]) & set(PUBLICATION_OUTPUTS):
            fields = fields + PUBLICATION_OUTPUTS
        exact_fields(needs[job]["outputs"], fields, "candidate upstream outputs")
        for field, value in needs[job]["outputs"].items():
            if field.endswith("_attempt") or field.endswith("_artifact_id"):
                number(value, field)
            else:
                digest_value(value, field, oci=field in ("candidate_digest", "platform_digest"))
    publish = needs["publish"]["outputs"]
    require(number(publish["publish_attempt"], "publish attempt") <= attempt, "publish attempt is in the future")
    if "source_attempt" in publish:
        require(number(publish["source_attempt"], "source attempt") <= number(publish["sdk_attempt"], "SDK attempt") <=
                number(publish["publish_attempt"], "candidate consumer attempt"), "publication attempts are out of order")
    if stage == "sign":
        native = needs["native-aarch64"]["outputs"]
        require(number(publish["publish_attempt"], "publish attempt") <= number(native["native_attempt"], "native attempt") <= attempt,
                "candidate producer attempts are out of order")
    return needs


def validate(value):
    fields = ("schema_version", "kind", "repository", "workflow", "source_commit", "run_id", "sign_attempt",
              "candidate_manifest_sha256", "probe_bundle_sha256", "native_report_sha256", "artifacts")
    require(type(value) is dict, "candidate recovery must be an object")
    exact_fields(value, fields + (("publication",) if value.get("schema_version") == 2 else ()), "candidate recovery")
    require(type(value["schema_version"]) is int and value["schema_version"] in (1, 2) and
            value["kind"] == "crossforge-candidate-recovery", "unsupported candidate recovery schema")
    require(value["repository"] == REPOSITORY and value["workflow"] == WORKFLOW, "candidate recovery source is not trusted")
    require(type(value["source_commit"]) is str and re.fullmatch(r"[0-9a-f]{40}", value["source_commit"]),
            "candidate recovery source commit is invalid")
    for field in ("run_id", "sign_attempt"):
        positive(value[field], field)
    for field in ("candidate_manifest_sha256", "probe_bundle_sha256", "native_report_sha256"):
        digest_value(value[field], field)
    exact_fields(value["artifacts"], PREFIXES, "candidate recovery artifacts")
    ids = []
    for role, item in value["artifacts"].items():
        exact_fields(item, ("id", "name", "attempt"), "candidate recovery artifact")
        ids.append(positive(item["id"], "artifact ID"))
        positive(item["attempt"], "artifact producer attempt")
        require(item["name"] == "%s-%d-%d" % (PREFIXES[role], value["run_id"], item["attempt"]),
                "artifact name differs from its original run and attempt")
    artifacts = value["artifacts"]
    require(len(set(ids)) == len(ids), "candidate artifact IDs must be distinct")
    require(artifacts["identity"]["attempt"] == artifacts["probes"]["attempt"] <= artifacts["native"]["attempt"] <= value["sign_attempt"],
            "candidate artifact producer attempts are out of order")
    if value["schema_version"] == 2:
        exact_fields(value["publication"], ("source", "sdk"), "candidate publication lineage")
        for item in value["publication"].values():
            exact_fields(item, ("attempt", "checkpoint_sha256"), "candidate publication producer")
            positive(item["attempt"], "publication attempt")
            digest_value(item["checkpoint_sha256"], "publication checkpoint SHA256")
        require(value["publication"]["source"]["attempt"] <= value["publication"]["sdk"]["attempt"] <= artifacts["identity"]["attempt"],
                "candidate publication attempts are out of order")
    return value


def document(candidate, needs, run_id, attempt):
    upstream(needs, attempt, "sign")
    publish, native = needs["publish"]["outputs"], needs["native-aarch64"]["outputs"]
    require(content_sha256(candidate) == publish["candidate_sha256"] and candidate["digest"] == publish["candidate_digest"] and
            candidate["platform_manifest_digest"] == publish["platform_digest"], "candidate differs from original published identity")
    artifacts = {}
    for role, source, id_field, attempt_field in (("identity", publish, "identity_artifact_id", "publish_attempt"),
            ("probes", publish, "probe_artifact_id", "publish_attempt"), ("native", native, "native_artifact_id", "native_attempt")):
        producer_attempt = number(source[attempt_field], attempt_field)
        artifacts[role] = {"id": number(source[id_field], id_field), "attempt": producer_attempt,
            "name": "%s-%d-%d" % (PREFIXES[role], run_id, producer_attempt)}
    value = {"schema_version": 1, "kind": "crossforge-candidate-recovery", "repository": REPOSITORY,
        "workflow": WORKFLOW, "source_commit": candidate["source_commit"], "run_id": run_id, "sign_attempt": attempt,
        "candidate_manifest_sha256": content_sha256(candidate), "probe_bundle_sha256": publish["probe_bundle_sha256"],
        "native_report_sha256": native["native_report_sha256"], "artifacts": artifacts}
    if "source_attempt" in publish:
        value["schema_version"] = 2
        value["publication"] = {phase: {"attempt": number(publish[phase + "_attempt"], phase + " publication attempt"),
            "checkpoint_sha256": publish[phase + "_checkpoint_sha256"]} for phase in ("source", "sdk")}
    return validate(value)


def bind(value, candidate_sha256, run):
    validate(value)
    require(value["repository"] == run["repository"] and value["workflow"] == run["workflow_path"] and
            value["run_id"] == run["id"] and value["source_commit"] == run["head_sha"] and value["sign_attempt"] == run["attempt"],
            "candidate recovery differs from the exact successful run")
    require(value["candidate_manifest_sha256"] == candidate_sha256, "candidate recovery manifest differs")
    return copy.deepcopy(value)


def artifact_names(value):
    validate(value)
    return sorted([item["name"] for item in value["artifacts"].values()] +
                  ["candidate-signature-%d-%d" % (value["run_id"], value["sign_attempt"])])


def verify_artifacts(value, pages):
    """Check immutable IDs against read-only GitHub run artifact metadata."""
    validate(value)
    require(type(pages) is list and pages, "candidate artifact metadata pages are missing")
    records = []
    for page in pages:
        require(type(page) is dict and type(page.get("artifacts")) is list, "invalid artifact metadata page")
        records.extend(page["artifacts"])
    require(all(type(item) is dict for item in records), "invalid artifact metadata record")
    for selected in value["artifacts"].values():
        matches = [item for item in records if item.get("id") == selected["id"]]
        require(len(matches) == 1, "original candidate artifact is missing or duplicated")
        item = matches[0]
        run = item.get("workflow_run")
        require(item.get("name") == selected["name"] and item.get("expired") is False and type(run) is dict,
                "original candidate artifact name or availability differs")
        require(run.get("id") == value["run_id"] and run.get("head_sha") == value["source_commit"] and
                run.get("head_branch") == "main" and type(run.get("repository_id")) is int and
                run["repository_id"] > 0 and run.get("head_repository_id") == run["repository_id"],
                "original candidate artifact run or source differs")
    return value
