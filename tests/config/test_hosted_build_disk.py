from pathlib import Path
import runpy
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
DISK = runpy.run_path(str(ROOT / "scripts/prepare-hosted-build-disk.py"))


class HostedBuildDiskTests(unittest.TestCase):
    def test_refuses_local_and_self_hosted_cleanup(self):
        for environment in ({}, {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted"}):
            with mock.patch("subprocess.run") as run:
                with self.assertRaises(ValueError):
                    DISK["prepare"](environment)
                run.assert_not_called()

    def test_removes_only_unused_sdks_and_propagates_failure(self):
        environment = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"}
        with mock.patch("pathlib.Path.exists", return_value=True), mock.patch("subprocess.run") as run:
            DISK["prepare"](environment)
            self.assertEqual(run.call_args_list, [
                mock.call(["sudo", "rm", "-rf", "--", path], check=True)
                for path in ("/usr/local/lib/android", "/usr/share/dotnet", "/usr/local/.ghcup", "/opt/ghc")
            ])
            run.side_effect = OSError("cleanup failed")
            with self.assertRaises(OSError):
                DISK["prepare"](environment)

    def test_missing_sdks_are_harmless(self):
        with mock.patch("pathlib.Path.exists", return_value=False), mock.patch("subprocess.run") as run:
            DISK["prepare"]({"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"})
            run.assert_not_called()
