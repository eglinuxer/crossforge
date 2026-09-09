#!/usr/bin/env python3
"""Release unused hosted-runner SDK space before starting BuildKit."""

import os
from pathlib import Path
import shutil
import subprocess


# Crossforge builds its toolchains inside locked containers. Keep the runner's
# Python, Node, Docker, checkout, and tool cache for Actions and diagnostics.
UNUSED_SDKS = (
    "/usr/local/lib/android",
    "/usr/share/dotnet",
    "/usr/local/.ghcup",
    "/opt/ghc",
)


def prepare(environment):
    if environment.get("GITHUB_ACTIONS") != "true" or environment.get("RUNNER_ENVIRONMENT") != "github-hosted":
        raise ValueError("disk preparation requires an ephemeral GitHub-hosted runner")
    before = shutil.disk_usage("/").free
    for directory in UNUSED_SDKS:
        if Path(directory).exists():
            subprocess.run(["sudo", "rm", "-rf", "--", directory], check=True)
    after = shutil.disk_usage("/").free
    print(f"Hosted build disk: free_before={before} free_after={after} reclaimed={after - before}")


if __name__ == "__main__":
    prepare(os.environ)
