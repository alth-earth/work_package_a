# `issue_time` 取得与使用规则

## 1. 定义

`issue_time` 是“A 从哪个 UTC 时刻起允许把该帧交给模拟系统”的门禁时间，不是
数据描述时刻，也不一定等于模型生产者的精确发布时间。

```text
源产品生成/同步/取得 ── issue_time 门禁 ── ingest_time
                               │
                               └── simulation_time >= issue_time 才可见

数据描述的环境时刻 ─────────────────────── valid_time
```

优先使用可证明的生产者发布时间；没有时只能使用“不早于真实可用时刻”的保守上界。
保守值能防止偷看未来，但不能用来统计精确发布延迟。

## 2. 当前证据方法

| `method` | 何时使用 | `authoritative` |
|---|---|---:|
| `explicit_catalog` | 已保存、能精确绑定产品/周期/帧的发布记录，或操作者显式提供的可审计记录 | 可为 true |
| `http_last_modified` | 2xx 源文件响应能通过 URL/参数精确绑定目标 valid time | 可为 true；不得绑定到别的缓存文件/周期 |
| `dataset_attribute` | 文件内明确的 publication/creation 字段，且与获取时刻和 valid time 合理 | 可为 true |
| `copernicus_service_sync` | Copernicus ARCO 服务副本的同步/更新字段 | 固定 false |
| `conservative_retrieval` | 成功完整取得 payload 的时刻 | 固定 false |

任何证据都保存：

```text
issue_time, method, authority, reference,
observed_at, raw_value, authoritative
```

sidecar 顶层 `issue_time` 必须与证据内部完全一致。

## 3. 两个容易误用的来源

### 3.1 Copernicus ARCO

`arco_updated_date` 表示 ARCO 服务中数据副本的更新时间，不代表原始生产者何时发布
该模型周期。Copernicus Toolbox 文档也明确说明该字段不是生产者最后更新时间：

<https://toolbox-docs.marine.copernicus.eu/en/v2.2.1/usage/describe-usage.html>

所以 A 只把它作为 `copernicus_service_sync` 的保守可用门禁，固定
`authoritative=false`。Native Copernicus 采集目前更保守地使用成功取得时刻。

### 3.2 NOMADS filter

NOMADS filter 可能按请求动态生成区域子集，其 HTTP `Last-Modified` 不自动等于 GFS
原始模型发布时间。Native GFS 会保存该头用于审计，但以完整 GRIB 成功取得时刻作为
`conservative_retrieval` 门禁，因此本轮真实 GFS 帧是 `suspect`。

若未来接入能精确保存 GFS 周期正式发布记录的目录/API，可以改用
`explicit_catalog`，但必须先增加源合同测试，不能仅改标签。

## 4. HTTP 与旧下载器绑定规则

旧函数一次调用可能同时返回缓存帧和新下载帧。A 不把一次调用中的任意 HTTP 证据
全局套到所有 payload：

- 只接受 2xx 响应；
- 请求 URL/参数必须含有能绑定该 valid time 的日期/周期/lead；
- GFS 必须能从 cycle + `fNNN` 推回该帧 valid time；
- `.nc/.grib/.grib2` 后缀只是加分证据，不足以单独匹配；
- 无法一一绑定就拒绝该候选。

这样可避免“新下载响应的 Last-Modified 被错误绑定到旧缓存帧”。

## 5. 合理性检查

候选时间必须：

- 不晚于 `observed_at + 10 min`；
- 可解析为 UTC；
- 非 static 数据通常位于该批 valid-time 范围前后 45 天内；
- static 不受 45 天限制，但仍需来源证据。

因此，交付样本中 `valid_time=2026` 却带 `bulletin_date=2024` 的冰漂属性不会
覆盖坐标时间，也不会成为自动 issue time。本地 mtime、文件名或今天重新查询的目录
状态永远不能补造过去的 issue time。

## 6. “严格模式”准确含义

旧下载器默认不启用“成功获取时刻”最终兜底；所有已有 resolver 都失败时拒绝。
这不等于“只接受 authoritative”：Copernicus service-sync 虽非权威，仍是可审计的
安全门禁，可以被接受，但最终质量不能为 `good`。

显式 `--allow-conservative-retrieval` 才允许成功获取时刻兜底。Native GFS/Copernicus
本身就是保守采集入口，会主动使用 retrieval gate。

## 7. 证据质量与内容质量分离

A 同时评估：

1. availability evidence：发布时间/可用门禁证据；
2. content QC：单位、物理范围、有限值、缺测比例、坐标、几何等。

最终 `quality_flag` 取二者更差者。例如：

- 内容值完全正常，但 issue time 仅为保守获取时刻 → `suspect`；
- 发布时间权威，但内容缺测较多 → `suspect` 或 `degraded`；
- 未知单位、全缺测、越界值或非法 GeoJSON → 直接拒绝。

所以 `suspect` 不等于“数据值已知错误”，B 应结合 `content_qc` 和
`issue_time_evidence` 解释并计算自己的 `confidence`。

## 8. 合法 sidecar 片段

```json
{
  "issue_time": "2026-08-11T11:16:10Z",
  "valid_time": "2026-08-11T12:00:00Z",
  "quality_flag": "suspect",
  "metadata": {
    "issue_time_evidence": {
      "issue_time": "2026-08-11T11:16:10Z",
      "method": "conservative_retrieval",
      "authority": "NOAA GFS/NOMADS",
      "reference": "saved request URL",
      "observed_at": "2026-08-11T11:16:10Z",
      "raw_value": "2026-08-11T11:16:10Z",
      "authoritative": false
    },
    "forecast_reference_time": "2026-08-11T06:00:00Z",
    "forecast_lead_hours": 6
  }
}
```

完整 sidecar 还必须含 payload SHA-256、大小和 `publication_id`；见 JSON Schema。

## 9. 历史回放保存要求

必须长期保存当时的 raw payload、sidecar、source snapshot、请求 URL/参数和证据原始
值。今天的 catalogue 状态只能证明今天看到什么，不能倒推过去何时已经可见。
