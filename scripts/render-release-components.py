#!/usr/bin/env python3
"""Render the complete release graph from the stable core and vcpkg extension."""

import runpy
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
VCPKG_EXTENSION = runpy.run_path(
    str(SCRIPT_DIRECTORY / "release-components-vcpkg.py")
)
CORE = runpy.run_path(
    str(SCRIPT_DIRECTORY / "release-components-core.py"),
    init_globals={
        "COMPONENT_EXTENSIONS": (VCPKG_EXTENSION["extend_component_graph"],)
    },
)

for name, value in CORE.items():
    if not name.startswith("__"):
        globals()[name] = value
for name in (
    "CMAKE_HOST_TOOL_POLICY",
    "NINJA_HOST_TOOL_POLICY",
    "VCPKG_CONTRACT_POLICY",
    "VCPKG_INTEGRATION_POLICY",
    "VCPKG_UPSTREAM_TIER1_POLICY",
    "VCPKG_UPSTREAM_TIER2_POLICY",
    "VCPKG_UPSTREAM_TIER3_POLICY",
):
    globals()[name] = VCPKG_EXTENSION[name]


if __name__ == "__main__":
    raise SystemExit(CORE["main"]())
