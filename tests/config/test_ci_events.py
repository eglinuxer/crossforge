"""Exercise the workflow's actual event/diff selection against a real Git DAG."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]


class CIEventSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.name", "CI fixture")
        self.git("config", "user.email", "ci-fixture@example.invalid")
        (self.repo / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/ci-plan.py", self.repo / "scripts/ci-plan.py")
        self.base = self.commit("docs/readme.md")
        workflow = (ROOT / ".github/workflows/verify-quick.yml").read_text()
        selection = workflow.split("      - name: Select affected build profile\n", 1)[1]
        self.command = textwrap.dedent(
            selection.split("        run: |\n", 1)[1].split("\n      - name: Select component roots", 1)[0])

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo,
                                       stderr=subprocess.PIPE, text=True).strip()

    def commit(self, path):
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("fixture\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "--message", "Fixture change")
        return self.git("rev-parse", "HEAD")

    def select(self, event, head, push_base="", pr_base=""):
        output = self.repo / ".git/github-output"
        output.write_text("")
        environment = dict(os.environ, EVENT_NAME=event, GITHUB_SHA=head,
                           PUSH_BASE=push_base, PR_BASE=pr_base,
                           GITHUB_OUTPUT=str(output))
        result = subprocess.run(["bash", "-e", "-c", self.command], cwd=self.repo,
                                env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return output.read_text().strip()

    def test_main_push_selects_changed_paths(self):
        for path, expected in (("docs/guide.md", "none"),
                               ("scripts/build-cpython-cross.sh", "python"),
                               ("tools/crossforge/cli.py", "sdk"),
                               ("unknown/input", "full")):
            with self.subTest(path=path):
                base = self.git("rev-parse", "HEAD")
                head = self.commit(path)
                self.assertEqual(self.select("push", head, push_base=base),
                                 "profile=" + expected)

    def test_missing_push_base_and_manual_dispatch_select_full(self):
        head = self.commit("docs/guide.md")
        for base in ("", "0" * 40, "f" * 40):
            with self.subTest(base=base):
                self.assertEqual(self.select("push", head, push_base=base), "profile=full")
        self.assertEqual(self.select("workflow_dispatch", head), "profile=full")
        self.assertEqual(self.select("pull_request", head, pr_base="f" * 40), "profile=full")

    def test_deleted_source_still_selects_its_build(self):
        base = self.commit("scripts/build-cpython-cross.sh")
        self.git("rm", "scripts/build-cpython-cross.sh")
        self.git("commit", "--quiet", "--message", "Delete build script")
        self.assertEqual(self.select("push", self.git("rev-parse", "HEAD"),
                                     push_base=base), "profile=python")

    def test_pr_uses_merge_base_instead_of_unrelated_main_changes(self):
        self.git("checkout", "--quiet", "-b", "topic")
        head = self.commit("docs/topic.md")
        self.git("checkout", "--quiet", "main")
        main = self.commit("config/release.json")
        self.assertEqual(self.select("pull_request", head, pr_base=main), "profile=none")


if __name__ == "__main__":
    unittest.main()
