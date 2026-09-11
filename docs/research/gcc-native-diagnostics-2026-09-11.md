# GCC native 诊断修复记录

PR #3 的 [GCC full 运行](https://github.com/eglinuxer/crossforge/actions/runs/34622092599/job/103349224336)
在 `ca4d4297782f1ac996ce143628c765a9d1ecb290` 上失败，测试实际 checkout 为
`7b42dc2e5df5c3760f48e88611acc7cb6d6aff24`。冻结基线之外新增 8 条
`test for excess errors`，没有 resolved 记录：C++ 的 `pr90773-1d.C`
涉及三个标准，C 的 `pr101395-2.c`、`pr101395-3.c`、`pr115978-1.c`、
`pr115978-2.c`、`pr57275.c` 各一条。

这些用例都使用 `-march=native`，失败上下文出现同一条 AVX10.1 alias
告警。[GCC 15 变更说明](https://gcc.gnu.org/gcc-15/changes.html)及
[上游选项补丁](https://gcc.gnu.org/pipermail/gcc-patches/2025-March/678222.html)
明确规定这条告警。锁定的 Red Hat GCC 15.2.1 源码中，native driver
会为探测到的 AVX10.1 特性加入带 `Warn` 的选项。实际 PR 的 CPU 型号没有
出现在已有证据中，因此这里不推断具体型号。

修复沿用已有资格测试补丁机制，只为上述六个文件增加精确的 `dg-warning`。
`full-site.exp` 中的有效目标探针使用 `-march=x86-64` 编译并直接执行
`__builtin_cpu_supports` 检查，独立决定告警是否应当出现。它不根据待匹配的
告警反推条件。所有原始编译选项和运行断言保留；告警文本使用转义的点及
行结束断言，兼容 DejaGNU 收集的 CR/LF。补丁文件及修改前后的源码分别固定 SHA256。

工具链版本、源码构建配方和 ABI 集合未变。完整测试计划改变，因此更新基线
文件中的 `plan_sha256` 与 release 的内容绑定。原有 87 条失败记录的 status、
suite、test、count 逐项完全一致，没有把这 8 条新增失败写入允许集合。

本地 Docker 使用固定工具链安装组件
`sha256:2a71734c38ca5234de1c4784afe0c3c01786de718015bb59ce990a3499edbd1f`
和测试上下文
`sha256:b638a7a57917bcd13dac88a4f537eaaba4ce28a4848ec47a4f2d85bc9c14c48d`。
独立核对的有限 DejaGNU 试验结果如下：

- 六个原始用例合计 14 项检查全部 PASS，没有遗漏或跳过。
- 明确指定 AVX10.1 的仅编译正例产生 8 项预期诊断/多余错误检查 PASS。
- 缺失应有告警、额外告警、条件不符时出现告警、运行时 abort 四类反例，
  在 C 及三个 C++ 标准下产生恰好 16 条预期 FAIL。
- 1432 项配置回归及 40 项打包回归全部通过，没有跳过；锁文件、生成文件、
  脚本语法及 actionlint 检查通过。

试验中的告警正例显式选择目标条件，仅验证 DejaGNU 的真实诊断处理；它没有
模拟 AVX10.1 CPU。实际 native 特性探针在本地 CPU 上编译和执行。
完整本地 GCC full 与修复后的真实 PR 验收仍在等待，有限试验不是资格化结果。

原始记录位于 `/tmp/crossforge-gcc-native-diagnosis/`：
`diagnostics-verification.json` 固定有限试验的全部文件哈希，`all-checks.json`
记录完整快速验证。`dejagnu-v1.log` 保留遗漏 schema 枚举导致的失败；v2 保留
筛选参数覆盖及行结束匹配问题；v3 为修正后完整正反例。完整门禁首次仅因
Buildx 输出目录授权缺失而未执行，第二次使用明确的任务目录授权和强制测试 RUN。
