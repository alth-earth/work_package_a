# 跨 A/B/C/D 边界导航（v2 当前摘要）

本文件只冻结依赖方向和 A 为下游提供的前置条件，不复制容易漂移的完整 Python
dataclass。当前合同真源为：

- 共享事实与运行身份：`/root/my_project/arctic_route_contracts/`；
- A→B：本仓库 [AB_INTERFACE.md](AB_INTERFACE.md) 与正式 `a.dataset-bundle.v2`；
- B 当前实现与运行入口：`/root/my_project/work_package_b/README.md`；
- B 历史开发要求与旧 ZIP 审计：
  `/root/my_project/work_package_b_handoff/工作包B-v2正式开发交接书.md`；
- B→C：`/root/my_project/work_package_c/docs/BC_CONTRACT.md`、Python 模型和
  `risk-frame-v2.schema.json`；
- C→D：`/root/my_project/work_package_c/docs/CD_CONTRACT.md`、Python 模型和
  `route-plan-v2.schema.json`。

## 1. 依赖方向和当前交付状态

```text
Shared facts ───────────────────────────────────────────────┐
    │                                                       │
    ▼                                                       ▼
A --StandardDataFrame + DatasetBundle--> B --RiskFrame v2--> C --RoutePlan v2--> D
                       RunContext v2 ────────────────────────┘
```

- A 是正式环境数据唯一入口；B 不扫描 A 的 incoming/raw/SQLite。
- B 负责逐小时连续化、风险、置信度、显式硬约束规则和
  `environment_speed_factor`；不生成路线。
- C 不调用 B 内部模型；负责最终船速、ETA、全局/滚动规划和 CD 发布。
- D 只读当前 RunContext 下通过身份围栏的结果。
- 2026-08-12 收到的 `工作包B.zip` 不是可复现正式 B：没有随包交付训练源码、权重或
  评估；包装脚本依赖 ZIP 外部缺失工程且默认 `skip_training=True`；11 个风险帧只覆盖
  约 81 h，间隔也不是逐小时；没有冰缘模型。不能把它写成“逐小时预测模型已交付”。

## 2. 全链路身份

| 字段 | 精确含义 |
|---|---|
| `run_id` | 一次不可变运行；BC/CD 原样传播 |
| `scenario_id` | 版本化的完整演示/实验场景 |
| `corridor_id` | 数据裁剪和允许航区；A 的 `route_id` 映射到这里 |
| `vessel_profile_id` | 共享船舶事实；当前 Nordic Odyssey 仅为公开参考、未标定 |
| `config_digest` | 共享 Scenario/Corridor/Vessel + A DatasetBundle 的内容摘要 |
| `model_config_digest` | B 自有风险/时间/插值规则摘要，不得冒充公共摘要 |
| `planner_config_digest` | C 自有船模/代价/规划/重规划规则摘要 |
| `generation_id` | seek/reset 隔离代次 |
| `as_of_time` | 当前合同名保留；在 A bundle 中表示 `knowledge_as_of` 门禁 |
| `valid_time` | 环境或风险描述的 UTC 时刻 |
| `generated_at` | 算法墙钟完成时刻，只用于审计和性能 |

`corridor_id` 不是 `scenario_id`，更不是具体 `plan_id`。任何身份或摘要不匹配，消费者
必须拒绝，不能通过重命名文件继续运行。

## 3. A 为 B 提供什么

每个 `StandardDataFrame`：

- 只有一个 `valid_time`；
- `issue_time <= knowledge_as_of`；
- 有 corridor、generation、source/version/checksum/quality；
- 有真实归档验证的 source snapshot 或 raw publication、内容 QC、网格 identity 和
  规范化摘要；普通插件自报 checksum 不能获得 provenance；
- payload 对消费者不可变；
- `PreparedWindow.payload_attestations` 逐 data ID 绑定完整 record 与规范 payload，B 在输入
  信封和 build 前独立重算并深快照；
- 可由 `prepare_window_for_b` 对调用方显式时域逐类检查最低覆盖、完整请求窗、内部缺口
  和 provenance；
- `DatasetBundle.v2` 以 `bundle_id + bundle_digest` 精确绑定所有实际选中帧，
  并逐类型绑定可重算的 cadence、support、gaps、provenance 与 complete 证明；v1
  只用于 legacy 读取，禁止创建正式 RunContext。
- 跨进程/重启后由 B 输入信封显式携带 `generation_id + knowledge_as_of + bundle`，
  通过 A 公共 `resolve_dataset_bundle_for_b()` 恢复精确 revision；B 不读取 A 的
  SQLite/ready/raw。RunContext 有 bundle ID/digest 和场景起止时间，但不含 generation、
  knowledge cutoff、当前 clock snapshot 或完整 bundle 文档；两侧身份必须交叉校验。

A 的规范注册表现有 14 类环境数据。船舶 CSV/参数属于共享 `VesselProfile`，不是 A 的
第 15 类。A 不提供共享目标风险网格，不输出风险或速度因子。

共享场景把其中 12 类固定为运行必需：风、温度、能见度、波浪、海流、水位、五类
海冰层和 `land_sea_mask`；`bathymetry`、`long_term_restricted_area` 是可选研究/信息
层。共享包会拒绝缺必需层或画像外类型；“可选”不表示 A 未实现，也不授予 hard-mask
语义。完整 14 类来源验收仍应包含两类可选层。

关键语义：

- `sea_ice_type` 是 A 对 neXtSIM 未来分量做确定性分类，不是 B 模型；
- `sea_ice_edge` 是 A 按 15% 冰密集度阈值确定性派生，不需要训练；
- 含潮总流优先、detided 仅显式后备，二者互斥且禁止相加；
- `bathymetry` 当前为研究静态层，不自动生成吃水/净水深 hard mask；
- `land_sea_mask` 是海陆事实分类；`source_valid_mask` 只是某源完整变量有效域，二者
  不能互相替代；
- EMODnet 四类法律图层保留来源语义，A 永不按名称自动生成 hard mask。

## 4. 场景、时间和数据窗口

时域不是固定 7 天、9 天、156 h 或 24 h：

- Murmansk–Dikson 默认 168 h，允许 144–216 h；
- Tromsø–Isfjorden 默认 96 h，允许 72–144 h；
- 超出走廊/来源能力返回 `forecast_coverage_insufficient`，不静默截断。

双场景必须分开：

- `retrospective_best_estimate` 是事后最佳估计，不能写成严格还原当时发布的预测；
- `frozen_forecast` 必须显式锚定 UTC 起点并冻结快照，禁止 implicit latest。

事后模式中 `simulation_time` 决定当前放出哪个 `valid_time`，`knowledge_as_of` 决定
允许看到哪些后来归档的 revision；因果模式中二者相同。每次演示必须按相同场景时间
重新运行 A、B、C、D。

0.3.1 的旧 9 类长窗 bundle
`a-bundle-c8b2c039c50f92086e3953e6` 仍是回归基线，但不能和 0.4.0 的新层 smoke
静默拼成 14 类正式输入。

## 5. B 必须向 C 输出什么

正式 `RiskFrame v2` 顶层冻结：

```text
schema_version="bc.risk-frame.v2"
risk_id
run_id, scenario_id, corridor_id, vessel_profile_id
config_digest, model_config_digest, generation_id
valid_time, as_of_time, generated_at, model_version
payload, source_summary, provenance="formal"
```

payload 当前为 EPSG:4326、严格递增一维 latitude/longitude 的二维网格：

| 变量 | 语义 |
|---|---|
| `risk_score` | `[0,1]` 连续软风险 |
| `risk_level` | `1..5` 解释/展示等级 |
| `hard_mask` | True 表示 C 不得扩展 |
| `confidence` | `[0,1]` 数据、时间处理与模型置信度 |
| `environment_speed_factor` | `(0,1]`；B 的综合环境速度影响 |

B 的近期 MVP 应是确定性、可审计的逐小时连续化；缺测、低质量、窗外目标或无因果
支撑时 fail closed。若以后增加预测模型，必须随包交付训练代码、权重、训练数据身份、
评估和独立 `model_config_digest`。

B 不输出最终有效船速。C 用 `environment_speed_factor × 船型静水基准速度` 计算最终
速度，并做自己的最低操舵/可行性检查；不得再从 risk/confidence 重复折减。

## 6. C 的四层路线责任

导师提出的结构已纳入 C 路线图：

1. 全航程参考线：覆盖实际完整航程，不固定为 9 天，判断大尺度航道与通道；
2. 24–72 h 主通道：判断未来进入哪个冰区通道；
3. 0–24 h 滚动优化：高精度气象导航和冰区避险；
4. 0–6 h 可执行线：0–2 h 高可信、2–4 h 推荐、4–6 h 预测。

四层共享一个 RunContext，并分别有 request/revision 围栏。当前 C 已迁移 v2 合同并
保留现有时间依赖 A*；四层仍是增量开发路线，不应把文档写成全部算法已实现。

C 当前采样继续遵循：连续风险双线性/时间线性，hard mask 对参与格点和时界取 OR，
confidence 与速度因子取保守最小；不外推风险、不把缺测当安全、不静默吸附被硬掩膜
阻断的端点。

## 7. 来源追踪和法律/安全边界

B 的 `source_summary` 至少逐项映射 A：

```text
source_id <- record.source
data_id <- record.data_id
issue_time <- record.issue_time
valid_time <- record.valid_time
version <- record.version
quality_flag <- record.quality_flag.value
checksum <- record.checksum
```

一帧使用多个变量、时刻或 revision 时要全部列出并确定性去重。A 的 quality 是来源
证据与内容 QC 下界，不等于 B 的 confidence。

军事区、保护区、规划区等名称不天然等于法律禁航；B/C 只能用显式、版本化且有依据的
规则编译 hard/soft/information。未知法律效果不得自动 hard-mask，也不得自动当安全。

## 8. 联调最小验收

### A→B

- 两包读取同一 `RunContext.v2` 和精确 A bundle；
- 每个必需层 `covers_requested_window=true`、`provenance_complete=true`；
- corridor、generation、simulation/knowledge 时间一致；
- 网格覆盖共享场景 bbox，来源可追到 A checksum/snapshot；
- 矢量、波向、分类、水深和 mask 使用正确处理，不补零当安全。

### B→C

- `bc.risk-frame.v2` Python 模型和 JSON Schema 均通过；
- `run_id/config_digest/model_config_digest` 完整且身份串线会被拒绝；
- 逐小时闭区间覆盖实际 ETA，不只 24 h；
- 缺测、低 confidence、硬掩膜和超预测窗 fail closed；
- 同一 B 参数原样迁移到第二航区，C 核心无需因输入工厂变化而修改。

### C→D

- `RoutePlan v2` 通过模型和 Schema；
- 原样传播公共身份并另带 `planner_config_digest`；
- D 只展示同一场景/代次的最新 revision；旧任务迟到结果不得重新激活。

正式执行顺序是：共享场景 → A 全窗采集/回放 → DatasetBundle → RunContext → B → C
→ D。当前 `work_package_b/` 已完成 `demo_unvalidated` 确定性工程基线，并以正式公共
接口通过 12 类、动态时域和持久 committed-window 的夹具联调；当前真实 A 长窗仍是
v1/9 类/旧 corridor，故端到端**实源**验收仍是明确待办，不能把夹具通过改写为实源完成。
