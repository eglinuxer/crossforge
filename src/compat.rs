//! Compat-pack generation (design doc §4.2, milestone M3): libstdc++
//! nonshared hybrid linking, the cross-compiled take on RH devtoolset.
//!
//! The full static `libstdc++.a` built by the toolchain's GCC is pruned
//! against the baseline `libstdc++.so.6` export list: members whose defined
//! symbols all exist in the baseline are dropped (resolved dynamically at run
//! time); members providing anything newer are kept in
//! `libstdc++_nonshared.a` and linked statically. A linker script installed
//! in GCC's own library directory (searched before the target lib dir)
//! redirects `-lstdc++` to `baseline .so + nonshared archive`.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use crate::ar;
use crate::baseline::BaselineDef;
use crate::compiler::CompilerArtifact;
use crate::elfdyn;
use crate::engine::{Cmd, Runner};
use crate::error::{Error, Result};
use crate::spec::ToolchainSpec;
use crate::target::TargetArch;

/// Where the nonshared archive came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NonsharedSource {
    /// Built by the RH gcc-toolset compat patches (source-level, hand-tuned).
    RedHat,
    /// Produced by crossforge's object-level pruning of `libstdc++.a`
    /// (fallback for source trees without the RH patches).
    Pruned,
}

/// BFD output format name per target, for the linker script's OUTPUT_FORMAT.
fn output_format(arch: TargetArch) -> &'static str {
    match arch {
        TargetArch::X86_64 => "elf64-x86-64",
        TargetArch::Aarch64 => "elf64-littleaarch64",
    }
}

/// A generated compat-pack, installed inside the toolchain prefix.
#[derive(Debug, Clone)]
pub struct CompatArtifact {
    /// The nonshared static archive.
    pub nonshared: PathBuf,
    /// The linker script shadowing `libstdc++.so`.
    pub linker_script: PathBuf,
    pub source: NonsharedSource,
    /// Pruning statistics (zero for the RH source).
    pub kept_objects: usize,
    pub dropped_objects: usize,
}

/// Builds and installs the libstdc++ compat-pack for a built compiler.
#[derive(Debug)]
pub struct CompatBuilder<'a, R: Runner> {
    pub runner: &'a R,
    /// Directory for intermediate object files and logs.
    pub work_dir: PathBuf,
}

impl<'a, R: Runner> CompatBuilder<'a, R> {
    /// Installs `libstdc++_nonshared.a` + linker script into GCC's internal
    /// library directory. Prefers the RH-built nonshared archive when the
    /// baseline declares one and the source tree carried the gcc-toolset
    /// patches; otherwise falls back to pruning the toolchain's `libstdc++.a`
    /// against the sysroot baseline. Idempotent.
    pub fn build(
        &self,
        spec: &ToolchainSpec,
        baseline: &BaselineDef,
        compiler: &CompilerArtifact,
    ) -> Result<CompatArtifact> {
        let triple = &compiler.triple;
        let gcc_libdir = gcc_internal_libdir(&compiler.prefix, triple)?;
        let script_path = gcc_libdir.join("libstdc++.so");
        let nonshared_path = gcc_libdir.join("libstdc++_nonshared.a");
        if script_path.is_file() {
            tracing::info!(script = %script_path.display(), "compat-pack already installed, skipping");
            let source = if compiler
                .prefix
                .join(triple)
                .join("lib64")
                .join(format!(
                    "libstdc++_nonshared{}.a",
                    baseline.rh_nonshared.as_deref().unwrap_or("")
                ))
                .is_file()
            {
                NonsharedSource::RedHat
            } else {
                NonsharedSource::Pruned
            };
            return Ok(CompatArtifact {
                nonshared: nonshared_path,
                linker_script: script_path,
                source,
                kept_objects: 0,
                dropped_objects: 0,
            });
        }

        // Preferred path: the RH-built nonshared archive for this baseline.
        if let Some(level) = &baseline.rh_nonshared {
            let rh_archive = compiler
                .prefix
                .join(triple)
                .join("lib64")
                .join(format!("libstdc++_nonshared{level}.a"));
            if rh_archive.is_file() {
                std::fs::copy(&rh_archive, &nonshared_path)?;
                write_linker_script(&script_path, spec)?;
                tracing::info!(
                    archive = %rh_archive.display(),
                    "compat-pack installed from RH nonshared{level}"
                );
                return Ok(CompatArtifact {
                    nonshared: nonshared_path,
                    linker_script: script_path,
                    source: NonsharedSource::RedHat,
                    kept_objects: 0,
                    dropped_objects: 0,
                });
            }
            tracing::warn!(
                baseline = %baseline.alias,
                "baseline declares rh_nonshared={level} but the archive is missing \
                 (sources without RH patches?); falling back to object-level pruning"
            );
        }

        // Baseline export set from the sysroot's abilist (produced in M1).
        let sysroot = compiler.prefix.join(triple).join("sysroot");
        let abilist_path = sysroot.join(".crossforge/abilists/libstdc++.so.6.abilist");
        let baseline = read_abilist_names(&abilist_path)?;
        tracing::info!(
            symbols = baseline.len(),
            "baseline libstdc++ export set loaded"
        );

        // Prune the full static library member by member.
        let full_archive = compiler.prefix.join(triple).join("lib64/libstdc++.a");
        let archive_data = std::fs::read(&full_archive)?;
        let members = ar::parse(&archive_data)?;
        let obj_dir = self.work_dir.join(format!("nonshared-{}", spec.id()));
        std::fs::create_dir_all(&obj_dir)?;
        let mut kept: Vec<PathBuf> = Vec::new();
        let mut dropped = 0usize;
        for (index, member) in members.iter().enumerate() {
            let defined = elfdyn::defined_global_symbols(member.data)?;
            // Keep only members defining something the baseline lacks; members
            // with no global definitions contribute nothing and are dropped.
            if !defined.is_empty() && defined.iter().all(|s| baseline.contains(s)) {
                dropped += 1;
                continue;
            }
            if defined.is_empty() {
                dropped += 1;
                continue;
            }
            let path = obj_dir.join(format!("{index:03}-{}", member.name));
            std::fs::write(&path, member.data)?;
            kept.push(path);
        }
        tracing::info!(kept = kept.len(), dropped, "libstdc++.a pruned");

        // Repack with the toolchain's ar (creates the required symbol index).
        let ar_bin = compiler.prefix.join("bin").join(format!("{triple}-ar"));
        let mut cmd = Cmd::new(ar_bin.display().to_string())
            .arg("rcs")
            .arg(nonshared_path.display().to_string())
            .log(
                self.work_dir
                    .join("logs")
                    .join(format!("{}-nonshared-ar.log", spec.id())),
            );
        for path in &kept {
            cmd = cmd.arg(path.display().to_string());
        }
        self.runner.exec(&cmd)?;

        write_linker_script(&script_path, spec)?;
        tracing::info!(script = %script_path.display(), "compat-pack installed (pruned)");
        Ok(CompatArtifact {
            nonshared: nonshared_path,
            linker_script: script_path,
            source: NonsharedSource::Pruned,
            kept_objects: kept.len(),
            dropped_objects: dropped,
        })
    }
}

/// Linker script: old symbols resolve against the baseline .so inside the
/// sysroot (`=` prefix), new ones statically from the nonshared archive
/// (same directory, already on the -L path).
fn write_linker_script(script_path: &Path, spec: &ToolchainSpec) -> Result<()> {
    let script = format!(
        "/* crossforge compat-pack: baseline {} ({}) */\n\
         OUTPUT_FORMAT({})\n\
         INPUT ( =/usr/lib64/libstdc++.so.6 -lstdc++_nonshared )\n",
        spec.baseline,
        spec.target,
        output_format(spec.target),
    );
    std::fs::write(script_path, script)?;
    Ok(())
}

/// Locates `<prefix>/lib/gcc/<triple>/<version>/` without hardcoding the GCC
/// version directory name.
fn gcc_internal_libdir(prefix: &Path, triple: &str) -> Result<PathBuf> {
    let base = prefix.join("lib/gcc").join(triple);
    for entry in std::fs::read_dir(&base)? {
        let entry = entry?;
        if entry.path().is_dir() {
            return Ok(entry.path());
        }
    }
    Err(Error::Io(std::io::Error::new(
        std::io::ErrorKind::NotFound,
        format!("no GCC version directory under {}", base.display()),
    )))
}

/// Reads the symbol-name column of an abilist file (`VERSION name` per line).
fn read_abilist_names(path: &Path) -> Result<BTreeSet<String>> {
    let text = std::fs::read_to_string(path)?;
    Ok(text
        .lines()
        .filter_map(|line| line.split_whitespace().nth(1))
        .map(str::to_string)
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn abilist_names_are_second_column() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("x.abilist");
        std::fs::write(
            &path,
            "GLIBCXX_3.4 _ZSt4cout\nGLIBCXX_3.4.25 _ZSt9from_x\n- unversioned\n",
        )
        .unwrap();
        let names = read_abilist_names(&path).unwrap();
        assert!(names.contains("_ZSt4cout"));
        assert!(names.contains("_ZSt9from_x"));
        assert!(names.contains("unversioned"));
        assert_eq!(names.len(), 3);
    }

    #[test]
    fn output_format_covers_targets() {
        assert_eq!(output_format(TargetArch::X86_64), "elf64-x86-64");
        assert_eq!(output_format(TargetArch::Aarch64), "elf64-littleaarch64");
    }
}
