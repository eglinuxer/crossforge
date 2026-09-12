"""Exercise the verifier action's external local-export boundary."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ComponentVerifierExportTests(unittest.TestCase):
    def test_export_grants_only_its_existing_destination_with_spaces(self):
        action = (ROOT / ".github/actions/setup-component-verifier/action.yml").read_text()
        script = textwrap.dedent(action.split("      run: |\n", 1)[1]).split("expected=", 1)[0]
        self.check_export(script, "crossforge-catalog-verifier")

    def test_public_and_pilot_cosign_exports_use_the_same_narrow_boundary(self):
        for name, count in (("component-pilot", 3), ("candidate", 1), ("promote", 1), ("rollback", 1)):
            workflow = (ROOT / ".github/workflows" / (name + ".yml")).read_text()
            scripts = re.findall(r'          output="\$RUNNER_TEMP/cosign-tool"\n.*?(?=          expected=)',
                                 workflow, re.S)
            self.assertEqual(len(scripts), count)
            for index, script in enumerate(scripts):
                with self.subTest(workflow=name, export=index):
                    self.check_export("set -Eeuo pipefail\n" + textwrap.dedent(script), "cosign-tool")

    def check_export(self, script, output_name):
        with tempfile.TemporaryDirectory(prefix="verifier export ") as temporary:
            root = Path(temporary)
            binary = root / "bin"
            binary.mkdir()
            docker = binary / "docker"
            docker.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
destination = pathlib.Path(os.environ['RUNNER_TEMP']) / os.environ['OUTPUT_NAME']
pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps(args))
if not destination.is_dir():
    raise SystemExit('export destination must exist before Bake evaluates entitlements')
if '--allow=fs.write=' + str(destination) not in args:
    raise SystemExit('additional privileges requested')
""")
            docker.chmod(0o755)
            capture = root / "arguments.json"
            result = subprocess.run(["bash", "-c", script], cwd=ROOT,
                env=dict(os.environ, PATH=str(binary) + os.pathsep + os.environ["PATH"],
                         RUNNER_TEMP=str(root), CAPTURE=str(capture), OUTPUT_NAME=output_name),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(capture.read_text())
            output = root / output_name
            self.assertEqual([arg for arg in args if arg.startswith("--allow")],
                             ["--allow=fs.write=" + str(output)])
            self.assertEqual(args[args.index("--set") + 1],
                             "cosign-host-tool.output=type=local,dest=" + str(output))


if __name__ == "__main__":
    unittest.main()
