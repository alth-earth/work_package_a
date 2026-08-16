> **文档治理声明**
>
> - 本文件角色：当前工作包 A 纯版本变更记录。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 原文归档：[CHANGELOG.archive-20260814-pre-governance.md](CHANGELOG.archive-20260814-pre-governance.md)。
> - 改造原因：将版本历史与原文件后半部的旧 README/操作说明分离；完整历史措辞仍由归档逐字保留。

# 工作包 A 变更记录

详细的历史说明和当期命令保存在
[治理前 CHANGELOG](CHANGELOG.archive-20260814-pre-governance.md)。当前状态与待办以
[work_package_a_handoff.md](work_package_a_handoff.md) 为准。

## Unreleased - 2026-08-15

## Unreleased - 2026-08-16（RC1）

- 新增 `scripts/rebuild_topaz_native.py` 与 `src/arctic_route_data/curvilinear.py`：
  Copernicus `originalGrid` 原生曲网格按 20h 窗口分段采集 → 保守最近邻重网格
  （20 km 阈值、水掩膜、跨陆防护、无外推）→ 快照/发布；TOPAZ 5 类重建为
  `cmems-origg-97062ef099c4`（725 帧），旧 default-part 版本退役隔离。
- 新增 `scripts/coverage_audit.py`：corridor spatial finite coverage gate
  （navigable/finite/unknown + per-variable missing）。
- 交付 mur/dikson RC1 bundle `a-bundle-32cafad4…` 与 RunContext run …0b0005；
  doctor PASS（4168 项）。

- 完成文档治理：README 改为短入口，新增统一 handoff；将场景/来源稳定事实与顶层冲刺日历
  分离。
- 交付冻结演示数据 `tromso_isfjorden_august_2026_demo_v1`：12 类齐全、连续 144 h、
  `complete=true`，DatasetBundle/RunContext 已生成并双位置备份；`ocean_current` 显式使用
  detided 后备。本轮仅新增运行数据与文档，未修改代码、Schema 或依赖。
- 记录网络与下载约定（直连可用；代理 127.0.0.1:10808 无效；Copernicus 凭据可用）。

## 0.4.2 - 2026-08-13

- 接入 `arctic-route-contracts 0.3.0` 和至少 48 h 的共享航时缓冲。
- 主线画像固定为恰好 12 类必需层；水深和长期限制区改为独立可选采集/报告。
- 增加 NCEI 官方 THREDDS Range 后备，并保持来源失败原因和精确入口证据。

## 0.4.1 - 2026-08-13

- 新增 `resolve_dataset_bundle_for_b()`，支持跨进程按 data ID 精确恢复 v2 bundle。
- 加固 cadence、payload attestation、深快照和 simulation/knowledge/generation 围栏。
- v1 保持历史读取能力，不得进入正式恢复和 RunContext。

## 0.4.0 - 2026-08-12

- 注册 14 类环境数据，新增正式 `land_sea_mask`；船舶事实迁入共享契约。
- 接入共享走廊、双场景语义、动态时域和 RunContext v2。
- 新增 neXtSIM 冰型/冰缘、GEBCO 水深/陆海分类与 EMODnet 分类证据入口。
- 明确 A 保留源网格，统一目标网格和风险语义归 B。

## 0.3.1 - 2026-08-11

- 修复最低窗与完整窗混淆，增加可独立复核的 provenance 与 bundle 身份。
- 历史实源联合回放达到 9 类、1001 帧；该证据不等于当前 12 类正式主线。

## 0.3.0 - 2026-08-11

- 建立真实 GFS 未来窗、三时间 UTC、不可变归档、revision、回放和 doctor 证据链。
- 加固路线隔离、模拟 seek/generation 和物理语义验证。

## 0.2.0 - 2026-08-08

- 首次形成自包含的工作包 A 工程基线和 A→B 公共边界。
- 引入 manifest、SimulationClock、AB cache、原子摄取和旧下载器适配。
