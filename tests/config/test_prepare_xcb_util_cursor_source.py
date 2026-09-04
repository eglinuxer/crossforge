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
SCRIPT = REPOSITORY / "scripts/prepare-xcb-util-cursor-source.py"
PREPARE = runpy.run_path(str(SCRIPT))


class PrepareXcbUtilCursorSourceTests(unittest.TestCase):
    def component(self):
        binding = json.loads(
            (REPOSITORY / "config/generated/release-binding.json").read_text(
                encoding="utf-8"
            )
        )
        record = next(
            record
            for record in binding["components"]
            if record["component"] == "sources/xcb-util-cursor"
        )
        return REPOSITORY / record["path"], record["canonical_sha256"]

    def fixture_archive(self, root, extra=None):
        path = root / "xcb-util-cursor.tar.xz"
        records = []
        with tarfile.open(str(path), "w:xz") as archive:
            for index, name in enumerate(PREPARE["EXPECTED_FILES"]):
                payload = ("xcb-cursor-marker-%02d-%s\n" % (index, name)).encode(
                    "utf-8"
                )
                member = tarfile.TarInfo(
                    "%s/%s" % (PREPARE["TOP_DIRECTORY"], name)
                )
                member.size = len(payload)
                member.mode = 0o755 if name == "configure" else 0o644
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

    def test_component_binds_archive_signature_key_layout_and_license(self):
        path, digest = self.component()
        _document, source, signature, license_identity, layout = PREPARE[
            "load_policy"
        ](path, digest)
        self.assertEqual(source["size"], 273084)
        self.assertEqual(
            source["sha256"],
            "fdeb8bd127873519be5cc70dcd0d3b5d33b667877200f9925a59fdcad8f7a933",
        )
        self.assertEqual(signature["size"], 566)
        self.assertEqual(
            signature["key"]["fingerprint"],
            "3ab285232c46ae43d8e192f4dab0f78ea6e7e2d2",
        )
        self.assertEqual(license_identity["expression"], "MIT")
        self.assertEqual(layout["member_count"], 40)

    def test_signature_evidence_is_exact_and_canonical(self):
        path, digest = self.component()
        _document, _source, signature, _license, _layout = PREPARE[
            "load_policy"
        ](path, digest)
        payload, envelope_sha256 = PREPARE["decode_signature_evidence"](
            REPOSITORY / signature["evidence"], signature
        )
        self.assertEqual(len(payload), 566)
        self.assertEqual(
            envelope_sha256,
            "6f8208ce445a0227658aaa73509b3669fc2f8b3a0b7a7c9d9798c39ca2f95372",
        )

    def test_small_archive_exercises_safe_layout_and_file_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, source, layout = self.fixture_archive(Path(temporary))
            archive, files = PREPARE["inspect_archive"](path, source, layout)
        self.assertEqual(archive["sha256"], source["sha256"])
        self.assertEqual([record["file"] for record in files], list(PREPARE["EXPECTED_FILES"]))
        self.assertEqual(files[1]["mode"], "0755")

    def test_archive_rejects_a_link_even_when_it_stays_under_the_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            link = tarfile.TarInfo(PREPARE["TOP_DIRECTORY"] + "/linked")
            link.type = tarfile.SYMTYPE
            link.linkname = "COPYING"
            path, source, layout = self.fixture_archive(Path(temporary), extra=link)
            with self.assertRaisesRegex(
                PREPARE["XcbCursorSourceError"], "link or special"
            ):
                PREPARE["inspect_archive"](path, source, layout)

    def test_manifest_schema_rejects_unknown_fields(self):
        component_path, component_sha256 = self.component()
        _document, source, signature, license_identity, layout = PREPARE[
            "load_policy"
        ](component_path, component_sha256)
        sizes = [1284, 685003, 30898, 305]
        modes = ["0644", "0755", "0644", "0644"]
        manifest = {
            "$schema": PREPARE["SCHEMA_ID"],
            "schema_version": 1,
            "kind": "crossforge-xcb-util-cursor-source",
            "version": PREPARE["VERSION"],
            "source_component": {
                "component": PREPARE["COMPONENT_NAME"],
                "canonical_sha256": component_sha256,
            },
            "archive": {
                "file": PREPARE["ARCHIVE_NAME"],
                "sha256": source["sha256"],
                "size": source["size"],
            },
            "signature": {
                "file": PREPARE["SIGNATURE_NAME"],
                "sha256": signature["sha256"],
                "size": signature["size"],
                "evidence": signature["evidence"],
                "evidence_sha256": "6f8208ce445a0227658aaa73509b3669fc2f8b3a0b7a7c9d9798c39ca2f95372",
                "key": signature["key"],
            },
            "license": license_identity,
            "top_directory": layout["top_directory"],
            "member_count": layout["member_count"],
            "files": [
                {
                    "file": record["file"],
                    "sha256": record["sha256"],
                    "size": sizes[index],
                    "mode": modes[index],
                }
                for index, record in enumerate(layout["files"])
            ],
        }
        PREPARE["validate_manifest"](
            manifest,
            REPOSITORY
            / "config/schemas/xcb-util-cursor-source-manifest.schema.json",
        )
        invalid = copy.deepcopy(manifest)
        invalid["trusted"] = True
        with self.assertRaises(PREPARE["XcbCursorSourceError"]):
            PREPARE["validate_manifest"](
                invalid,
                REPOSITORY
                / "config/schemas/xcb-util-cursor-source-manifest.schema.json",
            )

    def test_wrong_component_digest_is_rejected(self):
        path, _digest = self.component()
        with self.assertRaises(PREPARE["XcbCursorSourceError"]):
            PREPARE["load_policy"](path, "0" * 64)

    def test_script_remains_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
