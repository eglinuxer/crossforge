import argparse
import ast
import json
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/run-qt-target-runtime.py"
RUNTIME = runpy.run_path(str(SCRIPT))
CANDIDATE = runpy.run_path(
    str(REPOSITORY / "scripts/candidate_manifest.py")
)


def source_bundle_identity(release, commit):
    return {
        "source_commit": commit,
        "release_sha256": CANDIDATE["canonical_sha256"](release),
        "archive": {
            "file": "crossforge-source-%s.tar.zst" % commit,
            "sha256": "6" * 64,
            "size": 2866173957,
        },
    }


class RunQtTargetRuntimeTests(unittest.TestCase):
    def arguments(self, arch):
        return argparse.Namespace(
            arch=arch,
            runtime_root=Path("/runtime-root"),
            qemu_cpu="cortex-a53",
            qemu_uname_release="4.18.0",
            qemu=None,
            native_release=False,
            candidate=None,
            candidate_schema=None,
            expected_source_commit=None,
            input_rootfs_sha256=None,
        )

    def test_script_remains_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )

    def test_runtime_diagnostics_are_bounded(self):
        value = "\n".join("line-%03d" % index for index in range(140))
        output = RUNTIME["bounded_diagnostics"](value)
        self.assertEqual(len(output.splitlines()), 120)
        self.assertTrue(output.startswith("line-020"))

    def test_rocky_810_os_release_is_accepted(self):
        RUNTIME["validate_rocky_release"](
            'NAME="Rocky Linux"\nID="rocky"\nVERSION_ID="8.10"\n'
        )
        with self.assertRaisesRegex(
            RUNTIME["ValidationError"], "runtime root is not Rocky 8.10"
        ):
            RUNTIME["validate_rocky_release"](
                'NAME="Rocky Linux"\nID="rocky"\nVERSION_ID="8.9"\n'
            )

    def test_native_command_uses_chroot_without_qemu(self):
        command = RUNTIME["runtime_command"](
            self.arguments("x86_64"), RUNTIME["runtime_environment"]()
        )
        self.assertEqual(
            command[:5],
            ["timeout", "90s", "chroot", "/runtime-root", "/usr/bin/env"],
        )
        self.assertNotIn("qemu-aarch64", " ".join(command))
        self.assertIn("QT_QPA_PLATFORM=offscreen", command)
        loader = RUNTIME["loader_command"](
            self.arguments("x86_64"),
            RUNTIME["runtime_environment"](),
            "/usr/plugins/platforms/libqxcb.so",
        )
        self.assertEqual(
            loader[-3:],
            [
                "/lib64/ld-linux-x86-64.so.2",
                "--list",
                "/usr/plugins/platforms/libqxcb.so",
            ],
        )

    def test_aarch64_command_uses_explicit_pinned_qemu_shape(self):
        command = RUNTIME["runtime_command"](
            self.arguments("aarch64"), RUNTIME["runtime_environment"]()
        )
        self.assertEqual(
            command[:6],
            [
                "timeout",
                "180s",
                "chroot",
                "/runtime-root",
                "/.crossforge/qemu-aarch64",
                "-L",
            ],
        )
        self.assertIn("cortex-a53", command)
        self.assertIn("4.18.0", command)
        self.assertIn("QT_QPA_PLATFORM=offscreen", command)
        loader = RUNTIME["loader_command"](
            self.arguments("aarch64"),
            RUNTIME["runtime_environment"](),
            "/usr/libexec/QtWebEngineProcess",
        )
        self.assertEqual(
            loader[-3:],
            [
                "/lib/ld-linux-aarch64.so.1",
                "--list",
                "/usr/libexec/QtWebEngineProcess",
            ],
        )
        plugin = RUNTIME["plugin_command"](
            self.arguments("aarch64"),
            RUNTIME["runtime_environment"](),
            "/usr/plugins/platforms/libqoffscreen.so",
        )
        self.assertIn("LD_DEBUG=libs", plugin)
        self.assertIn(
            "CROSSFORGE_QT_PLUGIN=/usr/plugins/platforms/libqoffscreen.so",
            plugin,
        )
        self.assertEqual(
            plugin[-1],
            "/opt/crossforge-qualification/qt/qt-plugin-probe",
        )

    def test_plugin_evidence_does_not_require_a_dso_init_hook(self):
        trace = "123: find library=libQt6Core.so.6 [0]; searching\n"
        dependencies = RUNTIME["LOADER"][
            "normalize_loader_debug_listing"
        ](trace)
        dependencies.append("dlopen:/usr/plugins/platforms/libqxcb.so")
        self.assertEqual(
            sorted(set(dependencies)),
            [
                "dlopen:/usr/plugins/platforms/libqxcb.so",
                "needed:libQt6Core.so.6",
            ],
        )

    def test_native_aarch64_release_command_has_no_chroot_or_qemu(self):
        arguments = self.arguments("aarch64")
        arguments.native_release = True
        arguments.runtime_root = Path("/")
        command = RUNTIME["runtime_command"](
            arguments, RUNTIME["runtime_environment"]()
        )
        self.assertEqual(command[:4], ["timeout", "180s", "/usr/bin/env", "-i"])
        self.assertNotIn("chroot", command)
        self.assertNotIn("qemu", " ".join(command).lower())
        self.assertIn("QT_QPA_PLATFORM=offscreen", command)
        with self.assertRaisesRegex(
            RUNTIME["ValidationError"],
            "native release is not executing on AArch64",
        ):
            RUNTIME["validate_executor"](arguments, {})

    def test_native_release_binds_candidate_and_rootfs_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = json.loads(
                (REPOSITORY / "config/release.json").read_text(
                    encoding="utf-8"
                )
            )
            candidate = CANDIDATE["candidate_document"](
                release,
                "1" * 40,
                "sha256:" + "2" * 64,
                "sha256:" + "3" * 64,
                "sha256:" + "4" * 64,
                "sha256:" + "5" * 64,
                source_bundle_identity(release, "1" * 40),
            )
            candidate_path = root / "candidate.json"
            candidate_path.write_text(
                json.dumps(candidate) + "\n", encoding="utf-8"
            )
            arguments = self.arguments("aarch64")
            arguments.native_release = True
            arguments.candidate = candidate_path
            arguments.candidate_schema = (
                REPOSITORY / "config/schemas/candidate.schema.json"
            )
            arguments.expected_source_commit = "1" * 40
            arguments.input_rootfs_sha256 = "4" * 64
            binding = RUNTIME["validate_candidate_binding"](
                arguments, release
            )
            self.assertEqual(binding["source_commit"], "1" * 40)
            self.assertEqual(binding["digest"], "sha256:" + "2" * 64)
            self.assertEqual(
                binding["canonical_sha256"],
                CANDIDATE["canonical_sha256"](candidate),
            )
            arguments.input_rootfs_sha256 = "invalid"
            with self.assertRaisesRegex(
                RUNTIME["ValidationError"], "rootfs digest is invalid"
            ):
                RUNTIME["validate_candidate_binding"](arguments, release)

    def test_non_release_tiers_reject_candidate_inputs(self):
        arguments = self.arguments("x86_64")
        arguments.expected_source_commit = "1" * 40
        with self.assertRaisesRegex(
            RUNTIME["ValidationError"],
            "must not receive candidate inputs",
        ):
            RUNTIME["validate_candidate_binding"](arguments, {})

    def test_runtime_schema_requires_release_only_identity_fields(self):
        schema = RUNTIME["STRICT"]["load_json"](
            REPOSITORY / "config/schemas/qt-target-runtime.schema.json"
        )
        RUNTIME["STRICT"]["validate_schema_subset"](schema)
        identity_schema = schema["properties"]["identity"]
        identity = {
            "target": {
                "arch": "aarch64",
                "triple": "aarch64-unknown-linux-gnu",
            },
            "tier": "clean-rocky-qemu",
            "base_image": {
                "index_digest": "sha256:" + "1" * 64,
                "manifest_digest": "sha256:" + "2" * 64,
            },
            "release_sha256": "3" * 64,
            "runtime_qualification": {
                "component": "future/qt-runtime-qualification",
                "canonical_sha256": "4" * 64,
                "plan_sha256": "5" * 64,
            },
            "build_evidence_sha256": "6" * 64,
            "overlay_evidence_sha256": "7" * 64,
            "artifact_tree": {"entries": 1, "sha256": "8" * 64},
        }
        validate = RUNTIME["STRICT"]["validate"]
        validate(identity, identity_schema, schema, "$.identity")
        identity["tier"] = "native-release"
        with self.assertRaises(RUNTIME["ValidationError"]):
            validate(identity, identity_schema, schema, "$.identity")
        identity["candidate"] = {
            "source_commit": "9" * 40,
            "repository": "ghcr.io/eglinuxer/crossforge",
            "digest": "sha256:" + "a" * 64,
            "platform_manifest_digest": "sha256:" + "b" * 64,
            "canonical_sha256": "c" * 64,
        }
        identity["input_rootfs_sha256"] = "d" * 64
        validate(identity, identity_schema, schema, "$.identity")
        identity["tier"] = "clean-rocky-qemu"
        with self.assertRaises(RUNTIME["ValidationError"]):
            validate(identity, identity_schema, schema, "$.identity")

    def test_artifact_tree_is_deterministic_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in RUNTIME["ARTIFACT_DIRECTORIES"]:
                (root / relative).mkdir(parents=True)
            for relative in (
                RUNTIME["CONSUMER"],
                RUNTIME["PLUGIN_CONSUMER"],
                "usr/libexec/QtWebEngineProcess",
                "usr/lib/libQt6Core.so.6.8.4",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(relative.encode("utf-8"))
            first = RUNTIME["artifact_tree"](root)
            second = RUNTIME["artifact_tree"](root)
            self.assertEqual(first, second)
            self.assertGreater(first["entries"], 0)
            link = root / "usr/lib/libQt6Escape.so.6"
            link.symlink_to("/outside")
            with self.assertRaises(RUNTIME["ValidationError"]):
                RUNTIME["artifact_tree"](root)


if __name__ == "__main__":
    unittest.main()
