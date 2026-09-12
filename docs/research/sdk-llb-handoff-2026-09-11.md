# SDK 本地 OCI 交接

PR #3 的 [SDK 汇总](https://github.com/eglinuxer/crossforge/actions/runs/34622092599/job/103384627370)
在 2026-09-11 19:01 UTC 构建 `python-dev-append-cp310` 时失败：
`trying to send message larger than max (17158344 vs. 16777216)`。
BuildKit 原始日志将调用定位到 Dockerfile frontend 的 `LLBBridge/Solve`。
这说明发送的消息超过 16 MiB 协议限制；不能把它当作编译进程内存不足。
原始源码 checkout 为 `7b42dc2e5df5c3760f48e88611acc7cb6d6aff24`，
SDK 阶段退出码 102，内部耗时 192.6 秒。此前六行 Python 和 vcpkg 已分别通过。

上游 [#6726](https://github.com/moby/buildkit/issues/6726) 报告了关联上下文
重复序列化共享子图的问题，但其示例在 Inputs 响应触发 4 MB 限制，与本次
Solve 请求的 16 MiB 错误不完全相同。本次修复不假设上游问题已经精确复现，
也不通过日志大小参数调整 RPC 限制。

`ci-build.py run sdk` 在本地及不使用认证组件的 CI 路径中，按原有六行顺序
构建 cumulative SDK。每完成一次 append，就导出同次运行内的本地 OCI
镜像，从本次 Bake metadata 取得 digest，核对所选 linux/amd64 manifest、
config 及每个压缩层，再把下一步的原始 SDK base 边替换为该 digest。
Python final 和完整 SDK 仍分别执行原有门禁。

这一做法缩短传给每个 frontend 的祖先依赖图。各行编译、资格检查、COPY、
重复行拒绝、manifest 重算及最终消费者检查的 Docker 配方不变。main 的认证
组件路径继续使用原有签名/receipt 验证；本地快照不进入那个信任体系，也不
作为跨运行恢复记录或可发布候选。

所有步骤共享原有 330 分钟预算。输入文件、Docker 配方及两份 Bake 文件的
内容身份在步骤前后重新核对。非零退出、缺失 metadata、错误 digest、损坏
OCI 或源码变化都会中止交接。诊断中保留原始图、每一步实际图、metadata、
内容校验结果和完整日志。大型 OCI 目录位于诊断目录之外，不上传到 Actions。
正常推进时最多保留前后两个快照；失败时保留尚在使用的快照及部分导出。

验证状态：相关 7 项交接回归和 26 项 CI 执行回归通过；真实六行 SDK Docker
构建已启动。完整本地构建及真实 PR 尚未完成，因此暂不宣称问题已解决或有
稳定耗时收益。原始失败证据位于 `build/bake-check-linked-targets/pr3-sdk-artifact.zip`；
本地试验记录位于 `/tmp/crossforge-sdk-local-handoff-validation/`。
