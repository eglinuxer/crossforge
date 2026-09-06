import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]


class RepositorySupportContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.security = (REPOSITORY / "SECURITY.md").read_text(encoding="utf-8")
        cls.support = (REPOSITORY / "SUPPORT.md").read_text(encoding="utf-8")
        cls.bug = (
            REPOSITORY / ".github/ISSUE_TEMPLATE/bug_report.yml"
        ).read_text(encoding="utf-8")
        cls.issue_config = (
            REPOSITORY / ".github/ISSUE_TEMPLATE/config.yml"
        ).read_text(encoding="utf-8")
        cls.promotion = (
            REPOSITORY / ".github/workflows/promote.yml"
        ).read_text(encoding="utf-8")

    def test_security_findings_have_one_private_non_issue_route(self):
        advisory = (
            "https://github.com/eglinuxer/crossforge/security/advisories/new"
        )
        self.assertIn(advisory, self.security)
        self.assertIn(advisory, self.issue_config)
        self.assertIn("Do not open a public issue", self.security)
        self.assertIn("no fixed response-time sla", self.security.lower())

    def test_public_bug_form_requires_reproduction_identity_and_safety(self):
        for field in (
            "id: digest",
            "id: info",
            "id: target",
            "id: engine",
            "id: reproducer",
            "id: expected",
            "id: actual",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.bug)
        self.assertGreaterEqual(self.bug.count("required: true"), 9)
        self.assertIn("immutable digest", self.bug)
        self.assertIn("no undisclosed vulnerability", self.bug)

    def test_support_scope_matches_the_release_contract(self):
        for value in (
            "linux/amd64",
            "x86_64-unknown-linux-gnu",
            "aarch64-unknown-linux-gnu",
            "gts15-el8",
            "3.9 is EOL",
            "3.10 through 3.12",
            "3.13 and 3.14",
            "Version tags are immutable",
        ):
            with self.subTest(value=value):
                self.assertIn(value, self.support)

    def test_stable_promotion_fails_before_mutation_without_private_reporting(self):
        self.assertIn(
            "private_vulnerability_reporting_enabled:", self.promotion
        )
        check = self.promotion.index("private-vulnerability-reporting")
        draft = self.promotion.index("gh release create")
        registry = self.promotion.index("docker buildx imagetools create")
        self.assertLess(check, draft)
        self.assertLess(check, registry)


if __name__ == "__main__":
    unittest.main()
