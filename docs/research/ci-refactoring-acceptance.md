# CI 重构验收状态

本页是六项已确认方案的验收索引，便于评审；详细历史在[实施进度](ci-refactoring-progress.md)。主实现 `f502449` 的 PR #3 仍在运行，尚未合并 main；本地 GCC full 与八阶段源码 SDK 已成功，更新 release 绑定后的 SDK 最终集成继续验证。PR #5 的增量选择修复已通过其真实检查。后续独立分支已补齐 main 资格重放、候选实际执行记录和共享控制模块选路，分别取得本地 Docker 验证；这些后续改造仍需真实 PR/main 验收。本页不构成候选或发布资格证据。

| 已确认方向 | 当前实现与已取得证据 | 尚待验收 |
|---|---|---|
| main 增量，手动生成完整候选 | main push/PR/手动入口、权限边界、required status 和候选入口已分开；工作流静态检查与事件/失败分支回归通过 | 新工作流在 GitHub 上的真实文档、Python、未知 base、PR 和手动候选事件 |
| Docker/Bake 固定组件交接 | 双工具链、GCC 测试上下文和全部原始 Python 组件已有本地 OCI、材料与独立消费验收；受限签名目录及 registry 接口已接入 | GitHub OIDC 签名、跨 job/run 取得与部分重试；引用保留策略的远程验收 |
| 按组件与下游选择 | 材料闭包、领域失效、未知路径全量兜底和完整结果集合校验已接入；实际 cp39 材料变更已完成计划、源码重放、五份组件、本行资格、独立安装和两种 SDK 验收，其他五行引用保持一致 | GitHub 受影响变更与动态任务验收 |
| 完整 Python 行与 SDK 交接 | 新报告链的六行各 7 条 fresh RUN、六个独立安装各 2 条 fresh RUN；`python-dev` 的 13 条和 `sdk-complete-dev` 的 14 条 fresh RUN 均通过原校验器复核 | 正式候选的实际执行闭环；本地通过不代替候选最终 digest 与原生 ARM 证据 |
| 资格复用、强制重放与恢复 | 原 producer/receipt/执行记录保留；主行工作流接入签名交接，SDK 最终集成强制执行；raw 与完整 32+6 恢复记录、候选分阶段 checkpoint 和同 run 部分重试契约已有回归；本地 vcpkg、x86_64 工具链与 cp39 强制重放通过 | 真实 GitHub 恢复、候选最终集成与原生 ARM、签名及 digest-only promotion |
| 当前 runner、并行度与领域重构 | 标准库模块边界及 Python 3.6 兼容检查已分批落实，测试 fixture 优化已有本地对照；Python 并行上限仍为 2 | 连续三次匹配输入基线、受影响变更、2→3 并行对照和 GitHub 墙钟/job-hours 数据 |

此前完整回归在固定 Docker 工具容器中通过配置 1,419 项和打包 40 项，无跳过；四个锁定验证器、三个 renderer、shell 和全部 workflow actionlint 通过。其后完整 SDK 恢复记录仅改编排与诊断，受影响 69 项及 Rocky 8 Python 3.6 的 48 项通过。各批源码摘要、日志与 fixture 边界分别见[主行接入记录](main-python-row-integration-2026-09-11.json)和[完整恢复记录验证](main-sdk-complete-checkpoint-2026-09-11.json)。这些结果不合并成一次未发生的全量测试。

[新版本地 Docker 重放](runtime-replay-a56bb29-2026-09-11.json)固定 `a56bb29` 源码归档及全部输入。两种 SDK 各消费两份工具链和六份 qualified row，捕获图与实际 RUN 名称中均未出现 GCC/CPython 自身源码编译入口；消费者样例的编译仍保留。完整 SDK 的最终报告覆盖 24 个 Python/目标/链接方式组合、两个打包目标及两个 launcher 消费者，launcher 样例记录为交叉编译、未执行。`python-dev` 为 577.654 秒，完整 SDK 为 777.453 秒，均包含取得与复核，不能据此声称 GitHub 已提速。

跨 runner 仍逐项比较原物理环境。不同 runner 未匹配时 SDK 会重新验收缺失行，不能宣称已经消除跨 job 重复资格。完整 32+6 恢复记录只覆盖全部来自已认证目录的行；SDK 本 job 补验的未签名行不产生完整恢复记录。主 job 自动恢复入口尚未接入。组件 registry 目前保留全部 digest 标签，不自动删除，尚未取得按引用清理的容量效果。

用户已确认执行顺序：全部本地 Docker 与真实 PR 检查通过后，合并并推送 main；随后执行要求真实 main、精确 workflow SHA 和干净源码的组件签名、候选及原生 ARM 门禁。合并不等于整个方案验收完成。表中未完成的实际验证仍不能由静态图、fixture、本地历史计时或旧版本结果代替。

[PR #3](https://github.com/eglinuxer/crossforge/pull/3) 已作为草稿推送。首次真实 PR 配置检查共运行 1,424 项，出现两个失败、一个错误及两个跳过：新 main 带入的旧发布 job 断言不适用于拆分流程，另两个 vcpkg 测试误把默认字节码目录计为额外依赖。三个问题已在 Docker 中复现；保留上游诊断、适配公共消费者 job 断言并覆盖两种字节码环境后，全部受影响的 55 项通过，无跳过，候选工作流 actionlint 通过。详情见[真实 PR 验收记录](pr-rollout-2026-09-11.json)，尚未将失败的 PR 检查记为通过。

最新发现与修复：真实 cp39 补丁实验暴露了独立 append 对完整 release 的依赖，已改用本行固定组件，修复后的计划只选 inputs、cp39 和 SDK。[本批记录](python-append-scope-2026-09-11.json)分别保存首次完整回归的旧断言失败和修正后 75 项复验。新配方的六个独立安装已全部通过：每行两条 fresh RUN、原行 receipt/manifest 与裁剪后的输入均经原校验器复核，实际安装输入均不含完整 release。新版两种 SDK 的十三/十四条 fresh RUN、各八份组件依赖、六份原行记录及导出报告均已通过断网 Docker 原校验器复核，完整实际日志中没有 GCC/CPython 源码编译。

[受影响变更验收](cp39-affected-change-audited-2026-09-11.json)使用独立规范化的 before/after 副本，只改 cp39 补丁说明、release 摘要及独立审计摘要，未改变补丁 hunks、校验规则或仓库正式版本。第一次实验漏改独立审计摘要，被 inputs 门禁正确拒绝，[失败记录](cp39-affected-change-acceptance-2026-09-11.json)及原文件全部保留。新副本通过四个验证器与原材料计划；回归同时验证漏改审计摘要会失败、补齐后仅选 inputs/cp39/SDK。固定脚本按原计划执行 inputs，另做 cp39 强制源码重放，再生产五份新的 cp39 原始组件、重新取得本行资格，消费另外五行原 receipt 并完成独立安装及两种 SDK，各门禁已跨保留的尝试完成，失败批次仍单独记录。各 batch 曾共享本地 builder；计时包含排队，不能用来声称 GitHub 已提速。

补齐审计摘要后的实际 inputs 和源码门禁均已通过；外层本地实验脚本随后因重复新建同名进度文件退出。原 runner、成功门禁和失败日志均保留。新的继续执行脚本固定原成功结果的独立摘要，重新使用原 source plan/fresh RUN verifier 及当前物理环境核验后进入组件生产，每阶段使用独立进度文件，不将复核记为新源码执行。新 cp39 的五份原始组件、本行七条资格 RUN 和独立安装两条 RUN 均已通过。五份旧 raw receipt 与旧行资格都被原接口以材料不匹配拒绝；新目标组件仍绑定原工具链，另外五行仍精确引用原 subjects/qualification。随后 SDK 校验器发现缓存别名记录并拒绝该 batch；失败现场保留。待其他批次全部退出后，使用相同源码、组件与原严格校验器串行重跑两种 SDK，均已通过并经断网 Docker 复核：十三/十四条 fresh RUN、八份依赖、新 cp39 加其他五行原 receipt，以及导出报告字节和模式全部匹配。完整 SDK 的 cp314 COPY-only 导出在 600 秒超时后，于新目录重试同一 digest 恢复；日志保留两次尝试，临时目录按原消费者流程在成功后清理。

第二次真实 PR 配置 1,424 项与打包 40 项已通过，分别有两个与一个资产依赖跳过；Docker 静态检查被固定 Buildx 的 `target:` 检查请求缺陷拦住。[本地修复记录](bake-static-check-2026-09-11.json)保留失败尝试与原生正反例，实际原范围的 11 个目标已通过无警告检查。本次修复后的固定 Docker 完整配置 1,431 项 / 344.331 秒、打包 40 项 / 1.211 秒通过，均无跳过；锁定配置/RPM、三个 renderer、完整 Python/Bash/C 语法、其余 Bake 图与全部 workflow actionlint 通过，后者仅保留既有 concurrency.queue 兼容例外。默认启用字节码，锁定真实 zstd/nFPM 资产补齐远程跳过项。原 Rocky 8 平台 Python 阶段四条 RUN 强制重跑通过。仍须修复后的全部真实 PR 检查，不能据此合并 main。

## 配置测试并行的补充验收（2026-09-11）

[本地独立分支记录](configuration-test-parallelism-2026-09-11.json)确认完整 1,447 项配置测试、40 项打包测试无跳过通过；原 1,431 项与新增 16 项测试身份逐项对齐。四进程单次本地对照约减少 71% 测试墙钟时间，尚无真实 GitHub 提速或连续三次基线结论。最终小改动以受影响回归、实际 Rocky 8 和原范围原生静态检查分别复验。此优化不调整 Python 构建矩阵上限，不替代下列签名、候选、恢复、原生 ARM 或性能门槛。

## 真实 PR 结果与本地整合验证（2026-09-11）

[本批结构化记录](combined-ci-validation-2026-09-11.json)分别保存各次运行的来源、结果和证据摘要。配置并行的独立 [PR #4](https://github.com/eglinuxer/crossforge/pull/4) 全部已选门禁通过；完整 1,447 项配置测试耗时 301.811 秒，有两个既有 zstd 资产跳过，40 项打包测试有一个既有 nFPM 资产跳过，本地 Docker 已补齐。其 quick job 为 440 秒，inputs job 为 133 秒；材料计划只选择 `platform-python-check`，没有编译器输入变更。这是不同 runner 上的首次观察，不能替代连续匹配输入的性能对照，也不能替代整合后 PR #3 的检查。

旧版本 [PR #3 的完整运行](https://github.com/eglinuxer/crossforge/actions/runs/34622092599)已结束并失败。inputs、两套工具链、六行 Python、GCC smoke 和 vcpkg 均通过；vcpkg job 6,924 秒，内部构建 6,858.9 秒。GCC full 新增八条 native 诊断失败；SDK 在 `LLBBridge/Solve` 发送 17,158,344 字节时超过 16,777,216 字节限制。分别见 [GCC 诊断修复](gcc-native-diagnostics-2026-09-11.md)与 [SDK 交接修复](sdk-llb-handoff-2026-09-11.md)。原失败记录保留，不将它们记为通过。

整合版本 `a6cbfd6` 在固定、断网、4 CPU / 16 GiB Docker 中通过完整 1,457 项配置测试和 40 项打包测试，无跳过。配置测试 159.906 秒；独立复核确认四个进程的实际测试身份与完整清单一致，并核对 872 份输入文件的字节、权限和源码摘要。锁定配置/RPM、生成文件、语法与全部 workflow actionlint 通过。GCC full 与六行源码 SDK 的实际 Docker 构建仍在执行；共享 builder 的排队耗时不用于性能比较。尚未取得整合后的真实 PR 全通过，main 仍未合并。

四个已提交版本在 Docker 中经原材料闭包接口对照：整合未改变任何编译器源码输入，单独 GCC 修复与整合版本的完整 GCC 门禁输入完全相同。两种 SDK 相对单独交接修复的唯一材料差异是 `config/release.json`，配方参数相同；因此仍需用更新的 release 绑定执行 SDK 最终集成，不能直接把旧绑定上的通过记为整合验收。

## 当前检查结果与 main 资格重放接线（2026-09-11）

本地 GCC full 已完成，原校验器接受全部四套 suite 的 461,362 条结果；87 条 unexpected 的状态、suite、identity 与 occurrence 均保持原基线。实际 full RUN 为 3,740.8 秒，报告摘要为 `72a4deea5bdfd2ddd98cfe5b2adcc32d2aa682f3ce1d1366fa17fdac5ae557ea`。这证明本地完整资格通过；GitHub 的实际 native 环境仍以 PR #3 当前运行结果为准。源码 SDK 已通过 cp313、cp311 的 OCI 交接并继续后续行，尚未完成整轮及更新 release 绑定后的最终 SDK 集成。

增量选择修复的 [PR #5](https://github.com/eglinuxer/crossforge/pull/5) 在 `8d545fa` 的[真实运行](https://github.com/eglinuxer/crossforge/actions/runs/34646398867)通过全部已选检查。实际合并树与本地源码树一致；1,459 项配置测试的身份逐项匹配本地完整覆盖，两个既有 zstd 资产跳过和一个 nFPM 打包跳过已在本地 Docker 补齐。quick job 为 318 秒，配置测试 224.319 秒；材料计划只选择 `platform-python-check`，没有编译输入变化。它仍以 PR #3 工作分支为 base，不能代替主改造及合并后 main 专属门禁。

另在独立分支补齐 main 的工具链、GCC smoke/full、vcpkg 重放接线。这些 job 原本只验证 raw 组件，却允许普通 BuildKit 缓存满足资格阶段；它们尚无认证资格目录，无法证明旧报告匹配当前物理环境。现在已选择的五个范围均经过原 replay executor，强制执行其资格 RUN，并核对完整执行区间和未改变的物理环境。保持原动态目标、组件输入和只读权限，观察记录不冒充可复用签名 receipt。候选发布图内的 raw/cache 路线仍独立存在，其报告交接与新鲜度必须另行补齐，尚不能宣布第五批或候选闭环完成。

[本次记录](main-qualification-freshness-2026-09-11.json)保留修复前五个范围缺少 replay 参数的失败、修复后真实 composite shell 至 CLI 的回归，以及固定断网 Docker 中完整 1,460 项配置测试 / 153.559 秒、40 项打包 / 1.702 秒的通过结果，无跳过。锁定验证、生成文件、完整语法及 workflow actionlint 通过。第一次完整调用漏挂 Docker CLI，相关检查失败或跳过；原记录保持失败，补齐 CLI 和固定 Buildx 后重新执行了完整检查。main 接线尚待真实受信入口验收；本次没有重跑已通过的 GCC full 或发布候选。

## 候选执行与共享控制模块选路（2026-09-11）

[候选执行记录](candidate-qualification-execution-2026-09-11.json)对应 `96f019d`：实际发布图补齐 GCC smoke 与 vcpkg contract/Tier 1–3 依赖，并强制声明的资格阶段重新执行。SDK checkpoint schema 3 绑定原 producer、源码、组件选择、物理环境、完整 RUN 记录与候选 image index digest；旧版 checkpoint 读取仍保留。固定 Docker 中 1,471 项配置测试、40 项打包及其余 quick 检查通过，无跳过。真实小型 BuildKit 图两次各完成三个物理 RUN、覆盖四个逻辑步骤，中间的缓存命中对照被拒绝。这不是完整候选执行证明；实际候选耗时、原生 ARM 和恢复仍待 main 入口验收。

[语义选路记录](semantic-ci-scope-2026-09-11.json)修复了语法检查掩盖运行期范围的问题。实际材料图显示六个共享控制模块原先只选择 `platform-python-check`；排除其语法清单后，未映射模块使用原有完整验证兜底，已明确映射的 SDK 控制模块仍只选 inputs/SDK。新增回归先复现两项漏检，再通过完整 1,474 项配置测试 / 121.234 秒、40 项打包及其余 quick 检查，无跳过。源码 SDK 已完成六行、`python-dev` 与 `sdk-complete-dev` 的八阶段执行和七次本地 OCI 交接；其计时包含共享 builder 排队，不是性能基线。

[并行度对照入口](../python-matrix-comparison.md)保留默认上限 2，允许手动 main CI 对同一提交指定 2 或 3，并同时应用于原始 Python 组件与资格行矩阵。输入在组件规划前及内层工作流分别校验，每次保存设置、源码和 run/attempt 记录。[本地验证](python-parallelism-control-2026-09-11.json)通过真实 shell 正反例、完整 1,476 项配置测试 / 150.744 秒、40 项打包及其余 quick 检查，无跳过。两个旧固定值断言的失败尝试仍保留。该入口尚未执行真实 main 的 3 行并行，连续三次基线和性能对照仍待完成。
