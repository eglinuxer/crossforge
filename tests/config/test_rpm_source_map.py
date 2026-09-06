import ast
import copy
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/capture-rpm-source-map.py"
CAPTURE = runpy.run_path(str(SCRIPT))


class RPMSourceMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = CAPTURE["load_release"](
            REPOSITORY / "config/release.json",
            REPOSITORY / "config/schemas/release.schema.json",
        )
        cls.lock, cls.transaction = CAPTURE["load_host_runtime_context"](
            cls.release,
            REPOSITORY / "locks/host-runtime-el8-x86_64.json",
        )

    def rpmdb_text(self):
        return "\n".join(
            "%s\tsource-%03d.src.rpm" % (nevra, index)
            for index, nevra in enumerate(
                self.transaction["manifests"]["base"]["packages"]
            )
        ) + "\n"

    def test_exact_base_manifest_is_mapped_to_source_rpms(self):
        document = CAPTURE["build_document"](
            self.release, self.lock, self.transaction, self.rpmdb_text()
        )
        self.assertEqual(len(document["packages"]), 148)
        self.assertEqual(
            [record["nevra"] for record in document["packages"]],
            self.transaction["manifests"]["base"]["packages"],
        )
        schema = CAPTURE["STRICT"]["load_json"](
            REPOSITORY / "config/schemas/rpm-source-map.schema.json"
        )
        CAPTURE["STRICT"]["validate_schema_subset"](schema)
        CAPTURE["STRICT"]["validate"](
            document, schema, schema, "$"
        )

    def test_reviewed_map_matches_the_fixed_rpmdb_capture(self):
        path = (
            REPOSITORY
            / "evidence/sources/rocky-base-rpm-sources.json"
        )
        expected = CAPTURE["STRICT"]["load_json"](path)
        rpmdb = "\n".join(
            "%s\t%s" % (record["nevra"], record["source_rpm"])
            for record in expected["packages"]
        ) + "\n"
        document = CAPTURE["build_document"](
            self.release, self.lock, self.transaction, rpmdb
        )
        CAPTURE["validate_expected"](
            document,
            path,
            REPOSITORY / "config/schemas/rpm-source-map.schema.json",
        )

    def test_missing_extra_duplicate_and_binary_only_mappings_fail(self):
        lines = self.rpmdb_text().splitlines()
        mutations = (
            lines[:-1],
            lines + ["extra-0:1-1.x86_64\textra-1.src.rpm"],
            lines + [lines[0]],
            [lines[0].split("\t")[0] + "\t(none)"] + lines[1:],
        )
        for values in mutations:
            with self.subTest(values=len(values)):
                with self.assertRaises(CAPTURE["ValidationError"]):
                    CAPTURE["build_document"](
                        self.release,
                        self.lock,
                        self.transaction,
                        "\n".join(values) + "\n",
                    )

    def test_release_must_bind_the_exact_host_runtime_lock(self):
        release = copy.deepcopy(self.release)
        release["host_locks"]["host-runtime"][
            "canonical_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(
            CAPTURE["ValidationError"], "lock digest differs"
        ):
            CAPTURE["load_host_runtime_context"](
                release,
                REPOSITORY / "locks/host-runtime-el8-x86_64.json",
            )

    def test_docker_target_reads_only_the_fixed_rpmdb_without_network(self):
        dockerfile = (REPOSITORY / "docker/Dockerfile").read_text(
            encoding="utf-8"
        )
        block = dockerfile.split(" AS rocky-base-source-map", 1)[1]
        block = block.split("\nFROM ", 1)[0]
        self.assertIn(
            "FROM config-validate AS rocky-base-source-map", dockerfile
        )
        self.assertIn("RUN --network=none", block)
        self.assertIn("rpm -qa --qf", block)
        self.assertIn("%{SOURCERPM}", block)
        self.assertIn("capture-rpm-source-map.py", block)
        self.assertIn("--expected", block)
        self.assertIn(
            "evidence/sources/rocky-base-rpm-sources.json", block
        )
        bake = (REPOSITORY / "docker-bake.hcl").read_text(encoding="utf-8")
        ci = (REPOSITORY / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('target "rocky-base-source-map"', bake)
        self.assertIn("rpm-source-lock-validated", ci)

    def test_capture_script_is_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
