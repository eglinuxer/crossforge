import argparse
import ast
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/run-qt-target-runtime.py"
RUNTIME = runpy.run_path(str(SCRIPT))


class RunQtTargetRuntimeTests(unittest.TestCase):
    def arguments(self, arch):
        return argparse.Namespace(
            arch=arch,
            runtime_root=Path("/runtime-root"),
            qemu_cpu="cortex-a53",
            qemu_uname_release="4.18.0",
            qemu=None,
            native_release=False,
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
