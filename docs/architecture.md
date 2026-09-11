# Crossforge 重写架构

> 状态：已接受的实施基线（2026-08-28）
> 本文是当前实现的架构契约。旧 Rust 原型及其设计记录只保留在 tag `prototype-rust-2026-08-28`。
>
> 实施进度：canonical DNF resolver、双架构 sysroot、三层 host build locks、独立 host runtime、两套 GTS15 C/C++/LTO cross slice、冻结 EL8 ABI 集，以及 CPython 3.9–3.14 的 build/x86_64/aarch64 行与完整 ELF ownership gate 已完成；3.14 包含私有静态 zstd 1.5.7。最终 SDK 已重基于独立 host runtime 并通过离线集成资格化；CMake 4.4.0/Ninja 1.13.2 host-tool overlay、vcpkg 供应链、五套 triplet/chainload toolchain、真实无下载 overlay-port 契约、三层 curated-port 门禁、生产分包门禁、x86_64 四套完整 GCC qualification、原生 ARM release 工作流，以及 Qt 6.8.4 source acceptance、host 配置/构建、双 target 配置/完整构建、clean-Rocky 与显式 QEMU 运行时资格均已完成。原生 ARM 首次候选实证（含 candidate-bound Qt 运行时）及其余发布供应链尚未完成，当前稳定产物仍未发布。

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
├── host-tools/ninja/1.13.2/
├── host-tools/cmake/4.4.0/
├── cmake/
├── meson/
├── vcpkg/triplets/
├── env/
└── release.json
```

镜像还包含原生 GTS15 C/C++ 编译器、Meson、固定 CMake 4.4.0/Ninja 1.13.2 overlay、Make、Autoconf、Automake、Libtool、pkg-config、Git、bison、flex、常用归档/文本工具、QEMU、固定版本的 vcpkg 和 nFPM。RPM 所有的旧 CMake/Ninja 保留但不处于 PATH 首位。RPM 构建工具、DejaGNU、Qt 源码、gperf 及 WebEngine 专用工具只存在于内部构建/测试 stage。Rust、Conan、auditwheel、cibuildwheel 不进入产品镜像。

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
约 730 个无关包。Crossforge 已用 host-build-common transaction 锁定 `%prep` 实际使用的 RPM 工具与命令，
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
GROUP ( =/lib64/libgcc_s.so.1 libgcc.a libgcc_eh.a )
```

系统 unwinder 保持共享，以保证跨 DSO exception 正确。

## 5. 配置、锁与可追溯性

`config/release.json` 是唯一人工维护的版本事实来源，并由 `config/schemas/release.schema.json` 严格校验。版本、NEVRA、URL、SHA256、target、Python adapter、Ninja/vcpkg commit、nFPM 版本、基础镜像 digest，以及资格化实际消费的 8 份 ABI baseline/provider JSON 逻辑路径与 canonical digest 都在其中固定。

规划阶段可以用 `status: "pending"` 明示尚未核实的来源，禁止填入猜测值；任何 candidate/release 构建都必须使用 `validate-release.py --require-locked`，存在一个 pending pin 即失败。

规范配置、RPM plans/transactions/locks、测试 manifest 和 `crosspack.json` 统一使用严格 JSON + JSON Schema。loader 必须拒绝重复 key、未知字段和未知 `schema_version`。配置身份由 canonical JSON 的 SHA256 计算，不受空白或格式化影响。RPM 供应链固定为三层证据：人工 plan、规范化 install/upgrade/remove 动作并保留精确 reason 的 DNF transaction、实收 payload content lock；不得从下载目录反推或伪造 solver reason。Bake HCL、CMakePresets、vcpkg manifest、GitHub Actions YAML 等继续使用各自工具的原生格式；生成文件禁止手工修改，并由 CI 检查漂移。

## 6. Sysroot 与运行时兼容性

产品只包含两份不可变 Rocky 8.10 sysroot。每个 RPM lock 记录完整 NEVRA、仓库、下载地址和 SHA256。DNF 只用于维护时求解和生成 lock；正式构建只消费 lock，不读取实时仓库 metadata，也不实现自己的 dependency solver。

Crossforge 不定义 staging/overlay 目录、产品级 sysroot profile 或第三方依赖布局。下游可以通过 GCC 标准 `--sysroot`、CMake 或 pkg-config 覆盖默认值，但自定义 sysroot 的内容和兼容性由下游负责。Qt 的扩展依赖只属于测试夹具，不构成产品 API。

默认 ABI/ISA 契约为：

- glibc 2.28 floor；EL8 `libstdc++.so.6` 与 `libgcc_s.so.1` 动态运行时；
- x86_64 使用 `-march=x86-64 -mtune=generic`，aarch64 使用 `-march=armv8-a -mtune=generic`；
- 禁止 host 头文件/库泄漏、超出冻结集合的 GLIBC/GLIBCXX/CXXABI/GCC 符号以及 `libcrypt.so.1` 专属的 XCRYPT 符号、DT_RELR、text relocation、可执行栈和构建目录绝对 RUNPATH；
- “glibc >= 2.28”只是 ABI floor，不代表无条件支持所有此类发行版。

`locks/sysroot-*.json` 可因 Rocky errata 更新；`abi/el8/{x86_64,aarch64}.json` 则冻结最低允许的符号集合。sysroot 更新不得静默扩大 ABI，只有显式升级产品 baseline 才能修改冻结集合。

Python 的非 core 动态依赖不进入上述通用 ABI baseline。`config/python-runtime-providers.json` 固定 8 个 SONAME、7 个 RPM owner、NEVRA、实收 RPM 摘要与 DSO 摘要；`evidence/abi/el8-*-python-provider-catalog.json` 冻结 core+Python provider 的完整 ELF record，并由 policy digest、compile report、runtime tier、row manifest 与 cumulative SDK 共同绑定。locked tier 对所有 provider 做逐字节核验；clean Rocky 只允许 core DSO 的 errata 字节差异，且完整 catalog 必须不变，8 个 Python provider 在两层始终逐字节相同。

资格化重新读取 Python 主程序、最小扩展及全部 `lib-dynload` ELF 的实际字节，绑定 ELF class/endianness、主程序与 shared-object 角色、PIE/RELRO/NOW/RPATH/loader tags、`DT_NEEDED` closure、versioned import、COPY relocation，以及 strong/weak unversioned symbol 的唯一所有权。主程序的全局导出 record 必须与其实际 ELF record 完全相同；未知 provider、私有 core 版本、无 owner 或多 owner 的 strong symbol 均失败。运行时还要求实际加载的 SONAME resolve 到受审 provider 路径；动态 `libzstd` 始终禁止。

两份 target transaction 由固定 Rocky digest 内的 `python3-dnf` 从空 installroot
通过上游 [`Base.resolve()`/`download_packages()` API](https://dnf.readthedocs.io/en/latest/api_base.html)
分别解析 14 个显式 roots，各精确得到 78 个 RPM。Resolver 禁用 plugins/system repo，
以 `arch/basearch/ignorearch` 显式实现 foreign-arch 求解，固定模块策略和 solver flags，并记录 DNF action/reason、base/remove/result
inventories。BaseOS `repomd.xml.asc`、完整 metadata checksum 链、逐包 NEVRA、URL、
仓库 checksum、实收 SHA256、source RPM 与 Rocky 签名均被锁定。正式 assembly
不访问仓库；它预置经签名 `filesystem` manifest 验证的 usrmerge 链接，再以无
scripts/triggers 的 RPM transaction 安装，最终 rpmdb 必须逐项等于 result manifest。

aarch64 日常运行门禁不依赖宿主 binfmt。测试执行器固定为 QEMU 10.2.3 的
amd64 static PIE，绑定 tonistiigi/binfmt 的 index/amd64 manifest、二进制 SHA256
及 source commit；执行时固定 `cortex-a53` 与 EL8 `4.18.0` uname override。Rocky
arm64 根文件系统只作为 source stage 被复制，所有 arm ELF 都由 amd64 stage
显式调用 QEMU 执行。该结果只能标记为 QEMU-qualified，不能替代发布前原生
EL8/aarch64 终检。QEMU 不进入任何 cross-build stage；最终 amd64 SDK 只复制已经
固定并验证的静态执行器，供显式运行 aarch64 产物使用。

Host 构建环境使用三个独立 transaction：common 从固定基础镜像解析为 119 install
以及 9 upgrade（并记录对应 9 remove）；GCC additive delta 只含 `bison`、`flex`、
`libzstd-devel` 与依赖 `m4`；Python additive delta 只声明 bzip2、libffi、libuuid、
OpenSSL、SQLite 与 xz 的开发 roots。三层均逐包验签后在 `--network=none` 下执行真实
scripts/triggers，并核对完整 rpmdb。`libzstd-devel` 不进入 common/Python 层，避免改变
binutils 的 `--with-zstd=auto` 探测。最终用户镜像的 host runtime lock 已从干净
Rocky base 独立求解，不继承这些 build-only packages：43 个显式 roots 产生 152 个
验签 payload，以 131 install、21 upgrade 与对应 21 remove 在无网络阶段重放；
PowerTools 只允许提供 Meson 与 Ninja。
Rocky 的 Meson 包强制依赖系统 Python development package；这是受审的上游打包闭包，
不等同于 Crossforge 的 CPython build-devel transaction。最终 SDK 只以该 runtime 为
祖先，再通过 COPY 汇入已资格化的 toolchain、sysroot 与 Python row；GCC/Python build
transaction、源码和 staging 根不会进入产品闭包。

GCC testsuite 的 host 依赖是独立的 test-only additive transaction：
`expect` 来自 BaseOS，`dejagnu` 来自 PowerTools，EL8 GCC 8 及其依赖来自
AppStream/BaseOS。GCC 8 只用于 GTS libstdc++ DTS harness 的宿主版本探测；
PowerTools 在此闭包中只允许 `dejagnu`，整个 transaction 与测试源码均不进入最终 SDK。

## 7. Python SDK

每个 CPython minor 由同一份精确 patch source 构建一份 amd64 build Python 和两份 target Python：

```text
CPython source
├── build: linux/amd64
├── target: x86_64-unknown-linux-gnu + EL8
└── target: aarch64-unknown-linux-gnu + EL8
```

首发支持 3.9–3.14，共 6 个 build Python 和 12 个 target Python。3.9–3.10 使用 legacy adapter，3.11 使用 transition adapter，3.12–3.14 使用 modern adapter；精确 patch 版本与独立的 `eol`/`security`/`bugfix` 支持状态写在 `release.json`。EOL minor 不承诺上游安全修复。

当前已完成 CPython 3.9.25、3.10.21 legacy、3.11.16 transition 以及 3.12.14、3.13.15、3.14.7 modern 六行：每行各有 amd64 build Python、两个真正 cross target SDK、最小 C extension、全量 `lib-dynload` ELF 审计，以及 locked-sysroot/clean-Rocky 双运行时探针。通用 Python Dockerfile 只描述一条 row pipeline；Bake 生成独立版本/target DAG。资格化完成的 row 经 scratch 导出，再由 append-only 层聚合。Phase 5 固定 cp313，Phase 6 固定 cp313+cp311，Phase 7 固定 cp313+cp311+cp312，Phase 8 固定 cp313+cp311+cp312+cp314，Phase 9 固定追加 cp310；Phase 10 与最新 `python-dev`/`python-matrix` 再追加 cp39。

内部交接将 build Python 和两个 target 的安装目录分别 scratch 导出，构建 guard 日志与 source manifest 另作两份审计组件。静态资格阶段从锁定 host 出发，显式消费这些安装/审计输入与相应工具链，不继承 CPython 编译阶段。默认 Bake 的 context 仍指向源码 export；`python_components.py` 先独立重算 producer 输入、核验 receipt 和 OCI 字节，再将对应 target 的 context 替换为固定 digest。同名 context 的身份按 Bake target 限定，禁止跨架构混用。`python_qualification.py` 对整行强制执行现有门禁，并把安装树、报告、真实执行日志和原 producer 封装为 qualification 组件；显式复用通过独立预期输入和可信 receipt SHA256 核验，再调用原 row finalizer 检查实际安装文件。六行正式 receipt、显式组件消费、六行 Python SDK 与完整 SDK 组装均已通过本地 Docker 实跑。SDK 对每行重新核验原资格和实际文件，强制执行既有 append/final 集成，消费图不包含 GCC/CPython 源码编译；原 producer 与行执行区间保持不变。生产 CI 接入、跨 run 信任和新候选原生 ARM 的验收状态见[重构进度](research/ci-refactoring-progress.md)。

3.9–3.12 均以各自文件路径和 SHA256 锁定 gh-115382 backport，显式把 target sysconfigdata 与 build Python 的 `PYTHONPATH` 隔离；3.12 保持 modern adapter，因为其扩展已由 configure/Makefile 构建。3.9–3.10 上游没有 `--with-build-python`、`--with-pkg-config` 或 `HOSTRUNNER`，legacy adapter 必须显式注入精确 `PYTHON_FOR_BUILD`/`PYTHON_FOR_REGEN`，用 `setup.py` 构建扩展，并以 `siphash24` 作为运行时 hash contract。3.9 还没有 `--disable-test-modules`，因此保留上游测试模块，但不据此承诺 EOL 安全支持。其 `setup.py` 使用独立 `distutils.sysconfig`；3.9 补丁必须把该初始化原子委托给 source-only stdlib loader。`sharedmods` 前的动态门禁要求两套 sysconfig 的 `CC`、`AR`、`LDSHARED`、`SOABI`、`EXT_SUFFIX`、`MULTIARCH` 和 `CONFIG_ARGS` 完全一致，且 target build/lib 不得进入 build Python 的 `sys.path`，从而同时阻止 aarch64 显式失败和 x86_64 同 SOABI 静默回退。

cross build 在 configure 前用目标 ELF canary 实测 `execve`/`execv`、PATH 与 varargs exec、`fexecve`、`execveat`、`posix_spawn(p)`、`dlopen` 和 `dlmopen`，构建后拒绝 canary/`conftest` 之外的记录；它是动态 libc/loader 的可审计策略护栏，不是覆盖直接 syscall 或静态程序的安全沙箱。3.9–3.10 要求 `HOSTRUNNER` 不存在，3.11+ 要求其为空；无 QEMU 的 cross stage、精确 build Python patch version 和 sysconfig 隔离仍是主正确性契约。

clean-Rocky tier 从固定 OCI child 出发，只叠加同一 target lock 中七个精确验签 runtime RPM；因 OCI 与 sysroot errata 版本可不同，该 `--nodeps` overlay 仅验证精确 DSO 字节兼容性，不是可部署的 RPM transaction，也不进入 SDK。两套 runtime tier 都把真实 tmpfs 挂到 `/dev/shm`，并实际执行 `multiprocessing.Lock()` 与 libc unnamed semaphore。aarch64 只使用固定 QEMU，发布前仍需原生 ARM 终检。

两个 `python-runtime-clean-<arch>` 构建阶段只读取已认证的 `rpm/sysroot-<arch>` 投影、原 lock/transaction/metadata 和信任根，不再依赖整份 release、release schema 或维护用 RPM plan。原 materializer 仍核对完整锁事务、全部 RPM 字节及签名，然后选择原七个 runtime RPM，保留两次 transaction 检查和前后 rpmdb/os-release 约束。overlay schema 2 用明确的 `input_binding` 记录组件摘要，不声明完整 release 摘要；runtime reader 和目标最终验证器从已认证的行/目标 policy 获取预期组件，旧模式则从完整 release 推导，并检查原镜像、target、sysroot、包摘要及实际运行时证据。旧 schema 1 继续严格绑定原完整 release，不能进入只带组件的资格阶段。

目标报告链使用 compile schema 5、runtime schema 4 和 final schema 5，三者核对同一份行/目标 `input_binding`；两个 runtime tier 在执行前认证根投影与 compile binding，最终报告继续校验源码、ABI、产物、guard、私有 zstd、运行库及嵌套报告的精确序列化摘要。runtime stage 从锁定 host 工具根出发，只显式复制十二个脚本和静态阶段传入的配置，不再复制 release/schema/renderer/source bridge。原 `--release` 模式保留 runtime schema 3、final schema 4 和完整 release 校验，禁止把两种 runtime 格式混入同一最终报告。真实 Bake 输入捕获已证明产品版本变化不再影响十二个目标 runtime 资格输入；这是材料范围检查，实际新报告链执行及正式 CI 复用仍待验收。

行汇总通过独立可信的 `python/<row>-qualification` 根摘要认证两份目标策略，再复用 prepared-source reader 核对共同的 source/build-policy 与精确源码清单，总计六份投影。`cpython-row-assemble` 从锁定 host 工具根出发，显式复制十四个 Python 文件，不继承完整 release host；原安装树、ABI、ELF、build Python、zstd 和嵌套报告检查继续执行。新行清单 schema 3 用行 `input_binding` 替代完整 release SHA256 与全行 qualification pair，要求 source schema 2 和 target final schema 5。原 `--release` 调用仍产生 schema 2；SDK append 和正式 receipt 验收新增 `--row-manifest`，从当前完整 release 独立重算新行策略，检查实际文件并比较整份清单，不能信任清单自报摘要。最终 SDK 同样核对行策略后重新执行 host 集成，自身继续绑定完整 release。材料实验显示产品版本不再使六行资格输入失效，cp39 source 和私有 zstd 各只影响自身行；这不代表取得了实际新资格或跨机器复用证据。

正式行 CI 的生产/签名工作流和目录查找接口已接入 main 动态 matrix；候选 SDK 发布自身仍使用独立路径。被选中的每行均须在原 `sdk-toolchains-dev` 基座完成两个新的独立 append RUN，安装后的 row manifest 与资格产物一致。原始组件和资格产物经原验证器复核，安装失败不得发布新产物或交接签名。查找方重新绑定七个上游组件、当前构建及物理环境，只有目录输入索引缺失才请求新资格；签名、传输或实际行验收失败均报错。新 catalog schema 4 只授权固定 `produce-python-row.yml` 的完整行资格 receipt，原始组件 signer 权限保持原范围。复用保留原 producer；新生产必须通过既有正式资格执行器并在发布前核对输入、receipt、环境与 clean source，重试沿用原 run/attempt 和精确交接摘要。实际 GitHub 签名、跨 run/跨机器环境验收仍未完成。

完整 SDK 另提供 `acquire-python-sdk` 与 `execute-python-sdk-catalog` CLI：前者从签名目录取得两份共享工具链、六行原始组件及六份行资格，只有完整就绪才输出可消费的组件清单；后者随后调用原 SDK executor，独立重验实际文件并重新执行最终集成。目录缺失显式列出待生产组件/行，验签或产物核验失败则报错，不隐式重编译。OCI 数据与上传诊断目录严格分离，旧本地组件清单接口保持。main SDK 已通过下述过渡控制器调用 acquisition，独立目录执行 CLI 和候选发布路径保持原接口，严格物理环境匹配未放宽。

`verify-main-builds.yml` 的 SDK job 使用 `run-component-sdk` 与标准库 `ci_sdk.py`。它要求两份工具链和三十份 Python 原始组件全部已验收，仅在当前输入对应的资格索引缺失时，在 SDK 所在 worker 补做该行双目标完整资格；验签、传输或既有证据失败不能触发回退。父进程最多调度两个独立 Python 子进程，共享原 builder，并在子进程前后及最终集成前后复核源码、调用身份与完整物理环境。子进程请求另绑定独立 canonical SHA256。使用独立解释器避免既有资格模块通过 `runpy.run_path` 修改共享解释器状态时发生并发冲突；依据见 [Python 官方文档](https://docs.python.org/3/library/runpy.html)。六行全部通过后，原 SDK executor 重新检查实际 receipt/文件，并强制执行 append/final 门禁。

SDK job 仍只有 contents/read 和 packages/read 权限；匹配的签名行保留原 producer，新行只留在本 job，不发布或签名。被选中的独立 Python 任务通过行生产/签名工作流交接资格产物，required status 校验完整行 matrix；严格物理环境不匹配时，SDK 仍可能补验同一行。candidate 前置资格调用同一工作流，候选镜像发布本身仍走原始组件绑定路径。SDK 编排与目录/恢复实现变更由增量选择器显式选择 canonical SDK roots，诊断区分编排原因与真实源码输入变化。实际新 Docker 运行、GitHub 事件和性能仍待验收。

在补做资格或最终集成之前，SDK job 保存原 raw schema 的三十二份固定选择及独立 SHA256，诊断上传后的 summary 给出 run/artifact ID、摘要和文件位置。失败时可按原 raw recovery 接口及严格来源约束取回这些原始组件；这份记录不包含新执行的六行资格，不能冒充下面的完整 SDK 恢复记录。新的主 SDK job 尚无自动恢复入口，OCI blobs 与安装树也不进入诊断工件。

每份新行封存成功、receipt 与计划输入及 producer 一致，并保存诊断后，SDK 控制器清理该行的 `payload/` 和 `extracted/` 两份中间安装目录，保留 OCI、receipt 和输入记录供原 SDK executor 独立验收。资格失败、输入不匹配或诊断保存失败不会触发清理；只清理本 job 的重复安装副本，不删除 registry 产物，也不替代按引用保留策略。

本地 OCI 元数据及整行安装树的导出通过 `local_export.py` 限制每次传输为十分钟。超时后终止该 Docker 客户端及其 Buildx 子进程，保留未完成目录，使用相同固定 digest 和仅含 COPY 的配方向新目录重试一次；普通导出错误直接失败。只有成功目录进入原字节、契约、报告及安装树验收，第二次超时仍失败。行 CI 在正常中间目录清理前保存两次导出配方及超时记录。该机制不重跑资格，也不把中断的 producer 标记为成功。

这两个目录消费入口可用 `--record-component-recovery` 在完整取得 32 个原始组件和六行资格后、最终集成开始前写出 `component-recovery.json`；acquisition 结果给出它的 canonical SHA256。恢复时同时提供 `--component-recovery` 与独立保存的 `--component-recovery-sha256`，并使用新的数据/诊断目录。SDK 恢复 schema 1 嵌套原 raw recovery schema 1，额外固定各行的 catalog/artifact digest、输入及 receipt SHA256 和原 producer，不扩大原始组件 schema 的资格角色权限。它要求干净 Git checkout 的同一提交、SDK 根、实际来源材料与完整物理执行环境；仅部分输入就绪时不生成完整恢复记录。

重试通过原目录验签、产物与资格验收函数取得所有固定引用，并逐项核对原选择；缺失、替换、来源或环境漂移均失败。最终 SDK 集成仍重新执行；失败时保留取得阶段的恢复记录，成功前再核对记录摘要、源码与环境。该入口不替代 registry 信任，不复用最终集成报告，也未接通主工作流的自动恢复。

target SDK 包含解释器、stdlib、headers、`pyconfig.h`、`_sysconfigdata_*`、扩展模块和构建元数据。即使 x86_64 build/target 架构相同，也不得复用。每个 target 必须验证 zlib、bz2、lzma、ctypes、ssl、hashlib、sqlite3、uuid 等约定模块，以及最小 C extension 的编译、ELF 架构和 import；3.14 另验 `compression.zstd`。

Rocky 8 的 zstd 1.4.4 低于 CPython 3.14 `compression.zstd` 所需的 1.4.5。Phase 8 因此从签名和 hash 锁定的上游源构建 PIC 私有静态 zstd 1.5.7，分别产生 host、x86_64 和 aarch64 prefix，并只链接进 `_zstd`；全局不可变 sysroot 未改动。编译资格化绑定精确 zstd build manifest/component identity，确认 `_zstd` 唯一、静态符号完整，且无 zstd `DT_NEEDED`、动态导出、RPATH 或 text relocation。locked-sysroot 与 clean-Rocky 运行时 tier 都实际执行 one-shot、streaming、dictionary、multithreaded、tarfile 和 zipfile zstd 探针。

Phase 8 的可执行入口为：

```console
$ docker buildx bake zstd-source zstd-host-build zstd-x86_64-build zstd-aarch64-build
$ docker buildx bake python-native-phase8
$ docker buildx bake cpython-cp314-x86_64-qualify-build cpython-cp314-aarch64-qualify-build
$ docker buildx bake cpython-cp314-x86_64-qualify cpython-cp314-aarch64-qualify
$ docker buildx bake python-cp314-dev python-phase8-dev
$ docker buildx bake python-matrix
$ docker buildx bake phase8
```

Phase 9 在 Phase 8 的固定行集合后追加 cp310，不改变旧 phase 的行成员。全局 release 或资格策略维护仍会重新绑定并资格化这些行，因此旧 phase target 不是字节级不可变发布快照：

```console
$ docker buildx bake python-native-phase9
$ docker buildx bake cpython-cp310-x86_64-qualify-build cpython-cp310-aarch64-qualify-build
$ docker buildx bake cpython-cp310-x86_64-qualify cpython-cp310-aarch64-qualify
$ docker buildx bake python-cp310-dev python-phase9-dev
$ docker buildx bake python-matrix
$ docker buildx bake phase9
```

组件投影把 `hash_algorithm` 和新增行序列归入 qualification policy，而不归入各行 build policy。因此引入 cp310 会改变共享 Python qualification identity，但 cp311–cp314 的 source、build-policy、native 和两个 target build component digest 必须逐一保持不变；回归测试锁定的是组件身份边界，不把共用构建脚本变更误称为 BuildKit layer 命中保证。

Phase 10 在 Phase 9 的固定五行后追加 cp39，并完成首发 3.9–3.14 矩阵：

```console
$ docker buildx bake python-native-phase10
$ docker buildx bake cpython-cp39-x86_64-qualify-build cpython-cp39-aarch64-qualify-build
$ docker buildx bake cpython-cp39-x86_64-qualify cpython-cp39-aarch64-qualify
$ docker buildx bake python-cp39-dev python-phase10-dev
$ docker buildx bake python-matrix
$ docker buildx bake phase10
```

cp39 的 source、build-policy、native 与两套 target build identity 独立新增，原五行对应 identity 保持不变；共享 qualification policy 与 aggregate identity 按设计重新绑定六行。compile/final 报告、runtime preflight 和双架构 row manifest 都重新从 release/policy 计算该身份，而不信任传入摘要。完整 Phase 10 已实际通过 6 个 build Python、12 个 cross SDK、两套 target 的 locked-sysroot/clean-Rocky 运行时资格化及六行 append-only 聚合。

CI 重构已另增六个行级 qualification policy、十二个行/目标输入投影及六个双目标汇总。`python_qualification_policy.py` 可通过独立固定的目标投影 digest 验证并读取当前行的配置；最终消费者可从完整 release 重新计算预期。原 89 个组件文档保持不变。静态编译资格阶段现已消费新投影及其认证的 source/build-policy 依赖，schema 5 用明确的 input binding 代替完整 release 和全行 qualification 身份；该阶段不再复制完整 release、schema 或 renderer。runtime preflight 与最终验证器从完整 release 独立推导预期，再进行原有 source、sysroot、ABI、ELF、guard 和原始序列化校验；schema 4 编译报告继续按旧完整 release 精确校验。最终报告仍保留完整 release 和全行 qualification 身份，不因嵌套新编译报告而允许跨 release 重用旧执行记录。这些配置身份不能替代实际产物、测试代码、环境和执行证据。正式资格报告链的迁移与实跑仍在进行，详见[内部组件说明](internal-components.md#python-row-qualification-policy-inputs)。

Python 契约是“支持交叉编译扩展”，不是 PEP 517/wheel 编排器。Crossforge 不做 wheel retag、vendoring、manylinux repair，也不支持 PyPy、free-threaded 或 debug Python。

## 8. vcpkg 集成

vcpkg 固定版本要求现代 CMake/Ninja，而 EL8 RPM 只提供旧版本。Crossforge 因此将
vcpkg tool database 选定的 CMake 4.4.0 与 Ninja 1.13.2 官方 Linux 资产安装到独立
`/opt/crossforge/host-tools/cmake/4.4.0` 和
`/opt/crossforge/host-tools/ninja/1.13.2`，保持
`VCPKG_FORCE_SYSTEM_BINARIES=1`，并把 overlay 置于 PATH 首位。Ninja 资产以完整 commit、
GitHub tag-ref/release 原始证据、GitHub SHA256、vcpkg SHA512、解包后 ELF 摘要及
源码 `COPYING` 共同绑定；不因 lightweight tag 或 `immutable:false` release
声称上游签名信任。资格化必须证明 Ninja、CMake、Meson 与 vcpkg 均选择该绝对路径，
且不得覆盖 `/usr/bin/ninja`。

CMake 资产绑定 vcpkg SHA512、独立 SHA256/大小、`cmake`/`ctest`/`cpack` 三个 ELF
摘要及 BSD-3-Clause 许可。对应的 4.4.0 官方源码归档、21 项 SHA-256 清单及其分离
签名也进入同一 source component；断网门禁验证 33,033 个成员、构建入口、README、
源码许可，并证明签名清单同时绑定源码和实际交付的 Linux binary。该签名使用的
subkey 已于 2024-08-12 到期，却在 2026-07-09 签发，因此只能作为明确的
`upstream-signing-subkey-expired-before-signing` 例外，不能宣传为当前有效签名。
二进制离线资格化要求其最高 GLIBC 版本不超过 2.17，无
RPATH/TEXTREL、动态依赖闭合，并实际运行 CMake/Ninja、CTest 与 CPack；
`/usr/bin/cmake` 不得被覆盖。SDK 还要求 `vcpkg fetch cmake` 返回该绝对路径。

供应链基础固定 vcpkg `2026.07.29` / commit
`9e593bb18ea69cc5095e012465dcd675a822ed0d`，并保留非 shallow 的完整 commit
历史；version database 中 22 个不可由 tag 到达的 port tree 按固定 OID 补齐，离线
批量验证全部 3,054 个文件引用的 39,823 个 `git-tree`。匹配的 vcpkg-tool
`2026-07-27` amd64 glibc 二进制单独绑定 SHA256、上游 SHA512、Microsoft PGP
签名、公钥指纹及 LICENSE/NOTICE；同 commit 的源码归档也固定 URL、大小、SHA256、
2,457 个成员、CMake/入口摘要，并证明归档内 LICENSE/NOTICE 与实际选定许可逐字节
一致。构建不在线执行 bootstrap。网络 stage 只获取 registry、签名工具与工具源码，
Git object/许可证核验、PGP、源码布局、Rocky 8 工具执行和 scratch 导出均离线完成；
工具源码只位于 source export 的 `/materials`，不进入 SDK root。上游未把 EL8 系列列为完整支持 host；Crossforge 只声明对该固定
版本和下述资格化端口集合负责，不将本项目结果表述为上游平台支持。

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

当前 `sdk-phase13-base` 直接基于两套 toolchain slice（不依赖 Python matrix），并把固定 registry/tool 安装到
`/opt/crossforge/vcpkg/root`，并从 release component policy 生成三份 CMake
toolchain 与上述五套 triplet。镜像只设置 host triplet，不设置默认 target
triplet；target 必须由用户显式选择。离线资格化会重验完整 Git 历史与工具身份，
再验证锁定 Ninja 路径，并分别构建 host、x86_64 cross 与 aarch64 cross 的
C/C++、static-to-shared PIC 探针；aarch64 产物仅在资格化边界通过固定 QEMU 执行。
`vcpkg-contract-qualified` 另在禁网 stage 中对五套 triplet 逐一执行真实 manifest-mode
`vcpkg install`，同时清空 binary source、阻止源站下载。自有 target probe 必须通过
host-only dependency 生成目标头文件，静态/动态库、编译器、sysroot、Ninja 路径和
ELF machine 均由实际产物复核；共享库只允许 vcpkg 修复后的精确
`DT_RUNPATH=$ORIGIN`。x86_64 consumer 原生执行，aarch64 consumer 只通过固定 QEMU
执行。vcpkg 需要的 patchelf 0.19.0 归档按 URL、SHA256、SHA512 与大小预取，在离线
门禁中再次核验，只进入临时 downloads root。downloads、buildtrees、packages、
installed tree 和该 helper 资产均不进入产品根目录。

curated-port Tier 1 固定 `zlib 1.3.2#1` 与 `fmt 12.2.0#1`。网络 stage 只获取三份
显式 URL/SHA256/SHA512/大小资产，离线 stage 在五套 triplet 中分别执行 manifest
install，验证版本、静态/动态布局、ELF machine 与精确 `$ORIGIN` RUNPATH，并原生或
通过固定 QEMU 执行组合 consumer。Tier 2 固定 `OpenSSL 3.6.3`、`curl 8.21.0#1`
与 `curl[core,openssl]` feature 集，使用同样的离线五 triplet 门禁验证 crypto/TLS
静态与动态库并执行组合 C consumer。Tier 3 固定 protobuf `6.33.4#2`、Boost
`1.91.0` 与 compiled `boost-json`，其 23 个源码/许可证资产均绑定 URL、SHA256、
SHA512 与大小。门禁先构建、审计并执行 amd64 `protoc 33.4`，再由它生成 C++，
为全部 triplet 链接并执行 Protobuf/Boost.JSON consumer；同次运行的 host installed
tree 仅复制到隔离 target scratch root，不启用 binary cache，也不进入产品镜像。

## 9. 构建系统无关的分包

`crossforge package` 调用自研的薄编排层 `crosspack`，后者只接受 staged filesystem 或显式 `source → destination` 映射，不调用也不识别 CMake、Meson、Autotools、Make、Cargo 或 Bazel。因此 CPack 不是正式打包路径。

`crosspack` 负责：

- 按 manifest 将文件分入 runtime、development、tools、debug 等 component；
- 拒绝路径逃逸、文件重叠、遗漏和错误 target ELF；
- 映射 x86_64/aarch64 到 RPM 和 DEB 架构，生成精确 component 间依赖；
- 使用 target objcopy 拆分 debug symbols，执行 ABI/`DT_NEEDED`/RPATH 检查；
- 生成可复现 manifest、SHA256 和临时 nFPM 配置。

固定版本的 nFPM 负责真正的 DEB/RPM 编码、metadata、压缩、scriptlets 和签名接口。Crossforge 不自研包格式，不猜测 SONAME 对应的发行版包名；RPM/DEB 外部依赖必须由下游分别声明。首发不负责 APT/YUM repository 发布。

Phase 14 已固定 nFPM 2.47.0 的 tag/commit、Linux amd64 二进制与源码归档、上游
checksum manifest、Sigstore bundle 和 MIT 许可。联网 stage 仅下载；禁网 stage 重算
SHA256/SHA512/大小，验证 checksum 对二进制归档的绑定，并检查 bundle message digest
及证书中归档的 workflow identity、OIDC issuer、commit 与 tag。独立的
`sigstore-sources-qualified` 进一步用 TUF-authenticated Cosign 3.1.3 在断网阶段验证
该 bundle 的签名、Fulcio 证书链、精确 workflow identity/OIDC issuer、SCT、Rekor
SET/inclusion proof 与 RFC3161 timestamp；因此 release 状态为 `verified`。

`docker buildx bake packaging-qualified` 为 x86_64/aarch64 分别交叉构建真实 ELF，
两次生成 runtime/development/tools/debug 的 DEB/RPM 并要求逐字节一致；随后在固定 Debian
amd64 manifest 中用 `dpkg --root`、在 Rocky 中用 `rpm --root`/`--ignorearch` 安装，
复核精确版本/架构、symlink 与每个普通文件的 SHA256。packages、安装 root 和下载物
均不进入 SDK。启用 `debug_symbols` 时，debug component 必须没有人工 files，且其
component dependency 必须精确覆盖所有 ELF owner；crosspack 在私有 staging 副本中用
对应 target `objcopy --only-keep-debug/--strip-debug/--add-gnu-debuglink` 生成 detached
symbols，原 staging 不变。随后 target `readelf` 拒绝 `DT_RPATH`、TEXTREL、包含空项、
绝对路径或 `..` 的 RUNPATH，以及不能由同一 package set 或固定 target sysroot 解析的
`DT_NEEDED`。RUNPATH 从 ELF 最终 destination 解析，只有仍受 root-owned private prefix
约束的 `$ORIGIN/../lib` 一类规范路径可跨父目录；每个 NEEDED 必须唯一解析到可达的
package component 或 sysroot provider，跨 component 时还必须有显式依赖边。SONAME、
NEEDED、解析后的 provider/destination、RUNPATH、导出符号摘要、readelf 摘要和 sysroot
provider inventory 摘要进入 canonical plan。Phase 14 已实际安装并复核双架构共 16 个包。

## 10. 用户接口

唯一 launcher 是一个无网络、无插件系统的轻量 Python CLI：

```text
crossforge --version
crossforge info
crossforge env
crossforge shell
crossforge run
crossforge package plan
crossforge package build
```

`run`/`shell` 显式选择 `--target x86_64|aarch64`，并可选择 `--python 3.14`、`--vcpkg` 与 `--linkage static|dynamic`。它只在子进程中设置 compiler、sysroot、CMake、Meson、pkg-config、Python 和 vcpkg 环境，不修改全局环境，也不根据宿主或项目内容猜测 target。未选择 target 时使用原生 GTS15 host 环境。

`env` 以 shell 或版本化 JSON 输出同一选择结果，但只暴露 Crossforge 管理的白名单
变量，不回显继承环境中的 token 或 credential。`run`/`shell` 以 exec 语义替换 launcher，
使退出码和 signal 直接到达真实进程。vcpkg downloads/binary cache 位于可写的
`CROSSFORGE_CACHE_ROOT`，不得写入 root-owned `/opt/crossforge/vcpkg/root`；target
选择清除 inherited `PKG_CONFIG_PATH`，Python 选择清除 inherited `PYTHONPATH`。

该入口现已安装到 packaging SDK 的 `/usr/local/bin/crossforge`。`package plan` 在不
调用 nFPM 的情况下生成完整 canonical plan，`package build` 才消费同一 manifest、
staging root 与输出目录执行编码；nFPM 路径、版本和摘要从镜像内固定 release 自动
取得，用户不能替换 backend。`run`/`shell` 复制当前环境再覆盖显式选择，提供完整
compiler/binutils、sysroot、pkg-config、CMake toolchain 与 `MESON_CROSS_FILE`。启用
vcpkg 时才设置明确 triplet 并把 CMake 切到 vcpkg toolchain；`--python` 必须同时
选择 target，并隔离 build Python 与唯一 target sysconfigdata。Phase 15 的
`sdk-complete-dev` 从 packaging-qualified vcpkg SDK 出发，只复制已经资格化的六行
Python 产物和 final report，再由 launcher 离线遍历 native host 以及 2 target × 6
Python minor × 2 linkage 的 24 种选择。环境存在性不能代替用户路径验收：同一阶段还
必须通过 `crossforge run --target` 为 x86_64/AArch64 分别执行真实 CMake 4.4.0 +
Ninja 配置和 C/C++20 链接，复核 CMake cache 的 toolchain/compiler 绝对路径与最终
ELF machine；target binary 只检查字节和 ELF，不执行。public-candidate 再从匿名拉取的
digest、只读 root 和 UID 1000 可写 workspace 重跑同一 fixture，覆盖真实下游容器边界。
该目标仍为 cache-only `-dev`，不得发布。
公开 `sdk-candidate` 默认使用 `crossforge` UID/GID 1000，保持 `/opt/crossforge`
root-owned；workspace、home、cache 和 `/tmp` 是唯一运行时可写边界。显式覆盖任意
UID/GID 时，不可写的 inherited home 会安全回退到该 UID 专用的 `/tmp` home/cache。

## 11. 验收与发布门禁

构建流程必须是：

```text
source commit → build once → candidate digest → 原物验收 → registry-side promotion
```

所有测试通过完整 OCI digest 拉取候选镜像。Release 不重建，只给已资格化 digest 增加不可变版本标签并更新稳定通道。手动 `stable promotion` 工作流以成功的 public-candidate run ID 和 `PROMOTE-v<version>` 双重输入为入口，并使用 `production` environment 作为运维审批边界；它从 GitHub API 重新约束 workflow path、main head、run attempt 与成功结论，再 checkout 候选的精确 source commit。四组不可变 workflow artifact 必须仍可下载，所有 `candidate.json` 副本逐字节相同，source binding、原生 AArch64 compiler 证据及其原始输入、固定 TUF root 下的 candidate/source 签名均重新验证后才允许写 tag。

候选的来源镜像发布、SDK 发布和最终匿名消费者检查分别运行。两个发布 job 在推送并绑定身份后保存严格 checkpoint，下游按成功上游的不可变 artifact ID 下载，并验证独立 canonical SHA256、原 source/run/attempt、当前 release、OCI 原始 index、Buildx metadata、来源归档身份及固定 SBOM generator 报告。SDK checkpoint 嵌入原来源 checkpoint，五份来源文件保持原字节。checkpoint 只记录发布身份；SDK 构建前仍匿名验证完整来源归档，最终消费者仍重新验证公开证明与镜像集成，且仅有 packages:read 权限。

候选前置验证现在以固定 `profile: full` 调用组件工作流，覆盖全部 canonical release stages，并集中生产、签名和保存缺失的原始工具链/Python 组件。候选不进入周期性资格缓存 writer 队列。原始组件 producer 的调用者只增加 main 上的明确手动 candidate workflow，仍要求 clean checkout、workflow/source SHA 相同，并保留 raw role 范围和 writer/signer 权限分离。SDK 发布再次认证目录、核对当前输入及实际 OCI，绑定 33 份原始组件后重新解析实际消费图；若仍包含 GCC/CPython 源码编译则失败。发布后重新检查源码、来源归档、图和执行环境。此接口保留既有资格门禁，不是正式行资格 receipt 复用；真实 GitHub 执行和性能尚待验收。

部分重试可复用成功上游的 source/SDK checkpoint 或最终消费者/native artifact。签名 artifact 中的 `candidate-recovery.json` 绑定完整 candidate manifest 摘要、probe/report 字节摘要和原 artifact ID/name/attempt；recovery schema 2 追加来源/SDK 的原 attempt 和 checkpoint SHA256，要求 source ≤ SDK ≤ 最终消费者 ≤ native ≤ signing。promotion schema 2 将恢复记录嵌入 `release-promotion.json`，经 GitHub 同 run/source 元数据和既有语义验证后进入持久归档。旧 promotion schema 1 保持所有 artifact 同 attempt 的严格契约。推送成功但尚未成功封存上传 checkpoint 的失败、跨 candidate run 恢复及实际 GitHub 部分重试验收尚未完成；不能通过 tag 推断缺失的 checkpoint。

SDK checkpoint schema 2 另保存 `component-selection.json`。最终消费者转交其独立 SHA256，签名前按这个摘要和候选 source commit 检查原组件选择，再以 recovery schema 3 将其完整嵌入恢复记录；原 catalog/receipt/OCI digest 与 producer 随 promotion 进入已有十四份 payload 的持久归档。诊断文件过期不会抹去这些原组件引用；记录本身仍不授予资格。旧 checkpoint/recovery schema 保持原严格读取契约。

原始工具链/Python producer 的部分重试另以成功上游 job 输出约束交接：签名接收原 producer invocation、handoff SHA256 和 artifact ID；存储接收原签名 artifact ID、catalog/bundle 原始字节 SHA256 及 signer invocation。同一可信 run/source 内要求 producer ≤ signer ≤ 当前重试，下载前拒绝缺失、失败或不完整的上游输出。签名重试不改写原 receipt/catalog producer；存储重试保留原签名字节，并继续执行固定 verifier，不重新编译或签名。相关嵌套 caller 只补齐 artifact 下载所需 actions:read，writer 与 signer 权限仍分开。成功上传前的中途失败、跨 run 恢复和真实 GitHub 重试验收仍未完成。

晋升仅通过 registry-side manifest copy 给 candidate digest 增加 `v<version>` 和 `gts15-el8`，并给其绑定的 source digest 增加 `source-v<version>` 和 `source-gts15-el8`。stable promotion 只接受不含 prerelease/build metadata 的三段 SemVer。版本 tag 不存在时才可创建；已存在时必须已指向完全相同 digest，否则失败，稳定通道在两份版本 tag 就绪后才移动。注销 registry 后必须匿名重新解析四个 tag 的原始 manifest 字节并得到预期 digest，随后生成符合严格 schema、跨重试字节稳定的 `release-promotion.json`。十四份 candidate/source/native ARM/Sigstore/OCI-attestation/SBOM-generator/promotion 原始证据同时进入固定名称、顺序、owner、mode 和零时间戳的 USTAR，内层严格 manifest 逐文件绑定 SHA256/大小，外层另有 SHA256 sidecar；验证器从归档安全流式解出临时文件并重新运行原始语义门禁，不能只信任内层 manifest。

GitHub repository 必须在首次发布前启用 immutable releases 和 Private Vulnerability Reporting。`production` environment 必须只允许 `main` deployment、要求唯一的 repository-owner reviewer，并在单维护者模型下允许 self-review；仓库默认 `GITHUB_TOKEN` 必须保持 read-only 且不能批准 PR。promotion/rollback 共用的 control-plane action 从只读管理 API 取得上述五份状态并通过严格 schema 验证，缺失 environment 或任一弱化都会在任何 Release/OCI 写入前失败。晋升随后创建或幂等恢复 draft，上传 evidence tar、sidecar、`candidate.json` 与 `release-promotion.json`；只有 OCI 版本/通道 tag 全部精确解析后才发布 draft，并要求 Release API 返回 `immutable:true` 和四份带 SHA256 digest 的完整资产。由此 Git tag 和 release assets 在 Actions 90 天工件过期后仍受 GitHub 不可变发布与 release attestation 保护，未公开的安全报告也有私密入口。该工作流不得构建 SDK/source、不得重新签名，也不能把成功 job status 当作资格证据。首次 public candidate 和首次 stable promotion 尚未实际运行，因此当前仍是 implemented/unproven。

每个 GitHub Actions workflow 必须显式声明顶层最小权限，不能依赖可被管理员修改的
仓库默认值；普通 CI 固定为 `contents: read`。所有非本地 Action 必须引用完整 40 位
commit SHA，版本号只能作为便于审计的注释，禁止执行可移动 tag。

长期 rollback 不依赖已经过期的 candidate workflow artifacts。手动 `stable rollback`
只接受三段稳定 SemVer 和 `ROLLBACK-v<version>`，与 promotion 共用不可取消的
`stable-promotion` concurrency group。它要求目标 GitHub Release 已 immutable，Git tag
精确 peel 到 release commit，四份 asset 的 API digest/size 与下载字节一致，并从 17 项
evidence tar 重新验证 native ARM、Qt、source、SLSA/SPDX、SBOM generator 与签名身份；
随后匿名确认版本 tag 和 OCI digest，只按 source→SDK 顺序移动两个 stable channel。
rollback 不创建/移动版本 tag、不重建、不重签，失败后的已完成 source-channel 更新可由
同一幂等 workflow 安全重试。

托管 CI 与发布资格运行于 GitHub runner；本地开发验证使用同一 Docker/Bake 图。具体执行图、缓存权限、冷构建测量和
失败诊断见 [`github-actions.md`](github-actions.md)。受信 main 资格化通过独立 GHCR
registry cache 复用中间层；PR 只能读缓存。缓存不改变资格化与发布身份，candidate
必须先通过相同提交的完整分阶段资格化，再沿原有图生成和验证公开 digest。

测试分层如下：

- PR：JSON/Schema、逐文件 Bash/Python syntax、Python 单测、Docker/Bake 与 Actions 静态检查，以及比较 checked Bake 材料闭包后选择的真实构建；共享输入沿实际下游传播，未知路径、缺失 base 或不支持的材料解析执行 full 兜底。`pr-required` 汇总必要检查，意外 skip/cancel 不得放行；
- candidate：双 target C/C++/ABI、代表 Python、vcpkg ports、DEB/RPM 安装测试；
- nightly/full：双 target 的 `gcc/` 下 `check-gcc` 与 `check-g++`、针对最终 compiler/runtime 的 installed `runtest --tool libstdc++`、`check-target-libgomp`，完整 Python 矩阵，以及 Qt 6.8.4 双 target；语言测试必须直接从 `gcc/` 子目录启动，使 GNU Make 的 jobserver 分片真正分配给对应 DejaGNU worker，禁止从顶层 `check-gcc` 间接重跑全部语言；GCC 15 的顶层 `check-target-libgcc` 是无测试、无 summary 的空目标，不能作为资格化证据；libgcc 由 compiler testsuite 与最终 hybrid runtime 门禁覆盖；
- release：同一 digest 的原生 aarch64 终检和资格化证明检查。

CI 中可能持续数小时的 Python/vcpkg、GCC 与 Qt Bake solve 必须由
`ci-build.py` 调用 `run-with-heartbeat.py` 启动：完整 stdout/stderr 保存为 artifact 日志，
每 60 秒输出 PID、elapsed 与日志大小，每 30 秒记录资源用量；退出码保持不变，
SIGINT/SIGTERM 转发给 Buildx。候选发布仍直接使用同一心跳包装器。心跳只证明
进程仍由 runner 管理，不替代各资格化脚本自己的超时、结果或证据门禁。
Qt 的内部 Ninja 输出继续完整写入资格证据日志；同一包装器以独占日志模式执行命令，
每 60 秒额外报告日志字节数，因此长时间步骤可以区分持续编译与内部停滞而不把海量
构建输出复制到 Actions 日志。

原生 aarch64 release gate 分为两个互不混淆的执行域。`linux/amd64` publish job 先从匿名可拉取的完整 candidate digest 运行镜像内交叉工具链，重新生成 C、C++20、LTO、LTO archive、libgcc wide-division 和跨 DSO exception 探针；compile report、八个 artifact、candidate identity、AArch64 qualification component 与 candidate policy component 被封装为固定顺序、固定 owner/mode/mtime 的 USTAR，并对整包计算 SHA256。随后 `ubuntu-24.04-arm` runner 只下载这份不可变 bundle，在固定 Rocky Linux 8.10 arm64 child manifest 中以 `--platform linux/arm64 --network none --read-only` 原生执行，要求 `RUNNER_ARCH=ARM64`、host/container `uname -m=aarch64`，且不得携带 QEMU。最终 JSON 重新绑定 bundle、candidate OCI index/platform digest、release、runtime manifest、loader/DSO 解析和每个执行结果；任一身份不一致都使整个 public-candidate workflow 失败。Qt native gate 同时从实际执行的 rootfs 保存 target-build 和 runtime-overlay 原始证据；`validate-qt-native-release.py` 在上传前重新验证 schema、自摘要、qualification component、release/base image、candidate/source commit、rootfs 及最终报告对两份输入的 canonical digest，后续晋升不能只信任 GitHub job 的绿色状态。该工作流未实际产生首份公开候选证据前，状态仍是 implemented/unproven，不能宣称 release-qualified。

GCC testsuite 必须指向镜像内最终安装的 compiler，并使用 EL8 shared runtime + Crossforge nonshared/libgcc 的最终 hybrid 组合。GTS 的 Graphite 补丁在运行时加载 `libisl.so.23`，因此工具链在 compiler-private 目录携带由同一锁定 SRPM 构建且校验 SONAME 的 ISL；hybrid `libgcc_s.so` 在 EL8 shared DSO 后以 `libgcc.a` 和 `libgcc_eh.a` 补充 GCC 15 新符号，并以 heap-trampoline 实际运行门禁验证。`libgomp` 只留在 build tree 中供测试。

installed libstdc++ 测试从独立 workdir 运行，`LD_LIBRARY_PATH` 只包含 EL8 sysroot 的 `libstdc++.so.6`、`libgcc_s.so.1` 与目标编译器前缀内的 `libatomic.so.1`；三者都先解析并约束在可信根内，拒绝 host 或 build-tree library 泄漏。四个 DejaGNU worker 复用上游 `GCC_RUNTEST_PARALLELIZE_DIR` 原子分片协议，按分钟报告进度；连续 600 秒无日志增长或总时长超过 7200 秒即失败。当前 GTS 快照缺少 `GCOV_UNDER_TEST` 与 cross `gcc-ar` testsuite 修复，两项按上游补丁精确回移。EL8 shared libstdc++ 保留 PR93672 的无限循环缺陷，因此该单例以 pinned test-only patch 明确标为 UNSUPPORTED；其他 runtime 差异仍进入精确候选，不得隐式跳过。

x86_64 在锁定的 EL8 test host 中直接执行；aarch64 日常使用固定 QEMU，分别在锁定 sysroot 与干净 Rocky arm64 根执行并生成结构化证据，release 使用原生 ARM。DejaGNU 1.6.1 的 local `unix_load` 不读取 `exec_shell` board field，因此两套 aarch64 board 必须显式覆写 `unix_load`，将每次目标执行前缀固定为 QEMU/runtime root/CPU/uname 并保留 DejaGNU status/output 语义；禁止依赖宿主 binfmt。已知失败按 status + suite + test identity + occurrence 维护精确基线；新增或已消失的 FAIL/XPASS/ERROR/UNRESOLVED/KFAIL/KPASS/WARNING 均使基线差异失败，不允许用失败数量阈值掩盖回归。Phase 16 用 `execute.exp` 单例证明三个 board 与基线链路；Phase 17 先以隔离、不可发布的 x86_64 observation 生成候选，逐项审查并用第二次完整运行精确复现后才能形成 qualification baseline。

当前 x86_64 full baseline 包含 87 条精确 FAIL：21 条来自最终安装 `cc1` 加载 diagnostic text-art testsuite plugin 时缺少内部 `text_art::table::set_cell_span` 符号，66 条来自目标程序显式运行在冻结 EL8 `libstdc++.so.6.0.25`/libgcc_s 上的行为差异。它们仍完整保留在 qualification report 中，不是跳过项；任一记录新增、消失或 occurrence 改变都会使候选失败。候选阶段还会根据 release-bound full plan 从全部结果重新推导异常集合，并与 baseline 逐项比较。

Qt 验收固定 Qt 6.8.4 `qt-everywhere` 官方源码和 SHA256，构建完整开源 Linux desktop 模块集合，至少包括 qtbase、qtdeclarative、qtshadertools、qttools、qtwayland、qtmultimedia、qtquick3d 和 qtwebengine；不构建 examples/tests/docs。host tools 与 target 使用同版本并通过 `QT_HOST_PATH` 连接，required module/feature 被静默跳过即失败。Qt 产物只作为测试 artifact，不进入 SDK 镜像。

Qt source boundary 同时固定 994,798,840 字节归档、官方 108 字节 SHA256 sidecar 及其离线 base64 envelope。上游未提供独立签名，因此真实性边界明确标为 `hash-pinned-https-sidecar-no-signature`，不得包装成签名验证。离线 source acceptance 扫描全部 399,185 个 tar member，要求唯一 `qt-everywhere-src-6.8.4` 顶层、无绝对/逃逸路径、无 device/FIFO，并逐字节验证八个 required module 的 `CMakeLists.txt`、根 `configure` 与七份顶层许可证；输出仅为 cache-only source artifact，不进入任何 SDK/candidate ancestry。

`config/qt-qualification.json` 是 Qt 构建的严格 locked-input contract：固定八模块依赖顺序、same-version host Qt、CMake/Ninja 版本、WebEngine 的 Node/Python/html5lib/Bison/Flex/GPerf/pkg-config 与 Linux support checks、两套 target 及禁止 target execution 的 cross 边界。Rocky 8.10 host 固定 `nodejs:20` 与 `python38:3.8` module streams；由于仓库只提供 Python 3.6 路径下的纯 Python `python3-html5lib`，资格阶段必须显式设置 `/usr/lib/python3.6/site-packages`，并用 Python 3.8 实际导入 html5lib、six、webencodings，不能依赖偶然的全局 `PYTHONPATH`。三份 RPM plan 的 canonical digest 直接进入该 contract：host 闭包继承 `host-build-common`，启用既有 Perl streams 加 Node 20/Python 3.8，并显式锁定 Chromium host utilities 链接所需的 GCC Toolset 15 `libatomic` 开发库；两个 target 构建闭包分别通过显式 parent root/RPMDB 继承对应核心 sysroot，只选择 target/noarch 库和头文件，禁止 Node/Python/GPerf 等 host 工具混入。`host-qt-build` 锁含 191 个 RPM payload；x86_64/aarch64 构建 overlay 分别含 205/202 个，并为 Chromium/ANGLE 链接显式加入对应架构的 EL8 `libatomic`；公共 name 的 EVR 必须完全一致，只允许 x86_64 因 libdrm 依赖多出 `hwdata`、`libpciaccess`、`libpciaccess-devel`。

`config/qt-runtime-qualification.json` 独立绑定上述 build qualification component、clean-Rocky/QEMU/native-release tiers 和另两份从空 root 求解的 runtime lock，不得复用构建 overlay 或完整 sysroot。两套 runtime plan 请求 58 个与受审 ELF、平台插件、字体、TLS、NSS、PulseAudio、CUPS、AT-SPI、libinput 和图形栈直接相关的 target/noarch RPM 根，禁止 `-devel`、`-headers`、`-static` 与 host 工具；DNF 得到 x86_64 181 个、aarch64 179 个签名 payload，公共 package EVR 完全一致，唯一允许的 x86_64 增量为 `hwdata` 和 `libpciaccess`。五套锁、plan、签名 repomd 与相应计划摘要必须一起验证。`future/qt-runtime-qualification` 依赖 `future/qt-qualification`，反向依赖被禁止；更新 runtime lock 不改变 Qt、GCC、Python 或 vcpkg 的 build component 身份。三份 Qt build RPM lock stage 不读取维护用 `rpm-input-base` 或 `release.json`：专用 `qt-build-lock-input-base` 只携带 `future/qt-qualification`、三份 build RPM plan 与 schema，每个 lock 再同时绑定对应的 `rpm/host-build-common` 或 `rpm/sysroot-<arch>` 父组件。验证器交叉检查 Qt plan、RPM plan、父锁引用、当前 lock pin、Rocky base/trust 和签名 metadata，并把两份组件摘要写入安装 marker；因此 runtime-only release 变化不再影响 Qt build cache。运行 overlay、产物装配和探针位于独立 `docker/qt-runtime.Dockerfile`，只通过内部 cache-only `target:qt-<arch>-build-qualified-root` 消费资格化完整构建根；公开的 `qt-<arch>-qualified` 仍是 scratch 证据导出。修改 runtime 脚本或 staging 不得展开 Qt source/toolchain 构建图。最终 SDK/candidate 仍复制完整 `release.json`，所以必须重新绑定最终 OCI 摘要，但 Qt 产物不会因此进入产品镜像。

clean-Rocky overlay 先完整验签 runtime lock，再只安装基础镜像按 package name 尚未提供的 RPM；x86_64 安装 79 个并保留 102 个，aarch64 安装 77 个并保留 102 个。两边均实际保留 51 个与最新仓库锁 EVR 不同的基础包，以验证固定 OCI child 的真实兼容性，而不是用最新 sysroot 覆盖它们。Rocky 最小镜像的 `coreutils-single` 对空 root closure 中 `coreutils` 的文件级替代是唯一显式白名单，并连同两侧 NEVRA 进入证据；其他文件冲突一律失败。每个目标运行门禁都检查下游 Qt Widgets 消费者和 `QtWebEngineProcess` 的动态加载器闭包，并由锁定目标 GCC 在独立 runtime stage 编译最小纯 C `dlopen(RTLD_NOW)` 探针，对 offscreen、XCB、Wayland 和 FFmpeg multimedia 插件执行真实重定位；`LD_DEBUG=libs` 记录的解析结果会去除 PID 后进入确定性证据。随后消费者必须在 offscreen 模式真实构造 widget，且日志确认实际加载 `libqoffscreen.so`。AArch64 日常门禁只使用固定 QEMU 10.2.3/cortex-a53/4.18.0 显式执行，仍不能代替发布前原生 ARM64 终检。候选工作流并行要求 BuildKit 直接把不含 QEMU 的 AArch64 runtime root 输出为一次性 rootfs tar，先检查两份资格证据存在且 QEMU 路径缺席，再绑定 tar 的实际 SHA256 和大小；原生 `ubuntu-24.04-arm` runner 按摘要验收后以 `linux/arm64` 导入本地镜像，在断网、只读 root 下重跑同一套 Qt 门禁。native-release 证据必须同时绑定 rootfs SHA256、候选 source commit、公开 OCI index、amd64 manifest 及 candidate manifest 摘要；该 root 只在工作流工件中短期保留，既不推送 registry，也不进入 SDK/candidate ancestry。

Qt Multimedia 固定使用与 Qt 6.8.4 官方归属信息一致的 FFmpeg 7.1.1。`sources/ffmpeg` 锁定 FFmpeg 官网 tarball、分离签名、官网发布公钥及主指纹，并在断网阶段完成 GPG 验签、路径/类型/成员数/标志文件与 LGPL 许可身份检查。`ffmpeg-qualified` 为 host、x86_64 target 和 aarch64 target 构建 Qt 所需的五个 shared/PIC 库，显式禁止 GPL、version3、nonfree、autodetect、程序及不需要的 avdevice/avfilter；证据绑定源码、Qt 计划、RPM 锁和构建器，并检查配置宏、ELF machine/SONAME/NEEDED、RPATH/TEXTREL、头文件、pkg-config、许可与 host 版本探针，cross 两行禁止执行目标代码。Qt configure 必须同时确认 FFmpeg、PulseAudio、GBM、libudev/libinput，不能接受 `No media backend found` 或 bundled minigbm 的降级。`qt-host-configure-qualified` 在断网阶段绑定 Qt/FFmpeg/xcb 源码与构建身份、计划和工具版本，验证模块/特性、独立 GNU C++20 编译以及告警集合；只允许文档已关闭时的 QDoc 和 Clang lupdate 两条提示。`qt-host-qualified` 在独立缓存层中验证 13 个下游 CMake 组件及同名共享库、XCB/offscreen 平台插件、`QtWebEngineProcess`、QtTools 用户入口和 WebEngine 资源，逐个检查 ELF machine、SONAME、NEEDED 解析、无 TEXTREL 以及仅限 `$ORIGIN` 或锁定 qualification 树的 RUNPATH，并通过 `qt-cmake` 真正编译和 offscreen 运行下游应用。该 host 产物仍只服务后续双 target 构建，不进入 SDK 或 candidate 祖先；host 资格也不能替代 target 资格或运行时验收。

Rocky 8.10 的 BaseOS/AppStream/PowerTools 不提供 Qt xcb platform plugin 所需的 `xcb-util-cursor-devel`，因此不得通过未锁定的 EPEL 暗中补齐。Crossforge 把上游 `xcb-util-cursor 0.1.6` 建模为独立的 `sources/xcb-util-cursor`：源码与 detached signature 固定到 X.Org 官方归档，发布公钥从 Arch Linux 官方打包仓库的全指纹路径独立取得，并在离线 source acceptance 中核对 archive/signature/key digest、主密钥完整指纹、GPG `VALIDSIG`、唯一顶层、40 个 member、MIT `COPYING` 与构建入口。`xcb-util-cursor-qualified` 在已锁 host 和双 target overlay 上构建同一源码；manifest 固定 builder digest、image、target、tier、parent/overlay lock、compiler dumpmachine、ELF machine/SONAME/NEEDED、header/pkg-config/license digest 及 RPATH/TEXTREL 结论。只有 host 执行 `dlopen` 探针，cross stages 不运行 target 产物。该前置库只服务 Qt qualification，其 host/target 产物不进入 SDK 或 candidate ancestry。

Qt target 源树固定回移 XNNPACK `1b11a8b0620afe8c047304273674c4c57c289755` 的生成源码/模板改动，以用 `uint16_t` NEON load 保持 FP16 位模式，并消除 GCC 15 对旧 `vld1q_dup_f16(uint16_t *)` 的类型拒绝。补丁路径、SHA256、作用域、上游仓库和完整 commit 均进入 qualification plan 身份；host 源树不应用该补丁。

`qt-target-configure-qualified` 分别绑定 x86_64/aarch64 的 target、sysroot、same-version host Qt、依赖构建证据、XNNPACK 补丁日志和完整工具身份，确认 cross compile、13 个必需组件和 Linux desktop/WebEngine 特性，并拒绝 host GN/Ninja、target pkg-config 执行或任何 QEMU/HOSTRUNNER 适配。`qt-target-build-qualified` 在完整安装后审计 13 个同名 Qt shared library、XCB/offscreen 插件、`QtWebEngineProcess` 和四个 WebEngine resource，要求精确 ELF machine/SONAME、全部 `DT_NEEDED` 有唯一受审 provider、无 TEXTREL，且 RUNPATH 只能使用目标 `/usr/lib` 或 `$ORIGIN`。资格化还用正式 chainload toolchain 和全部 13 个 `find_package(Qt6 COMPONENTS ...)` 真正构建 C++20 下游消费者，明确禁止把构建或 qualification 暂存路径写入产物。

双 target 的 `install_manifest.txt` 也属于受审证据：x86_64 为 13,649 条、aarch64 为 13,650 条，二者均对应 11,894 个唯一安装路径。允许的重复严格限于 125 个 `.prl` 文件各出现 15 次和五个公共生成头各出现两次；aarch64 只额外允许 NEON 私有头 `qdrawhelper_neon_p.h` 出现两次。任何新增、消失或次数变化都使资格失败。上述 cross-build tier 从不执行 target 代码；干净 Rocky 8.10 的原生/QEMU 执行和原生 ARM 终检属于独立 runtime 门禁。

## 12. 发布、供应链与许可边界

Rocky Linux 8.10 是基础镜像、host packages、sysroot 和 GTS SRPM 的单一供应链。所有源码、RPM、工具和基础镜像均固定 hash 或 digest；禁止 `curl | sh`。BuildKit cache 只用于加速，不构成发布身份或测试证据。

`release.json` 是唯一人工维护的版本源。`config/generated/` 将它投影为 build、qualification、supply 与 future 四类组件身份，并用单向 `release-binding.json` 绑定完整 release digest；共享 Python 实现策略另有显式投影。生成器要求每个 release 叶字段有明确分类，并保证版本行、架构及 host closure 的无关变化不会污染其他 build identity。ABI 输入只生成 `abi/{x86_64,aarch64}-baseline` 与 `abi/python-providers` 三个 qualification component：对应 toolchain qualification 依赖各自 baseline，Python aggregate 直接依赖三者。ABI pin 更新因此不会改变 GCC、Python row 或 zstd 的任何 build component。维护、GCC/Python 资格化和最终产品集成仍有完整 release 输入；工具链 smoke/runtime 资格化按以下独立策略绑定。

组件实现也遵循同一边界：`release-components-core.py` 只包含 toolchain、ABI、Python 等稳定核心，`release-components-vcpkg.py` 是 CMake/Ninja/vcpkg 扩展，`render-release-components.py` 仅组合两者并写入完整 component graph。Python 的 Docker 资格 stage 只复制核心文件；工具链 smoke/runtime stage 只需要 `toolchain_policy.py`、严格的 component reader 和实际探针辅助文件。vcpkg policy 或 fixture 变化因此不会因共享渲染脚本字节变化重跑 12 套 Python target 资格化。回归测试同时锁定共享组件摘要不变性和 Docker COPY 边界。

工具链策略以 `toolchain/<arch>-qualification` 的可信 canonical SHA256 为根，校验其 build、ABI baseline 及 build 下 GCC/binutils source 共五份已有投影。策略绑定 target/sysroot、版本与来源、冻结 ABI、干净 Rocky runtime 以及原生 x86_64 或显式固定 QEMU 执行器。Docker 资格阶段使用 `--components`，不复制完整 release；schema 2 报告的 `input_binding.policy_sha256` 绑定该策略，并禁止同时声称 `release_sha256`。旧 `--release` CLI 仍保留完整 release 报告和原校验语义。

最终 SDK 从当前完整 release 独立推导工具链策略，vcpkg SDK 则使用已认证组件策略；二者检查原报告、资格组件与 locked/clean runtime 成功状态。vcpkg 契约还要求工具链报告字节与已资格化 vcpkg SDK 记录的 SHA256 一致。无关 Python 变更可以保持工具链报告的原身份；组件 receipt 复用另外要求产物、完整实际资格材料、校验实现和执行环境严格匹配，并保留原 producer 与执行区间。任何新候选仍须执行最终镜像集成及原生 ARM 门禁。

共享 `toolchain_report.py` 提供直接消费已认证 toolchain policy 的入口；它要求 scoped 报告，不能把旧 release 报告改写成组件报告。完整 release 适配器保留旧格式及相同运行时校验。`vcpkg_policy.py` 认证 SDK build 根、两个独立工具链资格根，以及 CMake/Ninja 的来源输入；后续契约和 tier1–3 根逐层绑定 SDK、工具链与前序资格。每层 schema 2 报告使用自身输入 binding，后续阶段核对前序报告；SDK 与契约阶段仍重新验收实际工具链报告，tier2/3 还核对提供 patchelf 身份的契约报告。五个 Docker 阶段均只复制六个运行模块，不复制完整 release 或组件生成器，保留的投影位于 `/opt/crossforge/qualification/vcpkg/inputs`。

原 vcpkg `--release` CLI 继续生成 schema 1 报告并绑定精确完整 release；其消费者可从完整 release 独立推导预期，验收新 scoped 前序报告。`packaging-sdk` 在自己的消费边界复制当前 release，供 launcher、分包和完整 SDK 使用。完整 SDK 在最终集成时也从完整 release 独立推导 vcpkg SDK 策略，验收新报告或精确匹配的旧报告，其 schema 2 集成报告记录 vcpkg 报告格式和文件 SHA256。产品版本或单行 Python 输入变化可以保留 vcpkg 资格配置身份，最终 SDK 集成仍须重新执行。这些配置与图检查不是新执行 receipt、跨 runner 资格复用授权或 CI 耗时证明。

`replay-sources.yml` 提供独立的手动源码编译重放范围：单架构 binutils/GCC，或单行 build/x86_64/aarch64 CPython。`ci_source_replay.py` 从真实可达 recipe 核对原编译 RUN，仅对相应 owning stage 设置 `no-cache-filter`，并用结构化事件、实际环境和构建后材料复核确认新执行。CLI 要求显式固定 builder、新诊断目录，拒绝组件替换、资格重放和缓存写入组合，全部输出限制为 cache-only。既有 canonical 阶段门禁保留，但重放记录只证明选中编译阶段；源码取得、prepared 输入、其他编译器和资格步骤仍可使用普通缓存。`cold` 继续只控制远程缓存导入，不能单独证明重建。实际新编译和 GitHub 验收尚未运行。

Rocky OCI index、QEMU index/manifest/attestation/SLSA predicate、QEMU Git tag/commit，以及 Ninja GitHub tag-ref/release 与 commit 原始字节以 base64 envelope 签入 `evidence/`。离线 validator 必须重算 OCI/GitHub evidence digest 与 Git object ID，并验证 platform child manifest、attestation subject、provenance builder/build arguments 和源码 tag→commit 关系。QEMU 10.2.3 官方源码归档、分离签名和官网链接的发布经理公钥也已固定；`qemu-source-qualified` 在断网阶段核对 84,628 个成员、版本、许可证、唯一一个已审核绝对 symlink，并要求 GPG 同时产生精确 `VALIDSIG` 与 `EXPKEYSIG`。执行器并非 pristine QEMU 直接产物，因此同一 export 还锁定 provenance 指向的 tonistiigi/binfmt builder commit 源码、764 个成员、Dockerfile/configure、MIT 许可和实际启用的 `cpu-max-arm`/`preserve-argv0` 补丁。该 QEMU 签名创建于公钥 2026-05-11 到期之后，因此只能标记为 `cryptographically-valid-expired-key`，不能宣传为有效期内的维护者签名；正式发布仍需法律/安全评审接受此明确例外或取得上游更新的可信证据。Ninja lightweight tag 也无独立签名，因此依赖完整 commit 与多重内容摘要。

Sigstore 公共信任 bootstrap 已进入 release 的 supply identity：仓库保留官方 TUF root 5 到 root 15 的全部 exact-byte envelope、targets 14、其授权的 `trusted_root.json` 与 artifact key。最早的纯 platform-Python gate 用 OLPC canonical JSON 和 P-256 ECDSA 逐代验证旧/新 root threshold、自签 threshold、targets threshold、metadata 有效期及 target length/SHA256；该链已与 Cosign 的 TUF client 和 OpenSSL 结果交叉验证，且根轮换不会污染任何 build/qualification component。Cosign 3.1.3 二进制和 KMS bundle 同样固定 URL、大小与 SHA256；先由上述 artifact key 直接验证二进制签名，再由已认证的 Cosign 用完整 bundle 自验。`sigstore-sources-qualified` 随后在 `--network=none` 阶段对六个 CPython source bundle 执行真实消息签名、Fulcio 证书链、精确 SAN/issuer、SCT、Rekor SET/inclusion proof 与时间校验。3.10–3.14 均强制 RFC3161 timestamp；3.9.25 的上游 bundle 不含该字段，只允许唯一的 `upstream-bundle-omits-rfc3161` 例外并依赖已验证 Rekor integrated time。release 中六行状态因此为 `verified`，资格报告绑定 release、verifier、trust root 和每项 artifact/bundle/identity，且只进入 candidate 的 qualification 目录，不把 Cosign 或源码带入产品镜像。

每个 release 同时提供：

- 用户 SDK 镜像；
- 对应 source bundle（GTS/binutils SRPM、镜像及 sysroot RPM 对应 SRPM、CPython sources、patches、构建脚本和许可证）；
- SPDX/CycloneDX SBOM、max-mode provenance 和 qualification report；
- 通过 GitHub OIDC/Cosign 对镜像 digest 与资格化声明做的 keyless signature。

public-candidate 的 keyless signature 位于所有原生 ARM64 门禁之后。独立
`sign-candidate` job 重新下载并验证 candidate、compiler probe bundle、native GCC
report 与 Sigstore source report，再从
`cosign-host-tool` 导出 TUF-authenticated Cosign；它只对唯一 OCI digest 执行
`cosign sign --yes`。随后必须退出 GHCR，以 release 固定的 workflow SAN、GitHub
Actions OIDC issuer 和 `trusted_root.json` 匿名执行 `cosign verify`，并校验输出中每个
signature 的 repository/digest。签名证据作为 90 天 workflow artifact 保存；签名失败
或 native job 未完成时工作流整体失败，且仍不得创建 SemVer/稳定标签。

SBOM generator 固定为 BuildKit Syft scanner v1.12.0 的 OCI index 与 linux/amd64
manifest digest，candidate/source 构建不得解析 `stable-1`。其 annotated tag object、
peeled commit、43,732,088 字节源码、12,536 个归档成员、Apache-2.0 license 与固定
maintainer key 进入独立 supply component；联网 stage 只取得已固定 SHA256/SHA512 的
source/tag JSON，禁网 stage 同时复核 GitHub verified payload 和 OpenPGP `VALIDSIG`，
随后把源码、tag evidence、key 和报告加入 corresponding-source archive。

镜像内保留 `SOURCES.json`、`SOURCE-OFFER`、原始许可证文本和严格的 `/opt/crossforge/LICENSES.json`；该文件逐项绑定路径、组件、角色、大小与 SHA256，并明确标为 `file-inventory-not-legal-conclusion`。SPDX SBOM 与 SLSA v1 max-mode provenance 是 OCI attestation，不得误称为镜像内文件，也不得只因 Buildx 参数存在就宣称有效。source 与 SDK index 均必须只有一个指向 linux/amd64 manifest 的 unknown/unknown attestation descriptor；匿名拉取 attestation manifest 和两份 in-toto blob 后，门禁重算全部 descriptor digest/size，约束 OCI artifact subject，要求 provenance 的 Bake target、max LLB definition、完整 request 与 source revision，以及 SPDX document 结构。candidate 必须先生成并公开带 repository/revision OCI 标签的 source OCI，再把其 repository@digest、platform manifest、归档文件名、SHA256、大小、release digest 与 source commit 组成严格 source binding 写入 SDK。SDK 构建前必须退出 registry：不仅匿名解析 source OCI index，还要无凭据拉取实际 source platform、从 scratch 文件系统流式读取完整归档并重算 SHA256；只有 payload 可取得才允许重新登录并继续构建 SDK。匿名拉取 SDK 后必须逐字节比较 source binding、检查人类可读 offer，并重新验收许可证清单的 release digest、条目数量与 host-tools/Python/sysroot/vcpkg/Crossforge 根覆盖。发布门禁必须保证每个二进制组件可映射到长期可取得的准确源码。对外措辞只能表述为 “GTS-derived cross SDK built from Rocky Linux rebuild sources”，不得暗示 Red Hat 或 Rocky 官方支持、认证或背书；首次公开发布前仍需正式法律复核。

source bundle 不能只枚举构建过程中显式下载的 RPM。固定 amd64 Rocky child
本身含 148 个 base package；`rocky-base-source-map` 在该精确 child 的 RPMDB 中以
`--network=none` 读取 NEVRA→SOURCERPM，并与 host-runtime transaction 的完整 base
manifest 逐项比较后输出 scratch 证据。该基础闭包含 110 个唯一 SRPM，其中 54 个未在
其他锁中出现；当前 12 份 RPM lock 另含 1,433 个受签 binary payload、279 个唯一
SRPM 名称，合并后总需求为 333 个。`locks/rpm-source-el8.json` 已为全部需求固定
repository/URL、大小、SHA256、source header 和 Rocky 签名结论，总计
1,456,725,209 字节；BaseOS/AppStream/PowerTools 分别选择 229/99/5 项，七个跨
BaseOS/AppStream 的同名 URL 必须下载两份并证明字节一致。`rpm-source-lock-maintenance`
是唯一允许重新查询 source repositories 的维护目标，不进入 CI/candidate；正常门禁
`rpm-source-lock-validated` 只在断网阶段交叉验证 release pin、requirements、333 项、
别名优先级、Rocky trust 和总字节数。`rpm-source-bundle` 随后只按 lock 的 primary URL
下载全部 1.46 GB 字节，下载时核对大小/SHA256，再于 `--network=none` 阶段逐项重验
source header 与 Rocky 签名并输出 scratch-only `/source-rpms`。RPM 字节闭包因此已可实际
组装。最终 `source-bundle` 把它与六个 CPython、zstd、CMake/Ninja、完整 vcpkg Git
历史与 vcpkg-tool、nFPM、QEMU/binfmt、Qt/FFmpeg/xcb 资格化源码、23 份签名/校验/
Sigstore/公钥材料、10 份 source-stage metadata，以及与候选 commit 精确同文件集的
Crossforge 项目快照合并。断网 assembler 对 388 个输入逐项重算 SHA256/大小，输出
统一 `MANIFEST.json`，再生成可移植 checksum 的单一 `tar.zst`。candidate v2 同时绑定
SDK OCI、source OCI、两个 platform manifest 与内层 archive SHA256/大小；两套 OCI
必须匿名可读并在原生 ARM64 门禁之后一同 keyless-sign。实现门禁已经完成，但首次
成功公开 candidate 及正式法律评审之前仍不得宣称存在稳定 source release。

## 13. 维护与演进规则

安全修复、CPython patch、GTS patch、vcpkg commit 和 Rocky errata 更新必须通过自动差异报告与相应资格化；自动化可以开 PR，但不得自动合并。差异至少覆盖 RPM NEVRA、source hash、ABI exports、Python modules、vcpkg ports、镜像大小和 GCC baseline。

- patch release：安全修复、上游 patch、lock 更新，不改变公共接口；
- minor release：增删 Python minor、增加工具或扩展 `crosspack` schema；
- major release：GTS major、EL baseline、目录布局、canonical triple 或 vcpkg triplet 语义变化。

稳定标签只允许指向通过完整资格化的最新 release。

## 14. 依赖变体与生产分包契约

Phase 14 已证明现有 `crosspack` 能从显式 staging tree 生成字节可复现的双架构
DEB/RPM，拆分 debug symbols，审计动态 ELF，并用真实 `dpkg`/`rpm` 复核安装
payload。当前内部 schema 已提供格式特定 relations、精确 component edge、显式
mode/owner/group、`config`/`noreplace`、共享 project version 下格式特定的
epoch/release，并按最终 destination/RUNPATH 唯一解析 `DT_NEEDED` provider；
格式特定 lifecycle scriptlets 也已绑定摘要并经过真实安装、升级、卸载执行。
显式 independent component 已映射为 DEB `all`/RPM `noarch`，并以双 target 完整
component plan 和两代 package 字节一致性取得 `verified-independent` 证据。
`package plan/build` 也已支持选择 DEB、RPM 或两者，并把选择绑定进 canonical plan。
component 的单行 summary 与规范多段 description 已拆分，并经过真实包 metadata 查询。
单路径简写与显式 DEB/RPM destination 已同时支持；两套 libdir、debug destination、
symlink/冲突与 ELF provider closure 分开规划、审计和安装验证。sealed staging/variant
边界已要求外部 immutable manifest 绑定 config、target、variant、resolution 与完整
inventory，并在 plan/build 入口和每次编码前复核；上游 variant spec/resolution/assets
生成与 origin/link evidence 绑定仍须在首次公开发布前完成。
现有安装门禁也不构成目标发行版运行资格化。
不能先发布一个功能不足的 schema v1，再立即用 schema v2 修正基本生产语义。

### 14.1 职责边界

依赖和分包链固定为：

```text
variant spec
  → vcpkg resolution lock
  → sealed asset bundle
  → variant-isolated build/link evidence
  → sealed staging
  → crosspack plan
  → reproducible unsigned DEB/RPM
  → external signature/attestation
```

vcpkg 负责 port 版本、features、host/target dependency graph 与构建；triplet 表达
target、linkage 及全图 ABI 策略。`crosspack` 只消费最终 staging 和显式 provenance，
负责 component、文件/ELF/loader 审计、包 metadata 与格式编码编排。它不把整个
`vcpkg_installed` 直接转换成发行版包，不根据 SONAME 猜 DEB/RPM 包名，也不成为
CMake、Meson、Autotools 或其他项目构建系统的总编排器。

项目正常执行 build-system install 到干净 staging；私有携带的 vcpkg runtime DSO
再通过受控、可追溯的复制步骤加入。host tools、downloads、buildtrees、packages、
debug installed tree 和未选择的 variant 永远不能因便利而进入 staging。

### 14.2 两层变体身份

构建前生成严格、canonical 的 `variant-spec.json`。其摘要是公开 `variant_id`，并
至少绑定：

- target arch/triple、build configuration 与 static/dynamic linkage；
- Crossforge release、compiler、sysroot lock 与 CMake toolchain component 摘要；
- vcpkg commit、target/host triplet 名称及文件摘要；
- project manifest、`vcpkg-configuration.json`、builtin baseline、overrides 与显式
  selected features；
- overlay port/triplet 的完整树摘要；
- 会改变代码或 ABI 的 C/C++/link flags。

overlay 树摘要覆盖规范相对路径、文件类型、mode、普通文件摘要和 symlink target，
不包含物理 workspace 绝对路径。`CFLAGS`、`CXXFLAGS`、`CPPFLAGS`、`LDFLAGS`、
overlay、feature 与 toolchain override 等 ambient input 必须写入 spec 或被 launcher
拒绝；proxy、CA、cache 路径、并行度和 credential 不进入变体身份，且秘密不得进入
任何报告。

依赖解析后生成 `vcpkg-resolution.json`，记录全部 target/host ports 的精确 version、
port-version、features、triplet、vcpkg ABI、source/patch/license identity 与实际
runtime DSO。相同 `variant_id` 得到不同 resolution digest 时构建失败。vcpkg 内部
ABI hash 只作为 resolution/cache 证据，不能替代 Crossforge 的公开变体身份。

每个 variant 使用独立 build、installed、link-audit、staging 与 package root；target、
linkage、feature、baseline 或 toolchain identity 变化后不得复用旧 CMake build tree。
最终包名默认不包含 `variant_id`；完整 ID 写入 plan/result、SBOM、provenance 和包内
build-info。需要同时安装的 ABI 冲突变体必须使用不同 package name 或 SONAME，不能
依靠同名不同字节区分。

### 14.3 资格化维度与自定义 triplet

“支持”拆为三个独立状态：

- platform：`qualified`、`compatible-unqualified` 或 `external`；
- dependency graph：`contract-qualified`、`curated-tier1/2/3` 或
  `resolved-unqualified`；
- package：`crosspack-verified` 或未验证。

`qualified` platform 要求内置 triplet 的名称和摘要、Crossforge toolchain/sysroot、
CRT/linkage policy 与 release 完全相同。`crossforge-*` triplet namespace 由产品保留，
用户 overlay 不得用同名文件遮蔽官方 triplet。仍 chainload 官方 toolchain/sysroot、
保持 EL8 ABI/ISA 和动态 core runtime 的自定义 triplet 可标记为
`compatible-unqualified`；替换 compiler/sysroot/triple、提高 ISA/ABI 或静态替换 core
runtime 的变体是 `external`。

依赖图资格按精确 graph 而不是 port 名称判断。只有 port version、port-version、
features、target/host triplet、传递依赖、source/overlay 摘要与 vcpkg commit 全部匹配
时，才获得相应 curated profile。官方 triplet 上的其他 port、不同 feature、override
或 baseline 仍是 `resolved-unqualified`。这类 graph 可以被 Crosspack 结构化验证和
打包，但不得描述为 Crossforge 已资格化依赖图。公开接口提供机器可读的 reason code，
并允许组织策略要求特定 platform/dependency qualification；不存在把用户输入强制
标成 qualified 的 override。

### 14.4 Source asset 与 binary cache

registry/baseline 只锁依赖定义和内容期望，不保证上游 URL 长期可用。每个 resolution
必须产生 `vcpkg-assets.json`，记录所有 source archive、Git content、patch、license、
helper tool 和 host-generator asset 的清理后 URL、size、SHA256/SHA512、所属 port 与
portfile identity。实际资产组成 content-addressed、sealed dependency bundle；bundle
可存于本地或用户选择的私有/公开 OCI，不因使用 Crossforge 而自动公开可能受限的用户
源码。

网络只允许出现在 resolve/fetch 边界。正式重放从空 build/cache 开始，在
`--network=none` 下仅消费 sealed bundle；portfile 自行联网、无 hash 下载或缺少传递
asset 均失败。上游 URL 消失时，已封存 bundle 仍必须能重建。

binary cache 只是加速器。cache hit 后仍重验 variant、port/version/features、vcpkg
ABI、compiler/sysroot、archive 与 installed-tree 摘要、target ELF 和 license/SBOM。
不受信 PR 只能读 trusted cache 并写隔离的临时 namespace，不能污染 release cache；
trusted entry 绑定 builder、source closure、variant 与安装树 provenance。资格化至少
比较两个不同绝对 workspace 的空-cache离线构建和一个 trusted-cache命中构建；最终
installed tree、staging 与 packages 必须一致，物理路径不得泄漏。

### 14.5 Sealed staging 与同架构 host 泄漏

staging 经 `created → populating → sealed → packaged` 状态流转。通用 install wrapper
只做执行前后 inventory 与 provenance 捕获，不解释项目构建系统。sealed manifest
绑定 variant、resolution、规范路径、类型、mode、owner/group、symlink、文件摘要与
origin；`package plan` 和实际编码前都重新读取文件系统验证，seal 后任何变化都要求
重新 plan。debug 拆分继续在私有 staging 副本完成，原 staging 不变。

aarch64 包中的 amd64 ELF 可直接按 machine 拒绝；x86_64 target 与 amd64 host 同架构，
不能只靠 ELF header。每个 target ELF 必须一一关联到 target compiler 的 link evidence、
target vcpkg installed record，或显式签名的 external-prebuilt provenance。link evidence
证明使用 canonical cross compiler/sysroot，且没有 host include/library、host triplet 或
另一个 variant 输入。合法的 host-generated data 可以进入包，生成它的 host executable
不能因此进入 target component。

`.a`、linker script、`.pc` 与 CMake config 也属于审计对象：archive member 必须是
正确 target object/LTO input；linker script、pkg-config 与 imported target 不得引用
workspace、buildtrees、host prefix 或 Crossforge 内部 sysroot。release component 禁止
复制 vcpkg `debug/bin`、`debug/lib`；debug package 必须从实际发布 ELF 生成并绑定其
build-id/debuglink。

### 14.6 Dynamic deployment 与 loader closure

动态依赖有两个显式模型。普通应用默认 `application-private`：真实 executable 与
vcpkg DSO 位于 root-owned、非用户可写的 `/opt/<vendor>/<product>/{bin,lib}`，可由
`/usr/bin` symlink/wrapper 提供入口，并由同一 runtime package 原子升级。公共共享库
必须显式选择 `system-library`，使用发行版 libdir、SONAME/runtime package、development
symlink/header/pkg-config/CMake metadata 和 ABI 升级契约。

application-private 允许经语义验证的 `$ORIGIN/../lib`。Crosspack 不再以字符串出现
`..` 一概拒绝，而是从 ELF 最终 destination 规范化展开 RUNPATH，要求解析结果仍在同一
root-owned package prefix 内。绝对路径、空项、未规范路径、逃逸到 prefix/包集合外、
用户可写目录或未声明 component 仍失败。

loader closure 不能只检查某个 SONAME basename 是否在任意 package 路径出现。审计器
从每个 ELF 的 destination、RUNPATH、允许的系统搜索路径、package component edge 和
runtime provider catalog 实际解析每个 `DT_NEEDED`，要求 provider 唯一，并记录目标
destination 与字节摘要；同名多 provider、存在但不可达或跨未声明 component 都失败。

应用私有目录永远禁止携带 loader、glibc core、`libgcc_s.so.1` 与
`libstdc++.so.6` 等冻结 EL8 core provider，防止 RUNPATH 覆盖产品动态 runtime
契约。其他 vcpkg DSO 可在完整 provenance、SBOM、license 与递归 closure 下私有携带。
真正公开系统库才安装到 system libdir，并承担 SONAME、ldconfig、Provides/Conflicts/
Obsoletes/Replaces 与并行 major 安装语义。

### 14.7 Static/header 依赖与 SBOM

最终 ELF 无法在 LTO、inline、archive member selection、dead-code elimination 与 strip
之后可靠反推出所有 static/header dependency。package SBOM 因此保守包含 resolution
中的完整 target closure；host dependency 另标为 build-tool-only，不能混入 target
runtime。

compiler/link wrapper 可在不改变 ELF 字节的审计模式下记录 link map、archive member、
LTO input、shared input、输出 build-id 与规范路径；compiler depfile 可把观察到的 header
映射回 vcpkg port。Crosspack 将 resolution、link/header evidence 与最终 loader audit
合并，把关系标为 observed 或 conservative。更精细的证据只能提升置信度，不能因缺少
观察记录而从保守 package SBOM 静默删除依赖。

包内 component SBOM/build-info 记录 variant、resolution、source/license 与 static、
header、vendored-dynamic、external-system、build-tool 关系，但不自引用最终包摘要；包
编码完成后的外部 attestation 再绑定 DEB/RPM SHA256、component SBOM、Crosspack
plan/result 和 Crossforge SDK digest。CVE 影响反向索引从 port/version/source identity
映射到 variant、package 与 ELF：static、conservative static/header 和私有 dynamic
默认要求重建；只有审查后的 VEX 才能缩小影响。

### 14.8 公开 Crosspack schema v1

首次公开前 schema v1 至少支持：

- 格式特定 DEB `depends`、`pre-depends`、`recommends`、`suggests`、`conflicts`、
  `provides`、`replaces`、`breaks` 与 RPM `requires`、`recommends`、`suggests`、
  `conflicts`、`provides`、`obsoletes`；
- component 依赖的格式正确、精确 epoch/version/release 约束；
- `config`/`noreplace`、doc/license、空目录，以及显式 mode/owner/group；
- 格式特定 pre/post install 与 pre/post remove scriptlets；脚本来自独立普通文件，
  其摘要/interpreter 进入 plan；
- `target` 与显式 `independent` architecture；
- DEB/RPM 各自 epoch/release、system libdir 与 debug destination；
- 单行 summary、规范多段 description 与 SPDX/`LicenseRef-*`；
- `package plan` 与实际 build 分离，并可选择 `deb`、`rpm` 或两者。

setuid/setgid、device、file capability 默认拒绝；systemd user/service、alternatives、
SELinux policy、APT/YUM repository 与任意发行版 dependency inference 不属于 v0.1
公共抽象。nFPM 仍是固定、不可由用户替换的编码 backend；Crosspack 不接受任意 nFPM
passthrough 配置来绕过 schema 和审计。

unsigned package 是可复现主体。签名在独立阶段消费其摘要，私钥/token/password 不得
进入 manifest、BuildKit layer 或日志；signed result 记录 unsigned/signed SHA256 与
签名者身份。RPM 可接外部 key/agent；DEB 首发依赖 detached attestation，APT repository
签名仍不属于产品范围。签名后字节不要求与 unsigned package 相同，但同一 package
version/release 永远不得重新上传不同 unsigned 内容。

### 14.9 Component、变体共存与升级

普通 application-private 包默认选择一个正式 linkage，主程序与私有 DSO 同 runtime
component 原子升级。若确实同时发布 static/dynamic 应用变体，使用不同 package name
并声明冲突，不能让同名同 version/release 表示不同内容。公共 library 的动态 runtime、
development、static archive 与 debug component 可分别共存；DEB/RPM 可使用各自惯用
包名。只有影响公共 ABI、文件路径或必须并行安装的 feature 才进入 package name；内部
feature 只进入 variant/SBOM。

同一 package name 表示可原地升级。dependency-only 更新通常保持 project version、
增加格式特定 package release；static 与私有 dynamic 依赖变化要求消费者原子重建。
公共 DSO 升级须用未 strip DWARF、export/version、SONAME、public header、CMake/pkg-config
和旧消费者运行测试判断 ABI；无法证明兼容时状态为 unknown 并阻止自动晋级。ABI
breaking 必须改变 SONAME major 或 package name，老 runtime major 可并行保留。回退
依赖优先发布更高 release 的 revert，不能覆盖旧 package 字节。

每次 baseline、feature、triplet、source 或 toolchain 更新产生结构化 variant diff，
覆盖 graph、assets、license、staging、ELF、package 与 SBOM，并分类为 metadata-only、
rebuild-compatible、runtime-compatible、abi-breaking 或 unknown。自动化只能开 PR 和
生成证据，不能自行批准 ABI/baseline、合并或移动稳定标签。

### 14.10 Independent component

`independent` 映射为 DEB `all` 与 RPM `noarch`，必须显式声明，不能按“不是 ELF”自动
猜测。官方 `verified-independent` 要求 x86_64/aarch64 独立构建后的路径、普通文件
字节、mode、owner/group、symlink 与 origin 完全相同；不允许归一化差异后放行。只构建
一个 target 时最多标为 `declared-independent`。

independent 默认禁止 ELF/object/archive、linker script、debug symbol、native Python
extension、`_sysconfigdata`、`.pyc`、QML cache/预编译 shader、target symlink 和含
sysroot/build path 的配置。普通 development component 保持 target-specific；只有纯
data/script/license 或经双 target 验证的 header-only payload 才进入 all/noarch。
common component 使用独立 identity，不嵌入单个 target `variant_id`；同一 noarch/all
artifact 分别与两套 target package set 做安装、升级和卸载验证。

### 14.11 Package format 与 runtime qualification

包格式可编码/安装不等于目标发行版运行兼容。qualification report 分别记录 format 与
runtime 状态。v0.1 正式边界为：

| 格式/target | format | Crossforge runtime |
|---|---|---|
| RPM/x86_64 | qualified | Rocky Linux 8.10 |
| RPM/aarch64 | qualified | Rocky Linux 8.10；release 需原生 ARM |
| DEB/x86_64（`amd64`） | qualified | unqualified |
| DEB/aarch64（`arm64`） | qualified | unqualified |

现有 `dpkg --force-architecture` 与 `rpm --ignorearch --nodeps` 门禁只算 format/结构
证据。RPM runtime qualification 还必须在干净 Rocky root 中安装精确依赖、不以
`--nodeps` 掩盖 closure，并执行 install→run→upgrade→run→remove；aarch64 同时覆盖
QEMU clean-Rocky 与原生 ARM。Bookworm 只是 pinned `dpkg` 测试宿主，不能据此声明
Debian/Ubuntu 运行支持。

application-private 的 `/opt` destination 可跨格式共用；system-library 必须由受控
逻辑 destination 映射到 RPM `/usr/lib64` 和 Debian multiarch libdir，并使用各自 debug
布局。DEB 外部 dependency 由用户按目标发行版声明，不能从 Rocky RPM owner 自动翻译。
v0.1 不实现用户自定义 runtime profile：用户测试结果可以外部保存，但不能进入
Crossforge 官方 qualification；增加 Debian/Ubuntu 或其他发行版 provider catalog、
双架构执行与生命周期维护属于后续 minor 产品扩展。

## 15. 目标仓库结构

```text
config/                  release.json 与 JSON Schema
config/rpm/              DNF 求解输入 plans
evidence/                可离线重算的 OCI、SLSA 与 Git 原始证据
locks/transactions/      DNF 规范化 action、精确 reason 与 inventory manifests
locks/metadata/          已验签 repomd 与 detached signatures
locks/                   host/sysroot 实收 RPM content locks
keys/                    固定的 RPM 签名信任根
abi/el8/                 冻结 ABI 集合
docker/                  Dockerfile 与 test.Dockerfile
docker-bake.hcl          仓库根部的 Buildx Bake 入口
scripts/                 SRPM、binutils、GCC、Python 和镜像组装脚本
tools/crossforge/         launcher、JSON/ABI 工具与 crosspack
integration/             CMake、Meson、vcpkg 集成文件
tests/{smoke,gcc,python,qt6,vcpkg,packaging}/
```

实现采用纵向切片：独立 host runtime、最终镜像 runtime rebase、双 target compiler/hybrid runtime、冻结 ABI、CPython 3.9–3.14 双 target 行、CMake/Ninja host-tool overlay、vcpkg source lock、五 triplet SDK 集成、真实无下载契约、三层 curated ports、带 debug/ELF 深审计的双格式分包门禁、单一 launcher、完整 SDK 聚合、x86_64 GCC full qualification、digest-bound 原生 ARM release 工作流、Qt source/host/双 target 构建与运行时资格，以及无重建的不可变发布闭环均已实现；后续必须取得首份公开候选/原生 ARM/稳定晋升实证，完成 GitHub 保护设置和正式法律复核。旧 Rust 实现已按用户决定删除，由原型 tag 提供完整历史快照。

Qt 兼容性资格验证为本地或手动按需运行，不属于默认 CI、每日 SDK qualification、candidate 签名或 release 晋升门禁。发布仍要求候选 digest 绑定的原生 ARM 工具链探针。SDK 发布不声明通过 Qt 验证。
