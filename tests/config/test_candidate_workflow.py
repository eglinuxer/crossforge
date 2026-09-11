import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]


class CandidateWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (
            REPOSITORY / ".github/workflows/candidate.yml"
        ).read_text(encoding="utf-8")
        cls.ci = (REPOSITORY / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
        cls.quick = (REPOSITORY / ".github/workflows/verify-quick.yml").read_text(encoding="utf-8")
        cls.setup = (
            REPOSITORY / ".github/actions/setup-locked-buildx/action.yml"
        ).read_text(encoding="utf-8")
        cls.attestations = (
            REPOSITORY
            / ".github/actions/validate-public-attestations/action.yml"
        ).read_text(encoding="utf-8")

    def test_candidate_is_explicit_and_main_push_runs_incremental_ci(self):
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertNotIn("pull_request:", self.workflow)
        self.assertNotIn("  push:", self.workflow)
        self.assertIn("  push:\n    branches: [main]", self.ci)
        self.assertIn('test "$GITHUB_REF" = refs/heads/main', self.workflow)
        self.assertIn("uses: ./.github/workflows/verify-quick.yml", self.workflow)
        self.assertIn("plan-components: false", self.workflow)
        qualification = self.workflow.split("  qualify:\n", 1)[1].split("  source-publication:\n", 1)[0]
        self.assertIn("uses: ./.github/workflows/verify-main-incremental.yml", qualification)
        self.assertIn("profile: full", qualification)
        self.assertNotIn("selection:", qualification)
        self.assertNotIn("uses: ./.github/workflows/qualification.yml", self.workflow)
        self.assertIn("    needs: quick\n", self.workflow)
        self.assertIn("uses: ./.github/workflows/verify-quick.yml", self.ci)
        self.assertNotIn("workflow_call:", self.ci)
        self.assertIn("packages: write", self.workflow)
        self.assertIn("python3 scripts/candidate-components.py build", self.workflow)
        self.assertIn('--reference "$CANDIDATE_REFERENCE" --sbom-generator "$SBOM_GENERATOR"', self.workflow)
        self.assertIn('--builder "$COMPONENT_BUILDER" --sha256 "$BINDING_SHA256"', self.workflow)
        self.assertNotIn("--provenance=", self.workflow)
        self.assertNotIn("--sbom=", self.workflow)
        self.assertNotIn("sdk-complete-dev.output", self.workflow)
        self.assertNotIn(":gts15-el8", self.workflow)
        self.assertNotIn(":v0.1.0", self.workflow)

    def test_tag_and_manifest_bind_run_attempt_source_and_both_digests(self):
        for value in (
            '"$GITHUB_SHA"',
            '"$GITHUB_RUN_ID"',
            '"$GITHUB_RUN_ATTEMPT"',
            '"$candidate_digest"',
            '"$platform_digest"',
        ):
            with self.subTest(value=value):
                self.assertIn(value, self.workflow)
        self.assertIn("candidate_manifest.py create", self.workflow)
        self.assertIn("candidate_manifest.py validate", self.workflow)
        self.assertIn("resolve_candidate_image.py buildx-digest", self.workflow)
        self.assertIn("resolve_candidate_image.py platform-digest", self.workflow)
        self.assertIn("--source-bundle-digest", self.workflow)
        self.assertIn("--source-bundle-platform-manifest-digest", self.workflow)
        self.assertIn("--source-bundle-identity", self.workflow)

    def test_complete_source_bundle_is_public_bound_and_signed(self):
        self.assertIn(
            'test -z "$(git status --porcelain --untracked-files=all)"',
            self.workflow,
        )
        self.assertIn('source_tag="source-$tag"', self.workflow)
        self.assertIn("docker buildx bake source-bundle", self.workflow)
        self.assertIn("source-bundle.output=type=image,push=true", self.workflow)
        self.assertIn(
            "source-bundle.attest=type=provenance,mode=max,version=v1",
            self.workflow,
        )
        self.assertIn(
            "source-bundle.attest+=type=sbom,generator=$SBOM_GENERATOR",
            self.workflow,
        )
        self.assertIn("docker buildx bake source-bundle-identity", self.workflow)
        self.assertIn("anonymous-source-index.json", self.workflow)
        self.assertIn(
            "Prove the complete source payload is anonymously retrievable",
            self.workflow,
        )
        self.assertIn("docker create --pull=always", self.workflow)
        self.assertIn("--platform linux/amd64 \"$source_image\" /bin/true", self.workflow)
        self.assertIn('tar -xOf - "$source_archive_file"', self.workflow)
        self.assertIn(
            'test "$observed_source_sha256" = "$SOURCE_ARCHIVE_SHA256"',
            self.workflow,
        )
        source_proof = self.workflow.index(
            "Prove the complete source payload is anonymously retrievable"
        )
        sdk_build = self.workflow.index(
            "Build once and push the source-bound candidate"
        )
        self.assertLess(source_proof, sdk_build)
        self.assertIn('"$cosign" sign --yes "$source_image"', self.workflow)
        self.assertIn("source-bundle-signature.json", self.workflow)
        self.assertNotIn("source-bundle", self.ci)

    def test_publish_digest_commands_select_their_own_bake_target(self):
        digests = {"source-bundle": "sha256:" + "1" * 64,
                   "sdk-candidate": "sha256:" + "2" * 64}
        with tempfile.TemporaryDirectory() as directory:
            for variable, target in (("source_digest", "source-bundle"),
                                     ("candidate_digest", "sdk-candidate")):
                command = re.search(variable + r"=\$\((.*?)\)", self.workflow, re.S)
                self.assertIsNotNone(command)
                arguments = shlex.split(command.group(1).replace("\\\n", ""))
                arguments = [arg.replace("$RUNNER_TEMP", directory) for arg in arguments]
                metadata = Path(arguments[arguments.index("--metadata") + 1])
                # Give both targets distinct identities so a wrong/default
                # target cannot accidentally pass with a shared fixture digest.
                metadata.write_text(json.dumps({
                    name: {"containerimage.digest": digest}
                    for name, digest in digests.items()
                }), encoding="utf-8")
                with self.subTest(target=target):
                    result = subprocess.run(
                        arguments, cwd=REPOSITORY, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), digests[target])
                    for invalid in ({}, {target: {}},
                                    {target: {"containerimage.digest": "latest"}}):
                        metadata.write_text(json.dumps(invalid), encoding="utf-8")
                        result = subprocess.run(
                            arguments, cwd=REPOSITORY, text=True, capture_output=True)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(result.stdout, "")

    def test_public_availability_is_checked_without_registry_credentials(self):
        logout = self.workflow.index("docker logout ghcr.io")
        anonymous = self.workflow.index("anonymous-candidate-index.json")
        upload = self.workflow.index("- name: Upload immutable candidate identity")
        self.assertLess(logout, anonymous)
        self.assertLess(anonymous, upload)
        self.assertIn("if-no-files-found: error", self.workflow)
        self.assertIn("retention-days: 90", self.workflow)

    def test_long_candidate_builds_emit_process_heartbeats(self):
        for label in (
            "source-bundle-publish",
            "sdk-candidate-publish",
        ):
            with self.subTest(label=label):
                self.assertIn(
                    "--label %s --interval 60" % label, self.workflow
                )
        self.assertEqual(
            self.workflow.count("scripts/run-with-heartbeat.py"), 2
        )

    def test_public_consumer_job_preserves_buildkit_diagnostics_after_failure(self):
        publish = self.workflow.split("\n  publish:", 1)[1].split(
            "\n  native-aarch64:", 1
        )[0]
        collect, upload = publish.split(
            "      - name: Collect publish build diagnostics\n", 1
        )[1].split(
            "      - name: Preserve publish build diagnostics\n", 1
        )
        for step in (collect, upload):
            self.assertIn("        if: always()\n", step)
            self.assertNotIn("continue-on-error:", step)
        self.assertIn("./scripts/collect-buildkit-diagnostics.sh", collect)
        self.assertIn('"$RUNNER_TEMP/build-diagnostics/publish"', collect)
        self.assertIn("uses: actions/upload-artifact@", upload)
        self.assertIn("${{ runner.temp }}/build-diagnostics/publish/", upload)
        self.assertIn("${{ github.run_id }}-${{ github.run_attempt }}", upload)
        self.assertLess(
            publish.index("Build downstream consumers through the public launcher"),
            publish.index("Collect publish build diagnostics"),
        )

    def test_public_candidate_runs_non_root_with_a_read_only_sdk(self):
        self.assertIn('docker run --rm --pull=always "$image" id -u', self.workflow)
        self.assertIn(')" = 1000', self.workflow)
        self.assertIn("docker run --rm --read-only", self.workflow)
        self.assertIn("--tmpfs /home/crossforge:rw,uid=1000,gid=1000,mode=0700", self.workflow)
        self.assertIn("crossforge env --target aarch64 --vcpkg --json", self.workflow)
        self.assertIn(
            "! grep -F '/opt/crossforge/vcpkg/root/downloads'", self.workflow
        )
        self.assertIn("cat /opt/crossforge/LICENSES.json", self.workflow)
        self.assertIn(
            '"crossforge-license-file-inventory"', self.workflow
        )
        self.assertIn(
            '"file-inventory-not-legal-conclusion"', self.workflow
        )
        self.assertIn(
            'startswith("/opt/crossforge/share/licenses/crossforge/")',
            self.workflow,
        )

    def test_publication_failure_preserves_full_log_and_original_exit_status(self):
        for label in ("corresponding source bundle", "source-bound candidate"):
            block = self.workflow.split("      - name: Build once and push the " + label + "\n", 1)[1]
            block = block.split("      - name:", 1)[0]
            script = block.split("        run: |\n", 1)[1]
            script = "\n".join(line[10:] for line in script.splitlines())
            # The GitHub expression values are inert fixture identities. The
            # workflow shell and heartbeat are real; the invoked publication
            # entry point (Docker or the checked candidate driver) is substituted.
            script = re.sub(r"\$\{\{ steps\.source\.outputs\.[a-z0-9_]+ \}\}", "fixture", script)
            with self.subTest(publication=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                binary = root / "docker"
                binary.write_text("#!/bin/sh\necho fixture-build-output\necho fixture-frontend-error >&2\nexit 2\n")
                binary.chmod(0o755)
                working = REPOSITORY
                if label == "source-bound candidate":
                    working = root
                    (root / "scripts").mkdir()
                    (root / "scripts/run-with-heartbeat.py").write_bytes((REPOSITORY / "scripts/run-with-heartbeat.py").read_bytes())
                    (root / "scripts/candidate-components.py").write_text(
                        "import sys\nprint('fixture-build-output', flush=True)\n"
                        "print('fixture-frontend-error', file=sys.stderr, flush=True)\nsys.exit(2)\n")
                result = subprocess.run(["bash", "-c", script], cwd=working,
                    env={**os.environ, "PATH": directory + ":" + os.environ["PATH"],
                         "RUNNER_TEMP": directory, "GITHUB_SHA": "a" * 40,
                         "SOURCE_REFERENCE": "fixture/source", "CANDIDATE_REFERENCE": "fixture/sdk",
                         "SBOM_GENERATOR": "fixture/generator", "COMPONENT_BUILDER": "fixture-builder", "BINDING_SHA256": "a" * 64},
                    text=True, capture_output=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual((root / "candidate-build-diagnostics/build.log").read_text(),
                                 "fixture-build-output\nfixture-frontend-error\n")
                self.assertFalse((root / "source-binding.json").exists())
                self.assertFalse((root / "candidate.json").exists())

    def test_each_publication_collects_and_uploads_diagnostics_after_failure(self):
        for phase, next_job, metadata in (("source", "sdk-publication", "source-build-metadata.json"),
                                          ("sdk", "publish", "build-metadata.json")):
            block = self.workflow.split("  " + phase + "-publication:\n", 1)[1].split("  " + next_job + ":\n", 1)[0]
            with self.subTest(phase=phase):
                self.assertIn('      - name: Export publication BuildKit diagnostics\n        if: always()\n'
                              '        run: ./scripts/collect-buildkit-diagnostics.sh "$RUNNER_TEMP/candidate-build-diagnostics"', block)
                self.assertIn("      - name: Preserve " + phase + " publication build diagnostics\n        if: always()", block)
                self.assertIn("name: candidate-" + phase + "-build-${{ github.run_id }}-${{ github.run_attempt }}", block)
                self.assertIn("${{ runner.temp }}/" + metadata, block)
                self.assertLess(block.index("Export publication BuildKit diagnostics"),
                                block.index("Preserve " + phase + " publication build diagnostics"))

    def test_public_launcher_builds_real_downstream_consumers(self):
        self.assertIn(
            "Build downstream consumers through the public launcher",
            self.workflow,
        )
        self.assertIn(
            "src=$GITHUB_WORKSPACE/tests/consumer,dst=/source,readonly",
            self.workflow,
        )
        self.assertIn("for target in x86_64 aarch64", self.workflow)
        self.assertIn('crossforge run --target "$target" --', self.workflow)
        self.assertIn("cmake -S /source -B", self.workflow)
        self.assertIn('cmake --build "$build" --verbose', self.workflow)
        self.assertIn("$triple-readelf", self.workflow)

    def test_public_attestations_are_downloaded_and_semantically_validated(self):
        self.assertIn("Validate public source provenance and SBOM", self.workflow)
        self.assertIn("Validate public SDK provenance and SBOM", self.workflow)
        self.assertEqual(
            self.workflow.count(
                "uses: ./.github/actions/validate-public-attestations"
            ),
            2,
        )
        self.assertIn("https://slsa.dev/provenance/v1", self.attestations)
        self.assertIn("https://spdx.dev/Document", self.attestations)
        self.assertIn("scripts/image_attestations.py", self.attestations)
        self.assertIn("blobs/$provenance_digest", self.attestations)
        self.assertIn("blobs/$sbom_digest", self.attestations)
        self.assertIn("source-attestations.json", self.workflow)
        self.assertIn("sdk-attestations.json", self.workflow)
        self.assertIn(".sbom.generator.repository", self.workflow)
        self.assertIn(".sbom.generator.digest", self.workflow)
        self.assertIn(".sbom.generator.manifest_digest", self.workflow)
        self.assertIn("sbom-generator-index.json", self.workflow)

    def test_native_arm_gate_consumes_the_exact_candidate_and_pinned_runtime(self):
        self.assertIn("runs-on: ubuntu-24.04-arm", self.workflow)
        self.assertIn('test "$RUNNER_ARCH" = ARM64', self.workflow)
        self.assertIn('test "$host_machine" = aarch64', self.workflow)
        self.assertIn(
            "CANDIDATE_DIGEST: ${{ needs.publish.outputs.candidate_digest }}",
            self.workflow,
        )
        self.assertIn(
            "EXPECTED_BUNDLE_SHA256: ${{ needs.publish.outputs.probe_bundle_sha256 }}",
            self.workflow,
        )
        self.assertIn("qualify-toolchain.py", self.workflow)
        self.assertIn("--skip-sysroot-execution", self.workflow)
        self.assertIn(
            "docker run --rm --pull=always --network none --read-only",
            self.workflow,
        )
        self.assertIn('sudo chown 1000:1000 "$probe_root"', self.workflow)
        self.assertNotIn('install -d -m 0777 "$probe_root"', self.workflow)
        self.assertIn("native-aarch64-release.py bundle", self.workflow)
        self.assertIn("native-aarch64-release.py execute", self.workflow)
        self.assertIn("native-aarch64-release.py validate", self.workflow)
        self.assertIn("docker pull --platform linux/arm64", self.workflow)
        self.assertIn("--pull=never --platform linux/arm64", self.workflow)
        self.assertIn("--network none --read-only", self.workflow)
        self.assertIn("needs: [publish]", self.workflow)
        self.assertIn("needs: [publish, native-aarch64]", self.workflow)
        for forbidden in ("qt-native-input", "run-qt-target-runtime.py",
                          "qt-native-aarch64-runtime.json"):
            self.assertNotIn(forbidden, self.workflow)

    def test_native_release_evidence_is_staged_under_one_artifact_root(self):
        self.assertIn(
            'EVIDENCE_ROOT: ${{ runner.temp }}/native-aarch64-release-evidence',
            self.workflow,
        )
        self.assertIn(
            'install -m 0644 "$file" "$EVIDENCE_ROOT/$(basename "$file")"',
            self.workflow,
        )
        self.assertIn(
            "path: ${{ runner.temp }}/native-aarch64-release-evidence/",
            self.workflow,
        )
        upload = self.workflow.split(
            "- name: Upload native AArch64 release evidence", 1
        )[1].split("- name: Report native AArch64 qualification", 1)[0]
        self.assertNotIn("native-aarch64-input/candidate.json", upload)
        self.assertNotIn("native-aarch64-output/native-aarch64.json", upload)

    def test_public_identity_and_signature_artifacts_have_flat_layouts(self):
        for root, count in (
            ("candidate-identity", 7),
            ("candidate-signature-evidence", 5),
        ):
            with self.subTest(root=root):
                self.assertIn("${{ runner.temp }}/%s/" % root, self.workflow)
                self.assertIn('" -eq %d' % count, self.workflow)
        self.assertIn('"$IDENTITY_ROOT/source-bundle.json"', self.workflow)
        self.assertIn('"$SIGNATURE_ROOT/$(basename "$file")"', self.workflow)

    def test_every_ci_and_candidate_job_uses_the_locked_buildx_setup(self):
        local_action = "uses: ./.github/actions/setup-locked-buildx"
        self.assertEqual(self.quick.count(local_action), 1)
        self.assertEqual(self.workflow.count(local_action), 4)
        self.assertIn("buildx-v0.36.1.linux-amd64", self.setup)
        self.assertIn("--retry 5 --retry-all-errors", self.setup)
        self.assertIn("--retry-delay 2 --connect-timeout 30", self.setup)
        self.assertIn(
            "48af8a397ebd60178778bf63611dbcebe5f5e7a9be90eb9147b24b9587455778",
            self.setup,
        )
        self.assertIn(
            "moby/buildkit:v0.33.0@sha256:6c2fa84a6b61ccd72899dde4239f8d5717f05f9a8ca6f3cad185fb1a95a94de3",
            self.setup,
        )
        self.assertIn(
            "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c",
            self.setup,
        )

    def test_candidate_qualification_is_not_cancelled_by_development_pushes(self):
        self.assertIn(
            "cancel-in-progress: ${{ github.event_name == 'pull_request' }}",
            self.ci,
        )
        self.assertIn("cancel-in-progress: false", self.workflow)
        self.assertIn("group: candidate-${{ github.ref }}-${{ github.sha }}", self.workflow)

    def test_qt_runtime_probe_is_compiled_with_strict_warnings(self):
        self.assertIn(
            "gcc -fsyntax-only -Wall -Wextra -Werror "
            "scripts/qt-plugin-probe.c",
            self.quick,
        )


if __name__ == "__main__":
    unittest.main()
