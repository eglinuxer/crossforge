# CI 重构验收状态

本页是六项已确认方案的验收索引，便于评审；详细历史在[实施进度](ci-refactoring-progress.md)。当前生产实现包含 `5e56bb8` 的 append 输入范围修复，位于 `codex/component-ci-refactor`；远程 main 的外部诊断修复 `e8aa68a` 正整合进该分支。本页不构成候选或发布资格证据。

| 已确认方向 | 当前实现与已取得证据 | 尚待验收 |
|---|---|---|
| main 增量，手动生成完整候选 | main push/PR/手动入口、权限边界、required status 和候选入口已分开；工作流静态检查与事件/失败分支回归通过 | 新工作流在 GitHub 上的真实文档、Python、未知 base、PR 和手动候选事件 |
| Docker/Bake 固定组件交接 | 双工具链、GCC 测试上下文和全部原始 Python 组件已有本地 OCI、材料与独立消费验收；受限签名目录及 registry 接口已接入 | GitHub OIDC 签名、跨 job/run 取得与部分重试；引用保留策略的远程验收 |
| 按组件与下游选择 | 材料闭包、领域失效、未知路径全量兜底和完整结果集合校验已接入；实际 cp39 材料变更计划已修复并开始端到端执行 | 完成受影响变更的计划与执行对照，以及 GitHub 动态任务验收 |
| 完整 Python 行与 SDK 交接 | 新报告链的六行各 7 条 fresh RUN、六个独立安装各 2 条 fresh RUN；`python-dev` 的 13 条和 `sdk-complete-dev` 的 14 条 fresh RUN 均通过原校验器复核 | 正式候选的实际执行闭环；本地通过不代替候选最终 digest 与原生 ARM 证据 |
| 资格复用、强制重放与恢复 | 原 producer/receipt/执行记录保留；主行工作流接入签名交接，SDK 最终集成强制执行；raw 与完整 32+6 恢复记录、候选分阶段 checkpoint 和同 run 部分重试契约已有回归；本地 vcpkg、x86_64 工具链与 cp39 强制重放通过 | 真实 GitHub 恢复、候选最终集成与原生 ARM、签名及 digest-only promotion |
| 当前 runner、并行度与领域重构 | 标准库模块边界及 Python 3.6 兼容检查已分批落实，测试 fixture 优化已有本地对照；Python 并行上限仍为 2 | 连续三次匹配输入基线、受影响变更、2→3 并行对照和 GitHub 墙钟/job-hours 数据 |

最近一次完整回归在固定 Docker 工具容器中通过配置 1,419 项和打包 40 项，无跳过；四个锁定验证器、三个 renderer、shell 和全部 workflow actionlint 通过。其后完整 SDK 恢复记录仅改编排与诊断，受影响 69 项及 Rocky 8 Python 3.6 的 48 项通过。各批源码摘要、日志与 fixture 边界分别见[主行接入记录](main-python-row-integration-2026-09-11.json)和[完整恢复记录验证](main-sdk-complete-checkpoint-2026-09-11.json)。这些结果不合并成一次未发生的全量测试。

[新版本地 Docker 重放](runtime-replay-a56bb29-2026-09-11.json)固定 `a56bb29` 源码归档及全部输入。两种 SDK 各消费两份工具链和六份 qualified row，捕获图与实际 RUN 名称中均未出现 GCC/CPython 自身源码编译入口；消费者样例的编译仍保留。完整 SDK 的最终报告覆盖 24 个 Python/目标/链接方式组合、两个打包目标及两个 launcher 消费者，launcher 样例记录为交叉编译、未执行。`python-dev` 为 577.654 秒，完整 SDK 为 777.453 秒，均包含取得与复核，不能据此声称 GitHub 已提速。

跨 runner 仍逐项比较原物理环境。不同 runner 未匹配时 SDK 会重新验收缺失行，不能宣称已经消除跨 job 重复资格。完整 32+6 恢复记录只覆盖全部来自已认证目录的行；SDK 本 job 补验的未签名行不产生完整恢复记录。主 job 自动恢复入口尚未接入。组件 registry 目前保留全部 digest 标签，不自动删除，尚未取得按引用清理的容量效果。

用户已确认执行顺序：全部本地 Docker 与真实 PR 检查通过后，合并并推送 main；随后执行要求真实 main、精确 workflow SHA 和干净源码的组件签名、候选及原生 ARM 门禁。合并不等于整个方案验收完成。表中未完成的实际验证仍不能由静态图、fixture、本地历史计时或旧版本结果代替。

[PR #3](https://github.com/eglinuxer/crossforge/pull/3) 已作为草稿推送。首次真实 PR 配置检查共运行 1,424 项，出现两个失败、一个错误及两个跳过：新 main 带入的旧发布 job 断言不适用于拆分流程，另两个 vcpkg 测试误把默认字节码目录计为额外依赖。三个问题已在 Docker 中复现；保留上游诊断、适配公共消费者 job 断言并覆盖两种字节码环境后，全部受影响的 55 项通过，无跳过，候选工作流 actionlint 通过。详情见[真实 PR 验收记录](pr-rollout-2026-09-11.json)，尚未将失败的 PR 检查记为通过。

最新发现与修复：真实 cp39 补丁实验暴露了独立 append 对完整 release 的依赖，已改用本行固定组件，修复后的计划只选 inputs、cp39 和 SDK。[本批记录](python-append-scope-2026-09-11.json)分别保存首次完整回归的旧断言失败和修正后 75 项复验。新配方的六个独立安装已全部通过：每行两条 fresh RUN、原行 receipt/manifest 与裁剪后的输入均经原校验器复核，实际安装输入均不含完整 release。新版两种 SDK 的十三/十四条 fresh RUN、各八份组件依赖、六份原行记录及导出报告均已通过断网 Docker 原校验器复核，完整实际日志中没有 GCC/CPython 源码编译。

[受影响变更验收](cp39-affected-change-audited-2026-09-11.json)使用独立规范化的 before/after 副本，只改 cp39 补丁说明、release 摘要及独立审计摘要，未改变补丁 hunks、校验规则或仓库正式版本。第一次实验漏改独立审计摘要，被 inputs 门禁正确拒绝，[失败记录](cp39-affected-change-acceptance-2026-09-11.json)及原文件全部保留。新副本通过四个验证器与原材料计划；回归同时验证漏改审计摘要会失败、补齐后仅选 inputs/cp39/SDK。固定脚本按原计划执行 inputs，另做 cp39 强制源码重放，再生产五份新的 cp39 原始组件、重新取得本行资格，消费另外五行原 receipt 并完成独立安装及两种 SDK，实际 batch 尚未完成。各 batch 共享本地 builder；计时包含排队，不能用来声称 GitHub 已提速。

补齐审计摘要后的实际 inputs 和源码门禁均已通过；外层本地实验脚本随后因重复新建同名进度文件退出。原 runner、成功门禁和失败日志均保留。新的继续执行脚本固定原成功结果的独立摘要，重新使用原 source plan/fresh RUN verifier 及当前物理环境核验后进入组件生产，每阶段使用独立进度文件，不将复核记为新源码执行。新 cp39 的五份原始组件、本行七条资格 RUN 和独立安装两条 RUN 均已通过。五份旧 raw receipt 与旧行资格都被原接口以材料不匹配拒绝；新目标组件仍绑定原工具链，另外五行仍精确引用原 subjects/qualification。随后 SDK 校验器发现缓存别名记录并拒绝该 batch；失败现场保留。待其他批次全部退出后，使用相同源码、组件与原严格校验器串行重跑两种 SDK，尚未完成。
