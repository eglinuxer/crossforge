import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import local_sdk, oci_layout
    from crossforge_internal.identity import canonical_bytes, content_sha256, IdentityError
finally:
    sys.path.pop(0)


class LocalSdkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "diagnostics"
        self.directory.mkdir()
        self.rows = ["cp39", "cp310"]
        definitions = {"base": {}, "row-cp39": {}, "row-cp310": {}}
        previous = "base"
        for row in self.rows:
            target = "python-dev-append-" + row
            definitions[target] = {"contexts": {
                "crossforge_sdk_base": "target:" + previous,
                "crossforge_python_row": "target:row-" + row}}
            previous = target
        definitions["python-dev"] = {"contexts": {"crossforge_sdk_base": "target:" + previous}}
        definitions["sdk-complete-dev"] = {"contexts": {"python": "target:python-dev"}}
        self.graph = {"target": definitions}
        self.cache = {"target": {name: {
            "cache-from": [{"type": "registry", "ref": "ghcr.io/test/cache:main-" + name}],
            "cache-to": [{"type": "registry", "ref": "ghcr.io/test/cache:main-" + name}]
                if name in ("python-dev", "sdk-complete-dev") else []}
            for name in definitions}}
        self.calls = []
        self.layouts = []
        self.stack = mock.patch.object(local_sdk.python_sdk, "validate_graph", return_value=self.rows)
        self.stack.start()
        self.addCleanup(self.stack.stop)
        self.source = mock.patch.object(local_sdk, "snapshot", return_value="1" * 64)
        self.source.start()
        self.addCleanup(self.source.stop)

    def export(self, layout):
        (layout / "blobs/sha256").mkdir(parents=True)
        (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')

        def blob(value, media):
            data = canonical_bytes(value)
            digest = content_sha256(value)
            (layout / "blobs/sha256" / digest).write_bytes(data)
            return {"mediaType": media, "digest": "sha256:" + digest, "size": len(data)}

        config = blob({"os": "linux", "architecture": "amd64",
                       "rootfs": {"type": "layers", "diff_ids": []}}, oci_layout.CONFIG)
        manifest = blob({"schemaVersion": 2, "mediaType": oci_layout.MANIFEST,
                         "config": config, "layers": []}, oci_layout.MANIFEST)
        (layout / "index.json").write_bytes(canonical_bytes({"schemaVersion": 2,
            "mediaType": oci_layout.INDEX, "manifests": [manifest]}))
        return manifest["digest"]

    def solve(self, target, recipe, metadata, data):
        selected = json.loads(recipe.read_text())
        self.calls.append((target, selected))
        if self.layouts:
            self.assertTrue(self.layouts[-1].is_dir())
            expected = "oci-layout://" + str(self.layouts[-1]) + "@sha256:"
            self.assertTrue(any(value.startswith(expected) for value in
                selected["target"][target]["contexts"].values()))
        for name, definition in selected["target"].items():
            self.assertEqual(definition["tags"], [])
            if name != target:
                self.assertEqual(definition["output"], [{"type": "cacheonly"}])
                self.assertEqual(definition["cache-to"], [])
        result = {"buildx.build.ref": "test/builder/" + target}
        output = selected["target"][target]["output"][0]
        if output["type"] == "oci":
            layout = Path(output["dest"])
            self.assertEqual(layout.parent, data)
            result["containerimage.digest"] = self.export(layout)
            self.layouts.append(layout)
        metadata.write_text(json.dumps({target: result}))
        return 0

    def execute(self, solve=None, roots=None):
        return local_sdk.execute(ROOT, self.graph, roots or ["python-dev", "sdk-complete-dev"],
                                 self.cache, self.directory, solve or self.solve)

    def result(self):
        return json.loads((self.directory / "local-sdk/result.json").read_text())

    def test_same_run_handoffs_preserve_all_gates_and_release_old_layouts(self):
        original = copy.deepcopy(self.graph)
        self.assertEqual(self.execute(), 0)
        self.assertEqual([name for name, _ in self.calls], [
            "python-dev-append-cp39", "python-dev-append-cp310", "python-dev", "sdk-complete-dev"])
        self.assertEqual(self.graph, original)
        self.assertTrue(all(not path.exists() for path in self.layouts))
        result = self.result()
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["qualification_receipt"])
        self.assertFalse(Path(result["data_directory"]).exists())
        for record in result["steps"][:-1]:
            self.assertEqual(record["oci"]["platform"], "linux/amd64")

    def test_python_only_selection_runs_final_gate_without_complete_sdk(self):
        self.assertEqual(self.execute(roots=["python-dev"]), 0)
        self.assertEqual(self.calls[-1][0], "python-dev")
        self.assertEqual(len(self.calls), 3)

    def test_failed_consumer_stops_and_retains_original_checkpoint(self):
        def solve(*args):
            if self.calls:
                return 23
            return self.solve(*args)

        self.assertEqual(self.execute(solve), 23)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.layouts[0].is_dir())
        self.assertEqual(self.result()["exit_code"], 23)
        self.assertEqual(self.result()["steps"][-1]["exit_code"], 23)

    def test_zero_exit_without_metadata_does_not_supply_a_checkpoint(self):
        with self.assertRaises((IdentityError, OSError)):
            self.execute(lambda *args: 0)
        self.assertEqual(self.result()["exit_code"], 1)
        self.assertEqual(len(self.result()["steps"]), 1)

    def test_missing_wrong_digest_and_tampered_oci_are_rejected(self):
        for mutation in ("digest", "blob", "target"):
            with self.subTest(mutation=mutation):
                child = self.directory / mutation
                child.mkdir()
                self.calls, self.layouts = [], []

                def solve(target, recipe, metadata, data):
                    self.solve(target, recipe, metadata, data)
                    value = json.loads(metadata.read_text())
                    if mutation == "digest":
                        value[target]["containerimage.digest"] = "sha256:" + "0" * 64
                    elif mutation == "target":
                        value = {"unrelated": value[target]}
                    else:
                        blob = self.layouts[-1] / "blobs/sha256" / value[target]["containerimage.digest"].split(":")[1]
                        blob.write_text("corrupt")
                    metadata.write_text(json.dumps(value))
                    return 0

                with self.assertRaises((IdentityError, OSError)):
                    local_sdk.execute(ROOT, self.graph, ["sdk-complete-dev"],
                                      self.cache, child, solve)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(json.loads((child / "local-sdk/result.json").read_text())["exit_code"], 1)

    def test_source_mutation_after_build_prevents_handoff(self):
        with mock.patch.object(local_sdk, "snapshot", side_effect=["1" * 64, "1" * 64, "2" * 64]):
            with self.assertRaisesRegex(IdentityError, "source inputs changed"):
                self.execute()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.result()["exit_code"], 1)

    def test_invalid_roots_and_broken_consumer_edge_fail_closed(self):
        for roots in ([], ["python-dev", "python-dev"], ["sdk-candidate"]):
            with self.subTest(roots=roots), self.assertRaises(IdentityError):
                local_sdk.plan(ROOT, self.graph, roots)
        with self.assertRaisesRegex(IdentityError, "no canonical consumer"):
            local_sdk.solve_graph(self.graph, self.cache, "python-dev",
                                  {"target": "missing", "reference": "not-used"}, None)


if __name__ == "__main__":
    unittest.main()
