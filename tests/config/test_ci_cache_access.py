import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
CACHE = runpy.run_path(str(ROOT / "scripts/check-ci-cache.py"))
REFERENCE = CACHE["CACHE_PREFIX"] + "validate"


class CacheAccessTests(unittest.TestCase):
    def setUp(self):
        sleep = mock.patch.object(CACHE["time"], "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_only_distinct_exports_in_the_cache_package_are_checked(self):
        value = {"target": {"validate": {"cache-to": [
            {"type": "registry", "ref": REFERENCE, "mode": "max"}]},
            "dependency": {"cache-to": []}}}
        self.assertEqual(CACHE["exported_references"](value), [REFERENCE])
        value["target"]["other"] = value["target"]["validate"]
        with self.assertRaises(ValueError):
            CACHE["exported_references"](value)
        for reference in ("ghcr.io/eglinuxer/crossforge:latest", "--help", "other/cache:tag"):
            value = {"target": {"validate": {"cache-to": [
                {"type": "registry", "ref": reference}]}}}
            with self.assertRaises(ValueError):
                CACHE["exported_references"](value)
        with self.assertRaises(ValueError):
            CACHE["exported_references"]({"target": {}})
        with self.assertRaises(ValueError):
            json.loads('{"target": {}, "target": {}}', object_pairs_hook=CACHE["unique_object"])

    def test_anonymous_inspection_cannot_use_the_writers_credentials(self):
        def inspect(command, **kwargs):
            self.assertEqual(command[:4], ["/pinned/buildx", "imagetools", "inspect", "--raw"])
            environment = kwargs["env"]
            for name in ("GITHUB_TOKEN", "CACHE_TOKEN", "DOCKER_AUTH_CONFIG", "BUILDX_CONFIG"):
                self.assertNotIn(name, environment)
            config = Path(environment["DOCKER_CONFIG"])
            self.assertEqual(json.loads((config / "config.json").read_text()), {"auths": {}})
            self.assertEqual(kwargs["timeout"], 30)
            return subprocess.CompletedProcess(command, 0, b'{"schemaVersion":2,"layers":[]}')
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            "GITHUB_TOKEN": "secret", "CACHE_TOKEN": "secret", "DOCKER_AUTH_CONFIG": "secret",
            "DOCKER_CONFIG": "/writer/config", "BUILDX_CONFIG": "/writer/buildx",
        }), mock.patch.object(CACHE["subprocess"], "run", side_effect=inspect):
            output = Path(directory) / "cache-access.json"
            self.assertEqual(CACHE["check_access"]([REFERENCE], Path("/pinned/buildx"), output), 0)
            report = json.loads(output.read_text())
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["authentication"], "anonymous")
            self.assertTrue(report["caches"][0]["manifest_digest"].startswith("sha256:"))

    def test_private_or_missing_cache_fails_and_retains_an_operational_report(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            CACHE["subprocess"], "run", return_value=subprocess.CompletedProcess([], 1, b"")
        ):
            output = Path(directory) / "cache-access.json"
            self.assertEqual(CACHE["check_access"]([REFERENCE], Path("/buildx"), output), 1)
            report = json.loads(output.read_text())
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["caches"][0]["reference"], REFERENCE)
            self.assertEqual(report["caches"][0]["status"], "unavailable")

    def test_transient_registry_failure_is_retried_with_a_bound(self):
        with mock.patch.object(CACHE["subprocess"], "run", side_effect=[
            subprocess.CompletedProcess([], 1, b""),
            subprocess.CompletedProcess([], 0, b'{"schemaVersion":2}'),
        ]) as run:
            result = CACHE["inspect_reference"](REFERENCE, Path("/buildx"), {})
            self.assertEqual(result["status"], "readable")
            self.assertEqual(result["attempts"], 2)
            self.assertNotIn("return_code", result)
            self.assertEqual(run.call_count, 2)
        with mock.patch.object(CACHE["subprocess"], "run", return_value=
                               subprocess.CompletedProcess([], 1, b"")) as run:
            result = CACHE["inspect_reference"](REFERENCE, Path("/buildx"), {})
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(run.call_count, 3)

    def test_timeout_and_invalid_manifest_cannot_pass(self):
        with mock.patch.object(CACHE["subprocess"], "run", side_effect=subprocess.TimeoutExpired([], 30)):
            result = CACHE["inspect_reference"](REFERENCE, Path("/buildx"), {})
            self.assertEqual(result["status"], "timeout")
        for payload in (b"not-json", b"{}", b"null"):
            with mock.patch.object(CACHE["subprocess"], "run", return_value=subprocess.CompletedProcess([], 0, payload)):
                result = CACHE["inspect_reference"](REFERENCE, Path("/buildx"), {})
                self.assertEqual(result["status"], "inspection-error")


if __name__ == "__main__":
    unittest.main()
