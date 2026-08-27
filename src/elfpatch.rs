//! Minimal ELF rewriting for wheel vendoring (design doc §9, milestone M8):
//! DT_SONAME / DT_NEEDED renaming and DT_RUNPATH injection on 64-bit
//! little-endian shared objects — the patchelf operations auditwheel repair
//! relies on, implemented natively.
//!
//! Strategy: append-only relocation of the dynamic metadata. A new `.dynstr`
//! (old content + new strings — every pre-existing offset stays valid, which
//! keeps version-need and symbol-name references intact) and a new
//! `.dynamic` are placed in a fresh RW `PT_LOAD` segment at the end of the
//! file, together with the enlarged program-header table; the ELF header,
//! `PT_PHDR`/`PT_DYNAMIC` entries and the `.dynamic`/`.dynstr` section
//! headers are re-pointed. The old metadata stays in place as dead bytes.
//! No section data moves, so hashes, symbols and relocations are untouched.

use std::collections::BTreeMap;

use crate::bytes;
use crate::error::{Error, Result};

const DT_NULL: u64 = 0;
const DT_NEEDED: u64 = 1;
const DT_STRTAB: u64 = 5;
const DT_STRSZ: u64 = 10;
const DT_SONAME: u64 = 14;
const DT_RPATH: u64 = 15;
const DT_RUNPATH: u64 = 29;
const DT_VERNEED: u64 = 0x6ffffffe;
const DT_VERNEEDNUM: u64 = 0x6fffffff;

const PT_LOAD: u32 = 1;
const PT_DYNAMIC: u32 = 2;
const PT_PHDR: u32 = 6;

const SHT_STRTAB: u32 = 3;
const SHT_DYNAMIC: u32 = 6;

const PHENT: usize = 56;
const SHENT: usize = 64;
const ALIGN: u64 = 0x10000;

fn elf_err(msg: impl Into<String>) -> Error {
    Error::Elf(format!("elfpatch: {}", msg.into()))
}

/// The rewrite operations one vendoring pass needs.
#[derive(Debug, Clone, Default)]
pub struct PatchOps {
    /// New DT_SONAME (vendored libraries get their hashed name).
    pub set_soname: Option<String>,
    /// DT_NEEDED renames: old soname → new (hashed) soname.
    pub replace_needed: BTreeMap<String, String>,
    /// DT_RUNPATH to set (replacing any existing DT_RPATH/DT_RUNPATH).
    pub set_runpath: Option<String>,
}

impl PatchOps {
    pub fn is_noop(&self) -> bool {
        self.set_soname.is_none() && self.replace_needed.is_empty() && self.set_runpath.is_none()
    }
}

struct Reader<'a>(&'a [u8]);

impl<'a> Reader<'a> {
    fn u16(&self, off: usize) -> Result<u16> {
        bytes::u16le(self.0, off).ok_or_else(|| elf_err("truncated"))
    }
    fn u32(&self, off: usize) -> Result<u32> {
        bytes::u32le(self.0, off).ok_or_else(|| elf_err("truncated"))
    }
    fn u64(&self, off: usize) -> Result<u64> {
        bytes::u64le(self.0, off).ok_or_else(|| elf_err("truncated"))
    }
}

/// Writes into a buffer this module built itself; a bad offset is a bug
/// here rather than hostile input, so it is checked and reported, never
/// allowed to index out of bounds.
fn put_u64(buf: &mut [u8], off: usize, v: u64) -> Result<()> {
    let end = off
        .checked_add(8)
        .filter(|e| *e <= buf.len())
        .ok_or_else(|| elf_err("write offset outside buffer"))?;
    buf[off..end].copy_from_slice(&v.to_le_bytes());
    Ok(())
}

/// Checked `base + delta` for a field inside a header.
fn field(base: usize, delta: usize) -> Result<usize> {
    bytes::add(base, delta).ok_or_else(|| elf_err("field offset overflows"))
}

/// Checked `base + index * size` for walking a header table.
fn entry_at(base: usize, index: usize, size: usize) -> Result<usize> {
    bytes::span(base, index, size).ok_or_else(|| elf_err("header table offset overflows"))
}

fn align_up(v: u64, align: u64) -> u64 {
    v.div_ceil(align) * align
}

/// Applies `ops` to a 64-bit LE `ET_DYN` object, returning the new file
/// contents.
pub fn patch_elf(data: &[u8], ops: &PatchOps) -> Result<Vec<u8>> {
    if ops.is_noop() {
        return Ok(data.to_vec());
    }
    let r = Reader(data);
    if !data.starts_with(b"\x7fELF") {
        return Err(elf_err("not an ELF file"));
    }
    if data.get(4) != Some(&2) || data.get(5) != Some(&1) {
        return Err(elf_err("only 64-bit little-endian objects are supported"));
    }
    let e_phoff = r.u64(0x20)? as usize;
    let e_shoff = r.u64(0x28)? as usize;
    let e_phnum = r.u16(0x38)? as usize;
    let e_shnum = r.u16(0x3c)? as usize;

    // Program headers.
    let mut phdrs: Vec<Vec<u8>> = (0..e_phnum)
        .map(|i| {
            entry_at(e_phoff, i, PHENT).and_then(|at| {
                bytes::slice(data, at, PHENT)
                    .map(<[u8]>::to_vec)
                    .ok_or_else(|| elf_err("program headers out of range"))
            })
        })
        .collect::<Result<_>>()?;

    let vaddr_to_off = |vaddr: u64| -> Result<usize> {
        for ph in &phdrs {
            let pr = Reader(ph);
            if pr.u32(0)? == PT_LOAD {
                let p_offset = pr.u64(8)?;
                let p_vaddr = pr.u64(16)?;
                let p_filesz = pr.u64(32)?;
                let end = p_vaddr.checked_add(p_filesz);
                if end.is_some_and(|e| vaddr >= p_vaddr && vaddr < e) {
                    return Ok((vaddr - p_vaddr + p_offset) as usize);
                }
            }
        }
        Err(elf_err(format!(
            "vaddr {vaddr:#x} not mapped by any PT_LOAD"
        )))
    };

    // Dynamic section entries (up to DT_NULL).
    let dyn_idx = phdrs
        .iter()
        .position(|ph| Reader(ph).u32(0).is_ok_and(|t| t == PT_DYNAMIC))
        .ok_or_else(|| elf_err("no PT_DYNAMIC segment"))?;
    let dyn_off = Reader(&phdrs[dyn_idx]).u64(8)? as usize;
    let dyn_size = Reader(&phdrs[dyn_idx]).u64(32)? as usize;
    let mut entries: Vec<(u64, u64)> = Vec::new();
    let dyn_end = field(dyn_off, dyn_size)?;
    let mut pos = dyn_off;
    while field(pos, 16)? <= dyn_end {
        let tag = r.u64(pos)?;
        let val = r.u64(field(pos, 8)?)?;
        if tag == DT_NULL {
            break;
        }
        entries.push((tag, val));
        pos = field(pos, 16)?;
    }

    let strtab_vaddr = entries
        .iter()
        .find(|(t, _)| *t == DT_STRTAB)
        .map(|(_, v)| *v)
        .ok_or_else(|| elf_err("no DT_STRTAB"))?;
    let strsz = entries
        .iter()
        .find(|(t, _)| *t == DT_STRSZ)
        .map(|(_, v)| *v as usize)
        .ok_or_else(|| elf_err("no DT_STRSZ"))?;
    let strtab_off = vaddr_to_off(strtab_vaddr)?;
    let old_dynstr =
        bytes::slice(data, strtab_off, strsz).ok_or_else(|| elf_err("dynstr out of range"))?;
    let str_at = |off: u64| -> Result<&str> {
        let raw = bytes::cstr(old_dynstr, off as usize)
            .ok_or_else(|| elf_err("unterminated dynstr entry"))?;
        std::str::from_utf8(raw).map_err(|_| elf_err("non-UTF-8 dynstr entry"))
    };

    // New dynstr: old content + appended strings (old offsets stay valid).
    let mut new_dynstr = old_dynstr.to_vec();
    let mut appended: BTreeMap<String, u64> = BTreeMap::new();
    let mut add_str = |s: &str, new_dynstr: &mut Vec<u8>| -> u64 {
        if let Some(off) = appended.get(s) {
            return *off;
        }
        let off = new_dynstr.len() as u64;
        new_dynstr.extend_from_slice(s.as_bytes());
        new_dynstr.push(0);
        appended.insert(s.to_string(), off);
        off
    };

    // Transform the dynamic entries.
    let mut new_entries: Vec<(u64, u64)> = Vec::new();
    let mut runpath_written = false;
    for (tag, val) in &entries {
        match *tag {
            DT_NEEDED => {
                let name = str_at(*val)?;
                match ops.replace_needed.get(name) {
                    Some(new) => new_entries.push((DT_NEEDED, add_str(new, &mut new_dynstr))),
                    None => new_entries.push((DT_NEEDED, *val)),
                }
            }
            DT_SONAME => match &ops.set_soname {
                Some(new) => new_entries.push((DT_SONAME, add_str(new, &mut new_dynstr))),
                None => new_entries.push((DT_SONAME, *val)),
            },
            DT_RPATH | DT_RUNPATH => match &ops.set_runpath {
                Some(rp) => {
                    if !runpath_written {
                        new_entries.push((DT_RUNPATH, add_str(rp, &mut new_dynstr)));
                        runpath_written = true;
                    }
                }
                None => new_entries.push((*tag, *val)),
            },
            _ => new_entries.push((*tag, *val)),
        }
    }
    if let Some(soname) = &ops.set_soname {
        if !entries.iter().any(|(t, _)| *t == DT_SONAME) {
            new_entries.push((DT_SONAME, add_str(soname, &mut new_dynstr)));
        }
    }
    if let Some(rp) = &ops.set_runpath {
        if !runpath_written {
            new_entries.push((DT_RUNPATH, add_str(rp, &mut new_dynstr)));
        }
    }

    // .gnu.version_r still names the old library files; collect vn_file
    // rewrites so ld.so can match version needs against the renamed
    // DT_NEEDED entries (missing this trips an ld.so assertion in
    // dl-version.c). The verneed section stays in place — only the 4-byte
    // string offsets are patched.
    let mut verneed_patches: Vec<(usize, u32)> = Vec::new();
    if !ops.replace_needed.is_empty() {
        let verneed_vaddr = entries
            .iter()
            .find(|(t, _)| *t == DT_VERNEED)
            .map(|(_, v)| *v);
        let verneed_num = entries
            .iter()
            .find(|(t, _)| *t == DT_VERNEEDNUM)
            .map(|(_, v)| *v as usize)
            .unwrap_or(0);
        if let Some(vaddr) = verneed_vaddr {
            let mut off = vaddr_to_off(vaddr)?;
            for _ in 0..verneed_num {
                let vn_file = r.u32(field(off, 4)?)? as u64;
                if let Ok(name) = str_at(vn_file) {
                    if let Some(new) = ops.replace_needed.get(name) {
                        let new_off = *appended
                            .get(new)
                            .ok_or_else(|| elf_err("renamed verneed file not in dynstr"))?;
                        verneed_patches.push((field(off, 4)?, new_off as u32));
                    }
                }
                let vn_next = r.u32(field(off, 12)?)? as usize;
                if vn_next == 0 {
                    break;
                }
                off = field(off, vn_next)?;
            }
        }
    }

    // Layout of the appended segment: [phdrs][dynamic][dynstr].
    let new_phnum = e_phnum + 1;
    let phdrs_size = (new_phnum * PHENT) as u64;
    let dynamic_size = ((new_entries.len() + 1) * 16) as u64;
    let max_vaddr = phdrs
        .iter()
        .filter(|ph| Reader(ph).u32(0).is_ok_and(|t| t == PT_LOAD))
        .map(|ph| Ok(Reader(ph).u64(16)? + Reader(ph).u64(40)?))
        .collect::<Result<Vec<u64>>>()?
        .into_iter()
        .max()
        .ok_or_else(|| elf_err("no PT_LOAD segments"))?;
    let seg_vaddr = align_up(max_vaddr, ALIGN);
    let seg_off = align_up(data.len() as u64, ALIGN);
    let phdrs_vaddr = seg_vaddr;
    let dynamic_vaddr = align_up(seg_vaddr + phdrs_size, 8);
    let dynstr_vaddr = dynamic_vaddr + dynamic_size;
    let seg_size = dynstr_vaddr + new_dynstr.len() as u64 - seg_vaddr;

    // Patch STRTAB/STRSZ in the new dynamic entries.
    for (tag, val) in &mut new_entries {
        match *tag {
            DT_STRTAB => *val = dynstr_vaddr,
            DT_STRSZ => *val = new_dynstr.len() as u64,
            _ => {}
        }
    }

    // Rebuild the program header table.
    for ph in &mut phdrs {
        let ptype = Reader(ph).u32(0)?;
        if ptype == PT_PHDR {
            put_u64(ph, 8, seg_off)?;
            put_u64(ph, 16, phdrs_vaddr)?;
            put_u64(ph, 24, phdrs_vaddr)?;
            put_u64(ph, 32, phdrs_size)?;
            put_u64(ph, 40, phdrs_size)?;
        } else if ptype == PT_DYNAMIC {
            put_u64(ph, 8, seg_off + (dynamic_vaddr - seg_vaddr))?;
            put_u64(ph, 16, dynamic_vaddr)?;
            put_u64(ph, 24, dynamic_vaddr)?;
            put_u64(ph, 32, dynamic_size)?;
            put_u64(ph, 40, dynamic_size)?;
        }
    }
    let mut load = vec![0u8; PHENT];
    {
        let buf = &mut load;
        buf[0..4].copy_from_slice(&PT_LOAD.to_le_bytes());
        buf[4..8].copy_from_slice(&6u32.to_le_bytes()); // RW
        put_u64(buf, 8, seg_off)?;
        put_u64(buf, 16, seg_vaddr)?;
        put_u64(buf, 24, seg_vaddr)?;
        put_u64(buf, 32, seg_size)?;
        put_u64(buf, 40, seg_size)?;
        put_u64(buf, 48, ALIGN)?;
    }
    phdrs.push(load);

    // Assemble the output file.
    let mut out = data.to_vec();
    for (off, val) in &verneed_patches {
        let end = off
            .checked_add(4)
            .filter(|e| *e <= out.len())
            .ok_or_else(|| elf_err("verneed patch outside file"))?;
        out[*off..end].copy_from_slice(&val.to_le_bytes());
    }
    out.resize(seg_off as usize, 0);
    for ph in &phdrs {
        out.extend_from_slice(ph);
    }
    out.resize((seg_off + (dynamic_vaddr - seg_vaddr)) as usize, 0);
    for (tag, val) in &new_entries {
        out.extend_from_slice(&tag.to_le_bytes());
        out.extend_from_slice(&val.to_le_bytes());
    }
    out.extend_from_slice(&[0u8; 16]); // DT_NULL
    out.extend_from_slice(&new_dynstr);

    // ELF header: new program header table location and count.
    put_u64(&mut out, 0x20, seg_off)?;
    out[0x38..0x3a].copy_from_slice(&(new_phnum as u16).to_le_bytes());

    // Section headers: re-point .dynamic and .dynstr (identified by type +
    // old address) so readelf/strip stay consistent with runtime reality.
    for i in 0..e_shnum {
        let base = entry_at(e_shoff, i, SHENT)?;
        let sr = Reader(&out);
        let sh_type = sr.u32(field(base, 4)?)?;
        let sh_addr = sr.u64(field(base, 16)?)?;
        if sh_type == SHT_DYNAMIC {
            put_u64(&mut out, field(base, 16)?, dynamic_vaddr)?;
            put_u64(
                &mut out,
                field(base, 24)?,
                seg_off + (dynamic_vaddr - seg_vaddr),
            )?;
            put_u64(&mut out, field(base, 32)?, dynamic_size)?;
        } else if sh_type == SHT_STRTAB && sh_addr == strtab_vaddr {
            put_u64(&mut out, field(base, 16)?, dynstr_vaddr)?;
            put_u64(
                &mut out,
                field(base, 24)?,
                seg_off + (dynstr_vaddr - seg_vaddr),
            )?;
            put_u64(&mut out, field(base, 32)?, new_dynstr.len() as u64)?;
        }
    }
    Ok(out)
}

/// Reads DT_RUNPATH/DT_RPATH from an ELF (for tests and the vendor audit).
pub fn read_runpath(data: &[u8]) -> Result<Option<String>> {
    let r = Reader(data);
    let e_phoff = r.u64(0x20)? as usize;
    let e_phnum = r.u16(0x38)? as usize;
    let mut dynamic = None;
    let mut loads = Vec::new();
    for i in 0..e_phnum {
        let base = e_phoff + i * PHENT;
        let ptype = r.u32(base)?;
        let p_offset = r.u64(base + 8)?;
        let p_vaddr = r.u64(base + 16)?;
        let p_filesz = r.u64(base + 32)?;
        if ptype == PT_DYNAMIC {
            dynamic = Some((p_offset as usize, p_filesz as usize));
        } else if ptype == PT_LOAD {
            loads.push((p_vaddr, p_filesz, p_offset));
        }
    }
    let Some((dyn_off, dyn_size)) = dynamic else {
        return Ok(None);
    };
    let mut strtab = None;
    let mut runpath_off = None;
    let mut pos = dyn_off;
    while pos + 16 <= dyn_off + dyn_size {
        let tag = r.u64(pos)?;
        let val = r.u64(pos + 8)?;
        match tag {
            DT_NULL => break,
            DT_STRTAB => strtab = Some(val),
            DT_RPATH | DT_RUNPATH => runpath_off = Some(val),
            _ => {}
        }
        pos += 16;
    }
    let (Some(strtab), Some(rp)) = (strtab, runpath_off) else {
        return Ok(None);
    };
    let file_off = loads
        .iter()
        .find(|(v, sz, _)| strtab >= *v && strtab < *v + *sz)
        .map(|(v, _, o)| (strtab - v + o) as usize)
        .ok_or_else(|| elf_err("strtab not mapped"))?;
    let start = file_off + rp as usize;
    let end = data[start..]
        .iter()
        .position(|c| *c == 0)
        .map(|p| start + p)
        .ok_or_else(|| elf_err("unterminated runpath"))?;
    Ok(Some(
        String::from_utf8_lossy(&data[start..end]).into_owned(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::elfdyn;

    /// Compiles a tiny shared object with the host gcc.
    fn build_test_lib(dir: &std::path::Path) -> Vec<u8> {
        let src = dir.join("t.c");
        std::fs::write(&src, "int the_answer(void) { return 42; }\n").unwrap();
        let out = dir.join("libt.so");
        // --no-as-needed keeps a DT_NEEDED on libm even though nothing calls
        // into it, giving the rename test something to rewrite.
        let status = std::process::Command::new("gcc")
            .args([
                "-shared",
                "-fPIC",
                "-Wl,-soname,libt.so.1",
                "-Wl,--no-as-needed",
                "-o",
            ])
            .arg(&out)
            .arg(&src)
            .arg("-lm")
            .status()
            .expect("host gcc is required for elfpatch tests");
        assert!(status.success());
        std::fs::read(&out).unwrap()
    }

    #[test]
    fn rename_soname_needed_and_set_runpath() {
        let dir = tempfile::tempdir().unwrap();
        let data = build_test_lib(dir.path());
        let before = elfdyn::inspect(&data).unwrap();
        assert_eq!(before.soname.as_deref(), Some("libt.so.1"));
        assert!(!before.needed.is_empty());

        // Rename every DT_NEEDED, including libc (which carries the verneed
        // entries the vn_file rewrite must follow).
        let mut ops = PatchOps {
            set_soname: Some("libt-deadbeef.so.1".to_string()),
            set_runpath: Some("$ORIGIN/../demo.libs".to_string()),
            ..Default::default()
        };
        for needed in &before.needed {
            ops.replace_needed
                .insert(needed.clone(), format!("{needed}.vendored"));
        }
        let patched = patch_elf(&data, &ops).unwrap();

        let after = elfdyn::inspect(&patched).unwrap();
        assert_eq!(after.soname.as_deref(), Some("libt-deadbeef.so.1"));
        for needed in &before.needed {
            assert!(after.needed.contains(&format!("{needed}.vendored")));
            assert!(!after.needed.contains(needed));
        }
        assert_eq!(
            read_runpath(&patched).unwrap().as_deref(),
            Some("$ORIGIN/../demo.libs")
        );
        // Untouched: exported symbols still parse; version needs now name
        // the renamed files (the vn_file rewrite ld.so depends on).
        let symbols = elfdyn::exported_symbols(&patched).unwrap();
        assert!(symbols.iter().any(|s| s.name == "the_answer"));
        assert_eq!(after.version_needs.len(), before.version_needs.len());
        assert!(!before.version_needs.is_empty());
        for (b, a) in before.version_needs.iter().zip(&after.version_needs) {
            assert_eq!(a.file, format!("{}.vendored", b.file));
            assert_eq!(a.version, b.version);
        }
    }

    #[test]
    fn noop_returns_input() {
        let dir = tempfile::tempdir().unwrap();
        let data = build_test_lib(dir.path());
        let patched = patch_elf(&data, &PatchOps::default()).unwrap();
        assert_eq!(patched, data);
    }

    #[test]
    fn patched_lib_still_links_and_loads() {
        let dir = tempfile::tempdir().unwrap();
        let data = build_test_lib(dir.path());
        let ops = PatchOps {
            set_soname: Some("libt-cafebabe.so.1".to_string()),
            set_runpath: Some("$ORIGIN".to_string()),
            ..Default::default()
        };
        let patched = patch_elf(&data, &ops).unwrap();
        let lib = dir.path().join("libt-cafebabe.so.1");
        std::fs::write(&lib, &patched).unwrap();

        // dlopen the patched library and call into it.
        let main_src = dir.path().join("main.c");
        std::fs::write(
            &main_src,
            r#"
#include <dlfcn.h>
int main(void) {
    void *h = dlopen("./libt-cafebabe.so.1", RTLD_NOW);
    if (!h) return 1;
    int (*f)(void) = (int (*)(void))dlsym(h, "the_answer");
    if (!f) return 2;
    return f() == 42 ? 0 : 3;
}
"#,
        )
        .unwrap();
        let exe = dir.path().join("main");
        let status = std::process::Command::new("gcc")
            .arg(&main_src)
            .args(["-o"])
            .arg(&exe)
            .args(["-ldl"])
            .status()
            .unwrap();
        assert!(status.success());
        let run = std::process::Command::new(&exe)
            .current_dir(dir.path())
            .status()
            .unwrap();
        assert!(run.success(), "patched library failed to dlopen/run");
    }
}
