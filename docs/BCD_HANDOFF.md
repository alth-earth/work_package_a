# 跨 B/C/D 边界导航（A 侧摘要）

这份文件不再复制一套容易漂移的 BC/CD dataclass。当前状态是：

- A→B：以本仓库 [AB_INTERFACE.md](AB_INTERFACE.md) 为真源；
- B→C：以 `/root/my_project/work_package_c/docs/BC_CONTRACT.md`、Python 模型和
  `risk-frame-v1.schema.json` 为真源；
- C→D：以 `/root/my_project/work_package_c/docs/CD_CONTRACT.md`、Python 模型和
  `route-plan-v1.schema.json` 为真源；
- B 待完善项：以
  `/root/my_project/work_package_b_handoff/工作包B矛盾与完善开发交接书.md` 为实施清单。

本文件只冻结依赖方向和 A 必须为下游提供的前置条件。

## 1. 依赖方向

```text
A --StandardDataFrame--> B --RiskFrame v1--> C --RoutePlan v1--> D
```

- B 不扫描 A 的 incoming/raw，不根据文件名恢复时间；
- C 不读取 A 的 SQLite/缓存，也不调用 B 内部模型；
- D 不调用 C 求解器；
- 各边界只交换不可变、版本化对象和明确查询接口。

## 2. 全链路共同身份

| 字段 | 精确含义 |
|---|---|
| `scenario_id` | 一次完整演示/试验配置 |
| `corridor_id` | 数据裁剪和允许航区；A 的历史字段 `route_id` 映射到这里 |
| `vessel_profile_id` | 船型/性能上下文；当前 demo 船型明确 `demo_unvalidated` |
| `config_digest` | 一次运行所用共享配置快照 SHA-256 |
| `generation_id` | seek/reset 隔离代次 |
| `as_of_time` | 本次任务允许知道信息的截止 UTC 时刻 |
| `valid_time` | 环境/风险描述的 UTC 时刻 |
| `generated_at` | 算法墙钟完成时刻，只用于审计/性能 |

`corridor_id` 不是 `scenario_id`，更不是具体 `plan_id`。

## 3. A 为 B 提供什么

每个 `StandardDataFrame`：

- 只有一个 valid time；
- `issue_time <= as_of_time`；
- 有 `route_id/corridor_id`、generation、source/version/checksum/quality；
- 有 source snapshot、内容 QC、网格 identity 和规范化摘要；
- payload 不可变；
- 可用 `prepare_window_for_b` 检查 156 h 目标、132 h 最低覆盖和内部缺口。

A 不提供共享目标风险网格，不输出风险或速度因子。

## 4. B 当前必须向 C 输出什么

正式 `RiskFrame v1` 顶层至少冻结：

```text
schema_version="bc.risk-frame.v1"
risk_id
scenario_id, corridor_id, vessel_profile_id, config_digest, generation_id
valid_time, as_of_time, generated_at, model_version
payload, source_summary, provenance="formal"
```

payload 当前必须是 EPSG:4326、严格递增的一维 latitude/longitude 二维网格：

| 变量 | 语义 |
|---|---|
| `risk_score` | `[0,1]` 连续软风险 |
| `risk_level` | `1..5` 解释/展示等级 |
| `hard_mask` | True 表示 C 不得扩展 |
| `confidence` | `[0,1]` 数据+预测+模型置信度 |
| `environment_speed_factor` | 正式帧必需，`(0,1]`；B 的综合环境影响 |

责任冻结：B 不输出最终有效船速；C 用
`environment_speed_factor × 船型静水基准速度` 计算最终有效航速，并做最低安全/操舵
检查。C 不从 `risk_score` 或 `confidence` 再折一次速度，避免双重计权。

## 5. 为什么不能只生成 24 h

当前演示最长航程约 5.5 天。C v1 不支持在规划图中“等待未来风险帧”，也不在 BC
窗外外推。因此 B 必须按请求覆盖实际 ETA 时域；A 的默认目标 156 h、最低 132 h
就是为此设置。

短窗滚动可以作为未来优化，但必须先实现：

- 明确滚动触发；
- 新旧 RiskFrame/RoutePlan 的 revision 围栏；
- 当前路线在窗尾的安全策略；
- 缺帧/服务失败时 fail closed；
- 跨窗口可重复验收。

在这些机制完成前，不得把 24 h 风险图冒充完整 2–5.5 天航程输入。

## 6. C 当前的采样规则

- `risk_score`：空间双线性、时间线性；
- `hard_mask`：所有参与单元和时间边界取 OR；
- `confidence`、`environment_speed_factor`：参与值取保守最小；
- `risk_level`：由采样后的 risk score 重算；
- 上下文、网格、窗口或帧间距不匹配：拒绝，不当作安全。

B 做 A→目标网格处理时：矢量分量分别插值、波向圆周插值、分类/掩膜 nearest/
保守处理；禁止把 A 的 `quality_flag` 直接复制成 confidence。

## 7. 来源追踪

正式 B 的 `source_summary` 应逐项映射 A：

```text
source_id <- record.source
data_id <- record.data_id
issue_time <- record.issue_time
valid_time <- record.valid_time
version <- record.version
quality_flag <- record.quality_flag.value
checksum <- record.checksum
```

所有正式来源 `issue_time` 非空且 `<= RiskFrame.as_of_time`。若一帧使用多个输入时刻/
变量，必须全部列出并确定性去重。

## 8. 限制区和硬掩膜

A 只验证 GeoJSON 几何、经纬度、来源/authority 和可选
`navigation_effect=hard|soft|information`，并固定
`automatic_hard_mask_allowed=false`。

海洋保护区、规划区、军事区等图层名不天然等于法律禁航。B/C 必须由版本化场景政策
决定 hard/soft；未知法律效果至少降低置信度，不能自动硬屏蔽，也不能自动当安全。

## 9. 共享配置过渡

当前可运行配置仍在 `work_package_c/configs/`，包含 scenario、vessel、planner 和
replanning。A 只维护采集 corridors，并已把 Tromsø bbox 对齐到
`[10.0, 68.5, 22.0, 79.5]`。

但空间一致不等于时间一致：C 现有 `demo_tromso_to_svalbard_v1` 时域为
`2026-07-31 .. 2026-08-03`，A 本轮真实 GFS 时域为
`2026-08-11 .. 2026-08-18`。联调必须新增版本化 scenario/dataset snapshot 和新的
config digest，不能在旧 ID 下偷换时间与数据。

未来迁移到共享 `demo_scenarios/` 与 `contracts/` 时保持 ID、version、字段和 digest
语义不变，只改变配置根路径。A/B/C/D 不得各复制一份后独立修改。

## 10. 联调最小验收

### A→B

- 每个必需 A 层 `CoverageReport.complete=true`；
- route/corridor、generation、as-of 一致；
- 网格覆盖共享场景 bbox；
- source summary 可回到 A checksum/source snapshot；
- 矢量/波向/水深语义正确。

### B→C

- RiskFrame 正式字段和 Schema 全过；
- 覆盖实际 ETA，不只 24 h；
- `environment_speed_factor` 存在且 C 不二次折减；
- 缺测、低置信、hard mask 均 fail closed；
- seek 与旧 revision 不能覆盖新结果。

### C→D

- RoutePlan 通过 C 的正式 Schema/模型；
- D 只展示当前 scenario/generation 最新发布；
- 新结果计算中可展示上一版但必须标注；
- 旧代次、旧 request/revision 不得重新激活。
