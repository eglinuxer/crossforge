#!/usr/bin/env python3
"""Render the complete release graph from the stable core and extensions."""

import runpy
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
VCPKG_EXTENSION = runpy.run_path(
    str(SCRIPT_DIRECTORY / "release-components-vcpkg.py")
)
PACKAGING_EXTENSION = runpy.run_path(
    str(SCRIPT_DIRECTORY / "release-components-packaging.py")
)
CORE = runpy.run_path(
    str(SCRIPT_DIRECTORY / "release-components-core.py"),
    init_globals={
        "COMPONENT_EXTENSIONS": (
            VCPKG_EXTENSION["extend_component_graph"],
            PACKAGING_EXTENSION["extend_component_graph"],
        )
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
globals()["CROSSPACK_POLICY"] = PACKAGING_EXTENSION["CROSSPACK_POLICY"]
globals()["CROSSPACK_QUALIFICATION_POLICY"] = PACKAGING_EXTENSION[
    "CROSSPACK_QUALIFICATION_POLICY"
]
globals()["CROSSFORGE_LAUNCHER_POLICY"] = PACKAGING_EXTENSION[
    "CROSSFORGE_LAUNCHER_POLICY"
]


if __name__ == "__main__":
    raise SystemExit(CORE["main"]())
