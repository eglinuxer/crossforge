import re
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
DOCKERFILE = REPOSITORY / "docker/Dockerfile"


def stages(text):
    matches = list(
        re.finditer(
            r"^FROM(?:\s+--platform=[^\s]+)?\s+([^\s]+)"
            r"(?:\s+AS\s+([a-zA-Z0-9_.-]+))?\s*$",
            text,
            re.MULTILINE,
        )
    )
    result = {}
    parents = {}
    for index, match in enumerate(matches):
        name = match.group(2)
        if name is None:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result[name] = text[match.start():end]
        parents[name] = match.group(1)
    dependencies = {}
    for name, block in result.items():
        references = set(
            re.findall(r"(?:--from=|,from=)([a-zA-Z0-9_.-]+)", block)
        )
        references.add(parents[name])
        dependencies[name] = {item for item in references if item in result}
    return result, parents, dependencies


def digest_arg(component):
    return "CROSSFORGE_COMPONENT_%s_SHA256" % re.sub(
        r"[/-]", "_", component.upper()
    )


class DockerComponentWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = DOCKERFILE.read_text(encoding="utf-8")
        cls.stages, cls.parents, cls.dependencies = stages(cls.text)

    def reachable_stages(self, root):
        pending = [root]
        result = set()
        while pending:
            stage = pending.pop()
            if stage in result:
                continue
            result.add(stage)
            pending.extend(self.dependencies.get(stage, ()))
        return result

    def assert_no_full_release(self, block):
        self.assertNotIn("COPY config/ ./config/", block)
        self.assertNotIn("COPY config/release.json", block)
        self.assertNotIn("config/schemas/release.schema.json", block)
        self.assertNotIn("COPY evidence/", block)
        self.assertNotIn("COPY patches/", block)
        self.assertNotIn("COPY config/rpm/", block)

    def assert_component_stage(self, stage, component, minimum_calls=1):
        block = self.stages[stage]
        argument = digest_arg(component)
        source = "config/generated/components/%s.json" % component
        self.assertIn("ARG %s" % argument, block)
        self.assertIn("COPY %s " % source, block)
        self.assertGreaterEqual(
            block.count("--release-component-name %s" % component),
            minimum_calls,
        )
        self.assertGreaterEqual(
            block.count('--release-component-sha256 "$%s"' % argument),
            minimum_calls,
        )
        self.assert_no_full_release(block)

    def test_config_validation_copies_locked_keys_and_notices(self):
        release = self.stages["release-validate"]
        self.assertEqual(self.parents["release-validate"], "rocky-base")
        self.assertIn("COPY config/release.json", release)
        self.assertIn("validate-release.py", release)
        self.assertNotIn("COPY config/ ./config/", release)
        self.assertNotIn("config/generated", release)
        block = self.stages["config-validate"]
        self.assertEqual(self.parents["config-validate"], "release-validate")
        self.assertIn("COPY config/ ./config/", block)
        self.assertIn(
            "COPY keys/ZSTD-RELEASE-KEY.asc ./keys/ZSTD-RELEASE-KEY.asc",
            block,
        )
        self.assertIn("keys/MICROSOFT-RELEASE-KEY.asc", block)
        self.assertIn("COPY licenses/ ./licenses/", block)
        self.assertIn("validate-supply-chain-evidence.py", block)

    def test_source_fetches_are_exact_component_only_branches(self):
        cases = {
            "gts-gcc-source": "sources/gcc",
            "gts-binutils-source": "sources/binutils",
        }
        for stage, component in cases.items():
            with self.subTest(stage=stage):
                block = self.stages[stage]
                argument = digest_arg(component)
                self.assertEqual(self.parents[stage], "rocky-base")
                self.assertIn(
                    "COPY config/generated/components/%s.json" % component,
                    block,
                )
                self.assertIn("COPY scripts/release_component.py", block)
                self.assertIn("COPY scripts/fetch-release-source.py", block)
                self.assertIn("ARG %s" % argument, block)
                self.assertIn("--expected-component %s" % component, block)
                self.assertIn("--expected-scope build", block)
                self.assertIn('--expected-sha256 "$%s"' % argument, block)
                self.assertNotIn("validate-release.py", block)
                self.assert_no_full_release(block)

    def test_locked_base_contains_only_shared_component_validation_inputs(self):
        block = self.stages["rpm-locked-input-base"]
        self.assertEqual(self.parents["rpm-locked-input-base"], "rocky-base")
        for path in (
            "config/schemas/rpm-transaction.schema.json",
            "config/schemas/rpm-lock.schema.json",
            "keys/RPM-GPG-KEY-rockyofficial",
            "scripts/validate-release.py",
            "scripts/release_component.py",
            "scripts/validate-rpm-lock.py",
            "scripts/materialize-sysroot.py",
        ):
            self.assertIn("COPY %s" % path, block)
        self.assert_no_full_release(block)

    def test_every_normal_rpm_role_uses_its_exact_projection_and_digest(self):
        cases = {
            "rpm/sysroot-x86_64": (
                "sysroot-rpms-x86_64",
                "sysroot-x86_64",
            ),
            "rpm/sysroot-aarch64": (
                "sysroot-rpms-aarch64",
                "sysroot-aarch64",
            ),
            "rpm/host-build-common": (
                "host-build-common-rpms",
                "host-build-common-locked",
            ),
            "rpm/host-gcc-build": (
                "host-gcc-build-rpms",
                "host-gcc-build-locked",
            ),
            "rpm/host-gcc-test": (
                "host-gcc-test-rpms",
                "host-gcc-test-locked",
            ),
            "rpm/host-python-build": (
                "host-python-build-rpms",
                "host-python-build-locked",
            ),
            "rpm/host-runtime": (
                "host-runtime-rpms",
                "host-runtime-locked",
            ),
        }
        for component, role_stages in cases.items():
            for index, stage in enumerate(role_stages):
                with self.subTest(component=component, stage=stage):
                    self.assert_component_stage(
                        stage, component, minimum_calls=2 if index == 0 else 1
                    )
        for stage in (
            "sysroot-rpms-x86_64",
            "sysroot-x86_64",
            "sysroot-rpms-aarch64",
            "sysroot-aarch64",
            "host-build-common-rpms",
            "host-build-common-locked",
            "host-gcc-build-rpms",
            "host-gcc-test-rpms",
            "host-python-build-rpms",
            "host-runtime-rpms",
            "host-runtime-locked",
        ):
            self.assertEqual(self.parents[stage], "rpm-locked-input-base")

    def test_normal_build_ancestor_graph_never_reaches_full_release_base(self):
        normal = {
            "gts-gcc-source",
            "gts-binutils-source",
            "rpm-locked-input-base",
            "sysroot-rpms-x86_64",
            "sysroot-x86_64",
            "sysroot-rpms-aarch64",
            "sysroot-aarch64",
            "host-build-common-rpms",
            "host-build-common-locked",
            "host-gcc-build-rpms",
            "host-gcc-build-locked",
            "host-gcc-test-rpms",
            "host-gcc-test-locked",
            "host-python-build-rpms",
            "host-python-build-locked",
            "host-runtime-rpms",
            "host-runtime-locked",
            "binutils-prep-input",
            "gcc-prep-input",
            "binutils-x86_64",
            "binutils-aarch64",
            "gcc-x86_64-built",
            "gcc-aarch64-built",
            "gcc-x86_64",
            "gcc-aarch64",
        }
        for stage in normal:
            with self.subTest(stage=stage):
                reachable = self.reachable_stages(stage)
                self.assertNotIn("rpm-input-base", reachable)
                self.assertNotIn("config-validate", reachable)
                for ancestor in reachable:
                    self.assert_no_full_release(self.stages[ancestor])

    def test_maintenance_and_qualification_full_release_edges_are_explicit(self):
        maintenance = self.stages["rpm-input-base"]
        self.assertIn("COPY config/release.json", maintenance)
        self.assertIn("COPY config/schemas/release.schema.json", maintenance)
        for stage in (
            "rpm-resolve-sysroot-x86_64",
            "rpm-resolve-sysroot-aarch64",
            "rpm-resolve-host-build-common",
            "rpm-resolve-host-runtime",
        ):
            self.assertEqual(self.parents[stage], "rpm-input-base")
        for stage in (
            "rpm-resolve-host-gcc-build",
            "rpm-resolve-host-python-build",
        ):
            self.assertEqual(self.parents[stage], "host-build-common-locked")
            self.assertIn(
                "COPY --from=rpm-input-base /src/config/ ./config/",
                self.stages[stage],
            )
            self.assertIn("rpm-input-base", self.dependencies[stage])
        self.assertEqual(
            self.parents["rpm-resolve-host-gcc-test"],
            "host-gcc-build-locked",
        )
        self.assertIn(
            "COPY --from=rpm-input-base /src/config/ ./config/",
            self.stages["rpm-resolve-host-gcc-test"],
        )
        self.assertIn(
            "rpm-input-base", self.dependencies["rpm-resolve-host-gcc-test"]
        )

        for stage in (
            "python-runtime-clean-x86_64",
            "python-runtime-clean-aarch64",
        ):
            block = self.stages[stage]
            self.assertIn(
                "COPY --from=release-validate /src/config/release.json", block
            )
            self.assertIn(
                "COPY --from=release-validate /src/config/schemas/release.schema.json",
                block,
            )
            self.assertIn(
                "COPY --from=release-validate /src/config/schemas/rpm-plan.schema.json",
                block,
            )
            self.assertIn(
                "COPY --from=release-validate /src/config/rpm/sysroot-el8-",
                block,
            )
        self.assertIn(
            "COPY --from=release-validate /src/config/release.json",
            self.stages["runtime-smoke-aarch64"],
        )
        self.assertEqual(self.parents["runtime-smoke-x86_64"], "rocky-base")

    def test_install_outputs_keep_component_binding_evidence_visible(self):
        for stage in (
            "host-build-common-locked",
            "host-gcc-build-locked",
            "host-gcc-test-locked",
            "host-python-build-locked",
            "host-runtime-locked",
        ):
            self.assertIn("--marker /usr/share/crossforge/rpm-locks/", self.stages[stage])
        materializer = (
            REPOSITORY / "scripts/materialize-sysroot.py"
        ).read_text(encoding="utf-8")
        installer = (
            REPOSITORY / "scripts/install-host-rpm-lock.py"
        ).read_text(encoding="utf-8")
        self.assertIn("sysroot-release-binding.json", materializer)
        self.assertIn('"release_binding": release_binding', installer)

    def test_host_runtime_replay_is_offline_and_uses_only_its_bundle(self):
        block = self.stages["host-runtime-locked"]
        self.assertIn("RUN --network=none", block)
        self.assertIn("from=host-runtime-rpms", block)
        self.assertNotIn("host-build-common", block)
        self.assertNotIn("host-python-build", block)


if __name__ == "__main__":
    unittest.main()
