> **二次文档治理归档声明**
>
> - 本文件角色：2026-08-15 改造前的 A 场景与来源参考快照，仅供历史追溯。
> - 归档时间：2026-08-15（Asia/Shanghai）。
> - 现行文件：[SCENARIOS_AND_SOURCES.md](SCENARIOS_AND_SOURCES.md)。
> - 归档原因：补齐两条航线允许区域、栅格自动修正范围用途和稳定演示数据预置口径。
>
> <!-- ORIGINAL CONTENT START -->

> **文档治理声明**
>
> - 本文件角色：当前工作包 A 的稳定场景、走廊、来源与数据语义参考。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 原文归档：[SCENARIOS_AND_SOURCES.archive-20260814-pre-governance.md](SCENARIOS_AND_SOURCES.archive-20260814-pre-governance.md)。
> - 改造原因：移除短期十日排期职责，只保留可长期引用的技术事实、证据边界和验收口径。

# 工作包 A：场景、走廊与数据来源

本文件只记录稳定技术事实。当前优先级和日历见
[ABC_10_DAY_SPRINT.md](../../ABC_10_DAY_SPRINT.md)，项目状态与操作入口见
[work_package_a_handoff.md](../work_package_a_handoff.md)。

> 本系统是科研演示，不是适航、法律或导航安全系统。工程 `formal` 不等于科学校准。

## 1. 真值来源与系统边界

走廊、场景、船型和 RunContext 的唯一共享真值在
[arctic_route_contracts](../../arctic_route_contracts/)。A 的历史字段 `route_id` 等同共享
`corridor_id`；`scenario_id` 是完整演示/试验上下文，不能拿 plan ID 或目录名替代。

```text
共享 Scenario + Corridor + Vessel
              │
              ▼
A：显式 UTC 窗采集、留证、规范化、QC、不可变归档
              │ DatasetBundle v2
              ▼
共享层：RunContext v2
              │
              ▼
B：逐小时连续化、风险、confidence、environment_speed_factor
              │ RiskFrame v2
              ▼
C：最终船速、ETA、路径、四层规划与重规划
```

A 不生成 `risk_score`、`risk_level`、`hard_mask`、`confidence`、最终船速或路线。

## 2. 走廊与动态时域

| 走廊 | 角色 | 端点 | 默认/允许时域 |
|---|---|---|---|
| `offshore_murmansk_to_offshore_dikson` | 主开发 | 69.15°N, 33.60°E → 73.55°N, 80.40°E | 168 h / 144–216 h |
| `tromso_to_isfjorden_outer` | 迁移验证 | 69.75°N, 19.00°E → 78.15°N, 13.00°E | 96 h / 72–144 h |

朗伊尔城 `78.22°N, 15.65°E` 仅是 AIS 完整航次参考点，不参与路线优化。旧
`tromso_to_svalbard` 是历史标识，不能覆盖当前端点。

时域不是固定“9 天”，而是由候选路线距离、船型标称速度、保守环境速度因子和至少 48 h
缓冲计算，再向上取整到 24 h。超出正式上限返回
`forecast_coverage_insufficient`；不得截断尾段后仍称完整。

## 3. 双场景语义

每条走廊都有两类版本化场景：

1. `retrospective_best_estimate`：允许使用后来取得的历史分析/再分析，但必须保留实际
   retrieval gate，不能称为严格还原历史当时可见预测。
2. `frozen_forecast`：必须显式给出 UTC `simulation_start`，冻结当次可用周期和快照；
   禁止 implicit latest。

任何读取都必须同时满足：

```text
record.route_id == requested_corridor_id
record.issue_time <= knowledge_as_of
frame.generation_id == current_generation_id
```

因果模式固定 `knowledge_as_of == simulation_time`；事后最佳估计可以显式更晚，但这种差异
必须传播到 Scenario、bundle 和报告。

## 4. 12 类必需层与 2 类可选层

| `data_type` | 主要来源/派生 | 关键语义 | 画像 |
|---|---|---|---|
| `wind_field` | GFS/NCEI | 真东/真北，`m s-1` | 必需 |
| `temperature` | GFS/NCEI | 2 m 温度，K | 必需 |
| `visibility` | GFS/NCEI | m | 必需 |
| `wave` | Copernicus | 波向 from true north clockwise，圆周插值 | 必需 |
| `ocean_current` | Copernicus | 含潮总流优先；detided 显式后备；禁止相加 | 必需 |
| `water_level` | Copernicus | 海面高度，m | 必需 |
| `sea_ice_concentration` | Copernicus | 0..1 | 必需 |
| `sea_ice_drift` | Copernicus | 真东/真北，`m s-1` | 必需 |
| `sea_ice_thickness` | Copernicus | m | 必需 |
| `sea_ice_type` | neXtSIM 分量确定性派生 | 分类层，不是 ML 模型 | 必需 |
| `sea_ice_edge` | 冰密集度 ≥15% 的冰侧四邻域边界 | 分类派生 | 必需 |
| `land_sea_mask` | GEBCO elevation 确定性派生 | 表面分类，不是完整可通航 mask | 必需 |
| `bathymetry` | GEBCO 2026 | positive-up；研究层 | 可选 |
| `long_term_restricted_area` | EMODnet 分类 WFS 证据 | information；禁止自动 hard-mask | 可选 |

正式 A–B–C 主线 bundle 固定为恰好 12 类必需层。两类可选层单独采集和报告，其失败不
阻塞基线，也不授予它们导航安全语义。

## 5. 稳定数据决策

### 5.1 网格与插值

A 保存来源网格身份，不把异构源强制重采样到一个风险网格。B 负责覆盖检查和目标网格：

- 连续标量使用有物理依据的连续插值；
- 分类和掩膜使用 nearest；
- 矢量先确认真东/真北语义，再按分量插值；
- 波向使用 `sin/cos` 圆周插值。

旧资料“所有变量 nearest”与当前现状不符。

### 5.2 mask 与法律语义

- `source_valid_mask` 只表示源结构有效域，不等于陆海、可通航或法律 mask。
- `land_sea_mask` 是 B 构建陆地规则的基础事实，不等于完整 `hard_mask`。
- `bathymetry` 尚未结合可信吃水、潮位、净空和不确定性，不能作为核心安全约束。
- 保护区、军事区、空间规划和 Natura 2000 分类别保存；未知法律效果不得自动 hard，也
  不得自动视为安全。

### 5.3 来源和缺测

缺失、过期、future issue、错误单位、错误方向、错误走廊或覆盖不足必须拒绝或明确降级；
不得用全零、mtime、文件名或“最近可用”把未知伪装成安全。

## 6. 当前证据等级

证据必须区分：代码存在、fixture/合同测试、真实源小窗 smoke、完整长窗 bundle+doctor、
持续调度/监控。

- 工程基线：工作包 A 0.4.2；2026-08-14 `make check` 为 172 tests passed，Ruff、
  lock/sync 和 CLI 通过。
- 历史长窗：0.3.1 留有旧走廊、v1、9 类、1001 帧证据，只可审计/迁移。
- 新来源 smoke：NCEI byte-range、GEBCO、EMODnet 和早期 neXtSIM 小窗已验证。
- 当前缺口：主走廊恰好 12 类、168 h 的真实 DatasetBundle v2、RunContext、doctor 和
  exact resolver 仍未形成完整证据；latest v2 Copernicus 与 total-with-tide 需复验。
- 真实 A→B→C 端到端仍未完成。

## 7. 稳定验收闸门

正式 A 制品至少满足：

1. 使用共享版本化场景、走廊和船型；窗口起止、模式和知识截止显式。
2. 12 个必需类型逐项 `covers_requested_window=true`、
   `provenance_complete=true`、`complete=true`。
3. 每条记录有 UTC issue/valid/ingest、不可变 revision、checksum 和可复核 source snapshot。
4. `DatasetBundle v2` 通过 A 语义验证及共享包独立重算；v1 只作历史读取。
5. 跨进程 exact resolver 从公共 API 恢复完全相同的记录和 payload attestation。
6. doctor 对 ready/raw/sidecar/source snapshot 的路径、大小和 SHA-256 零错误。
7. 可选层独立报告；任何 fixture 或小窗 smoke 均不得替代真实完整窗。

## 8. 相关文档

- A 项目交接：[work_package_a_handoff.md](../work_package_a_handoff.md)
- A→B 接口：[AB_INTERFACE.md](AB_INTERFACE.md)
- 时间政策：[ISSUE_TIME_POLICY.md](ISSUE_TIME_POLICY.md)
- 证据追踪：[ARCHITECTURE_TRACE.md](ARCHITECTURE_TRACE.md)
- 旧 A 迁移边界：[LEGACY_MIGRATION.md](LEGACY_MIGRATION.md)
- 跨包边界：[BCD_HANDOFF.md](BCD_HANDOFF.md)
- 治理前完整原文：[SCENARIOS_AND_SOURCES.archive-20260814-pre-governance.md](SCENARIOS_AND_SOURCES.archive-20260814-pre-governance.md)
- 系统权威：[ARCTIC_ROUTE_SYSTEM.md](../../ARCTIC_ROUTE_SYSTEM.md)
- 当前冲刺：[ABC_10_DAY_SPRINT.md](../../ABC_10_DAY_SPRINT.md)
