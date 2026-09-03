#!/usr/bin/env python3
"""vcpkg and Ninja extension for the canonical release-component graph."""

import copy


def locked_asset(filename, url, sha256, sha512, size):
    return {
        "filename": filename,
        "url": url,
        "sha256": sha256,
        "sha512": sha512,
        "size": size,
    }


def boost_191_asset(repository, sha256, sha512, size):
    return locked_asset(
        "boostorg-%s-boost-1.91.0.tar.gz" % repository,
        "https://github.com/boostorg/%s/archive/boost-1.91.0.tar.gz"
        % repository,
        sha256,
        sha512,
        size,
    )


CMAKE_HOST_TOOL_POLICY = {
    "schema_version": 1,
    "install_prefix": "/opt/crossforge/host-tools/cmake",
    "binary_relative_paths": ["bin/cmake", "bin/cpack", "bin/ctest"],
    "path_precedence": "before-system",
    "system_binary": "/usr/bin/cmake",
    "consumers": ["cmake", "ctest", "cpack", "vcpkg"],
}
NINJA_HOST_TOOL_POLICY = {
    "schema_version": 1,
    "install_prefix": "/opt/crossforge/host-tools/ninja",
    "binary_relative_path": "bin/ninja",
    "license_relative_path": "share/licenses/ninja/COPYING",
    "path_precedence": "before-system",
    "system_binary": "/usr/bin/ninja",
    "consumers": ["cmake", "meson", "vcpkg"],
}
VCPKG_INTEGRATION_POLICY = {
    "schema_version": 1,
    "cmake_root": "/opt/crossforge/cmake",
    "crt_linkage": "dynamic",
    "execution_adapters": {
        "cmake_variable": "CMAKE_CROSSCOMPILING_EMULATOR",
        "environment_variable": "HOSTRUNNER",
    },
    "find_root_modes": {
        "include": "ONLY",
        "library": "ONLY",
        "package": "ONLY",
        "program": "NEVER",
    },
    "find_root_path_policy": "prepend-sysroot-preserve-existing",
    "host": {
        "architecture": "x64",
        "compiler_root": "/opt/rh/gcc-toolset-15/root/usr/bin",
        "cross_compiling": False,
        "library_linkage": "static",
        "toolchain": "host-gts15.cmake",
        "triplet": "crossforge-host-x64-el8",
    },
    "pic_flag": "-fPIC",
    "position_independent_code": True,
    "system_name": "Linux",
    "system_version": "4.18.0",
    "targets": {
        "aarch64": {
            "architecture": "arm64",
            "compiler_root": "/opt/crossforge/targets/aarch64-unknown-linux-gnu/bin",
            "dynamic_triplet": "crossforge-arm64-el8-dynamic",
            "processor": "aarch64",
            "static_triplet": "crossforge-arm64-el8",
            "sysroot": "/opt/crossforge/sysroots/el8/aarch64",
            "toolchain": "aarch64-unknown-linux-gnu.cmake",
            "triple": "aarch64-unknown-linux-gnu",
        },
        "x86_64": {
            "architecture": "x64",
            "compiler_root": "/opt/crossforge/targets/x86_64-unknown-linux-gnu/bin",
            "dynamic_triplet": "crossforge-x64-el8-dynamic",
            "processor": "x86_64",
            "static_triplet": "crossforge-x64-el8",
            "sysroot": "/opt/crossforge/sysroots/el8/x86_64",
            "toolchain": "x86_64-unknown-linux-gnu.cmake",
            "triple": "x86_64-unknown-linux-gnu",
        },
    },
    "triplet_root": "/opt/crossforge/vcpkg/triplets",
    "vcpkg_root": "/opt/crossforge/vcpkg/root",
}
VCPKG_CONTRACT_POLICY = {
    "schema_version": 1,
    "assets": {
        "patchelf": {
            "filename": "patchelf-0.19.0-x86_64.tar.gz",
            "url": "https://github.com/NixOS/patchelf/releases/download/0.19.0/patchelf-0.19.0-x86_64.tar.gz",
            "sha256": "a493df96abeecee55d539071e9bace94d32458a3baf54d9495da94f44c647d86",
            "sha512": "2a65c9cbdddcc7952cdbd6e98a2cf3da01386cf0f0b927a6bbcfe8131ecf0bfb17c534246635b5e6a090652ee54c903f9f9c4f3f1d2412dba59f287ae2ae8070",
            "size": 569003,
        }
    },
    "binary_sources": "clear",
    "downloads": "forbidden",
    "host_triplet": "crossforge-host-x64-el8",
    "triplets": [
        "crossforge-host-x64-el8",
        "crossforge-x64-el8",
        "crossforge-x64-el8-dynamic",
        "crossforge-arm64-el8",
        "crossforge-arm64-el8-dynamic",
    ],
    "files": [
        {
            "path": "manifest/vcpkg.json",
            "sha256": "82c7339ff0f5a6deb9341ab6956fbde95dd6609a3ae503c6e1660e7c9db0df15",
        },
        {
            "path": "ports/crossforge-host-probe/CMakeLists.txt",
            "sha256": "42a4b4d5c76c8b10ec13c9e2b469958a7e7a727648f5981f344e1c741bed2ae7",
        },
        {
            "path": "ports/crossforge-host-probe/copyright",
            "sha256": "bee6e7ffd2d81f6f009ecda87f141e55744e54b7f73d480a866d051aa9c6076c",
        },
        {
            "path": "ports/crossforge-host-probe/portfile.cmake",
            "sha256": "b78122b52c685f36462a44fa1c8ca36f67584f40b056e7be1e18f7a8eb083105",
        },
        {
            "path": "ports/crossforge-host-probe/probe.c",
            "sha256": "87323fff3acc938d97274e2aa723a4c8d868c1522d7c4844a515759b2709234b",
        },
        {
            "path": "ports/crossforge-host-probe/vcpkg.json",
            "sha256": "dae78d63985682b0702f0c5cabe4a9fb62f3032fc818c6adbb57c383fe76942d",
        },
        {
            "path": "ports/crossforge-target-probe/CMakeLists.txt",
            "sha256": "4e5c9ab9fc33cf42d9c98535157ab9ce4962f1a63a3887740e6eff4856625bf3",
        },
        {
            "path": "ports/crossforge-target-probe/copyright",
            "sha256": "bee6e7ffd2d81f6f009ecda87f141e55744e54b7f73d480a866d051aa9c6076c",
        },
        {
            "path": "ports/crossforge-target-probe/portfile.cmake",
            "sha256": "edde722f6730c494b8697a039766a45e31df0b3406e5a6e9a7e2d82f5dd2c076",
        },
        {
            "path": "ports/crossforge-target-probe/probe.c",
            "sha256": "eab43a212383f9e262783909ca2362e99124ec49f114942e3634c8171a368f3c",
        },
        {
            "path": "ports/crossforge-target-probe/probe.h",
            "sha256": "1ac333a679f98b4dd79adc3cfa25878614a178b667224b0338f364d49288f5c5",
        },
        {
            "path": "ports/crossforge-target-probe/vcpkg.json",
            "sha256": "77a5fd45be846d49c40345a418d6d15df3e6fb8ae2412d7435e2c6759798c586",
        },
    ],
}
VCPKG_UPSTREAM_TIER1_POLICY = {
    "schema_version": 1,
    "assets": [
        {
            "filename": "fmt-backport-4813.patch",
            "url": "https://github.com/fmtlib/fmt/commit/588b3a0f8f6a8bcf2a959cae882d5b2703e86737.patch?full_index=1",
            "sha256": "699f3188774bc40f040715a5ae33e21e052c7b104fb997dff3ccf6f758ede02c",
            "sha512": "afda8fdfcdcb4b0dd5df4d4dae96a57a85fb9c4b65d0b49d51258f0913d4aed93ed146ebf96ed7b277490b1dde6c7117f43332013071441a96c3147520de8368",
            "size": 1390,
        },
        {
            "filename": "fmtlib-fmt-12.2.0.tar.gz",
            "url": "https://github.com/fmtlib/fmt/archive/12.2.0.tar.gz",
            "sha256": "8b852bb5aa6e7d8564f9e81394055395dd1d1936d38dfd3a17792a02bebd7af0",
            "sha512": "5ac2ba0f54a484999ed5407d82b77aad170cea49a267decd2c0eedadf3b14413e2a83fcc8e9ca9c16640595e019b8636e160f72314d8be50653324e82ac745eb",
            "size": 738355,
        },
        {
            "filename": "madler-zlib-v1.3.2.tar.gz",
            "url": "https://github.com/madler/zlib/archive/v1.3.2.tar.gz",
            "sha256": "b99a0b86c0ba9360ec7e78c4f1e43b1cbdf1e6936c8fa0f6835c0cd694a495a1",
            "sha512": "16fea4df307a68cf0035858abe2fd550250618a97590e202037acd18a666f57afc10f8836cbbd472d54a0e76539d0e558cb26f059d53de52ff90634bbf4f47d4",
            "size": 1566911,
        },
    ],
    "binary_sources": "clear",
    "downloads": "forbidden",
    "files": [
        {
            "path": "consumer.cpp",
            "sha256": "d8639cf6844700b4d643bc28292969ff6234d321272471b8683aaed50df3394c",
        },
        {
            "path": "manifest/vcpkg.json",
            "sha256": "802a548ec91dd852acc01c2b7faa590836e18cb2d1bb66da61b6aec86ed69376",
        },
    ],
    "ports": [
        {
            "name": "fmt",
            "port_version": 1,
            "version": "12.2.0",
        },
        {
            "name": "zlib",
            "port_version": 1,
            "version": "1.3.2",
        },
    ],
    "required_features": {
        "fmt": ["core"],
        "zlib": ["core"],
    },
    "triplets": [
        "crossforge-host-x64-el8",
        "crossforge-x64-el8",
        "crossforge-x64-el8-dynamic",
        "crossforge-arm64-el8",
        "crossforge-arm64-el8-dynamic",
    ],
}
VCPKG_UPSTREAM_TIER2_POLICY = {
    "schema_version": 1,
    "assets": [
        {
            "filename": "curl-curl-curl-8_21_0.tar.gz",
            "url": "https://github.com/curl/curl/archive/curl-8_21_0.tar.gz",
            "sha256": "ec753aa6f408a3ca9f0d6d5f7a77417aecd1544db13c03ae5d443612bf367364",
            "sha512": "0ab6c99c3d5b86fb65c526db517c3159b11db2f8d82552d635c4887059c0602288603c93b754ce0ec543ea2f275122ccec2c8dcd866c2611b5b949c728ee72df",
            "size": 3592963,
        },
        {
            "filename": "madler-zlib-v1.3.2.tar.gz",
            "url": "https://github.com/madler/zlib/archive/v1.3.2.tar.gz",
            "sha256": "b99a0b86c0ba9360ec7e78c4f1e43b1cbdf1e6936c8fa0f6835c0cd694a495a1",
            "sha512": "16fea4df307a68cf0035858abe2fd550250618a97590e202037acd18a666f57afc10f8836cbbd472d54a0e76539d0e558cb26f059d53de52ff90634bbf4f47d4",
            "size": 1566911,
        },
        {
            "filename": "openssl-openssl-openssl-3.6.3.tar.gz",
            "url": "https://github.com/openssl/openssl/archive/openssl-3.6.3.tar.gz",
            "sha256": "c5524dd6bfaa8e8ff0f1be885c390d14f3ff0bd2de62a7311b65fcbb75cb7546",
            "sha512": "a89c08101fa1d7e3c09b14f4a90d450bcf336a4f6a3e6e4ea990e4deddcd9ce250472f9114438fd134ff4b47fe93dd47232308567088b2b1c0b2eb50e3b56bdf",
            "size": 55132237,
        },
    ],
    "binary_sources": "clear",
    "downloads": "forbidden",
    "files": [
        {
            "path": "consumer.c",
            "sha256": "a197ad39f22476e4d4fdea66eb3857c405fd83e05b3c46ef18316a75fcc479ff",
        },
        {
            "path": "manifest/vcpkg.json",
            "sha256": "51ac756cf1a47443b6e50be475857e68b091c30214a2fcf1e9c6f68d2f75f5fb",
        },
    ],
    "ports": [
        {"name": "curl", "port_version": 1, "version": "8.21.0"},
        {"name": "openssl", "port_version": 0, "version": "3.6.3"},
        {"name": "zlib", "port_version": 1, "version": "1.3.2"},
    ],
    "required_features": {
        "curl": ["core", "openssl"],
        "openssl": ["core"],
        "zlib": ["core"],
    },
    "triplets": [
        "crossforge-host-x64-el8",
        "crossforge-x64-el8",
        "crossforge-x64-el8-dynamic",
        "crossforge-arm64-el8",
        "crossforge-arm64-el8-dynamic",
    ],
}
VCPKG_UPSTREAM_TIER3_POLICY = {
    "schema_version": 1,
    "assets": [
        locked_asset(
            "abseil-abseil-cpp-20260107.1.tar.gz",
            "https://github.com/abseil/abseil-cpp/archive/20260107.1.tar.gz",
            "4314e2a7cbac89cac25a2f2322870f343d81579756ceff7f431803c2c9090195",
            "f5012885d6b6844a9cf5ed92ad5468b8757db33dfe1364bfb232fff928e06c550c7eb4557f45186a8ac4d18b178df9be267681abab4a6de40823b574afbe9960",
            2301097,
        ),
        locked_asset(
            "boost-1.91.0-LICENSE_1_0.txt",
            "https://raw.githubusercontent.com/boostorg/boost/refs/tags/boost-1.91.0/LICENSE_1_0.txt",
            "c9bff75738922193e67fa726fa225535870d2aa1059f91452c411736284ad566",
            "d6078467835dba8932314c1c1e945569a64b065474d7aced27c9a7acc391d52e9f234138ed9f1aa9cd576f25f12f557e0b733c14891d42c16ecdc4a7bd4d60b8",
            1338,
        ),
        boost_191_asset(
            "assert",
            "9145fba14048a46c0f65e5b28e68176d568148957ebf9bdf9e82bcc5d5a703a9",
            "6938da97de7d223c1459c038824148b467a53ce385b29e7f56df7788b5af0bb504598a042f9662d8d4a8337eb71365d47062b078783570a089263ae4b9400c02",
            21028,
        ),
        boost_191_asset(
            "cmake",
            "48ef2329ad3dbf720fc4d3b6d4b31f7d8dba49db65f4048889dd613fb8553f19",
            "ad642f211916c51365fd2ca9bf6c40d940a57390b6da1eb3932cd945aeaa984a42e31a9297ecc0a302ed1c42a48945867ecd3f4a18a327dfe1e1765bfd7dbcb5",
            41144,
        ),
        boost_191_asset(
            "compat",
            "df6cf47a7668ac7482c441441c19f70b4c1604b5607673d30eeee8bcd7471610",
            "46d40355047afcc4f13792658e8804819e930e18bc0b5b1b8beb6ff7b430dc97ff6cfc77c5c46a22b6703da849f0be5e83258433acdc658be30450eacde88812",
            49504,
        ),
        boost_191_asset(
            "config",
            "e69618fa862927db69b4dd8e6b070c647a799b892fd0ee141d28db0dab025531",
            "e2f264e3986000656a1c46930e2ce6a83ab07d09a88c286ce0b2509e56760549a1dcffd6c0f81d2edb019fdb632b7ec0ccaa9948e29d8b4b6f67dfc18e804c11",
            399041,
        ),
        boost_191_asset(
            "container",
            "fe5b36cf05b4e719c1b11eddd0d2f9eb06a0e86ae6b3cea4a818991fc7b28faf",
            "b2979f63e3cb26e1c3b9b95a9ce7b505106ee76cff8af2fbe87df3a372a86215ba3c7bc25b9c885b4a5cf32605231408709a357102aa662522641e051392142c",
            1456458,
        ),
        boost_191_asset(
            "container_hash",
            "5ec8bf37a75bef0bbac5f9e6e5f95d22a8e8a0034ce225d40ba701a483b34d27",
            "abf57f8a1082bad85b5fc1f9afd82eb6bb628b27f6edc985d397222de846dff9876e01097646c6102553bdf3aded4d4c0bedcd84beda95895a506e88230a1bc3",
            67307,
        ),
        boost_191_asset(
            "core",
            "fbc69a21a0b3c839a2657ed803f1e7c1e6426d3d6e7c8bddb6b7a498382b5cd1",
            "30899f6cb4a9ee4de422f6f7308f6fb6f556e8ed512065734551dc46032ae2055a86f0d042c022a531bcd1b6eded7ce37ca29a76934d19bcf6cc5c7f4b4796a4",
            177226,
        ),
        boost_191_asset(
            "describe",
            "755e216f0f36379dc87e32ff5ab16d87fa9df4276562f241669924298899fc2b",
            "28c3be98401b5e4f55fc42485541a9a6abcee9c7eb285384a95cc161c0beef3b1d85945c163ef762bcd50da1a21bf05b3debb3364109c37226eba177f09b9f45",
            43876,
        ),
        boost_191_asset(
            "endian",
            "ac00af6dd840cd078bff9c035a258c23931dab1e5e28d2578fec038c07674045",
            "f9d8a447249c8a426b694798e00a01a79c4a9b9e99c826c2fa70fb92268f70fab71bd74a3a6dbd45230af9544d1ea6570e06a7e0474e29ad639739fa8c14aa57",
            81922,
        ),
        boost_191_asset(
            "headers",
            "816820256339b2d789de6c37e5aca905ca1949b37ff7df168206a47fe3a90737",
            "c5fa2cd72f6e6666b7963b97bc359c75284b8fb540c30f3629a028b85270c9bc66c8a051383964f2bd4c1e005a4691593d15696e7ef39ea87cf6cff9e5691fb2",
            2023,
        ),
        boost_191_asset(
            "intrusive",
            "9709c94d0cb96d26c9cb325b842f300c1c067579907d34604d716f86556ae794",
            "e0addaec63ab6e5712855a29fa77acba68cf4a99067ba3dc1a312788d6dd465adb7aefdb555c65611e970ccb1ef26937f7c0b1f647e139e20c86e0bb005850d5",
            346472,
        ),
        boost_191_asset(
            "json",
            "ede116effcaa32e257fb7a34b3dff3127ef811901be20a628ae7e063a5d0dcb5",
            "851d599532f8cdebec878806b5c2c542cc760f7d3ed221d5e93e82002d732e4c54278d5cd95ce15970b1bd23d13ed828327ad00ae089afba5fcf9b2110ee2d52",
            4546109,
        ),
        boost_191_asset(
            "move",
            "9c0c45d240bcdebc0305e19c376746be082614401769366fa99de6968e2522ac",
            "8d0db64e06529f32114aa6228089baa148e48d37c211334827b2c4d66d34ad839e1652f7b0a667db9885f66bcc739d039c670e3f061a37d8a65bc5dca554d495",
            135657,
        ),
        boost_191_asset(
            "mp11",
            "6ab871e0ef397a2e7b0602f13a5f473a7381043c91b57fe907ca4925a6a1ee58",
            "5951acc5ab2f5661d9ac84654b6ba2761455469c9e2b52236ad4bbd5f1feac7c136fd07144dbf6954143e80b0dc12da09165f31e42107eb682bca4e46bf93225",
            130942,
        ),
        boost_191_asset(
            "predef",
            "8b2444f89b34a39745177f3eb6d3753bd4647dfffeb7aecfa32ee9cc1958bb33",
            "af8636d463a7b63b7953f7874c2ab107e6a7224c285ca23a65ae71671e60f230a410763fbe5927dfe7c87de561387bfb3ffea6202f051ffc4f6a248f1dab0387",
            109569,
        ),
        boost_191_asset(
            "system",
            "fa4a255820acad0964ac8f434e741e1970d1be9f819751db9a797a3226644089",
            "fbaf8dd0fa84531a30d919dcb712b416ad905af366d62f94d2b0eb859a83822ba8e88d30ff3b28b0d87e6c4a7ce5eb5342b36927fc8522b82f5e2088ff045368",
            104619,
        ),
        boost_191_asset(
            "throw_exception",
            "bba826d1380ccedbcf0468ae4b74012ac14c3830be30d9174fbaf8583b56ed67",
            "176b334133df8bc914113eca31c0885a356bf891df9a825705a34b2c29be6a139b43f791d86c734856a6014ed85a723e6e35a24ebed4dfb2724b90e725566a0d",
            20264,
        ),
        boost_191_asset(
            "variant2",
            "63a1f8031955871b4bca8f42e81adab5fbcee3fbf8b0ddfd0fb18ccf1cef6650",
            "8b8348aa3616318afb3d819c839f573e9593e8fb26e8509a54b997c5ca684ed0f62f55baba6770e358c280cac5e565abbcc36fc006eafb518d119952bf8ea7ca",
            57767,
        ),
        boost_191_asset(
            "winapi",
            "3a93594a0682e6b82735d0e1ff223320092465c66a11c92f634f7a15c0dfc480",
            "7cafe517462c1b0018060a5b468917b31e8fccd2643c34ed0a76a4eb7a77ebc6653d325a818756a92d6005fa810dd8d2843014b9b47925948a49bc81258dcfeb",
            133952,
        ),
        locked_asset(
            "madler-zlib-v1.3.2.tar.gz",
            "https://github.com/madler/zlib/archive/v1.3.2.tar.gz",
            "b99a0b86c0ba9360ec7e78c4f1e43b1cbdf1e6936c8fa0f6835c0cd694a495a1",
            "16fea4df307a68cf0035858abe2fd550250618a97590e202037acd18a666f57afc10f8836cbbd472d54a0e76539d0e558cb26f059d53de52ff90634bbf4f47d4",
            1566911,
        ),
        locked_asset(
            "protocolbuffers-protobuf-v33.4.tar.gz",
            "https://github.com/protocolbuffers/protobuf/archive/v33.4.tar.gz",
            "136a07aad488cc502b11c4416fe4a7df2dfdea1d0833a7a8211000bf952728ba",
            "540059a93721447cf4723bcca06e91c43a4399cb366c05bf84e9d8e2c439f3107ba17803f9d912549b54c471f2dcc4c9fc834145ec441dff31ca24f9a3543aa9",
            6889595,
        ),
    ],
    "binary_sources": "clear",
    "downloads": "forbidden",
    "files": [
        {
            "path": "CMakeLists.txt",
            "sha256": "2ec079bf6c0d003ec1a7d26a776c99c12dcdfebb8d7c4cf749a55e012fd1092d",
        },
        {
            "path": "consumer.cpp",
            "sha256": "952a9ae565d32a7c673ff67ab310530436ab79d996f2d1df443be1391b4277c0",
        },
        {
            "path": "manifest/vcpkg.json",
            "sha256": "8a3073d086e54a7fc80851acd4a3403bd9b7658ceb5545dc8214f187e7a6a5a6",
        },
        {
            "path": "message.proto",
            "sha256": "cd0dba53727b7cfabe7bd1591b6c45bcdb87dc33599ef064efbc34ddad857012",
        },
    ],
    "ports": [
        {"name": "boost-json", "port_version": 0, "version": "1.91.0"},
        {"name": "protobuf", "port_version": 2, "version": "6.33.4"},
        {"name": "zlib", "port_version": 1, "version": "1.3.2"},
    ],
    "required_features": {
        "boost-json": ["core"],
        "protobuf": ["core", "zlib"],
        "zlib": ["core"],
    },
    "triplets": [
        "crossforge-host-x64-el8",
        "crossforge-x64-el8",
        "crossforge-x64-el8-dynamic",
        "crossforge-arm64-el8",
        "crossforge-arm64-el8-dynamic",
    ],
}


def leaf_items(value, path=()):
    if isinstance(value, dict) and value:
        result = []
        for key in sorted(value):
            result.extend(leaf_items(value[key], path + (key,)))
        return result
    if isinstance(value, list) and value:
        result = []
        for index, item in enumerate(value):
            result.extend(leaf_items(item, path + (index,)))
        return result
    return [(path, value)]


def policy_materials(prefix, policy):
    return sorted(
        [
            {
                "path": prefix + "/".join(str(part) for part in path),
                "value": copy.deepcopy(value),
            }
            for path, value in leaf_items(policy)
        ],
        key=lambda material: material["path"],
    )


def vcpkg_sdk_scope(release, require):
    statuses = (
        release["host_locks"]["host-runtime"]["status"],
        release["host_tools"]["cmake"]["binary"]["status"],
        release["host_tools"]["ninja"]["binary"]["status"],
        release["host_tools"]["ninja"]["source"]["status"],
        release["vcpkg"]["release"]["status"],
        release["vcpkg"]["tool"]["status"],
    )
    require(
        all(status in ("pending", "locked") for status in statuses),
        "vcpkg SDK input status is invalid",
    )
    return "build" if all(status == "locked" for status in statuses) else "future"


def extend_component_graph(context):
    """Add only the Ninja/vcpkg domain to a prepared core graph."""
    add = context["add"]
    release = context["release"]
    require = context["require"]
    selector = context["selector"]
    toolchain_builds = context["toolchain_builds"]
    toolchain_qualifications = context["toolchain_qualifications"]
    add(
        "implementation/cmake-host-tool",
        "build",
        explicit_materials=policy_materials(
            "/@implementation/host-tools/cmake/", CMAKE_HOST_TOOL_POLICY
        ),
    )
    add(
        "implementation/vcpkg-integration",
        "build",
        explicit_materials=policy_materials(
            "/@implementation/vcpkg/", VCPKG_INTEGRATION_POLICY
        ),
    )
    add(
        "implementation/ninja-host-tool",
        "build",
        explicit_materials=policy_materials(
            "/@implementation/host-tools/ninja/", NINJA_HOST_TOOL_POLICY
        ),
    )
    add(
        "implementation/vcpkg-contract-qualification",
        "qualification",
        explicit_materials=policy_materials(
            "/@implementation/vcpkg-contract/", VCPKG_CONTRACT_POLICY
        ),
    )
    add(
        "host-tools/ninja",
        "build",
        selector(("baseline",), ("platforms",)),
        (
            "rpm/host-runtime",
            "sources/ninja",
            "implementation/ninja-host-tool",
        ),
    )
    add(
        "host-tools/cmake",
        "build",
        selector(("baseline",), ("platforms",)),
        (
            "host-tools/ninja",
            "rpm/host-runtime",
            "sources/cmake",
            "implementation/cmake-host-tool",
        ),
    )
    add(
        "vcpkg/sdk-build",
        vcpkg_sdk_scope(release, require),
        selector(("baseline",), ("platforms",)),
        (
            "rpm/host-runtime",
            "sources/vcpkg",
            "host-tools/ninja",
            "host-tools/cmake",
            "implementation/vcpkg-integration",
            toolchain_builds["x86_64"],
            toolchain_builds["aarch64"],
        ),
    )
    add(
        "vcpkg/contract-qualification",
        "qualification",
        selector(("baseline",), ("platforms",)),
        (
            "vcpkg/sdk-build",
            "implementation/vcpkg-contract-qualification",
            toolchain_qualifications["x86_64"],
            toolchain_qualifications["aarch64"],
        ),
    )
    add(
        "implementation/vcpkg-upstream-tier1-qualification",
        "qualification",
        explicit_materials=policy_materials(
            "/@implementation/vcpkg-upstream-tier1/",
            VCPKG_UPSTREAM_TIER1_POLICY,
        ),
    )
    add(
        "vcpkg/upstream-tier1-qualification",
        "qualification",
        selector(("baseline",), ("platforms",)),
        (
            "implementation/vcpkg-upstream-tier1-qualification",
            "vcpkg/contract-qualification",
        ),
    )
    add(
        "implementation/vcpkg-upstream-tier2-qualification",
        "qualification",
        explicit_materials=policy_materials(
            "/@implementation/vcpkg-upstream-tier2/",
            VCPKG_UPSTREAM_TIER2_POLICY,
        ),
    )
    add(
        "vcpkg/upstream-tier2-qualification",
        "qualification",
        selector(("baseline",), ("platforms",)),
        (
            "implementation/vcpkg-upstream-tier2-qualification",
            "vcpkg/upstream-tier1-qualification",
        ),
    )
    add(
        "implementation/vcpkg-upstream-tier3-qualification",
        "qualification",
        explicit_materials=policy_materials(
            "/@implementation/vcpkg-upstream-tier3/",
            VCPKG_UPSTREAM_TIER3_POLICY,
        ),
    )
    add(
        "vcpkg/upstream-tier3-qualification",
        "qualification",
        selector(("baseline",), ("platforms",)),
        (
            "implementation/vcpkg-upstream-tier3-qualification",
            "vcpkg/upstream-tier2-qualification",
        ),
    )
