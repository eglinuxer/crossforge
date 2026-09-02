import json
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
RENDERER = runpy.run_path(str(REPOSITORY / "scripts/render-bake.py"))


class PythonSourceComponentWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )
        cls.binding = json.loads(
            (REPOSITORY / "config/generated/release-binding.json").read_text(
                encoding="utf-8"
            )
        )
        cls.binding_records = {
            record["component"]: record
            for record in cls.binding["components"]
        }
        cls.bake = json.loads(RENDERER["render"](REPOSITORY))
        cls.targets = cls.bake["target"]
        dockerfile = (REPOSITORY / "docker/python.Dockerfile").read_text(
            encoding="utf-8"
        )
        cls.source_block = dockerfile.split(
            "FROM crossforge_rocky_amd64 AS cpython-source", 1
        )[1].split(
            "FROM crossforge_rocky_amd64 AS cpython-empty-patches-build", 1
        )[0]

    def expected_row(self, contract):
        entry = next(
            item
            for item in self.release["python"]["versions"]
            if item["version"].rsplit(".", 1)[0] == contract["minor"]
        )
        component = "python/%s-source" % contract["row"]
        policy = "implementation/python-%s-build-policy" % contract["row"]
        return {
            "CPYTHON_ROW": contract["row"],
            "CPYTHON_MINOR": contract["minor"],
            "CPYTHON_VERSION": entry["version"],
            "CPYTHON_ADAPTER": contract["adapter"],
            "CPYTHON_SOURCE_COMPONENT": component,
            "CPYTHON_SOURCE_COMPONENT_SHA256": self.binding_records[component][
                "canonical_sha256"
            ],
            "CPYTHON_BUILD_POLICY_COMPONENT": policy,
            "CPYTHON_BUILD_POLICY_COMPONENT_SHA256": self.binding_records[
                policy
            ]["canonical_sha256"],
        }

    def test_source_stage_copies_only_the_selected_projection_and_three_tools(self):
        block = self.source_block
        self.assertIn("ARG CPYTHON_SOURCE_COMPONENT\n", block)
        self.assertIn("ARG CPYTHON_SOURCE_COMPONENT_SHA256\n", block)
        self.assertIn(
            "COPY config/generated/components/python/${CPYTHON_ROW}-source.json",
            block,
        )
        self.assertEqual(block.count("COPY config/generated/components/"), 1)
        copy_region = block.split("RUN ", 1)[0]
        for tool in (
            "scripts/release_component.py",
            "scripts/python_row_contract.py",
            "scripts/fetch-release-source.py",
        ):
            self.assertEqual(copy_region.count(tool), 1, tool)
        for forbidden in (
            "crossforge_config",
            "config/release.json",
            "release.schema.json",
            "validate-release.py",
            "evidence/",
            "patches/",
            "verify-python-row.py",
            "prepare-cpython-source.py",
        ):
            self.assertNotIn(forbidden, block)

    def test_source_stage_checks_contract_then_uses_component_fetch_mode(self):
        block = self.source_block
        self.assertIn(
            "/work/scripts/python_row_contract.py \\\n      check \"$CPYTHON_VERSION\" \"$CPYTHON_ADAPTER\"",
            block,
        )
        self.assertIn(
            "--component-file /work/config/python-source-component.json",
            block,
        )
        self.assertIn(
            '--expected-component "$CPYTHON_SOURCE_COMPONENT"', block
        )
        self.assertIn("--expected-scope build", block)
        self.assertIn(
            '--expected-sha256 "$CPYTHON_SOURCE_COMPONENT_SHA256"', block
        )
        self.assertNotIn("--config", block)
        self.assertNotIn("--schema", block)

    def test_every_parameterized_row_target_carries_its_source_identity(self):
        for contract in RENDERER["IMPLEMENTED_ROWS"]:
            expected = self.expected_row(contract)
            row = contract["row"]
            source_target = self.targets["cpython-source-%s" % row]
            self.assertEqual(source_target["args"], expected)
            for name, target in self.targets.items():
                arguments = target.get("args", {})
                if arguments.get("CPYTHON_ROW") != row:
                    continue
                with self.subTest(row=row, target=name):
                    self.assertEqual(
                        arguments["CPYTHON_SOURCE_COMPONENT"],
                        expected["CPYTHON_SOURCE_COMPONENT"],
                    )
                    self.assertEqual(
                        arguments["CPYTHON_SOURCE_COMPONENT_SHA256"],
                        expected["CPYTHON_SOURCE_COMPONENT_SHA256"],
                    )

    def test_each_source_target_has_the_digest_pinned_rocky_named_context(self):
        base = self.release["base_image"]
        expected = {
            "crossforge_rocky_amd64": "docker-image://%s:%s@%s"
            % (
                base["repository"],
                base["tag"],
                base["manifests"]["amd64"],
            )
        }
        for contract in RENDERER["IMPLEMENTED_ROWS"]:
            target = self.targets["cpython-source-%s" % contract["row"]]
            self.assertEqual(target["contexts"], expected)
            self.assertNotIn("crossforge_config", target["contexts"])

    def test_phase_target_lists_remain_the_append_only_contract(self):
        expected_rows = {
            5: ("cp313",),
            6: ("cp313", "cp311"),
            7: ("cp313", "cp311", "cp312"),
        }
        for phase, rows in expected_rows.items():
            expected = [
                "validate",
                "platform-python-check",
                "host-python-build-locked",
            ]
            expected.extend("cpython-build-%s" % row for row in rows)
            expected.extend(
                ("python-runtime-clean-x86_64", "python-runtime-clean-aarch64")
            )
            expected.extend(
                "cpython-%s-%s-qualify" % (row, arch)
                for row in rows
                for arch in RENDERER["PYTHON_TARGETS"]
            )
            expected.append("python-phase%d-dev" % phase)
            self.assertEqual(
                self.bake["group"]["phase%d" % phase]["targets"],
                expected,
            )


if __name__ == "__main__":
    unittest.main()
