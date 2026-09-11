"""Candidate freshness covers its real graph, including shared RUN aliases."""

import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock

import test_candidate_components as fixtures

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from crossforge_internal import candidate_qualification as qualification, ci_replay
from crossforge_internal.identity import IdentityError, content_sha256, load_json


def events_for(selected):
    events = []
    for target, stages in sorted(selected["owners"].items()):
        for stage, settings in sorted(stages.items()):
            for index in range(settings["runs"]):
                name = "[%s %s %d/%d] RUN check %s" % (target, stage, index + 1, settings["runs"], settings["marker"] or "")
                events.append({"digest": "sha256:" + hashlib.sha256(name.encode()).hexdigest(), "name": name,
                    "started": "2026-09-11T00:00:01Z", "completed": "2026-09-11T00:00:02Z"})
    return events


def publication_fixture(directory, owner, binding_path, image_digest):
    """Synthetic execution over real captured recipes; no build is claimed."""
    fixture = fixtures.CandidateComponentGraphTests()
    fixture.setUpClass()
    fixture.setUp()
    try:
        fixture.binding = binding_path
        with fixture.patches():
            ready = fixture.prepare()
        inputs, selected = ready["inputs"], ready["qualification"]
        write = lambda name, value: (directory / name).write_text(json.dumps(value) + "\n")
        write("component-selection.json", ready["selection"])
        write(qualification.FILES[1], inputs)
        write(qualification.FILES[2], selected)
        (directory / qualification.FILES[3]).write_text("".join(json.dumps({"vertexes": [event]}) + "\n"
            for event in events_for(selected)))
        start, end = "2026-09-11T00:00:00Z", "2026-09-11T00:00:03Z"
        write(qualification.FILES[0], {"schema_version": 1, "kind": "crossforge-candidate-qualification-execution",
            "status": "passed", "producer": owner, "image_digest": image_digest,
            "inputs_sha256": content_sha256(inputs), "source_binding_sha256": ready["source_binding_sha256"],
            "component_selection_sha256": content_sha256(ready["selection"]), "execution": ready["qualification_execution"],
            "started_at": start, "completed_at": end,
            "vertices": qualification.fresh_vertices(directory / qualification.FILES[3], selected, start, end)})
    finally:
        fixture.doCleanups()


class CandidateQualificationGraphTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CandidateComponentGraphTests()
        self.fixture.setUpClass()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_canonical_candidate_includes_all_qualification_owners_before_publication(self):
        with self.fixture.patches():
            ready = self.fixture.prepare()
        graph = load_json(self.fixture.directory / "components.bake.json")
        selected = qualification.plan(ROOT, ready["inputs"])
        self.assertIn("gcc-testsuite-smoke-evidence", selected["owners"])
        self.assertIn("gcc-testsuite-full-qualification-evidence", selected["owners"])
        self.assertEqual(set(selected["owners"]["sdk-candidate"]), {"sdk-complete-dev", "sdk-candidate"})
        for row in ("cp39", "cp310", "cp311", "cp312", "cp313", "cp314"):
            self.assertIn("python-row-" + row, selected["owners"])
            self.assertEqual(selected["owners"]["python-dev-append-" + row]["python-sdk-append"],
                             {"runs": 2, "marker": '--row "' + row + '"'})
        for tier in (1, 2, 3):
            self.assertIn("vcpkg-upstream-tier%d-qualified" % tier, selected["owners"])
        original = copy.deepcopy(graph)
        forced = qualification.override(graph, selected)
        for target, definition in forced["target"].items():
            self.assertEqual(definition["no-cache-filter"], sorted(selected["owners"].get(target, {})))
            for key in ("args", "contexts", "cache-from", "output", "tags", "attest"):
                self.assertEqual(definition.get(key), original["target"][target].get(key))
        self.assertEqual(graph, original)
        for target in selected["owners"]:
            incomplete = copy.deepcopy(ready["inputs"])
            del incomplete["parameters"]["recipes"][target]
            with self.subTest(target=target), self.assertRaisesRegex(IdentityError, "omits a qualification owner"):
                qualification.plan(ROOT, incomplete)


class CandidateQualificationEventsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "execution.jsonl"
        self.start, self.end = "2026-09-11T00:00:00Z", "2026-09-11T00:00:10Z"
        self.plan = {"schema_version": 1, "kind": "crossforge-candidate-qualification-plan", "owners": {
            "full": {"base": {"runs": 1, "marker": None}},
            "smoke": {"base": {"runs": 1, "marker": None}}}}
        self.events = [{"digest": "sha256:" + "a" * 64, "name": "[%s base 1/1] RUN prepare" % owner,
                        "started": "2026-09-11T00:00:01Z", "completed": "2026-09-11T00:00:02Z"}
                       for owner in ("full", "smoke")]

    def verify(self, events):
        self.path.write_text("".join(json.dumps({"vertexes": [event]}) + "\n" for event in events))
        return qualification.fresh_vertices(self.path, self.plan, self.start, self.end)

    def test_shared_base_is_one_physical_run_with_both_independently_verified_owners(self):
        value = self.verify(self.events)
        self.assertEqual(len(value), 1)
        self.assertEqual({owner["target"] for owner in value[0]["owners"]}, {"full", "smoke"})
        # Other replay callers retain their stricter non-sharing contract.
        with self.assertRaisesRegex(IdentityError, "multiple targets"):
            ci_replay.fresh_vertices(self.path, self.plan["owners"], self.start, self.end)

    def test_missing_stale_cached_or_failed_owner_is_not_hidden_by_a_fresh_alias(self):
        cases = [self.events[:1], self.events[1:]]
        for key, value in (("cached", True), ("error", "failed"), ("completed", "2026-09-10T00:00:02Z")):
            cases.append([self.events[0], dict(self.events[1], **{key: value})])
        cases.append(self.events + [dict(self.events[0], name="[other base 1/1] RUN prepare", cached=True)])
        for events in cases:
            with self.subTest(events=events), self.assertRaises(IdentityError):
                self.verify(events)

    def test_a_shared_digest_cannot_cover_different_commands(self):
        with self.assertRaisesRegex(IdentityError, "different stage or instructions"):
            self.verify([self.events[0], dict(self.events[1], name="[smoke base 1/1] RUN different")])


class CandidateExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CandidateComponentGraphTests()
        self.fixture.setUpClass()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def run_build(self, fail=False, cached=False, wrong_reference=False):
        fixture = self.fixture
        with fixture.patches():
            ready = fixture.prepare()
            output = fixture.root / "output"
            output.mkdir()
            release = load_json(ROOT / "config/release.json")
            manifest = runpy.run_path(str(ROOT / "scripts/candidate_manifest.py"))
            environment = fixture.fixture.environment(attempt=2)
            reference = release["product"]["image_repository"] + ":" + manifest["candidate_tag"](
                release, ready["source_commit"], environment["GITHUB_RUN_ID"], environment["GITHUB_RUN_ATTEMPT"])
            generator = release["sbom"]["generator"]["repository"] + "@" + release["sbom"]["generator"]["digest"]
            image_digest = "sha256:" + "f" * 64
            commands = []

            def execute(command, **options):
                commands.append(command)
                self.assertIn("--progress=rawjson", command)
                self.assertIn("sdk-candidate.output=type=image,push=true", command)
                self.assertIn("sdk-candidate.attest=type=provenance,mode=max,version=v1", command)
                self.assertIn("sdk-candidate.attest+=type=sbom,generator=" + generator, command)
                self.assertEqual(command[command.index("-f") + 1], str(fixture.directory / "components.bake.json"))
                if fail:
                    raise qualification.subprocess.CalledProcessError(23, command)
                events = events_for(ready["qualification"])
                if cached:
                    events[0]["cached"] = True
                for event in events:
                    options["stderr"].write(json.dumps({"vertexes": [event]}) + "\n")
                (output / "build-metadata.json").write_text(json.dumps({"sdk-candidate": {"containerimage.digest": image_digest}}))

            with mock.patch.dict(os.environ, environment), mock.patch.object(qualification.subprocess, "run", side_effect=execute), \
                    mock.patch.object(qualification, "datetime") as clock:
                clock.utcnow.side_effect = [datetime(2026, 9, 11), datetime(2026, 9, 11, 0, 0, 3)]
                if fail or cached or wrong_reference:
                    with self.assertRaises((IdentityError, qualification.subprocess.CalledProcessError)):
                        qualification.execute(ROOT, fixture.binding, fixture.directory, content_sha256(ready), "fixture", output,
                            reference + "-other" if wrong_reference else reference, generator)
                    self.assertFalse((output / qualification.FILES[0]).exists())
                    self.assertEqual(len(commands), 0 if wrong_reference else 1)
                else:
                    result = qualification.execute(ROOT, fixture.binding, fixture.directory, content_sha256(ready), "fixture", output,
                        reference, generator)
                    self.assertEqual(len(commands), 1)
                    verified = qualification.verify(ROOT, output, result["producer"], image_digest, ready["selection"],
                        load_json(fixture.binding))
                    self.assertEqual(verified, result)
                    self.assertEqual(result["execution"], ready["qualification_execution"])

    def test_complete_build_binds_current_execution_to_the_emitted_image_digest(self):
        self.run_build()

    def test_failed_build_never_writes_successful_qualification(self):
        self.run_build(fail=True)

    def test_cached_build_is_rejected_before_qualification_can_be_checkpointed(self):
        self.run_build(cached=True)

    def test_wrong_candidate_reference_is_rejected_before_building(self):
        self.run_build(wrong_reference=True)

    def test_cli_preserves_the_original_build_failure_status(self):
        cli = runpy.run_path(str(ROOT / "scripts/candidate-components.py"))
        with mock.patch.object(qualification, "execute", side_effect=qualification.subprocess.CalledProcessError(23, ["build"])):
            self.assertEqual(cli["main"](["build", "--source-binding", "/binding", "--directory", "/prepared",
                "--builder", "fixture", "--sha256", "a" * 64, "--output-root", "/output",
                "--reference", "fixture:tag", "--sbom-generator", "fixture@sha256:" + "b" * 64]), 23)


if __name__ == "__main__":
    unittest.main()
