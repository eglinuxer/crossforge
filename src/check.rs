//! GCC upstream testsuite execution (DejaGnu) against a built toolchain.
//!
//! Runs `make check-*` targets in the toolchain's GCC build tree and parses
//! the resulting `.sum` files. Cross execution is handled with generated
//! DejaGnu board files: x86_64 targets run directly on the build machine
//! (their baseline glibc is older than the build container's), aarch64
//! targets run under user-mode qemu against the toolchain sysroot.

use std::path::{Path, PathBuf};

use crate::compiler::CompilerArtifact;
use crate::engine::{Cmd, Runner};
use crate::error::{Error, Result};
use crate::spec::ToolchainSpec;
use crate::target::TargetArch;

/// A testsuite selection.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CheckSuite {
    /// The C compiler testsuite (`check-gcc`).
    Gcc,
    /// The C++ compiler testsuite (`check-c++`).
    Cxx,
    /// The libstdc++ testsuite (`check-target-libstdc++-v3`).
    Libstdcxx,
}

impl CheckSuite {
    pub const ALL: &[CheckSuite] = &[CheckSuite::Gcc, CheckSuite::Cxx, CheckSuite::Libstdcxx];

    pub fn make_target(&self) -> &'static str {
        match self {
            CheckSuite::Gcc => "check-gcc",
            CheckSuite::Cxx => "check-c++",
            CheckSuite::Libstdcxx => "check-target-libstdc++-v3",
        }
    }

    pub fn name(&self) -> &'static str {
        match self {
            CheckSuite::Gcc => "gcc",
            CheckSuite::Cxx => "c++",
            CheckSuite::Libstdcxx => "libstdc++",
        }
    }

    /// `.sum` files this suite produces, resolved inside the GCC build dir.
    /// The libstdc++ sum lives under the target-triple subdirectory; fall
    /// back to a scan when the build tree was configured for another triple.
    fn sum_paths(&self, build_gcc: &Path, triple: &str) -> Vec<PathBuf> {
        match self {
            CheckSuite::Gcc => vec![build_gcc.join("gcc/testsuite/gcc/gcc.sum")],
            CheckSuite::Cxx => vec![build_gcc.join("gcc/testsuite/g++/g++.sum")],
            CheckSuite::Libstdcxx => {
                let preferred = build_gcc
                    .join(triple)
                    .join("libstdc++-v3/testsuite/libstdc++.sum");
                if preferred.is_file() {
                    return vec![preferred];
                }
                if let Ok(entries) = std::fs::read_dir(build_gcc) {
                    for entry in entries.flatten() {
                        let candidate = entry.path().join("libstdc++-v3/testsuite/libstdc++.sum");
                        if candidate.is_file() {
                            return vec![candidate];
                        }
                    }
                }
                vec![preferred]
            }
        }
    }
}

impl std::str::FromStr for CheckSuite {
    type Err = Error;

    fn from_str(s: &str) -> Result<Self> {
        match s {
            "gcc" | "c" => Ok(CheckSuite::Gcc),
            "c++" | "g++" | "cxx" => Ok(CheckSuite::Cxx),
            "libstdc++" | "libstdcxx" => Ok(CheckSuite::Libstdcxx),
            other => Err(Error::UnknownSuite(other.to_string())),
        }
    }
}

/// Parsed results of one testsuite run.
#[derive(Debug, Clone, Default)]
pub struct CheckSummary {
    pub suite: String,
    pub expected_passes: u64,
    pub unexpected_failures: u64,
    pub unexpected_successes: u64,
    pub expected_failures: u64,
    pub unresolved: u64,
    pub unsupported: u64,
    /// The `FAIL:` / `UNRESOLVED:` lines, verbatim.
    pub failures: Vec<String>,
}

impl CheckSummary {
    pub fn total_run(&self) -> u64 {
        self.expected_passes
            + self.unexpected_failures
            + self.unexpected_successes
            + self.expected_failures
            + self.unresolved
    }
}

/// Runs GCC testsuites in an existing build tree.
#[derive(Debug)]
pub struct CheckRunner<'a, R: Runner> {
    pub runner: &'a R,
    /// The engine work dir (containing `build/` with the GCC build trees).
    pub work_dir: PathBuf,
    pub jobs: usize,
}

impl<'a, R: Runner> CheckRunner<'a, R> {
    /// Runs the selected suites for a built toolchain and returns per-suite
    /// summaries. The GCC build tree from the toolchain build must still be
    /// present (`build/build-gcc-<id>`).
    pub fn run(
        &self,
        spec: &ToolchainSpec,
        compiler: &CompilerArtifact,
        suites: &[CheckSuite],
    ) -> Result<Vec<CheckSummary>> {
        let build_gcc = self
            .work_dir
            .join("build")
            .join(format!("build-gcc-{}", spec.id()));
        if !build_gcc.is_dir() {
            return Err(Error::Io(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                format!(
                    "GCC build tree {} not found; run `crossforge build` first (the tree is kept after a build)",
                    build_gcc.display()
                ),
            )));
        }
        let logs = self.work_dir.join("build/logs").join(spec.id());
        std::fs::create_dir_all(&logs)?;

        let board = self.write_board_files(spec, compiler)?;
        self.fixup_dts_exp()?;
        // Parallel check shards race on `mkdir plugin` (a plain mkdir in the
        // shard recipe); losing shards abort before ever running runtest.
        // Pre-creating the directory removes the race.
        std::fs::create_dir_all(build_gcc.join("gcc/plugin"))?;
        let path_env = format!(
            "{}/bin:/usr/local/bin:/usr/bin:/bin",
            compiler.prefix.display()
        );
        let mut summaries = Vec::new();
        for suite in suites {
            tracing::info!(
                suite = suite.name(),
                "running testsuite (this takes a while)"
            );
            // DejaGnu failures do not fail `make check`; a non-zero status
            // here means infrastructure breakage, but partial .sum files are
            // still worth parsing, so log and continue.
            let result = self.runner.exec(
                &Cmd::new("make")
                    .arg(format!("-j{}", self.jobs))
                    .arg("-k")
                    .arg(suite.make_target())
                    .arg(format!("RUNTESTFLAGS=--target_board={board}"))
                    .cwd(&build_gcc)
                    .env("PATH", &path_env)
                    // DejaGnu resolves the invoking user via `whoami` when
                    // USER/LOGNAME are unset, which crashes runtest inside
                    // containers running as a uid absent from /etc/passwd.
                    .env("USER", "crossforge")
                    // Unicode identifier / literal tests need a UTF-8 locale;
                    // container default is C/POSIX. C.UTF-8 ships with glibc
                    // 2.28+, no langpack required.
                    .env("LC_ALL", "C.UTF-8")
                    .env(
                        "DEJAGNU",
                        self.dejagnu_dir().join("site.exp").display().to_string(),
                    )
                    .log(logs.join(format!("check-{}.log", suite.name()))),
            );
            if let Err(e) = result {
                tracing::warn!(suite = suite.name(), error = %e, "make check returned an error; parsing partial results");
            }
            let mut summary = CheckSummary {
                suite: suite.name().to_string(),
                ..Default::default()
            };
            for path in suite.sum_paths(&build_gcc, &compiler.triple) {
                if !path.is_file() {
                    return Err(Error::Io(std::io::Error::new(
                        std::io::ErrorKind::NotFound,
                        format!(
                            "expected testsuite summary {} was not produced",
                            path.display()
                        ),
                    )));
                }
                // Testsuite output can contain arbitrary bytes; parse lossily.
                parse_sum(
                    &String::from_utf8_lossy(&std::fs::read(&path)?),
                    &mut summary,
                );
            }
            let failures_file = logs.join(format!("check-{}.failures", suite.name()));
            std::fs::write(&failures_file, summary.failures.join("\n"))?;
            tracing::info!(
                suite = suite.name(),
                passes = summary.expected_passes,
                unexpected_failures = summary.unexpected_failures,
                unsupported = summary.unsupported,
                failures_file = %failures_file.display(),
                "testsuite finished"
            );
            summaries.push(summary);
        }
        Ok(summaries)
    }

    fn dejagnu_dir(&self) -> PathBuf {
        self.work_dir.join("build/dejagnu")
    }

    /// The RH dts-test patches add `get_dts_base_major_version`, which parses
    /// `/usr/bin/gcc -dumpversion` with an `X.Y.Z` regexp; RH system compilers
    /// print a bare major ("8"), crashing every libstdc++ runtest at init.
    /// Rewrite the proc to tolerate single-component versions (idempotent).
    fn fixup_dts_exp(&self) -> Result<()> {
        let src_root = self.work_dir.join("build/src");
        let Ok(entries) = std::fs::read_dir(&src_root) else {
            return Ok(());
        };
        for entry in entries.flatten() {
            let dts = entry.path().join("libstdc++-v3/testsuite/lib/dts.exp");
            if !dts.is_file() {
                continue;
            }
            let text = std::fs::read_to_string(&dts)?;
            if text.contains("crossforge") {
                continue;
            }
            let robust = "# crossforge: rewritten to tolerate single-component -dumpversion\n\
                 proc get_dts_base_major_version { } {\n\
                 \x20   set dotted_version [exec /usr/bin/gcc -dumpversion]\n\
                 \x20   set major [lindex [split $dotted_version \".\"] 0]\n\
                 \x20   return $major\n\
                 }\n";
            std::fs::write(&dts, robust)?;
            tracing::info!(file = %dts.display(), "patched RH dts.exp version probe");
        }
        Ok(())
    }

    /// Writes the DejaGnu site config and the board file for this target,
    /// returning the board name.
    fn write_board_files(
        &self,
        spec: &ToolchainSpec,
        compiler: &CompilerArtifact,
    ) -> Result<String> {
        let dir = self.dejagnu_dir();
        let boards = dir.join("boards");
        std::fs::create_dir_all(&boards)?;
        std::fs::write(
            dir.join("site.exp"),
            format!("lappend boards_dir \"{}\"\n", boards.display()),
        )?;
        let (name, content) = match spec.target {
            // The target baseline glibc is older than the build container's,
            // so binaries execute directly on the build machine.
            TargetArch::X86_64 => (
                "crossforge-local".to_string(),
                // set_board_info only sets values that are not already set,
                // and DejaGnu pre-marks non-localhost board names as remote
                // before loading the board file — unset first.
                "load_generic_config \"unix\"\n\
                 process_multilib_options \"\"\n\
                 unset_board_info isremote\n\
                 set_board_info isremote 0\n\
                 set_board_info hostname localhost\n"
                    .to_string(),
            ),
            // User-mode qemu with the toolchain sysroot as the loader prefix.
            TargetArch::Aarch64 => {
                let sysroot = compiler.prefix.join(&compiler.triple).join("sysroot");
                (
                    "crossforge-qemu-aarch64".to_string(),
                    format!(
                        "load_generic_config \"sim\"\n\
                         process_multilib_options \"\"\n\
                         set_board_info is_simulator 1\n\
                         set_board_info sim \"qemu-aarch64\"\n\
                         set_board_info sim,options \"-L {}\"\n\
                         set_board_info sim_time_limit 300\n\
                         set_board_info gcc,stack_size 16384\n",
                        sysroot.display()
                    ),
                )
            }
        };
        std::fs::write(boards.join(format!("{name}.exp")), content)?;
        Ok(name)
    }
}

/// Accumulates one DejaGnu `.sum` file into a summary.
fn parse_sum(text: &str, summary: &mut CheckSummary) {
    for line in text.lines() {
        if let Some(rest) = line.strip_prefix("# of ") {
            let Some((label, value)) = rest.rsplit_once('\t').or_else(|| rest.rsplit_once(' '))
            else {
                continue;
            };
            let Ok(value) = value.trim().parse::<u64>() else {
                continue;
            };
            match label.trim() {
                "expected passes" => summary.expected_passes += value,
                "unexpected failures" => summary.unexpected_failures += value,
                "unexpected successes" => summary.unexpected_successes += value,
                "expected failures" => summary.expected_failures += value,
                "unresolved testcases" => summary.unresolved += value,
                "unsupported tests" => summary.unsupported += value,
                _ => {}
            }
        } else if line.starts_with("FAIL: ") || line.starts_with("UNRESOLVED: ") {
            summary.failures.push(line.to_string());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sum_parsing() {
        let text = "\
Running target crossforge-local
PASS: gcc.dg/pr1.c (test for excess errors)
FAIL: gcc.dg/pr2.c execution test
UNRESOLVED: gcc.dg/pr3.c compilation failed
XFAIL: gcc.dg/pr4.c known bug

\t\t=== gcc Summary ===

# of expected passes\t\t140123
# of unexpected failures\t2
# of expected failures\t\t512
# of unresolved testcases\t1
# of unsupported tests\t\t3210
";
        let mut summary = CheckSummary::default();
        parse_sum(text, &mut summary);
        assert_eq!(summary.expected_passes, 140123);
        assert_eq!(summary.unexpected_failures, 2);
        assert_eq!(summary.expected_failures, 512);
        assert_eq!(summary.unresolved, 1);
        assert_eq!(summary.unsupported, 3210);
        assert_eq!(summary.failures.len(), 2);
        assert!(summary.total_run() > 140000);
    }

    #[test]
    fn suite_parsing_and_targets() {
        assert_eq!("c++".parse::<CheckSuite>().unwrap(), CheckSuite::Cxx);
        assert_eq!(
            "libstdc++".parse::<CheckSuite>().unwrap().make_target(),
            "check-target-libstdc++-v3"
        );
        assert!("fortran".parse::<CheckSuite>().is_err());
    }
}
