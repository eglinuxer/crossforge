import ast
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/resolve-rpm-source-lock.py"
RESOLVER = runpy.run_path(str(SCRIPT))


class ResolveRPMSourceLockTests(unittest.TestCase):
    def url(self, repository, filename):
        record = next(
            item
            for item in RESOLVER["REPOSITORIES"]
            if item["id"] == repository
        )
        return record["baseurl"] + "Packages/%s/%s" % (
            filename[0].lower(),
            filename,
        )

    def location_fixture(self):
        unique = "acl-2.2.53-3.el8.src.rpm"
        required = set(RESOLVER["EXPECTED_DUPLICATES"]) | {unique}
        urls = [self.url("baseos", unique)]
        for name in sorted(RESOLVER["EXPECTED_DUPLICATES"]):
            urls.extend(
                [self.url("baseos", name), self.url("appstream", name)]
            )
        return required, "\n".join(urls) + "\n"

    def test_locations_are_complete_and_duplicate_precedence_is_explicit(self):
        required, text = self.location_fixture()
        locations = RESOLVER["parse_locations"](text, required)
        self.assertEqual(set(locations), required)
        for name in RESOLVER["EXPECTED_DUPLICATES"]:
            self.assertEqual(
                [record["repository"] for record in locations[name]],
                ["baseos", "appstream"],
            )

    def test_missing_and_unreviewed_aliases_fail_closed(self):
        required, text = self.location_fixture()
        with self.assertRaises(RESOLVER["ValidationError"]):
            RESOLVER["parse_locations"](
                "\n".join(text.splitlines()[:-1]) + "\n", required
            )
        unique = "acl-2.2.53-3.el8.src.rpm"
        with self.assertRaisesRegex(
            RESOLVER["ValidationError"], "alias set differs"
        ):
            RESOLVER["parse_locations"](
                text + self.url("appstream", unique) + "\n", required
            )

    def test_source_header_and_signature_are_both_required(self):
        function = RESOLVER["verify_source_rpm"]
        globals_ = function.__globals__
        original = globals_["run"]

        def successful(command, _label):
            if "--checksig" in command:
                return (
                    "Header V4 RSA/SHA256 Signature, key ID 6d745a60: OK\n"
                    "Payload SHA256 digest: OK\n"
                )
            return "acl\t2.2.53\t3.el8\t1\n"

        globals_["run"] = successful
        try:
            self.assertEqual(
                function(
                    Path("acl.src.rpm"),
                    "acl-2.2.53-3.el8.src.rpm",
                    Path("rpmkeys"),
                    Path("rpm"),
                    "7051c470a929f454cebe37b715af5dac6d745a60",
                ),
                {
                    "name": "acl",
                    "version": "2.2.53",
                    "release": "3.el8",
                    "source_package": True,
                },
            )
            globals_["run"] = lambda _command, _label: "NOT OK\n"
            with self.assertRaises(RESOLVER["ValidationError"]):
                function(
                    Path("acl.src.rpm"),
                    "acl-2.2.53-3.el8.src.rpm",
                    Path("rpmkeys"),
                    Path("rpm"),
                    "7051c470a929f454cebe37b715af5dac6d745a60",
                )
        finally:
            globals_["run"] = original

    def test_maintenance_target_verifies_repository_and_payload_signatures(self):
        dockerfile = (REPOSITORY / "docker/Dockerfile").read_text(
            encoding="utf-8"
        )
        block = dockerfile.split(" AS rpm-source-lock-maintenance", 1)[1]
        block = block.split("\nFROM ", 1)[0]
        self.assertEqual(block.count(".repo_gpgcheck=1"), 3)
        self.assertEqual(block.count(".gpgcheck=1"), 3)
        self.assertIn("rpm --import", block)
        self.assertIn("resolve-rpm-source-lock.py", block)
        self.assertNotIn("--network=none", block)
        bake = (REPOSITORY / "docker-bake.hcl").read_text(encoding="utf-8")
        self.assertIn('target "rpm-source-lock-maintenance"', bake)
        for workflow in (
            REPOSITORY / ".github/workflows/ci.yml",
            REPOSITORY / ".github/workflows/candidate.yml",
        ):
            self.assertNotIn(
                "rpm-source-lock-maintenance", workflow.read_text(encoding="utf-8")
            )

    def test_resolver_is_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
