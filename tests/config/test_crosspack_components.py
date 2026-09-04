import copy
import json
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
RENDERER = runpy.run_path(
    str(REPOSITORY / "scripts/render-release-components.py")
)
VALIDATOR = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
PREPARER_PATH = REPOSITORY / "scripts/prepare-nfpm-tool.py"
BUILDER_PATH = REPOSITORY / "scripts/build-crosspack-qualification.py"
FINALIZER_PATH = REPOSITORY / "scripts/finalize-crosspack-qualification.py"
COMPLETE_QUALIFIER_PATH = REPOSITORY / "scripts/qualify-complete-sdk.py"
PREPARER = runpy.run_path(str(PREPARER_PATH))
BAKE = runpy.run_path(str(REPOSITORY / "scripts/render-bake.py"))


def changed(before, after):
    return {
        name
        for name in set(before) & set(after)
        if RENDERER["canonical_sha256"](before[name])
        != RENDERER["canonical_sha256"](after[name])
    }


class CrosspackComponentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = VALIDATOR["load_json"](
            REPOSITORY / "config/release.json"
        )
        cls.components = RENDERER["render_component_documents"](
            cls.release
        )
        cls.digests = {
            name: RENDERER["canonical_sha256"](document)
            for name, document in cls.components.items()
        }

    def test_nfpm_and_crosspack_have_separate_build_identities(self):
        nfpm = self.components["sources/nfpm"]
        implementation = self.components["implementation/crosspack"]
        self.assertEqual(nfpm["scope"], "build")
        self.assertEqual(implementation["scope"], "build")
        self.assertTrue(
            all(item["path"].startswith("/nfpm/") for item in nfpm["materials"])
        )
        self.assertTrue(
            all(
                item["path"].startswith("/@implementation/crosspack/")
                for item in implementation["materials"]
            )
        )
        self.assertEqual(nfpm["dependencies"], [])
        self.assertEqual(implementation["dependencies"], [])
        sdk = self.components["packaging/sdk-build"]
        self.assertEqual(
            {item["component"] for item in sdk["dependencies"]},
            {
                "implementation/crosspack",
                "implementation/launcher",
                "sources/nfpm",
                "vcpkg/sdk-build",
            },
        )
        complete = self.components["product/sdk-qualification"]
        self.assertEqual(complete["scope"], "qualification")
        self.assertEqual(
            {item["component"] for item in complete["dependencies"]},
            {
                "implementation/complete-sdk-qualification",
                "implementation/launcher",
                "packaging/qualification",
                "python/qualification",
            },
        )
        qualification = self.components["packaging/qualification"]
        self.assertEqual(qualification["scope"], "qualification")
        self.assertEqual(
            {item["component"] for item in qualification["dependencies"]},
            {
                "implementation/crosspack-qualification",
                "packaging/sdk-build",
                "toolchain/aarch64-qualification",
                "toolchain/x86_64-qualification",
            },
        )

    def test_nfpm_change_does_not_invalidate_unrelated_components(self):
        release = copy.deepcopy(self.release)
        release["nfpm"]["binary"]["extracted_sha256"] = "0" * 64
        after = RENDERER["render_component_documents"](release)
        self.assertEqual(
            changed(self.components, after),
            {
                "sources/nfpm",
                "packaging/sdk-build",
                "packaging/qualification",
                "product/sdk-qualification",
            },
        )

    def test_crosspack_policy_matches_the_implemented_v1_boundary(self):
        policy = RENDERER["CROSSPACK_POLICY"]
        self.assertEqual(policy["schema_version"], 1)
        self.assertEqual(policy["formats"], ["deb", "rpm"])
        self.assertEqual(
            set(policy["targets"]), {"x86_64", "aarch64"}
        )
        self.assertEqual(
            policy["ownership"], "complete-exclusive-staged-tree"
        )
        self.assertEqual(
            policy["external_dependencies"], "explicit-per-format"
        )
        self.assertEqual(
            policy["debug_symbols"],
            "target-objcopy-only-keep-debug-strip-debug-gnu-debuglink",
        )
        self.assertEqual(
            policy["security"]["elf"]["needed"],
            "unique-loader-resolved-package-or-sysroot-provider",
        )
        self.assertEqual(
            policy["security"]["elf"]["runpath"],
            "origin-only-semantic-private-prefix-containment",
        )
        qualification = RENDERER["CROSSPACK_QUALIFICATION_POLICY"]
        self.assertEqual(qualification["installers"], {"deb": "dpkg", "rpm": "rpm"})
        self.assertTrue(
            qualification["requirements"]["detached_debug_symbols"]
        )
        self.assertTrue(
            qualification["requirements"]["dynamic_elf_audit"]
        )
        self.assertEqual(
            qualification["deb_test_image"]["amd64_manifest"],
            "sha256:5ae3c39ebd15e229dcedd5cee596b2497182493d41ff162e824ba13fc1b2b867",
        )
        launcher = RENDERER["CROSSFORGE_LAUNCHER_POLICY"]
        self.assertEqual(
            launcher["commands"], ["env", "info", "package", "run", "shell"]
        )
        self.assertEqual(
            launcher["target_selection"], "explicit-no-project-guessing"
        )
        complete = RENDERER["COMPLETE_SDK_QUALIFICATION_POLICY"]
        self.assertEqual(complete["matrix_size"], 24)
        self.assertFalse(complete["publishable"])

    def test_nfpm_source_archive_and_component_relationships_are_exact(self):
        nfpm = self.release["nfpm"]
        self.assertEqual(nfpm["source"]["tag"], "v" + nfpm["version"])
        self.assertEqual(
            nfpm["source"]["archive"],
            {
                "url": "https://github.com/goreleaser/nfpm/archive/refs/tags/v2.47.0.tar.gz",
                "sha256": "906b8dde0c5626376779f3ebddea3554b0eb6e1b8450a09bdc746cba92ef5dea",
                "sha512": "d5938898d59c3b9907fadb82a43e5dbd0c0954b90a1f8cf216dc39aeab57d69b3880280b3cc3d6fcbc916a7da28576d8bce7813ca00fbf567fe35e84947e4ec0",
                "size": 611939,
            },
        )
        component = self.components["sources/nfpm"]
        PREPARER["load_identity"](
            REPOSITORY / "config/generated/components/sources/nfpm.json",
            RENDERER["canonical_sha256"](component),
        )
        tampered = copy.deepcopy(component)
        tampered["materials"].pop()
        with self.assertRaises(PREPARER["PreparationError"]):
            PREPARER["load_identity"](
                REPOSITORY / "config/generated/components/sources/nfpm.json",
                RENDERER["canonical_sha256"](tampered),
            )

    def test_bake_graph_pins_nfpm_debian_and_qualification_edges(self):
        rendered = json.loads(BAKE["render"](REPOSITORY))
        targets = rendered["target"]
        self.assertEqual(targets["nfpm-tool"]["target"], "nfpm-tool-export")
        self.assertEqual(
            targets["packaging-sdk-dev"]["contexts"],
            {
                "crossforge_nfpm_tool": "target:nfpm-tool",
                "crossforge_sdk_base": "target:sdk-phase13-base",
            },
        )
        self.assertEqual(
            targets["packaging-sdk-dev"]["args"][
                "CROSSFORGE_LAUNCHER_COMPONENT_SHA256"
            ],
            self.digests["implementation/launcher"],
        )
        self.assertEqual(
            targets["packaging-qualified"]["contexts"]["crossforge_debian"],
            "docker-image://docker.io/library/debian:bookworm-slim@sha256:5ae3c39ebd15e229dcedd5cee596b2497182493d41ff162e824ba13fc1b2b867",
        )
        self.assertEqual(
            rendered["group"]["phase14"]["targets"],
            ["packaging-qualified"],
        )
        self.assertEqual(
            targets["sdk-complete-dev"]["contexts"],
            {
                "crossforge_packaging_qualified": "target:packaging-qualified",
                "crossforge_python_sdk": "target:python-dev",
            },
        )
        self.assertEqual(
            rendered["group"]["phase15"]["targets"],
            ["sdk-complete-dev"],
        )
        self.assertEqual(
            targets["sdk-candidate"]["inherits"], ["sdk-complete-dev"]
        )
        self.assertEqual(targets["sdk-candidate"]["target"], "sdk-candidate")
        self.assertEqual(
            targets["sdk-candidate"]["args"],
            {
                "CROSSFORGE_PRODUCT_VERSION": self.release["product"]["version"],
                "CROSSFORGE_PRODUCT_IDENTITY_SHA256": self.digests[
                    "product/identity"
                ],
            },
        )
        self.assertEqual(
            targets["sdk-candidate"]["output"], ["type=cacheonly"]
        )
        self.assertNotIn("tags", targets["sdk-candidate"])
        self.assertEqual(
            rendered["group"]["candidate"]["targets"], ["sdk-candidate"]
        )

    def test_docker_network_and_tool_boundaries_are_explicit(self):
        dockerfile = (
            REPOSITORY / "docker/packaging.Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertEqual(dockerfile.count("curl --fail --location --retry 3"), 4)
        for stage in (
            "nfpm-tool",
            "packaging-sdk",
            "crosspack-packages",
            "crosspack-deb-qualified",
            "crosspack-rpm-qualified",
            "packaging-qualified",
            "sdk-complete-dev",
            "sdk-candidate",
        ):
            block = dockerfile.split(" AS %s" % stage, 1)[1]
            block = block.split("\nFROM ", 1)[0]
            self.assertIn("RUN --network=none", block)
            self.assertNotIn("curl ", block)
        self.assertIn("FROM scratch AS nfpm-tool-export", dockerfile)
        self.assertIn(
            "COPY --chmod=0755 tools/crossforge/launcher /usr/local/bin/crossforge",
            dockerfile,
        )
        self.assertIn("crossforge info --json", dockerfile)
        self.assertNotIn("PYTHONPATH=/opt/crossforge/lib", dockerfile)
        self.assertNotIn("dnf install", dockerfile)
        self.assertNotIn("apt-get", dockerfile)
        candidate = dockerfile.split(" AS sdk-candidate", 1)[1]
        self.assertIn(
            "--expected-component product/identity", candidate
        )
        self.assertIn('test "${#CROSSFORGE_SOURCE_COMMIT}" -eq 40', candidate)
        self.assertIn("org.opencontainers.image.revision", candidate)
        self.assertIn("useradd --uid 1000 --gid 1000", candidate)
        self.assertIn("USER 1000:1000", candidate)
        self.assertIn("ENV HOME=/home/crossforge", candidate)
        self.assertNotIn("ENV CROSSFORGE_HOME=", candidate)

    def test_meson_cross_files_are_explicit_and_never_enable_execution(self):
        for arch, triple in (
            ("x86_64", "x86_64-unknown-linux-gnu"),
            ("aarch64", "aarch64-unknown-linux-gnu"),
        ):
            content = (
                REPOSITORY / "integration/meson" / (triple + ".ini")
            ).read_text(encoding="utf-8")
            with self.subTest(arch=arch):
                self.assertIn("needs_exe_wrapper = true", content)
                self.assertIn(
                    "sys_root = '/opt/crossforge/sysroots/el8/%s'" % arch,
                    content,
                )
                self.assertNotIn("exe_wrapper", content.split("[properties]", 1)[0])

    def test_packaging_scripts_remain_python36_and_posix_shell_compatible(self):
        import ast
        import subprocess

        for path in (
            PREPARER_PATH,
            BUILDER_PATH,
            FINALIZER_PATH,
            COMPLETE_QUALIFIER_PATH,
        ):
            ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
                feature_version=(3, 6),
            )
        process = subprocess.run(
            ["sh", "-n", str(REPOSITORY / "scripts/qualify-crosspack-install.sh")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)


if __name__ == "__main__":
    unittest.main()
