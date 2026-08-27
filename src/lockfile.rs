//! Sysroot lockfiles (design doc §5.1).
//!
//! Resolution reads live repository metadata and picks the newest build of
//! everything, so the same crossforge revision produces a different sysroot
//! next month. That is fine for "give me a baseline" and useless for "give
//! me *that* baseline again" — which is what a build environment identity
//! needs, and what reproducing a release requires.
//!
//! A lockfile records the exact answer: every package by NEVRA, content
//! hash and download URL. Building from one skips resolution entirely, so
//! it is also considerably faster — no repomd, no multi-megabyte primary.xml
//! parse — and needs nothing from the repositories but the RPMs themselves.

use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::error::{Error, Result};
use crate::repodata::RepoPackage;
use crate::target::TargetArch;

/// Bumped when the format changes in a way older readers cannot handle.
pub const LOCK_SCHEMA: u32 = 1;

/// One resolved package, pinned by content.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct LockedPackage {
    pub name: String,
    pub evr: String,
    pub arch: String,
    pub sha256: String,
    /// Absolute download URL, so replay needs no repository metadata.
    pub url: String,
}

/// The complete, replayable answer to one resolution.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SysrootLock {
    pub schema: u32,
    pub baseline: String,
    pub arch: String,
    pub profile: String,
    pub source: String,
    pub generator: String,
    /// Capabilities nothing provided, carried over so a locked build reports
    /// the same gaps as the resolution that produced it.
    #[serde(default)]
    pub unresolved: Vec<String>,
    pub package: Vec<LockedPackage>,
}

impl SysrootLock {
    /// Builds a lock from a resolved plan.
    pub fn from_resolved(
        baseline: &str,
        arch: TargetArch,
        profile: &str,
        source: &str,
        unresolved: &[String],
        packages: &[(String, RepoPackage)],
    ) -> Self {
        let mut package: Vec<LockedPackage> = packages
            .iter()
            .map(|(repo, p)| LockedPackage {
                name: p.name.clone(),
                evr: p.evr(),
                arch: p.arch.clone(),
                sha256: p.checksum.clone(),
                url: format!("{repo}{}", p.location),
            })
            .collect();
        // Sorted so a re-resolution that picks the same packages produces the
        // same file, whatever order the repositories listed them in.
        package.sort_by(|a, b| (&a.name, &a.arch).cmp(&(&b.name, &b.arch)));
        Self {
            schema: LOCK_SCHEMA,
            baseline: baseline.to_string(),
            arch: arch.to_string(),
            profile: profile.to_string(),
            source: source.to_string(),
            generator: format!("crossforge {}", env!("CARGO_PKG_VERSION")),
            unresolved: unresolved.to_vec(),
            package,
        }
    }

    pub fn write(&self, path: &Path) -> Result<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let header = format!(
            "# crossforge sysroot lock: {} / {} / profile {}\n\
             # Every package pinned by NEVRA, content hash and URL. Replay with\n\
             # `crossforge sysroot --locked <this file>`, which skips resolution\n\
             # entirely and therefore needs no repository metadata.\n\
             # Regenerate with `crossforge sysroot --lock <this file>`.\n\n",
            self.baseline, self.arch, self.profile
        );
        std::fs::write(path, header + &toml::to_string_pretty(self)?)?;
        Ok(())
    }

    pub fn read(path: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path)?;
        let lock: Self = toml::from_str(&text).map_err(Error::Registry)?;
        if lock.schema != LOCK_SCHEMA {
            return Err(Error::Lockfile(format!(
                "{}: schema {} is not the {LOCK_SCHEMA} this build understands",
                path.display(),
                lock.schema
            )));
        }
        Ok(lock)
    }

    /// Checks the lock describes what the caller is asking to build, so a
    /// mismatched file fails loudly instead of producing the wrong sysroot.
    pub fn check_matches(&self, baseline: &str, arch: TargetArch, profile: &str) -> Result<()> {
        if self.baseline != baseline || self.arch != arch.to_string() || self.profile != profile {
            return Err(Error::Lockfile(format!(
                "it describes {}/{}/{} but the request is {baseline}/{arch}/{profile}",
                self.baseline, self.arch, self.profile
            )));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pkg(name: &str, arch: &str) -> (String, RepoPackage) {
        (
            "https://repo.example/8/BaseOS/x86_64/os/".to_string(),
            RepoPackage {
                name: name.to_string(),
                arch: arch.to_string(),
                version: "2.28".to_string(),
                release: "251.el8".to_string(),
                location: format!("Packages/{name}.rpm"),
                checksum: "f".repeat(64),
                ..Default::default()
            },
        )
    }

    #[test]
    fn lock_roundtrips_and_sorts() {
        let dir = tempfile::tempdir().unwrap();
        let packages = vec![pkg("zlib", "x86_64"), pkg("glibc", "x86_64")];
        let lock = SysrootLock::from_resolved(
            "el8",
            TargetArch::X86_64,
            "minimal",
            "rocky-8",
            &["/bin/sh".to_string()],
            &packages,
        );
        // Sorted regardless of the order resolution produced.
        assert_eq!(lock.package[0].name, "glibc");
        assert_eq!(
            lock.package[0].url,
            "https://repo.example/8/BaseOS/x86_64/os/Packages/glibc.rpm"
        );
        assert_eq!(lock.unresolved, vec!["/bin/sh"]);

        let path = dir.path().join("el8-x86_64.lock.toml");
        lock.write(&path).unwrap();
        let back = SysrootLock::read(&path).unwrap();
        assert_eq!(back.package, lock.package);
        assert_eq!(back.schema, LOCK_SCHEMA);
    }

    #[test]
    fn mismatched_lock_is_rejected() {
        let lock = SysrootLock::from_resolved(
            "el8",
            TargetArch::X86_64,
            "minimal",
            "rocky-8",
            &[],
            &[pkg("glibc", "x86_64")],
        );
        assert!(
            lock.check_matches("el8", TargetArch::X86_64, "minimal")
                .is_ok()
        );
        assert!(
            lock.check_matches("el8", TargetArch::Aarch64, "minimal")
                .is_err()
        );
        assert!(
            lock.check_matches("el8", TargetArch::X86_64, "qt6")
                .is_err()
        );
    }
}
