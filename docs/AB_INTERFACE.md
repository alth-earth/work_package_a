# A → B（AB）接口

本文描述已经实现的 A→B 接口。B→C 的 `RiskFrame`、C→D 的 `RoutePlan` 及后续 AI 开发清单见 [BCD_HANDOFF.md](BCD_HANDOFF.md)。

## 稳定对象

`StandardDataFrame` 包含：

- `record: ManifestRecord`：不可变元数据；
- `payload`：已加载的 `xarray.Dataset` 或 GeoJSON 字典，发布后只读；
- `generation_id`：模拟时钟跳转代次。

`ManifestRecord` 的必需字段为：

```text
data_id, data_type, category, route_id, variables,
issue_time, valid_time, ingest_time,
bbox, crs, resolution,
source, quality_flag, version, checksum,
relative_path, size_bytes, media_type, metadata
```

全部时间进入接口前转为 UTC。时间语义分别为：

- `issue_time`：源产品对模拟系统可用的发布时间，是防未来信息泄漏的过滤键；
- `valid_time`：环境场描述的时刻，是 B 插值、保持或外推的时间轴；
- `ingest_time`：本系统接收时刻，只用于追溯延迟，不替代前两者。

旧下载器返回的多时次文件在进入 AB 前已经拆开，所以 B 不需要理解源文件里复杂的维度组合。每个 `StandardDataFrame` 只有一个 `record.valid_time`；原预报起报时间和步长保存在 `record.metadata.forecast_reference_time/forecast_lead_hours`。

```text
issue_time ──“这份资料何时可被系统知道”──► A/C 防未来信息泄漏
valid_time ──“这份资料描述哪个环境时刻”──► B 插值、预测、风险融合
ingest_time ─“A 实际何时收到”──────────► 运维延迟与审计
```

sidecar 的 `metadata.issue_time_evidence` 会原样进入 `ManifestRecord.metadata`，包括方法、权威机构、引用位置、原始值和是否权威。B 通常只按 `issue_time` 过滤；若证据为非权威保守降级，A 已把 `quality_flag` 标成 `suspect`，B 可进一步降低自己的 `confidence`。

## B 的读取方式

```python
frames = work_package_a.window_for_b(
    "wind_field",
    hours_before=48,
    hours_after=24,
)

for frame in frames:
    assert frame.record.issue_time <= clock.now
    valid_time = frame.record.valid_time
    dataset = frame.payload
```

B 若需要保证消费期间不被回收，应按 `data_id` 租用：

```python
with cache.lease(frame.record.data_id) as leased:
    calculate(leased.payload)
```

引用计数归零后，该帧才可因分区或全局内存上限被回收。

## 缓存规则

| 类型 | 默认行为 |
|---|---|
| static | 每类保留最新一份，跳转后继续复用但改挂新代次 |
| slow | 每个类型至少保留最近两帧 |
| dynamic | 默认每类型最多 64 帧，足够最近真实帧和已发布预报窗 |
| event | 保留未过期事件；`metadata.end_time` 过期后回收 |

内存按 payload 的 `nbytes` 估算，并只计一次；同一帧虽然进入多个变量分区，不重复计算内存。

## 跳转语义

`clock.seek()` 会：

1. 提升 `generation_id`；
2. 清空动态、缓变和事件帧；
3. 保留静态帧并重新挂载到新代次；
4. 使仍在加载的旧任务在 `cache.put()` 时得到 `A302 StaleGenerationError`；
5. 由 A 围绕新时刻重新预取。

因此 B 应在开始任务时保存代次，并在发布 BC 风险帧时继续携带该代次。C/D 可用相同机制丢弃跳转前的迟到结果。

C 不应再次解析 NetCDF 的源时间，也不应以自己的当前时间覆盖 `issue_time`。C 只需要：选择 `issue_time <= simulation_time` 的 B 输出，沿用 `valid_time` 作为规划时间轴，并检查 `generation_id` 防止跳转前结果混入。

## 缺测与质量

A 找不到已发布数据时发布 `MissingDataAlert`，不会创建全零数组。B 应根据 `quality_flag`、数据龄期、缺帧状态和预测时长生成自己的 `confidence`；不能把 A 的 `quality_flag` 直接等同于 B 的预测置信度。
