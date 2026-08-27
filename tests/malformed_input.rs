//! Robustness of the binary parsers against malformed input.
//!
//! crossforge parses bytes it did not produce — ELFs handed to the audit
//! gate, wheels from an index, RPMs from a mirror — so every one of these
//! entry points must answer with an error, never a panic. A panic in a gate
//! is a worse failure than a rejection, and in the library API it takes the
//! caller's process down with it.
//!
//! A test that panics fails, so simply calling each entry point over the
//! corpus is the assertion; the explicit checks below only pin the cases
//! that used to crash.

use std::io::Write;

/// Inputs shaped like the real formats but wrong in the ways that break
/// naive offset arithmetic: truncation, saturated fields, absurd counts.
fn corpus() -> Vec<(&'static str, Vec<u8>)> {
    let mut out: Vec<(&'static str, Vec<u8>)> = vec![
        ("empty", Vec::new()),
        ("one byte", vec![0x7f]),
        ("elf magic only", b"\x7fELF".to_vec()),
        ("elf ident only", b"\x7fELF\x02\x01\x01\x00".to_vec()),
        ("zip eocd magic only", b"PK\x05\x06".to_vec()),
        ("rpm lead only", b"\xed\xab\xee\xdb".to_vec()),
        ("ar magic only", b"!<arch>\n".to_vec()),
        ("text", b"not a binary at all\n".to_vec()),
    ];

    // A 64-bit LE ELF header whose every offset and count is saturated:
    // shoff/phoff = u64::MAX, shnum/phnum = u16::MAX. This is the shape that
    // used to overflow `shoff + i * shentsize`.
    let mut saturated = b"\x7fELF\x02\x01\x01\x00".to_vec();
    saturated.resize(64, 0xff);
    out.push(("elf header, all fields saturated", saturated));

    // Same, but with plausible entry sizes so the table walk is actually
    // attempted before it runs off the end.
    let mut big_tables = b"\x7fELF\x02\x01\x01\x00".to_vec();
    big_tables.resize(64, 0);
    big_tables[0x10..0x12].copy_from_slice(&3u16.to_le_bytes()); // ET_DYN
    big_tables[0x12..0x14].copy_from_slice(&62u16.to_le_bytes()); // x86-64
    big_tables[0x20..0x28].copy_from_slice(&u64::MAX.to_le_bytes()); // e_phoff
    big_tables[0x28..0x30].copy_from_slice(&(u64::MAX - 64).to_le_bytes()); // e_shoff
    big_tables[0x36..0x38].copy_from_slice(&56u16.to_le_bytes()); // e_phentsize
    big_tables[0x38..0x3a].copy_from_slice(&u16::MAX.to_le_bytes()); // e_phnum
    big_tables[0x3a..0x3c].copy_from_slice(&64u16.to_le_bytes()); // e_shentsize
    big_tables[0x3c..0x3e].copy_from_slice(&u16::MAX.to_le_bytes()); // e_shnum
    out.push(("elf with out-of-range header tables", big_tables));

    // A zip end-of-central-directory claiming entries that are not there,
    // at an offset past the end of the file.
    let mut eocd = vec![0u8; 22];
    eocd[0..4].copy_from_slice(&0x0605_4b50u32.to_le_bytes());
    eocd[10..12].copy_from_slice(&u16::MAX.to_le_bytes()); // entry count
    eocd[16..20].copy_from_slice(&u32::MAX.to_le_bytes()); // central dir offset
    out.push(("zip eocd pointing past the end", eocd));

    // Every prefix of a real-looking ELF: truncation at each byte boundary
    // exercises a different read.
    let mut base = b"\x7fELF\x02\x01\x01\x00".to_vec();
    base.resize(120, 0);
    base[0x3a..0x3c].copy_from_slice(&64u16.to_le_bytes());
    base[0x3c..0x3e].copy_from_slice(&2u16.to_le_bytes());
    for len in [8usize, 16, 32, 48, 63, 64, 70, 100] {
        out.push(("truncated elf", base[..len].to_vec()));
    }
    out
}

#[test]
fn parsers_reject_malformed_input_without_panicking() {
    let dir = tempfile::tempdir().unwrap();
    for (name, data) in corpus() {
        // In-memory ELF entry points.
        let _ = crossforge::inspect(&data);
        let _ = crossforge::exported_symbols(&data);
        let _ = crossforge::defined_global_symbols(&data);
        let _ = crossforge::patch_elf(
            &data,
            &crossforge::PatchOps {
                set_soname: Some("libx.so.1".to_string()),
                ..Default::default()
            },
        );
        let _ = crossforge::read_runpath(&data);

        // File-backed entry points.
        let path = dir.path().join("input.bin");
        std::fs::File::create(&path)
            .unwrap()
            .write_all(&data)
            .unwrap();
        let wheel = dir.path().join("demo-1.0-cp312-cp312-linux_x86_64.whl");
        std::fs::File::create(&wheel)
            .unwrap()
            .write_all(&data)
            .unwrap();
        let _ = crossforge::read_wheel(&wheel);
        let _ = crossforge::audit_wheel(
            &crossforge::WheelPolicy::builtin(),
            &wheel,
            crossforge::TargetArch::X86_64,
            &[],
        );
        println!("survived: {name}");
    }
}

#[test]
fn saturated_elf_header_is_an_error_not_a_panic() {
    // The exact shape that used to abort with "attempt to add with overflow"
    // while walking the section header table.
    let mut data = b"\x7fELF\x02\x01\x01\x00".to_vec();
    data.resize(64, 0xff);
    assert!(crossforge::inspect(&data).is_err());
    assert!(crossforge::exported_symbols(&data).is_err());
}

#[test]
fn a_wheel_may_not_dictate_an_allocation() {
    // A deflate entry declaring a 4GB uncompressed size must not be taken at
    // its word: the reader grows as it decompresses instead.
    let mut zip: Vec<u8> = Vec::new();
    let name = b"a.txt";
    let payload = [0x03u8, 0x00]; // empty deflate stream
    zip.extend_from_slice(&0x0403_4b50u32.to_le_bytes());
    zip.extend_from_slice(&20u16.to_le_bytes());
    zip.extend_from_slice(&0u16.to_le_bytes());
    zip.extend_from_slice(&8u16.to_le_bytes());
    zip.extend_from_slice(&[0u8; 4]);
    zip.extend_from_slice(&0u32.to_le_bytes()); // crc
    zip.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    zip.extend_from_slice(&u32::MAX.to_le_bytes()); // declared uncompressed size
    zip.extend_from_slice(&(name.len() as u16).to_le_bytes());
    zip.extend_from_slice(&0u16.to_le_bytes());
    zip.extend_from_slice(name);
    zip.extend_from_slice(&payload);

    let cdir_off = zip.len() as u32;
    let mut central: Vec<u8> = Vec::new();
    central.extend_from_slice(&0x0201_4b50u32.to_le_bytes());
    central.extend_from_slice(&0x031eu16.to_le_bytes());
    central.extend_from_slice(&20u16.to_le_bytes());
    central.extend_from_slice(&0u16.to_le_bytes());
    central.extend_from_slice(&8u16.to_le_bytes());
    central.extend_from_slice(&[0u8; 4]);
    central.extend_from_slice(&0u32.to_le_bytes());
    central.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    central.extend_from_slice(&u32::MAX.to_le_bytes());
    central.extend_from_slice(&(name.len() as u16).to_le_bytes());
    central.extend_from_slice(&[0u8; 8]);
    central.extend_from_slice(&0u32.to_le_bytes());
    central.extend_from_slice(&0u32.to_le_bytes());
    central.extend_from_slice(name);
    zip.extend_from_slice(&central);

    zip.extend_from_slice(&0x0605_4b50u32.to_le_bytes());
    zip.extend_from_slice(&[0u8; 4]);
    zip.extend_from_slice(&1u16.to_le_bytes());
    zip.extend_from_slice(&1u16.to_le_bytes());
    zip.extend_from_slice(&(central.len() as u32).to_le_bytes());
    zip.extend_from_slice(&cdir_off.to_le_bytes());
    zip.extend_from_slice(&0u16.to_le_bytes());

    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("demo-1.0-cp312-cp312-linux_x86_64.whl");
    std::fs::write(&path, &zip).unwrap();
    let entries = crossforge::read_wheel(&path).unwrap();
    assert_eq!(entries.len(), 1);
    assert!(entries[0].data.is_empty());
}
