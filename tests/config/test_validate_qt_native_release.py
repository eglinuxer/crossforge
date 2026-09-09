import ast
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/validate-qt-native-release.py"
VALIDATOR = runpy.run_path(str(SCRIPT))
RUNTIME = VALIDATOR["RUNTIME"]


class ValidateQtNativeReleaseTests(unittest.TestCase):
    def fixture(self):
        release = {
            "base_image": {
                "digest": "sha256:" + "1" * 64,
                "manifests": {"arm64": "sha256:" + "2" * 64},
            }
        }
        component = {
            "dependencies": [
                {
                    "component": "future/qt-qualification",
                    "canonical_sha256": "3" * 64,
                }
            ]
        }
        contract = {
            "qualification_component": component,
            "plan_sha256": "4" * 64,
            "plan": {
                "build_qualification": {"plan_sha256": "5" * 64}
            },
        }
        candidate = {
            "source_commit": "6" * 40,
            "repository": "ghcr.io/eglinuxer/crossforge",
            "digest": "sha256:" + "7" * 64,
            "platform_manifest_digest": "sha256:" + "8" * 64,
        }
        runtime_qualification, build_qualification = VALIDATOR[
            "qualification_identities"
        ](contract)
        build = {
            "identity": VALIDATOR["TARGET"],
            "qualification_component": build_qualification,
            "inputs": {"plan_sha256": "5" * 64},
        }
        base = {
            "index_digest": release["base_image"]["digest"],
            "manifest_digest": release["base_image"]["manifests"][
                "arm64"
            ],
        }
        overlay = {
            "identity": {
                "target": VALIDATOR["TARGET"],
                "base_image": base,
                "release_sha256": VALIDATOR["QUALIFICATION"][
                    "canonical_sha256"
                ](release),
                "runtime_qualification": runtime_qualification,
            }
        }
        candidate_sha256 = "9" * 64
        rootfs_sha256 = "a" * 64
        report = {
            "identity": {
                "target": VALIDATOR["TARGET"],
                "tier": "native-release",
                "base_image": base,
                "release_sha256": overlay["identity"]["release_sha256"],
                "runtime_qualification": runtime_qualification,
                "candidate": VALIDATOR["candidate_identity"](
                    candidate, candidate_sha256
                ),
                "input_rootfs_sha256": rootfs_sha256,
                "build_evidence_sha256": RUNTIME["canonical_sha256"](
                    build
                ),
                "overlay_evidence_sha256": RUNTIME["canonical_sha256"](
                    overlay
                ),
            },
            "executor": {"kind": "native"},
        }
        return {
            "report": report,
            "candidate": candidate,
            "candidate_sha256": candidate_sha256,
            "release": release,
            "contract": contract,
            "build": build,
            "overlay": overlay,
            "source_commit": candidate["source_commit"],
            "candidate_digest": candidate["digest"],
            "rootfs_sha256": rootfs_sha256,
        }

    def validate(self, fixture):
        return VALIDATOR["validate_native_binding"](
            fixture["report"],
            fixture["candidate"],
            fixture["candidate_sha256"],
            fixture["release"],
            fixture["contract"],
            fixture["build"],
            fixture["overlay"],
            fixture["source_commit"],
            fixture["candidate_digest"],
            fixture["rootfs_sha256"],
        )

    def test_exact_candidate_and_native_qt_evidence_are_accepted(self):
        fixture = self.fixture()
        self.assertIs(self.validate(fixture), fixture["report"])

    def test_candidate_rootfs_and_input_evidence_drift_are_rejected(self):
        mutations = (
            lambda value: value.__setitem__(
                "candidate_digest", "sha256:" + "b" * 64
            ),
            lambda value: value.__setitem__("rootfs_sha256", "c" * 64),
            lambda value: value["report"]["identity"]["candidate"].__setitem__(
                "canonical_sha256", "d" * 64
            ),
            lambda value: value["build"]["inputs"].__setitem__(
                "plan_sha256", "e" * 64
            ),
            lambda value: value["overlay"]["identity"].__setitem__(
                "release_sha256", "f" * 64
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                fixture = self.fixture()
                mutate(fixture)
                with self.assertRaises(VALIDATOR["ValidationError"]):
                    self.validate(fixture)

    def test_runtime_document_semantics_remain_independently_validatable(self):
        probes = []
        for index, artifact in enumerate(RUNTIME["LOADER_PROBES"]):
            dependencies = ["needed:lib%d.so" % index]
            probes.append(
                {
                    "artifact": artifact,
                    "dependencies": dependencies,
                    "sha256": RUNTIME["canonical_sha256"](dependencies),
                }
            )
        document = {
            "$schema": RUNTIME["SCHEMA_ID"],
            "schema_version": 1,
            "kind": "crossforge-qt-target-runtime",
            "qt_version": "6.8.4",
            "identity": {
                "target": VALIDATOR["TARGET"],
                "tier": "native-release",
                "base_image": {
                    "index_digest": "sha256:" + "1" * 64,
                    "manifest_digest": "sha256:" + "2" * 64,
                },
                "release_sha256": "3" * 64,
                "runtime_qualification": {
                    "component": "future/qt-runtime-qualification",
                    "canonical_sha256": "4" * 64,
                    "plan_sha256": "5" * 64,
                },
                "build_evidence_sha256": "6" * 64,
                "overlay_evidence_sha256": "7" * 64,
                "artifact_tree": {"entries": 1, "sha256": "8" * 64},
                "candidate": {
                    "source_commit": "9" * 40,
                    "repository": "ghcr.io/eglinuxer/crossforge",
                    "digest": "sha256:" + "a" * 64,
                    "platform_manifest_digest": "sha256:" + "b" * 64,
                    "canonical_sha256": "c" * 64,
                },
                "input_rootfs_sha256": "d" * 64,
            },
            "executor": {"kind": "native"},
            "execution": {
                "consumer_sha256": "e" * 64,
                "exit_code": 0,
                "stdout_sha256": "f" * 64,
                "stderr_sha256": "0" * 64,
                "loader": probes[0]["dependencies"],
                "loader_sha256": probes[0]["sha256"],
                "loader_probes": probes,
                "platform_plugin": RUNTIME["PLUGIN"],
            },
            "checks": {
                "clean_rocky": True,
                "dependency_closure": True,
                "plugin_dependency_closure": True,
                "offscreen_widget": True,
                "platform_plugin_loaded": True,
                "no_build_tree": True,
                "no_sysroot_marker": True,
            },
        }
        self.assertIs(
            RUNTIME["validate_evidence_document"](document), document
        )
        document["execution"]["loader_probes"][0]["sha256"] = "1" * 64
        with self.assertRaises(RUNTIME["ValidationError"]):
            RUNTIME["validate_evidence_document"](document)

    def test_candidate_does_not_require_optional_qt_evidence(self):
        workflow = (REPOSITORY / ".github/workflows/candidate.yml").read_text()
        for evidence in ("validate-qt-native-release.py", "qt-target-build.json",
                         "qt-runtime-overlay.json", "qt-native-aarch64-runtime.json"):
            self.assertNotIn(evidence, workflow)
        self.assertIn("native-aarch64-release.py validate", workflow)
        self.assertTrue(SCRIPT.is_file())

    def test_validator_is_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
