# `issue_time` 取得与使用规则

## 一句话定义

`issue_time` 不是“数据描述的时刻”，而是“A 从哪个时刻起允许把这份数据交给模拟系统”。理想情况下它是精确发布时间；目录只给更新时间时，就使用不早于真实可用时刻的保守门禁值。因此它首先服务于防止历史回放偷看未来资料，而不是用来估计源站的精确发布延迟。

```text
源站产生/更新产品 ── issue_time ── A 下载 ── ingest_time
                         │
                         └── 只有 simulation_time >= issue_time 才能交给 B

数据描述的环境时刻 ───────────────────────── valid_time
```

## 取值顺序

`SourceIssueTimeResolver` 按数据源和证据强弱解析：

HTTP 证据只接受 2xx 成功响应；若旧下载器请求失败后退回缓存，错误页的时间头不会污染缓存数据的 `issue_time`。

| 数据源 | 首选证据 | 后续证据 |
|---|---|---|
| Copernicus Marine | 按实际 `dataset_id` 调用官方 `copernicusmarine.describe()`，读取 catalogue 更新时间 | 下载响应 `Last-Modified`、可信 NetCDF 全局属性 |
| OSI SAF / MET Norway THREDDS | 被下载文件的权威 HTTP `Last-Modified` | catalogue 响应、可信 NetCDF 全局属性 |
| NOAA GFS / NOMADS | 筛选下载响应的权威 HTTP `Last-Modified` | 可信 NetCDF/GRIB 转换后属性 |
| GEBCO / EMODnet | 权威下载/WFS 响应 `Last-Modified` | 可信数据属性；静态数据不受与 `valid_time` 的 45 天邻近约束 |

Copernicus Marine Toolbox 的 `describe` 返回官方 catalogue 元数据，包括 dataset/product 和更新日期字段；实现保留具体字段路径和原始值作为证据，并且不会把“开始更新”时间当成“已完成发布”时间。参见 [Copernicus Marine CLI catalogue 文档](https://help.marine.copernicus.eu/en/articles/7970154-copernicus-marine-toolbox-cli-explore-the-catalogue-and-metadata) 与 [API catalogue 文档](https://help.marine.copernicus.eu/en/articles/8286798-copernicus-marine-toolbox-api-explore-the-catalogue-and-metadata)。THREDDS 的 catalogue 结构遵循其官方 XML catalogue 规范，参见 [THREDDS Data Server catalogue 文档](https://docs.unidata.ucar.edu/tds/current/adminguide/catalog.html)。

## 防止“看起来像时间”的错误值

动态数据的候选时间必须同时满足：

- 不晚于本次观测/下载时刻 10 分钟以上；
- 距该文件最早和最晚 `valid_time` 不超过 45 天；
- 能解析成带 UTC 语义的日期时间。

因此，资料样本中与 2026 有效时次不符的 2024 年 `bulletin_date` 会被拒绝。本地文件修改时间、仅靠文件名推断出的日期和未保存依据的人工猜测永远不参与自动解析。

## 严格模式与保守模式

默认严格模式取不到上述权威证据就报 `A102`，本批数据不进入 `ready`。

`--allow-conservative-retrieval` 允许把“成功下载完成时刻”作为一个安全上界：数据可能更早已经发布，但一定不会晚于我们成功取得它的时刻。该证据的 `authoritative=false`，帧的 `quality_flag=suspect`。这能保证不偷看未来，但不适合声称精确发布延迟。

## sidecar 保存什么

每个逐时次 payload 都有自己的 sidecar：

```json
{
  "issue_time": "2026-07-15T06:30:00Z",
  "valid_time": "2026-07-15T12:00:00Z",
  "quality_flag": "good",
  "metadata": {
    "issue_time_evidence": {
      "method": "copernicus_catalogue",
      "authority": "Copernicus Marine Data Store",
      "reference": "copernicusmarine.describe(dataset_id='...') field=...",
      "observed_at": "2026-07-15T07:00:00Z",
      "raw_value": "2026-07-15T06:30:00Z",
      "authoritative": true
    },
    "source_time_index": {"time": 2},
    "forecast_reference_time": "2026-07-15T00:00:00Z",
    "forecast_lead_hours": 12.0
  }
}
```

历史回放必须保留当时生成的 raw sidecar。今天查询到的目录状态只能证明今天看到的状态，不能倒推出过去某一时刻的目录内容。
