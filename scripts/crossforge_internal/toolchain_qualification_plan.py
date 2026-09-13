"""Expand complete selected gates into independently qualified target profiles."""

from .identity import require


ROOTS = {
    "toolchain-x86_64": ["toolchain-x86_64-dev"],
    "toolchain-aarch64": ["toolchain-aarch64-dev"],
    "gcc-smoke": ["gcc-testsuite-smoke-evidence"],
    "gcc-full": ["gcc-testsuite-full-qualification-evidence"],
}


def matrix(consumers, stages, group):
    require(group in ("toolchains", "gcc"), "unsupported qualification matrix group")
    names = ("toolchain-x86_64", "toolchain-aarch64") if group == "toolchains" else ("gcc-smoke", "gcc-full")
    result = []
    for stage in names:
        if stage not in consumers:
            continue
        require(consumers[stage] in (stages[stage], ROOTS[stage]),
                "qualification matrix requires the complete canonical gate: " + stage)
        if stage.startswith("toolchain-"):
            result.append({"arch": stage[len("toolchain-"):], "profile": "toolchain"})
        else:
            result.extend({"arch": arch, "profile": stage} for arch in
                          (("x86_64", "aarch64") if stage == "gcc-smoke" else ("x86_64",)))
    # Actions evaluates the matrix before an unselected caller is skipped.
    # The independently checked group output prevents this placeholder running.
    return {"include": result or [{"arch": "x86_64", "profile": "toolchain" if group == "toolchains" else "gcc-smoke"}]}
