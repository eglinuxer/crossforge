//! The audit gate (design doc §5.5, milestone M4): checks that built ELF
//! artifacts stay within their declared baseline, generalizing what
//! auditwheel does for Python wheels.
//!
//! Checks:
//! 1. `GLIBC_*` / `GLIBCXX_*` / `CXXABI_*` / `GCC_*` version needs are
//!    satisfiable by the baseline libraries (compared against the sysroot's
//!    abilists, per soname);
//! 2. no `GLIBC_ABI_DT_RELR` requirement and no DT_RELR relocations
//!    (old dynamic loaders reject both — build with `-z nopack-relative-relocs`);
//! 3. no `__isoc23_*` imports (a host-header leak: the sysroot was bypassed);
//! 4. `DT_NEEDED` sonames restricted to a whitelist (baseline libs + sonames
//!    the caller declares as shipped alongside);
//! 5. PT_INTERP matches the target architecture's loader path;
//! 6. `e_machine` matches the target architecture.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use crate::elfdyn::{self, ElfInfo};
use crate::error::Result;
use crate::sysroot::META_DIR;
use crate::target::TargetArch;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Severity {
    Error,
    Warning,
}

/// Baseline libraries that ship as *separate, optional* packages rather than
/// as part of a base install. The baseline provides them — so their symbol
/// versions are checked like any other — but a target system will only have
/// them if someone installed the package. Verified on a stock
/// `rockylinux:8` image: libc, libstdc++ and libgcc_s are present, these
/// four are not.
const OPTIONAL_RUNTIME_LIBS: &[&str] = &[
    "libgomp.so.1",
    "libatomic.so.1",
    "libquadmath.so.0",
    "libitm.so.1",
];

/// One audit finding.
#[derive(Debug, Clone)]
pub struct Finding {
    pub severity: Severity,
    /// Stable check identifier, e.g. `symbol-version`, `needed-whitelist`.
    pub check: &'static str,
    pub message: String,
}

/// The audit result for one ELF file.
#[derive(Debug, Clone)]
pub struct AuditReport {
    pub path: PathBuf,
    pub findings: Vec<Finding>,
}

impl AuditReport {
    /// True when no error-severity finding is present.
    pub fn passed(&self) -> bool {
        self.findings.iter().all(|f| f.severity != Severity::Error)
    }
}

/// Baseline-aware ELF auditor, built from a generated sysroot's abilists.
#[derive(Debug, Clone)]
pub struct Auditor {
    arch: TargetArch,
    /// soname → set of version strings its baseline build defines.
    versions: BTreeMap<String, BTreeSet<String>>,
    /// sonames a binary may depend on.
    allowed_needed: BTreeSet<String>,
}

impl Auditor {
    /// Builds the rule set from `<sysroot>/.crossforge/abilists/`.
    pub fn from_sysroot(sysroot_root: &Path, arch: TargetArch) -> Result<Self> {
        let dir = sysroot_root.join(META_DIR).join("abilists");
        let mut versions = BTreeMap::new();
        let mut allowed_needed = BTreeSet::new();
        for entry in std::fs::read_dir(&dir)? {
            let entry = entry?;
            let file_name = entry.file_name().to_string_lossy().into_owned();
            let Some(soname) = file_name.strip_suffix(".abilist") else {
                continue;
            };
            let text = std::fs::read_to_string(entry.path())?;
            let set: BTreeSet<String> = text
                .lines()
                .filter_map(|l| l.split_whitespace().next())
                .filter(|v| *v != "-")
                .map(str::to_string)
                .collect();
            allowed_needed.insert(soname.to_string());
            versions.insert(soname.to_string(), set);
        }
        Ok(Self {
            arch,
            versions,
            allowed_needed,
        })
    }

    /// Additionally allows a soname in DT_NEEDED (for libraries the caller
    /// ships alongside the audited binary).
    pub fn allow_needed(&mut self, soname: impl Into<String>) -> &mut Self {
        self.allowed_needed.insert(soname.into());
        self
    }

    /// Audits one ELF file on disk.
    pub fn audit_file(&self, path: &Path) -> Result<AuditReport> {
        let data = std::fs::read(path)?;
        let info = elfdyn::inspect(&data)?;
        Ok(AuditReport {
            path: path.to_path_buf(),
            findings: self.evaluate(&info),
        })
    }

    /// Core rule evaluation, separated for testability.
    pub(crate) fn evaluate(&self, info: &ElfInfo) -> Vec<Finding> {
        let mut findings = Vec::new();
        let error = |check, message: String| Finding {
            severity: Severity::Error,
            check,
            message,
        };
        let warn = |check, message: String| Finding {
            severity: Severity::Warning,
            check,
            message,
        };

        if info.machine != self.arch.e_machine() {
            findings.push(error(
                "arch",
                format!(
                    "e_machine {} does not match target {} ({})",
                    info.machine,
                    self.arch,
                    self.arch.e_machine()
                ),
            ));
        }
        if let Some(interp) = &info.interp {
            if interp != self.arch.interp() {
                findings.push(error(
                    "interp",
                    format!("PT_INTERP is {interp}, expected {}", self.arch.interp()),
                ));
            }
        }
        for soname in &info.needed {
            if !self.allowed_needed.contains(soname) {
                findings.push(error(
                    "needed-whitelist",
                    format!(
                        "DT_NEEDED {soname} is not a baseline library (use allow_needed() if shipped alongside)"
                    ),
                ));
            } else if OPTIONAL_RUNTIME_LIBS.contains(&soname.as_str()) {
                findings.push(warn(
                    "optional-runtime",
                    format!(
                        "DT_NEEDED {soname} is a baseline library but ships as a separate package, \
                         so a minimal target install will not have it; either require that package \
                         or link it statically (e.g. -l:libgomp.a with -Wl,--as-needed)"
                    ),
                ));
            }
        }
        for need in &info.version_needs {
            if need.version == "GLIBC_ABI_DT_RELR" {
                findings.push(error(
                    "dt-relr",
                    "requires GLIBC_ABI_DT_RELR (packed relative relocs); link with -z nopack-relative-relocs".to_string(),
                ));
                continue;
            }
            match self.versions.get(&need.file) {
                Some(set) if set.contains(&need.version) => {}
                Some(_) => findings.push(error(
                    "symbol-version",
                    format!(
                        "requires {}@{} but the baseline {} does not provide it",
                        need.file, need.version, need.file
                    ),
                )),
                None => findings.push(warn(
                    "symbol-version",
                    format!(
                        "requires {}@{} from a non-baseline library (unverifiable)",
                        need.file, need.version
                    ),
                )),
            }
        }
        if info.has_dt_relr {
            findings.push(error(
                "dt-relr",
                "contains DT_RELR packed relocations; old loaders cannot process them".to_string(),
            ));
        }
        for symbol in &info.undefined {
            if symbol.starts_with("__isoc23_") {
                findings.push(error(
                    "host-leak",
                    format!("imports {symbol}: host glibc headers leaked into the build (sysroot bypassed?)"),
                ));
            }
        }
        findings
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::elfdyn::VersionNeed;

    fn test_auditor() -> Auditor {
        let mut versions = BTreeMap::new();
        versions.insert(
            "libc.so.6".to_string(),
            BTreeSet::from(["GLIBC_2.2.5".to_string(), "GLIBC_2.28".to_string()]),
        );
        versions.insert(
            "libstdc++.so.6".to_string(),
            BTreeSet::from(["GLIBCXX_3.4".to_string(), "GLIBCXX_3.4.25".to_string()]),
        );
        Auditor {
            arch: TargetArch::X86_64,
            allowed_needed: versions.keys().cloned().collect(),
            versions,
        }
    }

    fn clean_info() -> ElfInfo {
        ElfInfo {
            machine: 62,
            etype: 3,
            interp: Some("/lib64/ld-linux-x86-64.so.2".to_string()),
            needed: vec!["libc.so.6".to_string()],
            version_needs: vec![VersionNeed {
                file: "libc.so.6".to_string(),
                version: "GLIBC_2.2.5".to_string(),
            }],
            ..Default::default()
        }
    }

    #[test]
    fn clean_binary_passes() {
        let findings = test_auditor().evaluate(&clean_info());
        assert!(findings.is_empty(), "{findings:?}");
    }

    #[test]
    fn optional_runtime_warns_but_passes() {
        // libgomp is a baseline library, but a separate package: a target
        // may not have it, so the gate warns instead of passing silently —
        // and still refuses anything over the baseline.
        let mut auditor = test_auditor();
        auditor.allowed_needed.insert("libgomp.so.1".to_string());
        auditor.versions.insert(
            "libgomp.so.1".to_string(),
            BTreeSet::from(["OMP_1.0".to_string(), "GOMP_4.0".to_string()]),
        );

        let mut info = clean_info();
        info.needed.push("libgomp.so.1".to_string());
        info.version_needs.push(VersionNeed {
            file: "libgomp.so.1".to_string(),
            version: "GOMP_4.0".to_string(),
        });
        let findings = auditor.evaluate(&info);
        let report = AuditReport {
            path: PathBuf::from("t"),
            findings,
        };
        assert!(report.passed(), "baseline-compatible OpenMP must not fail");
        assert!(
            report
                .findings
                .iter()
                .any(|f| f.check == "optional-runtime" && f.severity == Severity::Warning)
        );

        // The same library one version too new is still an error.
        let mut newer = info.clone();
        newer.version_needs.push(VersionNeed {
            file: "libgomp.so.1".to_string(),
            version: "OMP_5.1".to_string(),
        });
        let findings = auditor.evaluate(&newer);
        assert!(
            findings
                .iter()
                .any(|f| f.check == "symbol-version" && f.severity == Severity::Error)
        );
    }

    #[test]
    fn over_baseline_version_is_error() {
        let mut info = clean_info();
        info.version_needs.push(VersionNeed {
            file: "libc.so.6".to_string(),
            version: "GLIBC_2.34".to_string(),
        });
        let findings = test_auditor().evaluate(&info);
        assert!(
            findings
                .iter()
                .any(|f| f.check == "symbol-version" && f.severity == Severity::Error)
        );
    }

    #[test]
    fn dt_relr_lockout_is_error() {
        let mut info = clean_info();
        info.version_needs.push(VersionNeed {
            file: "libc.so.6".to_string(),
            version: "GLIBC_ABI_DT_RELR".to_string(),
        });
        info.has_dt_relr = true;
        let findings = test_auditor().evaluate(&info);
        assert_eq!(findings.iter().filter(|f| f.check == "dt-relr").count(), 2);
    }

    #[test]
    fn unknown_needed_is_error_until_allowed() {
        let mut info = clean_info();
        info.needed.push("libmusa.so.1".to_string());
        let mut auditor = test_auditor();
        assert!(
            auditor
                .evaluate(&info)
                .iter()
                .any(|f| f.check == "needed-whitelist")
        );
        auditor.allow_needed("libmusa.so.1");
        assert!(auditor.evaluate(&info).is_empty());
    }

    #[test]
    fn isoc23_import_is_host_leak() {
        let mut info = clean_info();
        info.undefined.push("__isoc23_strtol".to_string());
        let findings = test_auditor().evaluate(&info);
        assert!(findings.iter().any(|f| f.check == "host-leak"));
    }

    #[test]
    fn wrong_arch_and_interp_are_errors() {
        let mut info = clean_info();
        info.machine = 183;
        info.interp = Some("/lib/ld-linux-aarch64.so.1".to_string());
        let findings = test_auditor().evaluate(&info);
        assert!(findings.iter().any(|f| f.check == "arch"));
        assert!(findings.iter().any(|f| f.check == "interp"));
    }
}
