# CI 重构实施进度

目标是完成[六批实施方案](ci-refactoring-plan-2026-09-10.md)，不是仅完成触发器调整。工作分支为 `codex/component-ci-refactor`；研究基线为 `cf736eab8aa53b851874509d66676e5bac98dc27`。

| 批次 | 当前状态 | 仍需取得的证据 |
|---|---|---|
| 1：入口与前置回归 | 本地实现及 Docker 回归通过 | 修改后工作流的真实 GitHub 事件运行 |
| 2：组件契约与工具链交接 | 安装/测试上下文 OCI、cp39 x86_64 构建、x86_64 正式资格 receipt 与独立核验通过 | GCC full 正在执行；ARM、远程交接与 CI 接入 |
| 3：组件级增量计划 | 待实现 | 依赖传播、完整结果汇总和真实受影响构建 |
| 4：整行 Python/SDK 交接 | 待实现 | 双架构全 row、SDK 组装无 GCC/CPython 源码重编 |
| 5：资格复用及恢复 | 待实现 | 显式复用、强制执行、候选集成/ARM 及同 digest 恢复 |
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
