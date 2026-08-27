//! Minimal RPM package extraction, in pure Rust.
//!
//! An RPM is: a 96-byte lead, a signature header (8-byte aligned), a main
//! header, then the payload — a compressed cpio archive in `newc` format.
//! We skip both headers structurally and sniff the payload compression from
//! its magic bytes, so no header tag parsing is needed.

use std::collections::HashMap;
use std::io::Read;
use std::path::{Component, Path, PathBuf};

use crate::bytes;
use crate::error::{Error, Result};

const LEAD_LEN: usize = 96;
const HEADER_MAGIC: [u8; 3] = [0x8e, 0xad, 0xe8];

fn rpm_err(msg: impl Into<String>) -> Error {
    Error::Rpm(msg.into())
}

/// Extracts an RPM's payload into `dest`. Returns the number of filesystem
/// entries written (files, dirs, symlinks).
pub fn extract_rpm(data: &[u8], dest: &Path) -> Result<usize> {
    extract_rpm_filtered(data, dest, |_| true)
}

/// Extracts only the members `keep` accepts, by their archive-relative path
/// (`usr/share/doc/foo/README`). Used for sysroots, where documentation,
/// locales and target-architecture executables are pure weight: they cannot
/// run on the build host and nothing links against them.
pub fn extract_rpm_filtered(
    data: &[u8],
    dest: &Path,
    keep: impl Fn(&str) -> bool,
) -> Result<usize> {
    let payload = payload_of(data)?;
    let cpio = decompress_auto(payload)?;
    cpio::extract(&cpio, dest, &keep)
}

/// Returns the (still compressed) payload region of an RPM.
fn payload_of(data: &[u8]) -> Result<&[u8]> {
    if data.len() < LEAD_LEN + 16 {
        return Err(rpm_err("file too short"));
    }
    if data[..4] != [0xed, 0xab, 0xee, 0xdb] {
        return Err(rpm_err("bad lead magic"));
    }
    // Signature header is padded to an 8-byte boundary; the main header is not.
    let sig_end = skip_header(data, LEAD_LEN)?;
    let main_start = sig_end + (8 - sig_end % 8) % 8;
    let payload_start = skip_header(data, main_start)?;
    Ok(&data[payload_start..])
}

/// Skips one header structure starting at `offset`, returning the offset past it.
fn skip_header(data: &[u8], offset: usize) -> Result<usize> {
    let h = bytes::slice(data, offset, 16).ok_or_else(|| rpm_err("truncated header"))?;
    if h[..3] != HEADER_MAGIC {
        return Err(rpm_err("bad header magic"));
    }
    let nindex = u32::from_be_bytes([h[8], h[9], h[10], h[11]]) as usize;
    let hsize = u32::from_be_bytes([h[12], h[13], h[14], h[15]]) as usize;
    let end = bytes::span(offset, nindex, 16)
        .and_then(|o| bytes::add(o, 16))
        .and_then(|o| bytes::add(o, hsize))
        .ok_or_else(|| rpm_err("header size overflows"))?;
    if end > data.len() {
        return Err(rpm_err("header extends past end of file"));
    }
    Ok(end)
}

/// Decompresses gzip/xz payloads (the formats used by el7/el8), passing through
/// uncompressed data; zstd (el9+) is reported as unsupported until needed.
pub(crate) fn decompress_auto(data: &[u8]) -> Result<Vec<u8>> {
    match data {
        [0x1f, 0x8b, ..] => {
            let mut out = Vec::new();
            flate2::read::GzDecoder::new(data)
                .read_to_end(&mut out)
                .map_err(|e| rpm_err(format!("gzip payload: {e}")))?;
            Ok(out)
        }
        [0xfd, b'7', b'z', b'X', b'Z', 0x00, ..] => {
            let mut out = Vec::new();
            xz2::read::XzDecoder::new(data)
                .read_to_end(&mut out)
                .map_err(|e| rpm_err(format!("xz payload: {e}")))?;
            Ok(out)
        }
        [0x28, 0xb5, 0x2f, 0xfd, ..] => Err(Error::UnsupportedCompression("zstd".to_string())),
        _ => Ok(data.to_vec()),
    }
}

pub(crate) mod cpio {
    //! `newc` (SVR4) cpio archive extraction.

    use super::*;

    const S_IFMT: u32 = 0o170000;
    const S_IFDIR: u32 = 0o040000;
    const S_IFREG: u32 = 0o100000;
    const S_IFLNK: u32 = 0o120000;

    fn cpio_err(msg: impl Into<String>) -> Error {
        Error::Cpio(msg.into())
    }

    struct Entry<'a> {
        name: &'a str,
        mode: u32,
        nlink: u32,
        ino: u32,
        data: &'a [u8],
        next: usize,
    }

    fn hex_field(data: &[u8], pos: usize, index: usize) -> Result<u32> {
        let start = bytes::add(pos, 6)
            .and_then(|o| bytes::span(o, index, 8))
            .ok_or_else(|| cpio_err("header offset overflows"))?;
        let field = bytes::slice(data, start, 8).ok_or_else(|| cpio_err("truncated header"))?;
        let s = std::str::from_utf8(field).map_err(|_| cpio_err("non-ascii header field"))?;
        u32::from_str_radix(s, 16).map_err(|_| cpio_err("bad hex header field"))
    }

    fn read_entry(data: &[u8], pos: usize) -> Result<Entry<'_>> {
        let magic = bytes::slice(data, pos, 6).ok_or_else(|| cpio_err("truncated magic"))?;
        if magic != b"070701" && magic != b"070702" {
            return Err(cpio_err("bad entry magic"));
        }
        let ino = hex_field(data, pos, 0)?;
        let mode = hex_field(data, pos, 1)?;
        let nlink = hex_field(data, pos, 4)?;
        let filesize = hex_field(data, pos, 6)? as usize;
        let namesize = hex_field(data, pos, 11)? as usize;
        let name_start = bytes::add(pos, 110).ok_or_else(|| cpio_err("entry offset overflows"))?;
        let name_bytes =
            bytes::slice(data, name_start, namesize).ok_or_else(|| cpio_err("truncated name"))?;
        let name_end =
            bytes::add(name_start, namesize).ok_or_else(|| cpio_err("entry offset overflows"))?;
        let name = std::str::from_utf8(&name_bytes[..namesize.saturating_sub(1)])
            .map_err(|_| cpio_err("non-utf8 path"))?;
        let data_start = bytes::add(name_end, (4 - name_end % 4) % 4)
            .ok_or_else(|| cpio_err("entry offset overflows"))?;
        let file_data = bytes::slice(data, data_start, filesize)
            .ok_or_else(|| cpio_err("truncated file data"))?;
        let data_end =
            bytes::add(data_start, filesize).ok_or_else(|| cpio_err("entry offset overflows"))?;
        let next = bytes::add(data_end, (4 - data_end % 4) % 4)
            .ok_or_else(|| cpio_err("entry offset overflows"))?;
        Ok(Entry {
            name,
            mode,
            nlink,
            ino,
            data: file_data,
            next,
        })
    }

    /// Maps an archive member name to a path under `dest`, rejecting absolute
    /// escapes and `..` traversal.
    fn sanitize(name: &str, dest: &Path) -> Result<Option<PathBuf>> {
        let trimmed = name.trim_start_matches("./").trim_start_matches('/');
        if trimmed.is_empty() {
            return Ok(None);
        }
        let rel = Path::new(trimmed);
        for component in rel.components() {
            match component {
                Component::Normal(_) => {}
                Component::CurDir => {}
                _ => return Err(cpio_err(format!("unsafe path in archive: {name}"))),
            }
        }
        Ok(Some(dest.join(rel)))
    }

    pub fn extract(data: &[u8], dest: &Path, keep: &dyn Fn(&str) -> bool) -> Result<usize> {
        std::fs::create_dir_all(dest)?;
        let mut pos = 0;
        let mut written = 0usize;
        // Hard-link groups: members with nlink > 1 carry data only on the
        // last member; earlier ones are recorded and linked once data lands.
        let mut pending_links: HashMap<u32, Vec<PathBuf>> = HashMap::new();
        let mut link_source: HashMap<u32, PathBuf> = HashMap::new();
        loop {
            let entry = read_entry(data, pos)?;
            pos = entry.next;
            if entry.name == "TRAILER!!!" {
                break;
            }
            if !keep(entry.name.trim_start_matches("./").trim_start_matches('/')) {
                continue;
            }
            let Some(path) = sanitize(entry.name, dest)? else {
                continue;
            };
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent)?;
            }
            match entry.mode & S_IFMT {
                S_IFDIR => {
                    std::fs::create_dir_all(&path)?;
                    written += 1;
                }
                S_IFLNK => {
                    let target = std::str::from_utf8(entry.data)
                        .map_err(|_| cpio_err("non-utf8 symlink target"))?;
                    // A UsrMove link (`lib64` -> `usr/lib64`) can arrive
                    // after another package already materialized the same
                    // path as a real directory. Replacing it would discard
                    // what was extracted there; leaving it alone is safe
                    // because the layout fixups merge and relink afterwards.
                    if path.is_dir() && !path.is_symlink() {
                        tracing::debug!(
                            path = %path.display(),
                            "symlink member skipped: path already extracted as a directory"
                        );
                        continue;
                    }
                    let _ = std::fs::remove_file(&path);
                    std::os::unix::fs::symlink(target, &path).map_err(|e| {
                        cpio_err(format!("symlink {} -> {target}: {e}", path.display()))
                    })?;
                    written += 1;
                }
                S_IFREG => {
                    if entry.data.is_empty() && entry.nlink > 1 {
                        if let Some(source) = link_source.get(&entry.ino) {
                            let _ = std::fs::remove_file(&path);
                            std::fs::hard_link(source, &path).map_err(|e| {
                                cpio_err(format!(
                                    "hard link {} -> {}: {e}",
                                    path.display(),
                                    source.display()
                                ))
                            })?;
                            written += 1;
                        } else {
                            pending_links.entry(entry.ino).or_default().push(path);
                        }
                        continue;
                    }
                    std::fs::write(&path, entry.data)?;
                    set_mode(&path, entry.mode & 0o7777)?;
                    written += 1;
                    if entry.nlink > 1 {
                        for link in pending_links.remove(&entry.ino).unwrap_or_default() {
                            let _ = std::fs::remove_file(&link);
                            std::fs::hard_link(&path, &link).map_err(|e| {
                                cpio_err(format!(
                                    "hard link {} -> {}: {e}",
                                    link.display(),
                                    path.display()
                                ))
                            })?;
                            written += 1;
                        }
                        link_source.insert(entry.ino, path);
                    }
                }
                // Device nodes, fifos, sockets: irrelevant for a sysroot.
                _ => {}
            }
        }
        // Hard-link groups where no member carried data (all-zero-size files).
        for (_, paths) in pending_links {
            for path in paths {
                std::fs::write(&path, b"")?;
                written += 1;
            }
        }
        Ok(written)
    }

    fn set_mode(path: &Path, mode: u32) -> Result<()> {
        use std::os::unix::fs::PermissionsExt;
        // Ensure the owner can always read/write what we extracted.
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode | 0o600))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Builds a single newc entry.
    fn newc_entry(name: &str, mode: u32, ino: u32, nlink: u32, data: &[u8]) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(b"070701");
        let namesize = name.len() + 1;
        for value in [
            ino,
            mode,
            0,
            0,
            nlink,
            0,
            data.len() as u32,
            0,
            0,
            0,
            0,
            namesize as u32,
            0,
        ] {
            out.extend_from_slice(format!("{value:08x}").as_bytes());
        }
        out.extend_from_slice(name.as_bytes());
        out.push(0);
        while out.len() % 4 != 0 {
            out.push(0);
        }
        out.extend_from_slice(data);
        while out.len() % 4 != 0 {
            out.push(0);
        }
        out
    }

    fn archive(entries: &[Vec<u8>]) -> Vec<u8> {
        let mut out = Vec::new();
        for e in entries {
            out.extend_from_slice(e);
        }
        out.extend_from_slice(&newc_entry("TRAILER!!!", 0, 0, 1, b""));
        out
    }

    #[test]
    fn cpio_roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        let data = archive(&[
            newc_entry("./usr/include", 0o040755, 1, 2, b""),
            newc_entry("./usr/include/stdio.h", 0o100644, 2, 1, b"int printf();\n"),
            newc_entry("./usr/lib64/libc.so", 0o120777, 3, 1, b"libc.so.6"),
        ]);
        let n = cpio::extract(&data, dir.path(), &|_| true).unwrap();
        assert_eq!(n, 3);
        let content = std::fs::read_to_string(dir.path().join("usr/include/stdio.h")).unwrap();
        assert_eq!(content, "int printf();\n");
        let link = std::fs::read_link(dir.path().join("usr/lib64/libc.so")).unwrap();
        assert_eq!(link.to_str().unwrap(), "libc.so.6");
    }

    #[test]
    fn cpio_hardlinks() {
        let dir = tempfile::tempdir().unwrap();
        let data = archive(&[
            newc_entry("./a", 0o100644, 7, 2, b""),
            newc_entry("./b", 0o100644, 7, 2, b"shared"),
        ]);
        cpio::extract(&data, dir.path(), &|_| true).unwrap();
        assert_eq!(
            std::fs::read_to_string(dir.path().join("a")).unwrap(),
            "shared"
        );
        assert_eq!(
            std::fs::read_to_string(dir.path().join("b")).unwrap(),
            "shared"
        );
    }

    #[test]
    fn cpio_rejects_traversal() {
        let dir = tempfile::tempdir().unwrap();
        let data = archive(&[newc_entry("./../evil", 0o100644, 1, 1, b"x")]);
        assert!(matches!(
            cpio::extract(&data, dir.path(), &|_| true),
            Err(Error::Cpio(_))
        ));
    }

    #[test]
    fn rpm_payload_extraction() {
        use std::io::Write;
        // lead
        let mut rpm = vec![0u8; LEAD_LEN];
        rpm[..4].copy_from_slice(&[0xed, 0xab, 0xee, 0xdb]);
        // signature header: 0 entries, 4 bytes of data (forces 8-byte padding)
        rpm.extend_from_slice(&[0x8e, 0xad, 0xe8, 0x01, 0, 0, 0, 0]);
        rpm.extend_from_slice(&0u32.to_be_bytes());
        rpm.extend_from_slice(&4u32.to_be_bytes());
        rpm.extend_from_slice(&[0xaa; 4]);
        while (rpm.len()) % 8 != 0 {
            rpm.push(0);
        }
        // main header: 0 entries, 0 data
        rpm.extend_from_slice(&[0x8e, 0xad, 0xe8, 0x01, 0, 0, 0, 0]);
        rpm.extend_from_slice(&0u32.to_be_bytes());
        rpm.extend_from_slice(&0u32.to_be_bytes());
        // gzip-compressed cpio payload
        let cpio_data = archive(&[newc_entry("./etc/hello", 0o100644, 1, 1, b"world")]);
        let mut gz = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::fast());
        gz.write_all(&cpio_data).unwrap();
        rpm.extend_from_slice(&gz.finish().unwrap());

        let dir = tempfile::tempdir().unwrap();
        let n = extract_rpm(&rpm, dir.path()).unwrap();
        assert_eq!(n, 1);
        assert_eq!(
            std::fs::read_to_string(dir.path().join("etc/hello")).unwrap(),
            "world"
        );
    }
}
