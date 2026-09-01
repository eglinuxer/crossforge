import hashlib
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
FINALIZER = runpy.run_path(
    str(REPOSITORY / "scripts/finalize-aarch64-qualification.py")
)
LOADER_EVIDENCE = runpy.run_path(str(REPOSITORY / "scripts/loader_evidence.py"))


class Aarch64QualificationTests(unittest.TestCase):
    def write_result(self, directory, extra_lines=()):
        result = directory / "runtime.result"
        loader = Path(str(result) + ".loader")
        loader.write_text("libc.so.6 => /lib64/libc.so.6\n", encoding="utf-8")
        digest = hashlib.sha256(loader.read_bytes()).hexdigest()
        fields = {
            "schema_version": "1",
            "tier": "locked-sysroot",
            "status": "passed",
            "target": "aarch64-unknown-linux-gnu",
            "cpu": "cortex-a53",
            "uname_release": "4.18.0",
            "qemu_binary_sha256": "1" * 64,
            "qemu_version": "10.2.3",
            "runtime_os_release_sha256": "2" * 64,
            "loader_sha256": "3" * 64,
            "loader_evidence_sha256": digest,
            "hello_stdout_sha256": "4" * 64,
            "modern_stdout_sha256": "5" * 64,
        }
        lines = ["%s=%s" % item for item in fields.items()]
        lines.extend(extra_lines)
        result.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return result

    def test_runtime_result_binds_loader_listing(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.write_result(Path(temporary))
            parsed = FINALIZER["parse_runtime_result"](result, "locked-sysroot")
            self.assertEqual(parsed["status"], "passed")
            self.assertEqual(
                parsed["loader_dependencies"],
                ["libc.so.6 => /lib64/libc.so.6"],
            )

    def test_loader_evidence_ignores_aslr_addresses(self):
        first = """\
linux-vdso.so.1 (0x00000078bd100000)
libc.so.6 => /lib64/libc.so.6 (0x00000078bc000000)
/lib/ld-linux-aarch64.so.1 (0x00000078bd000000)
"""
        second = """\
/lib/ld-linux-aarch64.so.1 (0x000000743d000000)
libc.so.6   =>   /lib64/libc.so.6 (0x000000743c000000)
linux-vdso.so.1 (0x000000743e100000)
"""
        normalize = LOADER_EVIDENCE["normalize_loader_listing"]
        self.assertEqual(normalize(first), normalize(second))
        self.assertEqual(
            normalize(first),
            [
                "/lib/ld-linux-aarch64.so.1",
                "libc.so.6 => /lib64/libc.so.6",
                "linux-vdso.so.1",
            ],
        )

    def test_duplicate_runtime_result_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.write_result(Path(temporary), ("status=passed",))
            with self.assertRaises(FINALIZER["FinalizationError"]):
                FINALIZER["parse_runtime_result"](result, "locked-sysroot")

    def test_runtime_tier_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.write_result(Path(temporary))
            with self.assertRaises(FINALIZER["FinalizationError"]):
                FINALIZER["parse_runtime_result"](result, "clean-rocky")


if __name__ == "__main__":
    unittest.main()
