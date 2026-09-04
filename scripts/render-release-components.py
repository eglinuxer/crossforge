#!/usr/bin/env python3
"""Render the complete release graph from the stable core and extensions."""

import copy
import runpy
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
SUPPLY_EXTENSION = runpy.run_path(
    str(SCRIPT_DIRECTORY / "release-components-supply.py")
)
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
# Supply policy is deliberately overlaid after loading the immutable core.
# Toolchain and Python containers copy only release-components-core.py, so a
# candidate workflow change cannot invalidate their build or qualification
# layers merely because the shared script bytes changed.
CORE["CANDIDATE_MANIFEST_POLICY"].clear()
CORE["CANDIDATE_MANIFEST_POLICY"].update(
    copy.deepcopy(SUPPLY_EXTENSION["CANDIDATE_MANIFEST_POLICY"])
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
globals()["COMPLETE_SDK_QUALIFICATION_POLICY"] = PACKAGING_EXTENSION[
    "COMPLETE_SDK_QUALIFICATION_POLICY"
]


if __name__ == "__main__":
    raise SystemExit(CORE["main"]())
