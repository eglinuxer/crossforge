# CI 重构实施进度

目标是完成[六批实施方案](ci-refactoring-plan-2026-09-10.md)，不是仅完成触发器调整。工作分支为 `codex/component-ci-refactor`；研究基线为 `cf736eab8aa53b851874509d66676e5bac98dc27`。

| 批次 | 当前状态 | 仍需取得的证据 |
|---|---|---|
| 1：入口与前置回归 | 本地实现及 Docker 回归通过 | 修改后工作流的真实 GitHub 事件运行 |
| 2：组件契约与工具链交接 | 先前可行性试点通过；正式契约待实现 | 严格身份/材料验证、测试上下文与正式 producer/consumer |
| 3：组件级增量计划 | 待实现 | 依赖传播、完整结果汇总和真实受影响构建 |
| 4：整行 Python/SDK 交接 | 待实现 | 双架构全 row、SDK 组装无 GCC/CPython 源码重编 |
| 5：资格复用及恢复 | 待实现 | 显式复用、强制执行、候选集成/ARM 及同 digest 恢复 |
| 6：性能与领域重构 | 待实施对照 | 新基线、三次匹配输入重放和受影响变更、资源实验 |

## 批次 1 已实现

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
