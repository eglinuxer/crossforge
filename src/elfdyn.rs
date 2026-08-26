//! Minimal ELF dynamic-symbol reader for abilist extraction.
//!
//! Hand-rolled and deliberately narrow: 64-bit little-endian ELF only (the
//! x86_64 / aarch64 targets we support), reading `.dynsym`, `.dynstr`,
//! `.gnu.version` and `.gnu.version_d` to list exported symbols with their
//! definition versions.

use crate::error::{Error, Result};

const SHT_DYNSYM: u32 = 11;
const SHT_GNU_VERDEF: u32 = 0x6ffffffd;
const SHT_GNU_VERSYM: u32 = 0x6fffffff;
const SHN_UNDEF: u16 = 0;
const STB_LOCAL: u8 = 0;

fn elf_err(msg: impl Into<String>) -> Error {
    Error::Elf(msg.into())
}

/// One exported dynamic symbol, e.g. `printf` @ `GLIBC_2.2.5`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct DynSymbol {
    /// Definition version, `None` for unversioned exports.
    pub version: Option<String>,
    pub name: String,
}

struct Section {
    sh_type: u32,
    offset: usize,
    size: usize,
    link: u32,
    entsize: usize,
}

fn u16le(d: &[u8], at: usize) -> Result<u16> {
    Ok(u16::from_le_bytes(
        d.get(at..at + 2)
            .ok_or_else(|| elf_err("truncated"))?
            .try_into()
            .unwrap(),
    ))
}

fn u32le(d: &[u8], at: usize) -> Result<u32> {
    Ok(u32::from_le_bytes(
        d.get(at..at + 4)
            .ok_or_else(|| elf_err("truncated"))?
            .try_into()
            .unwrap(),
    ))
}

fn u64le(d: &[u8], at: usize) -> Result<u64> {
    Ok(u64::from_le_bytes(
        d.get(at..at + 8)
            .ok_or_else(|| elf_err("truncated"))?
            .try_into()
            .unwrap(),
    ))
}

fn sections(data: &[u8]) -> Result<Vec<Section>> {
    if data.len() < 64 || &data[..4] != b"\x7fELF" {
        return Err(elf_err("not an ELF file"));
    }
    if data[4] != 2 || data[5] != 1 {
        return Err(elf_err("only 64-bit little-endian ELF is supported"));
    }
    let shoff = u64le(data, 0x28)? as usize;
    let shentsize = u16le(data, 0x3a)? as usize;
    let shnum = u16le(data, 0x3c)? as usize;
    if shentsize < 64 {
        return Err(elf_err("bad section header entry size"));
    }
    let mut out = Vec::with_capacity(shnum);
    for i in 0..shnum {
        let base = shoff + i * shentsize;
        out.push(Section {
            sh_type: u32le(data, base + 4)?,
            offset: u64le(data, base + 24)? as usize,
            size: u64le(data, base + 32)? as usize,
            link: u32le(data, base + 40)?,
            entsize: u64le(data, base + 56)? as usize,
        });
    }
    Ok(out)
}

fn section_data<'a>(data: &'a [u8], s: &Section) -> Result<&'a [u8]> {
    data.get(s.offset..s.offset + s.size)
        .ok_or_else(|| elf_err("section outside file"))
}

fn cstr(strtab: &[u8], at: usize) -> Result<String> {
    let rest = strtab
        .get(at..)
        .ok_or_else(|| elf_err("string offset outside strtab"))?;
    let end = rest
        .iter()
        .position(|c| *c == 0)
        .ok_or_else(|| elf_err("unterminated string"))?;
    String::from_utf8(rest[..end].to_vec()).map_err(|_| elf_err("non-utf8 symbol name"))
}

/// Parses `.gnu.version_d` into a map from version index to version name.
fn verdef_map(data: &[u8], s: &Section, strtab: &[u8]) -> Result<Vec<(u16, String)>> {
    let d = section_data(data, s)?;
    let mut out = Vec::new();
    let mut pos = 0usize;
    loop {
        let ndx = u16le(d, pos + 4)?;
        let aux_offset = u32le(d, pos + 12)? as usize;
        let next = u32le(d, pos + 16)? as usize;
        // First aux entry holds the version name.
        let name_offset = u32le(d, pos + aux_offset)? as usize;
        out.push((ndx, cstr(strtab, name_offset)?));
        if next == 0 {
            break;
        }
        pos += next;
    }
    Ok(out)
}

const SHT_SYMTAB: u32 = 2;
const SHT_DYNAMIC: u32 = 6;
const SHT_GNU_VERNEED: u32 = 0x6ffffffe;
const PT_INTERP: u32 = 3;
const DT_NEEDED: u64 = 1;
const DT_SONAME: u64 = 14;
const DT_RELR: u64 = 36;

/// One entry from `.gnu.version_r`: this binary requires `version` from the
/// library `file` (e.g. `GLIBC_2.34` from `libc.so.6`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VersionNeed {
    pub file: String,
    pub version: String,
}

/// The facts about a dynamic ELF that the audit gate consumes.
#[derive(Debug, Clone, Default)]
pub struct ElfInfo {
    pub machine: u16,
    /// ELF `e_type` (2 = EXEC, 3 = DYN).
    pub etype: u16,
    pub interp: Option<String>,
    pub soname: Option<String>,
    pub needed: Vec<String>,
    pub version_needs: Vec<VersionNeed>,
    /// Names of undefined (imported) dynamic symbols.
    pub undefined: Vec<String>,
    pub has_dt_relr: bool,
}

/// Extracts the audit-relevant facts from a dynamic ELF in one pass.
pub fn inspect(data: &[u8]) -> Result<ElfInfo> {
    let secs = sections(data)?;
    let mut info = ElfInfo {
        etype: u16le(data, 0x10)?,
        machine: u16le(data, 0x12)?,
        ..Default::default()
    };

    // PT_INTERP from the program headers.
    let phoff = u64le(data, 0x20)? as usize;
    let phentsize = u16le(data, 0x36)? as usize;
    let phnum = u16le(data, 0x38)? as usize;
    for i in 0..phnum {
        let base = phoff + i * phentsize;
        if u32le(data, base)? == PT_INTERP {
            let offset = u64le(data, base + 8)? as usize;
            let size = u64le(data, base + 32)? as usize;
            let raw = data
                .get(offset..offset + size)
                .ok_or_else(|| elf_err("PT_INTERP outside file"))?;
            let end = raw.iter().position(|c| *c == 0).unwrap_or(raw.len());
            info.interp = Some(
                String::from_utf8(raw[..end].to_vec()).map_err(|_| elf_err("non-utf8 interp"))?,
            );
        }
    }

    // .dynamic: DT_NEEDED / DT_SONAME / DT_RELR.
    if let Some(dynamic) = secs.iter().find(|s| s.sh_type == SHT_DYNAMIC) {
        let strtab_section = secs
            .get(dynamic.link as usize)
            .ok_or_else(|| elf_err("bad .dynamic link"))?;
        let strtab = section_data(data, strtab_section)?;
        let d = section_data(data, dynamic)?;
        for entry in d.chunks_exact(16) {
            let tag = u64::from_le_bytes(entry[..8].try_into().unwrap());
            let value = u64::from_le_bytes(entry[8..].try_into().unwrap());
            match tag {
                DT_NEEDED => info.needed.push(cstr(strtab, value as usize)?),
                DT_SONAME => info.soname = Some(cstr(strtab, value as usize)?),
                DT_RELR => info.has_dt_relr = true,
                0 => break, // DT_NULL
                _ => {}
            }
        }
    }

    // .gnu.version_r: required (file, version) pairs.
    if let Some(verneed) = secs.iter().find(|s| s.sh_type == SHT_GNU_VERNEED) {
        let strtab_section = secs
            .get(verneed.link as usize)
            .ok_or_else(|| elf_err("bad .gnu.version_r link"))?;
        let strtab = section_data(data, strtab_section)?;
        let d = section_data(data, verneed)?;
        let mut pos = 0usize;
        loop {
            let cnt = u16le(d, pos + 2)? as usize;
            let file = cstr(strtab, u32le(d, pos + 4)? as usize)?;
            let mut aux_pos = pos + u32le(d, pos + 8)? as usize;
            for _ in 0..cnt {
                let version = cstr(strtab, u32le(d, aux_pos + 8)? as usize)?;
                info.version_needs.push(VersionNeed {
                    file: file.clone(),
                    version,
                });
                let next = u32le(d, aux_pos + 12)? as usize;
                if next == 0 {
                    break;
                }
                aux_pos += next;
            }
            let next = u32le(d, pos + 12)? as usize;
            if next == 0 {
                break;
            }
            pos += next;
        }
    }

    // Undefined dynamic symbols (imports).
    if let Some(dynsym) = secs.iter().find(|s| s.sh_type == SHT_DYNSYM) {
        let strtab_section = secs
            .get(dynsym.link as usize)
            .ok_or_else(|| elf_err("bad .dynsym link"))?;
        let strtab = section_data(data, strtab_section)?;
        let syms = section_data(data, dynsym)?;
        let entsize = if dynsym.entsize >= 24 {
            dynsym.entsize
        } else {
            24
        };
        for i in 0..dynsym.size / entsize {
            let base = i * entsize;
            let name_offset = u32le(syms, base)? as usize;
            let shndx = u16le(syms, base + 6)?;
            if shndx == SHN_UNDEF && name_offset != 0 {
                info.undefined.push(cstr(strtab, name_offset)?);
            }
        }
    }
    Ok(info)
}

/// Lists the global/weak symbols *defined* by a relocatable object (its
/// `.symtab`). Used for compat-pack pruning: an archive member all of whose
/// definitions already exist in the baseline library is dropped.
pub fn defined_global_symbols(data: &[u8]) -> Result<std::collections::BTreeSet<String>> {
    let sections = sections(data)?;
    let Some(symtab) = sections.iter().find(|s| s.sh_type == SHT_SYMTAB) else {
        return Ok(Default::default());
    };
    let strtab_section = sections
        .get(symtab.link as usize)
        .ok_or_else(|| elf_err("bad .symtab link"))?;
    let strtab = section_data(data, strtab_section)?;
    let syms = section_data(data, symtab)?;
    let entsize = if symtab.entsize >= 24 {
        symtab.entsize
    } else {
        24
    };
    let mut out = std::collections::BTreeSet::new();
    for i in 0..symtab.size / entsize {
        let base = i * entsize;
        let name_offset = u32le(syms, base)? as usize;
        let info = *syms
            .get(base + 4)
            .ok_or_else(|| elf_err("truncated symbol"))?;
        let shndx = u16le(syms, base + 6)?;
        // Defined (incl. SHN_ABS/SHN_COMMON), non-local, named.
        if shndx == SHN_UNDEF || info >> 4 == STB_LOCAL || name_offset == 0 {
            continue;
        }
        out.insert(cstr(strtab, name_offset)?);
    }
    Ok(out)
}

/// Lists all exported (defined, non-local) dynamic symbols with their versions.
pub fn exported_symbols(data: &[u8]) -> Result<Vec<DynSymbol>> {
    let sections = sections(data)?;
    let dynsym = sections
        .iter()
        .find(|s| s.sh_type == SHT_DYNSYM)
        .ok_or_else(|| elf_err("no .dynsym section"))?;
    let dynstr = sections
        .get(dynsym.link as usize)
        .ok_or_else(|| elf_err("bad .dynsym link"))?;
    let strtab = section_data(data, dynstr)?;
    let syms = section_data(data, dynsym)?;
    let entsize = if dynsym.entsize >= 24 {
        dynsym.entsize
    } else {
        24
    };
    let count = dynsym.size / entsize;

    let versym = sections
        .iter()
        .find(|s| s.sh_type == SHT_GNU_VERSYM)
        .map(|s| section_data(data, s))
        .transpose()?;
    let verdefs = sections
        .iter()
        .find(|s| s.sh_type == SHT_GNU_VERDEF)
        .map(|s| {
            let vd_strtab_section = sections
                .get(s.link as usize)
                .ok_or_else(|| elf_err("bad .gnu.version_d link"))?;
            verdef_map(data, s, section_data(data, vd_strtab_section)?)
        })
        .transpose()?
        .unwrap_or_default();

    let mut out = Vec::new();
    for i in 0..count {
        let base = i * entsize;
        let name_offset = u32le(syms, base)? as usize;
        let info = *syms
            .get(base + 4)
            .ok_or_else(|| elf_err("truncated symbol"))?;
        let shndx = u16le(syms, base + 6)?;
        if shndx == SHN_UNDEF || info >> 4 == STB_LOCAL || name_offset == 0 {
            continue;
        }
        let name = cstr(strtab, name_offset)?;
        let version = match versym {
            Some(vs) => {
                let raw = u16le(vs, i * 2)? & 0x7fff;
                if raw <= 1 {
                    None
                } else {
                    verdefs
                        .iter()
                        .find(|(ndx, _)| *ndx == raw)
                        .map(|(_, n)| n.clone())
                }
            }
            None => None,
        };
        out.push(DynSymbol { version, name });
    }
    out.sort();
    out.dedup();
    Ok(out)
}

/// Renders symbols in the glibc-abilist-like line format: `VERSION symbol`,
/// with `-` for unversioned symbols.
pub fn render_abilist(symbols: &[DynSymbol]) -> String {
    let mut out = String::new();
    for s in symbols {
        out.push_str(s.version.as_deref().unwrap_or("-"));
        out.push(' ');
        out.push_str(&s.name);
        out.push('\n');
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Locates the host's glibc for a smoke test; skips silently when absent.
    fn host_libc() -> Option<Vec<u8>> {
        for path in [
            "/lib/x86_64-linux-gnu/libc.so.6",
            "/lib64/libc.so.6",
            "/usr/lib64/libc.so.6",
            "/lib/aarch64-linux-gnu/libc.so.6",
        ] {
            if let Ok(data) = std::fs::read(path) {
                return Some(data);
            }
        }
        None
    }

    #[test]
    fn host_libc_exports_versioned_printf() {
        let Some(data) = host_libc() else {
            eprintln!("host libc not found; skipping");
            return;
        };
        let symbols = exported_symbols(&data).unwrap();
        let printf = symbols
            .iter()
            .find(|s| s.name == "printf")
            .expect("printf exported");
        let version = printf.version.as_deref().expect("printf is versioned");
        assert!(
            version.starts_with("GLIBC_"),
            "unexpected version {version}"
        );
        let rendered = render_abilist(&symbols);
        assert!(rendered.contains(" printf\n"));
    }

    #[test]
    fn rejects_non_elf() {
        assert!(matches!(
            exported_symbols(b"not an elf"),
            Err(Error::Elf(_))
        ));
    }
}
