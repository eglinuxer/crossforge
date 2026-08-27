use std::collections::BTreeMap;

use serde::Deserialize;

use crate::error::{Error, Result};
use crate::target::TargetArch;

/// A compatibility baseline: an immutable combination of glibc / libstdc++
/// version ceilings and a package source (design doc §3.2).
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BaselineDef {
    /// Baseline alias, e.g. `el8`.
    pub alias: String,
    /// glibc version ceiling, e.g. `2.28`.
    pub glibc: String,
    /// Kernel headers version, e.g. `4.18`.
    pub kernel_headers: String,
    /// libstdc++ symbol-version ceiling, e.g. `3.4.25` (`GLIBCXX_` prefix omitted).
    pub glibcxx: String,
    /// CXXABI symbol-version ceiling, e.g. `1.3.11`.
    pub cxxabi: String,
    /// Value of `_GLIBCXX_USE_CXX11_ABI`; false for baselines whose library
    /// has no `__cxx11` symbols).
    pub cxx11_abi: bool,
    /// Sysroot package source id, e.g. `rocky-8`.
    pub source: String,
    /// RH nonshared level for this baseline (`80` = RHEL 8, `48` = RHEL 7):
    /// when the toolchain sources carry the gcc-toolset compat patches, the
    /// compat stage uses the RH-built `libstdc++_nonshared<N>.a` directly.
    #[serde(default)]
    pub rh_nonshared: Option<String>,
    /// Target architectures supported by this baseline.
    pub arches: Vec<TargetArch>,
}

impl BaselineDef {
    pub fn supports(&self, arch: TargetArch) -> bool {
        self.arches.contains(&arch)
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RegistryFile {
    baseline: Vec<BaselineDef>,
}

/// Baseline registry: ships with el8 built in; callers can add or override
/// baselines via [`BaselineRegistry::merge_toml`].
#[derive(Debug, Clone, Default)]
pub struct BaselineRegistry {
    entries: BTreeMap<String, BaselineDef>,
}

const BUILTIN_TOML: &str = include_str!("registry/baselines.toml");

impl BaselineRegistry {
    /// Built-in registry (embedded at compile time; a parse failure is a crate
    /// bug, guarded by unit tests).
    pub fn builtin() -> Self {
        Self::from_toml(BUILTIN_TOML).expect("builtin baseline registry must parse")
    }

    /// Builds a registry from TOML text; duplicate aliases are rejected.
    pub fn from_toml(text: &str) -> Result<Self> {
        let mut registry = Self::default();
        registry.merge_toml(text)?;
        Ok(registry)
    }

    /// Merges a TOML document: entries override existing aliases (caller
    /// customization wins); duplicates within a single input are rejected.
    pub fn merge_toml(&mut self, text: &str) -> Result<()> {
        let file: RegistryFile = toml::from_str(text)?;
        let mut seen = std::collections::BTreeSet::new();
        for def in file.baseline {
            if !seen.insert(def.alias.clone()) {
                return Err(Error::DuplicateBaseline(def.alias));
            }
            self.entries.insert(def.alias.clone(), def);
        }
        Ok(())
    }

    pub fn get(&self, alias: &str) -> Option<&BaselineDef> {
        self.entries.get(alias)
    }

    pub fn iter(&self) -> impl Iterator<Item = &BaselineDef> {
        self.entries.values()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builtin_contains_el8_only() {
        let registry = BaselineRegistry::builtin();
        let el8 = registry.get("el8").unwrap();
        assert_eq!(el8.glibc, "2.28");
        assert_eq!(el8.glibcxx, "3.4.25");
        assert!(el8.cxx11_abi);
        assert!(el8.supports(TargetArch::Aarch64));
        assert_eq!(el8.source, "rocky-8");

        // el7 was dropped with its off-chain CentOS source; the registry is
        // still extensible, so a caller can bring its own.
        assert!(registry.get("el7").is_none());
    }

    #[test]
    fn merge_overrides_and_extends() {
        let mut registry = BaselineRegistry::builtin();
        registry
            .merge_toml(
                r#"
                [[baseline]]
                alias = "u20"
                glibc = "2.31"
                kernel_headers = "5.4"
                glibcxx = "3.4.28"
                cxxabi = "1.3.12"
                cxx11_abi = true
                source = "ubuntu-20.04"
                arches = ["x86_64"]
                "#,
            )
            .unwrap();
        let u20 = registry.get("u20").unwrap();
        assert!(u20.supports(TargetArch::X86_64));
        assert!(!u20.supports(TargetArch::Aarch64));
    }

    #[test]
    fn duplicate_alias_in_single_input_rejected() {
        let text = r#"
            [[baseline]]
            alias = "dup"
            glibc = "2.28"
            kernel_headers = "4.18"
            glibcxx = "3.4.25"
            cxxabi = "1.3.11"
            cxx11_abi = true
            source = "x"
            arches = ["x86_64"]

            [[baseline]]
            alias = "dup"
            glibc = "2.31"
            kernel_headers = "5.4"
            glibcxx = "3.4.28"
            cxxabi = "1.3.12"
            cxx11_abi = true
            source = "y"
            arches = ["x86_64"]
        "#;
        assert!(matches!(
            BaselineRegistry::from_toml(text),
            Err(Error::DuplicateBaseline(alias)) if alias == "dup"
        ));
    }
}
