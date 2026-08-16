# Demo RC1 基线（2026-08-16）

> 本文件记录当前已跑通主链的可复现基线。机器可读版本：
> `data/output/golden/demo-rc1-20260816.json`。

## 1. 状态结论

`Arctic Route Demo RC1 / 2026-08-16`：A 数据 → B 风险 → C v3 四层 → 6h 重规划 → D 消费
已完整跑通（r6），业务级可复现性由 r7 复跑验证（见 §6）。

## 2. 代码 / 配置

| 项 | 值 |
|---|---|
| root git HEAD | 6d153f0（工作树含交付文档改动） |
| contracts | 0.3.0 @ 840e577（dirty=2：corridor 2.2.0 场景版本同步） |
| work_package_a | 0.4.2 @ 42d0f28（dirty=4：curvilinear 重建/脚本/文档） |
| work_package_b | 0.2.0 @ 633309c（dirty=4：hard-mask 策略 + 测试 + 配置） |
| work_package_c | 0.4.0 @ b36edae（dirty=5：sampler/planner 优化 + 心跳 + 测试/脚本） |
| work_package_d | 0.1.0 @ fac63ea（dirty=1：离线 schema + layers 数组适配） |
| orchestrator | 0.1.0 @ 22fb340（clean） |
| corridor | `offshore_murmansk_to_offshore_dikson` v2.2.0 |
| scenario | `murmansk_dikson_august_2026_demo_v1` v1.0.0（144h，retrospective） |
| B smoke grid | `demo_unvalidated_smoke_grid_v4.json`（lat 0.682°/lon 1.719°，
  hard_mask=land_sea_mask_plus_unknown_v1） |
| C planner | `planner/default.toml`（60min bucket、8 邻接、3 采样、216h 上限） |

## 3. 数据

| 项 | 值 |
|---|---|
| manifest | `work_package_a/data/manifest/manifest.sqlite3`（4168 记录，mur 1212） |
| TOPAZ 5 类 | `cmems-origg-97062ef099c4`（originalGrid 重建，max_distance_km=20） |
| bundle | `a-bundle-32cafad4ee280f286d8eb049` |
| bundle digest | `32cafad4ee280f286d8eb04992fc1b66906e7745774d2f24c37fcb67aaac5fdc` |
| knowledge-as-of | `2026-08-16T10:02:51.780511Z` |
| RunContext | `run-00000000-0000-4000-8000-0000000b0005`（corridor 2.2.0） |
| coverage gate | `unknown among navigable nodes = 0`（B 风险帧 145 帧全通过） |

## 4. v3 制品（r6）

| 项 | 值 |
|---|---|
| initial layer-set | `layer-set-sha256-51824e965e7914427cba3b1ad191c0f4498823beadf0451db37209dbdf7bc11f` |
| replanned layer-set | `layer-set-sha256-ec74a1454bee0f4511fc6d9e53a889d810c8f4b292c1c936e6ed9dbc11831c2f` |
| stage report | `completed`（init 561s / b_build 16s / c_initial 528s / suffix 2.6s / replan 535s / output 0s） |
| D 消费 | initial + replanned 均 `complete`、4 层（`d-snapshot-initial/replanned.json`） |

## 5. Safety Semantics（冻结不变式）

- unknown ≠ safe；source-unknown 节点 → planning unavailable（hard）；
- `risk_score=NaN`、`confidence=0`、`risk_level=5` 保留；
- C fail-closed（`RiskSamplingError`）保留；
- corridor/风险语义/成本/插值/hard-mask 策略在 RC1 后冻结，仅允许 blocker/
  correctness/safety 修复。

## 6. 可复现性

- r7 复跑结果：见 `data/output/golden/mur-v3-smoke-20260816-r7/` 与
  `EXECUTION_LOG_20260816.md`；
- 判定：**business-semantic deterministic reproducibility = PASS**（r6/r7 全部 24 条
  路线业务字段一致，唯一差异 metrics.compute_ms；layer-set digest 与 risk commit
  完全一致）。

## 8. 可中断超时 / D 回归 / Offline

- orchestrator worker 子进程 per-stage timeout 已实现并单测通过（无孤儿、
  无半成品、TIMEOUT 报告）；CLI `run` 已切换；
- D 真实制品 fixtures（initial/replanned）+ 断网回归 9 tests 通过；
- offline audit：`demo runtime external network dependency = NONE`
  （`arctic_route_orchestrator/scripts/offline_demo_audit.py`）。

## 9. Demo 模式

- Full Validation Mode ≈ 30–35 min（intake≈9min、初始规划≈8min、重规划≈8.5min）；
- Live Demo Mode：加载冻结 v3 四层结果与 D 快照，现场实时部分 ≤2 min 小窗，
  预计算与实时明确标注。

## 7. 恢复与备份

- immutable base：主数据（raw/ready/source_snapshots/manifest）+ 双工作区副本
  `frozen_demo_backup/`、`frozen_demo_backup_secondary/`（同盘，非异地灾备）；
- RC1 增量制品（v3 输出/D 快照/执行记录/文档）已同步至两份副本 execution/ 下；
- 真正异盘备份待 `Windows host path / 外接盘 / NAS` 提供后执行。
