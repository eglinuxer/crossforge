import ast
import copy
import io
import json
import os
import runpy
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/native-aarch64-release.py"
sys.path.insert(0, str(REPOSITORY / "scripts"))
NATIVE = runpy.run_path(str(SCRIPT))
CANDIDATE = runpy.run_path(str(REPOSITORY / "scripts/candidate_manifest.py"))


class NativeAarch64ReleaseTests(unittest.TestCase):
    def write_json(self, path, document):
        path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def fixture(self, directory):
        root = Path(directory)
        release_path = REPOSITORY / "config/release.json"
        release_schema = REPOSITORY / "config/schemas/release.schema.json"
        release = NATIVE["validated_release"](release_path, release_schema)
        candidate = CANDIDATE["candidate_document"](
            release,
            "1" * 40,
            "sha256:" + "2" * 64,
            "sha256:" + "3" * 64,
        )
        candidate_path = root / "candidate.json"
        self.write_json(candidate_path, candidate)
        qualification_path = (
            REPOSITORY
            / "config/generated/components/toolchain/aarch64-qualification.json"
        )
        policy_path = (
            REPOSITORY
            / "config/generated/components/implementation/candidate-manifest.json"
        )
        binding_path = REPOSITORY / "config/generated/release-binding.json"
        binding_schema = REPOSITORY / "config/schemas/release-binding.schema.json"
        binding = NATIVE["validated_binding"](
            binding_path, binding_schema, release
        )
        qualification = NATIVE["selected_component"](
            binding,
            qualification_path,
            "toolchain/aarch64-qualification",
            "qualification",
        )
        compile_report = {
            "target": NATIVE["TARGET"],
            "release_sha256": NATIVE["canonical_sha256"](release),
            "qualification_component": qualification,
            "native_release_execution": {
                "status": "required",
                "executor": "native-el8-aarch64",
            },
            "locked_sysroot_execution": {"status": "not_run"},
            "clean_runtime_execution": {"status": "not_run"},
            "binaries": {
                name: {} for name in sorted(NATIVE["COMPILE_BINARIES"])
            },
        }
        compile_path = root / "compile-report.json"
        self.write_json(compile_path, compile_report)
        artifacts = root / "artifacts"
        artifacts.mkdir()
        for name in NATIVE["ARTIFACTS"]:
            path = artifacts / name
            path.write_bytes(("native-fixture-%s\n" % name).encode("utf-8"))
            os.chmod(str(path), 0o755)
        output = root / "native-aarch64-probes.tar"
        arguments = SimpleNamespace(
            release=release_path,
            release_schema=release_schema,
            candidate=candidate_path,
            candidate_schema=REPOSITORY / "config/schemas/candidate.schema.json",
            qualification_component=qualification_path,
            candidate_policy_component=policy_path,
            bundle_schema=(
                REPOSITORY
                / "config/schemas/native-aarch64-probe-bundle.schema.json"
            ),
            release_binding=binding_path,
            release_binding_schema=binding_schema,
            compile_report=compile_path,
            artifacts=artifacts,
            output=output,
        )
        NATIVE["create_bundle"](arguments)
        identity, components = NATIVE["identity_context"](arguments, release)
        validation = copy.copy(arguments)
        validation.bundle = output
        validation.expected_bundle_sha256 = NATIVE["sha256_file"](output)
        bundle_sha256, manifest, payloads = NATIVE["validate_bundle"](
            validation, release, identity, components
        )
        return {
            "arguments": arguments,
            "validation": validation,
            "release": release,
            "identity": identity,
            "components": components,
            "bundle_sha256": bundle_sha256,
            "manifest": manifest,
            "payloads": payloads,
        }

    def report(self, fixture):
        release = fixture["release"]
        manifest = fixture["manifest"]
        stdout = {
            "catch": b"",
            "hello": b"crossforge-c-ok\n",
            "libgcc-helper": b"",
            "lto": b"",
            "lto-archive": b"",
            "modern": b"crossforge-cxx-ok\n",
        }
        executions = {
            name: {
                "status": "passed",
                "stdout_sha256": NATIVE["sha256_bytes"](stdout[name]),
            }
            for name in NATIVE["EXECUTABLES"]
        }
        return {
            "$schema": NATIVE["REPORT_SCHEMA_ID"],
            "schema_version": 1,
            "kind": "crossforge-native-aarch64-release-qualification",
            "status": "passed",
            "executor": "native",
            "target": NATIVE["TARGET"],
            "candidate": fixture["identity"],
            "release_sha256": NATIVE["canonical_sha256"](release),
            "qualification_component": fixture["components"]["qualification"],
            "candidate_policy_component": fixture["components"]["candidate_policy"],
            "bundle_sha256": fixture["bundle_sha256"],
            "compile_report_sha256": manifest["compile_report"]["sha256"],
            "artifacts": manifest["artifacts"],
            "host": {
                "runner_label": "ubuntu-24.04-arm",
                "runner_arch": "ARM64",
                "host_machine": "aarch64",
                "container_machine": "aarch64",
                "kernel_release": "6.8.0-test",
            },
            "runtime": {
                "reference": "%s:%s@%s"
                % (
                    release["base_image"]["repository"],
                    release["base_image"]["tag"],
                    release["base_image"]["manifests"]["arm64"],
                ),
                "index_digest": release["base_image"]["digest"],
                "manifest_digest": release["base_image"]["manifests"]["arm64"],
                "os_release_sha256": "5" * 64,
                "loader_sha256": "6" * 64,
            },
            "loader_dependencies": sorted(
                [
                    "/lib/ld-linux-aarch64.so.1",
                    "libc.so.6 => /lib64/libc.so.6",
                    "libgcc_s.so.1 => /lib64/libgcc_s.so.1",
                    "libstdc++.so.6 => /lib64/libstdc++.so.6",
                    "libthrow.so => /tmp/crossforge-native-aarch64-probes/artifacts/libthrow.so",
                ]
            ),
            "executions": executions,
        }

    def test_bundle_is_deterministic_strict_and_candidate_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            first = Path(fixture["arguments"].output).read_bytes()
            NATIVE["create_bundle"](fixture["arguments"])
            self.assertEqual(Path(fixture["arguments"].output).read_bytes(), first)
            self.assertEqual(
                [record["name"] for record in fixture["manifest"]["artifacts"]],
                list(NATIVE["ARTIFACTS"]),
            )
            self.assertEqual(
                fixture["manifest"]["candidate"]["digest"],
                "sha256:" + "2" * 64,
            )

    def test_report_rebinds_bundle_runtime_and_native_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            report = self.report(fixture)
            NATIVE["validate_schema"](
                report,
                REPOSITORY
                / "config/schemas/native-aarch64-qualification.schema.json",
                NATIVE["REPORT_SCHEMA_ID"],
            )
            NATIVE["validate_report_document"](
                report,
                fixture["release"],
                fixture["identity"],
                fixture["components"],
                fixture["manifest"],
                fixture["bundle_sha256"],
            )
            for field in (
                "bundle_sha256",
                "runtime",
                "loader_dependencies",
                "executions",
            ):
                invalid = copy.deepcopy(report)
                if field == "bundle_sha256":
                    invalid[field] = "0" * 64
                elif field == "runtime":
                    invalid[field]["manifest_digest"] = "sha256:" + "0" * 64
                elif field == "loader_dependencies":
                    invalid[field] = [
                        line
                        for line in invalid[field]
                        if not line.startswith("libc.so.6 ")
                    ]
                else:
                    invalid[field]["hello"]["stdout_sha256"] = "0" * 64
                with self.subTest(field=field):
                    with self.assertRaises(NATIVE["NativeReleaseError"]):
                        NATIVE["validate_report_document"](
                            invalid,
                            fixture["release"],
                            fixture["identity"],
                            fixture["components"],
                            fixture["manifest"],
                            fixture["bundle_sha256"],
                        )

    def test_bundle_rejects_unexpected_or_nonregular_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unsafe.tar"
            with tarfile.open(str(path), "w") as archive:
                record = tarfile.TarInfo("../escape")
                record.size = 1
                archive.addfile(record, fileobj=io.BytesIO(b"x"))
            with self.assertRaises(NATIVE["NativeReleaseError"]):
                NATIVE["read_bundle"](path)

    def test_release_binding_is_the_component_digest_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            binding = json.loads(
                Path(fixture["arguments"].release_binding).read_text(
                    encoding="utf-8"
                )
            )
            record = next(
                record
                for record in binding["components"]
                if record["component"] == "implementation/candidate-manifest"
            )
            record["canonical_sha256"] = "0" * 64
            changed = Path(temporary) / "changed-binding.json"
            self.write_json(changed, binding)
            arguments = copy.copy(fixture["arguments"])
            arguments.release_binding = changed
            with self.assertRaises(NATIVE["NativeReleaseError"]):
                NATIVE["identity_context"](
                    arguments, fixture["release"]
                )

    def test_native_commands_have_a_hard_timeout(self):
        with mock.patch.object(
            NATIVE["subprocess"],
            "run",
            side_effect=subprocess.TimeoutExpired(["probe"], 30),
        ):
            with self.assertRaisesRegex(
                NATIVE["NativeReleaseError"], "exceeded 30 seconds"
            ):
                NATIVE["command_bytes"](["probe"], {})

    def test_script_remains_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
