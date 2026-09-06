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
SCRIPT = REPOSITORY / "scripts/prepare-qemu-source.py"
PREPARER = runpy.run_path(str(SCRIPT))


class PrepareQEMUSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = PREPARER["STRICT"]["load_json"](
            REPOSITORY / "config/release.json"
        )
        cls.policy = cls.release["qemu"]["executor"]["source"]["archive"]

    def test_release_binds_official_archive_signature_key_and_exception(self):
        self.assertEqual(
            self.policy["sha256"],
            "2aa0e420e4ea89ea34a833f4c4eced96a35b51a9ee8568b232692729b60b064d",
        )
        signature = self.policy["signature"]
        payload = base64.b64decode(
            b"".join(
                (REPOSITORY / signature["evidence"]).read_bytes().split()
            ),
            validate=True,
        )
        self.assertEqual(len(payload), signature["size"])
        self.assertEqual(hashlib.sha256(payload).hexdigest(), signature["sha256"])
        key = (REPOSITORY / signature["key"]["file"]).read_bytes()
        self.assertEqual(
            hashlib.sha256(key).hexdigest(), signature["key"]["sha256"]
        )
        self.assertEqual(
            signature["verification"],
            {
                "status": "cryptographically-valid-expired-key",
                "signature_time": "2026-05-27T22:12:30Z",
                "exception": "upstream-release-key-expired-before-signing",
            },
        )
        self.assertGreater(
            PREPARER["SIGNATURE"]["timestamp_epoch"](
                signature["verification"]["signature_time"]
            ),
            PREPARER["SIGNATURE"]["timestamp_epoch"](
                signature["key"]["expires_at"]
            ),
        )

    def test_signature_wrapper_passes_the_exact_expired_key_policy(self):
        function = PREPARER["verify_signature"]
        signature_tools = PREPARER["SIGNATURE"]
        original = signature_tools["verify_expired_key_signature"]
        policy = self.policy["signature"]
        calls = []

        def verify(gpg, archive, signature, key, selected):
            calls.append((gpg, archive, signature, key, selected))
            return {"status": selected["status"]}

        signature_tools["verify_expired_key_signature"] = verify
        try:
            result = function(
                Path("gpg"),
                Path("archive"),
                Path("signature"),
                Path("key"),
                policy,
            )
        finally:
            signature_tools["verify_expired_key_signature"] = original
        self.assertEqual(result, {"status": "cryptographically-valid-expired-key"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][-1],
            {
                "key_sha256": policy["key"]["sha256"],
                "primary_fingerprint": policy["key"]["fingerprint"],
                "signing_fingerprint": policy["key"]["fingerprint"],
                "signing_key_expires_at": policy["key"]["expires_at"],
                "signature_time": policy["verification"]["signature_time"],
                "status": policy["verification"]["status"],
                "exception": policy["verification"]["exception"],
            },
        )

    def test_archive_layout_and_reviewed_external_symlink_are_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "qemu.tar.xz"
            root = "qemu-fixture"
            payloads = {"VERSION": b"1\n", "COPYING": b"license\n"}
            with tarfile.open(str(archive_path), "w:xz") as archive:
                top = tarfile.TarInfo(root)
                top.type = tarfile.DIRTYPE
                archive.addfile(top)
                for name, payload in payloads.items():
                    member = tarfile.TarInfo(root + "/" + name)
                    member.size = len(payload)
                    archive.addfile(member, io.BytesIO(payload))
                link = tarfile.TarInfo(root + "/external")
                link.type = tarfile.SYMTYPE
                link.linkname = "/opt/example"
                archive.addfile(link)
            identity = PREPARER["file_identity"](archive_path)
            policy = {
                "sha256": identity["sha256"],
                "size": identity["size"],
                "layout": {
                    "top_directory": root,
                    "member_count": 4,
                    "version_sha256": hashlib.sha256(payloads["VERSION"]).hexdigest(),
                    "license_sha256": hashlib.sha256(payloads["COPYING"]).hexdigest(),
                    "reviewed_external_symlinks": [
                        {"path": root + "/external", "target": "/opt/example"}
                    ],
                },
            }
            self.assertEqual(
                PREPARER["verify_archive"](archive_path, policy), identity
            )
            wrong = copy.deepcopy(policy)
            wrong["layout"]["reviewed_external_symlinks"] = []
            with self.assertRaisesRegex(
                PREPARER["ValidationError"], "external symlink"
            ):
                PREPARER["verify_archive"](archive_path, wrong)

    def test_docker_qualification_is_offline_and_in_normal_ci(self):
        dockerfile = (REPOSITORY / "docker/Dockerfile").read_text(
            encoding="utf-8"
        )
        download = dockerfile.split(" AS qemu-source-download", 1)[1]
        download = download.split("\nFROM ", 1)[0]
        qualified = dockerfile.split(" AS qemu-source-qualified", 1)[1]
        qualified = qualified.split("\nFROM ", 1)[0]
        self.assertIn("fetch-release-source.py qemu", download)
        self.assertNotIn("--network=none", download)
        self.assertIn("RUN --network=none", qualified)
        self.assertIn("prepare-qemu-source.py", qualified)
        self.assertIn("source_signature.py", qualified)
        self.assertIn(
            'target "qemu-source-qualified"',
            (REPOSITORY / "docker-bake.hcl").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "qemu-source-qualified",
            (REPOSITORY / ".github/workflows/ci.yml").read_text(
                encoding="utf-8"
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
