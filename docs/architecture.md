# Crossforge 重写架构

> 状态：已接受的实施基线（2026-08-28）
> 本文是当前实现的架构契约。旧 Rust 原型及其设计记录只保留在 tag `prototype-rust-2026-08-28`。
>
> 实施进度：x86_64 sysroot 已锁定，GTS15 C/C++/LTO cross slice 已实际构建并通过 smoke；它仍是非发布 `-dev` target，直到 canonical DNF transaction 生成链和 host RPM closure 均锁定。aarch64、冻结 ABI 集、完整 GCC/Qt 验收及发布供应链尚未实现。

## 1. 产品契约

Crossforge 生成 **GTS-derived cross SDK**：它使用 Rocky Linux 提供的 Red Hat GCC Toolset 源码与补丁谱系，面向精确锁定的 EL8 sysroot，并通过 ABI、GCC、Qt 和 Python 验收保证行为。它不是 Red Hat 官方 GCC Toolset，也不承诺逐字节复刻原生 GTS。

产品只解决以下问题：

- 在 `linux/amd64` 主机上为 EL8 构建 x86_64 与 aarch64 C/C++ 产物；
- 为 CPython 3.9–3.14 提供匹配的 build Python 和两套 target Python SDK；
- 提供常用构建工具、vcpkg 集成以及构建系统无关的 DEB/RPM 分包能力；
- 将整套 SDK 作为一个经过原子资格化的 OCI 镜像交付。

Crossforge 不负责 wheel 构建或 repair、任意发行版依赖求解、第三方 sysroot 管理、APT/YUM 仓库发布，也不承诺所有 vcpkg ports 均可交叉编译。

## 2. 四轴平台模型

四个维度必须独立表达：

| 维度 | 首发取值 | 含义 |
|---|---|---|
| Build platform | `linux/amd64` | 执行 Docker/BuildKit 构建的机器 |
| Tool host | `linux/amd64` | 最终 GCC、Python build tools 运行的平台 |
| Compiler target | `x86_64-unknown-linux-gnu`、`aarch64-unknown-linux-gnu` | 编译器生成的 ELF 平台 |
| ABI baseline | EL8 | glibc、libstdc++、libgcc 的运行时下限 |

OCI platform 始终是 `linux/amd64`；target 架构不是镜像 platform。两个 target 都是真正的 cross build：即使 x86_64 target 与 host CPU 相同，也必须使用 `x86_64-unknown-linux-gnu-*`、独立 EL8 sysroot 和独立 target runtime，不能退化为 native build。裸 `gcc`/`g++` 只用于 host tools。

## 3. 唯一交付物

用户只使用一个镜像入口：

```text
ghcr.io/eglinuxer/crossforge:gts15-el8
```

版本标签（如 `v0.1.0`、`latest`）可以指向同一 digest，但不形成额外产品。镜像包含：

```text
/opt/crossforge/
├── targets/{x86_64-unknown-linux-gnu,aarch64-unknown-linux-gnu}/
├── sysroots/el8/{x86_64,aarch64}/
├── python/cp{39,310,311,312,313,314}/
├── cmake/
├── meson/
├── vcpkg/triplets/
├── env/
└── release.json
```

镜像还包含原生 GTS15 C/C++ 编译器、CMake、Meson、Ninja、Make、Autoconf、Automake、Libtool、pkg-config、Git、bison、flex、常用归档/文本工具、QEMU、固定版本的 vcpkg 和 nFPM。RPM 构建工具、DejaGNU、Qt 源码、gperf 及 WebEngine 专用工具只存在于内部构建/测试 stage。Rust、Conan、auditwheel、cibuildwheel 不进入产品镜像。

## 4. 构建架构

Dockerfile 描述 stage 和文件流，Buildx Bake 描述矩阵、缓存与目标；结构化 Bash 脚本负责 binutils、GCC 和 CPython 的上游构建；Python 标准库工具负责 JSON 校验、环境选择、ABI 审计和分包。不保留旧 Rust engine、公共 crate API、Runner trait 或自研 RPM/Yum/ELF/ZIP 解析器。

GTS15 工具链的来源与流程固定为：

```text
Rocky 8.10 GTS15 GCC/binutils SRPM
  → rpmbuild -bp（按 EL8 条件应用 vendor patch）
  → Crossforge cross configure/make/install
  → 最终 target compiler 与 runtimes
```

不执行原生 spec 的 `%build`，不从目标 GTS 二进制 RPM 拼装 compiler/runtime。x86_64 与 aarch64 共用同一 recipe，只允许显式、可审计的架构参数差异。首发语言仅为 C、C++、LTO；不交付 GDB、Fortran、Ada、D、Go 或 offload。

`%prep` 使用 `rpmbuild -bp --nodeps` 是有意且仅限此阶段：SRPM 的完整
`BuildRequires` 同时覆盖原生 `%build`、文档和测试，会为单纯解包/打补丁引入
约 730 个无关包。Crossforge 在 candidate 前另行锁定 `%prep` 实际使用的 RPM 工具与命令，
校验 SRPM 签名、SRPM/spec SHA256、EL8 RPM 宏和 prepared tree；cross build
依赖则按 Crossforge 自己的 configure/make recipe 锁定。其他阶段不得借此跳过
依赖检查。

target runtime 必须由 prepared source 完整交叉构建。RH 补丁必须直接生成 `libstdc++_nonshared80.a`；缺失即构建失败，不允许从完整静态库猜测或裁剪 fallback。最终链接模型为 EL8 动态运行库加新实现静态补充：

```ld
INPUT (
  =/usr/lib64/libstdc++.so.6
  -lstdc++_nonshared
  AS_NEEDED ( =/usr/lib64/libstdc++.so.6 )
)
GROUP ( =/lib64/libgcc_s.so.1 libgcc.a )
```

系统 unwinder 保持共享，以保证跨 DSO exception 正确。

## 5. 配置、锁与可追溯性

`config/release.json` 是唯一人工维护的版本事实来源，并由 `config/schemas/release.schema.json` 严格校验。版本、NEVRA、URL、SHA256、target、Python adapter、vcpkg commit、nFPM 版本和基础镜像 digest 都在其中固定。

规划阶段可以用 `status: "pending"` 明示尚未核实的来源，禁止填入猜测值；任何 candidate/release 构建都必须使用 `validate-release.py --require-locked`，存在一个 pending pin 即失败。

规范配置、RPM locks、测试 manifest 和 `crosspack.json` 统一使用严格 JSON + JSON Schema。loader 必须拒绝重复 key、未知字段和未知 `schema_version`。配置身份由 canonical JSON 的 SHA256 计算，不受空白或格式化影响。Bake HCL、CMakePresets、vcpkg manifest、GitHub Actions YAML 等继续使用各自工具的原生格式；生成文件禁止手工修改，并由 CI 检查漂移。

## 6. Sysroot 与运行时兼容性

产品只包含两份不可变 Rocky 8.10 sysroot。每个 RPM lock 记录完整 NEVRA、仓库、下载地址和 SHA256。DNF 只用于维护时求解和生成 lock；正式构建只消费 lock，不读取实时仓库 metadata，也不实现自己的 dependency solver。

Crossforge 不定义 staging/overlay 目录、产品级 sysroot profile 或第三方依赖布局。下游可以通过 GCC 标准 `--sysroot`、CMake 或 pkg-config 覆盖默认值，但自定义 sysroot 的内容和兼容性由下游负责。Qt 的扩展依赖只属于测试夹具，不构成产品 API。

默认 ABI/ISA 契约为：

- glibc 2.28 floor；EL8 `libstdc++.so.6` 与 `libgcc_s.so.1` 动态运行时；
- x86_64 使用 `-march=x86-64 -mtune=generic`，aarch64 使用 `-march=armv8-a -mtune=generic`；
- 禁止 host 头文件/库泄漏、超出冻结集合的 GLIBC/GLIBCXX/CXXABI/GCC 符号、DT_RELR、text relocation、可执行栈和构建目录绝对 RUNPATH；
- “glibc >= 2.28”只是 ABI floor，不代表无条件支持所有此类发行版。

`locks/sysroot-*.json` 可因 Rocky errata 更新；`abi/el8/{x86_64,aarch64}.json` 则冻结最低允许的符号集合。sysroot 更新不得静默扩大 ABI，只有显式升级产品 baseline 才能修改冻结集合。

当前 x86_64 lock 捕获了由 14 个显式 roots 经 DNF 求解得到的 78 个 RPM，并固定
repomd/primary metadata、逐包 NEVRA、URL、仓库 checksum、实收 SHA256、source
RPM 与 Rocky 签名指纹。正式 assembly 不访问仓库；它先从已验签
`filesystem` RPM manifest 核对并预置 EL8 usrmerge 链接，再以无 scripts/triggers
的完整 RPM transaction 安装锁定闭包。当前 renderer 会核验所给 RPM 集合，但
尚未自行执行并记录唯一的 DNF 求解命令；可发布 candidate 前必须补齐固定镜像、
空 installroot、transaction manifest 严格比对的 canonical resolver。

## 7. Python SDK

每个 CPython minor 由同一份精确 patch source 构建一份 amd64 build Python 和两份 target Python：

```text
CPython source
├── build: linux/amd64
├── target: x86_64-unknown-linux-gnu + EL8
└── target: aarch64-unknown-linux-gnu + EL8
```

首发支持 3.9–3.14，共 6 个 build Python 和 12 个 target Python。3.9–3.10 使用 legacy adapter，3.11 使用 transition adapter，3.12–3.14 使用 modern adapter；精确 patch 版本写在 `release.json`。3.9 及后续进入 EOL 的 minor 明确标记为 legacy，不承诺上游安全修复。

target SDK 包含解释器、stdlib、headers、`pyconfig.h`、`_sysconfigdata_*`、扩展模块和构建元数据。即使 x86_64 build/target 架构相同，也不得复用。每个 target 必须验证 zlib、bz2、lzma、ctypes、ssl、hashlib、sqlite3、uuid 等约定模块，以及最小 C extension 的编译、ELF 架构和 import；3.14 另验 `compression.zstd`。

Python 契约是“支持交叉编译扩展”，不是 PEP 517/wheel 编排器。Crossforge 不做 wheel retag、vendoring、manylinux repair，也不支持 PyPy、free-threaded 或 debug Python。

## 8. vcpkg 集成

镜像内固定一个 vcpkg commit，并提供资格化 triplets：

```text
crossforge-host-x64-el8
crossforge-x64-el8
crossforge-x64-el8-dynamic
crossforge-arm64-el8
crossforge-arm64-el8-dynamic
```

默认 target triplet 将第三方库静态、PIC 链接，CRT、glibc、libstdc++ 和 libgcc 保持动态；dynamic triplet 仅供显式选择。target triplet 通过 `VCPKG_CHAINLOAD_TOOLCHAIN_FILE` 使用 Crossforge toolchain，host dependencies 始终使用 host triplet。

Crossforge 只承诺 triplet、host/target 分离和代表性 ports（zlib、fmt、OpenSSL、curl、protobuf、Boost）的持续验收。项目自己的 `vcpkg.json`、builtin baseline、registry、overlay ports、许可证判断和 binary cache 由下游管理；镜像不预装编译好的 ports。

## 9. 构建系统无关的分包

`crossforge package` 调用自研的薄编排层 `crosspack`，后者只接受 staged filesystem 或显式 `source → destination` 映射，不调用也不识别 CMake、Meson、Autotools、Make、Cargo 或 Bazel。因此 CPack 不是正式打包路径。

`crosspack` 负责：

- 按 manifest 将文件分入 runtime、development、tools、debug 等 component；
- 拒绝路径逃逸、文件重叠、遗漏和错误 target ELF；
- 映射 x86_64/aarch64 到 RPM 和 DEB 架构，生成精确 component 间依赖；
- 使用 target objcopy 拆分 debug symbols，执行 ABI/`DT_NEEDED`/RPATH 检查；
- 生成可复现 manifest、SHA256 和临时 nFPM 配置。

固定版本的 nFPM 负责真正的 DEB/RPM 编码、metadata、压缩、scriptlets 和签名接口。Crossforge 不自研包格式，不猜测 SONAME 对应的发行版包名；RPM/DEB 外部依赖必须由下游分别声明。首发不负责 APT/YUM repository 发布。

## 10. 用户接口

唯一 launcher 是一个无网络、无插件系统的轻量 Python CLI：

```text
crossforge info
crossforge shell
crossforge run
crossforge package
```

`run`/`shell` 显式选择 `--target x86_64|aarch64`，并可选择 `--python 3.14`、`--vcpkg` 与 `--linkage static|dynamic`。它只在子进程中设置 compiler、sysroot、CMake、Meson、pkg-config、Python 和 vcpkg 环境，不修改全局环境，也不根据宿主或项目内容猜测 target。未选择 target 时使用原生 GTS15 host 环境。

## 11. 验收与发布门禁

构建流程必须是：

```text
source commit → build once → candidate digest → 原物验收 → registry-side promotion
```

所有测试通过完整 OCI digest 拉取候选镜像。Release 不重建，只给已资格化 digest 增加不可变版本标签并更新稳定通道。

测试分层如下：

- PR：JSON/Schema、shellcheck/shfmt、Python 单测、crosspack、Docker/Bake 静态检查和相关 smoke；
- candidate：双 target C/C++/ABI、代表 Python、vcpkg ports、DEB/RPM 安装测试；
- nightly/full：双 target 的 `check-gcc`、`check-g++`、`check-target-libgcc`、`check-target-libstdc++-v3`、`check-target-libgomp`，完整 Python 矩阵，以及 Qt 6.8.4 双 target；
- release：同一 digest 的原生 aarch64 终检和资格化证明检查。

GCC testsuite 必须指向镜像内最终安装的 compiler，并使用 EL8 shared runtime + Crossforge nonshared/libgcc 的最终 hybrid 组合。x86_64 直接执行，aarch64 日常使用固定 QEMU，release 使用原生 ARM。已知失败按 test identity 维护精确基线；新增 FAIL/ERROR/UNRESOLVED 直接失败，不允许用失败数量阈值掩盖回归。

Qt 验收固定 Qt 6.8.4 `qt-everywhere` 官方源码和 SHA256，构建完整开源 Linux desktop 模块集合，至少包括 qtbase、qtdeclarative、qtshadertools、qttools、qtwayland、qtmultimedia、qtquick3d 和 qtwebengine；不构建 examples/tests/docs。host tools 与 target 使用同版本并通过 `QT_HOST_PATH` 连接，required module/feature 被静默跳过即失败。Qt 产物只作为测试 artifact，不进入 SDK 镜像。

## 12. 发布、供应链与许可边界

Rocky Linux 8.10 是基础镜像、host packages、sysroot 和 GTS SRPM 的单一供应链。所有源码、RPM、工具和基础镜像均固定 hash 或 digest；禁止 `curl | sh`。BuildKit cache 只用于加速，不构成发布身份或测试证据。

每个 release 同时提供：

- 用户 SDK 镜像；
- 对应 source bundle（GTS/binutils SRPM、镜像及 sysroot RPM 对应 SRPM、CPython sources、patches、构建脚本和许可证）；
- SPDX/CycloneDX SBOM、max-mode provenance 和 qualification report；
- 通过 GitHub OIDC/Cosign 对镜像 digest 与资格化声明做的 keyless signature。

镜像内保留 `SOURCES.json`、`SOURCE-OFFER`、SBOM 和第三方许可证。发布门禁必须保证每个二进制组件可映射到长期可取得的准确源码。对外措辞只能表述为 “GTS-derived cross SDK built from Rocky Linux rebuild sources”，不得暗示 Red Hat 或 Rocky 官方支持、认证或背书；首次公开发布前仍需正式法律复核。

## 13. 维护与演进规则

安全修复、CPython patch、GTS patch、vcpkg commit 和 Rocky errata 更新必须通过自动差异报告与相应资格化；自动化可以开 PR，但不得自动合并。差异至少覆盖 RPM NEVRA、source hash、ABI exports、Python modules、vcpkg ports、镜像大小和 GCC baseline。

- patch release：安全修复、上游 patch、lock 更新，不改变公共接口；
- minor release：增删 Python minor、增加工具或扩展 `crosspack` schema；
- major release：GTS major、EL baseline、目录布局、canonical triple 或 vcpkg triplet 语义变化。

稳定标签只允许指向通过完整资格化的最新 release。

## 14. 目标仓库结构

```text
config/                  release.json 与 JSON Schema
config/sysroots/         DNF 求解输入计划
locks/                   host/sysroot 精确 RPM locks
keys/                    固定的 RPM 签名信任根
abi/el8/                 冻结 ABI 集合
docker/                  Dockerfile 与 test.Dockerfile
docker-bake.hcl          仓库根部的 Buildx Bake 入口
scripts/                 SRPM、binutils、GCC、Python 和镜像组装脚本
tools/crossforge/         launcher、JSON/ABI 工具与 crosspack
integration/             CMake、Meson、vcpkg 集成文件
tests/{smoke,gcc,python,qt6,vcpkg,packaging}/
```

实现采用纵向切片：x86_64 cross compiler 与 hybrid runtime 已完成；下一步实现 aarch64 对等切片，随后依次加入 Python、vcpkg/分包、完整 GCC/Qt 验收和原子发布。旧 Rust 实现已按用户决定删除，由原型 tag 提供完整历史快照。
