# crossforge 交叉编译工具链构建引擎 · 设计文档

- 版本：v0.8（2026-08-27）
- 状态：M0–M8 已交付（工具链全流水线 + python packs + wheel 端到端），持续演进
- 范围：开源的交叉编译工具链**构建引擎**（CLI 工具 + Rust library API）——解决 C/C++ 产物在旧 Linux 发行版上的 glibc / libstdc++ 兼容问题，构建任意（编译器 × target × 基线）组合的可重定位工具链，经 GHCR 分发预构建镜像

---

## 1. 背景与问题定义

「在新环境编译、在旧环境运行」在 Linux 上被两个单向兼容的运行库阻断，且二者机制不同：

1. **glibc：链接期符号版本选择问题。** ld 只能绑定链接时 `libc.so.6` 中的 default 版本符号（如 `memcpy@@GLIBC_2.14`）；运行时 ld.so 校验 `.gnu.version_r` 中的 `GLIBC_2.xx` 需求，缺失即拒绝加载。解法方向唯一：**让链接期看到旧版本的符号表**（旧 sysroot）。
2. **libstdc++：运行库增量问题。** ABI 严格追加（GCC 11 → `GLIBCXX_3.4.29`，RHEL 8 系统库止于 `3.4.25`），旧系统只缺新增符号。解法方向：**把增量静态带在产物里**。

市场空白（2026-08 调研结论）：**「现代 GCC + 任选旧 glibc 基线 + 现成可重定位二进制」没有现成提供方**。crosstool-NG 可自建但无官方二进制、旧组合无 CI 验证；Bootlin / ARM GNU Toolchain 只发新 glibc 目标；Yocto SDK 的 glibc 钉死在发行版版本；RH gcc-toolset 只有原生编译器形态（基线继承宿主）。crossforge 的定位就是补上这个空白——形态上是一个 **Rust crate（library-first）**，供其他 Rust 项目（发布流水线、xtask、内部构建系统）集成调用，按 spec 构建出任意组合的交叉编译工具链；CLI 仅作可选薄外壳。

## 2. 已确定的决策

| # | 决策 | 内容 |
|---|------|------|
| D1 | 技术路线 | **路线 A：交叉化 devtoolset** —— 旧 glibc 二进制 sysroot（解 glibc）+ `libstdc++_nonshared.a` 混合链接（解 libstdc++）。产物与用户侧系统 libstdc++ 完全 ABI 互操作，适配「被用户程序链接的闭源 C++ SDK」形态 |
| D2 | 编译器主体 | **GCC**，默认源码基线 **RH gcc-toolset-14**（GCC 14.2.1，2025-01 快照 + RH 补丁集，Rocky 8 SRPM（bug-for-bug RHEL 复刻）；2026-08-26 修订，原为 FSF 11.5.0）。配 binutils 2.41（gcc-toolset-14 官方搭配版本；2026-08-26 由 2.40 升级——其 libsframe 静态链接集成脆弱）；FSF tarball 组件已随 2026-08-26 重构移除（fallback 由对象级裁剪承担）；Clang 可作为后续副轨接入同一套 sysroot / compat-pack |
| D3 | 默认基线 | **el8**（glibc 2.28 + GLIBCXX 3.4.25，CXX11 ABI），且为唯一内置基线。**el7 已于 2026-08-27 移除**：Rocky Linux 从 8 起步、不存在 7，el7 只能取自已 EOL 的 CentOS 7 vault——那是整个项目里唯一游离于 Rocky/RHEL 链外的来源。基线注册表是 TOML 数据，下游若仍需 el7 可自带 source 注册，无需改代码 |
| D4 | Host / Target | Host 仅 **x86_64-linux**（manifest 预留 host 维度）；Target 首发 **x86_64 + aarch64**，架构维度预留（LoongArch 等后续接入） |
| D5 | 配置格式 | 全部配置文件统一 **TOML**：分发 manifest、基线注册表、sysroot / compat-pack 元数据、用户侧 `~/.crossforge/config.toml` |
| D6 | 交付形态 | **GitHub 开源工具**（2026-08-26 修订，原为 crates.io library-first）：CLI 为默认交付形态，GitHub Actions 构建**预装工具链的 Docker 镜像**发布到 GHCR（`ghcr.io/eglinuxer/crossforge/toolchain:<baseline>-<target>`）供用户直接下载使用；library API 完整保留（`--no-default-features`），但不发布 crates.io（`publish = false`） |

排除项及理由：

- **musl 静态**：静态 musl 无法 `dlopen`，GPU 闭源驱动 `.so` 与 NSS 模块均不可加载，与 GPU SDK 场景根本冲突。
- **Zig cc / stub 路线作主力**：stub 生成对数据符号处理有已知缺陷（zig#8896）、无条件静态 libc++ 在多 DSO 场景有冲突（zig#24831）、指定不存在的 glibc 版本静默回退；仅吸收其 abilist/stub 机制用于 sysroot 瘦身（见 §5.1 v2）。
- **静态隐藏 libc++（Chromium 路线）作主力**：牺牲与用户代码的 C++ ABI 互操作，仅适合纯 C ABI 边界产物；保留为后续副轨。

## 3. 概念模型：三类正交产物

核心设计：**编译器本体、glibc 基线、libstdc++ 基线解耦**，避免 crosstool-NG 式「每个组合完整重建」的笛卡尔积爆炸。

```
toolchain   编译器本体        per (host, target-arch)      与基线无关，跨基线共享
sysroot     目标根文件系统骨架  per (target-arch, baseline)   从旧发行版二进制包抽取
compat-pack libstdc++ 兼容包   per (toolchain, baseline, target-arch)
```

实现注记（v0.4）：当前实现中每个 `spec.id()`（gcc × baseline × target）产出一个自包含的
relocatable prefix（内嵌 sysroot 与 compat-pack），「编译器本体跨基线共享」保留为后续
store 层的去重优化；概念模型不变。此外强制旧 ABI 的基线（`cxx11_abi = false`）会把
`--with-default-libstdcxx-abi=gcc4-compatible` 烧入该基线的编译器构建（见 §4.3），
编译器本体因此本就与基线相关。

一次完整的工具链组装 = `toolchain × sysroot × compat-pack`，由 crate 的 build API 按 `ToolchainSpec` 驱动构建，并在本地 store 中按内容寻址拼合、增量复用（同一 sysroot 可配多个 toolchain）。

### 3.1 Target triple 命名

vendor 位采用通用的 `unknown`（2026-08-26 修订，原为自定义 `mt`——开源通用工具定位下不再绑定厂商标识）：

```
x86_64-unknown-linux-gnu
aarch64-unknown-linux-gnu
```

### 3.2 基线注册表（baseline registry）

基线是一等公民，每个基线定义一组不可变的版本上限：

| 别名 | glibc | kernel headers | libstdc++ 基线 | CXXABI | Dual ABI | 包源 |
|------|-------|----------------|----------------|--------|----------|------|
| `el8`（默认，唯一内置） | 2.28 | 4.18 | GLIBCXX_3.4.25（GCC 8.5） | 1.3.11 | `_GLIBCXX_USE_CXX11_ABI=1` | Rocky Linux 8（x86_64 / aarch64，2026-08-26 修订：原 AlmaLinux——Rocky 坚持 bug-for-bug 复刻 RHEL，Alma 仅承诺 ABI 兼容且带自有修订） |

**供应链单一化（2026-08-27）**：容器基底、sysroot 包源、gcc-toolset SRPM 现已全部来自 Rocky Linux，无一例外。原 `el7` 基线随之移除——Rocky 只发布 8/9/10，el7 内容只能取自 EOL 的 CentOS 7 vault，为一条已停止维护的链外来源；与其长期维持"容器 Rocky、sysroot CentOS"的割裂，不如收敛。

预留：`el9`（Rocky 9，glibc 2.34，正好对应 manylinux_2_34）、`el10`、`u20`（glibc 2.31）、欧拉/龙蜥（LoongArch 基线）等，注册表数据化（TOML），新增基线不改代码。

关于强制旧 ABI 的基线（`cxx11_abi = false`）：若基线库完全没有 `__cxx11` 符号（如 GCC 4.8 时代的 el7），允许 CXX11 ABI 会让全部字符串相关符号进入 nonshared 静态段，体积暴涨且与用户系统 GCC 编译的代码无法互传 `std::string`。机制保留在 §4.3，供下游自行注册这类基线时使用。

## 4. 关键机制

### 4.1 glibc 侧：二进制 sysroot，不从源码构建旧 glibc

sysroot 由旧发行版**二进制包**（RPM）抽取拼装：`glibc`、`glibc-devel`、`glibc-headers`、`kernel-headers`、`libgcc`、`libstdc++`（运行库，供链接期符号表用）。

不走 crosstool-NG 的「新 GCC 从源码编旧 glibc」路径——那是其主要痛点来源（`-fcommon`/`--disable-werror` 补丁兜底、组合无 CI 验证）。这带来一个重要简化：**sysroot 里已有完整 glibc，编译器构建无需三阶段 bootstrap**——直接 binutils → GCC（`--with-sysroot` + `--with-build-sysroot`）一遍完成，libgcc / libstdc++ 对着现成 glibc 配置。

### 4.2 libstdc++ 侧：nonshared 混合链接（devtoolset 机制的交叉化）

nonshared 归档有**两个来源**（v0.5 起，`NonsharedSource` 枚举）：

- **RedHat（默认基线优先）**：默认源码基线是 RH gcc-toolset-14 的 SRPM（GCC 14.2.1 + 42 个补丁），其中 `gcc14-libstdc++-compat.patch`（万行级）在 libstdc++ 源码树内新增 `src/nonshared{98,11,17,20}/` 目录——人工精修的显式实例化文件、按架构条件的 `asm(".hidden <sym>")` 可见性清单、RTTI 汇编 stub——构建期直接产出 `libstdc++_nonshared{44,48,80,110}.a` 四级基线归档（RHEL 6/7/8/9）。基线注册表以 `rh_nonshared` 字段（el8→`80`）声明采用哪级。**关键构建约束**：target 库必须带 `-D_GLIBCXX_ASSERTIONS` 编译（RH optflags 隐式约定）——它禁用 libstdc++ 头文件的 extern-template 声明，RH 的 `.hidden` 清单依赖 nonshared 对象据此自行发射 weak hidden 实例化；缺了它链接期会出现无定义的 hidden 符号（已实测踩坑并修复，CXXFLAGS_FOR_TARGET 注入）。
- **Pruned（fallback）**：对无 RH 补丁的源码组合（如 FSF tarball），用完整 `libstdc++.a` 按基线 abilist 做对象级自动裁剪——剔除「只含基线已有符号」的对象。精度低于 RH 方案（保留对象内混有基线符号、多 DSO 副本面更大），但任意 (GCC × 基线) 组合零人工。
2. **linker script `libstdc++.so`**：

   ```
   INPUT ( =/usr/lib64/libstdc++.so.6 -lstdc++_nonshared )
   ```

   `=` 前缀由 ld 解析为 sysroot 内路径，保持整体可重定位。安装到工具链内部搜索路径（`lib/gcc/<triple>/<version>/`），天然优先于 sysroot 中的真实 `libstdc++.so`。
3. 链接行为：基线库已有的符号 → 动态绑定（带旧版本号）；新增符号（`std::from_chars` 浮点版本、GCC 9 起并入主库的 `std::filesystem` 符号、C++20 库设施等）→ 从 nonshared 静态链入。运行时产物仅 `DT_NEEDED` 系统 `libstdc++.so.6` 且只引用其确有的符号。

基线 abilist 的权威来源是**目标发行版 `libstdc++.so.6` 的动态符号表**（随 sysroot 抽取时一并生成），GCC 源码树 `libstdc++-v3/config/abi/post/<triple>/baseline_symbols.txt` 作交叉验证。

ABI 不稳定的次要运行库（libgfortran 等）按 devtoolset 同款策略纯静态处理（v1 不含 Fortran，预留）。

### 4.2.1 libgcc 侧：同构的混合链接（2026-08-27）

libstdc++ 不是唯一会「编出基线跑不了的产物」的运行库，`libgcc_s` 有完全相同的问题，此前被漏掉。由 Qt 6 端到端样例暴露：阶段 1 产出的 host 工具启动即死于

```
rcc: /lib64/libgcc_s.so.1: version `GCC_12.0.0' not found (required by libQt6Core.so.6)
```

**缺口范围**（工具链 GCC 14.2.1 的 `libgcc_s` 导出集 减 el8 基线导出集）：

| 架构 | 新导出 | el8 基线 | 基线缺失 |
|------|--------|----------|----------|
| x86_64 | 183 | 139 | **44** |
| aarch64 | 155 | 122 | **33** |

缺失符号分四类：`_Float16` 转换助手（GCC 12）、`bfloat16` 转换（GCC 13）、`_BitInt` 运行时（GCC 14 的 `__mulbitint3` / `__divmodbitint4`）、以及 `__strub_*` / `__hardcfr_check`（GCC 14 加固设施）。Qt 6 经 `qfloat16` 命中第一类——且**只在 x86_64 命中**：aarch64 的半精度转换走硬件 `fcvt`，编译器不发 libgcc 调用，故其 libgcc.a 里根本没有这些成员。但 aarch64 同样有 33 个缺口（如 `__extendhftf2`、`_BitInt`），只是 Qt 没触发，属潜在雷。

**只有 C++ 受影响**，这是漏检这么久的原因。`gcc` 的 libgcc spec 是 `-lgcc --as-needed -lgcc_s --no-as-needed`，静态归档排在前面，缺失符号早就被静态解析掉了；而 `g++` 默认 `-shared-libgcc`，其 spec 在构建共享库时**只给 `-lgcc_s`**，libgcc.a 根本不上链接行，于是只能动态绑定到新版本号。

**修法**与 4.2 同构——在工具链内部搜索路径安装链接脚本 `libgcc_s.so` 遮蔽真实库：

```
INPUT ( =/usr/lib64/libgcc_s.so.1 -lgcc )
```

三点值得记：

1. **不需要裁剪归档**。libstdc++ 必须裁剪（静态链入整个 `libstdc++.a` 会造成 `std::string` 等多副本与全局状态分裂）；libgcc 不必：归档的惰性提取语义天然只捞「当前仍未定义」的成员，基线 `.so` 排在前面已把能答的都答了，剩下的才从归档取，并自动拉入传递依赖（实测 `__sfp_handle_exceptions` 被自动带入）。所以这里零构建步骤，只有一个静态文本文件。
2. **unwinder 必须保持共享**，否则跨 DSO 抛异常会因多份 FDE 注册表而失效——这正是 `-shared-libgcc` 存在的理由。此处的保证是**结构性**的而非靠约定：`_Unwind_*` 只存在于 `libgcc_eh.a`，`libgcc.a` 里一个都没有（实测 0 vs 31），归档根本无法提供它，只能来自基线 `.so`。实测产物 `_Unwind_Resume@GCC_3.0` 保持 UND。
3. **静态链入的成员是 PIC 安全的**。这些转换函数代码段内只有 `R_X86_64_PLT32` / `R_X86_64_PC32`，绝对重定位全在 `.debug_*` 段。

**验证**：`crossforge audit` 本就能检出此问题（`[symbol-version] requires libgcc_s.so.1@GCC_12.0.0 but the baseline does not provide it`）——即工具链一直在生产自己 audit 会拒收的产物。修复后两架构均 PASS，产物版本依赖回落到 `GCC_3.0` / `GLIBC_2.2.5` / `GLIBCXX_3.4.21` / `CXXABI_1.3`，全部在基线内。

### 4.3 默认注入的编译/链接选项

wrapper（或生成的 toolchain file）按基线注入：

| 选项 | 原因 |
|------|------|
| `--sysroot=<sysroot>` | 基线头文件与链接库（实现：GCC `--with-sysroot` 烧入，无需 wrapper 注入） |
| `-Wl,-z,nopack-relative-relocs` | 阻断 DT_RELR → `GLIBC_ABI_DT_RELR` 版本依赖（binutils ≥2.38 环境下旧机器的隐形地雷；工具链自带 binutils 2.41 默认不开 DT_RELR，audit 兜底检查） |
| 旧 string ABI（`cxx11_abi = false` 的基线） | 实现为 GCC configure `--with-default-libstdcxx-abi=gcc4-compatible`（编译器默认 `_GLIBCXX_USE_CXX11_ABI=0`，比 wrapper 注入宏更不可绕过），构建时输出 WARN 警示 |

注意不强制 `-std=`：默认 toolchain（GCC 14，默认 `gnu17`）不受 C23 符号重定向影响；后续 GCC 15+ toolchain 默认 `gnu23`，会使 `strtol` 等重定向到 `__isoc23_*@GLIBC_2.38`——el8 sysroot 的旧头文件天然不含该重定向，此风险仅存在于误用宿主头文件时，由 audit 兜底检出。

## 5. crate 架构

```
crossforge (lib)
├── spec       ToolchainSpec / 基线注册表 / target 定义（TOML 数据驱动，可由调用方扩展）
├── fetch      源码 tarball 与发行版包的下载、校验、缓存（可配镜像源）
├── sysroot    Layer 1：发行版包抽取 + abilist 提取
├── compiler   Layer 0：binutils + GCC 构建编排（Runner 抽象：本地 / 容器执行）
├── compat     Layer 2：libstdc++ nonshared 裁剪 + linker script 生成
├── pack       relocatable 打包 + TOML manifest 产出
├── audit      Layer 4：ELF 符号审计门禁；verify：旧发行版容器矩阵冒烟
└── cli        （feature = "cli"）薄封装，演示与手工操作
```

全部能力都在 crate 内以公开 API 暴露，各阶段独立可调用也可一键全流程；**在哪里执行由调用方决定**（典型形态：调用方的 CI 发布流水线离线构建、产物签名后入其分发源）。构建 GCC 所需的 el8 宿主环境通过 `Runner` 抽象注入——本地直跑或 podman/docker 容器内执行。

### 5.1 Layer 1：sysroot 生成器

- 输入：基线注册表条目（包源 URL、包名单、目标 arch）。
- 处理：RPM 解包 → 路径规范化（`usr/include`、`usr/lib64`、`lib64`）→ 修复绝对路径符号链接为相对 → 提取 `libc/libm/libpthread/libstdc++/libgcc_s` 的 abilist 存入 metadata → 内容寻址打包（zstd tar + sha256）。
- 产出 metadata（TOML）：基线各库版本、源包 NVR 列表、abilists、生成器版本。
- v2 预留：Zig 式 stub 化（库文件瘦身为纯动态符号表，sysroot 从数百 MB 压至数 MB）；Chromium 式 reversion（改写符号版本表进一步压低基线）。

### 5.2 Layer 0：toolchain 构建流水线

- 构建环境：el8 容器（host 工具因此要求宿主 glibc ≥ 2.28；后续可选静态 host 工具或 Yocto 式 interpreter 改写进一步放宽）。
- 流程：binutils → GCC（一遍，见 §4.1），`--with-sysroot` 指向占位路径并依赖 GCC 自身的重定位逻辑（sysroot 为 prefix 子路径时随安装位置平移）。
- 每个 (host, target) 一份产物；GCC 树内 libstdc++ 的完整静态库同时留档，供 Layer 2 裁剪。

### 5.3 Layer 2：compat-pack 生成器

- 输入：toolchain 留档的 libstdc++ 构建产物 + 基线 abilist。
- 处理：对象级裁剪 → 链接脚本生成 → 符号覆盖率自检（nonshared ∪ 基线 abilist ⊇ 完整新版 libstdc++ 导出集）。
- 端到端验收：C++20 样例（含 `std::from_chars` 浮点、`std::filesystem`、`std::ranges`、异常跨 so）在基线容器中运行通过。

### 5.4 crate API 与产物格式

核心 API 形状（M0 起稳定演进）：

```rust
use crossforge::{BaselineRegistry, BuildEngine, BuildConfig, TargetArch, ToolchainSpec};

let registry = BaselineRegistry::builtin();      // 内嵌 el8 内置，可 merge_toml() 扩展
let spec = ToolchainSpec::builder()
    .gcc("14.2.1")                               // 默认即 gcc-toolset-14，可省略
    .target(TargetArch::Aarch64)
    .baseline("el8")
    .build(&registry)?;                          // 校验基线存在且支持该 arch

let engine = BuildEngine::new(BuildConfig {
    work_dir: "/build/crossforge".into(),
    cache_dir: "/build/cache".into(),
    mirror: None,                                // 源码/RPM 镜像源
});
let artifact = engine.build(&spec, &registry)?;  // → 可重定位工具链目录 + manifest
```

设计要点：

- **各阶段独立成 API**：`sysroot::generate(...)`、`compiler::build(...)`、`compat::generate(...)`、`audit::check(...)` 均可单独调用，`BuildEngine::build` 是按依赖串起来的一键全流程；
- **`Runner` trait** 抽象命令执行（`LocalRunner` / 容器 Runner），调用方可注入自有执行环境；
- 错误用 `thiserror` 枚举（`#[non_exhaustive]`），过程日志走 `tracing`，长任务提供进度回调；
- `TargetArch` / 基线注册表均 `#[non_exhaustive]` / 数据驱动，对应 D4 的架构预留。

Store 布局（内容寻址 + 组装视图，根目录由 `BuildConfig` 指定；CLI 默认 `~/.crossforge`）：

```
~/.crossforge/
  store/sha256-<hash>/          # 只读、内容寻址
  toolchains/gcc14-el8/         # 组装视图（symlink/hardlink + wrapper）
    bin/x86_64-unknown-linux-gnu-g++
    bin/aarch64-unknown-linux-gnu-g++
  manifests/
```

Manifest（TOML，channel 模式，rustup 风格）：

```toml
[manifest]
schema = 1
channel = "stable"
date = "2026-08-25"

[[toolchain]]
id = "gcc14"
version = "14.2.1"
host = "x86_64-linux"          # 预留维度
target = "aarch64-unknown-linux-gnu"
url = "https://…/crossforge-toolchain-gcc14.2.1-x86_64_host-aarch64.tar.zst"
sha256 = "…"

[[sysroot]]
baseline = "el8"
arch = "aarch64"
glibc = "2.28"
source = "rocky-8.10"
url = "…"
sha256 = "…"

[[compat]]
toolchain = "gcc14"
baseline = "el8"
arch = "aarch64"
glibcxx = "3.4.25"
cxxabi = "1.3.11"
url = "…"
sha256 = "…"
```

可选 CLI（feature = "cli"，对 API 的一比一薄封装）：

```
crossforge build --baseline el8 --target aarch64   # 构建一条完整工具链
crossforge sysroot --baseline el8 --target x86_64               # 单独跑某一阶段
crossforge audit build/libfoo.so --baseline el8 [--fix]
crossforge verify --baseline el8 --matrix                       # 容器矩阵真机测试
```

产出的工具链自带集成物：wrapper 注入（默认，零改造）、`environment-setup` 脚本（Yocto 习惯）、CMake toolchain file / Bazel toolchain 声明生成。

### 5.5 Layer 4：audit / verify

`crossforge audit`（auditwheel 的泛化，作为 CI 门禁）逐 ELF 检查：

1. `GLIBC_*` 最高版本 ≤ 基线（对照 sysroot 携带的 abilist，逐库）；
2. `GLIBCXX_*` / `CXXABI_*` ≤ 基线；`GCC_*`（libgcc_s）≤ 基线；
3. 版本需求中不含 `GLIBC_ABI_DT_RELR`；
4. 不含 `__isoc23_*` 等宿主头文件泄漏特征（提示 sysroot 未生效）；
5. `DT_NEEDED` 白名单（libc/libm/libpthread/libdl/librt/libgcc_s/libstdc++ + 声明的自带库）；
6. 可执行文件 `PT_INTERP` 合法性。

`--fix` 预留接 polyfill-glibc（链接后降级符号版本），定位为存量二进制救急，不进默认流水线。

`crossforge verify` 在基线及更新的发行版容器矩阵（rockylinux8 / ubuntu20.04 / debian11 …）中做真机 exec + dlopen 冒烟，防审计规则遗漏。

`crossforge check` 跑 GCC 官方 DejaGnu 测试集（check-gcc / check-c++ / check-target-libstdc++-v3）：自动生成 board 文件（x86_64 直接执行——产物基线低于构建容器；aarch64 走 qemu-user + sysroot），解析 `.sum` 产出统计与 FAIL 明细，`--max-unexpected-failures` 可作门禁。2026-08-26 最终成绩（gcc14.2.1-el8-x86_64，Rocky 供应链 + 全部环境修复后）：**gcc 191,591 / c++ 256,746 / libstdc++ 17,801 passes（合计 466,138），gcc 与 c++ 两大 suite 零 unexpected failures**；libstdc++ 仅余 3 个：2 个为 RH dts-test 补丁标注的基线语义差异（string::reserve 收缩走基线旧语义——正是 nonshared 的设计行为）+ 1 个容器无 DNS 的网络测试，零工具链归因缺陷。首轮测试集还抓出一个真实功能缺陷：构建容器缺完整 gconv 模块导致 GCC configure 禁用 iconv、cc1 静默丢失 -fexec-charset（已修，见 buildenv 依赖）。踩坑记录：DejaGnu 在 `/etc/passwd` 无当前 UID 的容器里 `exec whoami` 崩溃（注入 USER 环境变量解决）；`set_board_info` 不覆盖既有值，需先 `unset_board_info isremote`；RH dts.exp 的版本探测不兼容单段 `-dumpversion` 输出（幂等改写该 proc）。

## 6. 非目标（v1）

- 不做第三方库的包管理 / 交叉编译（sysroot 结构对扩展开放，但不承诺）；
- 不做 rustup 式终端用户安装/更新管理——预构建工具链以 GHCR Docker 镜像分发（`docker pull` 即用），manifest/tar.zst 供非容器场景自取；
- 不替代构建系统，只供给工具链与环境；
- 不支持 musl / 全静态目标；不支持 Windows / macOS host；
- 不做 Canadian cross（manifest 已预留 host 维度）。

## 7. 里程碑

| 里程碑 | 内容 | 验收 | 状态 |
|--------|------|------|------|
| M0 | crate 骨架：spec / 基线注册表（内嵌 TOML，可扩展）/ target 类型、error + tracing 基建、`Runner` 抽象、`BuildEngine` API 形状 | `cargo test` 绿；API 形状评审通过 | ✅ 2026-08-25 |
| M1 | fetch + sysroot 模块：el8 × {x86_64, aarch64}，RPM 抽取与 abilist 提取 | sysroot 可被系统编译器 `--sysroot` 试链接 | ✅ 2026-08-25（gcc 试链接通过，产物仅需 GLIBC_2.2.5/2.4） |
| M2 | compiler 模块：容器 Runner 构建 binutils 2.40 + GCC 11.5 双 target；重定位验证 | 任意目录解包可编 hello world | ✅ 2026-08-26（重定位零参数编译；aarch64 经 qemu 运行） |
| M3 | compat 模块：nonshared 裁剪 + 链接脚本集成 | C++17/20 样例在 el8 容器运行；`objdump -T` 无超基线符号 | ✅ 2026-08-26（186 成员保留 109；GLIBCXX 需求 3.4.29→3.4.21） |
| M4 | audit 模块 + verify 容器矩阵 | 检查项覆盖 §5.5；可作调用方 CI 门禁 | ✅ 2026-08-26（almalinux8/rocky8/ubuntu20.04/debian11 全 PASS） |
| M5 | el7 基线（含强制旧 ABI 与警示）、pack/manifest 稳定化、feature "cli" 薄封装、GitHub 开源 + GHCR 镜像流水线 | 双基线双 target 全矩阵绿 | ✅ 2026-08-26（el7 产物 centos:7→debian:11 全 PASS；CI/toolchain-images workflows 就位）。**注**：el7 基线已于 2026-08-27 随供应链单一化移除，机制（强制旧 ABI、nonshared48）保留 |

## 8. 风险与开放问题

1. **nonshared 的持续维护**：默认路线已切换为直接消费 RH gcc-toolset SRPM（人工精修由 RH 承担，跟随其大版本节奏即可）；自研裁剪仅作 fallback。遗留任务：把 RH spec 的 nonshared 验证法（whole-archive 试链 + `readelf` 查 hidden UND + abilist 并集校验）自动化进 compat 模块，作为两种来源共同的构建期门禁——本次 `_GLIBCXX_ASSERTIONS` 踩坑本可由它在构建期拦截。
2. **多 DSO 各持 nonshared 副本的边角**：新特性类型的 typeinfo/vtable 多副本，异常跨 DSO 依赖 libstdc++ 的 strcmp fallback——SDK 发布物内部统一由 crossforge 工具链一次性链接可规避；audit 对同进程多副本场景仅能提示。
3. **旧 glibc 头文件 × 新 GCC 的小摩擦**：2.17 headers 在 `gnu23` 下的个别不兼容（如 `bool` 宏冲突）可能需要头文件级微补丁，随 sysroot 生成器维护。
4. **host 兼容门槛**：v1 要求宿主 glibc ≥ 2.28（el8 构建）；如需覆盖更旧 CI 宿主，启用静态 host 工具选项。
5. **LoongArch 基线**：欧拉/龙蜥的包源与 abilist 差异待调研，仅保留架构接口。

## 9. Python wheel 支持（2026-08-26 逐项评审定案）

目标：crossforge 具备「交叉构建 manylinux 合规、多版本 Python wheel」的**通用基础设施能力**（不绑定具体项目），支撑公开 PyPI 与自建 index 双渠道分发。八项决策（T1-T8）：

| # | 议题 | 决策 |
|---|------|------|
| T1 | 需求画像 | 通用能力；绑定层未选定（倾向 nanobind）；CPython 3.9–3.13（不含 free-threaded）；PyPI + 自建源 |
| T2 | manylinux 基线 | **仅 manylinux_2_28**（= el8 基线，工具链同款 gts14）；el7 本就不在 wheel 范围（旧 COW-ABI 税实证），后已整体移除；musllinux 不做（dlopen 冲突） |
| T3 | Python 物料 | **自建交叉 CPython**（供应链自主）：每版本先原生构建 x86_64（兼作 build-python 与 x86 物料）再交叉构建 aarch64；cp311+ 走官方 `--with-build-python`，cp39/310 走 CONFIG_SITE 老式 cross；五版本一次性交付；configure 对齐 manylinux 口径（`--disable-shared --with-ensurepip=no`）；目标依赖（openssl/libffi/zlib 等 -devel）经现有 sysroot 包列表机制提供；官方 manylinux 镜像提取版仅作 CI 对照基准（diff pyconfig.h/sysconfigdata） |
| T4 | ABI 策略 | 双路线：per-version 默认（5 版本 × 2 架构）；abi3 作构建选项（近零成本，验证侧展开多解释器 import）；cp39 档 Limited API 缺 buffer protocol（3.11 才有），分档策略留给具体项目 |
| T5 | 能力层级 | **端到端 `crossforge wheel`**（交叉版 cibuildwheel）：项目目录 → 全矩阵 wheel + 审计；环境生成器（crossenv 伪 venv + CMake/Meson cross 文件 + PYO3 变量，四类构建后端通吃）作为内部层 |
| T6 | 合规审计 | **自研 wheel 审计 + 自研 vendor**：内嵌 manylinux policy 数据表（注意 _2_28 的 GLIBCXX 上限 3.4.24 比 el8 系统库严一档，与 auditwheel JSON 做 CI 对照）；检查符号上限/23 库白名单/禁链 libpython/EXT_SUFFIX-tag 一致/RECORD；vendor 含 patchelf 级 soname/RPATH ELF 改写（纯 Rust）；`--exclude` 驱动库机制（libcuda 类） |
| T7 | 验证矩阵 | 三层：静态审计（必过）→ qemu/本机 import 冒烟（必过，用自建 CPython 执行）→ `--verify-images` 装机层（manylinux 容器全矩阵 + 发行版容器抽样，可选）；abi3 自动展开五解释器 |
| T8 | CI 分工 | **交叉构建、原生终检**：构建与前两层验证全在 x86 交叉完成（内网/GitHub 通用）；原生 arm（GH arm64 runner 已 GA）只做发布前装机终检与性能测试 |

关键调研结论（支撑上述决策）：manylinux_2_28 镜像 = AlmaLinux 8 + gcc-toolset-14（与我们同款）；Linux 扩展交叉物料仅需目标 `pyconfig.h` + `_sysconfigdata_*.py`（不链 libpython）；setuptools 交叉唯一通路是 `_PYTHON_SYSCONFIGDATA_NAME` + `_PYTHON_HOST_PLATFORM` + crossenv（conda-forge 生产验证，PEP 720 仍 Draft）；pybind11 至今无 abi3，nanobind 2.0+/PyO3 支持良好；free-threaded（cp313t/cp314t）与 abi3 不兼容，PEP 803（abi3t）落地 3.15，暂不入范围。

### 9.1 里程碑

| 里程碑 | 内容 | 验收 | 状态 |
|--------|------|------|------|
| M6 | python-pack：交叉 CPython 构建流水线（cp39–cp313 × x86_64/aarch64），sysroot 包列表扩展（openssl-devel 等），官方镜像对照门禁 | 十个 python-pack 产出；pyconfig.h/sysconfigdata 与官方 manylinux 对照 diff 清洁；aarch64 树在 qemu 下可执行 import | ✅ 2026-08-27（十包全产出并冒烟通过；对照门禁 10/10 PASS、ABI 关键集零差异） |
| M7 | `crossforge wheel` 端到端：环境组装（crossenv/cross files/PYO3）+ 四类后端编排 + wheel 静态审计（policy 表）+ import 冒烟 + abi3 展开 | 一个 pybind11/nanobind 样例项目与一个 setuptools 样例项目，一条命令产出全矩阵合规 wheel，manylinux 容器全版本 import 通过 | ✅ 2026-08-27（18 wheels 全绿：setuptools 10/10、nanobind 8/8（cp39 被 requires-python 过滤）；全部通过官方 manylinux 双架构容器 import 终检） |
| M8 | 自研 vendor（ELF soname/RPATH 改写）+ `--verify-images` 装机层 + CI 集成（wheel 维度产物与 arm 终检 job） | vendor 结果与 auditwheel repair 产物等价性对照；CI 全绿 | ✅ 2026-08-27（结构对照同构：.libs 布局/hash 改名/NEEDED-SONAME-verneed 改写/传递闭包一致，差异仅 RUNPATH vs 其老式 RPATH；三样例全矩阵含 vendor 路径全绿；CI wheels + arm 终检 job 就位） |

### 9.2 M6 实施记录（2026-08-27）

`crossforge python` 子命令 + `PythonBuilder` API（`src/python.rs`）落地，一条命令产出全矩阵：每版本先在 el8 构建容器内用本工具链**原生构建 x86_64**（同时充当 build-python，产物自身满足 glibc ≤ 2.28 可移植），再**交叉构建 aarch64**（3.11+ 走官方 `--with-build-python`；3.9/3.10 走 PYTHON_FOR_BUILD 老式通路，setup.py 从 sysroot 化的 `$CC -E -v` 输出推导目标头/库路径）。要点：

- **微版本与官方镜像完全同步**（3.9.25 / 3.10.21 / 3.11.16 / 3.12.14 / 3.13.15，皆为各线最新），configure 口径同款（`--disable-shared --with-ensurepip=no`、prefix `/opt/_internal/cpython-<v>`），对照 diff 天然最小。
- **对照门禁**（`scripts/compare-manylinux-python.py`）：解析双方 pyconfig.h `#define` 全集与 `_sysconfigdata_*.py`，ABI 关键集（SIZEOF/ALIGNOF/端序/EXT_SUFFIX/SOABI/ABIFLAGS 等）不一致即失败。十组全 PASS、ABI 零差异；非 ABI 差异均已归因（官方多装 curses/readline/ossp-uuid；编译器路径/标志字符串）。
- **交叉 configure 预置**：除 `/dev/ptmx`+`/dev/ptc` 必答项外，预置四个 run-test 探测的悲观默认（`ac_cv_computed_gotos=yes`、`ac_cv_aligned_required=no`、`ac_cv_broken_sem_getvalue=no`、`ac_cv_working_tzset=yes`），与原生（官方镜像）答案对齐——否则交叉包 eval loop 退化为 switch 分发。
- **目标依赖经 sysroot 包列表**：rocky-8 源扩 14 包（zlib/bzip2/xz/libffi/openssl/sqlite/libuuid 的运行库+devel）+ PowerTools 仓；同款 devel 包也进构建镜像（原生构建的 setup.py 探测**宿主**目录而非 sysroot——两侧同 NVR 保证原生/交叉包 stdlib 对称）。
- **冒烟门禁**：每包必须 `import math, struct, json, zlib, bz2, lzma, ctypes, ssl, hashlib, sqlite3, uuid`；x86_64 直跑、aarch64 经 qemu + 工具链 sysroot。10/10 通过。
- **生产裁剪**（对齐官方镜像）：strip 解释器与扩展、删嵌入专用静态 libpython（两处安装位）、删 test 目录——单包 305MB → 64–90MB（官方 66MB 同量级）。
- 踩坑存档：Rocky 极简镜像缺 `which` 使 3.9/3.10 交叉 configure 的候选循环静默残留裸 `python`（探测循环 `which ... || continue` 全跳过后循环变量未清空）；`/dev/ptc` 是 AIX 探测项而非 ptem。

### 9.3 M7 实施记录（2026-08-27）

`crossforge wheel <project>` 一条命令：项目目录 → 全矩阵 manylinux_2_28 合规 wheel。四个新模块：

- **whl**：自研最小 zip 读写（stored/deflate，flate2）+ PEP 427 文件名解析 + RECORD 校验/重算 + **retag**（审计通过后 `linux_*` → `manylinux_2_28_*`，改 WHEEL Tag 行、刷新 RECORD、可复现固定时间戳）。
- **wheelaudit**：内嵌 policy 表（`registry/manylinux-policy.toml`，转录自 auditwheel 6.8.1，含符号版本上限/24 库白名单/libz 私有符号黑名单）；检查 ELF arch、版本化符号需求 ≤ 上限（**GLIBCXX 3.4.24 确实比 el8 系统库严一档**）、DT_NEEDED ∈ 白名单 ∪ wheel 内相邻库、禁链 libpython、EXT_SUFFIX 与 python/abi tag 一致（abi3 wheel 不得携带版本化后缀）、RECORD 完整性。审计不过不 retag。
- **wheel**（编排）：native pack 建 venv + 钉版 pip wheel（25.2，3.9 支持线的末代）经 PYTHONPATH 引导 → 装 PEP 518 requires（`--no-build-isolation`，隔离环境会覆盖交叉 PYTHONPATH）→ 交叉环境注入（`_PYTHON_SYSCONFIGDATA_NAME` + `_PYTHON_HOST_PLATFORM` + 仅含目标 sysconfigdata 单文件的 PYTHONPATH；`CMAKE_TOOLCHAIN_FILE` + `-DPython_INCLUDE_DIR/SOABI` hints；`SETUPTOOLS_EXT_SUFFIX`；`PYO3_CROSS_LIB_DIR` 接口就绪）→ `pip wheel` → 审计 → retag → 三层验证。**原生 x86 wheel 同样走本工具链**——policy 的 GLIBCXX 上限连 el8 系统 libstdc++ 都超（3.4.25>3.4.24），nonshared 混合链接是原生轮子合规的前提，不只是交叉的。
- **requires-python 过滤**：读 `[project].requires-python` 下限跳过项目不支持的解释器（nanobind≥2.2 不支持 3.9 即由此路径处理）。

验收（2026-08-27 实测）：`examples/wheel-setuptools`（纯 C）10/10、`examples/wheel-nanobind`（C++/scikit-build-core/CMake）8/8（cp39 过滤），共 18 wheels；逐一通过 policy 审计、目标 pack import 冒烟（aarch64 经 qemu）、官方 `quay.io/pypa/manylinux_2_28_{x86_64,aarch64}` 容器内官方解释器 import 终检（aarch64 经宿主 binfmt qemu 运行 arm64 容器）。C++ 样例特意使用 `std::from_chars`（浮点）与 `std::filesystem`（GLIBCXX_3.4.29+ 物料）：产物 DT_NEEDED 仅 4 个白名单库、动态需求 GLIBCXX ≤ 3.4.21 / GLIBC ≤ 2.17，147 个相关符号以本地定义静态携带——nonshared 机制在 wheel 场景的直接实证。构建环境镜像补充 cmake + ninja-build（PowerTools）。abi3：构建选项就绪（产出 abi3 tag 自动跳过同架构后续版本并展开全解释器冒烟）；PyO3/maturin 环境接口就绪、端到端样例留 M8 顺带验证。


### 9.4 M8 实施记录（2026-08-27）

自研 vendor 三件套落地，`crossforge wheel` 至此覆盖 auditwheel repair 的全部职责：

- **elfpatch**（patchelf 级 ELF 改写，纯 Rust）：DT_SONAME/DT_NEEDED 改名 + DT_RUNPATH 注入。策略为 **append-only 搬迁**——新 `.dynstr`（旧内容 + 追加串，全部旧偏移原样有效，符号名/verneed 引用天然不破）与新 `.dynamic` 置于文件末尾新建的 RW `PT_LOAD`，连同扩容的 program-header 表整体搬入；改指 e_phoff、PT_PHDR/PT_DYNAMIC 与对应 section header。**关键踩坑**：`.gnu.version_r` 的 `vn_file` 库名必须随 DT_NEEDED 同步改写——漏改会直接触发 ld.so 断言（dl-version.c `needed != NULL`），patchelf 同样处理此项。单元测试含 dlopen 实跑验证。
- **vendor**：非白名单 DT_NEEDED（含经 vendored 库的传递闭包，从工具链 sysroot + `--vendor-path` 解析、按 e_machine 校验架构）复制进 `<distribution>.libs/`，内容 hash 改名（soname 首个 `.so` 前插 `-<8hex>`，同 soname 不同构建可同进程共存）；vendored 库自设 `RUNPATH=$ORIGIN`、扩展模块按目录深度设 `$ORIGIN/<ups><pkg>.libs`；RECORD 全量重建。vendor 后强制复审计，不过不 retag。
- **`--exclude`**：驱动类库（libcuda.so.1 形态）声明为运行环境提供——既不 vendor 也不报错（审计白名单临时并入）。
- **`--verify-images` 装机层**：任意镜像列表内用镜像自身解释器 import（manylinux `/opt/python` 布局优先，退化到版本匹配的 `python3`，不匹配跳过并警告），`--verify-manylinux` 成为官方镜像的便捷别名。
- **policy 表修正**：白名单补动态加载器（`ld-linux-{x86-64.so.2,aarch64.so.1}`）——auditwheel 运行时按架构注入同款；aarch64 上 glibc 库直接 NEEDED ld-linux，漏了会把加载器本身 vendor 进 wheel（实测踩到）。
- **等价性对照**（验收，2026-08-27 实测）：同一 demossl 项目分别经官方 manylinux 容器内 `auditwheel repair` 与本工具处理，结构同构——`.libs` 布局、hash 命名模式、扩展模块与 vendored 库的 NEEDED/SONAME 改写、libssl→libcrypto 传递闭包全部一致；差异仅（a）我们发 RUNPATH、auditwheel 发老式 RPATH（语义等价，每个 vendored 库自带 RUNPATH 无传递搜索缺口），（b）auditwheel 以真实文件名（.so.1.1.1k）为基、我们以 soname 为基，（c）auditwheel 附带 SBOM。三样例（setuptools/nanobind/vendored）全矩阵（vendored 双架构含 qemu 冒烟与官方双架构容器终检）全绿。
- **abi3 实测**（`examples/wheel-abi3`，Limited API 3.9 + `py_limited_api`）：每架构仅构建一次（cp39-abi3 tag），同架构后续版本自动跳过；冒烟自动展开全部五个解释器（x86 原生 + aarch64 qemu，10/10 import 通过），官方双架构容器装机通过。交叉构建下 setuptools 的 `.abi3.so` 后缀不受 SETUPTOOLS_EXT_SUFFIX 干扰（审计的 ext-suffix 检查兜底）。
- **vendor 清单**：vendor 时向 `<dist>.dist-info/crossforge-vendor.toml` 写入每个库的 soname、hash 名、来源路径与原始 sha256——auditwheel SBOM 的轻量对应物，供应链记录随 wheel 分发。
- **CI 集成**：`toolchain-images.yml` 新增 `wheels` job（从本 commit 的 GHCR 工具链镜像取 prefix、构建 packs 与三样例 wheel，CI 缩减为最新 CPython 单版本、`--verify-manylinux` 双架构经 binfmt qemu）与 `wheels-arm-check` job（GH 原生 arm64 runner 上对每个 aarch64 wheel 在官方容器内 pip install + import 终检，落实 T8「交叉构建、原生终检」）。
### 9.5 后续增量（2026-08-27）

**PyO3 / maturin 端到端打通**（T5 的第四类后端补齐）：

- 构建环境分层：`docker/buildenv-rust.Dockerfile` 在基础镜像上叠加 rustup（minimal profile + x86_64/aarch64 两个 target 的 std，约 600MB）。刻意不并入基础镜像——GCC 构建与 python packs 都不需要 Rust，八个工具链 job 不该为此付下载代价。`RUSTUP_HOME` 烧进镜像，**`CARGO_HOME` 故意不设**：镜像内目录对非特权构建用户只读，cargo 需落到 `$HOME/.cargo`（wheel 编排提供的可写 scratch HOME）。
- 交叉驱动全靠环境变量，无需项目改造：`CARGO_BUILD_TARGET=<triple>`（我们的 triple 与 Rust target 三元组恰好同名）+ `CARGO_TARGET_<TRIPLE>_LINKER=<triple>-gcc`，PyO3 侧 `PYO3_CROSS_LIB_DIR`（指向目标 pack 的 lib，pyo3 从中递归找 sysconfigdata）+ `PYO3_CROSS_PYTHON_VERSION`。
- **原生 x86 同样必须用本工具链的 linker**：cargo 默认用宿主 `cc` 链接，产物会带宿主 glibc 需求、直接超 policy 上限——这与 §9.3 中 C++ 的结论同构，Rust 侧同样成立。
- cc-rs 兼容：设 `TARGET_CC/TARGET_CXX/TARGET_AR` 供目标侧对象使用，同时显式设 `HOST_CC/HOST_CXX=gcc/g++`——否则 build script（宿主侧）会误用我们注入的交叉 `CC`。
- 实测（`examples/wheel-pyo3`，pyo3 0.29 + maturin 1.15）：x86_64 与 aarch64 均一次通过；交叉产物在 qemu 下真实调用成功（`add(2,40)=42`，运行时解释器 3.12.14），DT_NEEDED 仅 4 个白名单库、GLIBC ≤ 2.28。maturin 1.15 自带 CycloneDX SBOM 一并进入 wheel。

**python packs 分发**（免去用户本地构建 CPython）：

- **GHCR 镜像，每 (版本 × 架构) 一个**：`ghcr.io/eglinuxer/crossforge/python:<tag>-<arch>`（另带 `-<sha>` 精确钉版）。pack 按其 configure 前缀 `/opt/_internal/cpython-<v>` 安装进镜像，因此镜像**本身就是可直接运行的 CPython**（`docker run ... python3.12 -c ...`，arm64 经 binfmt 亦可）。Dockerfile 刻意只有 COPY 无 RUN——arm64 变体可在 x86 宿主上零仿真构建。
- **`crossforge python --pull`**：从镜像把 `/opt/_internal` 拷回本地 pack 树（`--registry` / `--image-ref` 可指定源与钉版），并对拉取结果照跑 import 冒烟。拉取失败但本地已有该镜像时降级使用（离线/`docker load` 场景）。拉到的 pack 与自建 pack 完全等价——实测直接用于 wheel 构建通过。
- **`--pack --out`**：产出 `crossforge-python-<id>.tar.zst` + `.python.toml` 侧文件（sha256/大小/版本/基线），`manifest.toml` 增加 `[[python]]` 数组；CI 在 tag 上作为 release 资产上传。单包约 14MB。
- **注意**：pack 面向 el8 基线，其扩展模块依赖基线时代的运行库（libffi.so.6 等），在现代宿主直跑会 ImportError；冒烟因此需要 `--image` 基线容器。曾尝试用 `LD_LIBRARY_PATH` 指向 sysroot 绕过——**会把基线 libc 配上宿主 ld.so 直接段错误**，已放弃并在错误信息中给出正确指引。
- CI 重排为 `build → python-packs（5 版本矩阵，推镜像 / tag 时传 release）→ wheels（**改为 --pull**，顺带验证发布路径）→ wheels-arm-check`；wheels job 增补 PyO3 样例（用 Rust 镜像）。

## 10. 交叉构建环境：host + target 与 sysroot profile（2026-08-27）

需求来源：issue #1 的 "Native companion" 一节，其真实场景是 **Qt 6 的完整交叉编译**。Qt 交叉是三段式——先**原生**构建 host 工具（moc / rcc / uic / qmltyperegistrar / qmlcachegen / syncqt / qt-cmake），再用 `-DQT_HOST_PATH=<host qt>` 交叉构建 target Qt，下游项目再重复同一形状。任何带构建期代码生成器的项目（protoc / flatc / bindgen）都是同一结构。

结论：**真正的交付单元不是一条工具链，而是一套交叉构建环境 = 原生 host 工具链 + 目标交叉工具链 + 足够深的目标 sysroot，三者同一闭包身份。**

### 10.1 原生编译器入口

两个 prefix 的可执行文件全部带 triple 前缀，因此共存零冲突（实测交集为空）；但也因此**都不提供裸 `gcc`/`cc`**——host 工具的原生构建会静默落到发行版自带的 GCC 8.5（既非资格化编译器，也不满足 Qt 6 的 C++17 要求）。故：target 与宿主架构一致的工具链（即 x86_64）安装无前缀驱动别名（`gcc/g++/cc/c++/ar/ld/...`，27 个），语义上完全正确，交叉工具链绝不安装。实测裸 `gcc --version` = 14.2.1、原生 `std::filesystem` 编译运行通过。

### 10.2 sysroot profile 与依赖闭包解析

现状问题：`sources.toml` 是**手写平铺包列表且无依赖解析**，目标 sysroot 只有 11 个 pkgconfig 条目——Qt 目标侧需要几十个包及其传递闭包，手写不可行。

实现（自研 solver，保持纯 Rust 与完整供应链记录）：

- `primary.xml` 解析扩展出 `provides` / `requires` / `file`；**跳过** `pre="1"` 的安装脚本依赖、rich/boolean 依赖与 `rpmlib(...)`——sysroot 是链接期树，不是可引导根文件系统；
- `resolve_closure()`：provider 索引（含 `pkgconfig(x11)` 这类 provides 与文件 provides）+ 广度优先传递闭包；provider 选择规则为**包名精确匹配 > 最短包名（dnf 同款 tie-break）> 最新 EVR**；版本约束不求解——单一仓库快照内部自洽；
- 缺失的 seed **一次性全量报告**（而非逐个报错），未满足的能力单独记录为非致命；
- 排除表支持尾部 `*` 前缀通配，默认剔除 bootstrap/运行时链（shell、coreutils、locale、daemon、解释器）。

**分档 profile**（`minimal` / `gui` / `x11` / `wayland` / `qt6`，`include` 组合）：`minimal` 保持策展精确列表不做解析（其内容已被验证，不得漂移）；其余为 seed 集 + 闭包解析。profile 可声明**自己的附加仓库**——这是为了把 EPEL 的引入限制在真正需要它的档位：RHEL 8 确实缺 `xcb-util-cursor`（Qt 6.5+ xcb QPA 插件的硬依赖）与 `minizip`（qtwebengine），而更窄的档位继续保持纯 Rocky/RHEL 供应链。

实测（el8 × aarch64 × qt6）：**315 个包解析、零缺失 seed、452MB、192 个 pkgconfig 条目**，X11 / wayland / EGL / freetype / xkbcommon / xcb-cursor / vulkan / gstreamer 头文件齐备；19 项未满足能力全部是被刻意排除的 bootstrap 链。

**profile 进入身份**：`ToolchainSpec.sysroot_profile` 参与 `id()`（`gcc14.2.1-el8-qt6-aarch64`），非默认档才追加，既保证「同 gcc/基线/target 但 sysroot 不同 = 不同构建环境」可区分，又不改动既有 minimal 工具链的 id 与已发布 tag。

### 10.3 环境导出

- `toolchain.cmake` 补 `CMAKE_SYSROOT` 与 pkg-config 环境（`PKG_CONFIG_SYSROOT_DIR` / `PKG_CONFIG_LIBDIR`，裸 `PKG_CONFIG_PATH` 会泄漏宿主库）；
- 新增 `crossenv.sh`：非 CMake 构建系统（autotools / meson / 裸 make）的同款环境，脚本自定位以保持可重定位，并在每次 source 时重新生成 meson cross file（meson 无法表达相对路径）。

### 10.4 crossenv bundle 镜像

`docker/crossenv.Dockerfile` 把 **target 交叉工具链 + x86_64 原生 companion** 组合进一个镜像，两个来源镜像**按 digest 钉住**（多阶段 `COPY --from`，两份 `/opt/crossforge/<id>` 天然合并不冲突）。PATH 顺序为 host 优先——裸 `gcc` 必须解析到原生 companion。这不违反 issue #1 的「不做胖矩阵镜像」：它不是全矩阵，而是**真实交叉构建的最小可用单元**，且下游只需钉一个 digest。单工具链镜像继续保留（身份与自由组合用途）。

CI 每次提交为 el8 × aarch64 组合 bundle 并做双向验证（裸 `gcc` 必须是 14.2.1 且为 x86_64、原生 C++17 编译执行、交叉产物 `file` 校验为 aarch64、cmake 可用）。

### 10.6 sysroot 精简与编译器跨 profile 复用（2026-08-27）

**只抽取链接需要的内容**：sysroot 是链接期树，文档、翻译、目标架构可执行文件都是死重，且**逐层放大**（prefix 内嵌 sysroot → 镜像内嵌 prefix → crossenv 内嵌两个 prefix）。在抽取时过滤（而非事后删除）：qt6 aarch64 sysroot **452MB → 271MB**、其 prefix **~770MB → 545MB**，同时 192 个 pkg-config、3168 个 CMake 包配置、28 份 wayland 协议 XML 完整保留，X11+wayland+xkbcommon+EGL+freetype 交叉链接照常成功。

排除表刻意是**黑名单而非白名单**——`usr/share` 下既有可弃的（doc/man/locale/icons）也有必需的（CMake 配置、pkg-config、aclocal 宏、wayland 协议、GIR），白名单会静默切断依赖发现。

过程中暴露两个缺陷：

- **TOML 顺序陷阱**：`exclude` 键写在了 `[source.arch_packages]` **之后**，于是被解析成那张表的一项（一个名叫 `exclude` 的"架构"）——解析器此前**根本没有排除任何东西**，filesystem/tzdata 等 bootstrap 链一直被静默拉进 sysroot。因为架构表接受任意键，`deny_unknown_fields` 与类型系统都看不见，故 `merge_toml` 现在拒绝非架构名的键。
- filesystem 恢复排除后，它的 UsrMove 符号链接（`lib64` → `usr/lib64`）不再最先到达，后续包以真实目录形式提供同一路径导致抽取 EEXIST 失败。现在符号链接成员落在已抽取目录上时跳过（布局修复阶段随后归一化），且 cpio 错误携带出错路径。

**编译器跨 profile 复用**：构建 qt6 profile 曾花约 3 分钟重新产出一个已经存在的编译器。不变式是——**工具链本体与目标运行库只依赖 (gcc, binutils, baseline, target)**，更深的 sysroot 增加的是*用户代码*的头文件与库，libstdc++/libgcc 只需要 glibc，而 glibc 由基线固定。

依赖此假设前先做了实证对比（minimal vs qt6 两个 prefix）：`c++config.h` 完全一致、libstdc++ 导出符号集完全一致（6274 个）、预定义宏完全一致、configure 参数仅 prefix/sysroot 路径不同。二进制逐字节不同，但只因各自在不同构建目录完成——GCC 会烧入构建路径，那是另一个议题（可复现性），不构成编译器不同的证据。

因此非默认 profile 改为**克隆基础工具链并替换 sysroot**。GCC 按自身位置解析 `--with-sysroot`（可重定位 prefix 本就依赖这一性质），故克隆体直接指向新内容而无需重建：**0.8 秒**取代整轮构建，并经 `-print-sysroot` 指向自身、X11/wayland/EGL 链接、`std::format`+`std::filesystem` 经 nonshared 通过 audit 三项验证。

一个需知的后果：GCC 构建树属于基础工具链，故 `crossforge check` 跑的是那一份——这是正确的（本就是同一个编译器），但意味着克隆出的 profile 没有自己的构建树。

### 10.7 生产级补全：sanitizer / 加固 / cross-gdb（2026-08-27）

**Sanitizer**：`--disable-libsanitizer` 自首次编译器构建起就在，`-fsanitize=address` 直接 `cannot find -lasan`。实验证明代价很小——strip 后 prefix **+8MB**、GCC 构建 32 核下 **2m38s → 3m47s**——五套（ASan/HWASan/LSan/TSan/UBSan）全部构建成功且实测有效（ASan 抓到堆溢出、UBSan 抓到移位溢出）。基线相关的用法要点：el8 自带 `libasan.so.5`、GCC 14 产出 `libasan.so.8`，故**动态形式合理地被 audit 拒绝**，`-fsanitize=address -static-libasan` 既能运行又通过审计。

**加固分两类处理**：

- **真缺陷进 audit（错误级）**：可执行栈（`PT_GNU_STACK` 带 X 位——加固内核与 SELinux 直接拒绝加载；GCC 对使用嵌套函数的代码就会产出，链接器当场警告而此前无人下游检查）与文本重定位（`DT_TEXTREL`）。用嵌套函数产物实证（`GNU_STACK` 变为 RWE），且对包括 wheel 样例在内的正常产物零误报。
- **策略做成可选 specs**：`<prefix>/share/crossforge/hardened.specs`，用 `-specs=` 开启，含 `-fstack-protector-strong`、`-D_FORTIFY_SOURCE=2`、完整 RELRO。**不默认注入**——静默改变编译器产出正是构建系统积累谜团的方式；RH 系统编译器烧入这些是发行版政策而非编译器职责。文件里每个条件都对着本编译器验证过：C/C++ 优化时注入 FORTIFY、`-O0` 跳过（否则 glibc 警告）、用户已指定级别时让位。**PIE 刻意不含**——它是唯一会因非 PIC 静态库而破坏他人构建的加固项，需显式 `-fPIE -pie`。

**cross-gdb**：每条工具链现在附带 `<triple>-gdb`（17.2，strip 后 10MB，约占构建 90 秒）。关键在于它链什么：按常规方式对着容器内的包构建，产出的 gdb 需要 `libmpfr.so.4`（el8 专有 soname）与 `libexpat.so.1`（最小镜像没有）——**在 rockylinux:8 能跑，在 ubuntu:20.04 与 debian:11 直接失败**，而旁边的编译器三个都能跑。那等于悄悄让渡掉整个项目赖以存在的性质。故先把 expat 与 MPFR 构建为静态宿主库（每个 work dir 一次）再链入；余下的 lzma/gmp/libstdc++ 到处都有。三个发行版实测可运行，并能在 x86_64 宿主上反汇编 aarch64 产物。

顺带一个踩坑：`--without-mpfr` 看似更省事（两个目标都是 IEEE 浮点，MPFR 只在模拟异构浮点格式时才有用），但它在 gdb 17.2 里是坏的——configure 把路径留成字面量 `no`，libtool 随后在 `cd no/lib` 处失败。

### 10.8 供应链：能钉的钉住，钉不住的记录（2026-08-27）

三条线索指向同一个问题——构建的输入全是移动靶，而它的输出把构建机带进了别人的产物。

**sysroot lockfile**：解析取每个包的最新构建，同一 crossforge revision 下个月产出的 sysroot 就不同。`--lock` 写出解析结果（NEVRA + 内容哈希 + URL），`--locked` 回放且**完全不读仓库元数据**。逐文件验证：11,125 个条目、集合完全一致、内容抽样一致，且耗时 **5 秒**——相比每个仓库解析数 MB 的 primary.xml 是数量级差异。`sysroot-locks/` 收录当前 el8 的 minimal 与 qt6 两份答案。

**不可变引用**：父镜像按 digest 钉（用的是 index digest，故 `--platform` 仍能解析双架构——已用 arm64 pack 镜像实测）；actions 钉到 commit SHA 并把 tag 留作注释；`rust-toolchain.toml` 使 "stable" 不会在两次运行之间悄悄换掉编译器。

**构建路径**：nonshared 归档会被**静态链进用户二进制**，所以烧在里面的路径会一路旅行到别人的产物里——`/tmp/crossforge/build/src/...` 原样出现在编译好的程序中。现以 `-ffile-prefix-map` 把构建树映射到 `/usr/src/crossforge`（发行版放调试源码的惯用形状）。用户二进制里剩下的是他们自己工具链安装位置的路径，那本就是他们的。

**一个买不到的东西：可复现的编译器。** 同一 spec 在**同一路径**下构建两次，驱动 `gcc` 逐字节一致，而 `cc1plus` 不一致——说明非确定性并不只是路径，固定构建目录也解决不了。因此身份必须来自**记录输入 + 对产物树取哈希**，而不是"重建并比对"。这一条直接决定了 issue #1 的资格化层该怎么设计（见 §10.5）。

### 10.5 对 issue #1 的影响

「Native companion」一节需升级：不只是「manifest 暴露足够身份让下游判断可组合」，而是 crossforge 直接交付**已组合**的环境。另有一条原文未覆盖：**sysroot profile 必须进入 manifest 身份**——同一 (gcc, baseline, target) 而 profile 不同即为不同构建环境，下游必须能区分并 fail closed，否则 Qt 构建会拖到链接期才暴露缺包。

## 11. 参考

- RH gcc-toolset 机制：CentOS Stream dist-git `gcc-toolset-*-gcc` spec；LWN 862013
- 符号版本机制与断点：glibc 2.34 libpthread 合并（LWN 864920）、DT_RELR lockout（sourceware libc-alpha 2022-03）
- sysroot 实践：Chromium `docs/linux/sysroot.md`、`reversion_glibc.py`；conda-forge linux-sysroot-feedstock
- abilist/stub：ziglang/libc-abi-tools；cerisier/toolchains_llvm_bootstrapped
- 审计：pypa/auditwheel；corsix/polyfill-glibc
- 可重定位：Yocto `relocate_sdk.py`；crosstool-NG relocation 文档
