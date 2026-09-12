import ast
import copy
import json
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
FETCH_SCRIPT = REPOSITORY / "scripts/fetch-sigstore-assets.py"
VERIFY_SCRIPT = REPOSITORY / "scripts/verify-sigstore-assets.py"
REPORT_SCRIPT = REPOSITORY / "scripts/validate-sigstore-report.py"
FETCH = runpy.run_path(str(FETCH_SCRIPT))
VERIFY = runpy.run_path(str(VERIFY_SCRIPT))
REPORT = runpy.run_path(str(REPORT_SCRIPT))


class SigstoreAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = FETCH["load_release"](
            REPOSITORY / "config/release.json",
            REPOSITORY / "config/schemas/release.schema.json",
        )
        cls.schema = REPORT["STRICT"]["load_json"](
            REPOSITORY
            / "config/schemas/sigstore-verification.schema.json"
        )

    def report(self):
        verifier = self.release["sigstore"]["verifier"]
        trust = self.release["sigstore"]["trust"]
        return {
            "$schema": REPORT["SCHEMA_ID"],
            "schema_version": 1,
            "kind": "crossforge-sigstore-source-verification",
            "status": "verified",
            "release_sha256": REPORT["canonical_sha256"](self.release),
            "verifier": {
                "version": verifier["version"],
                "git_commit": verifier["git_commit"],
                "binary_sha256": verifier["binary"]["sha256"],
                "kms_bundle_sha256": verifier["kms_bundle"]["sha256"],
                "tuf_root_version": trust["final_root_version"],
                "tuf_targets_version": trust["targets_version"],
                "trusted_root_sha256": trust["trusted_root_sha256"],
                "artifact_key_sha256": trust["artifact_key_sha256"],
            },
            "artifacts": REPORT["expected_artifacts"](self.release),
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

    def test_release_locks_cosign_and_all_source_statuses_are_verified(self):
        verifier = self.release["sigstore"]["verifier"]
        self.assertEqual(verifier["status"], "locked")
        self.assertEqual(verifier["version"], "3.1.3")
        self.assertEqual(
            verifier["git_commit"],
            "11926fa5bbbbde47e88fc006b625a17769b743b2",
        )
        self.assertTrue(
            all(
                entry["source"]["sigstore"]["verification"]
                == "verified"
                for entry in self.release["python"]["versions"]
            )
        )
        self.assertEqual(
            self.release["nfpm"]["sigstore"]["status"], "verified"
        )

    def test_download_plan_is_exact_sorted_and_host_limited(self):
        plan = FETCH["asset_plan"](self.release)
        self.assertEqual(len(plan), 10)
        self.assertEqual(
            [record["file"] for record in plan],
            sorted(record["file"] for record in plan),
        )
        self.assertEqual(
            {record["file"] for record in plan},
            {
                "cosign",
                "cosign-kms.sigstore.json",
                "nfpm-checksums.txt",
                "nfpm-checksums.sigstore.json",
            }
            | {
                "Python-%s.tar.xz" % entry["version"]
                for entry in self.release["python"]["versions"]
            },
        )
        with self.assertRaises(FETCH["ValidationError"]):
            FETCH["asset_record"](
                "asset",
                {
                    "url": "https://example.com/asset",
                    "sha256": "0" * 64,
                    "size": 1,
                },
            )

    def test_supply_validator_rejects_weakened_sigstore_policy(self):
        release = copy.deepcopy(self.release)
        release["sigstore"]["verifier"]["policy"][
            "require_tlog"
        ] = False
        with self.assertRaisesRegex(
            VERIFY["SUPPLY"]["EvidenceError"], "verifier policy differs"
        ):
            VERIFY["SUPPLY"]["validate_evidence"](
                release, REPOSITORY
            )

    def test_download_manifest_rejects_extra_files(self):
        payload = b"locked"
        record = {
            "file": "asset",
            "url": "https://github.com/asset",
            "sha256": VERIFY["sha256_bytes"](payload),
            "size": len(payload),
            "mode": 0o644,
        }
        manifest = {
            "schema_version": 1,
            "kind": "crossforge-sigstore-downloads",
            "files": [record],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "asset").write_bytes(payload)
            (root / "asset").chmod(0o644)
            (root / "downloads.json").write_text(
                json.dumps(manifest) + "\n", encoding="utf-8"
            )
            self.assertEqual(
                VERIFY["load_downloads"](root, [record]),
                {"asset": root / "asset"},
            )
            (root / "unexpected").write_text("x", encoding="utf-8")
            with self.assertRaises(VERIFY["ValidationError"]):
                VERIFY["load_downloads"](root, [record])

    def test_report_is_release_bound_and_tamper_evident(self):
        report = self.report()
        self.assertIs(
            REPORT["validate_report_document"](
                report, self.release, self.schema
            ),
            report,
        )
        mutations = []
        wrong_release = copy.deepcopy(report)
        wrong_release["release_sha256"] = "0" * 64
        mutations.append(wrong_release)
        wrong_verifier = copy.deepcopy(report)
        wrong_verifier["verifier"]["binary_sha256"] = "0" * 64
        mutations.append(wrong_verifier)
        wrong_artifact = copy.deepcopy(report)
        wrong_artifact["artifacts"][0]["verified"] = False
        mutations.append(wrong_artifact)
        for document in mutations:
            with self.subTest(document=document):
                with self.assertRaises(REPORT["ValidationError"]):
                    REPORT["validate_report_document"](
                        document, self.release, self.schema
                    )

    def test_timestamp_exception_matches_the_only_bundle_without_rfc3161(self):
        observed = {}
        for entry in self.release["python"]["versions"]:
            sigstore = entry["source"]["sigstore"]
            with tempfile.TemporaryDirectory() as directory:
                bundle_path = VERIFY["TUF"]["decode_base64_evidence"](
                    REPOSITORY / sigstore["bundle_evidence"],
                    Path(directory) / "bundle.json",
                    "CPython bundle",
                )
                observed["Python-%s.tar.xz" % entry["version"]] = VERIFY[
                    "signed_timestamp_count"
                ](VERIFY["load_bundle"](bundle_path))
        self.assertEqual(
            {name for name, count in observed.items() if count == 0},
            {"Python-3.9.25.tar.xz"},
        )
        self.assertTrue(all(count >= 1 for count in observed.values() if count))

    def test_docker_ci_and_candidate_bind_the_offline_qualification(self):
        dockerfile = (REPOSITORY / "docker/Dockerfile").read_text(
            encoding="utf-8"
        )
        packaging = (REPOSITORY / "docker/packaging.Dockerfile").read_text(
            encoding="utf-8"
        )
        bake = (REPOSITORY / "docker-bake.hcl").read_text(encoding="utf-8")
        ci = (
            (REPOSITORY / ".github/workflows/verify-quick.yml").read_text(encoding="utf-8")
            + (REPOSITORY / "scripts/ci-build.py").read_text(encoding="utf-8")
        )
        candidate = (REPOSITORY / ".github/workflows/candidate.yml").read_text(
            encoding="utf-8"
        )
        qualified = dockerfile.split(" AS sigstore-assets-qualified", 1)[1]
        qualified = qualified.split("\nFROM ", 1)[0]
        self.assertIn("RUN --network=none", qualified)
        self.assertIn("verify-sigstore-assets.py", qualified)
        self.assertIn('target "sigstore-sources-qualified"', bake)
        self.assertIn('target "cosign-host-tool"', bake)
        self.assertIn("sigstore-sources-qualified", ci)
        self.assertIn("sigstore-sources-qualified", candidate)
        self.assertIn(
            "needs: [publish, native-aarch64]", candidate
        )
        self.assertIn("id-token: write", candidate)
        self.assertIn('"$cosign" sign --yes "$image"', candidate)
        self.assertIn("--trusted-root", candidate)
        self.assertIn("candidate-signature-${{ github.run_id }}", candidate)
        self.assertLess(
            candidate.index("  native-aarch64:"),
            candidate.index("  sign-candidate:"),
        )
        self.assertIn("crossforge_sigstore_qualified", packaging)
        self.assertIn(
            "/opt/crossforge/qualification/sigstore.json", packaging
        )
        self.assertNotIn("/sigstore-downloads", packaging)

    def test_scripts_remain_python36_compatible(self):
        for path in (FETCH_SCRIPT, VERIFY_SCRIPT, REPORT_SCRIPT):
            with self.subTest(path=path):
                ast.parse(
                    path.read_text(encoding="utf-8"),
                    filename=str(path),
                    feature_version=(3, 6),
                )


if __name__ == "__main__":
    unittest.main()
