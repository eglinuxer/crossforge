import json
import runpy
import subprocess
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
COMPLETE = runpy.run_path(
    str(REPOSITORY / "scripts/render-release-components.py")
)
CORE = runpy.run_path(
    str(REPOSITORY / "scripts/release-components-core.py")
)
VCPKG_COMPONENTS = {
    "host-tools/cmake",
    "host-tools/ninja",
    "implementation/cmake-host-tool",
    "implementation/ninja-host-tool",
    "implementation/vcpkg-contract-qualification",
    "implementation/vcpkg-integration",
    "implementation/vcpkg-upstream-tier1-qualification",
    "vcpkg/contract-qualification",
    "vcpkg/sdk-build",
    "vcpkg/upstream-tier1-qualification",
}


class ReleaseComponentDomainIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )

    def digests(self, documents):
        return {
            name: COMPLETE["canonical_sha256"](document)
            for name, document in documents.items()
        }

    def test_complete_renderer_is_core_plus_the_vcpkg_extension(self):
        core = CORE["render_component_documents"](self.release)
        complete = COMPLETE["render_component_documents"](self.release)
        self.assertEqual(set(complete) - set(core), VCPKG_COMPONENTS)
        self.assertEqual(
            self.digests(core),
            {
                name: digest
                for name, digest in self.digests(complete).items()
                if name in core
            },
        )

    def test_core_cannot_write_a_partial_generated_graph(self):
        process = subprocess.run(
            [str(REPOSITORY / "scripts/release-components-core.py"), "--check"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(process.returncode, 2)
        self.assertIn("use render-release-components.py", process.stderr)

    def test_vcpkg_policy_change_only_rebinds_the_vcpkg_contract(self):
        policy = COMPLETE["VCPKG_CONTRACT_POLICY"]
        original = policy["downloads"]
        before_core = self.digests(
            CORE["render_component_documents"](self.release)
        )
        before_complete = self.digests(
            COMPLETE["render_component_documents"](self.release)
        )
        try:
            policy["downloads"] = "test-isolated-change"
            after_core = self.digests(
                CORE["render_component_documents"](self.release)
            )
            after_complete = self.digests(
                COMPLETE["render_component_documents"](self.release)
            )
        finally:
            policy["downloads"] = original
        self.assertEqual(after_core, before_core)
        self.assertEqual(
            {
                name
                for name in before_complete
                if before_complete[name] != after_complete[name]
            },
            {
                "implementation/vcpkg-contract-qualification",
                "vcpkg/contract-qualification",
                "vcpkg/upstream-tier1-qualification",
            },
        )

    def test_python_and_toolchain_stages_do_not_copy_the_vcpkg_domain(self):
        python_dockerfile = (
            REPOSITORY / "docker/python.Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn("scripts/release-components-core.py", python_dockerfile)
        self.assertNotIn("scripts/render-release-components.py", python_dockerfile)
        self.assertNotIn("scripts/release-components-vcpkg.py", python_dockerfile)

        toolchain_dockerfile = (REPOSITORY / "docker/Dockerfile").read_text(
            encoding="utf-8"
        )
        for stage, next_stage in (
            ("toolchain-x86_64-qualify-build", "toolchain-aarch64-qualify-build"),
            ("toolchain-aarch64-qualify-build", "runtime-smoke-aarch64"),
            ("runtime-smoke-aarch64", "toolchain-aarch64-qualify"),
        ):
            block = toolchain_dockerfile.split(" AS " + stage, 1)[1].split(
                " AS " + next_stage, 1
            )[0]
            with self.subTest(stage=stage):
                self.assertIn("scripts/release-components-core.py", block)
                self.assertNotIn("scripts/render-release-components.py", block)
                self.assertNotIn("scripts/release-components-vcpkg.py", block)


if __name__ == "__main__":
    unittest.main()
