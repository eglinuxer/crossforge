# CI 重构实施进度

目标是完成[六批实施方案](ci-refactoring-plan-2026-09-10.md)，不是仅完成触发器调整。工作分支为 `codex/component-ci-refactor`；研究基线为 `cf736eab8aa53b851874509d66676e5bac98dc27`。用户已授权在全部所需验证通过后合入 `main` 并推送远程；当前仍在实施和验收，不提前合并。

| 批次 | 当前状态 | 仍需取得的证据 |
|---|---|---|
| 1：入口与前置回归 | 本地实现及 Docker 回归通过 | 修改后工作流的真实 GitHub 事件运行 |
| 2：组件契约与工具链交接 | 本地 OCI/registry 往返、双架构安装组件/工具链资格、x86_64 GCC full 与 cp39 消费通过；main 缺失工具链集中生产及只读消费已接线 | GitHub 实跑、真实签名与跨 run 验收 |
| 3：组件级增量计划 | 真实材料选择和动态 CI 已实现；main 原始工具链/Python 组件集中准备及只读消费已接线；Python 共享运行时根、目标 compile/runtime/final 与行汇总已消费相应组件输入 | GitHub 实跑、其他领域资格 COPY 范围、正式 Python 行资格复用接入 |
| 4：整行 Python/SDK 交接 | 此前六行正式资格、Python SDK 与完整 SDK 本地实跑通过；新报告链、正式行 CI 生产/签名与目录驱动的完整 SDK 消费路径通过 Docker 契约回归，原始 Python 组件 CI 消费已接线 | 新报告链 Docker 实跑、正式行资格及 SDK 目录消费的动态 matrix 接入与远程验收 |
| 5：资格复用及恢复 | 工具链与 Python 行本地显式复用通过；签名目录、持久存储、候选组件消费及分阶段恢复、raw producer 部分重试接线本地验证通过 | 真实 GitHub 跨 run 信任和 producer 重试、其他资格领域、候选集成/原生 ARM |
| 6：性能与领域重构 | 本地慢测试画像及不可变 fixture 对照完成，全量 config 约 192→141 秒 | GitHub 新基线、三次匹配输入重放和受影响变更、资源实验 |

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

## 批次 4：矩阵扩展与完整 SDK 入口

扩展 cp314 时发现，私有 zstd 构建会从另一条 context 回到工具链源码图。`python_components.bind_build` 现在依据实现中的行契约，校验 zstd 目标并将其接到同一份已核验的 target 工具链；其他架构的 zstd 节点保持独立。真实 cp314 五份构建组件和完整行资格通过，source inventory 确认两个 cross 构建没有 GCC 源码输入，资格仍包含 zstd 静态链接和双运行时检查。cp313、cp312 也已完成五份组件及整行资格，cp311/cp310 继续实跑。当前记录见[矩阵交接观测](python-matrix-handoff-2026-09-10.json)。

`python_sdk.py` 提供 `bind-python-sdk` 和 `execute-python-sdk`，支持 `python-dev` 与 `sdk-complete-dev`。它要求全部六行、固定追加顺序和相同的工具链 receipt；对每行重新捕获输入并核验完整资格 artifact，然后替换对应 append context。材料捕获拒绝 GCC/CPython 源码编译，保留现有最终集成、vcpkg SDK、打包和消费者门禁；vcpkg upstream Tier 3 仍是独立候选门禁。六行 Python 消费图为 12 个目标，执行要求 12 条 append RUN 和 1 条 final RUN；完整 SDK 再要求其最终 RUN。绑定命令明确不声明集成已执行，执行命令只导出本地报告并保留原行资格记录。

Docker 回归 config 1062 项、191.733 秒，packaging 40 项、0.760 秒，均零失败（既有 2/1 项跳过）；四项 locked validators、三个 renderer 检查及实际 Rocky 8 platform-python 3.6 门禁通过。缺行、额外行、错序、错版本、错误 context 和混用工具链 receipt 的拒绝分支均覆盖。六行/完整 SDK 的实际执行等待全部 producer 成功后进行，这里的图检查不作资格证明。

## 批次 6：快速回归的 fixture 开销

在固定 Docker 工具镜像、4 CPU、16 GiB、断网条件下，用 cProfile 测量 Python qualification 与 row manifest 两组共 80 项测试。画像显示同一 release/ABI 下重复构造 fixture、provider catalog 检查与缩进 JSON 编码占用明显；profile 本身增加了开销，其 500.017 秒只用于定位热点，不作为性能基线。原始 profile 与日志在 `/tmp/crossforge-python-test-profile/`，摘要见[fixture 性能记录](python-test-fixtures-2026-09-10.json)。

`test_python_qualification.py` 现在只复用当前进程自己生成的 fixture 数据快照，key 包含 Python 版本、完整 release/ABI context 内容摘要及实际 provider catalog 字节摘要。快照保存为不可变 bytes，每次恢复生成独立对象图，并保留原先的对象引用关系；原 JSON 字节在独立临时目录恢复。正式 validator 和每个篡改用例照常执行，没有缓存通过/失败判断。新增回归验证嵌套修改、文件破坏、引用关系及同路径输入内容变化不会跨用例污染，也不会命中错误模板。

不带 profiler 的本地对照中，原 qualification suite 51 项为 100.188 秒；优化后包含新增隔离检查的 52 项为 47.756 秒。完整 config 从 1062 项、191.733 秒变为 1063 项、140.528 秒，均零失败且保留 2 项既有跳过；packaging 40 项、0.789 秒通过，保留 1 项既有跳过。两次测试使用相同 Docker 镜像与资源限制，期间存在其他本地组件工作，因此仅记录这一组本地结果，不外推 GitHub 墙钟收益。当前 runner 上的新 CI 基线、Python 并行度 2→3 及完整流水线的三次重放仍待实施。

## 批次 5：签名组件目录的信任边界

新增 `component-catalog.py` 与独立 `component_catalog.py` 模块。目录只包含同一原 producer 的 receipt 与固定 registry digest；未知字段、重复/歧义条目、混用 run/attempt/producer、错误 receipt 或 registry 均拒绝。消费者使用自身可信 checkout 中固定的 Cosign 二进制和 Sigstore 根，校验精确工作流/main identity、GitHub issuer/repository、dispatch 事件和原 source commit，再按当前独立输入选择引用。签名错误是错误；正确签名下缺少匹配输入才返回 `missing`，供后续 planner 安排 producer。输出仍需经过现有 OCI 和领域资格验证。

同 run pilot 新增消费者门禁之后的签名 job，只有该 job 获得 `id-token:write`，不具备 registry 写权限；签名前再次核验 handoff SHA256 和精确 clean commit/run/attempt，签名后立即调用同一消费者验证接口。最终 required gate 包含签名结果。目录与 bundle 目前保存在七天 Actions pilot artifact 中；永久引用保留、发现、缺失产物调度和生产消费尚未完成，也尚无真实 GitHub 身份签名或跨 run 运行结果。

[本地验证记录](component-catalog-2026-09-10.json)：Docker config 1075 项、166.620 秒，packaging 40 项、0.873 秒通过，分别保留既有 2/1 项跳过。四项 locked validators、三个 renderer、实际 Rocky 8 platform-python 3.6 强制门禁和 actionlint 通过。actionlint 首次调用的既有 `concurrency.queue` 例外模式不准确；仅修正调用模式后独立重跑静态检查通过，未修改该配置或重复运行已通过单测。真实固定 Cosign 的参数检查与无效 bundle 拒绝通过；单元回归中的模拟仅用于核验委托参数和拒绝路径，不作正向密码学证明。

## 批次 4：六行资格完成及 SDK 进度记录修正

cp311、cp310 分别完成五份构建组件及正式行资格，最后的资格生产/封装为 188.87 秒与 165.39 秒；各自 7 条规定 RUN 均新执行，两个架构的 locked/clean runtime tier 通过。连同此前 cp39/cp312/cp313/cp314，六行正式组件全部完成。完整记录补充到[矩阵交接观测](python-matrix-handoff-2026-09-10.json)。

首次六行 SDK 构建与报告导出完成，但执行证据读取器按 digest 保留最后一条事件，接收了下游 Bake 别名平移后的时间，导致一条 RUN 落到执行区间外并拒绝结果。核对固定 Buildx 0.36.1 的 `ResetTime` 与每目标 writer 调用后，SDK 读取器改为使用每条规定 RUN 所属的 Bake 目标和行，仍拒绝任何别名上的 cache/error，仍要求本次区间、每目标准确 RUN 数与唯一 digest。没有放宽共享资格读取器或重新绑定旧行 receipt。

[进度诊断记录](sdk-progress-2026-09-10.json)保留原始日志 SHA256、同一 vertex 的原目标/别名时间及上游来源。9 项 SDK 回归、2.176 秒通过，覆盖下游覆盖、缺少原目标、区间外原目标、别名缓存/失败、重复与漏执行；实际 Rocky 8/Python 3.6 强制门禁通过。旧日志复查只用于诊断，不将失败执行改写为通过；新输入身份下的六行与完整 SDK 已另开目录重跑，原日志和产物保留。

SDK 修正后的独立重跑已通过，见[六行 SDK 实跑记录](python-sdk-integration-2026-09-10.json)。`python-dev` 完整消费为 380.28 秒，其中 Bake 执行记录区间为 21:48:46–21:52:56 UTC。12 个图目标不包含 GCC/CPython 源码编译；12 条 append RUN 加 1 条 final RUN 均通过所属目标验收，六份导出行 manifest 与原正式资格 coverage SHA256 一致。每行保留原 producer、执行区间与 receipt，不改写为本次行资格。完整 `sdk-complete-dev` 已继续执行，仍未声明通过；本地组件和前置缓存已存在，不将此数字外推为 GitHub 性能。

签名试点工作流也已纳入控制平面路径识别，避免单独修改该手动工作流时被未知路径兜底误选为完整编译。13 项增量计划 Docker 回归、0.070 秒通过；未知路径仍保持全量兜底。

完整 `sdk-complete-dev` 随后通过：总消费 727.27 秒，Bake 记录区间 21:57:39–22:05:03 UTC，14 条规定集成 RUN 均新执行。最终 Python 与 complete SDK 报告均为 passed，六份行 manifest 继续与原正式资格产物一致；输入材料图不包含 GCC/CPython 源码编译。完整结果补充到[SDK 实跑记录](python-sdk-integration-2026-09-10.json)。本地两条组装根均已完成；独立 vcpkg upstream Tier 3、GCC full 等候选领域和真实 native ARM 仍遵循各自门禁，不由这次本地 SDK 集成代替。

## 批次 5：签名目录的持久存储、发现与固定引用恢复

`catalog_registry.py` 将 canonical catalog 与 Sigstore bundle 封装为固定两文件 OCI artifact。公共发布接口先验证签名，再以完整 manifest digest 建立保留 tag，最后更新精确输入的查找 tag。查找 tag 只提供位置；reader 固定首次取得的 manifest digest，逐 blob 校验文件名、大小与内容摘要，再验证签名和当前完整输入。显式恢复只接受同一内部仓库的 digest 引用，缺失时直接失败；普通发现仅将固定 ORAS 1.3.4 的精确 manifest 404 响应识别为缺失，权限、传输和错误索引均保留为错误。

pilot 新增独立 `catalog-store` job，按签名 job 的不可变 artifact ID 下载目录，使用 pinned Cosign 复核后写入 registry。该 job 具有 packages:write，但没有 OIDC；最终 gate 要求存储成功。七天 Actions artifact 仅留存诊断引用；registry 中保留完整 digest tag，当前不引入自动删除，后续容量与回收策略必须保护候选/发布所引用的目录与组件。

[本地存储观测](catalog-storage-2026-09-10.json)：真实 ORAS 在无外网、无 host port 的临时 registry 中完成两份合成目录的上传和按 digest 下载。输入 tag 改指第二份后，第一份仍由原 digest 完整恢复；相同 catalog/bundle 再次封装得到相同 OCI digest。公共 publish 与 lookup 均被真实固定 Cosign 拒绝无效 bundle；低层字节传输用未签名 fixture，不宣称正向 GitHub 信任或组件资格。临时 registry 已清理。

Docker 全量 config 1090 项、147.414 秒，packaging 40 项、0.871 秒通过，保留既有 2/1 项跳过；四项 locked validators、三个 renderer、实际 Rocky 8 platform-python 3.6 强制门禁及 actionlint 通过。生产缺失产物调度、跨 run 消费接线、真实 GitHub 身份签名、候选恢复和 native ARM 仍需推进；没有发布远程内容或改变锁定来源/ABI/baseline。

## 批次 5：只读跨 run 组件消费入口

手动 pilot 增加 `build`/`reuse` 两种模式。默认 build 保留生产、同 run 消费、签名和存储；reuse 只读 registry，使用当前输入查找或显式原 catalog digest，取得并逐项校验原始组件后执行同一套消费者门禁。最终 gate 对每种模式分别要求准确的 success/skipped job 集合，缺失、取消、失败、意外执行和未知模式均拒绝。reuse 没有 packages:write/OIDC，也不会重新签发旧 producer 的 receipt。

`component_resolution.toolchain` 在签名与精确输入校验之后下载固定 OCI，运行已有 receipt/metadata/实际字节校验，再次捕获当前材料与 BuildKit 环境后封存结果。成功返回原 producer、subject、固定 context 与 catalog 证据；索引不存在只返回无 subject/context 的 `build-required`。当前只读试点据此失败并指出需要 producer，生产自动补齐尚未接入。签名、传输、产物校验失败或显式恢复引用缺失均不转成重建请求。

[本地消费验证](catalog-consumer-2026-09-10.json)：Docker 全量 config 1101 项、152.204 秒及 packaging 40 项、0.761 秒通过，保留既有 2/1 项跳过；四项 locked validators、三个 renderer、实际 Rocky 8 platform-python 3.6 强制门禁与 actionlint 通过。回归覆盖信任模式冲突、先认证后传输、运行中输入/环境变化、原 producer 保留、缺失组件不得开始资格化，以及诊断中不得包含大型 OCI。

同一不可变源码快照还从既有 registry roundtrip 的本地 OCI 逐项核验输入和独立 receipt digest，实际重放重构后的共用消费函数：toolchain/runtime 与 GCC smoke 各 2 条规定 RUN 新执行、验收通过，GCC smoke 为 16 PASS；cp39 x86_64 构建通过，材料图无 GCC 源码。共用消费阶段耗时 87.78 秒，原始组件 producer 与本次 local 资格 producer 分开保留。此实跑调用的是已验证本地组件路径，不冒充 GitHub 签名或跨 run 密码学验证；新工作流尚未远程运行。

## 批次 3：CI 工具链组件绑定接口

`ci-build.py run` 增加可选组件 reader 参数，复用已建立的目录/OCI 验证接口。binder 按当前选中 Bake 图中的 canonical producer 边界，分别解析两个架构的安装产物和 GCC 测试上下文，每个角色只取一次；成功后替换所有对应 context。输入索引不存在时保留原 producer 的可达边并在 `components/binding.json` 明确记录；签名、传输与产物错误终止整个 stage。原选中 roots、逐 root 执行及整体超时保持，组件取得时间计入阶段观测。资格化 cold/cache-write 路径不能混用这一增量接口。

将小型目录、receipt、输入证据的保留逻辑提取为共用函数，原 pilot 继续使用；下载的 OCI 数据必须与上传诊断目录分离。当前只交付 CLI opt-in，自动 main 接入与 PR 凭据隔离、缺失 producer 的生产编排仍待连接，没有向 PR 新增 registry 权限。

[绑定验证记录](ci-component-binding-2026-09-10.json)：Docker 定向回归共 40 项通过（6 项绑定、3 项 catalog consumer、7 项 resolver、24 项 CI build）；三个 renderer 检查与实际 Rocky 8 platform-python 3.6 强制门禁通过。使用不可变源码快照、既有独立可信 receipt 和真实 OCI 校验，在本地替换 resolver 的信任入口后执行实际 `ci-build.run_stage`，工具链阶段成功，重新解析的消费图只有 `toolchain-x86_64-dev`，材料闭包不含 GCC 源码构建。该 cache-only 暖运行记录为 6.2 秒，保留原组件 producer，不宣称 GitHub 签名验证、新执行资格或远程性能收益。

后续核对本地配置时，补齐 resolver 对 `DOCKER_CONFIG` 的支持：显式 `docker_config` 优先，其次使用环境指定目录，再使用默认用户目录，确保 registry 与 Buildx 不会意外读取不同配置。Docker resolver 8 项定向回归通过（0.019 秒），覆盖环境配置与显式覆盖；日志 SHA256 为 `316558a93ba028238bcd514bbec8a07c6b5122f2fa8e4ce8499b5d85bc6111f8`。

## 批次 3：main 组件读取与 PR 权限隔离接入

`ci.yml` 现在只负责独立 push/PR/manual 入口，quick 检查抽到 contents:read-only 的 `verify-quick.yml`。候选和 pilot 直接复用该 quick 工作流并关闭日常构建选择，避免低权限 preflight 嵌套到 package reader。抽取前后，从 quick job 定义到全部静态/配置/语法/Bake 检查的主体逐字节相同，摘要记录在[本地验证](main-component-routing-2026-09-10.json)。

两个互斥 caller 分开授予权限：原 GitHub 仓库的 main push/dispatch 进入 `builds-components`（contents:read + packages:read），PR/fork/非 main 进入 `builds-readonly`（仅 contents:read）。可复用增量构建的工作 job 继承对应 caller；计划、输入准备及最终摘要三个 job 显式降为 contents:read。新增 `builds` gate 按真实事件重算预期分支，必须一个 success、另一个 skipped；原 `pr-required` 名称与 quick/builds 成功要求保持。该结构遵循 GitHub 可复用工作流权限只能维持或降低的规则，没有给 PR 新增 registry 凭据。

main 的工具链、Python、vcpkg、GCC、SDK 选中阶段启用已有组件绑定 CLI，使用 pinned ORAS/Cosign、签名目录及实际 OCI 核验。登录前再次检查 GitHub server、仓库、main ref 与 push/dispatch 事件；token 经 stdin 传入，阶段结束退出登录。索引缺失仍明确保留源码 producer，认证/传输/产物错误终止；源码 fallback 产物尚未集中封存发布，Python 行组件接线也继续推进。

Docker 最终 config 1113 项、148.309 秒，packaging 40 项、0.853 秒通过，保留既有 2/1 项跳过；四项 locked validators、三个 renderer、实际 Rocky 8 platform-python 3.6 强制门禁和 actionlint 通过。新增事件矩阵执行真实 planner 与 credential guard，覆盖 PR、PR-target、fork、非 main、tag、其他 server、计划失败和分支结果错配。中途修正了一处 action YAML 缩进，以及跟随 quick 抽取/权限继承边界变化的旧测试定位；完整复测零失败。远程 rollout 前仍须由 manual pilot 验证 package 访问及真实签名 build/reuse；本轮未执行远程发布或 dispatch。

## 批次 2/3：集中补齐 main 所需工具链组件

main 的组件分支现经 `verify-main-incremental.yml` 先解析实际选中 Bake 图，再按架构调用 `produce-toolchain.yml`。每个 producer 只处理该图需要的安装/测试上下文角色：验证已有签名目录的可用性；精确索引缺失才构建、封存并上传原始组件。认证或传输错误不能伪装成缺失。已有条目保留原 producer，由下游独立下载并核验实际 OCI，不重新签发为本次生产。

新增 `component_ci.py` 集中可信来源和 Bake 图捕获，原 pilot CLI 保持兼容；`ci_toolchains.py` 负责角色需求、缺失生产和结果检查。schema 2 handoff 显式声明一个架构和非空的新角色子集，仍由上游 job 的独立 SHA256、不可变 Actions artifact ID 和精确 commit/run/attempt 绑定。schema 1 保留原双角色 x86_64 试点契约。main catalog schema 2 只允许原始工具链角色和精确 `produce-toolchain.yml@refs/heads/main` 的 push/dispatch 身份；原 schema 1 的 pilot 身份不放宽，main 签名入口不能签发 Python 或资格 receipt。

主分支 caller 的最大权限只供受控的嵌套工作流使用；实际 raw producer/store job 只有 packages:write，sign job 只有 id-token:write，下游 caller 显式降为 contents:read + packages:read。PR 路径保持 contents:read。主分支源检查还要求原始 `ci.yml@refs/heads/main` caller、workflow/source SHA 一致和 clean checkout。阶段摘要拒绝缺失、未知或无效的计划输出，并分别检查需要执行的架构、是否有新产物，以及签名/保存的准确 success/skipped 结果。下游启用 `--require-components`，集中准备后若索引缺失则在 Bake 执行前失败，保留诊断，避免各下游再次编译。

[本地记录](central-toolchain-production-2026-09-10.json)：Docker 全量 config 1129 项、149.053 秒和 packaging 40 项、0.885 秒通过，保留既有 2/1 项跳过；四项 locked validators、三个 renderer 和 actionlint 通过。随后补充两项签名 CLI/角色边界回归，最终定向 64 项、3.254 秒及 actionlint 通过，最终 renderer 检查通过。实际 Rocky 8 Python 3.6 强制门禁完成递归编译、旧 CLI 和新 `ci-toolchains.py --help` 执行。

断网 Docker 内用固定 Buildx 对十种真实选择范围核对组件需求：文档/单纯输入检查无工具链；单架构工具链只选对应安装组件；cp39、vcpkg、SDK 需要双架构安装组件；GCC smoke 与全量 CI 需要两种角色/双架构；现有 GCC full canonical 目标只需要 x86_64 两种角色。探针最初因输入未排序、随后因误把当前 GCC full 预期设为双架构而失败；修正探针预期后全部通过，生产选择器未为探针修改。这些是依赖图与编排验证，不是新增资格结果或 GitHub 时延测量。

本批未推送代码、执行远程 workflow 或发布 GHCR 内容。仍需由 manual pilot 实测 package 访问与真实 GitHub 签名，再验收 main 新工作流；Python 行组件的生产 CI 消费、资格范围收窄、候选最终集成/原生 ARM、同 digest 恢复及完整性能重放继续推进。组件保留仍不自动删除。

## 批次 3/4：原始 Python 组件集中准备与 CI 消费

`ci-toolchains.py plan` 现在同时从真实选中 Bake 图提取 Python 原始组件需求，`ci-python.py` 按行校验并执行该计划。`produce-python.yml` 验证所需工具链后，依次解析 build Python、两个目标安装产物及其构建审计上下文。签名索引确实缺失才生产；错误签名、权限、传输或实际字节不匹配均终止。已有组件保留原 producer；本次 handoff 只包含新产物，以独立 SHA256、不可变 artifact ID 和准确 source/run/attempt 交接。catalog schema 3 将原始 Python 角色限定到独立 `produce-python.yml@refs/heads/main` 签名身份，既有试点/工具链策略保持各自边界。

新增 `verify-main-builds.yml` 保留现有选中门禁命令，用显式只读 leaf job 与有写入能力的 producer 调用区分权限。Python 原始组件准备与 GCC/vcpkg 共用输入/工具链前置，可以并行；Python 消费与 SDK 等待组件矩阵。行并行上限保持 2。PR 路径未改为有权限的 main 工作流。最终结果检查重算矩阵，并拒绝缺失、失败、取消、意外执行和被改写的输出。

`ci-build.py --python-components --require-components` 复用共享目录/OCI 验证，按依赖顺序核验原始图中的精确输入，再替换消费图所有对应 context。SDK 选择若仅包含某一行，不自动扩展六行。构建产物不是资格证明，原有行/SDK 门禁仍保留。跨机器复用正式行资格时应如何处理原始生产环境身份，仍待确认；本批没有改变现有严格资格环境匹配。

[本地验证记录](python-ci-components-2026-09-10.json)保留验证日志、脚本、输入快照和文件摘要。真实 Docker cp39 消费从 7 份独立可信的既有本地 receipt/OCI 取得两份工具链及五份 Python 组件，阶段执行 335.3 秒，探针完整耗时 336.24 秒；重新解析后的材料闭包无 GCC/native CPython/cross CPython 源码编译。registry 查找与下载在此探针中是明确的本地 fixture，实际 OCI 字节、receipt 与当前输入核验仍走生产代码；不宣称 GitHub 签名、远程发布、新执行资格或 CI 性能验收。

第一次消费因本地新快照目录 `0775` 与原生产快照 `0755` 不同而被输入验证器拒绝。另建统一 `0755` 的快照后通过，原失败快照与日志保留，未放宽匹配规则。十种真实 Bake 范围检查通过：文档、输入、工具链、GCC、vcpkg 不安排 Python 生产；cp39/cp314 和 SDK 单行仅安排该行；完整 SDK/全量 CI 安排六行。实际 Rocky 8 / Python 3.6 强制门禁执行新 CLI 与递归模块检查，通过 89 个投影检查。

最终 Docker 全量 config 1161 项、154.315 秒及 packaging 40 项、0.956 秒通过，保留既有 2/1 项跳过；四项 locked validators、三个 renderer 和 actionlint 通过。此前完整运行发现新工作流缺少默认权限声明，补上 `contents: read` 后完整复测；各 leaf job 的显式权限未放宽。定向 53 项回归还覆盖各 job 的准确 success/skipped 集合、被修改的矩阵、签名模式隔离、消费者权限，以及原门禁命令在两条 CI 路径中的一致性。

补充 producer 实跑因 Docker socket 的宿主 daemon 控制能力被自动审批拒绝，没有启动。改用断网、无 socket 的图核对，在原记录环境与此前验证过的 receipt 绑定下，确认 cp39 五份原始组件在 producer 图和 consumer 图中得到相同输入身份。这个补充结果仅是输入图验证，不计为新 OCI 校验、producer 实跑或 GitHub 身份验证；此前已完成的实际消费和 Rocky 门禁独立保留。

本批尚未推送或远程 dispatch。正式行资格包生产/复用接线、真实 GitHub 信任验收、候选最终集成与 native ARM、资格范围收窄及 runner 性能实验继续推进；当前实现不能作为这些事项已完成的证据。


## 批次 3/5：GCC 资格策略脱离完整 release

新增自包含标准库模块 `gcc_testsuite_policy.py`，从既有 GCC 资格投影及其可信摘要依赖读取版本、smoke/full 计划、冻结 baseline 和 ARM QEMU 身份。三个正式 GCC 门禁改用 `--components`；原 `--release` CLI、显式 observation 分支和最终候选完整 release 验收保留。原 DejaGNU suite、执行命令、基线精确比较及 qualification receipt 的执行环境约束不变。

[本批观测](gcc-policy-inputs-2026-09-10.json)记录了无 daemon socket 的 Docker 验证：

- 全量 config 1166 项、156.079 秒，packaging 40 项、0.832 秒通过，分别保留既有 2 项 zstd 和 1 项 nFPM 资源缺失跳过。四项 locked validators、三个 renderer 检查及 actionlint 通过。
- 38 项聚焦回归通过，包括没有完整 release 文件的裁剪目录、错误策略和子组件摘要拒绝、旧/新计划与 baseline 等价、三个真实 Bake 根的材料范围及 RUN 数量。
- 固定 Rocky 8 镜像内的 platform-python 3.6.8 完成四个修改模块编译、双架构策略等价和 runner CLI 检查；该项为普通容器检查，没有执行 Bake 或 GCC。
- 隔离源中仅修改产品版本并依次重新生成投影、vcpkg 和 Bake：旧版三个 GCC 根的材料身份全部变化，新版三个根均保持不变；双架构安装和 GCC 测试上下文的四份原始编译材料在本次代码改动前后完全一致。该对照使用显式外部产物 fixture，不作为实际产物信任或资格证明。

实际双架构 GCC smoke 及 x86_64 GCC full 重跑待执行；现有聚合投影仍耦合 smoke/full 与双架构，本批只移除无关完整 release 输入，没有宣称已完成全部资格范围拆分。合并和推送须等待整体所需验证完成。

补充验收：下载并核对仓库固定的 zstd 1.5.7 源码/签名及 nFPM 2.47.0 archive/可执行文件摘要后，在非 root、断网且无 socket 的 Docker 容器中补跑三个资源测试，3 项全部通过（6.678 秒，无跳过）。因此本版本 1166 项 config 与 40 项 packaging 均已有实际通过结果；不是将原整套测试的跳过改写为通过。工作流剩余的 51 条前置命令也通过（4.516 秒），包括全部列出的 RPM 计划/锁、Qt/GCC 策略、Python/Bash/C 语法和 canonical Bake 图解析；BuildKit `--check` 不在本次无 socket 检查范围内。

实际 GCC 重跑已准备固定源提交 `9dab68281c5ca30786a2ff65250c9f8faeec1586`，但自动审批再次因工具容器挂载宿主 Docker socket 所带来的广泛 daemon 控制能力而拒绝，进程未启动。已向用户请求该三项门禁的具体授权，没有改用间接方式绕过。只读远程检查确认 `main` 仍为研究基线 `cf736eab`，工作分支尚未推送。


## 批次 5：显式 CI 资格重放入口

新增手动 `replay-qualification.yml` 和领域模块 `ci_replay.py`，复用 `ci-build.py` 的超时、资源监控、已认证组件读取和诊断上传。工作流只授予 contents/packages read，拒绝非原仓库 main；缺少组件时失败，不在重放路径回退到源码编译。支持双架构各自工具链、GCC smoke/full、六行 Python、vcpkg 三个 upstream tier 和 SDK 集成，共 12 种选定范围。具体语义见 [Actions 操作说明](../github-actions.md#explicit-qualification-replay)。

计划从真实可达 Docker recipe 取得每个 owning target/stage 的 RUN 数量，只在所选资格阶段设置 `no-cache-filter`。SDK 的六行 append 和 Python final 在第一轮执行，完整 SDK 的第二轮只强制自己的 final，避免重复强制同一组追加检查。`--cold` 仍仅控制远程缓存导入；源码强制重建是尚待接入的独立模式。

成功必须同时具备 Docker 成功退出和本次完整 RUN 证据：保留所属 target 的原时间，拒绝 cached/failed 别名、缺失/重复/过期/错误行事件，结束后重算源图并比较实际环境。新的诊断目录要求防止新失败与旧成功记录混合。输出为明确的 CI observation，不生成可复用资格 receipt，不替代候选最终集成或 native ARM。

[本批记录](ci-qualification-replay-2026-09-10.json)：

- 12 种真实 Bake 图计划及 override 解析通过；所强制 RUN 数为 x86_64 工具链 2、ARM 工具链 5、GCC smoke 5、GCC full 2、每行 Python 9、vcpkg 4、SDK 合计 14，过滤器不含 GCC/CPython 源码构建步骤。图检查不证明实际执行。
- 无 socket、断网 Docker 全量 config 1178 项、164.090 秒，packaging 40 项、1.232 秒通过，本次挂入固定 zstd/nFPM 资源，无跳过。四项 locked validators、三个 renderer 和 actionlint 通过。全量之后新增诊断目录保护及回归，最终 CI 定向 89 项、7.099 秒复测通过。
- Rocky 8 platform-python 3.6.8 完成模块编译/导入、12 个策略入口和合成日志验证。新验证器读取此前真实 Python SDK/完整 SDK 日志，分别得到与原验证结果完全相同的 13/14 条记录及时间；没有改写成新执行。

实际新重放和 GitHub 事件验收尚未执行，之前三项 GCC Docker socket 授权仍待答复；本批没有重试被拒绝的 socket 操作。正式行资格复用、完整候选恢复/集成/native ARM、runner 重放对照和保留策略继续推进。未合入 main、推送远程或发布镜像。

## 批次 5：失败阶段固定原组件选择

新增 `component_recovery.py`，将 CI reader 已验证的完整原始组件选择保存为独立摘要约束的恢复文档。每项记录输入身份、catalog/OCI/receipt digest 和原 producer，不包含本次下载路径，不签发资格结果。恢复先核对源码材料、构建执行输入、commit（GitHub 环境）、stage、roots 和完整组件集合，再通过既有认证与实际 OCI 校验接口按固定 catalog digest 取得产物。任何缺失、身份变化或原 producer 变化均失败，不替换 producer 或回退源码构建。

`ci-build.py` 在首个 Bake solve 前保存完整清单，后续构建失败仍保留；组件取得未完成则没有可恢复清单。取得组件后和执行成功后再次检查当前源码、图和执行输入，阻止把运行中变化记录为成功。恢复与记录均要求新诊断目录、必需的已认证组件，禁止 cold/cache write。main reader 默认记录，手动重放入口接受原 run ID、不可变诊断 artifact ID 和独立 SHA256，使用 actions:read 下载。接口和限制见 [Actions 说明](../github-actions.md#recover-the-original-component-selection)：只接受相同源提交和完全相同的阶段 roots，main 前进或部分选择变成完整阶段会拒绝。

[本地验证记录](ci-component-recovery-2026-09-10.json)：无 socket、断网 Docker 完整 config 1192 项、165.554 秒，packaging 40 项、1.163 秒全部通过，无跳过；四项 locked validators、三个 renderer 检查和 actionlint 通过。新增 13 项回归覆盖原清单保留、独立摘要、缺项/角色错配、下载路径迁移、producer/receipt/产物替换、依赖顺序、失败后的同 digest 重试、取得前后源码变化和实际 composite shell 参数。最初两个测试重复使用诊断目录，被既有目录保护提前拒绝；改为每次独立测试目录后通过，未削弱生产保护。

12 种真实 Bake 图和源码材料闭包检查通过：单架构工具链各 1 份、GCC smoke 4 份、GCC full/vcpkg 各 2 份、每行 Python 7 份、SDK 32 份原始组件。改变声明的 execution fixture 会使材料身份变化。此项只有无 daemon 的真实图解析，没有组件认证或资格执行。固定 Rocky 8 platform-python 3.6.8 完成三个运行模块编译及合成恢复文档校验。

实际 registry 恢复、GitHub 重放、完整 candidate/source image 恢复与 native ARM 仍待验收；GCC socket 挂载授权仍待答复，本批没有重新尝试或改用间接方式执行。正式 Python 资格跨机器边界未放宽，runner 并行上限仍为 2，组件保留不自动删除。未合入 main、推送或发布。

## 批次 5：候选 ARM/签名部分重试保留原产物

发现现有候选部分重试的明确断点：native/signing 按当前 `github.run_attempt` 拼下载名称，而仅重跑失败 job 时，成功 publisher 的 artifact 仍属于原 attempt；promotion 也把四组 artifact 全部假定为最后一次 attempt。新增 `candidate_recovery.py` 领域模块和 `candidate-recovery.py` CLI，改为传递成功上游的不可变 artifact ID，严格核对完整输出、原 candidate manifest 摘要、原 probe/report 字节摘要和 attempt 顺序。ARM 失败后重跑仍执行真实 native probes；签名失败后重跑引用相同候选和已成功的原 ARM 报告。新候选仍必须执行自己的最终集成和原生验证。

原候选及报告身份在签名前核验，随后 `candidate-recovery.json` 随签名证据保存。promotion 先取得最后成功签名 attempt 的 artifact，核对其记录与 GitHub 当前 run artifact 元数据中的 ID/name、可用性、run、main source 和仓库关系，再下载原 publisher/native 产物并执行已有完整语义及公开签名验证。schema 2 promotion 将来源记录嵌入持久证据；创建/读取归档时再次核对原 probe/report 字节，仍为 14 份严格 payload。旧 schema 1 和没有恢复记录的旧候选保持同 attempt 契约，不查找更早成功结果。同步修正文档遗留的“17 份”和必需 Qt 发布证据描述。

[本批验证](candidate-partial-recovery-2026-09-10.json)：最终无 socket、断网 Docker config 1203 项、163.685 秒及 packaging 40 项、1.220 秒全部通过，无跳过；四项 locked validators、三个 renderer 与 actionlint 通过。44 项定向候选/发布回归和真实 promotion CLI 对合成身份的创建、幂等重试、验证通过：publisher attempt 1、native attempt 2、sign attempt 3 保持同一个 candidate/source identity。固定 Rocky 8 platform-python 3.6.8 完成四个运行模块编译及 11 项新增回归，全部通过。

验证过程中修正了一处测试定位与新增 step 的冲突，并将 native report 摘要绑定到实际上传的 staging 文件。临时兼容性脚本最初命名 `platform.py`，遮蔽同名标准库后只收集到 0 项测试；该结果无效，未作为通过证据。改名并强制要求执行 11 项后重新通过。最终复核还将来源记录创建/校验从签名后移到签名前，避免身份失败发生在签名写入之后，并完整复测。

此批仅完成成功 publisher 之后的同 run 部分重试；publish job 内 source/SDK 推送后的中途失败仍需要分阶段 checkpoint，跨 candidate run 恢复和内部组件进入候选发布图仍待接入。真实 GitHub 部分重试、公开签名与 ARM 执行验收未运行；之前 Docker socket 授权仍待答复，未重试被拒绝操作。整体目标仍未完成，未合入 main、推送或发布。

## 批次 5：来源与 SDK 发布分别保存恢复检查点

将候选原 `publish` 拆为 `source-publication` → `sdk-publication` → 最终消费者 `publish`。前两者成功推送并绑定身份后保存严格元数据 checkpoint；下游只接受成功上游的不可变 artifact ID 和独立 canonical SHA256。新 `candidate_publication.py` 模块核对同 source/run、原 attempt、当前 release、原始 OCI index 与 Buildx digest、来源归档身份、固定 SBOM generator 报告及精确文件集。SDK checkpoint 嵌入原来源 checkpoint，并逐字节保留五份来源文件。未知字段、错摘要、缺失/额外文件、符号链接、跨 run/source、未来 attempt 和覆盖已有恢复输入均拒绝。

SDK 构建前仍匿名拉取并核对完整来源归档；最终消费者独立验证公开 OCI/SBOM/provenance、匿名非 root 镜像和双目标下游 fixture，并重新构建 native probe bundle。该 job 降为 packages:read，不再推送镜像。来源成功后 SDK 失败可引用原来源检查点；SDK 成功后最终消费者失败可引用原 SDK 检查点。原生 ARM 失败仍重新执行原生探针。recovery schema 2 记录来源/SDK 的原 attempt 与 checkpoint SHA256，经既有 promotion schema 2 嵌入持久来源记录，顺序要求 source ≤ SDK ≤ 最终消费者 ≤ native ≤ signing。

[本批验证](candidate-publication-checkpoints-2026-09-10.json)：断网、非 root、无 Docker socket 的 Docker config 1212 项、165.981 秒及 packaging 40 项、1.254 秒全部通过，无跳过；四项 locked validators、三个 renderer 和 actionlint 通过。46 项候选定向测试通过；固定 Rocky 8 platform-python 3.6.8 编译三个运行模块并执行 20 项恢复回归通过。补充核对拆分前后 13 个原构建/验收步骤，除声明的上游输出引用替换外逐字节相同；原 native/sign job 内容完整保留。

这些 checkpoint 是发布身份记录，不是资格报告。只有成功保存检查点的原 producer 才能恢复；推送成功但检查点尚未成功封存上传时失败，仍没有安全自动恢复入口。全量重跑仍重新执行 producer，不通过可变 tag 猜测缺失记录。本批未执行真实 GitHub 部分重试、镜像推送、原生 ARM 或新的 BuildKit solve；之前被自动审批拒绝的 Docker socket 操作未重试。跨 run 候选恢复、候选组件消费、资格复用的 CI 接线和性能重放仍需推进，整体目标未完成，尚未合入 main 或推送。

## 批次 3/4/5：完整候选接入原始组件，持久保留选择来源

候选不再调用串行缓存写入的 `qualification.yml`，改为以不可缩小的 `profile: full` 调用现有组件流程，保留全部 canonical release stages。缺失原始工具链/Python 组件在集中 producer 生产、按既有受限 raw role 目录签名并保存后，下游独立核验消费。producer 的可信入口只从明确 main CI caller 扩展到明确的 main 手动 candidate caller；两者均要求 workflow/source SHA 相同且 checkout clean，candidate 的 push/schedule/其他分支和任意其他 workflow 仍拒绝。周期性资格缓存 writer 及其队列保持原策略，Python matrix 仍为 2。

新增 `candidate_components.py` 和 CLI，在 SDK 发布前认证目录、核对当前材料与实际 OCI，替换 33 份 raw 组件边界，并通过真实 Bake `--print` 重新解析消费图。最终材料盘点拒绝 GCC/native CPython/cross CPython 源码编译，保留 SDK、Python、GCC full 等既有资格路径。缺失、认证失败或不完整的集中生产直接失败，不由 SDK 重走源码 fallback。独立 binding SHA256 绑定当前源码、来源镜像/归档身份、执行环境、实际消费图、材料与原选择；发布后再次核对，成功才封存 SDK checkpoint。大 OCI 数据与可上传的诊断目录分开。

SDK checkpoint schema 2 保存原 `component-selection.json`；最终消费者输出独立摘要并上传同一选择。签名 job 下载原 identity artifact，在外部签名前核对选择摘要及 source commit，以 recovery schema 3 嵌入完整 catalog/receipt/OCI digest 与原 producer。已有 promotion schema 2 将其保存在十四份 payload 的持久归档中，不依赖诊断 artifact 永久可用。旧 checkpoint/recovery schema 继续按原契约读取；选择记录不被解释成资格 receipt。

[本批验证](candidate-components-2026-09-10.json)：断网、非 root、无 Docker socket 的 Docker config 1220 项、169.964 秒及 packaging 40 项、1.185 秒全部通过，无跳过；四项 locked validators、三个 renderer 和 actionlint 通过。53 项候选定向回归通过。固定 Rocky 8 platform-python 3.6.8 编译七个运行文件，24 项兼容性/恢复回归通过。真实 canonical Bake 图使用明确的 resolver fixture 完成 33 份组件替换与重解析，保留 SDK/GCC 验收材料，确认源码编译输入消失；不是实际 OCI 认证或候选构建的证据。新增持久归档回归读取归档中的原组件选择，并重新验证整体归档；native 报告验证边界使用既有 fixture，不宣称原生执行。

初轮新测试有两处 fixture 错误：工具链 spec 的 role 应由调用参数提供，以及篡改测试不能用只写一次的生产 JSON writer 覆盖已有文件；修正 fixture 后定向与全量测试通过。没有降低生产校验条件。正式行资格 receipt 的 CI 复用及环境边界、原始组件 producer 的部分重试、跨 run 候选恢复、实际候选集成/native ARM、真实 GitHub 签名信任和性能重放仍待完成。此前 Docker socket 操作仍待明确授权，未重试被拒绝的操作；没有推送、发布镜像或合入 main，整体目标继续进行。

## 批次 5：原始组件签名和存储部分重试

修复原始组件签名重试的断点：工作流虽保存了成功 producer 的 artifact ID，`from-handoff` 却默认要求当前 attempt，导致只重跑签名时拒绝原 handoff。工具链/Python ensure 现在额外输出原 producer invocation；新的 `component_retry.py` 域模块和 CLI 在下载前核对成功上游的完整输出、正整数 artifact ID、独立 SHA256、同一可信 run/source 和 attempt 顺序。只有 main 工具链/Python 模式可显式选择原 invocation；未提供时及旧 pilot 保留原契约。

签名重试保留原 producer、receipt 和 catalog 字节。成功 signer 在既有固定 Cosign 验证后输出 catalog/bundle 的原始字节 SHA256 及 signer invocation；store 按不可变 artifact ID 取得原文件，要求 producer ≤ signer ≤ 当前 attempt，核对原字节后才取得 registry 凭据，并继续执行既有真实签名验证和确定性持久存储。元数据 helper 不验证签名、不信任下载的 authentication 报告；固定 verifier 没有减弱。嵌套 caller 到 sign/store 补齐显式 artifact-ID 下载所需 actions:read，签名 job 仍没有 packages:write，存储 job 仍没有 id-token:write。

[本批验证](component-producer-retry-2026-09-10.json)：断网、非 root、无 Docker socket 的 Docker config 1229 项、170.248 秒及 packaging 40 项、1.188 秒全部通过，无跳过；四项 locked validators、三个 renderer 和 actionlint 通过。9 项新回归覆盖双架构工具链/Python 原 producer 保留、连续签名重试 catalog 字节一致、同 run/source 和 attempt 次序、独立摘要、签名文件变更与缺失、符号链接拒绝、权限分离及完整嵌套权限传递。Rocky 8 platform-python 3.6.8 编译五个运行文件并执行全部 9 项新回归通过。测试中的签名文件是明确的未签名 fixture，仅验证元数据交接，不作为真实 Cosign 或 GitHub 签名成功证据。

此路径要求成功的 producer/sign job 输出和已上传 artifact；成功上传前失败、跨 run 恢复、真实 GitHub 部分重试仍未验收。正式 Python 行资格跨机器环境边界未放宽，runner 并行度仍为 2。此前被自动审批拒绝的 Docker socket 构建操作未重试，实际新 GCC 重放及候选集成/native ARM 等剩余验收继续等待。未合入 main、推送远程或发布镜像，整体目标保持进行中。

## 批次 5：显式重跑所选源码编译步骤

新增 `ci_source_replay.py` 和手动 `replay-sources.yml`，通过 `ci-build.py run --rebuild-sources --source-builder` 复用既有超时、资源监控、raw BuildKit 日志及执行证据校验。明确支持两套工具链和六行 Python，共八个单阶段范围；工具链重跑该架构 binutils/GCC 的两个 RUN，Python 行重跑 build Python 与双目标 CPython 所属阶段的六个 RUN（包含原行身份检查）。计划必须在真实材料闭包中找到完整 canonical root、原 producer stage 及编译命令，不能将缺失的源码节点当作成功。

仅对选中的 owning stage 设置 `no-cache-filter`，完整 canonical 阶段门禁继续存在；不强制源码下载、prepared 输入、无关编译器或全部资格阶段。Python 源码重建不会主动强制 GCC，但普通缺失依赖仍可能沿默认图构建。所有输出覆盖为 cache-only、清空 tags 和远程 cache exports。入口只有 contents:read，没有签名或包写权限；拒绝组件替换、资格重放、缓存写入组合和已有诊断目录。可额外选择既有 `cold` 以移除远程缓存导入，仍由实际编译事件证明重跑，不宣称完整空缓存构建。

新 observation 与资格 replay 使用不同 kind，记录原编译事件时间，保留 `qualification_receipt: false`。Docker 退出为零仍须检查每条 owning RUN 的完成、时间、cached/failed alias，以及运行后完整源码图和实际执行环境；缺项或变化留下失败状态。现有资格重放、恢复和普通 CI 路径保持原行为。

[本批验证](ci-source-replay-2026-09-10.json)：断网、非 root、无 Docker socket 的 Docker config 1238 项、173.178 秒与 packaging 40 项、1.279 秒全部通过，无跳过；四项 locked validators、三个 renderer 和 actionlint 通过。新增九项回归覆盖八个真实 Bake 图及 override 重解析、编译范围、缺失 source producer、缓存/失败/过期/错 owning 事件、源码和环境变化、非零退出、CLI 参数及真实 composite shell 模式约束。Rocky 8 platform-python 3.6.8 编译两个运行文件，六项执行证据/工作流 fixture 通过；CI CLI 保持既有较新 Python 运行基线，不宣称完整 CLI 已迁移到 3.6。

临时图检查脚本最初使用 `inspect.py` 命名，遮蔽标准库导致定向测试加载失败；改名后 34 项定向回归通过，随后新增 composite 回归进入上述最终全量验证。Rocky shell fixture 初次因没有 `python3` 命令而失败，最终驱动只为该 fixture 将 `python3` 临时链接到 platform-python 后通过，没有改动生产工作流或放宽断言。图与合成事件均不是实际编译证据。

尚未执行新的强制源码编译、GitHub 手动入口或性能对照；此前 Docker socket 授权仍待答复，未重试被拒绝的操作。正式 Python 行资格 CI 复用与环境边界、跨 run 恢复、真实候选/原生 ARM、runner 性能与保留容量仍待推进。整体未完成，未合入 main 或推送远程。

## 批次 3/4：Python clean-Rocky 共享运行时输入组件化

分析确认 Python 行 compile/runtime/final 报告和共享 clean-Rocky overlay 均绑定整份 release。此批先移除共享运行时根的耦合，作为后续整行资格输入拆分的前置：两个 `python-runtime-clean-<arch>` 阶段改为独立读取已认证的 `rpm/sysroot-<arch>` 投影，不再复制完整 release、release schema 或维护用 RPM plan。沿用原 materializer 的组件接口和完整 lock/transaction/metadata/trust 校验；完整 bundle 字节及 RPM 验签仍先于七个 runtime RPM 的选择，原两次 transaction、rpmdb 和 os-release 检查保持。

新增标准库模块 `python_runtime_overlay.py`，把 overlay schema 2 的 `input_binding` 明确绑定到目标 sysroot 组件，不冒充完整 release SHA256。runtime reader 和原行 finalizer 从当前完整 release 独立计算预期组件，继续检查固定镜像、目标、sysroot/transaction、精确 package/NEVRA/digest 和实际 rpmdb/os-release。schema 1 按原完整 release 摘要验证，不能被当作跨 release 复用证据。现有 compile/runtime/final 报告和 Python qualification aggregate 的完整 release 依赖尚未移除；最终 SDK 的完整绑定、资格环境边界与原生 ARM 门禁不变。

[本批记录](python-runtime-inputs-2026-09-10.json)：

- 断网、非 root、无 Docker socket 的 Docker config 1248 项、179.259 秒与 packaging 40 项、1.215 秒全部通过，无跳过。四项 locked validators、三个 renderer 和 actionlint 通过；三个 renderer 按规定顺序生成后没有 generated 文件差异。
- 32 项定向回归通过。新增十项覆盖双架构真实 lock/component 校验、无 release/plan 的裁剪目录、完整 bundle 校验调用顺序、混合/缺失输入、组件与镜像/target/sysroot/包摘要篡改、runtime 实物清单核对、新旧 overlay 的最终报告验证。裁剪目录的 RPM 校验/安装调用及 runtime rpmdb 使用明确 fixture，未宣称进行了新 RPM 安装或目标执行。
- 固定 Rocky 8 platform-python 3.6.8 编译四个运行文件，并执行九项新契约与消费者回归通过；真实 Bake 图检查在普通工具容器中进行。
- 对照规范化的原提交 `8eb2ec0` 与当前源码快照，34 份原始编译组件（双架构工具链安装/GCC 测试上下文，以及六行 Python 的全部安装/审计组件）材料闭包全部不变。两个运行时根的材料按本批契约变化。
- 分别在旧版和新版隔离源中只将产品版本改为 `0.1.1`，依次生成三个输出后重新解析真实 Bake 图：旧版两个运行时根均失效，新版均保持原材料身份。此项没有实际 BuildKit solve，不作为耗时、产物认证或资格执行证明。

初轮新增 fixture 使用了错误的最终报告字段 `runtime_results`，并遗漏裁剪目录所需 Rocky RPM 公钥；修正为原 `executions` 字段并补齐信任根后，定向和全量均通过，没有降低生产检查。实际新 clean-Rocky 安装/Python 双目标 runtime 重放、整行资格投影、正式行 CI 复用、GitHub 信任/恢复及性能验收仍待推进。此前 Docker socket 授权仍待答复，没有重新尝试被拒绝的操作；未合入 main、推送或发布。

## 批次 3/4：Python 行/目标资格配置契约

新增六份 `implementation/python-<row>-qualification-policy`、十二份 `python/<row>-<arch>-qualification` 和六份双目标行汇总。target 投影通过依赖绑定当前 target build、toolchain qualification 和 runtime RPM 输入，并直接固定当前 Python source/signature、ABI 与执行策略；cp314 额外固定 zstd。保留旧全行 qualification policy/aggregate 的原文档，不改写旧报告身份。

`python_qualification_policy.py` 只依赖标准库、最小 component reader 和行契约。使用独立可信的 target 投影 digest 验证根文档及其 row policy；最终消费者通过完整 release renderer 独立重算预期。返回明确的配置策略和 input binding，不创建裁剪版 release 摘要，不将配置摘要冒充产物或执行证据。当前 compile/runtime/final schema、Docker 资格输入及正式 CI 报告消费者仍走旧路径；本批是报告链迁移的契约前置，尚未使生产资格任务缩小重跑范围。

[本批观测](python-row-policy-2026-09-10.json)：最终 Docker config 1258 项、191.202 秒和 packaging 40 项、1.236 秒全部通过，无跳过；四项 locked validators、三个 renderer `--check` 及 actionlint 通过（仅保留原 concurrency.queue 解析兼容例外）。68 项定向回归通过；固定 Rocky 8 platform-python 3.6.8 编译两个运行模块并通过十项新契约回归。裁剪目录只包含三个 Python 模块与每目标两份投影，覆盖 cp314 的 zstd 和 ARM QEMU 策略读取，没有完整 release/schema/renderer。

精确失效回归覆盖单行 source/patch/support/signature、目标 sysroot/ABI、共享 provider、QEMU、zstd 和产品/供应链元数据；目标间仍共享现有 RPM base-image 输入，测试保留其真实传播范围。对规范化 `dced2c8` 和本批源码解析真实 Bake 图，34 个原始编译组件及两个共享 runtime 根均保持原输入身份，原 89 个组件文档全部不变；新增投影使总数增至 113，Bake 输出本身没有改变。图、配置解析和 fixture 不是新运行资格或性能验收。

首轮投影回归要求旧精确影响集合补上新增的行/目标节点；保留全部原断言后通过。首轮全量 1258 项只有 Bake 测试仍断言组件总数 89，更新为 113 后重新完成上述全量验证。未变更版本、ABI/GCC 基线或目标执行限制。

下一步将新契约接入 compile/runtime/final producer/reader 和行/SDK 汇总，保留旧报告的精确完整 release 校验。正式跨机器资格环境边界、真实 GitHub 信任与恢复、候选/原生 ARM、源码/资格重放、三次基线和并行度实验仍待验收；Docker socket 自动审批拒绝后的授权仍待答复，未重试或绕过该操作。整体仍在实施，未合入 main、推送或发布。

## 批次 3/4：静态 Python 编译资格消费行/目标输入（2026-09-11）

`cpython-qualify-build` 已切换到行/目标 qualification 投影，不再复制完整 release、release schema、renderer 或 source-to-release bridge。目标投影增加对本行 source component 的直接依赖，row qualification policy 继续绑定 build policy；阶段只复制这四份投影和相应 ABI 输入。qualifier 在访问编译产物之前，通过独立可信的资格根摘要重新认证 source/build-policy 并核对完整 prepared source manifest，不能靠几组互不核对的 CLI pin 拼接输入。

`qualify-cpython.py` 的显式组件模式生成 compile schema 5，以 `input_binding` 替换全 release SHA256 和全行 qualification pair；旧 CLI 模式保留 schema 4 与原身份，并延迟加载完整 renderer。编译后的 source、sysroot/transaction、ABI、ELF、target guard、SDK tree、扩展与私有 zstd 检查继续执行。runtime preflight 和原 finalizer 接受 v4/v5 两种 compile，但新 binding 必须由当前完整 release 独立推导，混合字段、错 schema/digest 或组件篡改均拒绝。

runtime schema 3 与 final schema 4 仍精确绑定完整 release。finalizer 的全行 qualification pair 从完整 release 独立生成；嵌套 v5 compile 的序列化摘要及两个原 runtime report 的绑定仍逐一验证，不能借此把旧完整报告迁移到新 release。row/SDK 汇总继续走原完整 release 门禁。该批只缩小静态资格输入，正式整行资格复用尚未接通。

[验证记录](python-compile-inputs-2026-09-11.json)：断网、非 root、无 Docker socket 的工具容器中 config 1268 项、217.868 秒和 packaging 40 项、1.196 秒全部通过，无跳过；四项 locked validators、三个 renderer 和 actionlint 通过（仅既有 queue 兼容例外）。固定 Rocky 8 platform-python 3.6.8 编译四个运行模块并执行十项新增回归通过。测试覆盖十二套实际投影/source manifest 的输入认证、六行新 compile 在原 runtime/final 门禁内的消费、裁剪目录无 release/renderer 加载、旧 CLI、类型/字段/摘要/源码篡改、ABI/ELF/guard 拒绝，以及 cp314 zstd 的显式组件路径。报告与执行是 fixture；zstd isolated test 使用真实 manifest 校验并 mock 模块 ELF audit，不记成实际新资格执行。

对规范化 `3d19020` 与当前源码解析真实 Bake 图，34 个原始编译组件和两个共享 runtime 根输入不变；十二个静态资格输入按新契约变化。只改产品版本：旧实现十二个静态资格均失效，新实现均保持原身份；只改 cp39 source digest：新实现仅两个 cp39 静态资格失效；只改 x86_64 ABI identity：仅六个 x86_64 静态资格失效，ARM 保持不变。95 份原有组件文档不变，本批只更新十二份目标投影及六份行汇总。上述数据是材料范围观测，不是 BuildKit solve、时间优化或真实执行证明。

首轮定向/全量回归提示旧测试仍期望全行参数和 release COPY；改为精确断言新参数、四份投影与最小复制范围。新增 zstd fixture 最初用了紧凑 JSON，与预期 serialized digest 不同；统一实际 fixture 字节后保留原 digest 比较，完成最终验证。源码版本、ABI/GCC baseline 和交叉构建中禁止目标执行的规则未变。

下一步迁移 runtime/final 及 row/SDK 的行级输入生产路径，并完成真实静态/运行时重放。GitHub 信任/恢复、正式跨机器资格环境边界、候选/原生 ARM、强制源码重放和性能实验仍待验收。此前 Docker socket 自动审批拒绝后的授权仍待答复，未重试或绕过；未合入 main、推送或发布。

## 批次 3/4：Python 目标运行时与最终报告消费行/目标输入（2026-09-11）

`cpython-runtime-input` 改为从锁定 host 工具根继承，显式复制十二个运行模块；只接收静态阶段传入的投影/ABI 文件及 runtime roots，不复制完整 release、schema、renderer 或 source bridge。两架构 Bake 资格目标收到各自行/目标根 pin，shell 与阶段前置检查核对 row/version/adapter，runtime 输入选择器在运行探针之前认证根投影和 compile binding。

组件模式形成固定报告链：compile schema 5、两个 runtime schema 4、目标 final schema 5 与 clean overlay schema 2。三个 Python 报告核对同一 `input_binding`；源码与签名、sysroot/transaction、五类 ABI 输入、ELF、guard、安装树、扩展、私有 zstd、运行库、探针及嵌套报告序列化摘要仍保留原校验。x86_64 使用原生 chroot，ARM 对照 policy 中的固定 QEMU binary/version/CPU/uname。旧 `--release` CLI 保留 runtime schema 3、final schema 4 与精确完整 release 身份；旧 final 可接收原 compile 4/5，但 runtime 格式必须保持 3，禁止混入新 runtime 报告。

行 finalizer 和 SDK append 从完整 release 独立推导新目标 policy；行清单 schema 2 及全行 qualification pair 保持完整 release 身份。最终 SDK 集成也已补齐 schema 5 消费：仍核对行清单绑定的 report/tree/解释器字节，再独立从完整 release 推导目标策略。没有把配置 binding 当作正式执行 receipt，既有组件生产者/安装文件/执行日志验收保持原边界；正式整行 CI 复用尚未完成。

[验证记录](python-runtime-inputs-2026-09-11.json)：最终禁网、非 root、无 Docker socket 的工具容器中 config 1279 项、257.5 秒及 packaging 40 项、1.161 秒全部通过，无跳过；四项 locked validators、三个 renderer `--check`、shell syntax 与 actionlint 通过（仅保留原 concurrency.queue 兼容例外）。固定 Rocky 8 platform-python 3.6.8 编译六个变更运行文件，九项 runtime/final 契约回归及一项最终 SDK 消费回归通过。六行 x86_64 fixture 报告经过新 final producer 与完整 release consumer；ARM fixture 单独覆盖 exact QEMU executor；裁剪 cp314 runtime 目录只带十二个脚本和两份 policy 投影，不加载完整 renderer。行 dispatch 与最终 SDK 身份 fixture 不声称替代完整嵌套报告门禁或实际目标执行。

真实 Bake 图和规范化源快照比较 `d5ed6d6` 与本批实现：34 个原始编译组件输入不变，113 份 component 文档原字节不变；共享 overlay/qualification 实现变化使两个 runtime 根、十二个静态与十二个运行时资格闭包更新。只改产品版本：旧实现十二个运行时资格均失效，新实现均不变；cp39 source 只影响其两目标，x86_64 ABI 只影响六个 x86_64，QEMU executor 只影响六个 ARM。SDK 消费者补丁后的规范化源再次确认这 60 个已测闭包一致。此处没有实际 BuildKit solve，也不是 CI 耗时或正式资格证明。

初轮定向测试中两处 fixture 仍访问旧模块名称，修正后重测；新增负例发现旧 final 可以包入新 runtime，现已收紧格式配对。代码审阅补齐最终 SDK 的旧 schema-only 限制后，再次完整运行全部测试。一次补充材料比较误将工作目录权限与归档规范化权限混用；改用相同 Git 导出与 mode 规范化后全部一致，未修改生产闭包算法。

下一步迁移行汇总/SDK 的输入范围，并接通正式 qualified-row CI 生产与消费；保持现有物理执行环境约束，等待跨机器资格边界的明确决定。新报告链实际 Docker 重放、GitHub 信任/重试、候选/原生 ARM、强制源码重放、三次基线及并行度实验仍待验收。此前自动审批拒绝容器挂载主机 Docker socket（会授予广泛 daemon 控制），明确授权仍待答复，本批没有重试或绕过。未合入 main、推送或发布。

## 批次 3/4：Python 行汇总与 SDK 消费行级输入（2026-09-11）

新增 `python_row_policy.py`，以独立可信的 `python/<row>-qualification` 根摘要认证两份目标策略，并核对两目标共同的行契约、支持状态与 source/build-policy 身份。复用原 source preparer 的 reader 和 manifest 构造逻辑，逐份认证六份投影并核对完整 prepared source manifest，没有复制 source/patch 解析逻辑或构造不完整的 release。`cpython-row-assemble` 从锁定 host 工具根继承，只复制十四个 Python 文件，不再带入完整 release、schema、renderer 或 source bridge。

新行清单使用 schema 3，以行 `input_binding` 替代完整 release SHA256 和全行 qualification pair；必须消费 source schema 2、compile 5、runtime 4 和 target final 5，安装树、ABI、ELF、build Python、zstd 及目标报告摘要检查保持原要求。原 `--release` 调用保留 row schema 2。SDK append 与正式 receipt 验收通过 `--row-manifest` 选择待验格式，再从当前完整 release 独立推导行策略、核验实际文件并重算整份清单，额外保留输出字节比较。最终 SDK 也已接收新行 binding，其最终集成报告继续绑定完整 release；不能把行输入相同视为允许省略 SDK 集成。

[验证记录](python-row-inputs-2026-09-11.json)：最终断网、非 root、无 Docker socket 的工具容器中 config 1290 项、273.269 秒和 packaging 40 项、1.211 秒全部通过，无跳过；四项 locked validators、三个 renderer `--check`、shell syntax 与 actionlint 通过（仅既有 concurrency.queue 兼容例外）。定向 82 项回归通过；固定 Rocky 8 platform-python 3.6.8 编译四个变更运行文件并执行九项新增回归通过。裁剪 cp314 行阶段只含十四个脚本和六份投影，生产输入选择器不加载完整 renderer 或 source bridge。

六行 fixture 均验证组件生产与完整 release 消费输出完全相同；篡改源码、组件 pin、目标报告引用、安装文件、metadata、额外字段或混合新旧身份会失败。行 fixture 的目标报告执行体、目标 validator 和 ELF 工具边界是显式 mock，实际 fixture 文件/树摘要与既有 zstd 汇总仍执行；全量套件另含真实目标报告验证器的回归。这些检查不代表取得新的目标或整行执行资格。

对规范化 `67ec867` 和本批源码解析真实 Bake 图：34 个原始编译、两个 clean runtime 根、十二个 static 与十二个 target runtime 资格闭包均不变；仅六个行汇总闭包更新，113 份组件文档原字节不变。单改产品版本，旧实现六行均失效，新实现六行均不变；改 cp39 source 只影响 cp39，改私有 zstd 只影响 cp314；共享 x86_64 ABI 或 ARM QEMU 的变化仍影响包含该目标的全部六行。这是材料范围观测，不是 BuildKit solve、实际执行或耗时测量。

首轮定向测试有两处旧图断言仍要求原 row host 和不带根 pin 的参数，已改为精确检查新的最小阶段及可信根。第一次全量运行另发现旧材料测试仍要求 `config/release.json`；改为精确检查六份投影且禁止完整 release/schema，同时保留七个已验证 subject 及不包含 GCC/CPython 源码编译器的断言，随后重新完整运行全部测试并通过。

下一步接通正式 qualified-row 的 CI 生产与 SDK 消费，保持现有物理执行环境约束，等待跨机器资格边界决定。新报告链实际 Docker 重放、GitHub 信任/重试、候选/原生 ARM、强制源码重放、三次基线及并行度实验仍待验收。此前自动审批拒绝容器挂载主机 Docker socket（会授予广泛 daemon 控制），明确授权仍待答复；本批未重试或绕过。未合入 main、推送或发布。

## 批次 4/5：正式 Python 行的 CI 生产、目录查找与签名交接边界（2026-09-11）

新增 `ci-python-row.py`、`ci_python_rows.py` 和仅供复用调用的 `produce-python-row.yml`。入口检查既有可信 main caller 与精确 clean source，经已认证目录取得两份工具链安装组件及五份原始 Python 组件，缺失任一原始组件就报错，不在行资格 job 内隐式重编译 GCC/CPython。新 `python_row_resolution.resolve` 随后重新绑定七个 subject、捕获完整当前资格输入，查找签名目录、拉取 digest 固定的 OCI，并调用原 `python_qualification.verify_local` 核验实际安装字节、报告与执行 vertices。

仅明确缺失输入索引会请求 fresh qualification；签名失败、传输失败、receipt/报告验收失败、执行环境变化和固定恢复引用缺失均不回退。命中结果保留原 producer 和 `verified-prior-execution`，不生成新 handoff 或签名。缺失时调用原正式资格生产者重跑完整 compile/runtime/row 门禁并验证封装后的产物；CI 适配器在发布前核对计划输入、receipt、clean source 与物理执行环境。失败时保留已有输入、BuildKit progress 和资格报告，不上传安装树或 OCI blobs；诊断路径拒绝 symlink。

`python_row_handoff.py` 绑定单一完整行、原始 run/attempt、构建及物理环境、产物 digest 和完整 metadata 清单。新 catalog schema 4 只接受固定 `produce-python-row.yml@refs/heads/main` signer 的完整行资格 receipt，并核对允许的 main 事件和原 source SHA。原始工具链/Python 的 signer 仍不能授权资格报告，行 signer 也不能授权原始组件；`from-handoff --python-row-ci` 与旧入口不能混用。producer、signer、registry writer 分别使用所需权限，签名/存储重试沿用原 artifact ID、handoff/catalog/bundle 摘要与 producer invocation，不将旧执行改写为当前 attempt。

[验证记录](python-row-ci-2026-09-11.json)：断网、非 root、无 Docker socket 的固定工具容器中 config 1308 项、273.949 秒及 packaging 40 项、1.271 秒全部通过，无跳过；四项 locked validators、三个 renderer `--check`、shell syntax 与 actionlint 通过（仅既有 concurrency.queue 兼容例外）。定向 71 项回归通过；固定 Rocky 8 platform-python 3.6.8 编译七个运行文件并执行十八项新测试通过。覆盖 handoff 身份、签名权限隔离、原始 attempt 重试、七组件依赖顺序、目录/传输/执行失败不得视为 miss、变化的 source/host 不得发布，以及失败诊断范围。

本批新测试是控制流和契约 fixture：上游组件域验证、OCI transport、Cosign、实际目标执行及正式行验收均为显式 mock；全量套件保留相应原有域回归，没有取得新资格执行、GitHub 签名或跨 run 复用证据。代码审阅后补充了发布前身份检查与失败日志保留，再执行最终定向和全量测试。版本 pin、ABI/GCC baseline、Docker 资格配方和严格物理环境比较策略均未改变。只读 `git ls-remote` 确认远程 main 仍为 `cf736eab8aa53b851874509d66676e5bac98dc27`。

新的可复用工作流尚未被 main 动态 matrix 或 candidate SDK 调用；下一步接入选中行的资格取得、忠实反映所选工作的 required status 和 SDK 消费，不能据当前入口就声称 CI 行资格复用已上线。跨机器物理执行环境边界仍待决定；新报告链实际 Docker 重放、真实 GitHub 信任/重试、候选/原生 ARM、强制源码重放和性能实验仍待验收。此前自动审批拒绝容器挂载主机 Docker socket（会授予广泛 daemon 控制），明确授权仍待答复；本批未重试或绕过。未合入 main、推送或发布。

## 批次 4/5：签名目录自动取得完整 SDK 输入并连接原集成执行器（2026-09-11）

新增 `python_sdk_catalog.py`，通过 `acquire-python-sdk` 和 `execute-python-sdk-catalog` 两个 CLI 接入原完整 SDK 消费路径。首先复用从 `python_sdk.bind` 提取的 canonical graph 验证，确认两份工具链和六行各五份原始 Python 组件的完整依赖集合；共享工具链只做一次目录取得，随后按原依赖 reader 取得各行组件，再调用正式行目录 reader 核验七个 subject 和资格记录。没有增加一套手写的 source/build/qualification 身份规则。

全部就绪时输出与原本地 SDK binder 兼容的 `components.json`。原始组件或行资格缺失时返回非零状态、明确的 `required_builds`/`required_rows`，不输出可消费的完整组件清单；不受影响的行仍可检查并留下诊断。验签、传输或域验收失败直接报错，执行环境在前后均须保持一致。OCI 数据与诊断目录不能重叠，包括经父目录 symlink 指向同一路径的情形。

集成 CLI 仅在完整就绪后调用原 `python_sdk.execute`，重新核对所有本地 receipts、安装文件、原始执行证据和当前输入，再执行原 SDK append/final 集成。取得目录本身不声明集成成功；只有原集成执行器成功后才写外层 `result.json`。两条新命令均为只读 registry consumer，不发布产物、不隐式补编译或补资格。原 `bind-python-sdk` / `execute-python-sdk` 的本地组件清单接口和验证要求保留。

[验证记录](sdk-catalog-consumption-2026-09-11.json)：固定工具容器中 config 1322 项、277.22 秒及 packaging 40 项、1.204 秒全部通过，无跳过；四项 locked validators、三个 renderer `--check`、shell syntax 与 actionlint 通过（仅既有 concurrency.queue 兼容例外）。定向 46 项回归通过；固定 Rocky 8 platform-python 3.6.8 编译三个运行文件并执行十四项新回归通过。Rocky 测试读取同一轮工具容器保存的真实 Bake print，仅替换获取图的测试入口，原来声明的域/传输/执行 mock 边界保持明确。

两个实际 SDK 根的 Bake 解析均确认两份共享工具链、六行各五份原始组件。新 acquisition 的 fixture 结果继续进入原 SDK binder 和真实材料捕获：最终依赖止于八个产物边界，不含 GCC/CPython 自身源码编译；这不是新 SDK 运行证明。回归覆盖精确依赖/行顺序、单行与共享依赖缺失、缺失 producer 不得被遗漏、验签/传输/资格失败、环境变化、数据/诊断目录重叠、执行失败不写成功标记，以及两条 CLI 的非零失败语义。新 tests 的 registry、域验收和实际 integration 调用是显式 fixture/mock，未声称实际下载/验签或新资格执行。

补充核对 runner 的官方保证：[GitHub 标准 hosted runner 文档](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)说明常规非单 CPU runner 使用新的 VM；[runner-images 说明](https://github.com/actions/runner-images/blob/main/README.md)说明 GA 镜像按周更新；[Docker 资源约束文档](https://docs.docker.com/engine/containers/resource_constraints/)说明 CPU/内存限制通过宿主 cgroup 控制。由此结合本项目目前绑定的 CPU、微码、内核、Docker 和内存等字段，可以推断仅固定 OS 标签或资源上限不足以证明全部环境字段一致；本轮没有实测跨 runner 失配比例，也没有把该推断当作性能数据。已向用户明确询问环境边界选择，未放宽现有严格匹配。

主 CI 动态任务、所选工作对应的 required status、增量选择器对新编排代码的覆盖、精确跨 run 恢复以及候选发布的调用接入仍待完成。新报告链实际 Docker 重放、GitHub 信任/重试、候选/原生 ARM、强制源码重放和性能实验仍待验收。此前自动审批拒绝容器挂载主机 Docker socket（会授予广泛 daemon 控制），明确授权仍待答复；本批未重试或绕过。未合入 main、推送或发布。

## 批次 3/6：vcpkg 工具链报告消费与模块范围（2026-09-11）

检查发现 GCC 三个正式资格阶段已经使用独立投影，vcpkg SDK 则仍复制完整 release、组件生成器和 Python 行模块，后续契约与三层 upstream 报告也绑定完整 release。本批先拆开共享工具链报告验收的输入接口：新增 `qualify_policy_toolchain_report` 直接消费已认证策略，要求 scoped 报告及精确策略格式；旧 `qualify_prior_toolchain_report` 保留完整 release 和旧报告兼容路径。两个入口共享原有来源、版本、sysroot、ABI、QEMU、运行时结果和安全文件检查，不把旧报告改写为新执行。

vcpkg SDK 新增可选工具链组件目录和两个资格根 pin，要求一起提供且完整覆盖两架构。Docker/Bake 正式路径使用该入口，沿各自依赖认证八份投影，再与当前 release 独立推导的策略比较；随后执行原报告检查。该阶段只复制五个运行模块，去掉组件生成器、Python 行模块和不再需要的 validator。CMake/Ninja、执行器及 vcpkg 的完整 release 报告链尚未迁移，既有 CLI 和报告 schema 均保持兼容。

[验证记录](vcpkg-toolchain-inputs-2026-09-11.json)：固定、断网、非 root、无 Docker socket 的工具容器中 config 1335 项、277.919 秒及 packaging 40 项、1.300 秒全部通过，无跳过；四项 locked validators、三个 renderer `--check`、shell syntax 和 actionlint 通过（仅既有 concurrency.queue 兼容例外）。最终定向 70 项回归通过；固定 Rocky 8 platform-python 3.6.8 编译两个变更运行文件，新增十三项测试全部通过。Rocky 图测试读取同轮工具容器保存的真实 Bake print，仅替换测试取图入口。

新增测试使用合成工具链报告，但认证组件、构造预期策略和验收报告均执行真实域代码；两项 CLI dispatch 测试显式 mock 资格函数。覆盖两架构完整 release 与组件策略入口结果相同、错误 pin/缺失投影/篡改源码、策略格式、混合新旧身份、ABI/runtime/marker、报告 symlink、不完整 CLI 选项，以及只带五个运行模块和八份投影的裁剪目录。没有执行目标编译器、运行时或 vcpkg port 资格；源码输入图也不代表新执行证据。

对规范化 `0b39c2a` 与本批源码解析实际 Bake 图：34 个原始编译组件和 3 个 GCC 正式资格阶段闭包保持不变，113 份组件文档原字节不变；五个 vcpkg 阶段因配方和模块范围更新而改变。单改 `python_row_contract.py`，旧实现五个 vcpkg 阶段均失效，新实现均不受影响。单改产品版本，旧、新实现仍均使这五个阶段失效，明确保留完整 release 迁移的后续工作。此处是材料范围实验，不能用本地回归耗时预测 GitHub runner 改造收益。

首轮定向检查中，新图测试误将组件 scope 名 `build` 当作材料角色，改用实际允许的 `qualification` 后通过；生产材料解析器未变。补齐 CLI 完整性回归后重新完成定向、Rocky 和完整套件。下一步迁移 vcpkg SDK 的宿主工具与执行器输入，以及契约/三层报告的配置绑定，再测材料范围和真实资格；严格物理环境匹配仍未放宽。

主 CI qualified-row/SDK 调用、精确恢复、真实 GitHub 信任/重试、新报告链及源码 Docker 重放、候选/原生 ARM、引用保留策略和性能实验仍待完成。此前自动审批拒绝容器挂载主机 Docker socket（广泛 daemon 控制），明确授权仍待答复；本批未重试或绕过。未合入 main、推送或发布。

## 批次 3/6：vcpkg 五阶段输入与报告绑定（2026-09-11）

新增标准库 `vcpkg_policy.py`，SDK 模式从 build 根及两个独立工具链资格根认证工具链、Ninja/CMake 来源与实际二进制身份；契约和 tier1–3 模式沿前序根推导同一组输入。SDK 与工具链资格必须绑定相同 build，CMake 与 SDK 必须绑定相同 Ninja，宿主工具必须绑定相同 runtime。读取完整 tier3 策略只需要三个运行模块与十七份投影；既有来源、integration 和各层 fixture/asset 文件继续由原资格函数认证。

五个 Docker 阶段均使用 `--components`、六个明确复制的运行模块和持久化的 `/opt/crossforge/qualification/vcpkg/inputs`，不再复制完整 release、schema 或 renderer。新 schema 2 报告绑定该层输入策略，严格限制顶层字段；后序阶段认证前序报告，契约重验实际工具链报告并核对 SDK 记录的原文件 SHA256，tier2/3 另外认证提供 patchelf 身份的契约报告。CMake/Ninja 安装文件、来源、五套 triplet、port 源码与显式 QEMU 执行检查保留。

原 `--release` CLI 继续生成 schema 1 和精确完整 release 身份，兼容消费者从完整 release 独立推导新前序报告策略。`packaging-sdk` 在自己的边界复制当前 release，避免依赖已经移除的 vcpkg 继承文件。完整 SDK 独立推导 vcpkg SDK 策略、检查 scoped 或精确匹配的 legacy 报告，schema 2 集成结果记录原 vcpkg 报告文件 SHA256。配置 binding 不是正式执行 receipt，不放宽物理环境匹配或取消最终集成。

[验证记录](vcpkg-policy-inputs-2026-09-11.json)：固定、断网、非 root、无 Docker socket 的工具容器中 config 1359 项、282.052 秒及 packaging 40 项、1.295 秒全部通过，无跳过；四项 locked validators、三个 renderer `--check`、shell syntax 和 actionlint 通过（仅既有 concurrency.queue 兼容例外）。最终定向 80 项、6.905 秒通过；固定 Rocky 8 platform-python 3.6.8 编译五个变更运行文件，37 项回归、5.122 秒通过。Rocky 图测试只替换取图入口，使用同轮工具容器生成的实际 Bake print。

真实 Bake print 与规范化源码材料比较 `83fed59` 和最终实现：34 个原始编译组件、3 个 GCC 正式资格阶段的闭包保持不变，113 份生成配置逐字节不变。五个 vcpkg 阶段随实现变化而更新；单改产品版本或 cp39 source digest，旧实现五层均失效，新实现均不受影响。单改 Python 行模块，两者均不受影响；单改 QEMU executor 或 Ninja binary identity，新实现仍使五层全部失效；只改 tier3 consumer，则只影响 tier3。最终格式收紧后重新执行全部材料实验，没有把图与输入范围观测写成 BuildKit 执行或性能改善。

新增回归使用合成工具链/vcpkg 报告，组件认证、策略推导、binding 与既有工具链报告核验执行真实代码；producer dispatch 中实际已安装工具、来源、asset/archive 和 port/consumer 执行是显式 mock。实际运行过的原域回归仍包含在全量套件内。这些测试不构成新目标执行、vcpkg 锁定源码资格或新 SDK 资格证据。

首轮全量通过后，手工复核发现 packaging 隐式继承 release 的依赖，增加边界及裁剪目录回归并修正。裁剪测试最初将脚本放在临时根目录，而生成器按实际 `scripts/` 布局查找 validator；改用 Docker 一致的目录结构后通过。随后新增 schema 2 顶层未知/缺失字段拒绝，并再次完成最终定向、Rocky、全量和材料验证。版本 pin、ABI/GCC baseline、生成配置和交叉构建中禁止目标执行的限制未变。

主 CI qualified-row/SDK 接入、精确恢复、真实 GitHub 信任/重试、新报告链和源码 Docker 重放、候选/原生 ARM、按引用保留及性能实验仍待完成。此前自动审批拒绝容器挂载主机 Docker socket（会授予广泛 daemon 控制），明确授权仍待答复；本批未重试或绕过。跨 runner 物理环境边界也仍待决定。未合入 main、推送或发布。

## 批次 5：完整 SDK 输入的固定引用恢复（2026-09-11）

新增 `python_sdk_recovery.py`，在既有 raw recovery schema 1 外封装 SDK 恢复记录，逐行固定六份资格 catalog/artifact digest、输入与 receipt SHA256 以及原始 producer。原 raw schema 的角色范围保持不变，不能使用 raw producer 授权资格。SDK 记录按原依赖发现逻辑要求两份共享工具链、六行各五份原始组件以及六份完整行资格，不接受缺失、额外或混同行身份。

`acquire-python-sdk` 和 `execute-python-sdk-catalog` 新增显式记录与恢复选项。所有输入完整就绪后、最终集成之前保存记录及独立 canonical SHA256；仅部分取得输入时不生成完整恢复记录。恢复必须使用新数据/诊断目录、同一干净 Git 提交、SDK 根、来源材料和完整物理执行环境；raw binder 与行 reader 收到固定 catalog 引用，继续执行原签名、产物、安装文件和资格记录验收。固定引用缺失或与原 producer/输入/receipt 不同均失败，不静默换新组件或补跑资格。最终集成仍调用原 executor，失败时保留取得阶段的记录，成功之前再次核对恢复摘要、当前源码和环境。

[验证记录](sdk-component-recovery-2026-09-11.json)：固定、断网、非 root、无 Docker socket 的工具容器中 config 1376 项、284.673 秒及 packaging 40 项、1.265 秒全部通过，无跳过；四项 locked validators、三个 renderer `--check`、shell syntax 和 actionlint 通过（仅既有 concurrency.queue 兼容例外）。定向 62 项、6.778 秒通过；固定 Rocky 8 platform-python 3.6.8 编译三个变更运行模块并运行十六项新增回归、2.926 秒通过。Rocky 使用同轮工具容器保存的真实 Bake print，仅替换测试取图入口；其最小镜像不带 Git，另一个真实 Git 测试在工具容器的定向及完整套件中执行。

新增十七项回归覆盖严格 schema/角色/registry/producer、独立文档摘要、原始运行身份保留、32+6 集合、所有固定引用转发、缺失/替换不得回退、部分取得不能生成完整记录、失败集成后的同选择重试、前后源码/环境变化和 CLI 成对参数及 symlink 拒绝。Git 清洁状态用例在工具容器内创建真实临时仓库、提交并修改文件，实际调用 Git。另一个用例使用真实 SDK Bake 图及原材料捕获函数，验证完整 host 身份进入恢复材料摘要；只 mock Git revision 发现。

SDK 编排测试的 registry、签名、域产物验收、实际执行环境探测和最终 integration 是显式 fixture/mock，源码状态在这些用例中也是可控 fixture；旧 reader/信任与恢复域回归仍在全量和定向套件中运行。没有取得新的 GitHub 签名、跨 run 下载、目标执行或候选证据。初次定向运行中，原 SDK fixture 的部分 raw subject 是空占位，补齐测试需要的 receipt/layout/hash 后重测；同时修正了指向不存在测试模块的命令。未放宽生产摘要校验。

本批未改动 Docker 配方、生成配置、版本 pin 或 ABI/GCC baseline。SDK 自动目录消费和恢复尚未接通 main/候选；行 producer 的跨 run 取得与完整工作流恢复仍待集成。实际新报告链、源码重放、GitHub 信任/重试、候选/原生 ARM、引用保留和性能实验仍待验收。跨 runner 资格环境边界及此前 Docker socket 自动审批拒绝后的明确授权仍待答复；本批没有挂载 socket 或绕过拒绝，未合入 main、推送或发布。

## 批次 3/4/6：主 SDK 目录消费与同 worker 补验（2026-09-11）

主 SDK job 改用 `run-component-sdk` 和标准库控制器 `ci_sdk.py`，由现有 acquisition 取得两份工具链、三十份 Python 原始组件及六行资格。严格匹配的签名行保留原 producer；只有资格输入索引缺失时，才在 SDK 所在 worker 补做该行双目标完整资格。缺少原始组件、验签、传输或证据失败均停止，不隐式重编译 GCC/CPython 或把错误当作缓存缺失。六行全部通过后，继续由原 SDK executor 独立检查 receipt/实际文件并强制执行 append/final 集成。

跨 runner 的物理环境边界尚未确定，本批按已明确告知的过渡假设保留全部严格环境字段。父进程最多调度两个独立 Python 子进程，共享原 builder；每个请求绑定独立 canonical SHA256，子进程前后及最终集成前后核对源码、调用与执行环境。最初实现直接在线程中调用资格函数，手工复核发现其既有动态模块加载使用 `runpy.run_path`；依据 [Python 官方文档](https://docs.python.org/3/library/runpy.html) 的共享解释器状态限制，改为独立进程后重新完成最终检查。并行度仍为 2，未进行 2→3 实验。

SDK job 权限继续为 contents/read 与 packages/read，新增行只保存在本 job，不发布或签名。补做资格和集成之前保存原 raw schema 的 32 份固定选择及独立摘要，上传诊断后的 summary 提供 run/artifact ID、canonical SHA256 和路径。原 raw recovery 接口可在自身严格约束下取得这些原始组件；该记录不包含新执行的行资格，不能替代完整 SDK 恢复记录。OCI blobs 与安装树不进入诊断工件，CLI 保留心跳、资源采样和失败退出记录，并拒绝覆盖已有 run 诊断。

增量选择器明确识别 SDK 控制器、目录消费与恢复实现的变更，选择 canonical SDK roots，在原因中单列 `orchestration_files`。实际源码材料差异仍保留自身解释，不能把纯编排变化声称为 compiler inputs 变化；混合未知路径继续全量兜底。现有独立 Python job、原 job needs/timeout、权限和 required status 检查保留，可能与 SDK worker 的补验重复。`produce-python-row.yml` 仍未接入主 matrix。候选前置资格复用此工作流，因此也使用新 SDK job；候选镜像发布本身仍走原始组件路径。

[验证记录](main-sdk-integration-2026-09-11.json)：固定、断网、非 root、无 Docker socket 的工具容器中 config 1395 项、291.129 秒及 packaging 40 项、1.151 秒全部通过，无跳过；四项 locked validators、三个 renderer `--check`、shell syntax 和 actionlint 通过（仅既有 concurrency.queue 兼容例外）。最终定向 59 项、9.019 秒通过；固定 Rocky 8 platform-python 3.6.8 编译三个生产文件并执行十七项新增回归、5.511 秒通过。Rocky 图入口读取同轮工具容器保存的实际 Bake print。

新增 SDK 编排测试执行真实 acquisition 控制流、SDK 图验收、raw recovery contract 和子进程请求解码，显式 mock registry/域产物验收、GitHub 来源与物理环境观察、资格生产及最终执行。成功子进程调度在 fixture 中转入真实请求处理，另一个负向测试在工具容器与 Rocky 中实际启动独立 Python 进程，确认请求被修改时在访问源码/Docker 前拒绝。没有声称取得新的实际行 BuildKit solve。回归覆盖精确 32+6 集合、只补缺失行、两个并发槽、保留原 producer、失败及输入/环境漂移不写成功、原始恢复选择留存、诊断目录保护和工作流权限/门禁约束。

首轮定向失败包括旧 raw subject fixture 字段不全、错误的参数位置及复用诊断目录，修正 fixture 后增加生产目录拒绝及回归。首轮完整套件和线程版本通过记录另行保留；最终记录以改为独立进程、加入原始恢复记录和实际子进程负向用例后的定向、Rocky 与全量运行作为依据。未改 Docker 配方、生成配置、版本 pin 或 ABI/GCC baseline。

实际新报告链与源码 Docker 重放、GitHub 签名/跨 run 取得/重试、候选与原生 ARM、主 SDK 完整恢复、按引用保留及三次基线/受影响变更/并行度实验仍待验收，未宣称 CI 耗时或磁盘改善。此前自动审批拒绝容器挂载主机 Docker socket（广泛 daemon 控制），明确授权仍待答复；本批未重试或绕过。未合入 main、推送或发布。

## 验收准备：独立行门禁与当前源码原始组件（2026-09-11）

复核主 SDK 与独立 Python job 后，不能仅凭完整 SDK 含六行就删除独立行 job。实际 Bake 图中，六个 `python-<row>-dev` 均从 `sdk-toolchains-dev` 开始；累计 SDK 只有首行 cp313 使用相同基座，其余五行分别继承前一行。这些独立安装门禁与累计组合覆盖不同。后续消除重复资格时应交接已验收行产物，再继续执行原独立安装及累计最终门禁，并由 required status 明确核对承接关系；本轮没有修改任务选择或删减门禁。

为先验收已经变更的实际报告链，从干净 `cb115b51a3038a9d84c68c0e98a2f255379da4f7` 导出规范化源码。固定、断网、非 root、无 Docker socket 的工具容器重新解析当前 SDK、GCC context 与独立行 Bake 根，用原记录中的 build identity 比较材料，并对四份工具链/GCC 原始组件和三十份 Python 原始组件运行真实 OCI descriptor、manifest/config/layer 字节校验。34 份输入均与原 receipt 精确匹配，receipt pin 另与已提交的历史观察记录核对。[完整结果](runtime-replay-preflight-2026-09-11.json)保存逐项身份与来源。

这次检查未观察新的 daemon/物理环境。Python 的依赖图绑定只在此次材料捕获中替换了需要 BuildKit 提取的验证回调；替代回调实际核对 receipt、预期输入和 OCI blobs，但没有验收嵌入 contract 或安装树，不能据此授权消费。实际执行器仍必须完成这些原检查。所有旧行资格均从准备的重放输入中移除，新报告链要求重新执行；既有原始组件没有因这批资格代码变化而需要重新编译的材料差异。

新的重放目录 `/tmp/crossforge-runtime-replay-cb115b5/` 固定源码、1,007 项源码清单、执行脚本和输入文件摘要；`run.py --check-only` 已在无 socket 的只读 Docker 容器中实际通过。待执行阶段包括双工具链、双 GCC smoke/x86_64 full、六行 Python 及两种 SDK 集成，各阶段只使用原本地资格 API、新目录和真实 local producer。SDK 只接受该清单中已经成功的新行结果；源码或实际环境变化不能写成功。`run-authorized.sh` 是尚未执行的具体命令，使用 `crossforge-ci-review`，不操作其他 builder、不发布或伪造 GitHub 身份。

此前自动审批因广泛宿主 daemon 控制拒绝挂载 Docker socket，本轮已就这份具体命令请求明确授权。未重试挂载，也未改用其他 daemon 入口。独立行安装、vcpkg upstream 强制重放、源码重建、真实 GitHub/候选/原生 ARM、恢复保留和性能验收仍是完整目标的一部分；准备清单不是资格通过记录。未改实现、生成配置或版本/ABI/GCC pin，未合入 main、推送或发布。

## 已获授权：当前源码的实际 Docker 资格重放（2026-09-11，进行中）

用户明确同意前一节准备的 Docker socket 挂载与本地验证，之前的授权阻碍已经解除。继续使用 `crossforge-ci-review` 和固定工具镜像，只读消费规范化的 `cb115b5` 源码归档。未操作其他 builder，未更改版本或 ABI/GCC baseline。前述章节中的“授权仍待答复”是当时的历史状态。

[运行记录](runtime-replay-2026-09-11.json)保存当前已经完成的四个阶段及独立结果摘要：x86_64 工具链 20.567 秒、2 个 fresh RUN；AArch64 工具链 33.961 秒、5 个 fresh RUN；x86_64 GCC smoke 94.707 秒、2 个 fresh RUN、16 PASS；AArch64 GCC smoke 282.492 秒、3 个 fresh RUN，locked-sysroot 与 clean-rocky 各 16 PASS。原生产器执行后，原独立 consumer 重新核验 OCI、嵌入契约、报告、执行记录及当前输入，源码和实际执行环境前后匹配才写成功。

首次启动发现 namespace 报告的 socket 组与宿主实际组不符，随后使用宿主组 989；第二次临时脚本的 local producer URN 多出冒号，被原 validator 正确拒绝，修正临时脚本后才执行。AArch64 首轮因外层工具容器断网，无法获取固定 QEMU 镜像的 registry token；外层改用 bridge 后通过。资格 RUN 原有的断网规则、镜像 digest、严格 producer 和证据校验均保留，失败目录独立保存。

cp39 已完成静态资格、双目标 locked/clean 运行及行汇总，但外层独立产物复核尚未结束；GCC full 仍在执行，不记为通过。builder 的 worker 并行上限保持 1；这两个阶段会互相等待，外层工具容器的 CPU/内存限制也不约束外部 BuildKit worker。因此表中时间包含取得、提取、测试、封装和复核，不作为 GitHub runner 优化幅度或三次性能基线。

接着完成其余五行、新 SDK 汇总以及六个独立 Python 安装门禁。独立安装准备脚本保留各行原来的 `sdk-toolchains-dev` 基座，只在完整行 receipt 经原验证器验收后交接产物，并要求原 append 的两个 RUN 新执行；当前仅通过无 socket 的准备检查，还没有取得安装通过结果。完整目标仍需源码/vcpkg 重放、真实 GitHub/候选/原生 ARM、恢复/保留和性能验收；未合入 main、推送或发布。

## 批次 4/6：SDK 成功行的中间目录占用（2026-09-11）

实际 cp39 封存目录保留了两份解压安装树，`du` 各约 669 MiB，而用于后续消费的 OCI 约 163 MiB。`ci_sdk.fresh_row` 现在先由原 producer 完成资格、封存和产物验收，确认 receipt 对应计划输入、产物与 producer，并由原诊断保存逻辑复制报告和执行记录，随后才清理本行 `payload/` 与 `extracted/`。OCI、receipt、输入记录和诊断继续保留，SDK 的原独立验收不变。资格执行失败、输入不匹配或诊断复制失败不清理；删除前同时检查两个 staging 根，拒绝符号链接根，内部安装符号链接不指向删除目标。

[本地验证记录](sdk-staging-cleanup-2026-09-11.json)：固定、断网、非 root、无 socket 的工具容器完成 config 1,398 项、365.790 秒及 packaging 40 项、1.773 秒，全部通过且无跳过；定向 47 项、10.402 秒与固定 Rocky 8 Python 3.6.8 的 20 项、7.837 秒通过。四个锁定验证器、三个 renderer 与 shell syntax 通过。完整驱动最后因遗漏 actionlint 二进制退出 1，单独挂载既有工具后静态检查通过，只保留既有 concurrency.queue 兼容例外，没有重跑已经通过的测试。定向初次还分别遗漏 Docker CLI 和拼错相邻测试模块名，补全测试环境及正确模块后通过；没有放宽生产校验。

新增回归实际操作临时文件和符号链接，覆盖成功清理但保留 OCI/receipt/诊断、资格与输入失败保留现场，以及诊断复制失败阻止集成；registry、产物生产和 SDK 执行仍是显式 fixture。另取本次实际 cp39 封存产物的独立副本，调用新清理函数移除 1,300,195,598 字节常规文件，原重放目录不变；原 `python_qualification.verify_local` 随后验收保留的 OCI，目前仍在等待同一 builder 的 GCC full 长步骤，未写通过结果。该探针不是新的行资格执行。

GCC full 的 C++ 套件已完成，实际汇总含 232,928 项 expected PASS；C 套件仍在运行，四套件最终基线比对尚未完成。cp39 外层独立验收、上述清理后的独立验收及后续行/SDK 仍待继续。所有测试计时都与当前 GCC full 重叠，不能作为并行度实验或性能改善比例。此次清理仅减少本 job 已完成行的重复安装副本，未实现 registry 引用保留、完整恢复或去重独立 Python job，也未合入 main、推送或发布。

## Docker 验收进展：GCC full、前三行与独立安装（2026-09-11）

GCC full 已完成四个套件、封存及原独立 consumer 验收，外层进程退出 0。[运行记录](runtime-replay-2026-09-11.json)中绑定两个 fresh RUN、454,053 PASS、3,367 XFAIL、5,902 UNSUPPORTED 和既有 87 FAIL；原 verifier 精确比较固定 baseline 的状态、套件、身份与出现次数，没有新增或消失的异常记录，也未修改 baseline。完整本地阶段 3,392.617 秒，其中资格 solve 区间 3,300 秒。

cp39、cp310、cp311 整行均已完成双目标 static/locked-sysroot/clean-rocky、行汇总、OCI 封存和独立安装树验收，各有 7 个 fresh RUN。实际封存报告分别核对其摘要及 compile 5、runtime 4、target final 5、row 3 格式，确认走过新的 scoped 报告链。cp39 的资格 solve 为 287 秒，但外层耗时 3,678.896 秒，包含随后等待 GCC full 释放同一 worker；cp310 外层 1,011.199 秒、cp311 358.596 秒也包含取得和复核，不能用它们计算 CI 提速比例。

cp39 与 cp310 又分别按原 `python-<row>-dev` 根完成独立安装，使用 `sdk-toolchains-dev` 基座，各有两个新执行的 append RUN，产出的行清单字节与原 qualified row 完全一致。输入捕获要求只消费两份原始工具链与一份已独立验收行，且不含 GCC/CPython 自身源码编译入口；没有删除原主工作流的独立行门禁。cp311 独立安装已经启动，后续行资格顺序推进，Python 同时最多两行，worker 并行上限仍为 1。

[SDK 清理探针](sdk-staging-cleanup-2026-09-11.json)的实际 OCI 独立验收也已退出 0：删除独立副本中 1,300,195,598 字节中间文件后，原 consumer 从保留的 170,353,262 字节 OCI 中核验实际文件、报告与执行记录，所得 qualification 与原 cp39 成功结果逐字段完全一致，receipt SHA256 和原 producer 保留，原重放目录完整。该探针没有重跑行资格，时间包含排队，不作为清理性能基线。

当前继续 cp312–cp314 及剩余独立安装，之后执行两类 SDK 汇总。只读远程检查确认 main 仍为 `cf736eab8aa53b851874509d66676e5bac98dc27`。源码/vcpkg 重放、真实 GitHub 信任/重试、候选与原生 ARM、完整恢复/引用保留以及三次基线和资源实验仍待完成；未合入 main、推送或发布。

## Docker 验收发现：本地 OCI 导出停顿与有限重试（2026-09-11）

cp312、cp313 后续整行资格及 cp311–cp313 独立安装均已退出 0，因此本次旧源码重放目前有五行完整资格、五个独立安装通过。cp314 完成七个新执行的资格 RUN 和行汇总后，在封存 OCI 的本地解包导出中停止进展，没有生成最终 receipt 或阶段成功结果。该导出客户端、builder 均接近空闲，工具容器使用约 1.348 GiB / 16 GiB、无 OOM，部分文件保持 257,427,381 字节；同一固定 OCI 的独立限时导出却在 19.7 秒完成。仅对核实过的原 Buildx 客户端发送 SIGQUIT 保存堆栈后，原 batch 退出 1，全部原目录和日志保留。堆栈包含大量 fsutil 文件接收等待；尚未认定具体上游根因，未改 Buildx/BuildKit pin。

`local_export.py` 现在为本地元数据和整行导出提供每次十分钟上限，只对超时重试一次。超时终止该 Docker 客户端的整个进程组，并清理仍存活的 Buildx 子进程；两个尝试各有独立目录、相同固定 OCI 输入和仅含 COPY 的配方。普通导出错误不重试，第二次超时仍失败。成功目录随后经过原字节、契约、报告和安装树验收。行 CI 在 SDK 正常中间目录清理之前保存导出配方和超时记录，不复制大安装树。该改动不能将旧的 cp314 失败阶段补记为成功。

[修复验证记录](local-export-timeout-2026-09-11.json)：固定、断网、无 socket 工具容器通过 config 1,405 项 / 315.362 秒、packaging 40 项 / 1.327 秒，四个锁定验证器、三个 renderer 和 shell 检查通过。完整驱动只在最后 actionlint 因测试用 Git archive 缺少 `.git` 无法发现项目而退出 1；在该临时副本初始化 Git 后静态检查通过，保留既有 concurrency.queue 兼容例外。最终补充实际超时秒数记录和诊断保存后，相关模块 78 项 / 9.257 秒、固定 Rocky 8 Python 3.6 的 8 项 / 1.072 秒通过，无跳过。新增用例实际启动父子进程，验证父进程先退出时仍清理忽略 SIGTERM 的子进程，另验证失败目录隔离、仅超时重试、重试耗尽、固定输入以及 SDK 清理后的诊断保留。

同一实际 cp314 OCI 又通过新函数完成一次受控超时恢复：首轮临时注入 1 秒限制，第二轮使用生产十分钟限制；35.992 秒内完成导出及原安装树/报告验收，覆盖与旧 producer 生成的资格内容一致。记录明确区分这次传输与文件验收和新的资格执行，原 cp314 仍无成功 receipt。因 Python 资格实现文件发生变化，后续会对新源码取得新的当前输入行 receipt，既有历史结果不改写。SDK 汇总尚未启动。

同一 OCI 的另一条元数据调用路径也完成实际导出，4.056 秒内逐项比较七份契约、执行记录和报告的字节摘要及文件模式；与原封存目录完全相同。该检查覆盖通用元数据图和整行导出图的两个实际调用入口，没有生成新资格记录。

另外已准备两种 SDK 汇总之后的 vcpkg 锁定源码资格重放与 x86_64 工具链/cp39 编译步骤强制重建脚本，通过断网容器中的固定输入与原策略接口检查，尚未执行实际重放。跨 GitHub runner 环境兼容策略已单独提出，当前继续逐项匹配原宿主边界；[GitHub 官方文档](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job)说明常规托管 job 使用新的 runner 实例，故不能假定不同 job 的物理环境身份相同。未合入 main、推送或发布。

## 候选发布诊断与新版行重放（2026-09-11）

只读核对旧 main 的候选运行 `34478167419`：`publish` job 在资格阶段之后失败，SDK 构建日志重新进入 AArch64 GCC prepared-source 阶段，最终 Dockerfile frontend 退出 2。原始 job 日志与摘要记录在[候选诊断观察](candidate-publication-diagnostics-2026-09-11.json)。未从该日志确定 frontend 的根因，也未把 Qt cache miss 认定为原因。这是远程 `cf736ea` 的旧工作流，不能用于证明当前 topic 分支行为。

当前候选已有原始组件交接和分阶段 checkpoint，本次只补齐 source-publication 与 sdk-publication 的完整构建日志、每分钟心跳/日志大小、无论成功失败都执行的 BuildKit 诊断与独立工件。工件使用 run/attempt 名称保留 90 天，并在存在时包含 Buildx metadata。构建退出码继续决定门禁；诊断不构成发布或资格通过证明，runner 丢失时仍可能来不及上传。

固定、断网、非 root、无 socket 的 Docker 工具容器通过相关六个模块共 51 项回归 / 8.145 秒，无跳过；candidate actionlint 和诊断 shell syntax 通过，仅保留既有 concurrency.queue 兼容例外。新增故障注入执行实际工作流 shell 与原心跳包装，只将 Docker 替换成写出 stdout/stderr 后返回 2 的命令，确认两个发布步骤都保留完整日志且返回 2。首轮测试的 GitHub 表达式替换遗漏字段名数字，修正 fixture 后重跑上述全部模块；未调整生产行为来绕过错误。未改 Docker 配方或资格实现，不影响正在运行的固定 a56bb29 源码归档。

[新版本地重放](runtime-replay-a56bb29-2026-09-11.json)已确认 cp314 与 cp39 两行的完整资格、OCI 封存与原独立 consumer 验收通过，各七个 fresh RUN；两个独立安装也通过，各两个 fresh append RUN。旧 cb115b5 batch 保留为 cp314 导出失败后停止，没有追写成功。本次 batch 正顺序推进 cp310–cp313，然后执行两种 SDK 汇总和已准备的 vcpkg/源码强制重放。所有计时仍是本地功能观测，不是 GitHub 并行度或三次性能基线。

跨 runner 环境策略尚未收到新选择，继续严格比较全部物理环境字段。主行签名 producer、完整 SDK 恢复、候选资格复用、真实 GitHub/原生 ARM、引用保留及性能验收仍需完成；没有合入 main、推送或发布。

## 批次 3/4/5：主 Python 行的签名交接与独立安装门禁（2026-09-11）

主 `python` matrix 现在调用既有 `produce-python-row.yml`，由计划输出明确的行集合，保留两行并行以及原 inputs/toolchains/raw-Python 依赖。最终 required status 比较完整计划和行 matrix，不能以缺失、取消、失败或意外 skipped 代替成功。调用方授予 reusable workflow 的最大权限，内部仍将 producer 的 packages/write、signer 的 id-token/write 和 store 的 packages/write 分开；普通 SDK 消费 job 保持只读。

新增 `python_row_install.py` 将先前实际验证过的独立安装路径纳入正式接口。每行无论新取得还是复用资格，都重新核对七份原始输入和完整行 receipt/OCI/文件，再交接到原 `python-<row>-dev`。基座必须是 `sdk-toolchains-dev`，不得换成累计 SDK；两个原 append RUN 必须本次执行，安装后的 row manifest 必须与已验收行一致。失败阻止新产物发布、签名 handoff 与 job 成功。累计 SDK 的六次 append 和 final 仍另行执行。新安装控制器的单独变更会选择各独立行，不能宣称 compiler inputs 变化；未知路径仍全量兜底。

[验证记录](main-python-row-integration-2026-09-11.json)：固定、断网、非 root、无 socket 工具容器中配置 1,419 项 / 412.167 秒与打包 40 项 / 1.759 秒全部通过，无跳过；定向 101 项 / 20.732 秒通过。四个锁定验证器、三个 renderer、shell 与全部 workflow 的 actionlint 通过，仅保留既有 concurrency.queue 兼容例外。固定 Rocky 8 Python 3.6.8 编译四个生产模块并通过 46 项回归，测试主体 4.574 秒；Bake 图来自同轮固定工具容器的实际 print。图与编排用例使用显式 OCI、执行环境、BuildKit 和 registry/signing fixtures，不将这些回归计作真实资格执行。

实际正式安装接口另消费本轮已验收的 cp39 行，完成原始组件和资格产物复核、两个 fresh append RUN 与行清单比较，外层退出 0，213.394 秒。捕获材料只有两份工具链与一份 qualified row；实际执行日志没有 GCC/CPython 源码编译 RUN。源码是基于 `fb836c5`、含本批实现的固定规范化工作副本，记录明确标为 source_dirty=true 并固定全部目录模式及文件字节/模式，未伪造 GitHub 或 clean producer。原行 receipt 与 producer 保留，未创建新行资格。首轮临时包装脚本混用物理/逻辑路径，在访问 daemon 前失败；第二轮副本目录为 0775，与原始组件的 0755 不符，原 verifier 正确拒绝。两次现场保留，第三轮使用新的规范化副本，不修改验证器或旧 receipt。

新版 a56bb29 的六行完整资格现均已通过，各七个 fresh RUN；六个独立安装也均已退出 0，各两个 fresh append RUN。顺序 batch 已启动 python-dev 汇总，随后继续 sdk-complete-dev 及 vcpkg/源码强制重放。主行签名工作流已完成代码接入，但实际 GitHub 发布/签名/跨 run 取得、完整 SDK 恢复、候选资格交接与原生 ARM、引用保留和性能验收仍待完成。严格物理环境不匹配时，SDK 仍会补验；没有宣称跨 runner 已去重或 CI 已提速。未合入 main、推送或发布。

## 批次 4/5：Python SDK 实际汇总与主 SDK 完整输入记录（2026-09-11）

新版 `python-dev` 已完成原 executor 的全部输入验收与集成，外层退出 0、577.654 秒：六行重新核验 receipt/OCI/实际文件，十二条累计 append 与一条 final RUN 本次执行。捕获图只有两份工具链和六份 qualified row 共八个组件依赖，图及实际执行日志均未出现 GCC/CPython 源码编译入口。六份汇总 row manifest 与原资格产物一致；最终报告为 passed，覆盖六个 build Python、六个双目标 Python 行和两个目标编译器，并记录固定 QEMU。摘要和原报告引用已纳入[本地重放记录](runtime-replay-a56bb29-2026-09-11.json)。这不是候选或原生 ARM 通过记录，计时仍不是 GitHub 性能基线。顺序 batch 已进入 `sdk-complete-dev`，后续 vcpkg/源码重放仍待完成。

主 SDK acquisition 现在请求完整恢复记录：只有三十二份原始组件与六份行资格全部来自已认证目录，才在最终集成前保存 32+6 文档。单独的 `sdk-recovery-reference.json` 固定摘要并在工件 summary 指明 `execution/acquisition/component-recovery.json`，原 raw-only 记录继续另行保留。最终集成成功后，主 SDK 与显式 catalog executor 共用完整记录、当前源码/根/材料和实际物理环境的复核函数。集成失败保留原选择；SDK 自行补验的未签名行不会产生完整记录或完整恢复 summary。

[本批验证](main-sdk-complete-checkpoint-2026-09-11.json)：固定、断网、非 root、无 socket 的 Docker 工具容器通过受影响的 SDK、目录、恢复和增量模块共 69 项 / 25.469 秒，无跳过；固定 Rocky 8 Python 3.6.8 编译两个生产模块并通过 48 项 / 23.645 秒，图入口使用固定工具容器实际 Bake print。main/candidate 调用方 actionlint 通过，只保留既有 concurrency.queue 兼容例外。前一批全量 1,419+40 项验证单独保留，本批只改编排、诊断和相应回归，没有重复未受影响的全量套件。新 summary 用例实际在独立 Python 进程执行 action 中的脚本，验证仅 raw、完整记录和工件缺失三种输出；源码/registry/签名/集成边界在编排测试中仍为显式 fixture。首次篡改用例误用只允许新文件的生产 JSON writer，被正确拒绝覆盖；改为测试直接篡改文件后，完整回归确认集成后的摘要复核会拒绝该变更。

该接入使已认证完整输入可通过既有显式 SDK catalog 恢复接口重新取得，尚未实现主 job 的自动恢复入口或未签名本地行的持久恢复。跨 runner 环境规则仍保持全部严格字段，实际 GitHub 信任/重试、候选资格复用与原生 ARM、引用保留及三次性能/受影响变更验收仍待完成。未合入 main、推送或发布。

## Docker 验收：完整 SDK 汇总通过（2026-09-11）

新版 `sdk-complete-dev` 已退出 0，777.453 秒完成输入验收、累计安装和最终集成；14 条要求本次执行的原 RUN 均通过执行记录核验。捕获依赖仍是两份工具链和六份 qualified row，图及实际 RUN 名称中没有 GCC/CPython 自身源码编译入口。最终报告为 passed，覆盖 24 个 Python/架构/链接方式组合、x86_64 与 AArch64 的 DEB/RPM 打包，以及两份 launcher 消费者交叉编译；launcher 样例明确记录未执行，不作为原生目标运行证据。

固定、断网、只读 Docker 工具容器随后使用原 `python_sdk.fresh_vertices` 与输入/文件校验接口独立复核两种 SDK 的已保存结果：13/14 条 fresh RUN、各八份组件依赖、六份原 row manifest 和全部导出报告摘要均匹配。完整 SDK 的 Python 最终报告还与先前 `python-dev` 导出字节一致。详见[本地运行记录](runtime-replay-a56bb29-2026-09-11.json)；这次独立复核没有生成新的资格执行或改写原 producer。

顺序 batch 继续执行 vcpkg 五阶段锁定源码资格重放，之后运行 x86_64 工具链与 cp39 的强制源码重建。[验收索引](ci-refactoring-acceptance.md)集中列出六项选择的当前证据和剩余门槛，避免将历史 fixture、本地功能计时或旧源码结果当成真实 GitHub/候选验收。只读核对远程 main 仍为 `cf736ea`。未合入 main、推送或发布。

## 实际增量实验发现：独立 Python 安装仍被完整 release 扩散（2026-09-11）

在独立 `4a730cc` 源码副本中仅改变 cp39 补丁的说明文字、同步 release 中的补丁摘要，再依次运行原 renderer 和锁定验证器。真实 Bake 材料计划正确区分三个 cp39 编译目标，却仍选中全部六行独立安装；另外五行的唯一变化文件均为 `config/release.json`，不是实际编译输入。原始 before/after 源清单和宽范围计划保留，未把这个现象描述成 GitHub 测量。

修复 `python-sdk-append`：只复制本行的 source、build policy、两份 target policy、row root 与 qualification policy 六份投影，renderer 将 row 根摘要同时传入独立和累计 append。复用已有 component 模式的 source verifier 和 row finalizer，保留安装前重复目录拒绝、安装后实际文件/ABI 检查与完整 manifest 字节比较，仍是原来的两个 RUN。quick/host 与最终 SDK 保留完整 release 校验；源码编译和行资格生产配方、ABI/GCC baseline 均未改。修复后同一独立变更的真实计划只选 inputs、cp39 和 SDK，五个其他独立行闭包完全相同。新增回归使用真实 renderer/Bake 与临时源码，而不是简化图替身。

[验证记录](python-append-scope-2026-09-11.json)保留完整配置运行 1,423 项 / 525.094 秒、14 个失败观察：它们全部来自三项仍要求旧完整 release 输入、或只允许 producer 携带 row 根摘要的断言，其中一项含十二个子场景。更新这些测试契约后，全部受影响模块 75 项 / 22.490 秒通过；生产代码在这次完整运行后未再修改。打包 40 项 / 1.890 秒、四个锁定验证器、三个 renderer、shell 与全部 workflow actionlint 通过，后者仅保留既有 concurrency.queue 兼容例外。固定 Rocky 8 Python 3.6.8 的 23 项 / 20.988 秒通过，覆盖裁剪后的 append finalizer 导入、六份行策略和 renderer 接线。没有把分开的复验记录写成一次未发生的全绿完整运行。

由于 Docker append 配方变化，另从固定规范化工作副本启动六个独立安装与两种 SDK 的实际验收；明确标为 source_dirty=true，仍保留原 a56bb29 行 receipt/producer，由原接口重新核验，尚未取得本批新的安装成功结果。此前 a56bb29 的完整 SDK 成功记录保留为历史，不能代替新配方验收。旧源码 batch 的 vcpkg Tier 2 已通过，Tier 3 仍在源码执行；二者共享 max-parallelism=1 的本地 builder，会互相等待。未合入 main、推送或发布。

## Docker 验收：vcpkg 锁定源码资格重放通过（2026-09-11）

固定 a56bb29 batch 的 vcpkg 阶段已完成，外层退出 0、1,355.230 秒。原 replay verifier 确认 contract 与 Tier 1–3 共四条指定资格 RUN 本次执行；五份 SDK/contract/Tier 报告均为 schema 2、passed，并通过原策略绑定检查。每个 upstream tier 包含 host-static 与双目标 static/dynamic 共五种组合。输入图消费两份工具链安装产物，没有 GCC/CPython 源码编译输入；上游 fixture 的锁定源码编译继续执行，不把普通 binary cache 恢复记作新源码资格。

固定、断网、只读 Docker 容器随后独立复核原输入身份、四条 fresh RUN、五份报告的策略绑定及文件摘要，退出 0；[运行记录](runtime-replay-a56bb29-2026-09-11.json)保存原始结果和复核脚本摘要。该阶段是本地强制重放观测，不是新签名资格 receipt、GitHub 性能基线或候选/原生 ARM 证据。顺序 batch 已进入 x86_64 工具链源码重建，cp39 源码重建随后执行；新 append 配方的验收 batch 也仍在推进，未合入 main、推送或发布。

## Docker 验收：x86_64 强制源码与新版 cp39 安装通过（2026-09-11）

固定 a56bb29 batch 的 x86_64 工具链源码重放完成，776.038 秒。原计划指定的 binutils 与 GCC 两条编译 RUN 均本次执行；断网只读 Docker 再次用原 `ci_source_replay.plan` 重算材料计划，并用原 fresh RUN verifier 核对执行记录，退出 0。随后 cp39 源码重放仍在运行，其 AArch64 工具链前置依赖发生普通缓存未命中，确实再次编译 GCC；这个强制源码路径允许未选中前置依赖使用或未命中普通缓存，不把它描述成只编译了 CPython。

新 append 配方的 cp39 独立安装通过，1,304.503 秒包含共享 builder 等待，两条要求本次执行的 RUN 均通过原校验器。安装结果与 a56bb29 原行 manifest 的字节摘要相同，原 producer/receipt 未改，实际捕获输入不再包含完整 `config/release.json`。随后断网 Docker 独立复核输入身份、两条 fresh RUN、manifest 文件模式及摘要和原资格记录，退出 0。其余五行及两种 SDK 尚待此轮实际结果，未拿旧配方成功覆盖新版配方。

[受影响变更验收](cp39-affected-change-acceptance-2026-09-11.json)已启动，使用之前保存的修复后 cp39 补丁说明变更副本，固定运行脚本、51 个输入文件摘要及两个源码清单/文件模式，预检在无网络、无 Docker socket 的固定工具容器中通过。实际流程遵循原 inputs 选择，再做独立源码重放、五份 cp39 原始组件生产、本行七条资格 RUN、独立安装和两种 SDK；后两者使用新 cp39 与另外五行原 receipt，并逐一执行原消费者校验。源码副本明确标为本地 `source_dirty=true`，不会改写仓库正式补丁/版本或冒充 GitHub producer。此 batch 与已有两批共享专用 review builder，当前是功能与增量边界验收，不作为隔离性能基线。尚未合入 main、推送或发布。
