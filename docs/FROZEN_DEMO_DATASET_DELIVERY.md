# 冻结演示数据集交付说明

> 生成时间：2026-08-15（Asia/Shanghai）
> 关联场景：`tromso_isfjorden_august_2026_demo_v1`
> 关联走廊：`tromso_to_isfjorden_outer`

## 1. 结论

已交付一套**来源可查、12 类齐全、连续 144 h** 的冻结演示数据，12 类覆盖全部
`complete=true`，bundle 与 RunContext 已生成并做双位置备份。

说明：首选目标是 168 h，但共享 corridor 的时域政策上限为 144 h（`tromso_to_isfjorden_outer`
允许 72–144 h），合同在 168 h 时正确拒绝；因此按“凑不齐 168 则使用走廊允许上限”的原则，
冻结窗取 144 h（2026-08-11T06:00Z → 2026-08-17T06:00Z），这属于走廊政策内的最大窗，不是
失败降级。

## 2. 网络与采集约定（本会话起长期有效）

所有下载/抓取/访问任务遵守：

1. **代理判断**：先测直连与代理可用性，取可用且快者；明显失败不反复重试。
2. **人机验证与凭证**：多数数据源有人机验证或登录要求，自动化无法绕过时停止重试。
3. **处理原则**：能自动完成则自动完成；遇到验证码、凭证、封锁、限流时，说明问题并给出
   可照做的替代方案（换源、换时段、手动下载步骤），由项目负责人手动执行。
4. **落档要求**：网络结论与操作步骤写回本文档与 `ARCTIC_ROUTE_SYSTEM.md` 数据获取章节。

本批次实测：直连 NOMADS=200、CMEMS=307（正常跳转）、GEBCO=200；本地代理
`127.0.0.1:10808` 无效，故采用直连。Copernicus 使用 `.env.copernicus` 凭据，无需人机验证。

## 3. 交付内容

### 3.1 场景与身份

- 场景：`tromso_isfjorden_august_2026_demo_v1`
- 模式：`retrospective_best_estimate`（数据为 2026-08-15 事后下载，知识截止显式）
- 知识截止：`2026-08-15T09:37:34.830829Z`（等于 bundle 内最大记录 issue_time，满足
  orchestrator intake 的 as_of == max issue 校验）
- 走廊：`tromso_to_isfjorden_outer`（bbox 10–22°E，68.5–79.5°N）
- 船型：`nordic_odyssey_reference_v1`（演示散货船）
- run_id：`run-00000000-0000-4000-8000-0000000a0001`

### 3.2 制品

| 制品 | 位置（主仓） | 备份 1 | 备份 2 |
|---|---|---|---|
| DatasetBundle v2 | `work_package_a/data/output/bundles/tromso_isfjorden_august_2026_demo_v1.bundle.json` | `frozen_demo_backup/tromso_isfjorden_aug2026/` | `/tmp/arctic_demo_backup/tromso_isfjorden_aug2026/` |
| RunContext v2 | 同目录 `*.run-context.json` | 同上 | 同上 |
| manifest | `work_package_a/data/manifest/manifest.sqlite3` | 同上 | 同上 |
| ready 帧 | `work_package_a/data/ready/tromso_to_isfjorden_outer/` | 同上 | 同上 |

> 2026-08-15 因 bundle as_of_time 修正（09:38:00Z → 09:37:34.830829Z），旧版 RunContext
> 保留为 `*.r1.run-context.json`（主仓与两份备份均保留），当前规范文件为
> `*.run-context.json`。

### 3.3 覆盖矩阵（144 h 窗口）

| data_type | 帧数/步长 | complete | 来源 |
|---|---|---|---|
| land_sea_mask | 1（静态） | ✅ | GEBCO 2026 |
| ocean_current | 145 × 1h | ✅ | Copernicus（detided 后备，显式记录） |
| sea_ice_concentration | 145 × 1h | ✅ | Copernicus |
| sea_ice_drift | 145 × 1h | ✅ | Copernicus |
| sea_ice_edge | 145 × 1h | ✅ | Copernicus 派生 |
| sea_ice_thickness | 145 × 1h | ✅ | Copernicus |
| sea_ice_type | 145 × 1h | ✅ | Copernicus 派生 |
| temperature | 49 × 3h | ✅ | GFS 0.25° |
| visibility | 49 × 3h | ✅ | GFS 0.25° |
| water_level | 145 × 1h | ✅ | Copernicus |
| wave | 49 × 3h | ✅ | Copernicus（PT3H 产品） |
| wind_field | 49 × 3h | ✅ | GFS 0.25° |

> 注：GFS/Copernicus 部分产品为 3 h 原生步长，B 的逐小时处理负责对齐；窗口覆盖判定按来源
> 原生步长通过。

## 4. 差距与说明

1. 首选 168 h 未达成：corridor 政策上限 144 h 所致（非数据缺失），已在场景中固化 144 h。
2. `ocean_current` 含潮总流产品当前不可用，已显式降级为 detided 后备并在采集日志注明，
   两者未相加。
3. GFS/Copernicus 波浪与大气层为 3 h 原生步长，B/C 演示按现有合同处理；若后续希望小时级
   波动细节，需要换产品（届时另行评估）。
4. 主走廊 `offshore_murmansk_to_offshore_dikson` 尚未形成同样完整的冻结窗，属已知差距；
   若演示需要长航线，需另行采集 7 月窗口或新建场景。

## 5. 数据缺失时如何恢复

### 5.1 备份位置

- 备份 1：`${ARCTIC_ROUTE_ROOT}/frozen_demo_backup/tromso_isfjorden_aug2026/`
- 备份 2：`/tmp/arctic_demo_backup/tromso_isfjorden_aug2026/`

### 5.2 恢复步骤

```bash
# 以备份 1 为例；备份 2 同构
BACKUP=${ARCTIC_ROUTE_ROOT}/frozen_demo_backup/tromso_isfjorden_aug2026
cd ${ARCTIC_ROUTE_ROOT}/work_package_a
cp "$BACKUP/tromso_isfjorden_august_2026_demo_v1.bundle.json" data/output/bundles/
cp "$BACKUP/tromso_isfjorden_august_2026_demo_v1.run-context.json" data/output/bundles/
cp "$BACKUP/manifest.sqlite3" data/manifest/manifest.sqlite3
rm -rf data/ready/tromso_to_isfjorden_outer && cp -a "$BACKUP/tromso_to_isfjorden_outer" data/ready/
```

恢复后运行：

```bash
cd ${ARCTIC_ROUTE_ROOT}/work_package_a && make doctor
```

`doctor` 必须返回 `errors: []`。随后用 `arctic-data replay` 复验 12 类覆盖，再进入 B→C→D。

### 5.3 现场快速替代

若演示现场连备份也丢失，可重新采集（需外网与 Copernicus 凭据）：

```bash
cd ${ARCTIC_ROUTE_ROOT}/work_package_a
export UV_CACHE_DIR=${ARCTIC_ROUTE_ROOT}/work_package_a/.uv-cache2 UV_PYTHON_INSTALL_DIR=${ARCTIC_ROUTE_ROOT}/work_package_a/.uv-python2
arctic-data acquire-forecast --corridor tromso_to_isfjorden_outer \
  --contracts-config-root ../arctic_route_contracts/configs \
  --sources gfs copernicus gebco --types land_sea_mask ocean_current sea_ice_concentration \
  sea_ice_drift sea_ice_edge sea_ice_thickness sea_ice_type temperature visibility water_level wave wind_field \
  --start 2026-08-11T06:00:00Z --end 2026-08-17T06:00:00Z --mode retrospective_best_estimate \
  --copernicus-env-file .env.copernicus
```

现场网络不可用时应停止并改用备份，不现场拼数据。

## 6. 后续建议

- 比赛前至少做一次“删除 ready → 从备份恢复 → doctor → replay”的演练；
- 主走廊若进入演示候选，另建 7 月场景并补齐 12 类/144–168 h 后再交付；
- 冻结数据与凭据不得进入 Git；备份目录加入 `.gitignore` 或放在 `.git` 外。
