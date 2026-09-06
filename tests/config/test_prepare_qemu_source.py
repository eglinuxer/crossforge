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
            PREPARER["timestamp_epoch"](
                signature["verification"]["signature_time"]
            ),
            PREPARER["timestamp_epoch"](signature["key"]["expires_at"]),
        )

    def test_signature_requires_exact_valid_and_expired_status_records(self):
        function = PREPARER["verify_signature"]
        globals_ = function.__globals__
        original = {
            name: globals_[name]
            for name in ("file_identity", "key_fingerprints", "gpg_status")
        }
        policy = self.policy["signature"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "archive"
            signature = root / "signature"
            key = root / "key"
            for path in (archive, signature, key):
                path.write_bytes(b"fixture")

            status = (
                "[GNUPG:] EXPKEYSIG 3353C9CEF108B584 Michael Roth\n"
                "[GNUPG:] VALIDSIG CEACC9E15534EBABB82D3FA03353C9CEF108B584 "
                "2026-05-27 1779919950 0 4 0 1 10 00 "
                "CEACC9E15534EBABB82D3FA03353C9CEF108B584\n"
            )
            globals_["file_identity"] = lambda _path: {
                "sha256": policy["key"]["sha256"],
                "size": key.stat().st_size,
            }
            globals_["key_fingerprints"] = lambda _gpg, _key, _home: [
                policy["key"]["fingerprint"]
            ]
            globals_["gpg_status"] = lambda command, _home: (
                (status, "") if "--verify" in command else ("", "")
            )
            try:
                function(Path("gpg"), archive, signature, key, policy)
                for bad_status in (
                    status.replace("[GNUPG:] EXPKEYSIG 3353C9CEF108B584 Michael Roth\n", ""),
                    "[GNUPG:] BADSIG 3353C9CEF108B584 Michael Roth\n" + status,
                ):
                    with self.subTest(status=bad_status):
                        globals_["gpg_status"] = lambda command, _home: (
                            (bad_status, "")
                            if "--verify" in command
                            else ("", "")
                        )
                        with self.assertRaises(PREPARER["ValidationError"]):
                            function(Path("gpg"), archive, signature, key, policy)
            finally:
                globals_.update(original)

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
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
