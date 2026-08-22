# RC2 冻结基线（2026-08-17）

> 本文件记录 RC2 多场景冻结基线。Scenario A（RC1）以
> `DEMO_RC1_BASELINE_20260816.md` 为权威；本文件为 Scenario B（Tromsø）
> 与运行/性能决策的权威记录。

## 1. 状态结论

**RC2 Frozen Baseline = ESTABLISHED（2026-08-17）**。判定依据：

- RC1 golden regression = PASS（r6/r7 digest/checksums 不变，业务输出一致）；
- Scenario B（Tromsø → Isfjorden）144h qualification r1 = PASS；
- Scenario B 144h reproducibility r2 = PASS（layer-set digest 与业务输出与 r1
  完全一致）；
- 双场景 regression（`rc1_golden_regression.py` +
  `rc2_second_scenario_regression.py`）= PASS；
- 内存优化已决策（采用 consumer_view 共享只读数组 + PreparedWindow/envelope
  释放），并行已决策（objective 级 2-worker = EXPERIMENTAL / 正式路径串行）。

## 2. Scenario A（RC1 Golden）

- 权威：`DEMO_RC1_BASELINE_20260816.md`；
- corridor 2.2.0 / bundle `a-bundle-32cafad4…` / RunContext `run-…0b0005`；
- initial `layer-set-sha256-51824e96…`、replanned `layer-set-sha256-ec74a145…`；
- 本阶段仅 B 帧新增 provenance attrs（不影响业务），RC1 golden 文件未改。

### 2.1 Repository versions（RC2 Frozen）

| Repo | SHA |
|---|---|
| root | `e3f43f2` |
| contracts | `54ee071` |
| work_package_a | `5834575` |
| work_package_b | `6269420` |
| work_package_c | `ccd1e53` |
| work_package_d | `0539f31` |
| arctic_route_orchestrator | `cccad9f` |

Demo Engineering 分支 `demo-engineering` 从以上 RC2 Frozen commits 派生。

## 3. Scenario B（RC2 Golden）

| 项 | 值 |
|---|---|
| scenario | `tromso_isfjorden_august_2026_demo_v1`（144h） |
| corridor | `tromso_to_isfjorden_outer` v1.2.0 |
| bundle | `a-bundle-e1e3365fdf9922dcaad0b79e` |
| RunContext | `run-00000000-0000-4000-8000-0000000a0006` |
| B policy | `land_sea_mask_plus_unknown_ice_free_v1` |
| B grid | `demo_unvalidated_tromso_smoke_grid_v1.json`（0.375°×1.25°） |
| initial layer-set | `layer-set-sha256-e135e32d8a95dc7fb07ac536cc44c0ce455713355d970cf8a691962bbe3ec51f` |
| replanned layer-set | `layer-set-sha256-6e101345c48fda47cd4f77a3e39c00000a5c205bff29782488bfe80359717f42` |
| coverage | 145/145 gate=true；navigable=255；unknown=0；LAND=65；DATA_UNAVAILABLE=21；ice_free_neutralized=57 |
| r2 复现 | digest/business 与 r1 完全一致 |

## 4. 运行与性能决策

- Runtime：orchestrator worker + watchdog 可中断超时（真实验证）；
- Planner：**串行为正式默认**；objective 级 2-worker prototype =
  `EXPERIMENTAL / NOT ADOPTED`（benchmark speedup 1.48–1.49×，但正式 v3
  集成还需 timeout/lease/atomic-publication 硬化，暂不合并）；
- 内存：RC1 worker 峰值 4.18GB → 2.81GB（−33%）；Tromsø 144h 1.40GB → 0.97GB
  （−31%）；业务输出不变。

## 5. Safety Semantics（不变）

- unknown ≠ safe；source-unknown → hard（DATA_UNAVAILABLE）；fail-closed 保留；
- `hard_reason`（NONE/LAND/DATA_UNAVAILABLE/OTHER）；
- ice-free NOT_APPLICABLE：`0 <= TOPAZ siconc < 0.15`（权威阈值
  `ICE_EDGE_CONCENTRATION_THRESHOLD`）时 NEXTsim ice_type/edge 中性化为 0，
  其余 unknown 仍 fail-closed；provenance attrs + coverage preflight 可解释。

## 6. 冻结范围

RC2 冻结后仅允许 blocker / correctness / safety 修复；数据产品、corridor 1.2.0、
Scenario B、risk/hard-mask/ice-free 语义、C cost、规划算法、RC2 golden 制品
不得随意修改。
