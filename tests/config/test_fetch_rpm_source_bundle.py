import ast
import hashlib
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/fetch-rpm-source-bundle.py"
BUNDLE = runpy.run_path(str(SCRIPT))


class FetchRPMSourceBundleTests(unittest.TestCase):
    def records(self):
        payloads = {
            "one.src.rpm": b"source-one",
            "two.src.rpm": b"source-two",
        }
        records = [
            {
                "source_rpm": name,
                "url": "https://example.invalid/" + name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
            for name, payload in sorted(payloads.items())
        ]
        return payloads, {"sources": records}

    def test_checked_release_lock_is_the_fetch_authority(self):
        arguments = SimpleNamespace(
            release=REPOSITORY / "config/release.json",
            release_schema=REPOSITORY / "config/schemas/release.schema.json",
            requirements=REPOSITORY
            / "evidence/sources/rpm-source-requirements.json",
            requirements_schema=REPOSITORY
            / "config/schemas/rpm-source-requirements.schema.json",
            lock=REPOSITORY / "locks/rpm-source-el8.json",
            lock_schema=REPOSITORY / "config/schemas/rpm-source-lock.schema.json",
        )
        release, lock = BUNDLE["load_context"](arguments)
        self.assertEqual(len(lock["sources"]), 333)
        self.assertEqual(
            sum(record["size"] for record in lock["sources"]), 1456725209
        )
        self.assertEqual(
            release["source_bundle"]["rpm"]["lock"]["canonical_sha256"],
            BUNDLE["RESOLVE"]["canonical_sha256"](lock),
        )

    def test_fetch_is_atomic_and_uses_only_locked_primary_urls(self):
        payloads, lock = self.records()
        original = BUNDLE["RESOLVE"]["download"]
        calls = []

        def download(url, destination):
            calls.append(url)
            destination.write_bytes(payloads[destination.name])

        BUNDLE["RESOLVE"]["download"] = download
        try:
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "source-rpms"
                summary = BUNDLE["fetch_bundle"](lock, output, 2)
                self.assertEqual(summary, {"sources": 2, "bytes": 20})
                self.assertEqual(
                    sorted(calls), sorted(record["url"] for record in lock["sources"])
                )
                self.assertEqual(
                    sorted(path.name for path in output.iterdir()),
                    sorted(payloads),
                )
        finally:
            BUNDLE["RESOLVE"]["download"] = original

    def test_failed_fetch_removes_the_partial_bundle(self):
        payloads, lock = self.records()
        original = BUNDLE["RESOLVE"]["download"]

        def download(_url, destination):
            destination.write_bytes(payloads[destination.name] + b"tampered")

        BUNDLE["RESOLVE"]["download"] = download
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                output = root / "source-rpms"
                with self.assertRaises(BUNDLE["ValidationError"]):
                    BUNDLE["fetch_bundle"](lock, output, 2)
                self.assertFalse(output.exists())
                self.assertEqual(list(root.glob(".source-rpms.*")), [])
        finally:
            BUNDLE["RESOLVE"]["download"] = original

    def test_offline_verification_rechecks_hash_header_and_signature(self):
        payloads, lock = self.records()
        release = {
            "trust": {
                "rocky_rpm_key": {"fingerprint": "a" * 40}
            }
        }
        original = BUNDLE["RESOLVE"]["verify_source_rpm"]
        calls = []
        BUNDLE["RESOLVE"]["verify_source_rpm"] = (
            lambda path, name, rpmkeys, rpm, fingerprint: calls.append(
                (path.name, name, str(rpmkeys), str(rpm), fingerprint)
            )
        )
        try:
            with tempfile.TemporaryDirectory() as temporary:
                bundle = Path(temporary)
                for name, payload in payloads.items():
                    (bundle / name).write_bytes(payload)
                summary = BUNDLE["verify_bundle"](
                    release, lock, bundle, Path("rpmkeys"), Path("rpm")
                )
                self.assertEqual(summary, {"sources": 2, "bytes": 20})
                self.assertEqual(len(calls), 2)
                self.assertTrue(all(call[-1] == "a" * 40 for call in calls))
        finally:
            BUNDLE["RESOLVE"]["verify_source_rpm"] = original

    def test_docker_fetch_and_offline_verification_are_separate(self):
        dockerfile = (REPOSITORY / "docker/Dockerfile").read_text(
            encoding="utf-8"
        )
        fetch = dockerfile.split(" AS rpm-source-bundle-fetch", 1)[1]
        fetch = fetch.split("\nFROM ", 1)[0]
        verify = dockerfile.split(" AS rpm-source-bundle-verified", 1)[1]
        verify = verify.split("\nFROM ", 1)[0]
        self.assertIn("fetch-rpm-source-bundle.py fetch", fetch)
        self.assertNotIn("repoquery", fetch)
        self.assertNotIn("--network=none", fetch)
        self.assertIn("RUN --network=none", verify)
        self.assertIn("rpm --import", verify)
        self.assertIn("fetch-rpm-source-bundle.py verify", verify)
        self.assertIn(
            'target "rpm-source-bundle"',
            (REPOSITORY / "docker-bake.hcl").read_text(encoding="utf-8"),
        )
        for workflow in (
            REPOSITORY / ".github/workflows/ci.yml",
            REPOSITORY / ".github/workflows/verify-quick.yml",
            REPOSITORY / ".github/workflows/candidate.yml",
        ):
            self.assertNotIn(
                "rpm-source-bundle", workflow.read_text(encoding="utf-8")
            )

    def test_fetcher_is_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
