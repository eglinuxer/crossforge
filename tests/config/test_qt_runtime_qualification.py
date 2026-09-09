import copy
import json
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
VALIDATOR = runpy.run_path(
    str(REPOSITORY / "scripts/validate-qt-runtime-qualification.py")
)


class QtRuntimeQualificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = VALIDATOR["validate_release_contract"](
            REPOSITORY / "config/release.json"
        )

    def test_runtime_plan_is_separate_and_release_bound(self):
        self.assertEqual(
            self.contract["plan"]["build_qualification"],
            {
                "component": "future/qt-qualification",
                "plan_file": "config/qt-qualification.json",
                "plan_sha256": "031e8220250258286948f3cd66c0c3ddee26d1b9643503cdba0e506751c72033",
            },
        )
        self.assertEqual(
            self.contract["runtime_pair"],
            {
                "x86_64_packages": 181,
                "aarch64_packages": 179,
                "x86_64_only": ["hwdata", "libpciaccess"],
            },
        )
        self.assertEqual(
            self.contract["qualification_component"]["component"],
            "future/qt-runtime-qualification",
        )
        self.assertEqual(
            [
                item["component"]
                for item in self.contract["qualification_component"][
                    "dependencies"
                ]
            ],
            ["future/qt-qualification"],
        )

    def test_runtime_pair_rejects_unreviewed_architecture_drift(self):
        runtimes = copy.deepcopy(self.contract["locked_transactions"])
        aarch64 = next(
            transaction
            for transaction in runtimes
            if transaction["identity"]["arch"] == "aarch64"
        )
        aarch64["items"].pop()
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_runtime_pair"](runtimes)

    def test_release_binding_must_match_the_selected_release(self):
        release = VALIDATOR["STRICT"]["load_json"](
            REPOSITORY / "config/release.json"
        )
        release["product"]["version"] = "0.1.1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.json"
            path.write_text(
                json.dumps(release, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                VALIDATOR["ValidationError"], "release binding differs"
            ):
                VALIDATOR["validate_release_contract"](path)

    def test_runtime_tiers_keep_qemu_and_native_release_distinct(self):
        self.assertEqual(
            self.contract["plan"]["targets"], VALIDATOR["TARGETS"]
        )
        self.assertEqual(
            self.contract["plan"]["targets"][0]["runtime_tiers"],
            [{"tier": "clean-rocky", "executor": "native"}],
        )
        self.assertEqual(
            self.contract["plan"]["targets"][1]["runtime_tiers"],
            [
                {"tier": "clean-rocky-qemu", "executor": "explicit-qemu"},
                {"tier": "native-release", "executor": "native"},
            ],
        )

    def test_ci_and_candidate_validate_the_runtime_contract(self):
        ci = (REPOSITORY / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
        candidate = (REPOSITORY / ".github/workflows/candidate.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("./scripts/validate-qt-runtime-qualification.py", ci)
        stages = runpy.run_path(str(REPOSITORY / "scripts/ci-build.py"))["STAGES"]
        self.assertEqual(stages["qt-x86_64"], ["qt-x86_64-runtime-qualified"])
        self.assertEqual(stages["qt-aarch64"], ["qt-aarch64-runtime-qualified"])
        builds = (REPOSITORY / ".github/workflows/verify-builds.yml").read_text()
        self.assertIn("stage: [qt-x86_64, qt-aarch64]", builds)
        self.assertIn("needs: [plan, toolchains, qt-host]", builds)
        self.assertIn("timeout-minutes: 360", builds)
        self.assertIn(
            "./scripts/validate-qt-runtime-qualification.py", candidate
        )
        self.assertNotIn("qt-aarch64-native-runtime-root", candidate)
        self.assertIn("native-aarch64-release.py execute", candidate)

    def test_user_documentation_reports_current_qt_qualification_state(self):
        readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")
        architecture = (REPOSITORY / "docs/architecture.md").read_text(
            encoding="utf-8"
        )
        normalized = " ".join(
            (readme + architecture).replace("> ", "").split()
        )
        for stale in (
            "Qt acceptance and the remaining release supply chain are pending",
            "Locked dependencies do not yet mean Qt itself is qualified",
            "Qt 双 target 运行时资格及其余发布供应链尚未完成",
            "只服务未来 Qt qualification",
        ):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, normalized)
        self.assertIn(
            "docker buildx bake qt-host-qualified qt-target-build-qualified",
            readme,
        )
        self.assertIn(
            "docker buildx bake qt-target-runtime-qualified", readme
        )
        build_counts = {
            name: len(
                json.loads(
                    (REPOSITORY / "locks" / name).read_text(
                        encoding="utf-8"
                    )
                )["packages"]
            )
            for name in (
                "host-qt-build-el8-x86_64.json",
                "qt-target-el8-x86_64.json",
                "qt-target-el8-aarch64.json",
            )
        }
        self.assertIn(
            "The host lock contains %d RPM payloads"
            % build_counts["host-qt-build-el8-x86_64.json"],
            normalized,
        )
        self.assertIn(
            "target overlays contain %d and %d respectively"
            % (
                build_counts["qt-target-el8-x86_64.json"],
                build_counts["qt-target-el8-aarch64.json"],
            ),
            normalized,
        )
        self.assertIn(
            "%d-package x86_64 and %d-package AArch64 closures"
            % (
                self.contract["runtime_pair"]["x86_64_packages"],
                self.contract["runtime_pair"]["aarch64_packages"],
            ),
            normalized,
        )
        self.assertIn("A published SDK does not imply Qt qualification", normalized)
        self.assertIn("candidate-bound Qt 运行时", normalized)


if __name__ == "__main__":
    unittest.main()
