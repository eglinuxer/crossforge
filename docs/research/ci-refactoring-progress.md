# CI 重构实施进度

目标是完成[六批实施方案](ci-refactoring-plan-2026-09-10.md)，不是仅完成触发器调整。工作分支为 `codex/component-ci-refactor`；研究基线为 `cf736eab8aa53b851874509d66676e5bac98dc27`。

| 批次 | 当前状态 | 仍需取得的证据 |
|---|---|---|
| 1：入口与前置回归 | 本地实现及 Docker 回归通过 | 修改后工作流的真实 GitHub 事件运行 |
| 2：组件契约与工具链交接 | 本地 OCI/registry 往返、双架构安装组件/工具链资格、x86_64 GCC full 与 cp39 消费通过；同 run CI 试点已实现 | GitHub 试点实跑与生产 CI 接入 |
| 3：组件级增量计划 | 真实材料选择和动态 CI 已实现；本地范围实验及 cp39 x86_64 受影响构建通过 | GitHub 实跑、缩小资格 COPY 范围、与可信产物可用性及生产消费对接 |
| 4：整行 Python/SDK 交接 | cp39 双架构正式行资格 receipt 及单行 SDK append 的组件消费通过，无 GCC/CPython 源码编译 | 其余五行及完整 SDK/生产 CI |
| 5：资格复用及恢复 | 工具链与 cp39 的本地显式复用、强制执行和原记录保留通过 | 跨 run 信任、其他资格领域、候选集成/原生 ARM 及同 digest 恢复 |
| 6：性能与领域重构 | 待实施对照 | 新基线、三次匹配输入重放和受影响变更、资源实验 |

## 批次 1 已实现

本地提交：`6e0007f`。

- main push 进入 `ci.yml` 的现有保守 profile 选择；候选仅显式 dispatch。
- 后续 main push 取消过时的同类 selected build，manual CI 和 candidate quick 使用独立并发组；正式候选不被取消。
- 真实 Git fixture 执行工作流内的选择命令，覆盖文档、Python、CLI、未知路径、删除、缺失 base 和 PR merge-base。
- source-bundle 与 sdk-candidate 使用不同 fixture digest 执行实际发布解析命令；缺失字段/target 与非法 digest 必须失败。
- README 与 Actions 操作说明同步。

2026-09-10 Docker 验证：固定 Debian 12 image ID `sha256:1a36c51e0ec9f314f79f010e7546ca6bee1ef6b256fa1bef64b26888e662ed42`，4 CPU、16 GiB，断网且只读仓库，Buildx CLI 0.36.1 只读挂载，无 daemon socket。

- CI 相关 33 项、候选相关 32 项独立回归通过。
- 全量 config 920 项，183.004 秒，零失败，2 项真实 zstd 资源测试跳过。
- packaging 40 项，0.779 秒，零失败，1 项 nFPM 测试跳过。
- locked release、supply chain、frozen ABI、Python runtime providers 及三个 renderer `--check` 通过。
- 校验 SHA256 的 actionlint 1.7.12 在 Docker 内通过；只保留仓库既有的 concurrency.queue 解析兼容例外。

全量日志在本次工作机 `/tmp/crossforge-batch1-docker-tests.log`。输出中的 `compiler-failed` 和匿名 cache 失败提示来自预期失败 fixture；整套测试退出码为 0。

尚未改变粗粒度 `sdk`/`python` 阶段范围，尚未实现资格结果复用；当前普通缓存仍不构成发布证据。全目标保持进行中。

## 批次 2 已完成的基础工作

新增 `scripts/crossforge_internal/`，包初始化不导入领域策略。`identity.py` 提供严格 JSON、内容摘要与常规源文件身份；`component_inputs.py` 和对应 schema 分离组件输入身份，绑定文件字节/权限、参数、目标及上游输入身份与实际产物 digest。源提交保留给后续 provenance，不直接充当输入 key。

消费者必须与独立生成的完整预期材料集合比较。当前模块只处理显式声明的文件集合，**没有声称已经完成材料闭包发现、产物验证或可信 producer 验证**；这些是下一步接入正式交接的必要工作。原有自包含 component reader 和各 Docker stage 的领域隔离未被整体替换。

验证结果：15 项新增身份回归通过；70 项已有 component/renderer/领域隔离回归通过。实际 Rocky 8 `platform-python-check` 完成递归编译、内部模块导入、材料捕获/复核与摘要计算，并通过 89 个组件投影的 drift check。CI 与 Docker 的语法检查已覆盖嵌套子包。

接下来实现产物清单和 OCI 验证，随后连接真实 producer/consumer、完整输入闭包与 GCC 测试上下文。组件级增量选择与完整 Python/SDK 交接尚未实现。

## 批次 2：正式本地 OCI 接口

新增 [内部组件操作说明](../internal-components.md)。`component-artifact.py` 提供执行环境观察、独立输入捕获、工具链计划/构建及本地 consumer 核验入口。`component_artifacts.py` 管理安装产物、GCC 测试上下文等不同角色的嵌入契约和外部 receipt；receipt 不宣称资格化通过。原始 producer、提交、dirty 状态和 invocation 保留，消费者必须独立取得可信 receipt 摘要及当前完整预期材料。

`bake_materials.py` 对已解析 Bake 图做保守材料盘点。它遍历可达 Docker stage、COPY 与命名 context，绑定相关 recipe、锁定镜像、参数、文件/目录权限及 BuildKit 环境。它支持当前仓库所需的受限语法，遇到未知来源语法、未固定镜像、stage/context 重名或缓存/secret/SSH mount 会失败，不将其当成空依赖。当前所有显式 Bake 参数仍进入身份，目录 COPY 也暂未过滤 ignore 文件；这是保守失效范围，后续组件计划还要收窄。

OCI 模块只校验 root/platform/config/layer 的实际字节，镜像层应用与元数据读取交给固定 frontend 的 BuildKit COPY-only 图。正式 producer 对解析图移除 registry 输出、tags 和 cache exports，只创建新的本地 OCI 目录，并在构建后重新核验源材料。独立 consumer 验证后只输出固定 platform digest 的 context，不提供 latest 或源码回退。

2026-09-10 本地验证：

- 54 项组件/材料回归通过，覆盖错误 digest、角色/target/recipe/sysroot 错配、缺失/损坏/权限改变的元数据、原 producer 被改写、材料漏项、目录新增与权限变化、未知源语法及发布输出清除。
- 92 项已有组件、领域隔离和 Bake renderer 回归通过；三个 renderer `--check` 通过。
- 实际 Rocky 8 platform-python 3.6 完成递归编译、新 CLI 加载/参数解析、输入捕获及 89 个投影 drift check。远程缓存导入在断网工具容器中失败，但本次检查依靠已存在的本地输入完成，退出码为 0；没有将 cache importer 警告计为测试失败或远程缓存成功。
- 旧可行性实验镜像的 751,640,605 字节压缩 blob 全量校验通过；BuildKit 提取的三个原有材料/报告文件摘要与旧记录一致。这不将旧实验升级为正式资格结果。
- 正式最终工具链安装产物生成成功：platform digest `sha256:c863f454ac7466c0bbe839453463509d420e9875b983d55ba3f5f0038f22d0ee`；input SHA256 `1a132d98d0e98de8f02defc1688865273602c0225734ce5e49fa8f52948298ff`；receipt canonical SHA256 `0ac31bf839c3eeefe99aaf9f9c38faaaa120b640f462f6804ff83593b64ea7f4`。
- 全新 `crossforge-component-formal-consumer` 在独立重新捕获预期材料后，通过固定 receipt 与 OCI 验证，耗时 11.74 秒；cp39 x86_64 构建通过，耗时 132.96 秒。consumer 图只有 7 个目标，日志不含 `build-gcc.sh`，没有 GCC/binutils/toolchain 源码目标。其他依赖允许只读远程缓存，所以这不是全冷构建对照；也不是完整 Python row 或 GCC full 资格化。

本次发现快照目录权限会干扰缓存：初始新快照目录为 `0775`，旧快照为 `0755`，对应锁定 metadata 目录 COPY 未命中，继而重新编译 GCC。最终快照统一目录 `0755` 后 GCC 步骤命中缓存，OCI layers 导出 36.5 秒。这是本地实验的输入差异，不作为历史 GitHub 缓存失效原因或优化后的端到端耗时结论。

原始日志、OCI、receipt 和不可变源快照位于本次工作机 `/tmp/crossforge-component-formal/`，不入库。GitHub 事件实跑、远程可信传输、GCC full 上下文与报告交接、完整双架构 Python row、SDK 汇总和资格复用仍未完成，全目标保持进行中。

## 批次 2：工具链资格门禁消费组件

将工具链资格阶段、干净运行时门禁、dev 汇总以及 GCC smoke/full 的输入改为命名的安装/测试上下文入口。默认 Bake 仍提供源码导出的组件；renderer 仅向可达消费者添加上下文，导出节点不依赖自己。`gcc-<arch>-test-context-export` 现在明确导出 prepared source 与 GCC build tree，正式本地 producer 使用该边界。测试命令、目标执行规则与冻结基线保持原样；SDK 仍要求资格化的 dev 汇总，不直接绕过门禁消费 build export。

实测数据见 [门禁交接观测](toolchain-gate-handoff-2026-09-10.json)，原始 OCI、源快照、报告与日志位于本次工作机 `/tmp/crossforge-toolchain-gates/`。

- 安装组件本地 producer 7.51 秒，压缩层共 746,948,386 字节；首次 GCC 测试上下文 producer 90.75 秒，压缩层共 1,310,689,927 字节。这两种产物保持分离，普通 Python 消费者无需接收 GCC build tree。
- consumer 独立重算材料并核验两份 receipt，耗时 27.52 秒。随后通过固定 OCI digest 执行工具链 ABI/locked-sysroot/clean-Rocky smoke 与 GCC smoke，48.56 秒完成。
- 使用按 stage 的 `no-cache-filter` 重跑四个实际门禁步骤；原始日志证明它们均执行完成且没有 `CACHED`。consumer Bake 图仅包含两个资格根及报告导出目标，没有 GCC 源码构建目标，日志没有 `build-gcc.sh`。
- 用现有最终 SDK 的工具链报告验证函数重新检查 release、sysroot、版本、来源、资格组件与 clean-runtime marker；用现有 GCC normalizer 对原始 `.sum` 及冻结 baseline 重新生成报告并逐字段比较，得到 16 PASS，报告一致。
- Docker 完整 config 回归 976 项、182.946 秒、零失败、2 项既有 zstd 资源测试跳过；locked release、供应链、冻结 ABI、Python provider 及三个 renderer `--check` 通过。

这里证明了安装组件和原始测试上下文可共同运行既有门禁；尚未证明 GCC full 或 ARM 实跑完成。当前报告与组件 digest 的关联仍是本地观测，不能作为可复用的正式候选资格 receipt。后续继续完成该接口、远程可信交接和 CI 接入。


## 批次 2：正式本地资格 receipt 与输入模型 2

`component_qualification.py` 将资格生产和已有报告消费分开。生产端独立重算安装/测试上下文材料、核验可信 receipt 和 OCI，随后按固定 digest 替换命名 context。资格材料闭包在这些产物处停止，绑定其构建输入 SHA256 与实际 platform digest，同时绑定测试实现、策略/baseline、报告校验器和执行环境。

`qualification_execution.py` 观察 Docker/BuildKit、kernel、CPU 特征及 builder 容器资源/安全配置，前后比较环境；生产端按 stage 强制执行，保存 BuildKit raw JSON，并要求每个资格 RUN 都在本次时间范围内成功完成且没有 cache hit。完成报告验证后才生成只含契约、报告和原始日志的 scratch OCI。消费者重新提取、校验字节和报告语义，返回 `verified-prior-execution`，保留原 producer、运行区间和记录，不能把它改写成本次重新执行。

现有 SDK 的工具链报告验证提取到只依赖标准库的 `toolchain_report.py`，SDK 入口保留兼容 wrapper，并补上 x86_64 locked-sysroot 失败状态拒绝；GCC 沿用原 normalizer 的精确 status/suite/test/occurrence 基线比较。规范化 JSON 比较区分布尔与整数，额外检查已执行 suite、site/board、资格组件和 make 日志摘要。

材料模型升级为 2：交接 CLI/基础库不再自动成为编译输入，实际 COPY 的文件仍被绑定；收窄到相关 ARG、继承的全局默认值及隐式 frontend/proxy 参数。资格校验器仍是资格输入。旧 model 1 receipt 不能冒充新身份。此改动还没有把 main 的粗 profile 选择器替换为组件级计划。

实测见 [正式资格交接记录](qualification-handoff-2026-09-10.json)，原始数据在本次工作机 `/tmp/crossforge-qualification-receipt/`：

- 输入模型 2 的安装 OCI、GCC 测试上下文 OCI 生成并通过独立核验。
- 最终实现的 x86_64 工具链 ABI/locked/clean 门禁生产和封装 13.20 秒，独立消费核验 1.64 秒；GCC smoke 生产和封装 18.15 秒，独立核验 2.71 秒，16 PASS。
- 两个 profile 各有两个实际执行的 RUN；资格材料图和实际事件均不含 GCC 源码编译。这些数字来自已有组件导入与前置缓存的本地 builder，不是 GitHub 全冷或端到端性能承诺。
- Docker 完整 config 回归 997 项、186.214 秒、零失败、2 项既有 zstd 资源测试跳过；随后资格模块 11 项回归通过，包含全量之后新增的 3 个凭据拒绝/保留原记录场景。
- 四项 locked validators 和三个 renderer `--check` 通过；实际 Rocky 8 platform-python 3.6 递归编译、CLI 导入、输入接口和 89 个投影检查通过。

GCC full 正在通过同一正式接口重跑，尚无完成结论。ARM、本地完整 Python row/SDK、远程 registry/trust/retention、CI 动态计划和候选接入仍待完成。通用 receipt 的 `qualification` 角色本身不是资格证明；只有专用校验接口会验证完整执行和覆盖，且本地可信 digest 的来源仍由调用方负责。全目标保持进行中。


## 批次 2：registry 往返与同 run CI 试点

新增 `component-registry.py` 和领域模块 `registry_transfer.py`，使用上游 ORAS 复制已经封装好的 OCI，而不是再次调用 Docker exporter。CI 工具策略单独固定 ORAS archive、可执行文件 SHA256 和源提交；安装时只读取经过双重摘要核验的常规 binary，传输前再次核验 executable。上传前验证可信 receipt、预期材料和原 OCI，上传后检查远程 manifest 原始字节；下载必须使用与可信 receipt 一致的固定 digest、新输出目录，并检查所有选中平台 blob 和元数据。

本地 Docker registry 往返已完成，见 [registry 交接记录](registry-handoff-pilot-2026-09-10.json)：

- 安装产物、GCC 测试上下文、GCC smoke 资格包的 root/platform/config digest 均保持原值；下载后的元数据与原 receipt 一致。
- 重新捕获资格输入、核验下载后的资格包，通过 `verified-prior-execution` 保留原 local producer 和运行区间，GCC smoke 仍为 16 PASS。
- 下载的安装组件进入 cp39 x86_64 构建，完整消费过程 77.65 秒，实际 cross-Python RUN 42.7 秒；消费图只有 7 个目标，不含 GCC 源码目标，日志无 `build-gcc.sh`。
- 上传/验证安装、上下文和资格包分别为 3.10/2.53/1.10 秒，下载及核验分别 1.67/2.63/0.48 秒。它们是同机 loopback、tmpfs registry 的观测，不代表 GHCR 网络或 GitHub 总耗时。
- 测试 registry 未向主机发布端口，完成后已停止并删除；OCI、日志和下载结果留在本次工作机 `/tmp/crossforge-registry-pilot/`。

新增 `.github/workflows/component-pilot.yml`，手动 main-only，分开 producer 与 consumer job。producer 向独立内部组件 package 写入，consumer 只有 packages:read。上游 job output 独立提供 canonical handoff SHA256，Actions artifact ID 固定交接文件；`component_handoff.py` 拒绝跨提交、run、attempt、错角色、混合 producer、错 registry、错误/缺失 receipt 和环境错配。consumer 独立重算组件材料，再执行现有 x86_64 工具链/运行时和 GCC smoke 门禁，并构建 cp39 x86_64。最终状态要求全部所选 job 成功，失败、取消或跳过都不能变绿。

该试点尚未在 GitHub dispatch，尚未替换正式 `verify-builds.yml` 或 main 的 profile selector。它不提供跨 run 签名目录、永久候选/发布证据保留或失败恢复；当前对跨 attempt 的部分重试明确拒绝。registry 中以完整 root digest 派生的 tag 保持产物可达，本批未实现删除或 GC。

验证：13 项新增 transport/handoff/workflow 回归通过；Docker 全量 config 1013 项、190.302 秒、零失败、2 项既有 zstd 资源测试跳过；四项 locked validators、三个 renderer `--check` 通过；固定 actionlint 1.7.12 全工作流检查通过（仅既有 `concurrency.queue` 兼容例外）；实际 Rocky 8 platform-python 3.6 完成递归编译、三个组件 CLI 导入/参数解析及 89 个投影检查。

GCC full 的既有进程仍在执行；本批不会用 smoke、静态图或传输成功代替它。后续继续 ARM/完整 Python row、组件增量计划、生产 CI/SDK 接入和可信跨 run 复用，全目标保持进行中。


## GCC full 本地正式交接已通过

上述持续运行的 GCC full 已完成，producer 与封装总计 2469.93 秒，独立消费核验 6.16 秒。两条要求重跑的 RUN 均有本次完成且未命中缓存的结构化事件；资格材料图不含 `build-gcc.sh`。原始 GCC full report、所有 suite `.sum`、`.log`、`.make.log` 与执行事件已封装到本地资格 OCI，记录见 [正式资格交接数据](qualification-handoff-2026-09-10.json) 的 `gcc-full`。

- 454,050 PASS、87 FAIL、5,902 UNSUPPORTED、3,367 XFAIL。
- 87 条 FAIL 对应现有冻结基线的已知记录；原 normalizer 按 status、suite、identity、occurrence 精确核对，未修改 baseline 或放宽新增/消失记录规则。
- platform/root digest：`sha256:4fcf228a6842d4b39f6553bf20864ae1980ec67aa10e7dad8ebbaa627a950c5b`。
- 资格 receipt canonical SHA256：`1f0ccf936e783a8f82f37ce5ee31d311a91c0e503b03efed1543b18f0f8059d9`。

这完成了 x86_64 安装组件与原始 GCC 测试上下文的 full 资格实跑及正式本地 report 交接。它是本地固定环境的结果，不代替 GitHub runner 资格或候选的最终集成/native ARM。ARM、完整 Python row/SDK、生产 CI、增量计划和跨 run 信任/恢复等剩余范围保持不变。


## 批次 3：真实材料计划与动态日常 CI

`incremental_plan.py` 复用组件材料盘点，比较两个不可变 Git 源快照的可达 Docker recipe、COPY 文件/权限、参数和固定 context。每个快照先执行三个 renderer `--check`；未知路径、缺失 base、材料语法不支持或根目标集合变化会明确回退到完整 SDK/GCC 范围。Git archive 仅接收常规文件和目录，不跟随 symlink，不改动当前工作区。

日常 `ci.yml` 接入 `verify-incremental.yml`，原 `verify-builds.yml` 保留资格化/手动 profile。`ci_execution.py` 从同一份紧凑计划生成全部条件和矩阵，最终重新核对完整 job 集合、矩阵与标志；未选中任务可以跳过，选中任务的失败、取消或意外 skip 必须使 required status 失败。每个执行器将所选 root 与该 stage 的实际 Bake 图核对，然后只执行选中的 root；原始 source DAG 及全部实际依赖仍由 Bake 解析。日常矩阵并行度仍为 2，没有提前更改资源实验变量。

真实项目实验见 [增量选择观测](incremental-plan-2026-09-10.json)，原始快照、计划和日志位于本机 `/tmp/crossforge-incremental-plan/`：

- GCC full baseline 文件字节变化选中 GCC full/smoke 两个 evidence 根，编译材料没有变化。smoke 也被选中，因为它真实复制整个 baseline 目录；没有伪造更小的依赖范围。
- 共享 cross-Python 脚本变化选中 12 个 cross build 输入及 Python/SDK 下游，不改变两套 GCC 工具链输入。
- candidate supply policy 修改并按顺序再生成后，只选中输入/供应链验证根，不触发编译输入变化。
- cp39 patch 修改、更新 release 中对应 SHA 并重新生成后，只有 cp39 native/x86_64/aarch64 三个编译输入变化；其他资格阶段仍因 COPY 完整 release 文档失效。这是必须在后续资格接口/整行交接中消除的真实耦合。
- 修改后的 cp39 x86_64 在 Docker/Bake 实际构建通过：119.15 秒，cross-Python RUN 实际 40.6 秒。它独立重算材料并核验原安装 receipt 后消费相同工具链 digest；输入身份仍为 `2de95e2c4b46c7d3f636f842ba186ef7c3ded344b7ae9dff269d0749c568ce4d`。消费图 7 个目标，日志无 GCC 源码编译。本次正确使用 `no-cache-filter=cpython-cross`。

这里已经实现真实 source selection 与动态任务集合，但尚未将生产工作流切到可信组件消费。普通缓存缺失时，Bake 仍会构建相应源码依赖；计划不授予 artifact/qualification 复用权限。GCC/CPython 安装产物、资格报告和供应链绑定最终彻底分离，以及产物缺失时调度 producer，仍需与批次 4/5 一起完成。新的 GitHub workflow 尚未实跑，不能据此声称端到端耗时已经下降。


本批验证：Docker 完整 config 1033 项、185.921 秒、零失败（2 项既有 zstd 跳过），packaging 40 项、0.782 秒、零失败（1 项既有 nFPM 跳过）。完整回归之后进一步收紧 full 模式：必须执行 stage 内全部 canonical 根目标，不能只保持全部 stage 名称却遗漏根；新增回归和最终 CI 41 项、计划 13 项复核通过。四项 locked validators、三个 renderer `--check`、固定 actionlint 1.7.12 均通过；真实 Rocky 8 platform-python 3.6 完成递归编译、CLI 导入与 89 个投影检查。实际 Git base/head archive→renderer→Bake→完整计划 CLI 也已通过。没有修改 release pins、冻结 ABI 或 GCC baseline；实验改动只在 `/tmp` 隔离快照中。


## 双架构工具链交接与独立资格策略

ARM 安装组件及 GCC 测试上下文通过正式 producer 构建与封装，随后在 consumer builder 通过原工具链 ABI/locked-sysroot/clean-Rocky 门禁及 GCC smoke。记录见 [工具链策略与 ARM 交接观测](toolchain-policy-2026-09-10.json)。安装 producer 共 487.73 秒，测试上下文 103.51 秒；ARM GCC smoke 在 locked-sysroot 与 clean-Rocky 两层各得到 16 PASS，封装 51.09 秒、独立核验 0.42 秒。这些是 amd64 上显式固定 QEMU 的资格结果，不是原生 ARM 发布证据。

针对上批 cp39 实验发现的完整 release 耦合，新增标准库领域模块 `toolchain_policy.py`。工具链 smoke/runtime 阶段从可信 qualification SHA256 校验五份已有投影及其摘要依赖，读取 target/sysroot、GCC/binutils 来源与版本、ABI 和运行时策略。Docker stage 使用 `--components`，不再复制完整 release 或 release graph 实现。新 schema 2 报告只声明 scoped policy binding；旧 `--release` 路径继续保持完整 release binding，不能将原报告改写成新 release 的执行结果。

SDK 与 vcpkg SDK 从完整 release 独立推导策略并核验原报告；vcpkg 契约使用同一个报告验证器，并核对报告字节与其已资格化 SDK 记录一致。缺少/失败的运行时门禁、错误组件/策略/来源/sysroot、schema 降级、混合 scoped 与完整 release 声明、缺失或不安全的 clean marker 均被拒绝。

- 新接口实跑 x86_64 工具链门禁 17.04 秒、独立核验 0.45 秒；ARM 门禁 14.52 秒、独立核验 0.46 秒。各有 2/5 条本次成功且非缓存的资格 RUN，安装输入仍匹配之前的可信 receipt。资格材料不含完整 `config/release.json` 或 `build-gcc.sh`。
- 隔离源中修改 cp39 patch、同步 release 中的 SHA 并按顺序再生成后，真实计划不再选中两套 toolchain dev 根；编译材料仍只有 cp39 native/x86_64/aarch64 三项变化。GCC/Python 等其他资格根仍有完整 release 依赖，本批没有宣称它们也已独立。
- 从修改后的源重新捕获资格材料并核验原 OCI，两套资格输入 SHA256 都保持不变，分别用 1.95/2.05 秒完成主体绑定、材料核对与 prior-execution 消费，原 producer/执行区间/报告保持原值。这不是再次执行测试的声明。
- 最终 SDK 与 vcpkg SDK 的报告消费函数接受上述实际报告及无关 release 变更；vcpkg 契约兼容性由回归验证。本批尚未重新运行完整 vcpkg/最终 SDK 产品集成。

原始源快照、OCI、receipt 与日志保留在本机 `/tmp/crossforge-arm-components/` 和 `/tmp/crossforge-toolchain-policy/`，大文件不入库。这里的时长来自已有组件与前置缓存，不作为 GitHub 端到端加速比例。生产 CI 的可信组件消费、完整 Python row/SDK 交接、跨 run 信任与失败恢复、并行度实测等剩余目标继续推进。

本批最终验证：Docker 全量 config 1044 项、186.021 秒，packaging 40 项、0.770 秒，均零失败（分别 2 项既有 zstd 与 1 项既有 nFPM 跳过）；四项 locked validators 和三个 renderer `--check` 通过。真实 Rocky 8 platform-python 3.6 门禁强制执行，完成递归编译、组件 CLI 导入与 89 个投影检查。未修改任何版本 pin、冻结 ABI 或 GCC baseline，也未发布镜像或触发远程工作流。


## 批次 4：完整 cp39 行与 SDK append 的组件消费

新增 `python_components.py` 和 `python-inputs`、`produce-python`、`bind-python-row` CLI，使用同一份实际 Bake 材料模型及严格 OCI receipt 校验。build Python、两套 target 安装目录、两份 target-artifact guard 日志与 source manifest 共五份构建组件分别封装。安装产物不携带编译树、工具链或测试扩展；构建审计记录单独交接，不能充当资格通过证明。

`cpython-qualify-build` 从锁定 Python build host 出发，显式复制同一行 build Python、当前 target 安装产物、构建审计记录及工具链。原静态 ELF/ABI/ownership 检查和 target-execution guard 验证保持；两个运行时 tier 及 row finalizer 保持原资格契约。默认 Bake 仍由源码 export 提供 context；组件 CLI 核验后替换为固定 OCI digest。材料模块新增按 Bake target 限定的 context 身份，避免双架构使用同名 `crossforge_toolchain` 时混淆主体；既有非 scoped 输入身份保持兼容。

实跑与报告见 [Python 组件交接观测](python-component-handoff-2026-09-10.json)，源快照、五份构建 receipt、OCI、日志与完整行报告位于本机 `/tmp/crossforge-python-components/`：

- 五份正式 build receipt 均完成生产与核验；build Python producer 37.05 秒，x86_64 安装/审计 34.72/28.71 秒，ARM 安装/审计 74.93/29.66 秒。它们包含已有缓存、组件核验及导出成本，不是冷编译基线。
- 独立 consumer 核验上述五份产物及既有两套工具链后，完成 cp39 双架构静态资格、locked-sysroot/clean-Rocky 执行和整行组装。总计 166.45 秒；7 条要求重放的 RUN 都成功完成且未命中缓存，两份 schema 4 报告及四个 runtime tier 全部 passed。
- 整行消费图只有 9 个目标，材料和实际执行均不含 GCC/CPython 源码编译；导出行 OCI digest 为 `sha256:4d44e45677eedb2b9ba8c0571910eed8a1af25343b35fcf99d010dfae2f6ce06`，压缩层 169,238,411 字节。
- SDK 消费前独立重算相同 row inputs，重验 OCI root/platform/config digest 与两份工具链 receipt，再执行 `python-sdk-append`。68.01 秒完成，消费图只有 5 个目标；SDK 内重新生成 row manifest，并与原 manifest 逐字节一致。该步骤没有 GCC/CPython 源码编译节点，是单行 append 验证，尚非六行最终 SDK 集成。
- Docker 完整 config 1049 项、186.211 秒，packaging 40 项、0.790 秒，零失败；分别有 2 项既有 zstd 和 1 项既有 nFPM 跳过。四项 locked validators、三个 renderer `--check` 和实际 Rocky 8 platform-python 3.6 门禁通过。

本批五份构建组件具有通用 receipt；完整行资格仍是本地执行观测，尚未封装成可复用的正式资格 receipt。Python 报告仍绑定完整 release 与共享 aggregate，剩余五行、正式 row 资格交接、完整 SDK、生产 CI 接入和跨 run 信任/恢复继续推进。没有更改版本、ABI 或 GCC baseline，也没有发布任何镜像。

## 批次 4/5：正式 Python 行资格 receipt 与显式复用

`python_qualification.py` 新增 `python-qualification-inputs`、`produce-python-qualification` 和 `verify-python-qualification` CLI。输入身份从真实消费图捕获，要求同一行五份 Python 构建组件及两份工具链，共七个明确主体；额外绑定行资格验收代码、运行日志检查器，以及编排容器的 Python/readelf 版本和二进制 SHA256。producer 对两个静态资格阶段、两个运行时阶段和 row finalizer 设置精确的 `no-cache-filter`，再按实际 Docker recipe 计算并核验 RUN 数量。

封装的 OCI 同时包含行安装目录、原始报告、严格组件契约、执行日志和资格记录。consumer 需要独立取得可信 receipt SHA256，并重新捕获预期输入、逐项核验上游产物和 OCI 字节。验收调用现有 `finalize-python-row.py`，从实际安装文件重算三套 SDK 树、ELF/ABI、source、zstd 和双架构运行证据，再与原 row manifest 逐字节比较；没有另写宽松的“passed”判断。复用输出明确标记 `verified-prior-execution`，保留原 producer、时间和覆盖记录，不重新标记为本次执行。

[本地实跑记录](python-qualification-receipt-2026-09-10.json)与本机 `/tmp/crossforge-python-qualification/` 保存对应原始输入、receipt、OCI 和日志：

- cp39 资格执行及封装 173.39 秒。7 条规定 RUN 全部新执行；两个架构的 locked-sysroot/clean-Rocky tier 均通过。行产物为 `sha256:3df5fe417de748f6beeaf295a0c6cd2c8944451c988ac149dcba0fa05925b549`，receipt SHA256 为 `ea12499fb426bfa383d9d792eabfbb583c22eed34a7d637ddbd2e62eced12c8b`。
- 独立 CLI 核验 22.51 秒；资格记录与原始 producer、执行区间完全一致。其间进行安装文件/报告检查，没有重新执行 target runtime probes。
- SDK 消费前再次重算和核验完整行 receipt，然后实际执行两条 `python-sdk-append` RUN，重算 manifest 并逐字节比较通过。消费图只有 5 个目标，无 GCC/CPython 源码编译。新 BuildKit vertex 的观测区间为 50 秒，按秒精度记录，不包含此前 receipt 核验耗时。临时观测脚本最初误填了 3 条 RUN；按原 recipe 的 2 条重新核验原日志后通过，没有重跑成功的 SDK 构建。
- 在隔离目录中给 ARM 安装树增加一个文件并保留原报告，真实 finalizer 拒绝 `aarch64 target SDK tree differs from qualification`；原产物未改变。
- Docker 全量 config 1057 项、188.846 秒，packaging 40 项、0.779 秒，零失败；分别保留既有的 2/1 项跳过。四项 locked validators、三个 renderer 检查及强制执行的 Rocky 8 platform-python 3.6 门禁通过。

这些结果建立了本地 cp39 的正式资格交接与复用。Python 资格仍依赖完整 release/shared aggregate，其余五行、完整 SDK、生产 CI 的跨 run 信任/保留/恢复和最终候选原生 ARM 验证仍未完成。本地缓存及组件已存在，且部分检查并行运行，因此不把这些数字作为冷构建或 GitHub 性能结论。
