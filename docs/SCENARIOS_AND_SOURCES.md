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
- 当前缺口：主走廊恰好 12 类、168 h 的真实 DatasetBundle v2、RunContext、doctor 和
  exact resolver 仍未形成完整证据；latest v2 Copernicus 与 total-with-tide 需复验；
  2026-08-15 已另交付 `tromso_isfjorden_august_2026_demo_v1`（12 类/144 h 冻结演示数据，
  `complete=true`）作为当前演示底座；
- 真实 A→B→C 端到端仍未完成。

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

## 8. 相关文档

- [A handoff](../work_package_a_handoff.md)
- [A→B 接口](AB_INTERFACE.md)
- [时间政策](ISSUE_TIME_POLICY.md)
- [共享 contracts](../../arctic_route_contracts/arctic_route_contracts_handoff.md)
- [系统权威](../../ARCTIC_ROUTE_SYSTEM.md)
## 9. 航道通行情况可选层

`vessel_traffic` 是工作包 A 新增的可选动态数据层，用于接收航道通行情况模拟模型生成的
NetCDF 数据，并将其纳入 A 既有的 ready/manifest 管理体系。该层不属于 12 类必需环境数据，
不改变工作包 A 原有的数据完整性判定，也不替代任何官方环境数据源。

| `data_type` | 来源 | 主变量 | 单位/范围 | 用途 |
|---|---|---|---|---|
| `vessel_traffic` | AIS 与公开通航统计校准的航道通行情况模拟模型 | `vessel_traffic_risk` | 0-1 | 为工作包 B 提供通航拥挤度、邻近船舶干扰和航道活跃程度等可选动态风险因子 |

引入该层的原因是：两条北极研究航线的连续历史 AIS 通航轨迹难以直接、稳定、开放获取；
部分历史 AIS 接口需要额外授权，公开资料又多以年度报告、航次统计或区域摘要形式发布，
难以直接转化为逐时、逐网格的模型输入。因此，项目在不破坏官方环境数据体系的前提下，
以通航情况模拟模型补齐 B 侧对“实时通航状态相似输入”的需求，并在 manifest 中默认标记为
`suspect`，提示下游该层属于模拟增强数据而非权威实测 AIS 数据。

该层导入后仍遵循 A 的统一规范：空间范围使用既有航线 `route_id`，时间字段写入
`issue_time` 与 `valid_time`，文件进入 `data/ready/<route_id>/vessel_traffic/`，
元数据进入 manifest。B 侧读取时应把它作为可选特征使用，若缺失不应导致 A 的 12 类必需层
完整性失败。
