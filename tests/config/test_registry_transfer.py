import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import registry_transfer as transfer
    from crossforge_internal.identity import IdentityError, load_json
finally:
    sys.path.pop(0)


class RegistryTransferTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.policy = load_json(ROOT / ".github/locked-tools/oras.json")
        self.binary = self.root / "oras"
        self.binary.write_bytes(b"fixture executable; never run")
        self.binary.chmod(0o755)
        self.policy["binary_sha256"] = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        self.manifest = b'{"schemaVersion":2,"fixture":true}'
        self.digest = "sha256:" + hashlib.sha256(self.manifest).hexdigest()
        self.observation = {"root_digest": self.digest, "platform_digest": self.digest,
                            "config_digest": "sha256:" + "a" * 64, "platform": "linux/amd64"}

    def archive(self, duplicate=False, link=False):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for _ in range(2 if duplicate else 1):
                item = tarfile.TarInfo("oras")
                if link:
                    item.type, item.linkname = tarfile.SYMTYPE, "../../elsewhere"
                    archive.addfile(item)
                else:
                    item.size = self.binary.stat().st_size
                    archive.addfile(item, io.BytesIO(self.binary.read_bytes()))
            extra = tarfile.TarInfo("../../never-extracted")
            archive.addfile(extra)
        data = output.getvalue()
        path = self.root / "oras.tar.gz"
        path.write_bytes(data)
        self.policy["archive_sha256"] = hashlib.sha256(data).hexdigest()
        return path

    def test_installer_checks_both_hashes_and_extracts_only_regular_binary(self):
        archive = self.archive()
        destination = transfer.install_tool(self.policy, self.root / "installed", archive)
        self.assertEqual(destination.read_bytes(), self.binary.read_bytes())
        self.assertEqual(sorted(path.name for path in destination.parent.iterdir()), ["oras"])
        with self.assertRaises(IdentityError):
            transfer.install_tool(self.policy, destination.parent, archive)
        for field in ("archive_sha256", "binary_sha256"):
            policy = dict(self.policy, **{field: "0" * 64})
            with self.assertRaises(IdentityError):
                transfer.install_tool(policy, self.root / "bad", archive)
            self.assertFalse((self.root / "bad").exists())

    def test_installer_rejects_duplicate_or_link_binary(self):
        for duplicate, link in ((True, False), (False, True)):
            with self.assertRaises(IdentityError):
                transfer.install_tool(self.policy, self.root / "bad", self.archive(duplicate, link))
        with self.assertRaises(IdentityError):
            transfer.validate_tool(dict(self.policy, schema_version=True))
        with self.assertRaises(IdentityError):
            transfer.validate_tool(dict(self.policy, url="https://untrusted.invalid/binary"))

    def test_registry_references_cannot_use_credentials_urls_tags_or_nonlocal_http(self):
        for value in ("https://ghcr.io/org/components", "user:secret@ghcr.io/org/components",
                      "ghcr.io/org/components:latest", "ghcr.io/org/../components", "ghcr.io/org/components\n", "host:99999/components"):
            with self.subTest(value=value), self.assertRaises(IdentityError):
                transfer.repository(value)
        with self.assertRaises(IdentityError):
            transfer.repository("ghcr.io/org/components", True)
        with self.assertRaises(IdentityError):
            transfer.reference("ghcr.io/org/components:latest")
        self.assertEqual(transfer.reference("127.0.0.1:5000/components@" + self.digest, True),
                         ("127.0.0.1:5000/components", self.digest))

    def test_publish_copies_exact_digest_and_checks_original_manifest_bytes(self):
        with mock.patch.object(transfer.oci_layout, "inspect", return_value=self.observation), \
             mock.patch.object(transfer.subprocess, "run") as run, \
             mock.patch.object(transfer.subprocess, "check_output", return_value=self.manifest):
            result = transfer.publish(self.root, self.digest, "ghcr.io/org/components", self.binary, self.policy,
                                      registry_config=self.root / "credentials.json")
        args = run.call_args[0][0]
        self.assertIn("--from-oci-layout", args)
        self.assertIn(str(self.root) + "@" + self.digest, args)
        self.assertNotIn("--to-plain-http", args)
        self.assertEqual(result["reference"], "ghcr.io/org/components@" + self.digest)
        self.assertTrue(result["retention_tag"].endswith(self.digest[7:]))
        with mock.patch.object(transfer.oci_layout, "inspect", return_value=self.observation), \
             mock.patch.object(transfer.subprocess, "run"), \
             mock.patch.object(transfer.subprocess, "check_output", return_value=b"different"):
            with self.assertRaisesRegex(IdentityError, "manifest bytes differ"):
                transfer.publish(self.root, self.digest, "ghcr.io/org/components", self.binary, self.policy)

    def test_download_checks_blobs_and_cannot_overwrite_prior_output(self):
        destination = self.root / "download"
        remote = "ghcr.io/org/components@" + self.digest
        with mock.patch.object(transfer.subprocess, "run") as run, \
             mock.patch.object(transfer.oci_layout, "inspect", side_effect=IdentityError("corrupt blob")) as inspect:
            with self.assertRaisesRegex(IdentityError, "corrupt blob"):
                transfer.fetch(remote, destination, self.binary, self.policy)
            self.assertIn("--to-oci-layout", run.call_args[0][0])
            inspect.assert_called_once_with(destination, self.digest)
        with mock.patch.object(transfer.subprocess, "run") as run:
            with self.assertRaises(IdentityError):
                transfer.fetch(remote, destination, self.binary, self.policy)
            run.assert_not_called()

    def test_changed_or_symlink_executable_rejected_before_network(self):
        with mock.patch.object(transfer.subprocess, "run") as run:
            self.binary.write_bytes(b"changed")
            with self.assertRaises(IdentityError):
                transfer.fetch("ghcr.io/org/components@" + self.digest, self.root / "out", self.binary, self.policy)
            self.binary.unlink()
            self.binary.symlink_to(self.root / "missing")
            with self.assertRaises(IdentityError):
                transfer.fetch("ghcr.io/org/components@" + self.digest, self.root / "out", self.binary, self.policy)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
