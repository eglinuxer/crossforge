"""Main can read components without granting registry credentials to PR jobs."""

import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import ci_execution
finally:
    sys.path.pop(0)


def job(workflow, name):
    return re.split(r"\n  [a-z][a-z0-9-]*:\n", workflow.split("\n  " + name + ":\n", 1)[1], 1)[0]


class ComponentRoutingTests(unittest.TestCase):
    def environments(self):
        main = {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "eglinuxer/crossforge",
                "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "push"}
        return [(main, True), (dict(main, GITHUB_EVENT_NAME="workflow_dispatch"), True)] + [
            (dict(main, **{key: value}), False) for key, value in (
                ("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_EVENT_NAME", "pull_request_target"),
                ("GITHUB_EVENT_NAME", "schedule"), ("GITHUB_REF", "refs/pull/1/merge"),
                ("GITHUB_REF", "refs/heads/feature"), ("GITHUB_REF", "refs/tags/v1"),
                ("GITHUB_REPOSITORY", "fork/crossforge"), ("GITHUB_SERVER_URL", "https://example.invalid"))]

    def results(self, allowed):
        return {"quick": {"result": "success"},
                "builds-components": {"result": "success" if allowed else "skipped"},
                "builds-readonly": {"result": "skipped" if allowed else "success"}}

    def test_event_routes_and_final_status_reject_wrong_or_incomplete_permission_paths(self):
        for environment, allowed in self.environments():
            with self.subTest(environment=environment):
                self.assertEqual(ci_execution.component_reader_allowed(environment), allowed)
                results = self.results(allowed)
                self.assertTrue(ci_execution.check_routes(results, environment))
                for name in results:
                    for status in ("success", "skipped", "failure", "cancelled", None):
                        if results[name]["result"] != status:
                            changed = copy.deepcopy(results)
                            changed[name]["result"] = status
                            self.assertFalse(ci_execution.check_routes(changed, environment))
                    changed = copy.deepcopy(results)
                    del changed[name]
                    self.assertFalse(ci_execution.check_routes(changed, environment))
                self.assertFalse(ci_execution.check_routes(dict(results, extra={"result": "success"}), environment))

    def test_actual_planner_rejects_reader_credentials_before_untrusted_execution(self):
        for environment, allowed in self.environments():
            result = subprocess.run([sys.executable, str(ROOT / "scripts/ci-component-plan.py"), "execution",
                "--profile", "none", "--component-reader"], env=dict(os.environ, **environment, PYTHONOPTIMIZE="2"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(result.returncode == 0, allowed, result.stderr)
            if allowed:
                self.assertIn('toolchains=false', result.stdout)
            else:
                self.assertEqual(result.stdout, "")
                self.assertIn('component reader requires', result.stderr)

    def test_actual_credential_action_guard_matches_the_event_boundary(self):
        action = (ROOT / ".github/actions/setup-component-reader/action.yml").read_text()
        script = textwrap.dedent(action.split("      run: |\n", 1)[1].split("    - uses:", 1)[0])
        for environment, allowed in self.environments():
            result = subprocess.run(["bash", "-c", script], cwd=ROOT,
                env=dict(os.environ, **environment, PYTHONOPTIMIZE="2"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode == 0, allowed, result.stderr)
        self.assertIn('"$REGISTRY_TOKEN" | docker login', action)
        self.assertIn('--password-stdin', action)
        self.assertIn('uses: ./.github/actions/setup-component-verifier', action)
        verifier = (ROOT / ".github/actions/setup-component-verifier/action.yml").read_text()
        self.assertIn('validate-sigstore-report.py', verifier)
        self.assertIn('sha256sum', verifier)
        self.assertNotIn('sign-blob', action)

    def test_ci_permissions_are_split_at_callers_and_quick_preflight_cannot_inherit_packages(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        readonly = job(workflow, "builds-readonly")
        reader = job(workflow, "builds-components")
        self.assertNotIn("packages:", readonly)
        self.assertIn("contents: read", readonly)
        self.assertIn("component-reader: false", readonly)
        self.assertIn("packages: write", reader)
        self.assertIn("id-token: write", reader)
        self.assertIn("uses: ./.github/workflows/verify-main-incremental.yml", reader)
        self.assertNotIn("id-token:", readonly)
        self.assertNotIn("verify-main-incremental.yml", readonly)
        self.assertNotIn("workflow_call:", workflow)
        # The two job conditions must be exact complements. Independent event
        # tests above exercise the main-only policy and the action guard.
        expression = re.search(r"    if: \$\{\{ (.+) \}\}", reader).group(1)
        self.assertIn("    if: ${{ !(" + expression + ") }}", readonly)
        for condition in ("github.server_url == 'https://github.com'", "github.repository == 'eglinuxer/crossforge'",
                          "github.ref == 'refs/heads/main'", "github.event_name == 'push'", "github.event_name == 'workflow_dispatch'"):
            self.assertIn(condition, expression)
        self.assertIn("check-routes", job(workflow, "builds"))
        self.assertIn("needs: [quick, builds]", job(workflow, "pr-required"))
        quick = (ROOT / ".github/workflows/verify-quick.yml").read_text()
        self.assertNotIn("packages:", quick)
        self.assertNotIn("verify-incremental.yml", quick)
        self.assertIn("if: inputs.plan-components", quick)
        for name in ("candidate.yml", "component-pilot.yml"):
            caller = (ROOT / ".github/workflows" / name).read_text()
            self.assertIn("uses: ./.github/workflows/verify-quick.yml", caller)
            self.assertIn("plan-components: false", caller)
            self.assertNotIn("uses: ./.github/workflows/ci.yml", caller)

    def test_selected_build_jobs_inherit_only_their_callers_permissions_and_pass_pinned_tools(self):
        workflow = (ROOT / ".github/workflows/verify-incremental.yml").read_text()
        self.assertNotIn("\npermissions:\n", workflow)
        for name in ("plan", "inputs", "verified"):
            block = job(workflow, name)
            self.assertIn("permissions:\n      contents: read", block)
            self.assertNotIn("packages:", block)
        for name in ("toolchains", "python", "vcpkg", "gcc", "sdk"):
            block = job(workflow, name)
            self.assertNotIn("permissions:", block)
            self.assertIn("component-reader: ${{ inputs.component-reader }}", block)
            self.assertIn("component-builder: ${{ steps.buildx.outputs.builder }}", block)
        action = (ROOT / ".github/actions/run-build-stage/action.yml").read_text()
        self.assertIn("if: inputs.component-reader == 'true'", action)
        self.assertIn('"${selection[@]}" "${components[@]}"', action)
        self.assertIn('"$RUNNER_TEMP/component-data/$BUILD_STAGE"', action)
        self.assertIn("if: always() && inputs.component-reader == 'true'", action)
        self.assertIn("docker logout ghcr.io", action)
        self.assertIn("--require-components", action)

    def test_main_preparation_reduces_consumer_permissions_and_separates_signing_from_publication(self):
        wrapper = (ROOT / ".github/workflows/verify-main-incremental.yml").read_text()
        self.assertIn("workflow_call:", wrapper)
        self.assertNotIn("workflow_dispatch:", wrapper)
        for arch in ("x86_64", "aarch64"):
            producer = job(wrapper, arch)
            self.assertIn("uses: ./.github/workflows/produce-toolchain.yml", producer)
            self.assertIn("needs.plan.outputs." + arch + "-roles != '[]'", producer)
        consumer = job(wrapper, "builds")
        self.assertIn("uses: ./.github/workflows/verify-main-builds.yml", consumer)
        self.assertIn("packages: write", consumer)
        self.assertIn("id-token: write", consumer)
        self.assertIn("needs.ready.result == 'success'", consumer)
        self.assertIn("python-parts: ${{ needs.plan.outputs.python-parts }}", consumer)
        self.assertIn("check-ready", job(wrapper, "ready"))
        self.assertIn("if: always()", job(wrapper, "verified"))
        producer = (ROOT / ".github/workflows/produce-toolchain.yml").read_text()
        self.assertIn("workflow_call:", producer)
        self.assertNotIn("workflow_dispatch:", producer)
        ensure, sign, store = [job(producer, name) for name in ("ensure", "sign", "store")]
        self.assertLess(ensure.index('checked_source(Path.cwd(), "main")'), ensure.index('docker login'))
        self.assertIn("packages: write", ensure)
        self.assertNotIn("id-token:", ensure)
        self.assertIn("id-token: write", sign)
        self.assertNotIn("packages:", sign)
        self.assertIn("from-handoff --main-ci", sign)
        self.assertIn("needs.ensure.outputs.handoff-sha256", sign)
        self.assertIn("artifact-ids: ${{ needs.ensure.outputs.artifact-id }}", sign)
        self.assertIn("packages: write", store)
        self.assertNotIn("id-token:", store)
        self.assertIn("component-catalog.py publish", store)
        self.assertIn("check-production", job(producer, "verified"))
        for action in re.findall(r"uses: (\S+)", producer + wrapper):
            self.assertTrue(action.startswith("./") or re.fullmatch(r"[\w-]+/[\w-]+@[0-9a-f]{40}", action), action)
        for path in re.findall(r"^\s+path: (.+)$", producer, re.M):
            self.assertNotIn("/oci", path)
            self.assertNotEqual(path, "${{ runner.temp }}/component-producer/")


if __name__ == "__main__":
    unittest.main()
