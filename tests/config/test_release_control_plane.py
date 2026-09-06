import ast
import copy
import json
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/validate-release-control-plane.py"
VALIDATOR = runpy.run_path(str(SCRIPT))


class ReleaseControlPlaneTests(unittest.TestCase):
    def fixture(self):
        return {
            "immutable": {"enabled": True, "enforced_by_owner": False},
            "private": {"enabled": True},
            "environment": {
                "name": "production",
                "protection_rules": [
                    {
                        "id": 1,
                        "type": "required_reviewers",
                        "prevent_self_review": False,
                        "reviewers": [
                            {
                                "type": "User",
                                "reviewer": {"login": "eglinuxer", "id": 24752500},
                            }
                        ],
                    },
                    {"id": 2, "type": "branch_policy"},
                ],
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                },
            },
            "branches": {
                "total_count": 1,
                "branch_policies": [
                    {"id": 3, "name": "main", "type": "branch"}
                ],
            },
            "actions": {
                "default_workflow_permissions": "read",
                "can_approve_pull_request_reviews": False,
            },
        }

    def validate(self, fixture):
        return VALIDATOR["validate_control_plane"](
            fixture["immutable"],
            fixture["private"],
            fixture["environment"],
            fixture["branches"],
            fixture["actions"],
            "eglinuxer/crossforge",
            "eglinuxer",
        )

    def test_exact_single_reviewer_main_only_least_privilege_policy_passes(self):
        report = self.validate(self.fixture())
        self.assertEqual(report["environment"]["deployment_branches"], ["main"])
        self.assertEqual(report["environment"]["required_reviewer"], "eglinuxer")
        self.assertEqual(report["actions"]["default_workflow_permissions"], "read")
        self.assertTrue(all(report["checks"].values()))
        VALIDATOR["validate_schema"](
            report, REPOSITORY / "config/schemas/release-control-plane.schema.json"
        )

    def test_every_missing_or_weakened_control_fails_closed(self):
        mutations = (
            lambda value: value["immutable"].__setitem__("enabled", False),
            lambda value: value["private"].__setitem__("enabled", False),
            lambda value: value["environment"].__setitem__("name", "staging"),
            lambda value: value["environment"]["deployment_branch_policy"].update(
                {"protected_branches": True, "custom_branch_policies": False}
            ),
            lambda value: value["environment"]["protection_rules"][0].__setitem__(
                "prevent_self_review", True
            ),
            lambda value: value["environment"]["protection_rules"][0][
                "reviewers"
            ][0]["reviewer"].__setitem__("login", "someone-else"),
            lambda value: value["environment"]["protection_rules"].pop(),
            lambda value: value["branches"]["branch_policies"][0].__setitem__(
                "name", "release/*"
            ),
            lambda value: value["branches"].__setitem__("total_count", 2),
            lambda value: value["actions"].__setitem__(
                "default_workflow_permissions", "write"
            ),
            lambda value: value["actions"].__setitem__(
                "can_approve_pull_request_reviews", True
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                fixture = self.fixture()
                mutate(fixture)
                with self.assertRaises(VALIDATOR["ControlPlaneError"]):
                    self.validate(fixture)

    def test_report_output_is_atomic_idempotent_and_strict(self):
        report = self.validate(self.fixture())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "control-plane.json"
            self.assertTrue(VALIDATOR["write_json_once"](output, report))
            self.assertFalse(VALIDATOR["write_json_once"](output, report))
            changed = copy.deepcopy(report)
            changed["repository"] = "example/crossforge"
            with self.assertRaises(VALIDATOR["ControlPlaneError"]):
                VALIDATOR["write_json_once"](output, changed)
            observed = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(observed, report)

    def test_composite_is_shared_by_promotion_and_rollback(self):
        action = (
            REPOSITORY
            / ".github/actions/validate-release-control-plane/action.yml"
        ).read_text(encoding="utf-8")
        for endpoint in (
            "immutable-releases",
            "private-vulnerability-reporting",
            "environments/production",
            "environments/production/deployment-branch-policies",
            "actions/permissions/workflow",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint, action)
        for workflow in ("promote.yml", "rollback.yml"):
            content = (REPOSITORY / ".github/workflows" / workflow).read_text(
                encoding="utf-8"
            )
            self.assertEqual(
                content.count(
                    "uses: ./.github/actions/validate-release-control-plane"
                ),
                1,
            )

    def test_validator_is_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
