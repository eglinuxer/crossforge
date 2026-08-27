use std::collections::BTreeMap;

use serde::Deserialize;

use crate::error::{Error, Result};
use crate::target::TargetArch;

/// A package source for sysroot generation: the repos to read and the packages
/// to extract (design doc §5.1).
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceDef {
    /// Source id referenced by `BaselineDef::source`, e.g. `rocky-8`.
    pub id: String,
    /// Default base URL; a configured mirror replaces it.
    pub base: String,
    /// Repo URL templates with `{base}` / `{arch}` placeholders.
    pub repos: Vec<String>,
    /// Per-arch repo template overrides for distributions that split
    /// architectures across trees; arches not listed fall back to `repos`.
    #[serde(default)]
    pub arch_repos: std::collections::BTreeMap<String, Vec<String>>,
    /// Package names to extract into the sysroot: the base seed set every
    /// profile starts from.
    pub packages: Vec<String>,
    /// Packages never selected by dependency resolution (a sysroot is a
    /// link-time tree, so the runtime/bootstrap chain is dead weight).
    /// A trailing `*` matches by prefix.
    #[serde(default)]
    pub exclude: Vec<String>,
}

fn default_true() -> bool {
    true
}

/// A sysroot content profile (design doc §5.1): a named set of extra seed
/// packages resolved to their transitive closure, so deep dependency trees
/// (Qt and friends) do not have to be hand-listed. Profiles compose through
/// `include`, which is what keeps `gui` / `x11` / `wayland` / `qt6` tiered
/// instead of one flat blob.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProfileDef {
    pub id: String,
    /// Other profile ids merged into this one.
    #[serde(default)]
    pub include: Vec<String>,
    #[serde(default)]
    pub packages: Vec<String>,
    /// Extra repo URL templates this profile needs on top of the source's
    /// (`{arch}` placeholder). Kept per profile so the base supply chain
    /// stays as narrow as the content requires.
    #[serde(default)]
    pub repos: Vec<String>,
    /// Additional exclusions on top of the source's.
    #[serde(default)]
    pub exclude: Vec<String>,
    /// Resolve the dependency closure. The `minimal` profile keeps the
    /// curated exact list instead (its content is proven and must not drift).
    #[serde(default = "default_true")]
    pub resolve: bool,
}

/// The default profile: exactly the source's curated package list.
pub const DEFAULT_PROFILE: &str = "minimal";

/// A profile expanded through its `include` chain.
#[derive(Debug, Clone, Default)]
pub struct ExpandedProfile {
    pub id: String,
    pub packages: Vec<String>,
    pub repos: Vec<String>,
    pub exclude: Vec<String>,
    pub resolve: bool,
}

impl ExpandedProfile {
    /// Expands this profile's extra repo templates for `arch`.
    pub fn repo_urls(&self, arch: TargetArch) -> Vec<String> {
        self.repos
            .iter()
            .map(|t| t.replace("{arch}", arch.as_str()))
            .collect()
    }
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
    #[serde(default)]
    profile: Vec<ProfileDef>,
}

/// Registry of package sources: ships with the el8 source built in; callers
/// can add or override via [`SourceRegistry::merge_toml`].
#[derive(Debug, Clone, Default)]
pub struct SourceRegistry {
    entries: BTreeMap<String, SourceDef>,
    profiles: BTreeMap<String, ProfileDef>,
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
        for def in file.profile {
            self.profiles.insert(def.id.clone(), def);
        }
        Ok(())
    }

    /// Expands a profile through its `include` chain (depth-first, each
    /// profile contributing once).
    pub fn profile(&self, id: &str) -> Result<ExpandedProfile> {
        let mut out = ExpandedProfile {
            id: id.to_string(),
            resolve: true,
            ..Default::default()
        };
        let mut seen = std::collections::BTreeSet::new();
        self.expand_into(id, &mut out, &mut seen)?;
        Ok(out)
    }

    fn expand_into(
        &self,
        id: &str,
        out: &mut ExpandedProfile,
        seen: &mut std::collections::BTreeSet<String>,
    ) -> Result<()> {
        if !seen.insert(id.to_string()) {
            return Ok(());
        }
        let def = self
            .profiles
            .get(id)
            .ok_or_else(|| Error::UnknownProfile(id.to_string()))?;
        for parent in &def.include {
            self.expand_into(parent, out, seen)?;
        }
        for pkg in &def.packages {
            if !out.packages.contains(pkg) {
                out.packages.push(pkg.clone());
            }
        }
        for repo in &def.repos {
            if !out.repos.contains(repo) {
                out.repos.push(repo.clone());
            }
        }
        for ex in &def.exclude {
            if !out.exclude.contains(ex) {
                out.exclude.push(ex.clone());
            }
        }
        // The requested profile decides; includes only contribute content.
        if id == out.id {
            out.resolve = def.resolve;
        }
        Ok(())
    }

    pub fn profiles(&self) -> impl Iterator<Item = &ProfileDef> {
        self.profiles.values()
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
    fn builtin_contains_the_el8_source() {
        let registry = SourceRegistry::builtin();
        let el8 = registry.get("rocky-8").unwrap();
        assert!(el8.packages.iter().any(|p| p == "glibc-devel"));
        let urls = el8.repo_urls(TargetArch::Aarch64, None);
        assert_eq!(
            urls[0],
            "https://download.rockylinux.org/pub/rocky/8/BaseOS/aarch64/os/"
        );
        // The supply chain is Rocky end to end: no other source ships.
        assert_eq!(registry.iter().count(), 1);
    }

    #[test]
    fn mirror_overrides_base() {
        let registry = SourceRegistry::builtin();
        let el8 = registry.get("rocky-8").unwrap();
        let urls = el8.repo_urls(
            TargetArch::X86_64,
            Some("https://mirror.example.com/rocky/"),
        );
        assert_eq!(
            urls[0],
            "https://mirror.example.com/rocky/8/BaseOS/x86_64/os/"
        );
    }
}
