//! Sysroot generation (design doc §5.1): assemble a baseline sysroot from
//! binary distro packages — no from-source glibc builds — and extract abilists
//! for the audit gate.

use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::baseline::BaselineDef;
use crate::elfdyn;
use crate::error::{Error, Result};
use crate::fetch::Fetcher;
use crate::repodata::{self, RepoPackage};
use crate::rpm;
use crate::source::SourceRegistry;
use crate::target::TargetArch;

/// Libraries whose exported-symbol lists (abilists) are recorded for the audit
/// gate. Looked up under the usual library directories; missing ones are skipped.
const ABILIST_LIBS: &[&str] = &[
    "libc.so.6",
    "libm.so.6",
    "libpthread.so.0",
    "libdl.so.2",
    "librt.so.1",
    "libcrypt.so.1",
    "libgcc_s.so.1",
    "libstdc++.so.6",
];

const LIB_DIRS: &[&str] = &["lib64", "usr/lib64", "lib", "usr/lib"];

/// Relative directory inside the sysroot holding crossforge metadata.
pub const META_DIR: &str = ".crossforge";

#[derive(Debug, Clone, Serialize, serde::Deserialize)]
pub struct PackageRecord {
    pub name: String,
    pub evr: String,
    pub sha256: String,
    pub location: String,
}

#[derive(Debug, Clone, Serialize, serde::Deserialize)]
pub struct SysrootMetadata {
    pub baseline: String,
    pub arch: String,
    pub source: String,
    pub glibc: String,
    pub generator: String,
    pub packages: Vec<PackageRecord>,
    pub abilists: Vec<String>,
}

/// A generated sysroot on disk.
#[derive(Debug, Clone)]
pub struct SysrootArtifact {
    /// Sysroot root directory (pass to `--sysroot`).
    pub root: PathBuf,
    pub metadata: SysrootMetadata,
}

/// Drives sysroot generation: repo metadata → package selection → RPM
/// extraction → layout fixups → abilist extraction → metadata.
#[derive(Debug)]
pub struct SysrootGenerator<'a> {
    fetcher: &'a Fetcher,
    sources: &'a SourceRegistry,
    mirror: Option<String>,
}

impl<'a> SysrootGenerator<'a> {
    pub fn new(fetcher: &'a Fetcher, sources: &'a SourceRegistry, mirror: Option<String>) -> Self {
        Self {
            fetcher,
            sources,
            mirror,
        }
    }

    /// Generates the sysroot for `baseline` × `arch` under `out_dir`.
    ///
    /// Idempotent: an existing sysroot (detected via its metadata file) is
    /// returned as-is without touching the network.
    pub fn generate(
        &self,
        baseline: &BaselineDef,
        arch: TargetArch,
        out_dir: &Path,
    ) -> Result<SysrootArtifact> {
        let meta_path = out_dir.join(META_DIR).join("sysroot.toml");
        if meta_path.is_file() {
            tracing::info!(root = %out_dir.display(), "sysroot already generated, skipping");
            let text = std::fs::read_to_string(&meta_path)?;
            let metadata = toml::from_str(&text).map_err(Error::Registry)?;
            return Ok(SysrootArtifact {
                root: out_dir.to_path_buf(),
                metadata,
            });
        }
        if !baseline.supports(arch) {
            return Err(Error::UnsupportedArch {
                baseline: baseline.alias.clone(),
                arch: arch.to_string(),
            });
        }
        let source = self
            .sources
            .get(&baseline.source)
            .ok_or_else(|| Error::UnknownSource(baseline.source.clone()))?;

        // 1. Collect package lists from every repo.
        let mut available: Vec<(String, RepoPackage)> = Vec::new();
        for repo_url in source.repo_urls(arch, self.mirror.as_deref()) {
            for pkg in self.load_repo(&repo_url)? {
                available.push((repo_url.clone(), pkg));
            }
        }

        // 2. Pick the newest build of each wanted package for this arch.
        let mut picked: Vec<(String, RepoPackage)> = Vec::new();
        for name in &source.packages {
            let best = available
                .iter()
                .filter(|(_, p)| &p.name == name && p.arch == arch.as_str())
                .max_by(|(_, a), (_, b)| a.evr_cmp(b));
            match best {
                Some((repo, pkg)) => picked.push((repo.clone(), pkg.clone())),
                None => {
                    return Err(Error::PackageNotFound {
                        name: name.clone(),
                        arch: arch.to_string(),
                    });
                }
            }
        }

        // 3. Fetch and extract.
        std::fs::create_dir_all(out_dir)?;
        let mut records = Vec::new();
        for (repo, pkg) in &picked {
            let url = format!("{}{}", repo, pkg.location);
            let path = self.fetcher.fetch_cached(&url, &pkg.checksum)?;
            let data = std::fs::read(&path)?;
            let n = rpm::extract_rpm(&data, out_dir)?;
            tracing::info!(package = %pkg.name, evr = %pkg.evr(), entries = n, "extracted");
            records.push(PackageRecord {
                name: pkg.name.clone(),
                evr: pkg.evr(),
                sha256: pkg.checksum.clone(),
                location: pkg.location.clone(),
            });
        }

        // 4. Layout fixups.
        ensure_usrmove_links(out_dir)?;
        relativize_symlinks(out_dir)?;

        // 5. Abilists.
        let abilist_dir = out_dir.join(META_DIR).join("abilists");
        std::fs::create_dir_all(&abilist_dir)?;
        let mut abilists = Vec::new();
        for lib in ABILIST_LIBS {
            let Some(path) = find_lib(out_dir, lib) else {
                continue;
            };
            let data = std::fs::read(&path)?;
            let symbols = elfdyn::exported_symbols(&data)?;
            std::fs::write(
                abilist_dir.join(format!("{lib}.abilist")),
                elfdyn::render_abilist(&symbols),
            )?;
            abilists.push(lib.to_string());
        }

        // 6. Metadata.
        let metadata = SysrootMetadata {
            baseline: baseline.alias.clone(),
            arch: arch.to_string(),
            source: source.id.clone(),
            glibc: baseline.glibc.clone(),
            generator: format!("crossforge {}", env!("CARGO_PKG_VERSION")),
            packages: records,
            abilists,
        };
        std::fs::create_dir_all(meta_path.parent().unwrap())?;
        std::fs::write(&meta_path, toml::to_string_pretty(&metadata)?)?;
        tracing::info!(root = %out_dir.display(), "sysroot generated");
        Ok(SysrootArtifact {
            root: out_dir.to_path_buf(),
            metadata,
        })
    }

    /// Reads one repo's package list via repomd.xml → primary.xml.
    fn load_repo(&self, repo_url: &str) -> Result<Vec<RepoPackage>> {
        let repomd_url = format!("{repo_url}repodata/repomd.xml");
        let repomd = self.fetcher.fetch_bytes(&repomd_url)?;
        let (primary_href, primary_sha256) = repodata::parse_repomd(&repomd)?;
        let primary_url = format!("{repo_url}{primary_href}");
        let primary_path = self.fetcher.fetch_cached(&primary_url, &primary_sha256)?;
        let compressed = std::fs::read(&primary_path)?;
        let xml = rpm::decompress_auto(&compressed)?;
        repodata::parse_primary(&xml)
    }
}

/// el7+/el8 use the UsrMove layout where `/lib{,64}` are symlinks into `/usr`.
/// Packages may materialize real `lib64/` dirs during extraction (rpm records
/// paths through the symlink); merge those into `usr/` and restore the links.
fn ensure_usrmove_links(root: &Path) -> Result<()> {
    for dir in ["lib", "lib64"] {
        let top = root.join(dir);
        let under_usr = root.join("usr").join(dir);
        if top.is_symlink() {
            continue;
        }
        if top.is_dir() {
            std::fs::create_dir_all(&under_usr)?;
            merge_move(&top, &under_usr)?;
            std::fs::remove_dir_all(&top)?;
        }
        if !under_usr.exists() {
            std::fs::create_dir_all(&under_usr)?;
        }
        std::os::unix::fs::symlink(format!("usr/{dir}"), &top)?;
    }
    Ok(())
}

/// Moves the contents of `from` into `to`, merging directories.
fn merge_move(from: &Path, to: &Path) -> Result<()> {
    for entry in std::fs::read_dir(from)? {
        let entry = entry?;
        let src = entry.path();
        let dst = to.join(entry.file_name());
        if src.is_dir() && !src.is_symlink() {
            std::fs::create_dir_all(&dst)?;
            merge_move(&src, &dst)?;
            std::fs::remove_dir(&src)?;
        } else {
            if dst.exists() || dst.is_symlink() {
                std::fs::remove_file(&dst)?;
            }
            std::fs::rename(&src, &dst)?;
        }
    }
    Ok(())
}

/// Rewrites absolute symlink targets to relative ones so the sysroot stays
/// relocatable and safe to walk from outside a chroot.
fn relativize_symlinks(root: &Path) -> Result<()> {
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for entry in std::fs::read_dir(&dir)? {
            let entry = entry?;
            let path = entry.path();
            if path.is_symlink() {
                let target = std::fs::read_link(&path)?;
                if target.is_absolute() {
                    let link_dir = path.parent().unwrap();
                    let depth = link_dir
                        .strip_prefix(root)
                        .map(|p| p.components().count())
                        .unwrap_or(0);
                    let mut rel = PathBuf::new();
                    for _ in 0..depth {
                        rel.push("..");
                    }
                    rel.push(target.strip_prefix("/").unwrap());
                    std::fs::remove_file(&path)?;
                    std::os::unix::fs::symlink(&rel, &path)?;
                }
            } else if path.is_dir() {
                stack.push(path);
            }
        }
    }
    Ok(())
}

fn find_lib(root: &Path, name: &str) -> Option<PathBuf> {
    LIB_DIRS
        .iter()
        .map(|dir| root.join(dir).join(name))
        .find(|p| p.is_file())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn usrmove_merges_toplevel_lib64() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        std::fs::create_dir_all(root.join("lib64")).unwrap();
        std::fs::write(root.join("lib64/libc.so.6"), b"elf").unwrap();
        std::fs::create_dir_all(root.join("usr/lib64")).unwrap();
        std::fs::write(root.join("usr/lib64/libm.so.6"), b"elf").unwrap();
        ensure_usrmove_links(root).unwrap();
        assert!(root.join("lib64").is_symlink());
        assert!(root.join("usr/lib64/libc.so.6").is_file());
        assert!(root.join("usr/lib64/libm.so.6").is_file());
        // Access through the symlink still works.
        assert!(root.join("lib64/libc.so.6").is_file());
    }

    #[test]
    fn absolute_symlinks_become_relative() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        std::fs::create_dir_all(root.join("usr/lib64")).unwrap();
        std::fs::write(root.join("usr/lib64/libfoo.so.1"), b"elf").unwrap();
        std::os::unix::fs::symlink("/usr/lib64/libfoo.so.1", root.join("usr/lib64/libfoo.so"))
            .unwrap();
        relativize_symlinks(root).unwrap();
        let target = std::fs::read_link(root.join("usr/lib64/libfoo.so")).unwrap();
        assert_eq!(target.to_str().unwrap(), "../../usr/lib64/libfoo.so.1");
        assert!(root.join("usr/lib64/libfoo.so").is_file());
    }
}
