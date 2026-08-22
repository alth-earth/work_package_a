# 冬季研究数据集交付报告（Work Package A）

> 生成时间：2026-08-22
> 关联提案：`A-WINTER-MET-001`（APPROVED 2026-08-22）
> 关联场景：`tromso_isfjorden_february_2026_research_v1`
> 关联走廊：`tromso_to_isfjorden_outer`（corridor `1.2.0`）
> 窗口：2026-02-15T00:00Z → 2026-02-21T12:00Z（144 h，末端对齐 02-21T12Z）

---

## 1. 结论

本会话为冬季研究场景补齐了缺失的气象三要素并闭合了 12 h 末端缺口，产出了一套
**12 类齐全、来源可查、doctor 零错误、G6 默认 horizon 通过、已冻结 bundle** 的冬季数据集。
全部 A 治理闸门（G5/G6）闭合，`winter_bundle` 已可作为下游 B/C/D 的不可变输入。

核心交付物：
- 冻结 bundle：`work_package_a/data/tromso_to_isfjorden_outer_winter_20260215T000000Z_bundle.json`
- 12/12 数据 complete，`all_required_complete = True`（默认 `horizon_hours=144`，不带 `--allow-incomplete`）

---

## 2. 解决了什么问题

### 2.1 原始问题

冬季研究场景要求 12 类数据覆盖 2026-02-15..02-21 窗口，但存在两个缺口：

1. **气象三要素缺口**：`wind_field` / `temperature` / `visibility` 原有 NCEI GFS 直链
   路径已在 NCEI 云迁移期间被撤下（`NoSuchKey`/404/403），NOMADS 仅保留滚动窗口
   （2026-02 历史周期 403），导致这三类 2026-02 数据无法获取。
2. **12 h 末端缺口**：整套 2026-02 数据末端停在 02-21T00Z，而场景名义 horizon=144
   使回放 `requested_end` 延伸到 02-21T12Z，造成每类 12 h 缺口，G6 校验
   `all_required_complete=false`。

### 2.2 解决方案

| 缺口 | 方案 | 结果 |
|---|---|---|
| 气象三要素 | 改用 **CARRA 单层级再分析**（C3S/ECMWF，`reanalysis-carra-single-levels`，DOI 10.24381/cds.713858f6） | 147 帧全发布（3 类 × 49 时次） |
| 12 h 末端缺口 | 方案 (a)：为 8 类动态非 CARRA 源补 02-21T03/06/09/12Z | 数据末端对齐 02-21T12Z，缺口闭合 |

---

## 3. 已完成的工作（时间线）

1. **提案批准**：`WINTER_DATA_POLICY_PROPOSAL.md` 状态 DRAFT→APPROVED，填 Approval Record。
2. **CARRA adapter 实现**：新增 `src/arctic_route_data/carra_acquisition.py`，不改动既有
   NCEI/Copernicus 路径；含 CDS 请求构造、cfgrib 解析、curvilinear regrid、经度包裹。
3. **关键工程结论**：CARRA 单层级地表风 `10u/10v` 的 `standard_name` 已是
   `eastward_wind`/`northward_wind`（真东/真北），**无需投影旋转**。
4. **离线单元测试**：`tests/unit/test_carra_acquisition.py`（12 项，全 PASS，无需网络/GRIB）。
5. **CARRA 全量采集**：`scripts/carra_full_acquisition.py` 发布 147 帧
   （route_id 初误用 `A-winter-carra`，已清理并改正确路由 `tromso_to_isfjorden_outer`）。
6. **方案 (a) 补采**：`scripts/winter_non_carra_tail_acquisition.py` 为 8 类动态非 CARRA 源
   补 02-21T03/06/09/12Z 末尾时次（`land_sea_mask` 为 static，未补）。
7. **校验通过**：doctor `ok:true`（5461 checked，0 errors）；G6 默认 horizon=144
   `all_required_complete=True`。
8. **bundle 冻结**：`replay`（不带 `--allow-incomplete`）闸门通过，原子写入 bundle JSON。
9. **文档同步**：proposal、operation guide、scenario status 三处一致反映收尾状态。

---

## 4. 交付物清单

### 4.1 数据与制品

| 制品 | 路径 | 说明 |
|---|---|---|
| 冻结 bundle | `work_package_a/data/tromso_to_isfjorden_outer_winter_20260215T000000Z_bundle.json` | DatasetBundle.v2，generation_id 0，574 KB |
| manifest | `work_package_a/data/manifest/manifest.sqlite3` | 5461 条校验通过 |
| 12 类 ready 数据 | `work_package_a/data/ready/tromso_to_isfjorden_outer/<data_type>/` | 02-15T00Z..02-21T12Z 全覆盖 |
| CARRA 原始 GRIB | `work_package_a/data/carra/_raw_grib/` | 56 个 GRIB 缓存，备用 |

### 4.2 代码

| 文件 | 说明 |
|---|---|
| `src/arctic_route_data/carra_acquisition.py` | CARRA 单层级采集 adapter |
| `src/arctic_route_data/forecast_acquisition.py` | 新增 NCEI 可达性状态机、NOMADS/HAS/CARRA/AWS 常量（既有 9 类路径未改） |
| `tests/unit/test_carra_acquisition.py` | 12 项离线单元测试 |
| `scripts/carra_full_acquisition.py` | CARRA 全量采集脚本 |
| `scripts/winter_non_carra_tail_acquisition.py` | 方案 (a) 非 CARRA 末尾补采脚本 |

### 4.3 文档

| 文件 | 说明 |
|---|---|
| `arctic_route_governance/current/proposals/WINTER_DATA_POLICY_PROPOSAL.md` | 已批准 + Ingestion Execution Record + open item RESOLVED |
| `arctic_route_governance/reports/research-validation/WINTER_CARRA_PLAN_B_OPERATION_GUIDE.md` | 方案 B 操作指南 + 方案 (a) 执行记录 |
| `arctic_route_governance/current/reference/WINTER_SCENARIO_STATUS.md` | Round5 closure + 12/12 complete + bundle FROZEN |

---

## 5. 关键工程结论（避免返工）

1. **风矢量无需旋转**：CARRA 单层级地表风已是真东/真北分量，早期担心的旋转风险对
   单层级不成立；若未来扩展 CARRA 压力层/模式风需重新评估。
2. **CDS API 变量名用 display name**（`10m_u_component_of_wind` 等），不能用 GRIB
   shortName（`10u` 等，会被 CDS 拒绝 400）；本地 cfgrib 解析才用 shortName。
3. **CARRA 分析场不接受 `leadtime_hour` 字段**，加上反而 400，必须剔除。
4. **`land_sea_mask` 是 static**（GEBCO 派生，与时间无关），不在 Copernicus 动态源里，
   补时次时需排除——"9 类非 CARRA"实际可补的是 8 类动态源。
5. **manifest 不可变**：publish 的 metadata 含 `observed_at`，重跑同
   (route_id, data_type, valid_time) 会因字节不同触发"已发布且内容不同"。补采必须只
   触碰全新时间片，或逐类独立 try 避免单类失败拖累已成功类。
6. **Copernicus 历史可达**：对 2026-02-21 历史窗口可正常返回（TOPAZ/CMEMS/NEXTSIM）。

---

## 6. 新凭据与新网站（使用用法）

### 6.1 新凭据（均不入库、不打印）

| 凭据文件 | 环境变量/指向 | 用途 |
|---|---|---|
| `work_package_a/.cdsapirc` | `CDSAPI_RC=<repo>/.cdsapirc`（权限 600） | CDS token，下载 CARRA |
| `work_package_a/.env.copernicus` | 内为 `export COPERNICUSMARINE_SERVICE_USERNAME/PASSWORD` | Copernicus Marine 账号，下载 TOPAZ/CMEMS/NEXTSIM |

> 注意：`.env.copernicus` 是 `export KEY=VALUE` shell 风格，且 CDS 与 Copernicus Marine
> 是两套独立服务，凭据不能互相替代。

### 6.2 新网站 / 数据源

| 数据源 | 门户 | 用途 |
|---|---|---|
| CARRA（C3S/ECMWF） | https://cds.climate.copernicus.eu/datasets/reanalysis-carra-single-levels | 风/温/能见度，3h，2.5km，East Arctic |
| Copernicus Marine | https://data.marine.copernicus.eu/ | ocean_current(TOPAZ6)、wave、water_level、sea_ice_*(CMEMS/NEXTSIM) |

### 6.3 运行时环境

```bash
cd /root/my_project/work_package_a
export LD_LIBRARY_PATH="$PWD/.mamba-env/lib:$LD_LIBRARY_PATH"   # eccodes C 库
export PATH="$PWD/.mamba-env/bin:$PATH"
# 下载 CARRA 时另需：
export CDSAPI_RC="$PWD/.cdsapirc"
```

### 6.4 关键命令

```bash
# doctor 完整性校验
uv run python -m arctic_route_data.cli doctor --data-root data

# CARRA 全量采集（重凭据、耗时）
uv run --extra acquisition python scripts/carra_full_acquisition.py

# 方案 (a) 非 CARRA 末尾补采
uv run --extra acquisition python scripts/winter_non_carra_tail_acquisition.py

# G6 覆盖校验（默认 horizon=144，即 Gate 6 闸门）
uv run python -m arctic_route_data.cli replay \
  --config configs/work_package_a.toml \
  --route-id tromso_to_isfjorden_outer \
  --mode retrospective_best_estimate \
  --at 2026-02-15T00:00Z \
  --knowledge-as-of 2026-08-22T12:00Z \
  --horizon-hours 144 \
  --types land_sea_mask ocean_current sea_ice_concentration sea_ice_drift \
          sea_ice_edge sea_ice_thickness sea_ice_type temperature visibility \
          water_level wave wind_field \
  --bundle-output data/tromso_to_isfjorden_outer_winter_20260215T000000Z_bundle.json \
  --summary-only
```

---

## 7. 验收证据

| 检查 | 结果 |
|---|---|
| doctor（方案 a 后） | `ok: true`，checked=5461，errors=[]，warnings=[] |
| G6 默认 horizon=144 | `all_required_complete = True`，12 类全 complete |
| replay 退出码（不带 `--allow-incomplete`） | 0 |
| bundle 持久化 | `bundle_persisted = True`，generation_id 0 |
| CARRA 帧数 | 147（3 类 × 49 时次） |
| 单元测试 | `test_carra_acquisition.py` 12 项 PASS；全 A `pytest tests/unit` 184 PASS |
| lint | `ruff check src tests` = All checks passed |

---

## 8. 需要注意 / 遗留

1. **下游未接入**：winter bundle 已冻结，但 B/C/D 尚未基于它做 smoke 验证（proposal 要求
   "smoke only after formal winter bundle"，属下一步可选动作）。
2. **murmansk 主走廊**：`offshore_murmansk_to_offshore_dikson` 仍是 RC1 基线（8 月 demo），
   尚无同等完整的冬季冻结窗；与本冬季数据集无关，如需另建场景。
3. **GRIB 缓存保留**：`data/carra/_raw_grib/` 56 个文件保留备用，勿删。
4. **凭据安全**：`.cdsapirc` / `.env.copernicus` 不入库、不打印、不提交。
5. **网络**：Copernicus/CDS 偶发 502，补采/下载宜后台长任务（`setsid ... &`）避免中断；
   明显失败不反复重试。
