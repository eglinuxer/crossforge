"""The linked-check workaround must preserve inputs and reject unsafe rewrites."""

import copy
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import bake_check
    from crossforge_internal.identity import IdentityError
finally:
    sys.path.pop(0)


class BakeCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "check"
        self.output.mkdir()
        (self.root / ".dockerignore").write_text("ignored\n")
        self.file = self.root / "Dockerfile"
        self.source = ("# syntax=docker/dockerfile:1@sha256:" + "a" * 64 + "\n"
            "FROM rocky AS build\nARG BUILD_PIN\nRUN false\n"
            "FROM scratch AS exported\nCOPY --from=build /out /out\n"
            "FROM linked AS imported\nFROM imported AS consumer\nARG CHECK_PIN\nRUN false\n")
        self.file.write_text(self.source)
        (self.root / "Dockerfile.dockerignore").write_text("specific-ignore\n")
        common = {"context": ".", "dockerfile": "Dockerfile", "platforms": ["linux/amd64"],
                  "contexts": {"rocky": "docker-image://rocky@sha256:" + "b" * 64}}
        provider, consumer = copy.deepcopy(common), copy.deepcopy(common)
        provider.update(target="exported", args={"BUILD_PIN": "build"})
        consumer.update(target="consumer", args={"CHECK_PIN": "check"})
        consumer["contexts"]["linked"] = "target:provider"
        self.graph = {"group": {"default": {"targets": ["consumer"]}},
                      "target": {"provider": provider, "consumer": consumer}}

    def prepare(self):
        return bake_check.prepare(self.root, self.graph, self.output)

    def test_reconnects_original_export_with_all_targets_args_lines_and_ignore_rules(self):
        before = copy.deepcopy(self.graph)
        result = self.prepare()
        self.assertEqual(self.graph, before)
        self.assertEqual(self.file.read_text(), self.source)
        self.assertEqual(set(result["target"]), {"provider", "consumer"})
        self.assertEqual(result["target"]["provider"], before["target"]["provider"])
        consumer = result["target"]["consumer"]
        self.assertEqual(consumer["args"], {"BUILD_PIN": "build", "CHECK_PIN": "check"})
        self.assertNotIn("linked", consumer["contexts"])
        rewritten = Path(consumer["dockerfile"])
        self.assertEqual(rewritten.read_text(), self.source.replace("FROM linked AS", "FROM exported AS"))
        self.assertEqual(Path(str(rewritten) + ".dockerignore").read_text(), "specific-ignore\n")

    def test_missing_argument_cannot_override_consumer_or_provider_default(self):
        for replacement in (
                self.source.replace("ARG CHECK_PIN", "ARG BUILD_PIN=consumer-default\nARG CHECK_PIN"),
                self.source.replace("ARG BUILD_PIN", "ARG CHECK_PIN=provider-default\nARG BUILD_PIN")):
            with self.subTest(source=replacement):
                self.file.write_text(replacement)
                with self.assertRaisesRegex(IdentityError, "change a linked source closure"):
                    self.prepare()

    def test_special_implicit_build_arguments_cannot_be_added_to_other_target(self):
        for key in ("SOURCE_DATE_EPOCH", "HTTPS_PROXY", "BUILDKIT_SANDBOX_HOSTNAME"):
            with self.subTest(key=key):
                self.graph["target"]["provider"]["args"][key] = "changed"
                with self.assertRaisesRegex(IdentityError, "change a linked source closure"):
                    self.prepare()
                del self.graph["target"]["provider"]["args"][key]

    def test_conflicting_arguments_contexts_and_execution_options_are_rejected(self):
        original = copy.deepcopy(self.graph)
        for field, value in (("args", {"CHECK_PIN": "other"}),
                             ("contexts", {"rocky": "docker-image://rocky@sha256:" + "c" * 64}),
                             ("platforms", ["linux/arm64"]), ("context", "different")):
            with self.subTest(field=field):
                self.graph = copy.deepcopy(original)
                self.graph["target"]["provider"][field] = value
                with self.assertRaises(IdentityError):
                    self.prepare()

    def test_image_metadata_and_non_alias_links_are_rejected(self):
        for source in (
                self.source.replace("FROM scratch AS exported", "FROM build AS exported"),
                self.source.replace("COPY --from=build /out /out", "COPY --from=build /out /out\nENV PIN=value"),
                self.source.replace("FROM linked AS imported", "FROM --platform=linux/amd64 linked AS imported"),
                self.source.replace("ARG CHECK_PIN", "COPY --from=linked /out /again\nARG CHECK_PIN"),
                self.source.replace("ARG CHECK_PIN", "RUN --mount=from=linked,target=/input true\nARG CHECK_PIN")):
            with self.subTest(source=source):
                self.file.write_text(source)
                with self.assertRaises(IdentityError):
                    self.prepare()

    def test_forward_local_alias_is_rejected(self):
        self.file.write_text(self.source.replace("FROM scratch AS exported\nCOPY --from=build /out /out\n", "") +
                             "FROM scratch AS exported\nCOPY --from=build /out /out\n")
        with self.assertRaisesRegex(IdentityError, "precede"):
            self.prepare()

    def test_nested_links_and_unknown_bake_execution_fields_are_rejected(self):
        self.graph["target"]["provider"]["contexts"]["unused"] = "target:consumer"
        with self.assertRaisesRegex(IdentityError, "nested"):
            self.prepare()
        del self.graph["target"]["provider"]["contexts"]["unused"]
        self.graph["target"]["provider"]["network"] = "host"
        with self.assertRaisesRegex(IdentityError, "unsupported Bake target fields"):
            self.prepare()


if __name__ == "__main__":
    unittest.main()
