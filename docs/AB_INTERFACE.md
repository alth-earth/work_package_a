---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
Document Role: CANONICAL
Scope: A to B public interface
Branch: research-validation-system
Last Verified: 2026-08-21
---

# A → B（AB）接口 v0.4.2

本文是工作包 A 已实现接口的真源。B→C 的正式合同以
`work_package_c/docs/BC_CONTRACT.md`、C 的 Python 模型和 JSON Schema 为准。

## 1. 身份与时间

- A 的历史字段 `route_id` 等同全系统 `corridor_id`；它表示数据裁剪/允许航区。
- `scenario_id` 表示完整演示/试验上下文，由共享配置/编排层创建；A 不把它塞进 `route_id`。
- `issue_time`：系统从何时起允许知道该帧。
- `valid_time`：环境场描述的时刻。
- `ingest_time`：A 实际登记时刻。
- 全部时间是带时区 UTC。

任何 B 任务都应再次断言：

```python
assert frame.record.route_id == corridor_id
assert frame.record.issue_time <= knowledge_as_of
assert frame.generation_id == generation_id
```

## 2. 稳定对象

```python
@dataclass(frozen=True, slots=True)
class StandardDataFrame:
    record: ManifestRecord
    payload: xr.Dataset | Mapping
    generation_id: int
```

`ManifestRecord` 必需字段：

```text
data_id, data_type, category, route_id, variables,
issue_time, valid_time, ingest_time,
bbox, crs, resolution,
source, quality_flag, version, checksum,
relative_path, size_bytes, media_type, metadata
```

`metadata` 中 B 应保留的关键内容：

- `issue_time_evidence`；
- `source_snapshot_id`；
- `source_snapshot_relative_path/source_file/source_file_checksum`（原生采集帧）；
- `nominal_interval_hours`（原生采集帧的源 cadence）；
- `forecast_reference_time/forecast_lead_hours`（预报帧）；
- `normalization.grid_id/coordinate_digest/grid_topology`；
- `normalization.variables` 中的源变量、源单位、规范单位和矢量参考系；
- `content_qc`；
- 上游 checksum、产品/数据集 ID 和裁剪请求摘要（存在时）。

元数据会深度冻结。`consumer_copy()` 会为 xarray/Mapping payload 创建不共享可写数组的
深拷贝并把数组设为只读；B 不得原地修改 payload，应创建自己的派生 Dataset。只读位本身
不是信任边界，正式消费还必须复核下述 payload attestation。

`content_qc.source_valid_mask` 若 `present=true`，只表示原生 Copernicus
完整请求中派生的源数据空间有效域。新 `a.source-valid-mask.v2` 要求所有必需源变量
在完整请求窗内均有有限值；旧 v1 的任一变量语义只为历史兼容。`structural_mask_fraction` 是该域之外的
结构无效占比，`valid_domain_missing_fraction` 才是域内残余缺测。这个 mask
明确没有 navigation/classification 语义，B 不得将它直接转为陆海掩膜或
`hard_mask`。旧帧没有显式证据时，A 不推断这一掩膜。
普通 direct/sidecar producer 不能凭自报 attrs 获得该语义；A 要求 mask 与归档
Copernicus source snapshot 的精确路径、SHA-256、dataset ID、请求起止时刻绑定，
并对快照内 mask 做逐值、坐标和语义一致性检查。它不能替代正式
`land_sea_mask`；后者也只是海陆分类，不自动等于可通航 `hard_mask`。

## 3. 首选入口：一次性准备一致窗口

```python
prepared = a.prepare_window_for_b(
    route_id="offshore_murmansk_to_offshore_dikson",
    data_types=["wind_field", "wave", "ocean_current"],
    target_horizon_hours=168,
    minimum_complete_horizon_hours=168,
    knowledge_as_of=knowledge_as_of,
)
```

返回：

```python
@dataclass(frozen=True, slots=True)
class PreparedWindow:
    route_id: str
    generation_id: int
    as_of_time: datetime
    frames: Mapping[str, tuple[StandardDataFrame, ...]]
    payload_attestations: Mapping[str, str]
    coverage: Mapping[str, CoverageReport]
    dataset_bundle: DatasetBundle
```

`payload_attestations` 必须与所有实际 frames 的 `data_id` 一一对应。每个值由公共
`semantic_payload_digest(record, payload)` 计算，绑定完整 `ManifestRecord.to_dict()` 与
规范化 payload；xarray 会绑定 dims、coords、data_vars、dtype、shape、值和 attrs，Mapping
会递归绑定规范值。A 在实时准备和跨进程精确恢复时都先形成消费者私有深快照，再为该快照
生成证明。B 必须在建立输入信封时独立重算，并在算法读取前再次复核/快照，不能只相信
record 中的 ready-file checksum 或 NumPy `writeable=False`。

`CoverageReport` 给出：

```text
data_type, requested_start, requested_end, minimum_required_end,
available_start, available_end, expected_interval_hours,
missing_intervals, source_snapshot_ids,
has_start_support, meets_minimum_horizon, covers_requested_window,
provenance_complete, complete
```

规则：

- `start_time` 当前必须等于模拟时钟；
- 整次调用固定同一个 `ClockSnapshot`。`PreparedWindow.as_of_time` 和
  `DatasetBundle.as_of_time` 为允许使用 revision 的 `knowledge_as_of`；因果模式中
  它等于模拟时钟，事后最佳估计模式可显式更晚；
- 开始后时钟普通推进或 seek 都会抛 `StaleGenerationError`，调用方重试；
- 时间数据保留起点之前/等于起点的最近一帧、窗内帧，以及请求末端之后/
  等于末端的最近一帧，便于 B 做边界插值；
- `has_start_support`：至少有一帧 `valid_time <= requested_start` 且一帧
  `valid_time >= requested_start`；
- `meets_minimum_horizon`：有起点支撑、末端达到
  `minimum_required_end`，且在该范围内无超限内部缺口；它只是“最低可用”
  状态，不等于完整请求窗；
- `covers_requested_window`：同样的条件延伸到 `requested_end`；
- `provenance_complete`：归档型 DataSource 已对每条选中记录实际验证 ready
  checksum，并验证原生 source snapshot 或不可变 raw publication 的磁盘证据；
  仅放一个 truthy `source_snapshot_id`、伪造 checksum 字符串或普通插件自报
  metadata 都不算完整，static/event 也执行同一规则；
- `complete = covers_requested_window and provenance_complete`；不得因
  `meets_minimum_horizon=true` 就把 `complete` 改写为 true；
- 内部间隔超过 `expected_interval_hours` 即记入 `missing_intervals`；不允许用 1.5 倍
  容差把 90 分钟帧冒充每小时连续窗；
- static/event 会检索历史有效项，不会因为 `valid_time < requested_start` 被误删；
- `complete=false` 是正式缺测，不得用零值或“安全环境”替代。

节奏解析优先级为：调用方显式 `expected_interval_hours` > 帧 metadata
`nominal_interval_hours` > `service.py` 旧数据兼容后备。冻结预报 GFS 声明
3 h、NCEI 历史分析声明 6 h、CARRA East-domain 再分析声明 3 h、Copernicus wave 声明 3 h，当前 Arctic current/water/ice 原生产品
声明 1 h。`current/water=6 h`、`海冰=24 h` 只是缺少 metadata 的旧帧
后备，不是当前原生产品 cadence。同一窗口出现多个冲突声明时 A 拒绝隐式
选择；B 若需更密帧，应在自己的时间处理层生成并记录模型版本。

CARRA 的分析时次是 `valid_time`；没有权威逐帧发布时间证据时，A 使用成功获取时刻作为
`CONSERVATIVE_RETRIEVAL` 的 `issue_time`，不得把历史分析时次冒充系统当时已知。独立
`acquire-carra` 只发布 A records/manifest，不创建或修改 Contracts 场景，也不获得
`frozen_forecast` 资格。

### 3.1 `DatasetBundle.v2`：一次 AB 输入的精确身份与覆盖证明

单个 `source_snapshot_id` 只识别一个源产品/模型周期及其裁剪选择；例如相同
GFS cycle+bbox+types 的不同长度采集会复用该 ID。它既不是精确执行 ID，也不能
单独识别“GFS + wave + current + ice”的组合输入。`PreparedWindow.dataset_bundle`
因此固结：

```text
schema_version="a.dataset-bundle.v2"
bundle_id, bundle_digest
corridor_id, as_of_time
requested_start, requested_end, minimum_required_end
requested_data_types, source_snapshot_ids
record_count
records[] = data_id, data_type, issue_time, valid_time,
            source, version, quality_flag, checksum, source_snapshot_id
coverage[] = 每类型 records/provenance digest、cadence、support、gaps、complete
```

`bundle_digest` 为上述查询边界和全部实际选中记录（包括边界支撑帧）的
确定性 SHA-256，`bundle_id` 取其前 24 个十六进制字符。bundle 拒绝跨
corridor、future issue、重复 data ID 和 `missing` payload。B 应把
`bundle_id + bundle_digest` 作为本次输入身份保留到模型运行/场景证据中，不要
仅保存一个上游 snapshot ID。结构真源是
`schemas/dataset-bundle-v2.schema.json`。v2 的 `complete` 不是可信声明：共享
`arctic_route_contracts` 会从 records 独立重算逐类型 coverage，核对正式 cadence，
并要求所有 requested type 都满足完整窗和 provenance。v1 schema 仅用于读取历史
bundle；正式 RunContext 必须拒绝 v1。
跨进程消费必须调用 `DatasetBundle.from_dict()` 校验 record count、规范排序、
来源 ID 集合和 digest；JSON Schema 只能检查形状，不能独自证明内容身份。

### 3.2 持久 bundle 的正式精确恢复

B 跨进程或进程重启后不得扫描 A 的 SQLite、ready、raw 或 source snapshots。编排层先
以 `DatasetBundle.requested_start` 重建 A 的模拟时钟，并把运行态代次/知识截止写入 B
输入信封，然后调用公共入口：

```python
restored = a.resolve_dataset_bundle_for_b(
    persisted_bundle_mapping,
    generation_id=b_input.generation_id,
    knowledge_as_of=b_input.knowledge_as_of,
)
```

入口返回与实时路径相同形状的 `PreparedWindow`，并执行以下 fail-closed 检查：

1. 语义解析后必须是所有请求层 `complete=true` 的 `a.dataset-bundle.v2`；v1 仍可用
   `DatasetBundle.from_dict()` 做历史审计，但不能正式恢复；
2. 调用方 `knowledge_as_of` 必须精确等于 bundle `as_of_time`，且每条
   `issue_time <= knowledge_as_of`；
3. 调用方 `generation_id` 必须是非负整数并等于 A 当前 generation，A 当前
   simulation time 必须等于 bundle `requested_start`；
4. A 通过归档源公共能力按 `data_id` 解析精确不可变 revision，不按当前最新版本替换；
5. manifest 身份与 bundle record 一一对应，payload checksum、raw/source snapshot 和
   provenance ID 在加载前后重新验证；
6. 从实际记录、正式 cadence 和验证后的 provenance 重建 bundle，结果必须与持久
   bundle 完全相等；对交付深快照生成逐 data ID payload attestation；加载期间时钟或
   generation 变化则整批拒绝并重试。

内置 `LocalArchiveSource` 实现上述能力。普通第三方 `DataSource` 若没有
`get_record_by_id()` 与 `load_verified_frame()`，仍可用于诊断/实时准备，但不得冒充
正式跨进程恢复源。

这里的逐类型 `coverage[*].complete` 及汇总 `coverage_complete` 只证明调用方所请求类型
的窗口完整；正式
`RunContext.v2` 还会按共享场景画像复核类型集合。当前画像要求 12 类运行层完整：
风、温度、能见度、波浪、海流、水位、五类海冰层以及 `land_sea_mask`；
`bathymetry` 和 `long_term_restricted_area` 是可选研究/信息层。两类可选层仍是 A
已经实现的正式接口；本轮 A–B–C 主线 bundle 固定为恰好 12 类，两类可选层独立
采集和报告，失败不得阻塞基线。“可选”既不等于未实现，也不允许把水深或限制区
自动升级为 `hard_mask`。

事后场景不得复制文档中的固定知识截止时间。先以采集完成时刻做一次诊断准备，取所选
12 类记录的最大 `issue_time` 写入 `KNOWLEDGE_AS_OF`，再正式回放，并要求两次选中的
data IDs 一致。CLI 可原子持久该 JSON：

```bash
.mamba-env/bin/uv run arctic-data replay \
  --data-root data \
  --route-id offshore_murmansk_to_offshore_dikson \
  --at 2026-07-15T00:00:00Z \
  --mode retrospective_best_estimate \
  --knowledge-as-of "$KNOWLEDGE_AS_OF" \
  --types wind_field wave ocean_current \
  --horizon-hours 168 \
  --minimum-horizon-hours 168 \
  --bundle-output data/output/bundles/murmansk-july.json \
  --summary-only
```

CLI stdout 同时包含 coverage、selected data IDs、bundle 和 health，可用于人工/
AI 联调审核；`--summary-only` 将 IDs 改为计数并省略 stdout 中的 records，完整
records 仍原子写入文件。任一必需层不完整时默认返回非零且不持久化 bundle；
`--allow-incomplete` 只改变诊断命令退出码，不允许持久化不完整 bundle。

## 4. 其他读取入口

```python
# 仅做缓存预取；不生成 CoverageReport
a.prefetch(
    route_id=corridor_id,
    data_types=["wind_field"],
    horizon_hours=requested_horizon_hours,
)

# 模拟时刻及以前的最新有效帧，不会返回最远未来预报
current = a.latest_for_b(corridor_id, "wind_field")

# 显式取得缓存中最远未来预报
furthest = a.latest_forecast_for_b(corridor_id, "wind_field")

# 读取时间窗；参数顺序是 route_id, data_type
frames = a.window_for_b(
    corridor_id,
    "wind_field",
    hours_before=48,
    hours_after=requested_horizon_hours,
)

# 直接查询来源前后支撑帧
lower, upper = a.source.get_bracketing(
    "wind_field",
    target_time,
    route_id=corridor_id,
    as_of=knowledge_as_of,
)
```

`latest_for_b` 和 `latest_forecast_for_b` 不能混用。前者回答“现在/过去最后一帧”，后者才回答“档案中最远预报”。
公共 `prefetch()` 始终使用当前模拟时刻作为因果知识截止；需要显式
`knowledge_as_of` 的事后最佳估计只能调用 `prepare_window_for_b()`，不能调用私有
`_prefetch_at_snapshot()`。

## 5. revision 与缓存

分区键是：

```text
(route_id, data_type, variable)
```

逻辑帧键是：

```text
(route_id, data_type, valid_time)
```

同一逻辑帧多个 revision 按以下顺序选活动版本：

1. `quality_flag`：good > suspect > degraded > missing；
2. 较新的 `issue_time`；
3. 较新的 `ingest_time`；
4. `version`、`data_id` 确定性打破平局。

因此“新到达”不必然覆盖质量更高的旧版本。所有历史记录和内容寻址文件仍保留在 manifest/ready，缓存只选择当前活动 revision。

默认分区容量：static 1、slow 256、dynamic 256；event 按
`metadata.end_time` 和模拟时刻清理。全局内存默认 512 MiB。

```python
with cache.lease(frame.record.data_id) as leased:
    calculate(leased.payload)
```

租用保证该具体对象在计算期间不被物理删除；若新 revision 到达或分区裁剪，它仍可退出“活动版本”，租用结束后才释放。不要把 lease 当成长期版本锁。

## 6. seek/generation 语义

`clock.seek()`：

1. 严格递增 `generation_id`；
2. 清除 dynamic/slow/event；
3. 只把 `issue_time <= 新 simulation_time` 的 static 改挂新代次；
4. 没有提供新模拟时刻的直接 `reset_generation()` 会安全清空 static；
5. 旧代次加载在 `cache.put()` 时被拒绝。

B 开始计算时从 `RunContext.v2` 冻结 `(run_id, scenario_id, corridor_id,
vessel_profile_id, dataset_bundle_id, dataset_bundle_digest, config_digest)` 和场景起止时间；
`generation_id`、当前 simulation clock snapshot、`knowledge_as_of` 以及完整 DatasetBundle
文档不在 RunContext 中，必须由编排/B 输入信封另外显式冻结，并与 RunContext 中的
bundle ID/digest 交叉校验。发布 RiskFrame 前再次核对全部公共和运行态围栏。普通 tick
不增加 generation，所以长计算还应比较冻结时刻或由编排器取消/重试。

## 7. B 的空间/时间处理责任

| 数据 | A 的输出 | B 的默认处理 |
|---|---|---|
| 连续标量 | 原地理网格 + grid identity | 对共享目标网格做明确连续插值；记录方法和覆盖 |
| 分类/掩膜 | 分类值/布尔语义 | nearest/保守规则，不做普通双线性 |
| 风、流、冰漂 | 真东/真北两个分量 | 分量分别空间/时间插值；不得再次旋转 |
| 波向 | from true north clockwise | `sin/cos` 圆周插值，禁止直接对 359°/1° 求算术平均 |
| 保护区 | 有效 GeoJSON + 法律/来源摘要 | 场景政策决定 hard/soft/information；不能按图层名自动 hard-mask |
| 缺测 | `quality_flag`、QC、缺口 | 降低 confidence 或明确拒绝；禁止补零当安全 |

当前 C 的 `RiskFrame v2` 只接受 EPSG:4326、严格递增的一维
`latitude/longitude` 二维网格。A 可能提供 rectilinear、curvilinear 或
unstructured point 数据；正式 B 必须把兼容输入对齐到共享场景网格，并为越界/覆盖不足 fail closed。

## 8. A→B 来源摘要映射

B 的每个正式 `SourceReference` 至少这样映射：

```text
source_id   <- record.source
data_id     <- record.data_id
issue_time  <- record.issue_time
valid_time  <- record.valid_time
version     <- record.version
quality_flag<- record.quality_flag.value
checksum    <- record.checksum
```

一个风险帧使用了多个变量、时刻或 revision，就全部列出并确定性去重。不能只写“综合风险文件”。A 的 `quality_flag` 是来源证据与内容 QC 的下界，不等同于 B 的 `confidence`。

## 9. `DataSource` 插件不是信任边界

A 会在第三方/未来来源进入 AB 缓存前二次校验：

1. `list_available()` 每一项必须是 `ManifestRecord`，且 route/type 精确
   匹配当前请求、`issue_time <=` 固定 `knowledge_as_of`；
2. `load_frame()` 必须返回 `StandardDataFrame`，其 record 与被请求记录逐字段
   相等、generation 与固定时钟快照相等；Dataset 必须具有 manifest 声明变量、
   一致的 route/type/issue/valid attrs 和非空有效内容，GeoJSON 必须是
   FeatureCollection。

任一插件返回其他走廊/类型、未来发布记录、偷换 record 或旧 generation，
都会被拒绝，不能依赖插件自觉过滤。

第三方插件可以继续用于预取和诊断，但仅实现上述读取方法不会获得正式
`provenance_complete`。若它确实由不可变归档支撑，还必须提供可选能力：

```python
verified_provenance_id(record: ManifestRecord) -> str | None
```

该方法必须读取并验证真实归档证据后返回与记录声明一致的 ID；不能只回显
metadata。内置 `LocalArchiveSource` 会复核 ready checksum，并进一步复核精确
source snapshot 或 raw payload/sidecar。缺少该能力、抛错、返回不一致时统一按
未验证处理，窗口仍可诊断但 `provenance_complete=false`。

## 10. 归档 doctor 的证据边界

`arctic-data doctor` 不只查 ready/manifest checksum。对声明新归档绑定的记录，
它还检查：

- raw 目录恰有一对 payload/sidecar，文件大小和 SHA-256 相符；
- sidecar 的 publication ID、route/type、source/version、issue/valid time、
  quality 上限与 manifest 一致；
- `source_file_checksum` 已声明时，相应 `source_snapshot_id/source_file`
  存在且 checksum 相符；新记录的 `source_snapshot_relative_path` 还必须精确定位
  到归档根内的同一文件。

旧记录未声明 raw/snapshot 绑定时保持可读；一旦声明了证据字段，缺失或
篡改就是 doctor error。

## 11. 验收清单

- 两个 `route_id` 同时预取不串帧；
- 同时次 revision 选择符合上述规则；
- 所有帧 `issue_time <= knowledge_as_of`；
- 每个必需层分别检查 `meets_minimum_horizon`、
  `covers_requested_window`、`provenance_complete` 和 `complete`；不混淆最低线与目标窗；
- 时间帧 `source_snapshot_ids` 非空且与输入档案一致；
- 因果模式下 `PreparedWindow.as_of_time` 与冻结模拟时钟一致；事后模式下它与显式
  `knowledge_as_of` 一致；`DatasetBundle` 通过 Schema、A 语义校验和共享包独立重算，
  其 records 与实际选中帧一一对应，所有请求类型的 coverage/provenance 均完整；
- 跨进程/重启恢复不扫描 A 私有存储，按 data ID 取得精确 revision，并重验 payload、
  provenance、bundle、generation 和 simulation/knowledge 时间围栏；A/B 对每个实际
  payload 的语义证明一致，恢复后篡改会被拒绝；
- seek/tick 竞态不会返回混合时钟窗口；
- 来源插件无法注入未来、错 route/type、错 record/generation 或错 payload 类型；
- B 不原地修改 A payload；
- 波向、矢量、分类层使用正确插值；
- `source_valid_mask` 仅用于数据完整度，不直接用于导航硬掩膜；
- 正式 RiskFrame 保留完整来源摘要、上下文和 `environment_speed_factor`。


## Vessel Traffic Handoff

`vessel_traffic` is available as a dynamic Work Package A data type for the latest 144-hour window. It is generated at a 3-hour cadence and carries the variables `traffic_density`, `traffic_count`, `traffic_risk`, and `traffic_confidence`. This layer exists because complete historical AIS route traffic is difficult to obtain under ordinary access permissions. The generated traffic condition simulates real-time corridor traffic pressure for Work Package B risk modelling and keeps the same route identifiers and standard data-frame contract as other A-package inputs.
