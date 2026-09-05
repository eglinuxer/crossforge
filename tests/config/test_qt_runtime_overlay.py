import ast
import copy
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/assemble-qt-runtime.py"
OVERLAY = runpy.run_path(str(SCRIPT))


class QtRuntimeOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release_path = REPOSITORY / "config/release.json"
        cls.release = OVERLAY["load_json"](cls.release_path)
        cls.context = OVERLAY["MATERIALIZER"]["load_lock"](
            REPOSITORY / "locks/qt-runtime-el8-x86_64.json",
            release_path=cls.release_path,
        )

    def test_runtime_overlay_script_remains_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )

    def test_base_package_substitution_is_explicit_and_minimal(self):
        self.assertEqual(
            OVERLAY["BASE_PACKAGE_SUBSTITUTIONS"],
            {"coreutils": ("coreutils-single",)},
        )

    def test_partition_retains_base_packages_and_selects_missing_packages(self):
        before = [
            (
                "coreutils-single",
                "x86_64",
                "coreutils-single-0:8.30-base.x86_64",
            ),
            (
                "glibc",
                "x86_64",
                "glibc-0:2.28-base.x86_64",
            ),
            (
                "gpg-pubkey",
                "(none)",
                "gpg-pubkey-0:first.(none)",
            ),
            (
                "gpg-pubkey",
                "(none)",
                "gpg-pubkey-0:second.(none)",
            ),
        ]
        retained, selected = OVERLAY["partition_packages"](
            self.context["packages"], before
        )
        self.assertEqual(
            [item["name"] for item in retained], ["coreutils", "glibc"]
        )
        self.assertEqual(retained[0]["base_name"], "coreutils-single")
        self.assertNotIn(
            "glibc", {package["item"]["name"] for package in selected}
        )
        self.assertIn(
            "libatomic", {package["item"]["name"] for package in selected}
        )

    def test_evidence_binds_release_lock_and_base_image(self):
        retained = [
            {
                "name": "glibc",
                "base_name": "glibc",
                "base_nevra": "glibc-0:2.28-base.x86_64",
                "locked_nevra": "glibc-0:2.28-locked.x86_64",
            }
        ]
        selected = [
            package
            for package in self.context["packages"]
            if package["item"]["name"] == "libatomic"
        ]
        before = [
            ("glibc", "x86_64", "glibc-0:2.28-base.x86_64")
        ]
        after = before + [
            (
                selected[0]["item"]["name"],
                selected[0]["item"]["arch"],
                selected[0]["item"]["nevra"],
            )
        ]
        after.sort(key=lambda row: row[2])
        evidence = OVERLAY["build_evidence"](
            self.context,
            self.release,
            self.release["base_image"]["manifests"]["amd64"],
            retained,
            selected,
            before,
            after,
            "1" * 64,
            "2" * 64,
            "3" * 64,
        )
        OVERLAY["validate_evidence"](evidence)
        self.assertEqual(
            evidence["identity"]["runtime_lock"]["lock_sha256"],
            OVERLAY["canonical_sha256"](self.context["lock"]),
        )
        forged = copy.deepcopy(evidence)
        forged["unexpected"] = True
        with self.assertRaises(OVERLAY["ValidationError"]):
            OVERLAY["validate_evidence"](forged)

    def test_overlay_binds_separate_runtime_and_build_components(self):
        runtime_component = OVERLAY["COMPONENT"]["load_json"](
            REPOSITORY
            / "config/generated/components/future/qt-runtime-qualification.json"
        )
        digest = OVERLAY["COMPONENT"]["canonical_sha256"](
            runtime_component
        )
        self.assertEqual(
            OVERLAY["validate_qualification_binding"](
                REPOSITORY / "config/qt-runtime-qualification.json",
                REPOSITORY
                / "config/generated/components/future/qt-runtime-qualification.json",
                digest,
                REPOSITORY
                / "config/generated/components/future/qt-qualification.json",
                self.context,
            ),
            self.release["qt"]["runtime_qualification"]["plan"][
                "canonical_sha256"
            ],
        )

    def test_installation_is_offline_non_deployable_overlay(self):
        arguments = OVERLAY["installation_arguments"](
            Path("/runtime-root"),
            "foreign-test-arch",
            [Path("runtime.rpm")],
            True,
        )
        for flag in ("--nodeps", "--noscripts", "--notriggers", "--ignorearch", "--test"):
            self.assertIn(flag, arguments)


if __name__ == "__main__":
    unittest.main()
