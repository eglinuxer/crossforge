import ast
import copy
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/validate-qt-qualification.py"
VALIDATOR = runpy.run_path(str(SCRIPT))


class QtQualificationPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = VALIDATOR["validate_release_contract"](
            REPOSITORY / "config/release.json"
        )

    def test_plan_is_honest_future_policy_with_isolated_source_identity(self):
        plan = self.contract["plan"]
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["modules"], VALIDATOR["MODULES"])
        self.assertEqual(plan["host"], VALIDATOR["HOST"])
        self.assertEqual(
            [
                {key: record[key] for key in ("name", "component", "usage")}
                for record in plan["source_dependencies"]
            ],
            VALIDATOR["SOURCE_DEPENDENCIES"],
        )
        self.assertEqual(plan["targets"], VALIDATOR["TARGETS"])
        self.assertEqual(plan["required_features"], VALIDATOR["FEATURES"])
        self.assertEqual(
            self.contract["source_component_sha256"],
            "540c01aacded48198436157ab351090990ed06772f82f203563c32422b37a502",
        )
        self.assertEqual(
            self.contract["qualification_component"]["component"],
            "future/qt-qualification",
        )
        self.assertEqual(
            self.contract["qualification_component"]["scope"], "future"
        )
        self.assertEqual(
            [record["component"] for record in self.contract["source_dependencies"]],
            ["sources/xcb-util-cursor"],
        )

    def test_locked_qualification_cannot_be_claimed_with_pending_inputs(self):
        with self.assertRaisesRegex(
            VALIDATOR["ValidationError"], "still planned"
        ):
            VALIDATOR["validate_release_contract"](
                REPOSITORY / "config/release.json", require_locked=True
            )

    def test_semantic_mutations_fail_closed(self):
        plan = self.contract["plan"]
        mutations = []
        modules = copy.deepcopy(plan)
        modules["modules"].reverse()
        mutations.append(modules)
        feature = copy.deepcopy(plan)
        feature["required_features"]["qtwebengine"].pop()
        mutations.append(feature)
        target = copy.deepcopy(plan)
        target["targets"][1]["runtime_tiers"].pop()
        mutations.append(target)
        false_lock = copy.deepcopy(plan)
        false_lock["locks"][0]["status"] = "locked"
        mutations.append(false_lock)
        for candidate in mutations:
            with self.subTest(candidate=candidate):
                with self.assertRaises(VALIDATOR["ValidationError"]):
                    VALIDATOR["validate_plan"](candidate)

    def test_schema_rejects_unknown_fields(self):
        plan = copy.deepcopy(self.contract["plan"])
        plan["threshold"] = 1
        with self.assertRaises(VALIDATOR["ValidationError"]):
            schema = VALIDATOR["STRICT"]["load_json"](
                VALIDATOR["PLAN_SCHEMA"]
            )
            VALIDATOR["STRICT"]["validate"](plan, schema, schema, "$")

    def test_ci_validates_the_plan_without_claiming_it_is_locked(self):
        workflow = (REPOSITORY / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("./scripts/validate-qt-qualification.py", workflow)
        self.assertNotIn(
            "validate-qt-qualification.py --require-locked", workflow
        )

    def test_validator_remains_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
