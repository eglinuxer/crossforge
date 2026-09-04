import copy
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
CONTRACT = runpy.run_path(
    str(REPOSITORY / "scripts/validate-gcc-testsuite.py")
)


class GccTestsuiteContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = CONTRACT["validate_release_contract"](
            REPOSITORY / "config/release.json"
        )
        cls.plan = cls.contract["plan"]

    def write_summary(self, directory, text):
        path = Path(directory) / "gcc.sum"
        path.write_text(text, encoding="utf-8")
        return path

    def test_release_binds_exact_plan_and_three_runtime_baselines(self):
        self.assertEqual(self.plan["profile"], "smoke")
        self.assertEqual(
            self.plan["targets"], CONTRACT["TARGET_CONTRACT"]
        )
        self.assertEqual(
            set(self.contract["baselines"]),
            {
                ("x86_64-unknown-linux-gnu", "host-direct"),
                ("aarch64-unknown-linux-gnu", "locked-sysroot"),
                ("aarch64-unknown-linux-gnu", "clean-rocky"),
            },
        )

    def test_summary_records_are_grouped_by_exact_identity_and_occurrence(self):
        baseline = self.contract["baselines"][(
            "x86_64-unknown-linux-gnu", "host-direct"
        )]["document"]
        with tempfile.TemporaryDirectory() as directory:
            summary = self.write_summary(
                directory,
                "PASS: gcc.c-torture/execute/example.c execution, -O0\n"
                "PASS: gcc.c-torture/execute/example.c execution, -O0\n"
                "XFAIL: gcc.dg/example.c scan-assembler marker\n",
            )
            report = CONTRACT["normalize_summaries"](
                self.plan,
                baseline,
                {"gcc.execute": summary},
                {"test": "material"},
            )
        self.assertEqual(report["status_counts"], {"PASS": 2, "XFAIL": 1})
        self.assertEqual(report["results"][0]["count"], 2)
        self.assertEqual(report["unexpected"], [])

    def test_added_and_resolved_unexpected_results_both_fail(self):
        baseline = self.contract["baselines"][(
            "x86_64-unknown-linux-gnu", "host-direct"
        )]["document"]
        with tempfile.TemporaryDirectory() as directory:
            failure = self.write_summary(directory, "FAIL: exact test identity\n")
            with self.assertRaisesRegex(
                CONTRACT["ValidationError"], "added=.*exact test identity"
            ):
                CONTRACT["normalize_summaries"](
                    self.plan,
                    baseline,
                    {"gcc.execute": failure},
                    {},
                )
            expected_failure = copy.deepcopy(baseline)
            expected_failure["unexpected"] = [
                {
                    "suite": "gcc.execute",
                    "status": "FAIL",
                    "test": "exact test identity",
                    "count": 1,
                }
            ]
            passed = self.write_summary(directory, "PASS: exact test identity\n")
            with self.assertRaisesRegex(
                CONTRACT["ValidationError"], "resolved=.*exact test identity"
            ):
                CONTRACT["normalize_summaries"](
                    self.plan,
                    expected_failure,
                    {"gcc.execute": passed},
                    {},
                )

    def test_unknown_status_empty_summary_and_wrong_plan_digest_fail_closed(self):
        baseline = self.contract["baselines"][(
            "x86_64-unknown-linux-gnu", "host-direct"
        )]["document"]
        with tempfile.TemporaryDirectory() as directory:
            unknown = self.write_summary(directory, "MYSTERY: test\n")
            with self.assertRaisesRegex(
                CONTRACT["ValidationError"], "unknown DejaGNU status"
            ):
                CONTRACT["parse_summary"](
                    "gcc.execute", unknown, set(self.plan["unexpected_statuses"])
                )
            empty = self.write_summary(directory, "# no result records\n")
            with self.assertRaisesRegex(
                CONTRACT["ValidationError"], "contains no result"
            ):
                CONTRACT["parse_summary"](
                    "gcc.execute", empty, set(self.plan["unexpected_statuses"])
                )
        wrong = copy.deepcopy(baseline)
        wrong["plan_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            CONTRACT["ValidationError"], "plan digest differs"
        ):
            CONTRACT["validate_baseline"](
                wrong,
                self.plan,
                baseline["target"],
                baseline["runtime_tier"],
            )

    def test_plan_rejects_site_or_board_byte_identity_tampering(self):
        wrong_site = copy.deepcopy(self.plan)
        wrong_site["site"]["sha256"] = "0" * 64
        wrong_board = copy.deepcopy(self.plan)
        wrong_board["targets"][1]["runtime_tiers"][0]["board"][
            "sha256"
        ] = "0" * 64
        for candidate in (wrong_site, wrong_board):
            with self.assertRaises(CONTRACT["ValidationError"]):
                CONTRACT["validate_plan"](candidate)

    def test_runner_forces_final_compilers_and_never_mentions_xgcc(self):
        runner = (
            REPOSITORY / "scripts/run-gcc-testsuite.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"GCC_UNDER_TEST": str(compiler)', runner)
        self.assertIn('"GXX_UNDER_TEST": str(gxx)', runner)
        self.assertIn('set GCC_UNDER_TEST "%s -B%s/"', runner)
        self.assertIn('set GXX_UNDER_TEST "%s -B%s/"', runner)
        self.assertIn("catch {unset TEST_GCC_EXEC_PREFIX}", runner)
        self.assertIn('if "/gcc/xgcc" in test_log', runner)

    def test_custom_qemu_boards_preserve_dynamic_el8_execution(self):
        boards = REPOSITORY / "tests/gcc/boards"
        native = (boards / "crossforge-x86_64.exp").read_text(
            encoding="utf-8"
        )
        self.assertIn("unset_board_info isremote", native)
        self.assertIn("set_board_info isremote 0", native)
        self.assertNotIn("exec_shell", native)
        locked = (boards / "crossforge-aarch64-locked.exp").read_text(
            encoding="utf-8"
        )
        clean = (boards / "crossforge-aarch64-clean.exp").read_text(
            encoding="utf-8"
        )
        for text, root in (
            (locked, "/opt/crossforge/sysroots/el8/aarch64"),
            (clean, "/runtime-root"),
        ):
            self.assertIn("unset_board_info isremote", text)
            self.assertIn("set_board_info isremote 0", text)
            self.assertIn('setenv QEMU_LD_PREFIX "%s"' % root, text)
            self.assertIn("qemu-aarch64 -L %s" % root, text)
            self.assertIn("-cpu cortex-a53 -r 4.18.0", text)
            self.assertNotIn("-static", text)


if __name__ == "__main__":
    unittest.main()
