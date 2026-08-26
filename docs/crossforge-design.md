# crossforge 交叉编译工具链构建引擎 · 设计文档

- 版本：v0.6（2026-08-26）
- 状态：评审中
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
| D2 | 编译器主体 | **GCC**，默认源码基线 **RH gcc-toolset-14**（GCC 14.2.1，2025-01 快照 + RH 补丁集，Rocky 8 镜像 SRPM；2026-08-26 修订，原为 FSF 11.5.0）。配 binutils 2.40；FSF tarball 组件已随 2026-08-26 重构移除（fallback 由对象级裁剪承担）；Clang 可作为后续副轨接入同一套 sysroot / compat-pack |
| D3 | 默认基线 | **el8**（glibc 2.28 + GLIBCXX 3.4.25，CXX11 ABI）。**el7**（glibc 2.17 + GLIBCXX 3.4.19）作为可选长尾基线，明示其强制 `_GLIBCXX_USE_CXX11_ABI=0` 的代价 |
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
store 层的去重优化；概念模型不变。此外 el7 这类强制旧 ABI 的基线会把
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
| `el8`（默认） | 2.28 | 4.18 | GLIBCXX_3.4.25（GCC 8.5） | 1.3.11 | `_GLIBCXX_USE_CXX11_ABI=1` | AlmaLinux 8（x86_64 / aarch64） |
| `el7` | 2.17 | 3.10 | GLIBCXX_3.4.19（GCC 4.8.5） | 1.3.7 | 强制 `=0` | CentOS 7 vault（x86_64）、AltArch（aarch64） |

预留：`u20`（glibc 2.31）、`el9`（2.34）、欧拉/龙蜥（LoongArch 基线）等，注册表数据化（TOML），新增基线不改代码。

关于 el7 的 Dual ABI 说明：基线库完全没有 `__cxx11` 符号，若允许 CXX11 ABI 则全部字符串相关符号进入 nonshared 静态段，体积暴涨且与用户系统 GCC 编译的代码无法互传 `std::string`——与 RH devtoolset 的取舍一致，强制旧 ABI 并在文档与构建日志（WARN）中显著警示。

## 4. 关键机制

### 4.1 glibc 侧：二进制 sysroot，不从源码构建旧 glibc

sysroot 由旧发行版**二进制包**（RPM）抽取拼装：`glibc`、`glibc-devel`、`glibc-headers`、`kernel-headers`、`libgcc`、`libstdc++`（运行库，供链接期符号表用）。

不走 crosstool-NG 的「新 GCC 从源码编旧 glibc」路径——那是其主要痛点来源（`-fcommon`/`--disable-werror` 补丁兜底、组合无 CI 验证）。这带来一个重要简化：**sysroot 里已有完整 glibc，编译器构建无需三阶段 bootstrap**——直接 binutils → GCC（`--with-sysroot` + `--with-build-sysroot`）一遍完成，libgcc / libstdc++ 对着现成 glibc 配置。

### 4.2 libstdc++ 侧：nonshared 混合链接（devtoolset 机制的交叉化）

nonshared 归档有**两个来源**（v0.5 起，`NonsharedSource` 枚举）：

- **RedHat（默认基线优先）**：默认源码基线是 RH gcc-toolset-14 的 SRPM（GCC 14.2.1 + 42 个补丁），其中 `gcc14-libstdc++-compat.patch`（万行级）在 libstdc++ 源码树内新增 `src/nonshared{98,11,17,20}/` 目录——人工精修的显式实例化文件、按架构条件的 `asm(".hidden <sym>")` 可见性清单、RTTI 汇编 stub——构建期直接产出 `libstdc++_nonshared{44,48,80,110}.a` 四级基线归档（RHEL 6/7/8/9）。基线注册表以 `rh_nonshared` 字段（el8→`80`、el7→`48`）声明采用哪级。**关键构建约束**：target 库必须带 `-D_GLIBCXX_ASSERTIONS` 编译（RH optflags 隐式约定）——它禁用 libstdc++ 头文件的 extern-template 声明，RH 的 `.hidden` 清单依赖 nonshared 对象据此自行发射 weak hidden 实例化；缺了它链接期会出现无定义的 hidden 符号（已实测踩坑并修复，CXXFLAGS_FOR_TARGET 注入）。
- **Pruned（fallback）**：对无 RH 补丁的源码组合（如 FSF tarball），用完整 `libstdc++.a` 按基线 abilist 做对象级自动裁剪——剔除「只含基线已有符号」的对象。精度低于 RH 方案（保留对象内混有基线符号、多 DSO 副本面更大），但任意 (GCC × 基线) 组合零人工。
2. **linker script `libstdc++.so`**：

   ```
   INPUT ( =/usr/lib64/libstdc++.so.6 -lstdc++_nonshared )
   ```

   `=` 前缀由 ld 解析为 sysroot 内路径，保持整体可重定位。安装到工具链内部搜索路径（`lib/gcc/<triple>/<version>/`），天然优先于 sysroot 中的真实 `libstdc++.so`。
3. 链接行为：基线库已有的符号 → 动态绑定（带旧版本号）；新增符号（`std::from_chars` 浮点版本、GCC 9 起并入主库的 `std::filesystem` 符号、C++20 库设施等）→ 从 nonshared 静态链入。运行时产物仅 `DT_NEEDED` 系统 `libstdc++.so.6` 且只引用其确有的符号。

基线 abilist 的权威来源是**目标发行版 `libstdc++.so.6` 的动态符号表**（随 sysroot 抽取时一并生成），GCC 源码树 `libstdc++-v3/config/abi/post/<triple>/baseline_symbols.txt` 作交叉验证。

ABI 不稳定的次要运行库（libgfortran 等）按 devtoolset 同款策略纯静态处理（v1 不含 Fortran，预留）。

### 4.3 默认注入的编译/链接选项

wrapper（或生成的 toolchain file）按基线注入：

| 选项 | 原因 |
|------|------|
| `--sysroot=<sysroot>` | 基线头文件与链接库（实现：GCC `--with-sysroot` 烧入，无需 wrapper 注入） |
| `-Wl,-z,nopack-relative-relocs` | 阻断 DT_RELR → `GLIBC_ABI_DT_RELR` 版本依赖（binutils ≥2.38 环境下旧机器的隐形地雷；工具链自带 binutils 2.40 默认不开 DT_RELR，audit 兜底检查） |
| 旧 string ABI（仅 el7） | 实现为 GCC configure `--with-default-libstdcxx-abi=gcc4-compatible`（编译器默认 `_GLIBCXX_USE_CXX11_ABI=0`，比 wrapper 注入宏更不可绕过），构建时输出 WARN 警示 |

注意不强制 `-std=`：默认 toolchain（GCC 14，默认 `gnu17`）不受 C23 符号重定向影响；后续 GCC 15+ toolchain 默认 `gnu23`，会使 `strtol` 等重定向到 `__isoc23_*@GLIBC_2.38`——el7/el8 sysroot 的旧头文件天然不含该重定向，此风险仅存在于误用宿主头文件时，由 audit 兜底检出。

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

let registry = BaselineRegistry::builtin();      // 内嵌 el7/el8，可 merge_toml() 扩展
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
source = "almalinux-8.10"
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

`crossforge verify` 在基线及更新的发行版容器矩阵（centos7 / rockylinux8 / ubuntu20.04 / debian11 …）中做真机 exec + dlopen 冒烟，防审计规则遗漏。

`crossforge check` 跑 GCC 官方 DejaGnu 测试集（check-gcc / check-c++ / check-target-libstdc++-v3）：自动生成 board 文件（x86_64 直接执行——产物基线低于构建容器；aarch64 走 qemu-user + sysroot），解析 `.sum` 产出统计与 FAIL 明细，`--max-unexpected-failures` 可作门禁。2026-08-26 首轮完整成绩（gcc14.2.1-el8-x86_64）：**gcc 191,477 / c++ 256,602 / libstdc++ 17,642 passes，合计 46.5 万；32 个 unexpected failures 全部定性为容器 locale/网络噪声或 RH dts-test 补丁已标注的基线语义差异（如 string::reserve 收缩走基线旧语义——正是 nonshared 的设计行为），零工具链归因缺陷**。踩坑记录：DejaGnu 在 `/etc/passwd` 无当前 UID 的容器里 `exec whoami` 崩溃（注入 USER 环境变量解决）；`set_board_info` 不覆盖既有值，需先 `unset_board_info isremote`；RH dts.exp 的版本探测不兼容单段 `-dumpversion` 输出（幂等改写该 proc）。

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
| M5 | el7 基线（含强制旧 ABI 与警示）、pack/manifest 稳定化、feature "cli" 薄封装、GitHub 开源 + GHCR 镜像流水线 | 双基线双 target 全矩阵绿 | ✅ 2026-08-26（el7 产物 centos:7→debian:11 全 PASS；CI/toolchain-images workflows 就位） |

## 8. 风险与开放问题

1. **nonshared 的持续维护**：默认路线已切换为直接消费 RH gcc-toolset SRPM（人工精修由 RH 承担，跟随其大版本节奏即可）；自研裁剪仅作 fallback。遗留任务：把 RH spec 的 nonshared 验证法（whole-archive 试链 + `readelf` 查 hidden UND + abilist 并集校验）自动化进 compat 模块，作为两种来源共同的构建期门禁——本次 `_GLIBCXX_ASSERTIONS` 踩坑本可由它在构建期拦截。
2. **多 DSO 各持 nonshared 副本的边角**：新特性类型的 typeinfo/vtable 多副本，异常跨 DSO 依赖 libstdc++ 的 strcmp fallback——SDK 发布物内部统一由 crossforge 工具链一次性链接可规避；audit 对同进程多副本场景仅能提示。
3. **旧 glibc 头文件 × 新 GCC 的小摩擦**：2.17 headers 在 `gnu23` 下的个别不兼容（如 `bool` 宏冲突）可能需要头文件级微补丁，随 sysroot 生成器维护。
4. **host 兼容门槛**：v1 要求宿主 glibc ≥ 2.28（el8 构建）；如需覆盖更旧 CI 宿主，启用静态 host 工具选项。
5. **LoongArch 基线**：欧拉/龙蜥的包源与 abilist 差异待调研，仅保留架构接口。

## 9. 参考

- RH gcc-toolset 机制：CentOS Stream dist-git `gcc-toolset-*-gcc` spec；LWN 862013
- 符号版本机制与断点：glibc 2.34 libpthread 合并（LWN 864920）、DT_RELR lockout（sourceware libc-alpha 2022-03）
- sysroot 实践：Chromium `docs/linux/sysroot.md`、`reversion_glibc.py`；conda-forge linux-sysroot-feedstock
- abilist/stub：ziglang/libc-abi-tools；cerisier/toolchains_llvm_bootstrapped
- 审计：pypa/auditwheel；corsix/polyfill-glibc
- 可重定位：Yocto `relocate_sdk.py`；crosstool-NG relocation 文档
