//! Packaging (design doc §5.4): turn a built toolchain prefix into a
//! distributable `tar.zst` plus TOML manifest entries.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::engine::{Cmd, Runner, ToolchainArtifact};
use crate::error::Result;
use crate::fetch::hex;

/// One distributable toolchain bundle, as recorded in the manifest.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BundleEntry {
    pub id: String,
    pub gcc: String,
    pub binutils: String,
    pub baseline: String,
    pub arch: String,
    pub triple: String,
    /// Host platform the toolchain binaries run on.
    pub host: String,
    /// Tarball file name (relative to the manifest).
    pub file: String,
    pub sha256: String,
    pub size: u64,
    pub created_unix: u64,
}

/// A packed toolchain on disk.
#[derive(Debug, Clone)]
pub struct PackedToolchain {
    pub tarball: PathBuf,
    pub entry: BundleEntry,
}

/// One distributable python pack, as recorded in the manifest.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PythonEntry {
    /// Pack id, e.g. `cp312-aarch64`.
    pub id: String,
    /// Full CPython version, e.g. `3.12.14`.
    pub version: String,
    pub arch: String,
    pub baseline: String,
    pub file: String,
    pub sha256: String,
    pub size: u64,
    pub created_unix: u64,
}

#[derive(Debug, Serialize, Deserialize)]
struct ManifestFile {
    manifest: ManifestHeader,
    #[serde(default)]
    toolchain: Vec<BundleEntry>,
    #[serde(default)]
    python: Vec<PythonEntry>,
}

#[derive(Debug, Serialize, Deserialize)]
struct ManifestHeader {
    schema: u32,
    generator: String,
}

/// Packs a built toolchain prefix into `<out_dir>/crossforge-toolchain-<id>.tar.zst`
/// and writes a `.toml` sidecar with its manifest entry. Idempotent: an
/// existing tarball with sidecar is returned as-is.
pub fn pack_toolchain(
    artifact: &ToolchainArtifact,
    host: &str,
    out_dir: &Path,
    runner: &impl Runner,
) -> Result<PackedToolchain> {
    std::fs::create_dir_all(out_dir)?;
    let id = artifact.spec.id();
    let file = format!("crossforge-toolchain-{id}.tar.zst");
    let tarball = out_dir.join(&file);
    let sidecar = out_dir.join(format!("{file}.toml"));
    if tarball.is_file() && sidecar.is_file() {
        let entry: BundleEntry = toml::from_str(&std::fs::read_to_string(&sidecar)?)?;
        tracing::info!(tarball = %tarball.display(), "already packed, skipping");
        return Ok(PackedToolchain { tarball, entry });
    }

    let prefix = &artifact.root;
    let parent = prefix.parent().unwrap_or_else(|| Path::new("/"));
    let dir_name = prefix.file_name().unwrap().to_string_lossy().into_owned();
    tracing::info!(tarball = %tarball.display(), "packing");
    runner.exec(
        &Cmd::new("tar")
            .args([
                "--zstd",
                "-C",
                &parent.display().to_string(),
                "-cf",
                &tarball.display().to_string(),
                &dir_name,
            ])
            .log(out_dir.join(format!("{file}.log"))),
    )?;

    let (sha256, size) = file_digest(&tarball)?;
    let entry = BundleEntry {
        id,
        gcc: artifact.spec.gcc.clone(),
        binutils: artifact.spec.binutils.clone(),
        baseline: artifact.spec.baseline.clone(),
        arch: artifact.spec.target.to_string(),
        triple: artifact.spec.target.triple(),
        host: host.to_string(),
        file,
        sha256,
        size,
        created_unix: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0),
    };
    std::fs::write(&sidecar, toml::to_string_pretty(&entry)?)?;
    Ok(PackedToolchain { tarball, entry })
}

/// Packs a built python pack into
/// `<out_dir>/crossforge-python-<id>.tar.zst` with a `.python.toml` sidecar
/// (design doc §9): the release-asset form of a pack, next to the GHCR
/// images. Idempotent.
pub fn pack_python(
    pack: &crate::python::PythonPack,
    baseline: &str,
    out_dir: &Path,
    runner: &impl Runner,
) -> Result<(PathBuf, PythonEntry)> {
    std::fs::create_dir_all(out_dir)?;
    let id = format!("{}-{}", crate::python::pack_tag(&pack.version), pack.arch);
    let file = format!("crossforge-python-{id}.tar.zst");
    let tarball = out_dir.join(&file);
    let sidecar = out_dir.join(format!("{file}.python.toml"));
    if tarball.is_file() && sidecar.is_file() {
        let entry: PythonEntry = toml::from_str(&std::fs::read_to_string(&sidecar)?)?;
        tracing::info!(tarball = %tarball.display(), "already packed, skipping");
        return Ok((tarball, entry));
    }

    // The pack root is the DESTDIR; archive its contents so unpacking
    // anywhere reproduces `opt/_internal/cpython-<version>/`.
    tracing::info!(tarball = %tarball.display(), "packing python pack");
    runner.exec(
        &Cmd::new("tar")
            .args([
                "--zstd",
                "-C",
                &pack.root.display().to_string(),
                "-cf",
                &tarball.display().to_string(),
                ".",
            ])
            .log(out_dir.join(format!("{file}.log"))),
    )?;

    let (sha256, size) = file_digest(&tarball)?;
    let entry = PythonEntry {
        id,
        version: pack.version.clone(),
        arch: pack.arch.to_string(),
        baseline: baseline.to_string(),
        file,
        sha256,
        size,
        created_unix: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0),
    };
    std::fs::write(&sidecar, toml::to_string_pretty(&entry)?)?;
    Ok((tarball, entry))
}

/// Rebuilds `<out_dir>/manifest.toml` from the `.tar.zst.toml` (toolchain)
/// and `.python.toml` (python pack) sidecars.
pub fn write_manifest(out_dir: &Path) -> Result<PathBuf> {
    let mut entries = Vec::new();
    let mut python_entries = Vec::new();
    for dir_entry in std::fs::read_dir(out_dir)? {
        let path = dir_entry?.path();
        let name = path.file_name().unwrap_or_default().to_string_lossy();
        if name.ends_with(".python.toml") {
            python_entries.push(toml::from_str(&std::fs::read_to_string(&path)?)?);
        } else if name.ends_with(".tar.zst.toml") {
            entries.push(toml::from_str(&std::fs::read_to_string(&path)?)?);
        }
    }
    entries.sort_by(|a: &BundleEntry, b: &BundleEntry| a.id.cmp(&b.id));
    python_entries.sort_by(|a: &PythonEntry, b: &PythonEntry| a.id.cmp(&b.id));
    let manifest = ManifestFile {
        manifest: ManifestHeader {
            schema: 1,
            generator: format!("crossforge {}", env!("CARGO_PKG_VERSION")),
        },
        toolchain: entries,
        python: python_entries,
    };
    let path = out_dir.join("manifest.toml");
    std::fs::write(&path, toml::to_string_pretty(&manifest)?)?;
    tracing::info!(manifest = %path.display(), "manifest written");
    Ok(path)
}

/// Streaming sha256 + size of a file.
fn file_digest(path: &Path) -> Result<(String, u64)> {
    let mut file = std::fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 8 * 1024 * 1024];
    let mut size = 0u64;
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
        size += n as u64;
    }
    Ok((hex(&hasher.finalize()), size))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::LocalRunner;
    use crate::spec::ToolchainSpec;
    use crate::{BaselineRegistry, TargetArch};

    #[test]
    fn pack_and_manifest_roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        let prefix = dir.path().join("toolchains/gcc14.2.1-el8-x86_64");
        std::fs::create_dir_all(prefix.join("bin")).unwrap();
        std::fs::write(prefix.join("bin/fake-gcc"), b"#!/bin/true\n").unwrap();

        let registry = BaselineRegistry::builtin();
        let spec = ToolchainSpec::builder()
            .target(TargetArch::X86_64)
            .build(&registry)
            .unwrap();
        let artifact = ToolchainArtifact { root: prefix, spec };
        let out = dir.path().join("dist");
        let packed = pack_toolchain(&artifact, "x86_64-linux", &out, &LocalRunner).unwrap();
        assert!(packed.tarball.is_file());
        assert_eq!(packed.entry.sha256.len(), 64);
        assert!(packed.entry.size > 0);
        assert_eq!(packed.entry.triple, "x86_64-unknown-linux-gnu");

        // Idempotent re-pack returns the sidecar entry.
        let again = pack_toolchain(&artifact, "x86_64-linux", &out, &LocalRunner).unwrap();
        assert_eq!(again.entry.sha256, packed.entry.sha256);

        let manifest = write_manifest(&out).unwrap();
        let text = std::fs::read_to_string(manifest).unwrap();
        assert!(text.contains("schema = 1"));
        assert!(text.contains("gcc14.2.1-el8-x86_64"));
    }
}
