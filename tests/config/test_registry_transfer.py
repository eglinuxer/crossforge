import hashlib
import io
from pathlib import Path
import sys
import subprocess
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
             mock.patch.object(transfer.subprocess, "run", side_effect=[self.copied(), self.fetched()]) as run:
            result = transfer.publish(self.root, self.digest, "ghcr.io/org/components", self.binary, self.policy,
                                      registry_config=self.root / "credentials.json")
        args = run.call_args_list[0][0][0]
        self.assertIn("--from-oci-layout", args)
        self.assertIn(str(self.root) + "@" + self.digest, args)
        self.assertNotIn("--to-plain-http", args)
        self.assertEqual(result["reference"], "ghcr.io/org/components@" + self.digest)
        self.assertTrue(result["retention_tag"].endswith(self.digest[7:]))
        with mock.patch.object(transfer.oci_layout, "inspect", return_value=self.observation), \
             mock.patch.object(transfer.subprocess, "run", side_effect=[self.copied(), self.fetched(b"different")]):
            with self.assertRaisesRegex(IdentityError, "manifest bytes differ"):
                transfer.publish(self.root, self.digest, "ghcr.io/org/components", self.binary, self.policy)

    def copied(self):
        return subprocess.CompletedProcess([], 0, stdout=None, stderr=b"")

    def fetched(self, content=None):
        return subprocess.CompletedProcess([], 0, stdout=self.manifest if content is None else content, stderr=b"")

    def missing(self, digest=None, returncode=1, fetch=False):
        digest = digest or self.digest
        if fetch:
            remote = "ghcr.io/org/components@" + digest
            message = 'Error response from registry: failed to fetch the content of "%s": %s: not found\n' % (remote, remote)
        else:
            message = "Error response from registry: %s: not found\n" % digest
        return subprocess.CalledProcessError(returncode, [str(self.binary), "cp"],
                                            stderr=message.encode())

    def publish(self):
        return transfer.publish(self.root, self.digest, "ghcr.io/org/components", self.binary, self.policy)

    def test_publication_retries_only_the_same_copy_and_still_checks_remote_bytes(self):
        with mock.patch.object(transfer.oci_layout, "inspect", return_value=self.observation), \
             mock.patch.object(transfer.subprocess, "run", side_effect=[self.missing(), self.copied(), self.fetched()]) as run, \
             mock.patch.object(transfer.time, "sleep") as sleep, mock.patch.object(sys, "stderr", io.StringIO()) as error:
            self.assertEqual(self.publish()["reference"], "ghcr.io/org/components@" + self.digest)
        self.assertEqual(run.call_args_list[0], run.call_args_list[1])
        self.assertEqual(run.call_args_list[2].args[0][-1], "ghcr.io/org/components@" + self.digest)
        sleep.assert_called_once_with(1)
        self.assertIn("not found", error.getvalue())

    def test_final_manifest_visibility_retries_the_read_without_recopying(self):
        with mock.patch.object(transfer.oci_layout, "inspect", return_value=self.observation), \
             mock.patch.object(transfer.subprocess, "run", side_effect=[self.copied(), self.missing(fetch=True), self.fetched()]) as run, \
             mock.patch.object(transfer.time, "sleep") as sleep, mock.patch.object(sys, "stderr", io.StringIO()):
            self.publish()
        self.assertEqual(run.call_args_list[1], run.call_args_list[2])
        self.assertEqual([c.args[0][1] for c in run.call_args_list], ["cp", "manifest", "manifest"])
        sleep.assert_called_once_with(1)

    def test_persistent_missing_copy_fails_after_three_attempts_without_acceptance(self):
        failures = [self.missing() for _ in range(3)]
        with mock.patch.object(transfer.oci_layout, "inspect", return_value=self.observation), \
             mock.patch.object(transfer.subprocess, "run", side_effect=failures) as run, \
             mock.patch.object(transfer.time, "sleep") as sleep, mock.patch.object(sys, "stderr", io.StringIO()), \
             self.assertRaises(subprocess.CalledProcessError) as raised:
            self.publish()
        self.assertIs(raised.exception, failures[-1])
        self.assertEqual(run.call_count, 3)
        self.assertTrue(all(c.args[0][1] == "cp" for c in run.call_args_list))
        self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])

    def test_persistent_missing_final_manifest_does_not_accept_a_successful_copy(self):
        with mock.patch.object(transfer.oci_layout, "inspect", return_value=self.observation), \
             mock.patch.object(transfer.subprocess, "run", side_effect=[self.copied()] + [self.missing(fetch=True) for _ in range(3)]) as run, \
             mock.patch.object(transfer.time, "sleep"), mock.patch.object(sys, "stderr", io.StringIO()), \
             self.assertRaises(subprocess.CalledProcessError):
            self.publish()
        self.assertEqual([c.args[0][1] for c in run.call_args_list], ["cp", "manifest", "manifest", "manifest"])

    def test_authentication_transport_other_digest_and_signal_errors_are_not_retried(self):
        failures = [self.missing("sha256:" + "f" * 64), self.missing(returncode=-15),
            subprocess.CalledProcessError(1, [], stderr=b"Error response from registry: unauthorized: authentication required\n"),
            subprocess.CalledProcessError(1, [], stderr=b"Error response from registry: denied: requested access to the resource is denied\n"),
            subprocess.CalledProcessError(1, [], stderr=b"connection reset by peer\n"),
            subprocess.CalledProcessError(1, [], stderr=None)]
        for failure in failures:
            with self.subTest(error=failure.stderr), \
                 mock.patch.object(transfer.oci_layout, "inspect", return_value=self.observation), \
                 mock.patch.object(transfer.subprocess, "run", side_effect=failure) as run, \
                 mock.patch.object(transfer.time, "sleep") as sleep, mock.patch.object(sys, "stderr", io.StringIO()), \
                 self.assertRaises(subprocess.CalledProcessError) as raised:
                self.publish()
            self.assertIs(raised.exception, failure)
            run.assert_called_once()
            sleep.assert_not_called()

    def test_final_read_of_another_digest_is_not_retried(self):
        with mock.patch.object(transfer.oci_layout, "inspect", return_value=self.observation), \
             mock.patch.object(transfer.subprocess, "run", side_effect=[self.copied(), self.missing("sha256:" + "f" * 64, fetch=True)]) as run, \
             mock.patch.object(transfer.time, "sleep") as sleep, mock.patch.object(sys, "stderr", io.StringIO()), \
             self.assertRaises(subprocess.CalledProcessError):
            self.publish()
        self.assertEqual(run.call_count, 2)
        sleep.assert_not_called()

    def test_retry_success_cannot_accept_a_changed_remote_manifest(self):
        with mock.patch.object(transfer.oci_layout, "inspect", return_value=self.observation), \
             mock.patch.object(transfer.subprocess, "run", side_effect=[self.missing(), self.copied(), self.fetched(b"corrupt")]) as run, \
             mock.patch.object(transfer.time, "sleep") as sleep, mock.patch.object(sys, "stderr", io.StringIO()), \
             self.assertRaisesRegex(IdentityError, "manifest bytes differ"):
            self.publish()
        self.assertEqual(run.call_count, 3)
        sleep.assert_called_once_with(1)

    def test_invalid_local_bytes_fail_before_any_publication_or_retry(self):
        with mock.patch.object(transfer.oci_layout, "inspect", side_effect=IdentityError("corrupt local blob")), \
             mock.patch.object(transfer.subprocess, "run") as run, mock.patch.object(transfer.time, "sleep") as sleep, \
             self.assertRaisesRegex(IdentityError, "corrupt local blob"):
            self.publish()
        run.assert_not_called()
        sleep.assert_not_called()

    def test_missing_download_is_fatal_and_does_not_use_publication_retries(self):
        with mock.patch.object(transfer.subprocess, "run", side_effect=self.missing()) as run, \
             mock.patch.object(transfer.time, "sleep") as sleep, self.assertRaises(subprocess.CalledProcessError):
            transfer.fetch("ghcr.io/org/components@" + self.digest, self.root / "absent", self.binary, self.policy)
        run.assert_called_once()
        sleep.assert_not_called()

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
