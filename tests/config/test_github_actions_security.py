import re
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
WORKFLOWS = REPOSITORY / ".github/workflows"
ACTIONS = REPOSITORY / ".github/actions"
USES = re.compile(r"^\s*(?:-\s*)?uses:\s+([^#\s]+)", re.MULTILINE)
PINNED_ACTION = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


class GitHubActionsSecurityTests(unittest.TestCase):
    def test_standalone_workflows_declare_permissions_and_reusable_inherits(self):
        workflows = sorted(WORKFLOWS.glob("*.yml"))
        self.assertTrue(workflows)
        for path in workflows:
            with self.subTest(path=path.name):
                content = path.read_text(encoding="utf-8")
                if path.name in ("verify-builds.yml", "verify-incremental.yml"):
                    self.assertIn("workflow_call:", content)
                    for standalone in ("pull_request:", "push:", "schedule:", "workflow_dispatch:"):
                        self.assertNotIn(standalone, content)
                    self.assertNotIn("\npermissions:\n", content)
                    if path.name == "verify-builds.yml":
                        self.assertNotIn("permissions:", content)
                        self.assertIn("Permissions intentionally inherit from the caller", content)
                    else:
                        self.assertIn("Build jobs inherit the caller's permission boundary", content)
                        self.assertEqual(content.count("permissions:\n      contents: read"), 3)
                    continue
                permissions = content.index("\npermissions:\n")
                jobs = content.index("\njobs:\n")
                self.assertLess(permissions, jobs)
                self.assertNotIn("write-all", content[permissions:jobs])

        ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        permissions = ci.split("\npermissions:\n", 1)[1].split(
            "\njobs:\n", 1
        )[0]
        self.assertEqual(permissions, "  contents: read\n")

    def test_every_external_action_is_pinned_to_a_commit(self):
        files = sorted(WORKFLOWS.glob("*.yml"))
        files.extend(sorted(ACTIONS.glob("*/action.yml")))
        self.assertTrue(files)
        for path in files:
            content = path.read_text(encoding="utf-8")
            for reference in USES.findall(content):
                if reference.startswith("./"):
                    continue
                with self.subTest(
                    path=str(path.relative_to(REPOSITORY)),
                    reference=reference,
                ):
                    self.assertRegex(reference, PINNED_ACTION)


if __name__ == "__main__":
    unittest.main()
