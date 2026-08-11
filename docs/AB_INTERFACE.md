# A → B（AB）接口 v0.3

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
- `forecast_reference_time/forecast_lead_hours`（预报帧）；
- `normalization.grid_id/coordinate_digest/grid_topology`；
- `normalization.variables` 中的源变量、源单位、规范单位和矢量参考系；
- `content_qc`；
- 上游 checksum、产品/数据集 ID 和裁剪请求摘要（存在时）。

元数据会深度冻结。xarray 容器按消费者隔离，底层数组只读；B 不得原地修改
payload，应创建自己的派生 Dataset。

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
    frames: Mapping[str, tuple[StandardDataFrame, ...]]
    coverage: Mapping[str, CoverageReport]
```

`CoverageReport` 给出：

```text
data_type, requested_start, requested_end, minimum_required_end,
available_start, available_end, expected_interval_hours,
missing_intervals, source_snapshot_ids, complete
```

规则：

- `start_time` 当前必须等于模拟时钟；
- 整次调用固定同一个 `ClockSnapshot/as_of_time`；
- 开始后时钟普通推进或 seek 都会抛 `StaleGenerationError`，调用方重试；
- 动态/缓变数据要有起点下支撑、达到 132 h 最低末端且无超限内部缺口；
- static/event 会检索历史有效项，不会因为 `valid_time < requested_start` 被误删；
- `complete=false` 是正式缺测，不得用零值或“安全环境”替代。

默认期望间隔在 `service.py` 中声明：GFS 类 3 h、wave 3 h、current/water
level 6 h、海冰类 24 h。B 的模型若需要更密集的小时帧，应在自己的时间处理层生成并记录模型版本。

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

## 9. 验收清单

- 两个 `route_id` 同时预取不串帧；
- 同时次 revision 选择符合上述规则；
- 所有帧 `issue_time <= as_of_time`；
- `CoverageReport.complete` 对每个必需层为 true；
- `source_snapshot_ids` 非空且与输入档案一致；
- seek/tick 竞态不会返回混合时钟窗口；
- B 不原地修改 A payload；
- 波向、矢量、分类层使用正确插值；
- 正式 RiskFrame 保留完整来源摘要、上下文和 `environment_speed_factor`。
