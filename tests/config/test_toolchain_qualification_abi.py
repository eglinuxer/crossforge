import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "scripts"))
import abi_contract  # noqa: E402


SPEC = importlib.util.spec_from_file_location(
    "crossforge_qualify_toolchain",
    str(REPOSITORY / "scripts/qualify-toolchain.py"),
)
QUALIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUALIFIER)


class ToolchainQualificationAbiTests(unittest.TestCase):
    def test_real_baselines_are_canonical_and_target_bound(self):
        expected = {
            "x86_64": (
                "x86_64-unknown-linux-gnu",
                "efaaceb1b9264f04f8abd4f94724649b116e99532969d61027e825681fff8944",
            ),
            "aarch64": (
                "aarch64-unknown-linux-gnu",
                "bcc86a87b1f9afd9ef864c0c75d571afdfaf5b1ca7fb135e62a26f3a3c1c8aab",
            ),
        }
        for arch, (triple, digest) in expected.items():
            with self.subTest(arch=arch):
                baseline, identity = QUALIFIER.load_abi_baseline(
                    REPOSITORY / ("abi/el8/%s.json" % arch), arch, triple
                )
                self.assertEqual(identity["baseline"], "el8")
                self.assertEqual(identity["canonical_sha256"], digest)
                self.assertEqual(identity["target"], baseline["target"])

        with self.assertRaisesRegex(
            abi_contract.AbiContractError, "ABI target architecture differs"
        ):
            QUALIFIER.load_abi_baseline(
                REPOSITORY / "abi/el8/x86_64.json",
                "aarch64",
                "aarch64-unknown-linux-gnu",
            )

    def test_noncanonical_or_non_el8_baseline_is_rejected(self):
        source = REPOSITORY / "abi/el8/x86_64.json"
        document = abi_contract.load_json(source)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.json"
            path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                QUALIFIER.QualificationError, "not canonical JSON"
            ):
                QUALIFIER.load_abi_baseline(
                    path, "x86_64", "x86_64-unknown-linux-gnu"
                )

            document["baseline"] = "other"
            document["review"]["source_inventory"] = (
                "evidence/abi/other-x86_64-clean.json"
            )
            path.write_bytes(abi_contract.canonical_bytes(document) + b"\n")
            with self.assertRaisesRegex(
                QUALIFIER.QualificationError, "unsupported ABI baseline identity"
            ):
                QUALIFIER.load_abi_baseline(
                    path, "x86_64", "x86_64-unknown-linux-gnu"
                )

    def test_audit_artifact_supplies_all_readelf_sections_to_shared_auditor(self):
        outputs = [
            "dynamic symbols",
            "version information",
            "dynamic section",
            "program headers",
            "ELF header",
        ]
        with mock.patch.object(
            QUALIFIER,
            "run",
            side_effect=[(output, "") for output in outputs],
        ) as run_mock, mock.patch.object(
            abi_contract, "audit_readelf", return_value={"status": "passed"}
        ) as audit_mock:
            result = QUALIFIER.audit_artifact(
                Path("/toolchain/readelf"),
                Path("/work/hello"),
                {"baseline": "sentinel"},
                "toolchain/hello",
                "crossforge-qualified-v1",
            )

        self.assertEqual(result, {"status": "passed"})
        self.assertEqual(
            [call.args[0][1:-1] for call in run_mock.call_args_list],
            [
                ["--wide", "--dyn-syms"],
                ["--wide", "--version-info"],
                ["--wide", "-d"],
                ["--wide", "-l"],
                ["--wide", "-h"],
            ],
        )
        audit_mock.assert_called_once_with(
            {"baseline": "sentinel"},
            "toolchain/hello",
            *outputs,
            profile_name="crossforge-qualified-v1",
        )

    def test_fixed_artifacts_use_one_observation_and_eight_qualified_links(self):
        source = (REPOSITORY / "scripts/qualify-toolchain.py").read_text(
            encoding="utf-8"
        )
        expected_artifacts = {
            "toolchain/hello",
            "toolchain/modern",
            "toolchain/lto",
            "toolchain/lto-archive",
            "toolchain/libgcc-helper",
            "toolchain/libthrow.so",
            "toolchain/catch",
            "toolchain/libstdc++-nonshared-audit.so",
            "toolchain/compiler-default-canary",
        }
        self.assertEqual(
            {
                artifact
                for artifact in expected_artifacts
                if ('"%s"' % artifact) in source
            },
            expected_artifacts,
        )
        self.assertEqual(source.count("HARDENED_LINKER_FLAG"), 9)
        self.assertIn(
            'run([gcc, "-O2", smoke / "hello.c", "-o", compiler_default_canary])',
            source,
        )
        self.assertNotIn("audit_versions", source)
        self.assertNotIn("EL8 ceiling", source)

    def test_docker_passes_only_the_matching_tracked_baseline(self):
        dockerfile = (REPOSITORY / "docker/Dockerfile").read_text(encoding="utf-8")
        for arch in ("x86_64", "aarch64"):
            path = "/src/abi/el8/%s.json" % arch
            self.assertIn("COPY abi/el8/%s.json %s" % (arch, path), dockerfile)
            self.assertIn("--abi-baseline %s" % path, dockerfile)
            argument = (
                "CROSSFORGE_COMPONENT_TOOLCHAIN_%s_QUALIFICATION_SHA256"
                % arch.upper()
            )
            self.assertIn("ARG " + argument, dockerfile)
            self.assertIn(
                '"$%s"' % argument,
                dockerfile,
            )
        self.assertEqual(
            dockerfile.count("--qualification-component-sha256"), 2
        )
        self.assertEqual(
            dockerfile.count(
                "COPY scripts/abi_contract.py /work/scripts/abi_contract.py"
            ),
            2,
        )

    def test_release_baseline_and_toolchain_component_are_exact(self):
        release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )
        abi = abi_contract.validate_release_abi_identities(release)
        for arch in ("x86_64", "aarch64"):
            baseline = abi_contract.load_json(
                REPOSITORY / ("abi/el8/%s.json" % arch)
            )
            self.assertEqual(
                abi["targets"][arch]["baseline"]["canonical_sha256"],
                abi_contract.canonical_sha256(baseline),
            )
            expected = QUALIFIER.RELEASE_COMPONENTS[
                "toolchain_qualification_component"
            ](release, arch)
            self.assertEqual(
                QUALIFIER.RELEASE_COMPONENTS[
                    "bind_toolchain_qualification_component"
                ](release, arch, expected["canonical_sha256"]),
                expected,
            )
            with self.assertRaises(QUALIFIER.ProjectionError):
                QUALIFIER.RELEASE_COMPONENTS[
                    "bind_toolchain_qualification_component"
                ](release, arch, "0" * 64)


if __name__ == "__main__":
    unittest.main()
