import copy
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
CONTRACT = runpy.run_path(
    str(REPOSITORY / "scripts/validate-gcc-testsuite.py")
)
RUNNER = runpy.run_path(str(REPOSITORY / "scripts/run-gcc-testsuite.py"))


class GccTestsuiteContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = CONTRACT["validate_release_contract"](
            REPOSITORY / "config/release.json"
        )
        cls.plan = cls.contract["plan"]
        cls.full_plan = CONTRACT["validate_plan"](
            CONTRACT["load_json"](
                REPOSITORY / "config/gcc-testsuite-full.json"
            )
        )

    def write_summary(self, directory, text):
        path = Path(directory) / "gcc.sum"
        path.write_text(text, encoding="utf-8")
        return path

    def test_release_binds_exact_plan_and_three_runtime_baselines(self):
        self.assertEqual(self.plan["profile"], "smoke")
        self.assertEqual(
            self.plan["targets"], CONTRACT["TARGET_CONTRACT"]
        )

    def test_full_plan_has_exactly_the_four_real_upstream_testsuites(self):
        self.assertEqual(self.full_plan["profile"], "full")
        self.assertEqual(self.full_plan["host_gcc_major"], "8")
        self.assertEqual(self.full_plan["jobs"], 4)
        self.assertEqual(
            self.full_plan["suites"], CONTRACT["FULL_SUITE_CONTRACT"]
        )
        self.assertEqual(
            [
                suite["make_target"]
                for suite in self.full_plan["suites"]
                if "make_target" in suite
            ],
            [
                "check-g++",
                "check-gcc",
                "check-target-libgomp",
            ],
        )
        self.assertEqual(
            [
                suite.get("make_directory")
                for suite in self.full_plan["suites"]
                if suite["id"] in ("g++.full", "gcc.full")
            ],
            ["gcc", "gcc"],
        )
        self.assertEqual(
            self.full_plan["suites"][-1],
            {
                "id": "libstdc++.full",
                "driver": "runtest-installed",
                "tool": "libstdc++",
                "timeout_seconds": 7200,
                "sum_file": "libstdc++.sum",
                "runtestflags": [],
            },
        )

    def test_target_summary_templates_are_expanded_and_fail_closed(self):
        path = CONTRACT["resolve_summary_path"](
            "{target}/libgomp/testsuite/libgomp.sum",
            "aarch64-unknown-linux-gnu",
        )
        self.assertEqual(
            str(path),
            "aarch64-unknown-linux-gnu/libgomp/testsuite/libgomp.sum",
        )
        for value in ("../gcc.sum", "{unknown}/gcc.sum", "{target}/{target}/gcc.sum"):
            with self.assertRaises(CONTRACT["ValidationError"]):
                CONTRACT["resolve_summary_path"](
                    value, "x86_64-unknown-linux-gnu"
                )

    def test_observation_emits_a_non_qualifying_candidate_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            summaries = {}
            for index, suite in enumerate(self.full_plan["suites"]):
                path = Path(directory) / (suite["id"] + ".sum")
                status = "FAIL" if index == 0 else "PASS"
                path.write_text(
                    "%s: exact %s result\n" % (status, suite["id"]),
                    encoding="utf-8",
                )
                summaries[suite["id"]] = path
            report, candidate = CONTRACT["observe_summaries"](
                self.full_plan,
                "x86_64-unknown-linux-gnu",
                "host-direct",
                summaries,
                {"test": "material"},
            )
        self.assertEqual(report["kind"], "gcc-testsuite-observation")
        self.assertEqual(report["status"], "observed")
        self.assertNotIn("baseline_sha256", report)
        self.assertEqual(candidate["kind"], "gcc-testsuite-baseline")
        self.assertEqual(candidate["unexpected"], report["unexpected"])
        self.assertEqual(
            report["candidate_baseline_sha256"],
            CONTRACT["canonical_sha256"](candidate),
        )

    def test_full_plan_rejects_partial_or_reordered_suite_sets(self):
        partial = copy.deepcopy(self.full_plan)
        partial["suites"].pop()
        reordered = copy.deepcopy(self.full_plan)
        reordered["suites"].reverse()
        for candidate in (partial, reordered):
            with self.assertRaisesRegex(
                CONTRACT["ValidationError"], "full suite contract differs"
            ):
                CONTRACT["validate_plan"](candidate)
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
        wrong_patch = copy.deepcopy(self.full_plan)
        wrong_patch["source_patches"][0]["sha256"] = "0" * 64
        for candidate in (wrong_site, wrong_board, wrong_patch):
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
        self.assertIn('choices=("qualification", "observation")', runner)
        self.assertIn(
            '"observation mode must not claim a qualification component"',
            runner,
        )
        self.assertIn("print_log_tail(make_log)", runner)
        self.assertGreaterEqual(runner.count("print_log_diagnostics(source_log)"), 2)
        self.assertIn('"--fuzz=0"', runner)
        self.assertIn('target_record["before_sha256"]', runner)
        self.assertIn('target_record["after_sha256"]', runner)
        self.assertIn('driver == "runtest-installed"', runner)
        self.assertIn('"--tool"', runner)
        self.assertIn("prepare_runtime_links", runner)
        self.assertIn("GCC_RUNTEST_PARALLELIZE_DIR", runner)
        self.assertIn("GCC testsuite progress", runner)
        self.assertIn("made no observable progress for 600 seconds", runner)
        self.assertIn("dg-extract-results.sh", runner)
        self.assertIn('driver != "runtest-installed"', runner)
        self.assertIn("installed libstdc++ tests used a build-tree library", runner)
        self.assertIn('"g++": gxx', runner)
        self.assertIn('"gcc-ar": gcc_ar', runner)
        self.assertIn('"gcov": gcov', runner)
        self.assertIn('"GCC_AR_UNDER_TEST": str(gcc_ar)', runner)
        self.assertIn('"GCOV_UNDER_TEST": str(gcov)', runner)
        self.assertIn('str(tool_prefix) + ":" + suite_environment["PATH"]', runner)
        self.assertIn('Path(resolved_gxx).resolve() != gxx.resolve()', runner)

    def test_ci_isolates_qemu_smoke_from_peak_build_fanout(self):
        workflow = (REPOSITORY / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("sdk-complete-dev gcc-testsuite-smoke", workflow)
        self.assertEqual(
            workflow.count("docker buildx bake gcc-testsuite-smoke"), 1
        )
        complete = workflow.index(
            "docker buildx bake python-matrix vcpkg-upstream-tier3-qualified"
        )
        smoke = workflow.index("docker buildx bake gcc-testsuite-smoke")
        self.assertLess(complete, smoke)

    def test_progress_watchdog_stops_idle_workers_without_masking_the_error(self):
        class IdleProcess:
            def __init__(self):
                self.running = True

            def poll(self):
                return None if self.running else -15

            def terminate(self):
                self.running = False

        process = IdleProcess()
        with mock.patch.object(
            RUNNER["time"], "monotonic", side_effect=(0, 601)
        ), mock.patch.object(RUNNER["time"], "sleep"), mock.patch(
            "builtins.print"
        ):
            with self.assertRaisesRegex(
                RUNNER["ValidationError"], "no observable progress"
            ):
                RUNNER["wait_with_progress"](
                    "libstdc++.full", [process], [], 7200
                )
        self.assertFalse(process.running)

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

    def test_full_site_bridges_final_compilers_into_runtime_suites(self):
        site = (REPOSITORY / "tests/gcc/full-site.exp").read_text(
            encoding="utf-8"
        )
        self.assertIn("GCC_UNDER_TEST GXX_UNDER_TEST", site)
        self.assertIn("GCC_AR_UNDER_TEST", site)
        self.assertIn("GCOV_UNDER_TEST", site)
        self.assertIn("CROSSFORGE_GCC_TOOL_PREFIX", site)
        self.assertIn("set GCC_UNDER_TEST", site)
        self.assertIn("set GXX_UNDER_TEST", site)
        self.assertEqual(
            CONTRACT["file_sha256"](REPOSITORY / "tests/gcc/full-site.exp"),
            self.full_plan["site"]["sha256"],
        )
        self.assertEqual(len(self.full_plan["source_patches"]), 5)
        for patch in self.full_plan["source_patches"]:
            self.assertEqual(
                CONTRACT["file_sha256"](REPOSITORY / patch["file"]),
                patch["sha256"],
            )
            self.assertGreaterEqual(len(patch["targets"]), 1)


if __name__ == "__main__":
    unittest.main()
