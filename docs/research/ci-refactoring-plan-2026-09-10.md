# Crossforge CI 与内部架构重构实施方案

本方案汇总逐项讨论后确认的六项选择，并将其安排成可独立审阅、验证和回退的实施批次。代码分析基线为 `cf736eab8aa53b851874509d66676e5bac98dc27`。现状研究与有限 Docker 组件试点已完成；当前实现和验证状态持续记录在[实施进度](ci-refactoring-progress.md)。

完整依据见[现状分析](/home/eg/workspace/github/eglinux/crossforge/docs/research/ci-architecture-review-2026-09-10.md)与[观测数据及决策记录](/home/eg/workspace/github/eglinux/crossforge/docs/research/ci-architecture-observations-2026-09-10.json)。本文件描述实施设计，不作为发布资格证据。

## 已确认的六项选择

| 项目 | 已确认的方向 |
|---|---|
| 日常 CI | main 增量验证；手动或准备发布时生成完整候选 |
| 组件交接 | 保留 Docker/Bake，通过固定 digest 的内部组件交接；先试点一套工具链和一行 Python |
| 工作选择 | 按组件及下游依赖选择；区分构建、资格与供应链元数据失效；未知变更全量兜底 |
| 执行资源 | 先优化当前 GitHub runner；本地继续 Docker/Bake；组件交接后重测并行度 |
| 资格结果 | 严格匹配时复用可信原组件报告；每个新候选重新执行最终镜像集成和原生 ARM 验证；保留强制重放 |
| 内部代码 | 保留 Python 标准库和现有 CLI；按领域分批模块化，与 CI 改造配合推进 |

本地试点已经证明：固定 digest 的 x86_64 工具链安装产物可以被全新 builder 消费，cp39 x86_64 构建成功，consumer 图中没有 GCC 源码构建节点。它还没有证明完整双架构 Python row、GCC full 构建上下文交接、完整 SDK 和正式候选流程成立。

## 实施依赖与顺序

| 批次 | 交付物 | 前置条件 | 主要验收 |
|---|---|---|---|
| 1 | main 与候选入口分离、发布输入契约前置检查 | 无 | 普通 main push 不再生成候选；手动候选仍保留完整门禁 |
| 2 | 组件身份与产物清单模块、正式工具链交接试点 | 已有本地可行性实验 | 新 builder 按 digest 消费，材料不匹配或缺失时拒绝 |
| 3 | 组件级增量计划及 CI 动态任务集合 | 批次 2 的身份接口 | 精确选中受影响节点；未选中任务不能掩盖失败 |
| 4 | 完整 Python row 和 SDK 的组件消费 | 批次 2；与批次 3 配合 | 最终组装不再从源码编译 GCC/CPython，完整资格范围通过 |
| 5 | 显式资格复用、强制重放、候选失败恢复 | 完整产物及证据身份已建立 | 可解释复用原因；候选集成及 ARM 实际执行；恢复引用同一产物 |
| 6 | 并行度实测、快速测试优化、持续领域迁移 | 新交接流程可稳定重放 | 时延、资源和 job-hours 有实测对照，产品与资格契约保持 |

领域重构从批次 2 开始，服务于每批明确的接口需求。RPM、ABI、Python 和发布领域每次迁移一个边界，保留已有命令作为兼容入口。

## 批次 1：分开 main 开发反馈与完整候选

涉及[日常 CI](/home/eg/workspace/github/eglinux/crossforge/.github/workflows/ci.yml)、[候选入口](/home/eg/workspace/github/eglinux/crossforge/.github/workflows/candidate.yml)、[资格化入口](/home/eg/workspace/github/eglinux/crossforge/.github/workflows/qualification.yml)及相关测试。

当前 `ci.yml` 有处理 push base 的代码，但没有独立的 push 触发；main 是通过 candidate 调用 quick。实施时应同时调整两端：main 进入普通 CI，candidate 保留明确的完整候选入口。单独删除 candidate 的 push 会留下验证缺口。

批次 1 可先使用现有保守 profile 选择器。此时 docs 等已知路径可以减少工作，但 `sdk` 与 `python` 仍然对应较粗的相同阶段，不能宣布组件级增量已完成；精确选择由批次 3 交付。

前置检查应覆盖 source-bundle 与 sdk-candidate 各自的 Buildx metadata target、候选与来源绑定、必要字段和失败分支。当前 HEAD 已修正 source-bundle metadata target 的历史错误，应保留该回归并补充实际契约边界，不重复宣称修复同一缺陷。

同一可信源提交的 quick/preflight 可以在建立可核验结果交接后去重；独立 manual/schedule 必须仍能执行完整预检查。定期完整资格化的重放频率先沿用现行设置，待批次 5 区分强制重放语义后再调整。

验收场景包括 main 文档提交、Python 提交、无法确定 base 的提交、PR 更新和手动候选。检查最终 required status 是否忠实反映所选工作，确认日常提交不会取消已在执行的正式候选。

优先回归入口是[CI 计划与构建测试](/home/eg/workspace/github/eglinux/crossforge/tests/config/test_ci_build.py)、[候选工作流测试](/home/eg/workspace/github/eglinux/crossforge/tests/config/test_candidate_workflow.py)和[镜像身份解析测试](/home/eg/workspace/github/eglinux/crossforge/tests/config/test_resolve_candidate_image.py)。工作流静态检查与真实事件验证分别记录。

## 批次 2：建立可消费的内部组件契约

先提取组件身份、材料清单、产物引用的明确 Python 接口，供 renderer、producer、consumer 与后续 selector 共同使用。现有 component projection 是基础，还需要补齐源码、脚本、补丁和环境的实际输入闭包。

清单应分开表达三类身份：

| 身份 | 含义 |
|---|---|
| 构建输入身份 | recipe、来源、补丁、构建参数、平台角色、锁定依赖与实际输入文件决定的身份 |
| 产物身份 | OCI index/platform manifest digest，以及安装树或测试上下文的内容身份 |
| 资格身份 | 所测试产物、测试实现、策略/baseline、环境、tier 与原始执行记录的绑定 |

输入 key 用于查找，OCI digest 用于固定实际消费的字节；二者不可混用。消费时重新核验材料和报告的适用性。保留原始 producer、源提交、执行时间及报告 digest，由新候选引用，不能改写成“本次重新构建/测试过”。

工具链提供两种分别管理的内部产物：

- 安装产物：compiler、sysroot、必要运行库与引用记录，供 Python、vcpkg 和 SDK 消费。
- 测试上下文：GCC prepared source 和 build tree 等，供原样重跑 GCC full 使用。它不进入普通下游安装组件。

首个正式试点继续使用 x86_64 工具链到 cp39 x86_64 的交接，先验证 schema、digest、材料核验、Docker context 和失败分支。沿用本地 OCI layout 可以复现功能；远程 registry 交接还需单独验证传输、身份核验与恢复。

验收必须包含新 builder、错误 digest、错目标、错 sysroot/recipe、损坏或缺失报告，以及安装组件被错误用作测试上下文的情形。首次完整材料不匹配时，应拒绝复用并按计划重建或报错，不能回退到未经验证的 latest tag。

组件保留以引用关系为依据：已被候选/发布引用的产物和证据应受到保留保护，普通缓存与孤立临时产物分开管理。具体保留期限不属于当前已确认决策，接入远程存储时再用实际容量与发布追溯需求制定。

## 批次 3：把增量计划变成明确的任务集合

以[现有选择器](/home/eg/workspace/github/eglinux/crossforge/scripts/ci-plan.py)和[阶段工作流](/home/eg/workspace/github/eglinux/crossforge/.github/workflows/verify-builds.yml)为入口，计划输出应能解释：哪些组件要构建、哪些报告要重新取得、哪些产物可消费、每项工作为什么被选中，以及是否发生了全量兜底。

算法以组件材料变化和依赖传播为主，文件到领域的映射补充实际源码依赖；删除、重命名两侧路径、共享脚本、生成器变化和缺失 base 都必须覆盖。未知路径保守全量处理。可用产物缺失时，即使输入没变，也需要安排相应 producer。

配置投影通过 `--check` 后再用于计划。不能仅比较两个 Git 提交的 generated 文件而忽略生成器或实际输入实现变化，也不能把未经验证的 PR 产物作为可信候选输入。

| 验收变更 | 期望范围 |
|---|---|
| 纯文档 | 保留快速检查，无昂贵源码构建 |
| cp39 专用源码补丁 | cp39 构建与对应下游验证；可复用有效的未变化工具链 |
| 共享 Python 构建脚本 | 所有依赖它的 Python 行 |
| x86_64 sysroot | 对应工具链与下游；独立 ARM 输入按真实共享依赖判断 |
| GCC baseline / 测试实现 | 对应资格结果失效；保留可用安装产物及测试上下文 |
| 发布 metadata 逻辑 | 控制平面与相关最终绑定检查，不使无关编译输入失效 |
| 未识别路径或缺失比较基线 | 明确标注原因并全量兜底 |

测试既断言必须选中的任务，也断言不应被选中的昂贵节点。随后在 Docker 中做一次真实受影响构建，核对执行日志与计划一致。最终 required status 必须验证动态计划对应的完整结果集，不能将应执行任务的 skipped/cancelled 当作成功。

## 批次 4：完成整行 Python 与 SDK 交接

将正式试点扩展到 cp39 的 build Python、x86_64 target、aarch64 target 及 compile/locked/clean 全部现行资格要求，再逐步扩展其他 Python minor。组件级交接使用同一份材料模型，避免增加单独的一套行依赖规则。

SDK 组装消费已经完成的工具链与 Python row 产物。验收目标是其构建图与实际日志都不再出现 GCC/CPython 自身的源码编译；SDK 消费者 fixture 的编译和运行仍是必要验证。

vcpkg 的锁定来源构建资格继续保留。它的报告可按批次 5 的身份规则复用，但不能从普通 binary cache 恢复 fixture 后仍记成一次新完成的源码资格验证。

各 Docker stage 只复制自己需要的模块和材料。模块初始化保持轻量，不因导入公共包而拉入全部供应链、分包、vcpkg 或候选策略。[现有领域隔离测试](/home/eg/workspace/github/eglinux/crossforge/tests/config/test_release_component_domain_isolation.py)应继续覆盖无关领域变更的影响。

## 批次 5：资格复用与候选失败恢复

计划明确区分三种动作：构建、取得资格报告、验证已有资格报告。复用必须能证明产物、完整输入、测试策略和环境身份匹配，且报告有效、来源可信；任一条件不能证明时重新验证，无法通过则阻止候选。

新候选重新执行最终镜像集成和原生 ARM 验证，并把结果绑定到最终候选 digest。完整候选仍须满足全部资格覆盖，复用改变的是报告取得方式。

强制重放应明确指定范围：重跑测试、重新做 vcpkg 锁定源码资格、或从锁定来源重建。当前 `cold` 只去掉远程 cache imports；持久 builder 的本地缓存仍可能命中。实现必须通过真实执行记录证明被选中的步骤确实重跑，并避免测试重放参数意外向上使全部工具链重编译。

发布、ARM 或签名失败后，从同一组已核验 candidate/source/component digest 恢复失败阶段。保留原始 run/attempt 与证据关系；不能从别的提交随意拼接成功 job。新增 fixture 覆盖部分成功后的重试、材料错配和证据缺失。

## 批次 6：资源与测试性能对照

组件流程完成后重新测基线。Python matrix 从 2 提到 3 做对照，compiler jobs=4 和 BuildKit worker parallelism=1 先作为控制变量；之后根据真实关键路径决定是否继续增加。

旧运行数据上的调度模型显示，Python 4 行与 6 行并行时该阶段均可能受 vcpkg 约 143 分钟限制。它只用于安排实验，不能预测重构后的最终耗时，更不能作为购买 runner 的依据。

快速测试优化从已有测量热点着手：Python qualification 和 row manifest 两组约占本地单测耗时的 76%。先测量 fixture 构造、文件复制与重复校验，再调整不可变 fixture 复用和测试分组。跨篡改用例的可变状态必须隔离；未经内容身份验证的路径缓存不可代替验证。

每批代码迁移执行相应回归、renderer `--check`、裁剪后 Docker stage 导入与 platform-python 兼容性检查。引入子包时扩展现有仅检查顶层文件的语法检查。涉及构建语义、行布局或证据绑定的变更，再执行对应真实 Bake 门禁。

## 整体完成标准

开发反馈能解释选中范围，main 小改动不积压完整候选队列；内部组件在新 builder 上可按 digest 消费，SDK 汇总不重复编译 GCC/CPython；完整候选具有全部所需且身份匹配的证据，最终集成和原生 ARM 有本次执行记录；失败恢复保持相同产物身份；既有 CLI、ABI、独立交叉目标和来源约束保持。

记录排队时间、首个可靠反馈时间、完整资格化墙钟时间、job-hours、昂贵源码编译重复次数，以及失败前已消耗工作量。资源采样、组件传输和执行/复用记录作为辅助。至少完成连续三次匹配输入重放及一次受影响变更验证，再根据实测建立性能目标。

研究报告与本方案保留其历史测量口径。实现完成的条目再同步到 README、架构与 Actions 文档，明确区分已实现、Docker 本地通过、运行资格通过和发布闭环已验证。
