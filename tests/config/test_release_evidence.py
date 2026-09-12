import ast
import base64
import json
import runpy
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/release_evidence.py"
EVIDENCE = runpy.run_path(str(SCRIPT))
CANDIDATE = EVIDENCE["CANDIDATE"]
SOURCE = EVIDENCE["SOURCE"]
PROMOTION = EVIDENCE["PROMOTION"]
SIGSTORE = EVIDENCE["SIGSTORE"]


class ReleaseEvidenceTests(unittest.TestCase):
    def test_durable_payload_count_matches_the_strict_schema_and_docs(self):
        self.assertEqual(len(EVIDENCE["PAYLOAD_NAMES"]), 14)
        schema = json.loads(
            (
                REPOSITORY
                / "config/schemas/release-evidence-bundle-manifest.schema.json"
            ).read_text(encoding="utf-8")
        )
        files = schema["properties"]["files"]
        self.assertEqual(files["minItems"], 14)
        self.assertEqual(files["maxItems"], 14)
        self.assertIn("Fourteen original evidence files", (
            REPOSITORY / "README.md"
        ).read_text(encoding="utf-8"))
        self.assertIn("十四份", (
            REPOSITORY / "docs/architecture.md"
        ).read_text(encoding="utf-8"))

    def write_json(self, path, document):
        path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def fixture(self, directory):
        root = Path(directory)
        release = EVIDENCE["load_release"](
            REPOSITORY / "config/release.json",
            REPOSITORY / "config/schemas/release.schema.json",
        )
        release_sha256 = CANDIDATE["canonical_sha256"](release)
        commit = "1" * 40
        source_identity = {
            "$schema": (
                "https://crossforge.dev/schemas/"
                "source-bundle-identity.schema.json"
            ),
            "schema_version": 1,
            "kind": "crossforge-source-bundle-identity",
            "source_commit": commit,
            "release_sha256": release_sha256,
            "archive": {
                "file": "crossforge-source-%s.tar.zst" % commit,
                "sha256": "6" * 64,
                "size": 2866173957,
            },
        }
        candidate = CANDIDATE["candidate_document"](
            release,
            commit,
            "sha256:" + "2" * 64,
            "sha256:" + "3" * 64,
            "sha256:" + "4" * 64,
            "sha256:" + "5" * 64,
            source_identity,
        )
        source_binding = SOURCE["source_binding"](
            release,
            commit,
            candidate["source_bundle"]["digest"],
            candidate["source_bundle"]["platform_manifest_digest"],
            source_identity["archive"]["sha256"],
            source_identity["archive"]["size"],
        )
        candidate_run = PROMOTION["validate_candidate_run"](
            {
                "id": 123456,
                "run_attempt": 2,
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "head_branch": "main",
                "head_sha": commit,
                "path": ".github/workflows/candidate.yml",
                "repository": {"full_name": "eglinuxer/crossforge"},
                "head_repository": {"full_name": "eglinuxer/crossforge"},
                "html_url": (
                    "https://github.com/eglinuxer/crossforge/"
                    "actions/runs/123456"
                ),
            },
            "eglinuxer/crossforge",
            123456,
        )
        promotion = PROMOTION["promotion_document"](
            release,
            candidate,
            CANDIDATE["canonical_sha256"](candidate),
            candidate_run,
        )
        sigstore = {
            "$schema": SIGSTORE["SCHEMA_ID"],
            "schema_version": 1,
            "kind": "crossforge-sigstore-source-verification",
            "status": "verified",
            "release_sha256": release_sha256,
            "verifier": {
                "version": release["sigstore"]["verifier"]["version"],
                "git_commit": release["sigstore"]["verifier"]["git_commit"],
                "binary_sha256": release["sigstore"]["verifier"]["binary"][
                    "sha256"
                ],
                "kms_bundle_sha256": release["sigstore"]["verifier"][
                    "kms_bundle"
                ]["sha256"],
                "tuf_root_version": release["sigstore"]["trust"][
                    "final_root_version"
                ],
                "tuf_targets_version": release["sigstore"]["trust"][
                    "targets_version"
                ],
                "trusted_root_sha256": release["sigstore"]["trust"][
                    "trusted_root_sha256"
                ],
                "artifact_key_sha256": release["sigstore"]["trust"][
                    "artifact_key_sha256"
                ],
            },
            "artifacts": SIGSTORE["expected_artifacts"](release),
            "checks": {
                "cosign_bootstrap_signature": True,
                "cosign_bundle": True,
                "certificate_chain": True,
                "certificate_identity": True,
                "sct": True,
                "transparency_log": True,
                "inclusion_proof": True,
                "signed_timestamps_when_present": True,
                "offline": True,
            },
        }
        paths = {name: root / name for name in EVIDENCE["PAYLOAD_NAMES"]}
        self.write_json(paths["candidate.json"], candidate)
        self.write_json(paths["source-bundle.json"], source_identity)
        self.write_json(paths["source-binding.json"], source_binding)
        self.write_json(paths["release-promotion.json"], promotion)
        self.write_json(paths["release.json"], release)
        self.write_json(paths["sigstore-verification.json"], sigstore)
        for name, image_kind, selected in (
            ("sdk-attestations.json", "sdk-candidate", candidate),
            (
                "source-attestations.json",
                "source-bundle",
                candidate["source_bundle"],
            ),
        ):
            self.write_json(
                paths[name],
                {
                    "$schema": EVIDENCE["IMAGE_ATTESTATIONS"]["SCHEMA_ID"],
                    "schema_version": 1,
                    "kind": "crossforge-public-image-attestations",
                    "image_kind": image_kind,
                    "source_commit": commit,
                    "repository": selected["repository"],
                    "index_digest": selected["digest"],
                    "platform_manifest_digest": selected[
                        "platform_manifest_digest"
                    ],
                    "attestation_manifest": {
                        "digest": "sha256:" + "9" * 64,
                        "size": 123,
                    },
                    "attestations": [
                        {
                            "predicate_type": "https://slsa.dev/provenance/v1",
                            "digest": "sha256:" + "a" * 64,
                            "size": 456,
                            "statement_type": "https://in-toto.io/Statement/v1",
                        },
                        {
                            "predicate_type": "https://spdx.dev/Document",
                            "digest": "sha256:" + "b" * 64,
                            "size": 789,
                            "statement_type": "https://in-toto.io/Statement/v1",
                        },
                    ],
                    "checks": {
                        "oci_artifact": True,
                        "subject_bound": True,
                        "blob_digests": True,
                        "max_provenance": True,
                        "source_revision": True,
                        "spdx_document": True,
                    },
                },
            )
        generator = release["sbom"]["generator"]
        self.write_json(
            paths["sbom-generator-image.json"],
            {
                "$schema": (
                    "https://crossforge.dev/schemas/"
                    "sbom-generator-image.schema.json"
                ),
                "schema_version": 1,
                "kind": "crossforge-sbom-generator-image",
                "version": generator["version"],
                "repository": generator["repository"],
                "index_digest": generator["digest"],
                "manifest_digest": generator["manifest_digest"],
                "attestation_manifest_digest": generator["provenance"]
                ["attestation_manifest_digest"],
                "provenance_digest": generator["provenance"][
                    "predicate_digest"
                ],
                "source": {
                    "tag": generator["source"]["tag"],
                    "tag_object": generator["source"]["tag_object"],
                    "commit": generator["source"]["commit"],
                },
                "checks": {
                    "index": True,
                    "platform": True,
                    "oci_subject": True,
                    "slsa_v1": True,
                    "source": True,
                    "max_provenance": True,
                },
            },
        )
        signature = [
            {
                "critical": {
                    "identity": {
                        "docker-reference": candidate["repository"]
                    },
                    "image": {
                        "docker-manifest-digest": candidate["digest"]
                    },
                }
            }
        ]
        self.write_json(paths["candidate-signature.json"], signature)
        signature[0]["critical"]["identity"]["docker-reference"] = candidate[
            "source_bundle"
        ]["repository"]
        signature[0]["critical"]["image"]["docker-manifest-digest"] = candidate[
            "source_bundle"
        ]["digest"]
        self.write_json(paths["source-bundle-signature.json"], signature)
        paths["native-aarch64-probes.tar"].write_bytes(b"probe bundle\n")
        self.write_json(
            paths["native-aarch64.json"], {"bundle_sha256": "7" * 64}
        )
        trusted_root_payload = base64.b64decode(
            (
                REPOSITORY / release["sigstore"]["trust"]["trusted_root_evidence"]
            ).read_bytes()
        )
        paths["sigstore-trusted-root.json"].write_bytes(trusted_root_payload)
        return {
            "root": root,
            "release": release,
            "candidate": candidate,
            "promotion": promotion,
            "paths": paths,
            "schema": EVIDENCE["load_schema"](
                REPOSITORY
                / "config/schemas/release-evidence-bundle-manifest.schema.json"
            ),
        }

    def validators(self):
        return mock.patch.dict(
            EVIDENCE["NATIVE"], {"validate_report": lambda _arguments: None}
        )

    def create(self, fixture):
        native_patch = self.validators()
        with native_patch:
            manifest = EVIDENCE["validate_inputs"](
                fixture["paths"], fixture["release"], fixture["schema"]
            )
        archive = fixture["root"] / "crossforge-v0.1.0-release-evidence.tar"
        checksum = fixture["root"] / (
            "crossforge-v0.1.0-release-evidence.tar.sha256"
        )
        EVIDENCE["build_archive"](archive, fixture["paths"], manifest)
        EVIDENCE["write_sidecar"](checksum, archive)
        return manifest, archive, checksum

    def test_archive_is_deterministic_strict_and_fully_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            manifest, archive, checksum = self.create(fixture)
            first = EVIDENCE["sha256_file"](archive)
            self.assertFalse(
                EVIDENCE["build_archive"](
                    archive, fixture["paths"], manifest
                )
            )
            self.assertEqual(EVIDENCE["sha256_file"](archive), first)
            with tarfile.open(str(archive), "r:") as stream:
                members = stream.getmembers()
            self.assertEqual(
                [member.name for member in members],
                [EVIDENCE["MANIFEST_NAME"]] + list(EVIDENCE["PAYLOAD_NAMES"]),
            )
            self.assertTrue(
                all(
                    member.mode == 0o644
                    and member.uid == 0
                    and member.gid == 0
                    and member.mtime == 0
                    for member in members
                )
            )
            arguments = SimpleNamespace(archive=archive, sha256=checksum)
            native_patch = self.validators()
            with native_patch:
                observed = EVIDENCE["validate_archive"](
                    arguments, fixture["release"], fixture["schema"]
                )
            self.assertEqual(observed, manifest)
            self.assertEqual(
                observed["candidate"]["digest"], fixture["candidate"]["digest"]
            )

    def test_signature_source_and_checksum_drift_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            wrong_signature = json.loads(
                fixture["paths"]["candidate-signature.json"].read_text()
            )
            wrong_signature[0]["critical"]["image"][
                "docker-manifest-digest"
            ] = "sha256:" + "0" * 64
            self.write_json(
                fixture["paths"]["candidate-signature.json"], wrong_signature
            )
            native_patch = self.validators()
            with native_patch, self.assertRaisesRegex(
                EVIDENCE["ReleaseEvidenceError"], "signature.*identity differs"
            ):
                EVIDENCE["validate_inputs"](
                    fixture["paths"], fixture["release"], fixture["schema"]
                )

        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            _manifest, archive, checksum = self.create(fixture)
            checksum.write_text("0" * 64 + "  " + archive.name + "\n")
            native_patch = self.validators()
            with native_patch, self.assertRaisesRegex(
                EVIDENCE["ReleaseEvidenceError"], "archive checksum differs"
            ):
                EVIDENCE["validate_archive"](
                    SimpleNamespace(archive=archive, sha256=checksum),
                    fixture["release"],
                    fixture["schema"],
                )

    def test_nonregular_input_and_archive_member_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            target = fixture["paths"]["candidate-signature.json"]
            target.unlink()
            target.symlink_to(fixture["paths"]["source-bundle-signature.json"])
            native_patch = self.validators()
            with native_patch, self.assertRaises(
                EVIDENCE["ReleaseEvidenceError"]
            ):
                EVIDENCE["validate_inputs"](
                    fixture["paths"], fixture["release"], fixture["schema"]
                )

            unsafe = fixture["root"] / "unsafe.tar"
            with tarfile.open(str(unsafe), "w") as archive:
                member = tarfile.TarInfo("../escape")
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
                archive.addfile(member)
            with self.assertRaises(EVIDENCE["ReleaseEvidenceError"]):
                EVIDENCE["extract_archive"](unsafe, fixture["root"] / "extract")

    def test_script_remains_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
