# A → B（AB）接口 v0.3.1

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
assert frame.record.issue_time <= as_of_time
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

元数据会深度冻结。xarray 容器按消费者隔离，底层数组只读；B 不得原地修改
payload，应创建自己的派生 Dataset。

`content_qc.source_valid_mask` 若 `present=true`，只表示原生 Copernicus
完整请求中派生的源数据空间有效域。`structural_mask_fraction` 是该域之外的
结构无效占比，`valid_domain_missing_fraction` 才是域内残余缺测。这个 mask
明确没有 navigation/classification 语义，B 不得将它直接转为陆海掩膜或
`hard_mask`。旧帧没有显式证据时，A 不推断这一掩膜。
普通 direct/sidecar producer 不能凭自报 attrs 获得该语义；A 要求 mask 与归档
Copernicus source snapshot 的精确路径、SHA-256、dataset ID、请求起止时刻绑定，
并对快照内 mask 做逐值、坐标和语义一致性检查。

## 3. 首选入口：一次性准备一致窗口

```python
prepared = a.prepare_window_for_b(
    route_id="tromso_to_svalbard",
    data_types=["wind_field", "wave", "ocean_current"],
    target_horizon_hours=156,
    minimum_complete_horizon_hours=132,
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
    coverage: Mapping[str, CoverageReport]
    dataset_bundle: DatasetBundle
```

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
- 整次调用固定同一个 `ClockSnapshot/as_of_time`，并把它原样返回为
  `PreparedWindow.as_of_time`；
- 开始后时钟普通推进或 seek 都会抛 `StaleGenerationError`，调用方重试；
- 时间数据保留起点之前/等于起点的最近一帧、窗内帧，以及请求末端之后/
  等于末端的最近一帧，便于 B 做边界插值；
- `has_start_support`：至少有一帧 `valid_time <= requested_start` 且一帧
  `valid_time >= requested_start`；
- `meets_minimum_horizon`：有起点支撑、末端达到
  `minimum_required_end`，且在该范围内无超限内部缺口；它是“最低可用”
  状态，不等于完整请求窗；
- `covers_requested_window`：同样的条件延伸到 `requested_end`；
- `provenance_complete`：归档型 DataSource 已对每条选中记录实际验证 ready
  checksum，并验证原生 source snapshot 或不可变 raw publication 的磁盘证据；
  仅放一个 truthy `source_snapshot_id`、伪造 checksum 字符串或普通插件自报
  metadata 都不算完整，static/event 也执行同一规则；
- `complete = covers_requested_window and provenance_complete`；不得因
  `meets_minimum_horizon=true` 就把 `complete` 改写为 true；
- 内部间隔超过 `1.5 * expected_interval_hours` 才记入 `missing_intervals`；
- static/event 会检索历史有效项，不会因为 `valid_time < requested_start` 被误删；
- `complete=false` 是正式缺测，不得用零值或“安全环境”替代。

节奏解析优先级为：调用方显式 `expected_interval_hours` > 帧 metadata
`nominal_interval_hours` > `service.py` 旧数据兼容后备。原生 GFS 声明
3 h、Copernicus wave 声明 3 h，当前 Arctic current/water/ice 原生产品
声明 1 h。`current/water=6 h`、`海冰=24 h` 只是缺少 metadata 的旧帧
后备，不是当前原生产品 cadence。同一窗口出现多个冲突声明时 A 拒绝隐式
选择；B 若需更密帧，应在自己的时间处理层生成并记录模型版本。

### 3.1 `DatasetBundle.v1`：一次 AB 输入的精确身份

单个 `source_snapshot_id` 只识别一个源产品/模型周期及其裁剪选择；例如相同
GFS cycle+bbox+types 的不同长度采集会复用该 ID。它既不是精确执行 ID，也不能
单独识别“GFS + wave + current + ice”的组合输入。`PreparedWindow.dataset_bundle`
因此固结：

```text
schema_version="a.dataset-bundle.v1"
bundle_id, bundle_digest
corridor_id, as_of_time
requested_start, requested_end, minimum_required_end
requested_data_types, source_snapshot_ids
record_count
records[] = data_id, data_type, issue_time, valid_time,
            source, version, quality_flag, checksum, source_snapshot_id
```

`bundle_digest` 为上述查询边界和全部实际选中记录（包括边界支撑帧）的
确定性 SHA-256，`bundle_id` 取其前 24 个十六进制字符。bundle 拒绝跨
corridor、future issue、重复 data ID 和 `missing` payload。B 应把
`bundle_id + bundle_digest` 作为本次输入身份保留到模型运行/场景证据中，不要
仅保存一个上游 snapshot ID。结构真源是
`schemas/dataset-bundle-v1.schema.json`。
跨进程消费必须调用 `DatasetBundle.from_dict()` 校验 record count、规范排序、
来源 ID 集合和 digest；JSON Schema 只能检查形状，不能独自证明内容身份。

CLI 可原子持久该 JSON：

```bash
.mamba-env/bin/uv run arctic-data replay \
  --data-root data \
  --route-id tromso_to_svalbard \
  --at 2026-08-11T16:00:00Z \
  --types wind_field wave ocean_current \
  --horizon-hours 156 \
  --minimum-horizon-hours 132 \
  --bundle-output data/output/bundles/tromso-example.json \
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
    horizon_hours=156,
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
    hours_after=156,
)

# 直接查询来源前后支撑帧
lower, upper = a.source.get_bracketing(
    "wind_field",
    target_time,
    route_id=corridor_id,
    as_of=as_of_time,
)
```

`latest_for_b` 和 `latest_forecast_for_b` 不能混用。前者回答“现在/过去最后一帧”，后者才回答“档案中最远预报”。

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

B 开始计算时冻结 `(scenario_id, corridor_id, generation_id, as_of_time,
config_digest)`；发布 RiskFrame 前再次核对。普通 tick 不增加 generation，所以长计算还应比较冻结的 `as_of_time` 或由编排器取消/重试。

## 7. B 的空间/时间处理责任

| 数据 | A 的输出 | B 的默认处理 |
|---|---|---|
| 连续标量 | 原地理网格 + grid identity | 对共享目标网格做明确连续插值；记录方法和覆盖 |
| 分类/掩膜 | 分类值/布尔语义 | nearest/保守规则，不做普通双线性 |
| 风、流、冰漂 | 真东/真北两个分量 | 分量分别空间/时间插值；不得再次旋转 |
| 波向 | from true north clockwise | `sin/cos` 圆周插值，禁止直接对 359°/1° 求算术平均 |
| 保护区 | 有效 GeoJSON + 法律/来源摘要 | 场景政策决定 hard/soft/information；不能按图层名自动 hard-mask |
| 缺测 | `quality_flag`、QC、缺口 | 降低 confidence 或明确拒绝；禁止补零当安全 |

当前 C 的 `RiskFrame v1` 只接受 EPSG:4326、严格递增的一维
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
   匹配当前请求、`issue_time <=` 固定 `as_of_time`；
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
- 所有帧 `issue_time <= as_of_time`；
- 每个必需层分别检查 `meets_minimum_horizon`、
  `covers_requested_window`、`provenance_complete` 和 `complete`；不混淆最低线与目标窗；
- 时间帧 `source_snapshot_ids` 非空且与输入档案一致；
- `PreparedWindow.as_of_time` 与冻结时钟一致，`DatasetBundle` 通过 Schema，
  其 records 与实际选中帧一一对应；
- seek/tick 竞态不会返回混合时钟窗口；
- 来源插件无法注入未来、错 route/type、错 record/generation 或错 payload 类型；
- B 不原地修改 A payload；
- 波向、矢量、分类层使用正确插值；
- `source_valid_mask` 仅用于数据完整度，不直接用于导航硬掩膜；
- 正式 RiskFrame 保留完整来源摘要、上下文和 `environment_speed_factor`。
