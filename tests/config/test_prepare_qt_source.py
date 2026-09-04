import ast
import copy
import hashlib
import io
import json
import runpy
import tarfile
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/prepare-qt-source.py"
PREPARE = runpy.run_path(str(SCRIPT))


class PrepareQtSourceTests(unittest.TestCase):
    def component(self):
        binding = json.loads(
            (REPOSITORY / "config/generated/release-binding.json").read_text(
                encoding="utf-8"
            )
        )
        record = next(
            record
            for record in binding["components"]
            if record["component"] == "sources/qt"
        )
        return REPOSITORY / record["path"], record["canonical_sha256"]

    def fixture_archive(self, root, extra=None):
        path = root / "qt.tar.xz"
        records = []
        with tarfile.open(str(path), "w:xz") as archive:
            for index, name in enumerate(PREPARE["EXPECTED_FILES"]):
                payload = ("qt-marker-%02d-%s\n" % (index, name)).encode("utf-8")
                member = tarfile.TarInfo(
                    "%s/%s" % (PREPARE["TOP_DIRECTORY"], name)
                )
                member.size = len(payload)
                member.mode = 0o775 if name == "configure" else 0o664
                archive.addfile(member, io.BytesIO(payload))
                records.append(
                    {"file": name, "sha256": hashlib.sha256(payload).hexdigest()}
                )
            if extra is not None:
                archive.addfile(extra)
        source = {
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        layout = {
            "top_directory": PREPARE["TOP_DIRECTORY"],
            "member_count": len(PREPARE["EXPECTED_FILES"])
            + (1 if extra is not None else 0),
            "files": records,
        }
        return path, source, layout

    def test_component_binds_archive_checksum_layout_modules_and_licenses(self):
        path, digest = self.component()
        _document, source, checksum, layout = PREPARE["load_policy"](
            path, digest
        )
        self.assertEqual(source["size"], 994798840)
        self.assertEqual(
            source["sha256"],
            "1da37a32a583e7856d6fc13357c8ff6ad3ef7b877b8d276713b85026426d5246",
        )
        self.assertEqual(layout["member_count"], 399185)
        self.assertEqual(
            [record["file"] for record in layout["files"]],
            list(PREPARE["EXPECTED_FILES"]),
        )
        evidence = PREPARE["decode_checksum_evidence"](
            REPOSITORY / checksum["evidence"], checksum, source["sha256"]
        )
        self.assertEqual(evidence["size"], 108)
        self.assertEqual(
            evidence["sha256"],
            "f208721e3239cba3d21312295e7d991f378e83e79e51e55fe2ffb6c05726bb0a",
        )

    def test_small_archive_exercises_safe_layout_and_file_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, source, layout = self.fixture_archive(Path(temporary))
            archive, files = PREPARE["inspect_archive"](
                path, source, layout
            )
        self.assertEqual(archive["size"], source["size"])
        self.assertEqual(len(files), 16)
        self.assertEqual(files[7]["file"], "configure")
        self.assertEqual(files[7]["mode"], "0775")
        self.assertTrue(all(record["size"] > 0 for record in files))

    def test_archive_rejects_a_link_that_escapes_the_top_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            link = tarfile.TarInfo(
                PREPARE["TOP_DIRECTORY"] + "/unsafe-link"
            )
            link.type = tarfile.SYMTYPE
            link.linkname = "../../outside"
            path, source, layout = self.fixture_archive(
                Path(temporary), extra=link
            )
            with self.assertRaisesRegex(
                PREPARE["QtSourceError"], "escapes"
            ):
                PREPARE["inspect_archive"](path, source, layout)

    def test_manifest_schema_rejects_unknown_fields(self):
        component_path, component_sha256 = self.component()
        _document, source, checksum, layout = PREPARE["load_policy"](
            component_path, component_sha256
        )
        files = [
            {
                "file": record["file"],
                "sha256": record["sha256"],
                "size": 1,
                "mode": "0775" if record["file"] == "configure" else "0664",
            }
            for record in layout["files"]
        ]
        manifest = {
            "$schema": PREPARE["SCHEMA_ID"],
            "schema_version": 1,
            "kind": "crossforge-qt-source",
            "version": "6.8.4",
            "source_component": {
                "component": "sources/qt",
                "canonical_sha256": component_sha256,
            },
            "archive": {
                "file": PREPARE["ARCHIVE_NAME"],
                "sha256": source["sha256"],
                "size": source["size"],
            },
            "checksum": {
                "file": PREPARE["CHECKSUM_NAME"],
                "sha256": checksum["sha256"],
                "size": checksum["size"],
                "authentication": checksum["authentication"],
                "evidence": checksum["evidence"],
                "evidence_sha256": "0" * 64,
            },
            "top_directory": layout["top_directory"],
            "member_count": layout["member_count"],
            "files": files,
        }
        schema = REPOSITORY / "config/schemas/qt-source-manifest.schema.json"
        PREPARE["validate_manifest"](manifest, schema)
        invalid = copy.deepcopy(manifest)
        invalid["trusted"] = True
        with self.assertRaises(PREPARE["QtSourceError"]):
            PREPARE["validate_manifest"](invalid, schema)

    def test_wrong_component_digest_is_rejected(self):
        path, _digest = self.component()
        with self.assertRaises(PREPARE["QtSourceError"]):
            PREPARE["load_policy"](path, "0" * 64)

    def test_script_remains_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
