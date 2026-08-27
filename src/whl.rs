//! Wheel (PEP 427) archive handling: a minimal zip reader/writer plus the
//! wheel-specific pieces the wheel pipeline needs — filename tag parsing,
//! RECORD verification, and platform-tag rewriting (retagging a `linux_*`
//! wheel to `manylinux_2_28_*` after it passes the policy audit).
//!
//! Only the zip features wheels actually use are implemented: stored and
//! deflate entries, no zip64 (wheels of that size are out of scope), no
//! encryption.

use std::io::{Read, Write};
use std::path::Path;

use crate::error::{Error, Result};

fn zip_err(msg: impl Into<String>) -> Error {
    Error::Archive(format!("zip: {}", msg.into()))
}

/// One file inside a wheel.
#[derive(Debug, Clone)]
pub struct WheelEntry {
    pub name: String,
    pub data: Vec<u8>,
    /// Unix mode bits from the external attributes (0 when absent).
    pub mode: u32,
}

/// Parsed `distribution-version[-build]-python-abi-platform.whl` name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WheelName {
    pub distribution: String,
    pub version: String,
    pub build: Option<String>,
    pub python_tag: String,
    pub abi_tag: String,
    pub platform_tag: String,
}

impl WheelName {
    pub fn parse(file_name: &str) -> Result<Self> {
        let stem = file_name
            .strip_suffix(".whl")
            .ok_or_else(|| zip_err(format!("not a wheel filename: {file_name}")))?;
        let parts: Vec<&str> = stem.split('-').collect();
        match parts.len() {
            5 => Ok(Self {
                distribution: parts[0].to_string(),
                version: parts[1].to_string(),
                build: None,
                python_tag: parts[2].to_string(),
                abi_tag: parts[3].to_string(),
                platform_tag: parts[4].to_string(),
            }),
            6 => Ok(Self {
                distribution: parts[0].to_string(),
                version: parts[1].to_string(),
                build: Some(parts[2].to_string()),
                python_tag: parts[3].to_string(),
                abi_tag: parts[4].to_string(),
                platform_tag: parts[5].to_string(),
            }),
            n => Err(zip_err(format!(
                "wheel filename has {n} dash-separated fields (want 5 or 6): {file_name}"
            ))),
        }
    }

    pub fn file_name(&self) -> String {
        match &self.build {
            Some(build) => format!(
                "{}-{}-{}-{}-{}-{}.whl",
                self.distribution,
                self.version,
                build,
                self.python_tag,
                self.abi_tag,
                self.platform_tag
            ),
            None => format!(
                "{}-{}-{}-{}-{}.whl",
                self.distribution, self.version, self.python_tag, self.abi_tag, self.platform_tag
            ),
        }
    }
}

/// Reads every entry of a wheel into memory.
pub fn read_wheel(path: &Path) -> Result<Vec<WheelEntry>> {
    let data = std::fs::read(path)?;
    read_zip(&data)
}

fn u16at(data: &[u8], off: usize) -> Result<u16> {
    Ok(u16::from_le_bytes(
        data.get(off..off + 2)
            .ok_or_else(|| zip_err("truncated"))?
            .try_into()
            .unwrap(),
    ))
}

fn u32at(data: &[u8], off: usize) -> Result<u32> {
    Ok(u32::from_le_bytes(
        data.get(off..off + 4)
            .ok_or_else(|| zip_err("truncated"))?
            .try_into()
            .unwrap(),
    ))
}

const EOCD_SIG: u32 = 0x0605_4b50;
const CDIR_SIG: u32 = 0x0201_4b50;
const LOCAL_SIG: u32 = 0x0403_4b50;

fn read_zip(data: &[u8]) -> Result<Vec<WheelEntry>> {
    // End-of-central-directory: scan back over the (possibly present) comment.
    let mut eocd = None;
    let lo = data.len().saturating_sub(22 + 65535);
    for off in (lo..data.len().saturating_sub(21)).rev() {
        if u32at(data, off)? == EOCD_SIG {
            eocd = Some(off);
            break;
        }
    }
    let eocd = eocd.ok_or_else(|| zip_err("no end-of-central-directory record"))?;
    let entries = u16at(data, eocd + 10)? as usize;
    let mut off = u32at(data, eocd + 16)? as usize;

    let mut out = Vec::with_capacity(entries);
    for _ in 0..entries {
        if u32at(data, off)? != CDIR_SIG {
            return Err(zip_err("bad central directory signature"));
        }
        let method = u16at(data, off + 10)?;
        let csize = u32at(data, off + 20)? as usize;
        let usize_ = u32at(data, off + 24)? as usize;
        let name_len = u16at(data, off + 28)? as usize;
        let extra_len = u16at(data, off + 30)? as usize;
        let comment_len = u16at(data, off + 32)? as usize;
        let external = u32at(data, off + 38)?;
        let local_off = u32at(data, off + 42)? as usize;
        let name = String::from_utf8_lossy(
            data.get(off + 46..off + 46 + name_len)
                .ok_or_else(|| zip_err("truncated name"))?,
        )
        .into_owned();

        // Local header gives the actual data offset (its name/extra lengths
        // can differ from the central directory's).
        if u32at(data, local_off)? != LOCAL_SIG {
            return Err(zip_err("bad local header signature"));
        }
        let l_name = u16at(data, local_off + 26)? as usize;
        let l_extra = u16at(data, local_off + 28)? as usize;
        let data_off = local_off + 30 + l_name + l_extra;
        let raw = data
            .get(data_off..data_off + csize)
            .ok_or_else(|| zip_err("truncated entry data"))?;
        let content = match method {
            0 => raw.to_vec(),
            8 => {
                let mut decoder = flate2::read::DeflateDecoder::new(raw);
                let mut buf = Vec::with_capacity(usize_);
                decoder
                    .read_to_end(&mut buf)
                    .map_err(|e| zip_err(format!("inflate {name}: {e}")))?;
                buf
            }
            m => return Err(zip_err(format!("unsupported compression method {m}"))),
        };
        if !name.ends_with('/') {
            out.push(WheelEntry {
                name,
                data: content,
                mode: external >> 16,
            });
        }
        off += 46 + name_len + extra_len + comment_len;
    }
    Ok(out)
}

/// Writes entries as a wheel zip (deflate, fixed 1980-01-01 timestamps for
/// reproducibility, mode bits preserved in the external attributes).
pub fn write_wheel(path: &Path, entries: &[WheelEntry]) -> Result<()> {
    const DOS_DATE: u16 = 0x0021; // 1980-01-01
    let mut zip: Vec<u8> = Vec::new();
    let mut central: Vec<u8> = Vec::new();
    for entry in entries {
        let mut crc = flate2::Crc::new();
        crc.update(&entry.data);
        let crc = crc.sum();
        let mut encoder =
            flate2::write::DeflateEncoder::new(Vec::new(), flate2::Compression::default());
        encoder
            .write_all(&entry.data)
            .and_then(|_| encoder.finish())
            .map_err(|e| zip_err(format!("deflate {}: {e}", entry.name)))
            .map(|compressed| {
                let local_off = zip.len() as u32;
                let name = entry.name.as_bytes();
                zip.extend_from_slice(&LOCAL_SIG.to_le_bytes());
                zip.extend_from_slice(&20u16.to_le_bytes()); // version needed
                zip.extend_from_slice(&0u16.to_le_bytes()); // flags
                zip.extend_from_slice(&8u16.to_le_bytes()); // deflate
                zip.extend_from_slice(&0u16.to_le_bytes()); // time
                zip.extend_from_slice(&DOS_DATE.to_le_bytes());
                zip.extend_from_slice(&crc.to_le_bytes());
                zip.extend_from_slice(&(compressed.len() as u32).to_le_bytes());
                zip.extend_from_slice(&(entry.data.len() as u32).to_le_bytes());
                zip.extend_from_slice(&(name.len() as u16).to_le_bytes());
                zip.extend_from_slice(&0u16.to_le_bytes()); // extra len
                zip.extend_from_slice(name);
                zip.extend_from_slice(&compressed);

                central.extend_from_slice(&CDIR_SIG.to_le_bytes());
                central.extend_from_slice(&0x031eu16.to_le_bytes()); // made by: unix
                central.extend_from_slice(&20u16.to_le_bytes());
                central.extend_from_slice(&0u16.to_le_bytes());
                central.extend_from_slice(&8u16.to_le_bytes());
                central.extend_from_slice(&0u16.to_le_bytes());
                central.extend_from_slice(&DOS_DATE.to_le_bytes());
                central.extend_from_slice(&crc.to_le_bytes());
                central.extend_from_slice(&(compressed.len() as u32).to_le_bytes());
                central.extend_from_slice(&(entry.data.len() as u32).to_le_bytes());
                central.extend_from_slice(&(name.len() as u16).to_le_bytes());
                central.extend_from_slice(&0u16.to_le_bytes()); // extra
                central.extend_from_slice(&0u16.to_le_bytes()); // comment
                central.extend_from_slice(&0u16.to_le_bytes()); // disk
                central.extend_from_slice(&0u16.to_le_bytes()); // internal attrs
                central.extend_from_slice(&(entry.mode << 16).to_le_bytes());
                central.extend_from_slice(&local_off.to_le_bytes());
                central.extend_from_slice(name);
            })?;
    }
    let cdir_off = zip.len() as u32;
    zip.extend_from_slice(&central);
    zip.extend_from_slice(&EOCD_SIG.to_le_bytes());
    zip.extend_from_slice(&0u16.to_le_bytes()); // disk
    zip.extend_from_slice(&0u16.to_le_bytes()); // cdir disk
    zip.extend_from_slice(&(entries.len() as u16).to_le_bytes());
    zip.extend_from_slice(&(entries.len() as u16).to_le_bytes());
    zip.extend_from_slice(&(central.len() as u32).to_le_bytes());
    zip.extend_from_slice(&cdir_off.to_le_bytes());
    zip.extend_from_slice(&0u16.to_le_bytes()); // comment len
    std::fs::write(path, zip)?;
    Ok(())
}

/// `sha256=<urlsafe-b64-nopad>` as RECORD uses.
pub fn record_hash(data: &[u8]) -> String {
    use sha2::Digest;
    let digest = sha2::Sha256::digest(data);
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut b64 = String::new();
    for chunk in digest.chunks(3) {
        let b = [
            chunk[0],
            chunk.get(1).copied().unwrap_or(0),
            chunk.get(2).copied().unwrap_or(0),
        ];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        for i in 0..4 {
            if i <= chunk.len() {
                b64.push(ALPHABET[((n >> (18 - 6 * i)) & 0x3f) as usize] as char);
            }
        }
    }
    format!("sha256={b64}")
}

/// Verifies every RECORD hash; returns the entry names that fail.
pub fn verify_record(entries: &[WheelEntry]) -> Result<Vec<String>> {
    let record = entries
        .iter()
        .find(|e| e.name.ends_with(".dist-info/RECORD"))
        .ok_or_else(|| zip_err("wheel has no RECORD"))?;
    let listed: std::collections::BTreeMap<&str, &str> = std::str::from_utf8(&record.data)
        .map_err(|_| zip_err("RECORD is not UTF-8"))?
        .lines()
        .filter_map(|line| {
            let mut fields = line.split(',');
            Some((fields.next()?, fields.next()?))
        })
        .collect();
    let mut bad = Vec::new();
    for entry in entries {
        if entry.name.ends_with(".dist-info/RECORD") {
            continue;
        }
        match listed.get(entry.name.as_str()) {
            Some(hash) if !hash.is_empty() => {
                if record_hash(&entry.data) != *hash {
                    bad.push(entry.name.clone());
                }
            }
            _ => bad.push(entry.name.clone()),
        }
    }
    Ok(bad)
}

/// Rewrites the platform tag of a built wheel (e.g. `linux_aarch64` →
/// `manylinux_2_28_aarch64`): updates the `Tag:` lines in `WHEEL`, refreshes
/// its RECORD row, and writes the renamed wheel next to the original.
/// Returns the new path.
pub fn retag_platform(path: &Path, new_platform: &str) -> Result<std::path::PathBuf> {
    let file_name = path
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or_else(|| zip_err("bad wheel path"))?;
    let mut name = WheelName::parse(file_name)?;
    if name.platform_tag == new_platform {
        return Ok(path.to_path_buf());
    }
    let old_platform = std::mem::replace(&mut name.platform_tag, new_platform.to_string());

    let mut entries = read_wheel(path)?;
    let wheel_idx = entries
        .iter()
        .position(|e| e.name.ends_with(".dist-info/WHEEL"))
        .ok_or_else(|| zip_err("wheel has no WHEEL metadata"))?;
    let text = String::from_utf8_lossy(&entries[wheel_idx].data).into_owned();
    let rewritten: String = text
        .lines()
        .map(|line| {
            if line.starts_with("Tag:") {
                line.replace(&old_platform, new_platform)
            } else {
                line.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
        + "\n";
    entries[wheel_idx].data = rewritten.into_bytes();

    let wheel_name = entries[wheel_idx].name.clone();
    let new_hash = record_hash(&entries[wheel_idx].data);
    let new_len = entries[wheel_idx].data.len();
    if let Some(record) = entries
        .iter_mut()
        .find(|e| e.name.ends_with(".dist-info/RECORD"))
    {
        let text = String::from_utf8_lossy(&record.data).into_owned();
        let rewritten: String = text
            .lines()
            .map(|line| {
                if line.starts_with(&format!("{wheel_name},")) {
                    format!("{wheel_name},{new_hash},{new_len}")
                } else {
                    line.to_string()
                }
            })
            .collect::<Vec<_>>()
            .join("\n")
            + "\n";
        record.data = rewritten.into_bytes();
    }

    let new_path = path.with_file_name(name.file_name());
    write_wheel(&new_path, &entries)?;
    std::fs::remove_file(path)?;
    Ok(new_path)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_entries() -> Vec<WheelEntry> {
        let module = WheelEntry {
            name: "demo/__init__.py".to_string(),
            data: b"answer = 42\n".to_vec(),
            mode: 0o644,
        };
        let wheel_meta = WheelEntry {
            name: "demo-1.0.dist-info/WHEEL".to_string(),
            data: b"Wheel-Version: 1.0\nTag: cp312-cp312-linux_x86_64\n".to_vec(),
            mode: 0o644,
        };
        let record = WheelEntry {
            name: "demo-1.0.dist-info/RECORD".to_string(),
            data: format!(
                "demo/__init__.py,{},{}\ndemo-1.0.dist-info/WHEEL,{},{}\ndemo-1.0.dist-info/RECORD,,\n",
                record_hash(&module.data),
                module.data.len(),
                record_hash(&wheel_meta.data),
                wheel_meta.data.len(),
            )
            .into_bytes(),
            mode: 0o644,
        };
        vec![module, wheel_meta, record]
    }

    #[test]
    fn zip_roundtrip_preserves_entries() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("demo-1.0-cp312-cp312-linux_x86_64.whl");
        let entries = sample_entries();
        write_wheel(&path, &entries).unwrap();
        let back = read_wheel(&path).unwrap();
        assert_eq!(back.len(), entries.len());
        assert_eq!(back[0].name, "demo/__init__.py");
        assert_eq!(back[0].data, entries[0].data);
        assert_eq!(back[0].mode, 0o644);
        assert!(verify_record(&back).unwrap().is_empty());
    }

    #[test]
    fn wheel_name_parses_and_rebuilds() {
        let name = WheelName::parse("demo-1.0-cp312-cp312-linux_x86_64.whl").unwrap();
        assert_eq!(name.python_tag, "cp312");
        assert_eq!(name.platform_tag, "linux_x86_64");
        assert_eq!(name.file_name(), "demo-1.0-cp312-cp312-linux_x86_64.whl");
        assert!(WheelName::parse("demo-1.0.tar.gz").is_err());
    }

    #[test]
    fn retag_rewrites_wheel_and_record() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("demo-1.0-cp312-cp312-linux_x86_64.whl");
        write_wheel(&path, &sample_entries()).unwrap();
        let new_path = retag_platform(&path, "manylinux_2_28_x86_64").unwrap();
        assert!(
            new_path
                .file_name()
                .unwrap()
                .to_string_lossy()
                .contains("manylinux_2_28_x86_64")
        );
        assert!(!path.exists());
        let entries = read_wheel(&new_path).unwrap();
        let wheel_meta = entries.iter().find(|e| e.name.ends_with("WHEEL")).unwrap();
        let text = String::from_utf8_lossy(&wheel_meta.data);
        assert!(text.contains("Tag: cp312-cp312-manylinux_2_28_x86_64"));
        assert!(verify_record(&entries).unwrap().is_empty());
    }

    #[test]
    fn record_hash_matches_known_vector() {
        // sha256("") urlsafe-b64-nopad
        assert_eq!(
            record_hash(b""),
            "sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU"
        );
    }
}
