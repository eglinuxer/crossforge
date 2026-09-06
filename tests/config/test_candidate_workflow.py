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
        cls.setup = (
            REPOSITORY / ".github/actions/setup-locked-buildx/action.yml"
        ).read_text(encoding="utf-8")
        cls.attestations = (
            REPOSITORY
            / ".github/actions/validate-public-attestations/action.yml"
        ).read_text(encoding="utf-8")

    def test_candidate_is_manual_public_digest_only_output(self):
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertNotIn("pull_request:", self.workflow)
        self.assertNotIn("push:\n", self.workflow)
        self.assertIn("packages: write", self.workflow)
        self.assertIn("sdk-candidate.output=type=image,push=true", self.workflow)
        self.assertIn(
            "sdk-candidate.attest=type=provenance,mode=max,version=v1",
            self.workflow,
        )
        self.assertIn(
            "sdk-candidate.attest+=type=sbom,generator=$SBOM_GENERATOR",
            self.workflow,
        )
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

    def test_public_availability_is_checked_without_registry_credentials(self):
        logout = self.workflow.index("docker logout ghcr.io")
        anonymous = self.workflow.index("anonymous-candidate-index.json")
        upload = self.workflow.index("actions/upload-artifact@")
        self.assertLess(logout, anonymous)
        self.assertLess(anonymous, upload)
        self.assertIn("if-no-files-found: error", self.workflow)
        self.assertIn("retention-days: 90", self.workflow)

    def test_long_candidate_builds_emit_process_heartbeats(self):
        for label in (
            "source-bundle-publish",
            "sdk-candidate-publish",
            "qt-aarch64-native-runtime-root",
        ):
            with self.subTest(label=label):
                self.assertIn(
                    "--label %s --interval 60" % label, self.workflow
                )
        self.assertEqual(
            self.workflow.count("scripts/run-with-heartbeat.py"), 3
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
        self.assertIn("needs: [publish, qt-native-input]", self.workflow)
        qt_job = self.workflow.split("\n  qt-native-input:\n", 1)[1].split(
            "\n  native-aarch64:\n", 1
        )[0]
        self.assertIn("permissions:\n      contents: read", qt_job)
        self.assertNotIn("packages: write", qt_job)
        self.assertIn("qt-aarch64-native-runtime-root", self.workflow)
        self.assertIn(
            "qt-aarch64-native-runtime-root.output=type=tar,dest=$archive",
            self.workflow,
        )
        self.assertIn("tar -tf \"$archive\"", self.workflow)
        self.assertIn("qt-target-build.json", self.workflow)
        self.assertIn("qt-runtime-overlay.json", self.workflow)
        self.assertIn("(\\./)?opt/crossforge-qualification", self.workflow)
        self.assertIn("(\\./)?\\.crossforge/qemu-aarch64", self.workflow)
        self.assertIn("! grep -E -x", self.workflow)
        self.assertIn("qemu-aarch64", self.workflow)
        self.assertIn("compression-level: 0", self.workflow)
        self.assertIn(
            "EXPECTED_QT_ROOTFS_SHA256: "
            "${{ needs.qt-native-input.outputs.rootfs_sha256 }}",
            self.workflow,
        )
        self.assertGreaterEqual(
            self.workflow.count(
                "CANDIDATE_DIGEST: ${{ needs.publish.outputs.candidate_digest }}"
            ),
            2,
        )
        self.assertGreaterEqual(
            self.workflow.count(
                'test "$(jq -r .digest "$candidate")" = "$CANDIDATE_DIGEST"'
            ),
            2,
        )
        self.assertIn(
            "docker image import --platform linux/arm64", self.workflow
        )
        self.assertIn("run-qt-target-runtime.py", self.workflow)
        self.assertIn("--native-release", self.workflow)
        self.assertIn("--candidate /input/candidate.json", self.workflow)
        self.assertIn("--expected-source-commit \"$GITHUB_SHA\"", self.workflow)
        self.assertIn(
            "--input-rootfs-sha256 \"$EXPECTED_QT_ROOTFS_SHA256\"",
            self.workflow,
        )
        self.assertIn("qt-native-aarch64-runtime.json", self.workflow)
        self.assertIn(
            "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",
            self.workflow,
        )

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
            ("candidate-identity", 6),
            ("candidate-signature-evidence", 4),
        ):
            with self.subTest(root=root):
                self.assertIn("${{ runner.temp }}/%s/" % root, self.workflow)
                self.assertIn('" -eq %d' % count, self.workflow)
        self.assertIn('"$IDENTITY_ROOT/source-bundle.json"', self.workflow)
        self.assertIn('"$SIGNATURE_ROOT/$(basename "$file")"', self.workflow)

    def test_every_ci_and_candidate_job_uses_the_locked_buildx_setup(self):
        local_action = "uses: ./.github/actions/setup-locked-buildx"
        self.assertEqual(self.ci.count(local_action), 2)
        self.assertEqual(self.workflow.count(local_action), 3)
        self.assertIn("buildx-v0.36.1.linux-amd64", self.setup)
        self.assertIn("--retry 5 --retry-all-errors", self.setup)
        self.assertIn("--retry-delay 2 --connect-timeout 30", self.setup)
        self.assertIn(
            "48af8a397ebd60178778bf63611dbcebe5f5e7a9be90eb9147b24b9587455778",
            self.setup,
        )
        self.assertIn(
            "moby/buildkit:v0.32.2@sha256:28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8",
            self.setup,
        )
        self.assertIn(
            "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c",
            self.setup,
        )

    def test_main_qualification_is_not_cancelled_by_a_later_push(self):
        self.assertIn(
            "cancel-in-progress: ${{ github.event_name == 'pull_request' }}",
            self.ci,
        )
        self.assertIn("cancel-in-progress: true", self.workflow)

    def test_qt_runtime_probe_is_compiled_with_strict_warnings(self):
        self.assertIn(
            "gcc -fsyntax-only -Wall -Wextra -Werror "
            "scripts/qt-plugin-probe.c",
            self.ci,
        )


if __name__ == "__main__":
    unittest.main()
