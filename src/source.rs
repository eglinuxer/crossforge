use std::collections::BTreeMap;

use serde::Deserialize;

use crate::error::{Error, Result};
use crate::target::TargetArch;

/// A package source for sysroot generation: the repos to read and the packages
/// to extract (design doc §5.1).
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceDef {
    /// Source id referenced by `BaselineDef::source`, e.g. `almalinux-8`.
    pub id: String,
    /// Default base URL; a configured mirror replaces it.
    pub base: String,
    /// Repo URL templates with `{base}` / `{arch}` placeholders.
    pub repos: Vec<String>,
    /// Per-arch repo template overrides (e.g. CentOS 7 AltArch for aarch64);
    /// arches not listed fall back to `repos`.
    #[serde(default)]
    pub arch_repos: std::collections::BTreeMap<String, Vec<String>>,
    /// Package names to extract into the sysroot.
    pub packages: Vec<String>,
}

impl SourceDef {
    /// Expands repo templates for `arch`, with `mirror` overriding the base URL.
    pub fn repo_urls(&self, arch: TargetArch, mirror: Option<&str>) -> Vec<String> {
        let base = mirror.unwrap_or(&self.base);
        let base = base.trim_end_matches('/');
        let templates = self.arch_repos.get(arch.as_str()).unwrap_or(&self.repos);
        templates
            .iter()
            .map(|template| {
                template
                    .replace("{base}", base)
                    .replace("{arch}", arch.as_str())
            })
            .collect()
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SourcesFile {
    source: Vec<SourceDef>,
}

/// Registry of package sources: ships with el7/el8 sources built in; callers
/// can add or override via [`SourceRegistry::merge_toml`].
#[derive(Debug, Clone, Default)]
pub struct SourceRegistry {
    entries: BTreeMap<String, SourceDef>,
}

const BUILTIN_TOML: &str = include_str!("registry/sources.toml");

impl SourceRegistry {
    /// Built-in registry (embedded at compile time; a parse failure is a crate
    /// bug, guarded by unit tests).
    pub fn builtin() -> Self {
        Self::from_toml(BUILTIN_TOML).expect("builtin source registry must parse")
    }

    pub fn from_toml(text: &str) -> Result<Self> {
        let mut registry = Self::default();
        registry.merge_toml(text)?;
        Ok(registry)
    }

    /// Merges a TOML document: entries override existing ids (caller
    /// customization wins); duplicates within a single input are rejected.
    pub fn merge_toml(&mut self, text: &str) -> Result<()> {
        let file: SourcesFile = toml::from_str(text)?;
        let mut seen = std::collections::BTreeSet::new();
        for def in file.source {
            if !seen.insert(def.id.clone()) {
                return Err(Error::DuplicateSource(def.id));
            }
            self.entries.insert(def.id.clone(), def);
        }
        Ok(())
    }

    pub fn get(&self, id: &str) -> Option<&SourceDef> {
        self.entries.get(id)
    }

    pub fn iter(&self) -> impl Iterator<Item = &SourceDef> {
        self.entries.values()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builtin_contains_el8_and_el7_sources() {
        let registry = SourceRegistry::builtin();
        let el8 = registry.get("almalinux-8").unwrap();
        assert!(el8.packages.iter().any(|p| p == "glibc-devel"));
        let urls = el8.repo_urls(TargetArch::Aarch64, None);
        assert_eq!(
            urls[0],
            "https://repo.almalinux.org/almalinux/8/BaseOS/aarch64/os/"
        );
        assert!(registry.get("centos-7").is_some());
    }

    #[test]
    fn mirror_overrides_base() {
        let registry = SourceRegistry::builtin();
        let el8 = registry.get("almalinux-8").unwrap();
        let urls = el8.repo_urls(TargetArch::X86_64, Some("https://mirror.example.com/alma/"));
        assert_eq!(
            urls[0],
            "https://mirror.example.com/alma/8/BaseOS/x86_64/os/"
        );
    }
}
