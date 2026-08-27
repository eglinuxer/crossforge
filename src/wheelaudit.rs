//! Wheel policy audit (design doc §9, T6): validates that every ELF inside a
//! wheel satisfies the embedded manylinux_2_28 policy — symbol-version
//! ceilings (note GLIBCXX 3.4.24, one step below the el8 system library),
//! the DT_NEEDED whitelist, no libpython linkage, tag consistency, and
//! RECORD integrity. The table is transcribed from auditwheel's policy JSON
//! and cross-checked against it in CI.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use serde::Deserialize;

use crate::audit::{AuditReport, Finding, Severity};
use crate::elfdyn;
use crate::error::{Error, Result};
use crate::target::TargetArch;
use crate::whl::{self, WheelName};

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WheelPolicy {
    pub policy: String,
    pub auditwheel_reference: String,
    pub lib_whitelist: Vec<String>,
    pub symbol_versions: BTreeMap<String, BTreeMap<String, Vec<String>>>,
    pub blacklist: BTreeMap<String, Vec<String>>,
}

const BUILTIN_TOML: &str = include_str!("registry/manylinux-policy.toml");

impl WheelPolicy {
    pub fn builtin() -> Self {
        toml::from_str(BUILTIN_TOML).expect("builtin manylinux policy must parse")
    }

    /// The platform tag wheels get after passing this policy, e.g.
    /// `manylinux_2_28_x86_64`.
    pub fn platform_tag(&self, arch: TargetArch) -> String {
        format!("{}_{}", self.policy, arch.as_str())
    }

    fn versions(&self, arch: TargetArch) -> Result<&BTreeMap<String, Vec<String>>> {
        self.symbol_versions
            .get(arch.as_str())
            .ok_or_else(|| Error::Wheel(format!("policy has no symbol versions for {arch}")))
    }
}

/// Audits one built wheel against the policy. `arch` is the target the wheel
/// was built for; the wheel may still carry a `linux_*` tag (audit runs
/// before retagging). `excludes` are additional sonames the caller declares
/// the runtime provides (driver-style libraries, e.g. `libcuda.so.1`) —
/// allowed as DT_NEEDED and skipped by the symbol-version checks.
pub fn audit_wheel(
    policy: &WheelPolicy,
    path: &Path,
    arch: TargetArch,
    excludes: &[String],
) -> Result<AuditReport> {
    let file_name = path
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or_else(|| Error::Archive("bad wheel path".to_string()))?;
    let name = WheelName::parse(file_name)?;
    let entries = whl::read_wheel(path)?;
    let mut findings = Vec::new();

    // RECORD integrity.
    for bad in whl::verify_record(&entries)? {
        findings.push(Finding {
            check: "record",
            severity: Severity::Error,
            message: format!("RECORD hash mismatch or missing entry: {bad}"),
        });
    }

    // Platform tag must be linux_* (pre-retag) or already this policy's tag.
    let expected_platform = policy.platform_tag(arch);
    let linux_platform = format!("linux_{}", arch.as_str());
    if name.platform_tag != expected_platform && name.platform_tag != linux_platform {
        findings.push(Finding {
            check: "platform-tag",
            severity: Severity::Error,
            message: format!(
                "platform tag `{}` matches neither `{linux_platform}` nor `{expected_platform}`",
                name.platform_tag
            ),
        });
    }

    // Sonames shipped inside the wheel satisfy DT_NEEDED between its ELFs.
    let internal: BTreeSet<String> = entries
        .iter()
        .filter(|e| looks_like_elf(&e.data))
        .filter_map(|e| e.name.rsplit('/').next().map(str::to_string))
        .collect();
    let versions = policy.versions(arch)?;

    for entry in entries.iter().filter(|e| looks_like_elf(&e.data)) {
        let info = elfdyn::inspect(&entry.data)?;
        let short = entry.name.rsplit('/').next().unwrap_or(&entry.name);

        if info.machine != arch.e_machine() {
            findings.push(Finding {
                check: "arch",
                severity: Severity::Error,
                message: format!(
                    "{short}: ELF machine {:#x} does not match wheel arch {arch}",
                    info.machine
                ),
            });
            continue;
        }

        // Extension-module suffix consistency with the wheel tags.
        check_ext_suffix(&name, arch, short, &mut findings);

        for needed in &info.needed {
            if needed.starts_with("libpython") {
                findings.push(Finding {
                    check: "libpython",
                    severity: Severity::Error,
                    message: format!(
                        "{short}: links {needed}; manylinux wheels must not link libpython"
                    ),
                });
            } else if !policy.lib_whitelist.contains(needed)
                && !internal.contains(needed)
                && !excludes.contains(needed)
            {
                findings.push(Finding {
                    check: "needed",
                    severity: Severity::Error,
                    message: format!(
                        "{short}: DT_NEEDED {needed} is neither policy-whitelisted nor shipped in the wheel (vendor it)"
                    ),
                });
            }
        }

        for need in &info.version_needs {
            if internal.contains(&need.file) || !policy.lib_whitelist.contains(&need.file) {
                // Internal libs are the wheel's own business; non-whitelisted
                // external libs were already reported above.
                continue;
            }
            let Some((family, version)) = split_symbol_version(&need.version) else {
                continue;
            };
            match versions.get(family) {
                Some(allowed) if allowed.iter().any(|v| v == version) => {}
                Some(_) => findings.push(Finding {
                    check: "symbol-version",
                    severity: Severity::Error,
                    message: format!(
                        "{short}: requires {} from {} (over the {} ceiling)",
                        need.version, need.file, policy.policy
                    ),
                }),
                None => findings.push(Finding {
                    check: "symbol-version",
                    severity: Severity::Error,
                    message: format!(
                        "{short}: requires unknown version family {} from {}",
                        need.version, need.file
                    ),
                }),
            }
        }

        for (lib, symbols) in &policy.blacklist {
            if !info.needed.iter().any(|n| n == lib) {
                continue;
            }
            for sym in &info.undefined {
                if symbols.contains(sym) {
                    findings.push(Finding {
                        check: "blacklist",
                        severity: Severity::Error,
                        message: format!("{short}: references private symbol {sym} of {lib}"),
                    });
                }
            }
        }
    }

    Ok(AuditReport {
        path: path.to_path_buf(),
        findings,
    })
}

/// `GLIBC_2.28` → (`GLIBC`, `2.28`); also handles `CXXABI_FLOAT128` /
/// `CXXABI_TM_1` style names via the known family list.
fn split_symbol_version(version: &str) -> Option<(&str, &str)> {
    for family in ["GLIBCXX", "GLIBC", "CXXABI", "GCC", "LIBATOMIC", "ZLIB"] {
        if let Some(rest) = version.strip_prefix(family) {
            if let Some(v) = rest.strip_prefix('_') {
                return Some((family, v));
            }
        }
    }
    None
}

/// A versioned extension suffix must match the wheel's python tag and arch;
/// abi3 wheels must not carry per-version suffixes.
fn check_ext_suffix(name: &WheelName, arch: TargetArch, file: &str, findings: &mut Vec<Finding>) {
    let Some(rest) = file.split(".cpython-").nth(1) else {
        // Plain .so / .abi3.so: nothing version-specific to check beyond the
        // abi_tag itself (abi3 modules use `.abi3.so`).
        return;
    };
    // rest looks like `312-x86_64-linux-gnu.so`.
    let version_part: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    let expected_py = format!("cp{version_part}");
    if name.abi_tag == "abi3" {
        findings.push(Finding {
            check: "ext-suffix",
            severity: Severity::Error,
            message: format!("{file}: version-specific suffix inside an abi3-tagged wheel"),
        });
    } else if name.python_tag != expected_py {
        findings.push(Finding {
            check: "ext-suffix",
            severity: Severity::Error,
            message: format!(
                "{file}: suffix implies {expected_py} but the wheel is tagged {}",
                name.python_tag
            ),
        });
    }
    let arch_str = arch.as_str();
    if !rest.contains(arch_str) {
        findings.push(Finding {
            check: "ext-suffix",
            severity: Severity::Error,
            message: format!("{file}: suffix does not name the wheel arch {arch_str}"),
        });
    }
}

fn looks_like_elf(data: &[u8]) -> bool {
    data.starts_with(b"\x7fELF")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builtin_policy_parses_with_expected_ceilings() {
        let policy = WheelPolicy::builtin();
        assert_eq!(policy.policy, "manylinux_2_28");
        let x86 = policy.symbol_versions.get("x86_64").unwrap();
        // The GLIBCXX ceiling is 3.4.24 — stricter than el8's own 3.4.25.
        assert!(x86.get("GLIBCXX").unwrap().iter().any(|v| v == "3.4.24"));
        assert!(!x86.get("GLIBCXX").unwrap().iter().any(|v| v == "3.4.25"));
        assert!(x86.get("GLIBC").unwrap().iter().any(|v| v == "2.28"));
        assert!(policy.lib_whitelist.iter().any(|l| l == "libstdc++.so.6"));
        assert!(policy.blacklist.contains_key("libz.so.1"));
        assert_eq!(
            policy.platform_tag(TargetArch::Aarch64),
            "manylinux_2_28_aarch64"
        );
    }

    #[test]
    fn symbol_version_splitting() {
        assert_eq!(split_symbol_version("GLIBC_2.28"), Some(("GLIBC", "2.28")));
        assert_eq!(
            split_symbol_version("GLIBCXX_3.4.24"),
            Some(("GLIBCXX", "3.4.24"))
        );
        assert_eq!(
            split_symbol_version("CXXABI_FLOAT128"),
            Some(("CXXABI", "FLOAT128"))
        );
        assert_eq!(split_symbol_version("OPENSSL_1_1_0"), None);
    }

    #[test]
    fn ext_suffix_consistency() {
        let name = WheelName::parse("demo-1.0-cp312-cp312-linux_x86_64.whl").unwrap();
        let mut findings = Vec::new();
        check_ext_suffix(
            &name,
            TargetArch::X86_64,
            "mod.cpython-312-x86_64-linux-gnu.so",
            &mut findings,
        );
        assert!(findings.is_empty());
        check_ext_suffix(
            &name,
            TargetArch::X86_64,
            "mod.cpython-311-x86_64-linux-gnu.so",
            &mut findings,
        );
        assert_eq!(findings.len(), 1);
        let abi3 = WheelName::parse("demo-1.0-cp39-abi3-linux_aarch64.whl").unwrap();
        let mut findings = Vec::new();
        check_ext_suffix(
            &abi3,
            TargetArch::Aarch64,
            "mod.cpython-39-aarch64-linux-gnu.so",
            &mut findings,
        );
        assert_eq!(findings.len(), 1);
    }
}
