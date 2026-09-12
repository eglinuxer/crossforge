"""A mutable registry index must never become a component trust decision."""

import copy
import hashlib
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import test_component_catalog as fixtures


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import catalog_registry as registry, component_catalog
    from crossforge_internal.identity import IdentityError, canonical_bytes, load_json
finally:
    sys.path.pop(0)


class CatalogRegistryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ComponentCatalogTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.expected = self.fixture.expected
        self.repository = component_catalog.REPOSITORY
        self.binary = self.root / "oras"
        self.binary.write_bytes(b"fixture ORAS executable; not executed")
        self.binary.chmod(0o755)
        self.policy = load_json(ROOT / ".github/locked-tools/oras.json")
        self.policy["binary_sha256"] = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        self.files = {"catalog.json": self.fixture.path.read_bytes(), "catalog.sigstore.json": self.fixture.bundle.read_bytes()}
        self.manifest = {"schemaVersion": 2, "mediaType": registry.MANIFEST_TYPE, "artifactType": registry.ARTIFACT_TYPE,
            "config": copy.deepcopy(registry.EMPTY_CONFIG), "annotations": {registry.CREATED: self.fixture.producer["started_at"]},
            "layers": [{"mediaType": registry.FILES[name][0], "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                "size": len(data), "annotations": {registry.TITLE: name}} for name, data in sorted(self.files.items())]}
        self.raw = canonical_bytes(self.manifest)
        self.digest = "sha256:" + hashlib.sha256(self.raw).hexdigest()
        self.reference = self.repository + "@" + self.digest
        self.remote = self.repository + ":" + registry.input_tag(self.expected, "toolchain-install")

    def lookup(self, reference=None):
        return registry.lookup(self.root, self.expected, "toolchain-install", self.fixture.cosign, self.root / "download",
            self.repository, self.binary, self.policy, catalog_reference=reference)

    def test_lookup_key_binds_component_role_and_full_expected_inputs(self):
        original = registry.input_tag(self.expected, "toolchain-install")
        self.assertRegex(original, r"^input-[0-9a-f]{64}$")
        self.assertEqual(original, registry.input_tag(copy.deepcopy(self.expected), "toolchain-install"))
        for field, value in (("component", "toolchain/aarch64-install"), ("targets", ["aarch64-unknown-linux-gnu"]),
                             ("parameters", {"execution": {"fixture": "different"}})):
            changed = copy.deepcopy(self.expected)
            changed[field] = value
            self.assertNotEqual(original, registry.input_tag(changed, "toolchain-install"))
        self.assertNotEqual(original, registry.input_tag(self.expected, "gcc-test-context"))
        with self.assertRaises(IdentityError):
            registry.input_tag(self.expected, "qualification")

    def test_only_exact_manifest_not_found_is_a_discovery_miss(self):
        absent = 'Error response from registry: failed to fetch the content of "%s": %s: not found\n' % (self.remote, self.remote)
        result = subprocess.CompletedProcess([], 1, b"", absent.encode())
        with mock.patch.object(registry.subprocess, "run", return_value=result), \
             mock.patch.object(component_catalog, "select") as select:
            self.assertEqual(self.lookup()["status"], "missing")
        select.assert_not_called()
        self.assertFalse((self.root / "download").exists())
        for stderr, stdout, code in ((b"unauthorized\n", b"", 1), (b"connection timed out: not found\n", b"", 1),
                                     (absent.replace(self.remote, "other").encode(), b"", 1),
                                     (absent.encode(), b"partial response", 1), (absent.encode(), b"", 2)):
            with self.subTest(stderr=stderr), mock.patch.object(registry.subprocess, "run",
                    return_value=subprocess.CompletedProcess([], code, stdout, stderr)):
                with self.assertRaises(subprocess.CalledProcessError):
                    self.lookup()

    def test_recovery_uses_exact_catalog_and_never_turns_absence_into_a_miss(self):
        absent = 'Error response from registry: failed to fetch the content of "%s": %s: not found\n' % (self.reference, self.reference)
        with mock.patch.object(registry.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", absent.encode())) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                self.lookup(self.reference)
        self.assertEqual(run.call_args[0][0][-1], self.reference)
        with self.assertRaises(IdentityError):
            self.lookup(self.repository + ":latest")
        with self.assertRaises(IdentityError):
            self.lookup("ghcr.io/another/components@" + self.digest)

    def test_envelope_cannot_add_paths_remote_urls_configs_or_ambiguous_blobs(self):
        for change in ("schema", "artifact-type", "unknown", "config", "filename", "duplicate", "size", "large", "url", "digest"):
            value = copy.deepcopy(self.manifest)
            layer = value["layers"][0]
            if change == "schema": value["schemaVersion"] = True
            elif change == "artifact-type": value["artifactType"] = "unrelated"
            elif change == "unknown": value["subject"] = {}
            elif change == "config": value["config"]["size"] = True
            elif change == "filename": layer["annotations"][registry.TITLE] = "../../outside"
            elif change == "duplicate": value["layers"][1] = copy.deepcopy(layer)
            elif change == "size": layer["size"] = True
            elif change == "large": layer["size"] = 65 * 1024 * 1024
            elif change == "url": layer["urls"] = ["https://untrusted.invalid"]
            else: layer["digest"] = "sha256:wrong"
            with self.subTest(change=change), self.assertRaises(IdentityError):
                registry.manifest(canonical_bytes(value))
        with self.assertRaises(IdentityError):
            registry.manifest(self.raw, "sha256:" + "f" * 64)

    def test_downloads_only_fixed_filenames_by_digest_and_checks_actual_bytes(self):
        for corrupt in (False, True):
            directory = self.root / ("bad" if corrupt else "good")
            def fetch(command, **kwargs):
                self.assertEqual(command[1:3], ["blob", "fetch"])
                self.assertNotIn("pull", command)
                path = Path(command[command.index("--output") + 1])
                descriptor = next(layer for layer in self.manifest["layers"] if layer["annotations"][registry.TITLE] == path.name)
                self.assertEqual(command[-1], self.repository + "@" + descriptor["digest"])
                path.write_bytes(b"corrupt" if corrupt else self.files[path.name])
            with mock.patch.object(registry.subprocess, "run", side_effect=fetch):
                if corrupt:
                    with self.assertRaisesRegex(IdentityError, "blob differs"):
                        registry._download(self.repository, self.raw, directory, self.binary, self.policy, None, False)
                else:
                    result = registry._download(self.repository, self.raw, directory, self.binary, self.policy, None, False)
                    self.assertEqual(result["reference"], self.reference)
                    self.assertEqual((directory / "catalog.json").read_bytes(), self.files["catalog.json"])
        with mock.patch.object(registry.subprocess, "run") as run, self.assertRaises(IdentityError):
            registry._download(self.repository, self.raw, self.root / "other", self.binary, self.policy, None, False,
                digest="sha256:" + "f" * 64)
        run.assert_not_called()

    def test_lookup_authenticates_downloaded_bytes_and_rejects_wrong_input_index(self):
        downloaded = {"reference": self.reference, "catalog": str(self.fixture.path), "bundle": str(self.fixture.bundle)}
        accepted = {"status": "authenticated-reference", "entry": self.fixture.entry,
                    "authentication": {"producer": self.fixture.producer}}
        with mock.patch.object(registry, "_manifest", return_value=self.raw), \
             mock.patch.object(registry, "_download", return_value=downloaded), \
             mock.patch.object(component_catalog, "select", return_value=accepted) as select:
            result = self.lookup()
        self.assertEqual(result["entry"], self.fixture.entry)
        self.assertEqual(result["catalog"]["reference"], self.reference)
        self.assertEqual(select.call_args[0][1:3], (str(self.fixture.path), str(self.fixture.bundle)))
        self.assertEqual(select.call_args[0][-2:], (self.expected, "toolchain-install"))
        for response in ({"status": "missing", "entry": None}, subprocess.CalledProcessError(1, "cosign")):
            with mock.patch.object(registry, "_manifest", return_value=self.raw), \
                 mock.patch.object(registry, "_download", return_value=downloaded), \
                 mock.patch.object(component_catalog, "select", **(
                     {"side_effect": response} if isinstance(response, Exception) else {"return_value": response})):
                with self.assertRaises((IdentityError, subprocess.CalledProcessError)):
                    self.lookup()

    def test_invalid_signature_cannot_publish_any_bytes_or_update_any_index(self):
        with mock.patch.object(component_catalog, "verify", side_effect=subprocess.CalledProcessError(1, "cosign")), \
             mock.patch.object(registry, "pack") as pack, mock.patch.object(registry, "_publish_packed") as publish:
            with self.assertRaises(subprocess.CalledProcessError):
                registry.publish(self.root, self.fixture.path, self.fixture.bundle, self.fixture.cosign,
                    self.root / "packed", self.repository, self.binary, self.policy)
        pack.assert_not_called()
        publish.assert_not_called()
        self.assertFalse((self.root / "packed").exists())

    def test_publish_uses_verified_snapshots_and_retains_immutable_catalog_before_indexing(self):
        events = []
        def verify(source, catalog, bundle, cosign):
            events.append("verify")
            self.assertEqual(Path(catalog).read_bytes(), self.files["catalog.json"])
            self.fixture.path.write_bytes(b"changed after snapshot")
            return self.fixture.value, {"producer": self.fixture.producer}
        def pack(catalog, bundle, *args):
            events.append("pack")
            self.assertEqual(Path(catalog).read_bytes(), self.files["catalog.json"])
            return {"digest": self.digest, "manifest": self.raw, "catalog": self.fixture.value}
        def publish(*args):
            events.append("publish immutable")
            return {"reference": self.reference, "retention_tag": self.repository + ":catalog-" + self.digest[7:]}
        def index(command, **kwargs):
            events.append("index")
            self.assertEqual(command[1:3], ["tag", self.reference])
            self.assertEqual(command[3:], [registry.input_tag(self.expected, "toolchain-install")])
        with mock.patch.object(component_catalog, "verify", side_effect=verify), mock.patch.object(registry, "pack", side_effect=pack), \
             mock.patch.object(registry, "_publish_packed", side_effect=publish), mock.patch.object(registry.subprocess, "run", side_effect=index), \
             mock.patch.object(registry, "_manifest", return_value=self.raw):
            result = registry.publish(self.root, self.fixture.path, self.fixture.bundle, self.fixture.cosign,
                self.root / "packed", self.repository, self.binary, self.policy)
        self.assertEqual(events, ["verify", "pack", "publish immutable", "index"])
        self.assertEqual(result["authentication"]["producer"], self.fixture.producer)

    def test_catalog_repository_override_is_only_for_explicit_loopback_tests(self):
        with self.assertRaises(IdentityError):
            registry._repository("ghcr.io/another/components", False)
        with self.assertRaises(IdentityError):
            registry._repository(component_catalog.REPOSITORY, True)
        self.assertEqual(registry._repository("127.0.0.1:5000/components", True), "127.0.0.1:5000/components")


if __name__ == "__main__":
    unittest.main()
