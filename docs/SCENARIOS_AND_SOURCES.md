> **文档治理声明**
>
> - 本文件角色：工作包 A 当前场景、航区坐标、数据窗口与来源策略参考。
> - 改造时间：2026-08-15（Asia/Shanghai）。
> - 原文件去向：[SCENARIOS_AND_SOURCES_归档_20260815.md](SCENARIOS_AND_SOURCES_归档_20260815.md)。
> - 改造原因：补入图示最新坐标、端点栅格自动修正范围和稳定演示数据预置口径。

# 工作包 A：场景、航区与数据来源

## 1. 稳定演示数据策略

A 在开发阶段下载并预处理数据，持久化后供比赛现场的 B/C/D 离线读取。数据“最新”不是目标；
时间连续、场景一致、可重复加载才是目标。历史回放继续遵守发布时间门禁，稳定演示按冻结数据
版本、模拟时钟和 generation 运行。

## 2. 航区一：摩尔曼斯克外海—迪克森外海

| 字段 | 当前值 |
|---|---|
| `corridor_id` | `offshore_murmansk_to_offshore_dikson` |
| 数据 bbox | 67.50–75.00°N，30.00–85.00°E |
| 起点 | 69.55°N，34.00°E（corridor 2.2.0，Murmansk 外海） |
| 起点允许区域 | 69.45–69.75°N，33.30–34.70°E |
| 终点 | 73.80°N，80.00°E（corridor 2.2.0，Dikson 外海） |
| 终点允许区域 | 73.60–73.95°N，79.60–80.50°E |
| 默认/允许时域 | 168 h / 144–216 h |

> 2026-08-16 更新：corridor 由 2.1.0 调整为 2.2.0（起点/终点移至 12 类数据全有限的外海
> 区域，依据见 `最终交付说明.md §15/§17`）；起点修改有覆盖率依据，终点修改为非严格必要。

起点/终点允许区域就是端点栅格自动修正边界：只允许在区域内选择可航、同连通分量的节点，
并记录修正坐标、距离和原因。起点避开港池内部，终点避开港内及近岸浅水。

## 3. 航区二：特罗姆瑟外海—伊斯峡湾外部入口

| 字段 | 当前值 |
|---|---|
| `corridor_id` | `tromso_to_isfjorden_outer` |
| 数据 bbox | 68.50–79.50°N，10.00–22.00°E |
| 气象导航起点 | 69.75°N，19.00°E |
| 起点允许区域 | 69.40–70.00°N，18.00–20.50°E |
| 气象导航终点 | 78.15°N，13.00°E |
| 终点允许区域 | 77.90–78.40°N，12.00–16.50°E |
| 朗伊尔城 AIS 参考点 | 78.22°N，15.65°E |
| 默认/允许时域 | 96 h / 72–144 h |

规划在伊斯峡湾外部入口结束。AIS 可延伸到朗伊尔城以识别完整航次，但峡湾内部和港内轨迹不
纳入气象导航优化评价。旧 `tromso_to_svalbard` 及旧朗伊尔城算法终点均为历史口径。

## 4. 开发顺序

先完成摩尔曼斯克—迪克森主线，再以相同 B 参数和 C 流程迁移特罗姆瑟—伊斯峡湾。短航线可
用于 smoke，但不改变顺序。

## 5. 运行场景与时间

- `retrospective_best_estimate`：历史最佳估计，必须清楚写明知识截止；
- `frozen_forecast`：显式冻结 UTC 起点和本地快照，禁止 implicit latest；
- 历史回放：`issue_time <= knowledge_as_of <= simulation_time`；
- 稳定演示：读取冻结本地制品，仍传播 scenario、generation 和各级摘要。

## 6. 数据画像

必需 12 类：`land_sea_mask`、`ocean_current`、`sea_ice_concentration`、`sea_ice_drift`、
`sea_ice_edge`、`sea_ice_thickness`、`sea_ice_type`、`temperature`、`visibility`、
`water_level`、`wave`、`wind_field`。

`bathymetry` 和 `long_term_restricted_area` 为可选接口，不阻塞挑战杯演示，也不自动获得
hard-mask 语义。稳定演示仍按当前合同准备 12 类输入，但可以使用提前冻结的历史/演示数据而
不要求比赛当天最新；来源等级必须准确标注，不得把演示制品冒充科学或实时实源成果。

### 6.1 12 类必需层与 2 类可选层明细（源自：SCENARIOS_AND_SOURCES_归档_20260815.md）

| `data_type` | 主要来源/派生 | 关键语义 | 画像 |
|---|---|---|---|
| `wind_field` | GFS/NCEI（主）/ C3S CARRA（冬季补采） | 真东/真北，`m s-1` | 必需 |
| `temperature` | GFS/NCEI（主）/ C3S CARRA（冬季补采） | 2 m 温度，K | 必需 |
| `visibility` | GFS/NCEI（主）/ C3S CARRA（冬季补采） | m | 必需 |
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

> 多源说明（2026-08-24 补充）：`wind_field`/`temperature`/`visibility` 在规格层（`specs.DataTypeSpec.source_families`）同时登记了规范化来源键 `"noaa_gfs"`（主，NOAA GFS/NOMADS）与 `"c3s_carra"`（C3S CARRA 东域再分析）。CARRA 为冬季补采源，依据提案 **A-WINTER-MET-001**（2026-08-22 批准），仅覆盖特罗姆瑟→伊斯峡湾窗口 2026-02-15..02-21，目前尚未发布进 A 流水线（需 CDS 令牌 + eccodes），其变量名/单位由 `carra_acquisition.py:CARRA_DATA_TYPE_TO_API_VARIABLES` 保证与上述一致。`source_families` 不影响既有 `source_family`（人读标签）比较逻辑。

正式 A–B–C 主线 bundle 固定为恰好 12 类必需层；可选层单独采集和报告，失败不阻塞基线。

## 7. 来源与处理原则

- A 保存源网格，B 负责目标风险网格；
- 连续标量使用有物理依据的连续插值；分类和掩膜使用 nearest；矢量先确认真东/真北语义再按
  分量插值；波向使用 `sin/cos` 圆周插值（源自：SCENARIOS_AND_SOURCES_归档_20260815.md）；
- 含潮总流与 detided 流互斥，禁止相加；
- 矢量和波向保留方向语义，分类/掩膜不使用连续插值；
- 缺测、future、错误单位、错误走廊不能用零值或“最近文件”掩盖；
- 比赛数据应在赛前冻结、doctor、离线恢复并保存清单。

### 7.1 mask 与法律语义（源自：SCENARIOS_AND_SOURCES_归档_20260815.md）

- `source_valid_mask` 只表示源结构有效域，不等于陆海、可通航或法律 mask；
- `land_sea_mask` 是 B 构建陆地规则的基础事实，不等于完整 `hard_mask`；
- `bathymetry` 尚未结合可信吃水、潮位、净空和不确定性，不能作为核心安全约束；
- 保护区、军事区、空间规划和 Natura 2000 分类别保存；未知法律效果不得自动 hard，也不得
  自动视为安全；
- 缺失、过期、future issue、错误单位、错误方向、错误走廊或覆盖不足必须拒绝或明确降级。

### 7.2 当前证据等级（源自：SCENARIOS_AND_SOURCES_归档_20260815.md）

证据必须区分：代码存在、fixture/合同测试、真实源小窗 smoke、完整长窗 bundle+doctor、
持续调度/监控。

- 工程基线：A 0.4.2；2026-08-14 `make check` 为 172 tests passed，Ruff、lock/sync 和
  CLI 通过；
- 历史长窗：0.3.1 留有旧走廊、v1、9 类、1001 帧证据，只可审计/迁移；
- 新来源 smoke：NCEI byte-range、GEBCO、EMODnet 和早期 neXtSIM 小窗已验证；
- 2026-08-26 holdout 已形成新的真实 A→B→C 证据：A 使用 TOPAZ `originalGrid` 含潮总流
  （145 条 current records，`current_component=total`、`tide_included=true`），B/C 在隔离
  实验目录完成 145 帧风险与 12 条路线；该构件不是生产发布，也不改变冻结 M2 结论；
- 2026-03-22～03-28 development window 已完成第二个独立严寒样本，优先复用已处理冰海筛选数据，
  GFS 历史直链 404 已记录，气象三要素使用已批准 CARRA winter fallback；A 形成 145 条
  `current_component=total`、`tide_included=true` 的含潮总流记录，B/C 在隔离实验目录完成 145 帧与 12 条路线。
  两个窗口都执行 `TOTAL_ONLY_FAIL_CLOSED`，detided 不得进入正式 bundle。

### 7.3 稳定验收闸门（源自：SCENARIOS_AND_SOURCES_归档_20260815.md；实源主线补全后执行）

正式 A 制品至少满足：

1. 使用共享版本化场景、走廊和船型；窗口起止、模式和知识截止显式；
2. 12 个必需类型逐项 `covers_requested_window=true`、`provenance_complete=true`、
   `complete=true`；
3. 每条记录有 UTC issue/valid/ingest、不可变 revision、checksum 和可复核 source snapshot；
4. `DatasetBundle v2` 通过 A 语义验证及共享包独立重算；v1 只作历史读取；
5. 跨进程 exact resolver 从公共 API 恢复完全相同的记录和 payload attestation；
6. doctor 对 ready/raw/sidecar/source snapshot 的路径、大小和 SHA-256 零错误；
7. 可选层独立报告；任何 fixture 或小窗 smoke 均不得替代真实完整窗。

### 7.4 Winter 含潮总流原生网格与退役规则（2026-08-26 02:20 +08:00）

- 正式 current 首选固定为 `ARCTIC_ANALYSISFORECAST_PHY_TIDE_002_015` /
  `dataset-topaz6-arc-15min-3km-be` 的 `originalGrid`；A 通过显式 projected `x/y` 查询
  保留 TOPAZ 原生 2D 经纬度，不把规则经纬度重采样伪装成 native source。
- `require_total_current=True` 是正式严寒窗口的硬门禁：服务不可用、变量不全或 provenance
  不完整时直接失败；detided 仅允许在显式非正式 fallback 试验中使用，且不得与含潮流相加。
- 2026-02-22～02-28 holdout 已通过 6 个分块的 total-only 获取；旧 canonical/实验
  detided current payload、raw/source current snapshot 与 detided bundle 已在本轮闭环完成后按
  `/root/my_project/.runtime/experiments/detided-retirement-20260826/cleanup-ledger-v2.json`
  精确退役（4359 文件、1448 条 manifest rows、1,318,695,475 bytes），冻结备份目录只作历史保护，
  不作为当前数据源。

## 8. 相关文档

- [A handoff](../work_package_a_handoff.md)
- [A→B 接口](AB_INTERFACE.md)
- [时间政策](ISSUE_TIME_POLICY.md)
- [共享 contracts](../../arctic_route_contracts/arctic_route_contracts_handoff.md)
- [系统权威](../../ARCTIC_ROUTE_SYSTEM.md)

## Vessel traffic simulation addendum

This branch adds `vessel_traffic` as a generated dynamic data type for the latest 144-hour Work Package A window. It is produced every 3 hours for both configured study corridors and exposes `traffic_density`, `traffic_count`, `traffic_risk`, and `traffic_confidence` through the same Work Package A frame contract as the environmental sources. The layer is required because complete historical AIS route traffic cannot reliably be fetched or redistributed with ordinary permissions, while Work Package B still needs a traffic-pressure input that resembles real-time route conditions.

Detailed configuration, output variables, provenance notes, and A-to-B usage are documented in `docs/VESSEL_TRAFFIC_SIMULATION.md`.
