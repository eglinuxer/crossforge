//! Wheel vendoring (design doc §9, milestone M8): the auditwheel-repair
//! counterpart. Non-whitelisted shared libraries a wheel links are copied
//! into a `<distribution>.libs/` directory inside the wheel, renamed with a
//! content-hash suffix (so two wheels vendoring different builds of the same
//! soname can coexist in one process), and every ELF is rewritten natively
//! ([`crate::elfpatch`]): vendored libraries get the new DT_SONAME +
//! `$ORIGIN` DT_RUNPATH, extension modules get their DT_NEEDED renamed and a
//! DT_RUNPATH pointing at the `.libs` directory. Transitive dependencies of
//! vendored libraries are resolved from the same search paths.
//!
//! Vendoring never replaces the audit: the caller re-audits the vendored
//! wheel and only a passing wheel gets retagged.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use crate::elfdyn;
use crate::elfpatch::{self, PatchOps};
use crate::error::{Error, Result};
use crate::target::TargetArch;
use crate::wheelaudit::WheelPolicy;
use crate::whl::{self, WheelEntry, WheelName, record_hash};

/// One vendored library.
#[derive(Debug, Clone)]
pub struct VendoredLib {
    /// Original soname, e.g. `libssl.so.1.1`.
    pub soname: String,
    /// Hashed name it was vendored under, e.g. `libssl-1a2b3c4d.so.1.1`.
    pub vendored_name: String,
    /// Where the library was found.
    pub source: PathBuf,
}

/// Copies every non-whitelisted dependency into the wheel and rewrites the
/// ELFs. `search_paths` must hold target-arch libraries (typically the
/// toolchain sysroot's `usr/lib64`); `exclude` sonames are left untouched
/// (driver-style libraries the runtime provides). Returns the vendored set —
/// empty when the wheel was already clean.
pub fn vendor_wheel(
    wheel: &Path,
    arch: TargetArch,
    policy: &WheelPolicy,
    search_paths: &[PathBuf],
    exclude: &[String],
) -> Result<Vec<VendoredLib>> {
    let file_name = wheel
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or_else(|| Error::Wheel("bad wheel path".to_string()))?;
    let name = WheelName::parse(file_name)?;
    let mut entries = whl::read_wheel(wheel)?;

    let internal: BTreeSet<String> = entries
        .iter()
        .filter(|e| e.data.starts_with(b"\x7fELF"))
        .filter_map(|e| e.name.rsplit('/').next().map(str::to_string))
        .collect();
    let is_external = |soname: &str| -> bool {
        !policy.lib_whitelist.iter().any(|w| w == soname)
            && !internal.contains(soname)
            && !exclude.iter().any(|x| x == soname)
            && !soname.starts_with("libpython")
    };

    // Collect the sonames to vendor: direct wheel dependencies first, then
    // the transitive closure through the vendored libraries themselves.
    let mut wanted: Vec<String> = Vec::new();
    for entry in entries.iter().filter(|e| e.data.starts_with(b"\x7fELF")) {
        for needed in elfdyn::inspect(&entry.data)?.needed {
            if is_external(&needed) && !wanted.contains(&needed) {
                wanted.push(needed);
            }
        }
    }
    if wanted.is_empty() {
        return Ok(Vec::new());
    }

    let mut resolved: BTreeMap<String, (PathBuf, Vec<u8>)> = BTreeMap::new();
    let mut queue = wanted;
    while let Some(soname) = queue.pop() {
        if resolved.contains_key(&soname) {
            continue;
        }
        let path = find_library(&soname, arch, search_paths)?;
        let data = std::fs::read(&path)?;
        for needed in elfdyn::inspect(&data)?.needed {
            if is_external(&needed) && !resolved.contains_key(&needed) {
                queue.push(needed);
            }
        }
        resolved.insert(soname, (path, data));
    }

    // Hashed names for every vendored library.
    let rename: BTreeMap<String, String> = resolved
        .iter()
        .map(|(soname, (_, data))| (soname.clone(), hashed_name(soname, data)))
        .collect();
    let libs_dir = format!("{}.libs", name.distribution);

    // Rewrite and add the vendored libraries.
    let mut vendored = Vec::new();
    for (soname, (path, data)) in &resolved {
        let ops = PatchOps {
            set_soname: Some(rename[soname].clone()),
            replace_needed: rename.clone(),
            set_runpath: Some("$ORIGIN".to_string()),
        };
        let patched = elfpatch::patch_elf(data, &ops)?;
        entries.push(WheelEntry {
            name: format!("{libs_dir}/{}", rename[soname]),
            data: patched,
            mode: 0o755,
        });
        tracing::info!(soname, vendored = %rename[soname], from = %path.display(), "library vendored");
        vendored.push(VendoredLib {
            soname: soname.clone(),
            vendored_name: rename[soname].clone(),
            source: path.clone(),
        });
    }

    // Rewrite the wheel's own ELFs that referenced any vendored soname.
    for entry in &mut entries {
        if !entry.data.starts_with(b"\x7fELF") || entry.name.starts_with(&libs_dir) {
            continue;
        }
        let info = elfdyn::inspect(&entry.data)?;
        if !info.needed.iter().any(|n| rename.contains_key(n)) {
            continue;
        }
        // $ORIGIN-relative path from the module's directory up to .libs.
        let depth = entry.name.matches('/').count();
        let ups = "../".repeat(depth);
        let ops = PatchOps {
            set_soname: None,
            replace_needed: rename.clone(),
            set_runpath: Some(format!("$ORIGIN/{ups}{libs_dir}")),
        };
        entry.data = elfpatch::patch_elf(&entry.data, &ops)?;
    }

    // Vendor manifest into dist-info: what was vendored, from where, and
    // the original content hash — the supply-chain record auditwheel keeps
    // as an SBOM.
    let dist_info = entries
        .iter()
        .filter_map(|e| e.name.split_once('/').map(|(d, _)| d.to_string()))
        .find(|d| d.ends_with(".dist-info"))
        .ok_or_else(|| Error::Wheel("wheel has no dist-info directory".to_string()))?;
    let manifest = vendor_manifest(&vendored, &resolved)?;
    entries.push(WheelEntry {
        name: format!("{dist_info}/crossforge-vendor.toml"),
        data: manifest.into_bytes(),
        mode: 0o644,
    });

    rebuild_record(&mut entries)?;
    whl::write_wheel(wheel, &entries)?;
    Ok(vendored)
}

/// TOML manifest describing the vendored libraries.
fn vendor_manifest(
    vendored: &[VendoredLib],
    resolved: &BTreeMap<String, (PathBuf, Vec<u8>)>,
) -> Result<String> {
    use sha2::Digest;
    let mut out =
        String::from("# Libraries vendored into this wheel by crossforge (design doc §9, M8).\n");
    out.push_str(&format!(
        "generator = \"crossforge {}\"\n",
        env!("CARGO_PKG_VERSION")
    ));
    for lib in vendored {
        let (_, data) = &resolved[&lib.soname];
        let sha: String = sha2::Sha256::digest(data)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        out.push_str(&format!(
            "\n[[library]]\nsoname = \"{}\"\nvendored_as = \"{}\"\nsource = \"{}\"\nsource_sha256 = \"{sha}\"\n",
            lib.soname,
            lib.vendored_name,
            lib.source.display(),
        ));
    }
    Ok(out)
}

/// `libssl.so.1.1` + content → `libssl-1a2b3c4d.so.1.1`.
fn hashed_name(soname: &str, data: &[u8]) -> String {
    use sha2::Digest;
    let digest = sha2::Sha256::digest(data);
    let tag: String = digest[..4].iter().map(|b| format!("{b:02x}")).collect();
    match soname.find(".so") {
        Some(idx) => format!("{}-{tag}{}", &soname[..idx], &soname[idx..]),
        None => format!("{soname}-{tag}"),
    }
}

fn find_library(soname: &str, arch: TargetArch, search_paths: &[PathBuf]) -> Result<PathBuf> {
    for dir in search_paths {
        let candidate = dir.join(soname);
        if candidate.is_file() {
            let data = std::fs::read(&candidate)?;
            let info = elfdyn::inspect(&data)?;
            if info.machine == arch.e_machine() {
                return Ok(candidate);
            }
            tracing::warn!(
                lib = %candidate.display(),
                "skipping candidate with wrong architecture"
            );
        }
    }
    Err(Error::Wheel(format!(
        "cannot vendor {soname}: not found (target {arch}) in {}",
        search_paths
            .iter()
            .map(|p| p.display().to_string())
            .collect::<Vec<_>>()
            .join(", ")
    )))
}

/// Regenerates RECORD from the actual entries.
fn rebuild_record(entries: &mut [WheelEntry]) -> Result<()> {
    let record_idx = entries
        .iter()
        .position(|e| e.name.ends_with(".dist-info/RECORD"))
        .ok_or_else(|| Error::Wheel("wheel has no RECORD".to_string()))?;
    let record_name = entries[record_idx].name.clone();
    let mut lines: Vec<String> = entries
        .iter()
        .filter(|e| e.name != record_name)
        .map(|e| format!("{},{},{}", e.name, record_hash(&e.data), e.data.len()))
        .collect();
    lines.push(format!("{record_name},,"));
    entries[record_idx].data = (lines.join("\n") + "\n").into_bytes();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hashed_names_keep_the_so_suffix() {
        let name = hashed_name("libssl.so.1.1", b"content");
        assert!(name.starts_with("libssl-"));
        assert!(name.ends_with(".so.1.1"));
        assert_eq!(name.len(), "libssl.so.1.1".len() + 9);
        // Same soname, different content → different name.
        assert_ne!(name, hashed_name("libssl.so.1.1", b"other"));
        assert_eq!(hashed_name("weird", b"x").matches('-').count(), 1);
    }
}
