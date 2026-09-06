import ast
import copy
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/image_attestations.py"
ATTESTATIONS = runpy.run_path(str(SCRIPT))


class ImageAttestationTests(unittest.TestCase):
    def payload(self, document):
        return json.dumps(
            document, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def descriptor(self, payload, predicate_type=None):
        result = {
            "mediaType": ATTESTATIONS["IN_TOTO"],
            "digest": ATTESTATIONS["sha256_bytes"](payload),
            "size": len(payload),
        }
        if predicate_type is not None:
            result["annotations"] = {
                "in-toto.io/predicate-type": predicate_type
            }
        return result

    def fixture(self, directory, image_kind="sdk-candidate"):
        root = Path(directory)
        commit = "1" * 40
        platform_digest = "sha256:" + "2" * 64
        platform = {
            "mediaType": ATTESTATIONS["OCI_MANIFEST"],
            "digest": platform_digest,
            "size": 1234,
            "platform": {"os": "linux", "architecture": "amd64"},
        }
        subject = [
            {
                "name": "pkg:docker/crossforge?platform=linux%2Famd64",
                "digest": {"sha256": platform_digest.split(":", 1)[1]},
            }
        ]
        provenance = {
            "_type": ATTESTATIONS["STATEMENT"],
            "subject": subject,
            "predicateType": ATTESTATIONS["PROVENANCE"],
            "predicate": {
                "buildType": ATTESTATIONS["BUILDKIT"],
                "invocation": {
                    "parameters": {"args": {"target": image_kind}}
                },
                "buildConfig": {"llbDefinition": [{"id": "step0"}]},
                "metadata": {
                    "completeness": {
                        "parameters": True,
                        "environment": True,
                        "materials": True,
                    },
                    ATTESTATIONS["BUILDKIT_METADATA"]: {
                        "vcs": {
                            "revision": commit,
                            "source": "https://github.com/eglinuxer/crossforge",
                        }
                    },
                },
            },
        }
        sbom = {
            "_type": ATTESTATIONS["STATEMENT"],
            "subject": subject,
            "predicateType": ATTESTATIONS["SPDX"],
            "predicate": {
                "spdxVersion": "SPDX-2.3",
                "SPDXID": "SPDXRef-DOCUMENT",
                "dataLicense": "CC0-1.0",
                "creationInfo": {"creators": ["Tool: buildkit"]},
                "name": "crossforge",
            },
        }
        provenance_payload = self.payload(provenance)
        sbom_payload = self.payload(sbom)
        layers = [
            self.descriptor(provenance_payload, ATTESTATIONS["PROVENANCE"]),
            self.descriptor(sbom_payload, ATTESTATIONS["SPDX"]),
        ]
        manifest = {
            "schemaVersion": 2,
            "mediaType": ATTESTATIONS["OCI_MANIFEST"],
            "artifactType": ATTESTATIONS["ATTESTATION_ARTIFACT"],
            "config": {
                "mediaType": "application/vnd.oci.empty.v1+json",
                "digest": ATTESTATIONS["EMPTY_CONFIG_DIGEST"],
                "size": 2,
                "data": "e30=",
            },
            "layers": layers,
            "subject": {
                "mediaType": platform["mediaType"],
                "digest": platform["digest"],
                "size": platform["size"],
                "platform": platform["platform"],
            },
        }
        manifest_payload = self.payload(manifest)
        attestation = {
            "mediaType": ATTESTATIONS["OCI_MANIFEST"],
            "digest": ATTESTATIONS["sha256_bytes"](manifest_payload),
            "size": len(manifest_payload),
            "annotations": {
                "vnd.docker.reference.type": "attestation-manifest",
                "vnd.docker.reference.digest": platform_digest,
            },
            "platform": {"os": "unknown", "architecture": "unknown"},
        }
        index = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [platform, attestation],
        }
        index_payload = self.payload(index)
        paths = {
            "index": root / "index.json",
            "attestation_manifest": root / "attestation-manifest.json",
            "provenance": root / "provenance.json",
            "sbom": root / "sbom.json",
            "output": root / "report.json",
        }
        paths["index"].write_bytes(index_payload)
        paths["attestation_manifest"].write_bytes(manifest_payload)
        paths["provenance"].write_bytes(provenance_payload)
        paths["sbom"].write_bytes(sbom_payload)
        arguments = SimpleNamespace(
            index=paths["index"],
            attestation_manifest=paths["attestation_manifest"],
            provenance=paths["provenance"],
            sbom=paths["sbom"],
            expected_index_digest=ATTESTATIONS["sha256_bytes"](index_payload),
            platform_manifest_digest=platform_digest,
            image_kind=image_kind,
            repository="ghcr.io/eglinuxer/crossforge",
            source_commit=commit,
            output=paths["output"],
            schema=REPOSITORY / "config/schemas/image-attestations.schema.json",
        )
        return {
            "arguments": arguments,
            "index": index,
            "manifest": manifest,
            "provenance": provenance,
            "sbom": sbom,
            "paths": paths,
        }

    def rewrite(self, fixture, name, document):
        payload = self.payload(document)
        path_name = "attestation_manifest" if name == "manifest" else name
        fixture["paths"][path_name].write_bytes(payload)
        return payload

    def test_exact_oci_artifact_max_provenance_and_spdx_are_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            report = ATTESTATIONS["create_report"](fixture["arguments"])
            ATTESTATIONS["validate_schema"](
                report, fixture["arguments"].schema
            )
            self.assertEqual(
                [record["predicate_type"] for record in report["attestations"]],
                [ATTESTATIONS["PROVENANCE"], ATTESTATIONS["SPDX"]],
            )
            self.assertTrue(report["checks"]["max_provenance"])
            self.assertTrue(
                ATTESTATIONS["write_json_once"](
                    fixture["arguments"].output, report
                )
            )
            self.assertFalse(
                ATTESTATIONS["write_json_once"](
                    fixture["arguments"].output, report
                )
            )

    def test_index_manifest_and_blob_drift_fail_closed(self):
        mutations = (
            ("index", lambda value: value["manifests"].append(copy.deepcopy(value["manifests"][1]))),
            ("manifest", lambda value: value["subject"].__setitem__("digest", "sha256:" + "0" * 64)),
            ("manifest", lambda value: value["layers"].pop()),
            ("provenance", lambda value: value["predicate"].pop("buildConfig")),
            (
                "provenance",
                lambda value: value["predicate"]["metadata"][
                    ATTESTATIONS["BUILDKIT_METADATA"]
                ]["vcs"].__setitem__("revision", "0" * 40),
            ),
            ("sbom", lambda value: value["predicate"].__setitem__("SPDXID", "bad")),
        )
        for name, mutate in mutations:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                fixture = self.fixture(temporary)
                document = copy.deepcopy(fixture[name])
                mutate(document)
                self.rewrite(fixture, name, document)
                with self.assertRaises(ATTESTATIONS["AttestationError"]):
                    ATTESTATIONS["create_report"](fixture["arguments"])

    def test_target_and_blob_digest_mismatches_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            fixture["arguments"].image_kind = "source-bundle"
            with self.assertRaisesRegex(
                ATTESTATIONS["AttestationError"], "build target differs"
            ):
                ATTESTATIONS["create_report"](fixture["arguments"])

    def test_provenance_mode_revision_and_spdx_semantics_are_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            arguments = fixture["arguments"]
            ATTESTATIONS["validate_provenance"](
                fixture["provenance"],
                arguments.platform_manifest_digest,
                arguments.source_commit,
                arguments.image_kind,
            )
            ATTESTATIONS["validate_spdx"](
                fixture["sbom"], arguments.platform_manifest_digest
            )
            for mutate in (
                lambda value: value["predicate"].pop("buildConfig"),
                lambda value: value["predicate"]["metadata"][
                    ATTESTATIONS["BUILDKIT_METADATA"]
                ]["vcs"].__setitem__("revision", "0" * 40),
                lambda value: value["predicate"]["invocation"][
                    "parameters"
                ]["args"].__setitem__("target", "source-bundle"),
            ):
                provenance = copy.deepcopy(fixture["provenance"])
                mutate(provenance)
                with self.assertRaises(ATTESTATIONS["AttestationError"]):
                    ATTESTATIONS["validate_provenance"](
                        provenance,
                        arguments.platform_manifest_digest,
                        arguments.source_commit,
                        arguments.image_kind,
                    )
            sbom = copy.deepcopy(fixture["sbom"])
            sbom["predicate"]["SPDXID"] = "bad"
            with self.assertRaises(ATTESTATIONS["AttestationError"]):
                ATTESTATIONS["validate_spdx"](
                    sbom, arguments.platform_manifest_digest
                )
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            with fixture["paths"]["sbom"].open("ab") as stream:
                stream.write(b" ")
            with self.assertRaisesRegex(
                ATTESTATIONS["AttestationError"], "byte size differs"
            ):
                ATTESTATIONS["create_report"](fixture["arguments"])

    def test_nonregular_inputs_and_python_syntax_are_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            fixture["paths"]["sbom"].unlink()
            fixture["paths"]["sbom"].symlink_to(
                fixture["paths"]["provenance"].name
            )
            with self.assertRaises(ATTESTATIONS["AttestationError"]):
                ATTESTATIONS["create_report"](fixture["arguments"])
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
