# CI 重构实施进度

目标是完成[六批实施方案](ci-refactoring-plan-2026-09-10.md)，不是仅完成触发器调整。工作分支为 `codex/component-ci-refactor`；研究基线为 `cf736eab8aa53b851874509d66676e5bac98dc27`。

| 批次 | 当前状态 | 仍需取得的证据 |
|---|---|---|
| 1：入口与前置回归 | 本地实现及 Docker 回归通过 | 修改后工作流的真实 GitHub 事件运行 |
| 2：组件契约与工具链交接 | 输入身份基础模块通过 Docker 验证；交接仍在实现 | 产物清单、完整材料闭包、测试上下文与正式 producer/consumer |
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
