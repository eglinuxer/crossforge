import ast
import base64
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
SCRIPT = REPOSITORY / "scripts/prepare-cmake-source.py"
PREPARER = runpy.run_path(str(SCRIPT))
COMPONENT_PATH = REPOSITORY / "config/generated/components/sources/cmake.json"


class PrepareCMakeSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.component = json.loads(COMPONENT_PATH.read_text(encoding="utf-8"))
        cls.digest = PREPARER["COMPONENT"]["canonical_sha256"](cls.component)
        cls.tool = PREPARER["load_policy"](COMPONENT_PATH, cls.digest)

    def test_component_binds_source_binary_summary_signature_and_key(self):
        source = self.tool["source"]
        self.assertEqual(source["size"], 13275398)
        self.assertEqual(
            source["sha256"],
            "65757f442fdd242e27f1728fc26dc0cba4164f7a0791a5c788631c00080369bc",
        )
        self.assertEqual(
            source["checksums"]["signature"]["verification"],
            {
                "status": "cryptographically-valid-expired-key",
                "signature_time": "2026-07-09T18:21:38Z",
                "exception": "upstream-signing-subkey-expired-before-signing",
            },
        )
        self.assertEqual(
            source["layout"]["license_sha256"], self.tool["license"]["sha256"]
        )
        for policy, label in (
            (source["checksums"], "CMake checksum evidence"),
            (
                source["checksums"]["signature"],
                "CMake checksum signature evidence",
            ),
        ):
            payload = PREPARER["evidence_bytes"](
                REPOSITORY,
                policy["evidence"],
                policy["sha256"],
                policy["size"],
                label,
            )
            self.assertEqual(len(payload), policy["size"])

    def test_checksum_manifest_binds_source_and_shipped_binary(self):
        checksums = self.tool["source"]["checksums"]
        payload = base64.b64decode(
            b"".join((REPOSITORY / checksums["evidence"]).read_bytes().split()),
            validate=True,
        )
        parsed = PREPARER["parse_checksums"](
            payload,
            {
                "entries": checksums["entries"],
                "source_sha256": self.tool["source"]["sha256"],
            },
            self.tool["binary"],
        )
        self.assertEqual(len(parsed), 21)
        for replacement in (b"0" * 64, b"f" * 64):
            with self.subTest(replacement=replacement):
                tampered = payload.replace(
                    self.tool["source"]["sha256"].encode("ascii"),
                    replacement,
                    1,
                )
                with self.assertRaises(PREPARER["ValidationError"]):
                    PREPARER["parse_checksums"](
                        tampered,
                        {
                            "entries": checksums["entries"],
                            "source_sha256": self.tool["source"]["sha256"],
                        },
                        self.tool["binary"],
                    )

    def test_archive_layout_and_license_correspondence_are_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "cmake.tar.gz"
            root = "cmake-fixture"
            payloads = {
                "LICENSE.rst": b"license\n",
                "README.rst": b"readme\n",
                "CMakeLists.txt": b"cmake_minimum_required(VERSION 3.20)\n",
            }
            with tarfile.open(str(archive_path), "w:gz") as archive:
                top = tarfile.TarInfo(root)
                top.type = tarfile.DIRTYPE
                archive.addfile(top)
                for name, payload in payloads.items():
                    member = tarfile.TarInfo(root + "/" + name)
                    member.size = len(payload)
                    archive.addfile(member, io.BytesIO(payload))
            identity = PREPARER["file_identity"](archive_path, "fixture")
            layout = {
                "top_directory": root,
                "member_count": 4,
                "license_sha256": hashlib.sha256(payloads["LICENSE.rst"]).hexdigest(),
                "readme_sha256": hashlib.sha256(payloads["README.rst"]).hexdigest(),
                "cmakelists_sha256": hashlib.sha256(
                    payloads["CMakeLists.txt"]
                ).hexdigest(),
            }
            source = dict(identity, layout=layout)
            license_policy = {
                "expression": "BSD-3-Clause",
                "path": "doc/cmake/LICENSE.rst",
                "sha256": layout["license_sha256"],
                "size": 1498,
            }
            self.assertEqual(
                PREPARER["verify_archive"](
                    archive_path, source, license_policy
                ),
                identity,
            )
            wrong = copy.deepcopy(source)
            wrong["layout"]["member_count"] += 1
            with self.assertRaisesRegex(
                PREPARER["ValidationError"], "member count"
            ):
                PREPARER["verify_archive"](
                    archive_path, wrong, license_policy
                )

    def test_docker_source_verification_is_offline_and_in_normal_ci(self):
        dockerfile = (REPOSITORY / "docker/host-tools.Dockerfile").read_text(
            encoding="utf-8"
        )
        fetch = dockerfile.split(" AS cmake-source-fetch", 1)[1]
        fetch = fetch.split("\nFROM ", 1)[0]
        source = dockerfile.split("FROM cmake-source-fetch AS cmake-source", 1)[1]
        source = source.split("\nFROM ", 1)[0]
        self.assertIn("fetch-release-source.py cmake", fetch)
        self.assertNotIn("--network=none", fetch)
        self.assertIn("RUN --network=none", source)
        self.assertIn("prepare-cmake-source.py", source)
        bake = (REPOSITORY / "docker-bake.override.json").read_text(
            encoding="utf-8"
        )
        self.assertIn('"cmake-source"', bake)
        self.assertIn(
            "cmake-source",
            (
                (REPOSITORY / ".github/workflows/verify-quick.yml").read_text(encoding="utf-8")
                + (REPOSITORY / "scripts/ci-build.py").read_text(encoding="utf-8")
            ),
        )

    def test_preparer_is_python36_compatible(self):
        for path in (SCRIPT, REPOSITORY / "scripts/source_signature.py"):
            ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
                feature_version=(3, 6),
            )


if __name__ == "__main__":
    unittest.main()
