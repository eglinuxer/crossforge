import ast
import io
import runpy
import tarfile
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/build-xcb-util-cursor.py"
BUILD = runpy.run_path(str(SCRIPT))


class BuildXcbUtilCursorTests(unittest.TestCase):
    def test_build_identities_separate_host_execution_from_targets(self):
        identities = BUILD["IDENTITIES"]
        self.assertEqual(identities["host"]["role"], "host-qt-build")
        self.assertIsNone(identities["host"]["triple"])
        for triple, arch in (
            ("x86_64-unknown-linux-gnu", "x86_64"),
            ("aarch64-unknown-linux-gnu", "aarch64"),
        ):
            self.assertEqual(identities[triple]["role"], "qt-target")
            self.assertEqual(identities[triple]["arch"], arch)
            self.assertEqual(identities[triple]["triple"], triple)

    def test_source_extraction_rejects_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "source.tar.xz"
            with tarfile.open(str(archive_path), "w:xz") as archive:
                directory = tarfile.TarInfo(BUILD["TOP_DIRECTORY"] + "/")
                directory.type = tarfile.DIRTYPE
                archive.addfile(directory)
                copying = tarfile.TarInfo(BUILD["TOP_DIRECTORY"] + "/COPYING")
                copying.size = 4
                archive.addfile(copying, io.BytesIO(b"MIT\n"))
                link = tarfile.TarInfo(BUILD["TOP_DIRECTORY"] + "/linked")
                link.type = tarfile.SYMTYPE
                link.linkname = "COPYING"
                archive.addfile(link)
            with self.assertRaisesRegex(BUILD["BuildError"], "special"):
                BUILD["extract_source"](archive_path, root / "source", 3)

    def test_source_extraction_normalizes_release_timestamps(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "source.tar.xz"
            with tarfile.open(str(archive_path), "w:xz") as archive:
                member = tarfile.TarInfo(BUILD["TOP_DIRECTORY"] + "/configure")
                member.size = 4
                member.mtime = 123456789
                archive.addfile(member, io.BytesIO(b"exit"))
            destination = root / "source"
            BUILD["extract_source"](archive_path, destination, 1)
            self.assertEqual((destination / "configure").stat().st_mtime, 0)

    def test_manifest_schema_rejects_target_execution_claim_conflicts(self):
        schema = BUILD["STRICT"]["load_json"](
            REPOSITORY / "config/schemas/xcb-util-cursor-build.schema.json"
        )
        BUILD["STRICT"]["validate_schema_subset"](schema)
        checks = schema["$defs"]["checks"]
        with self.assertRaises(BUILD["STRICT"]["ValidationError"]):
            BUILD["STRICT"]["validate"](
                {
                    "no_rpath": True,
                    "no_textrel": True,
                    "target_execution": "qemu",
                    "host_probe_passed": False,
                },
                checks,
                schema,
                "$.checks",
            )

    def test_script_remains_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
