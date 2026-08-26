# 航道通行情况模拟数据接入说明

## 1. 定位

`vessel_traffic` 是工作包 A 新增的可选动态数据层，用于把航道通行情况模拟模型生成的
NetCDF 数据统一纳入 A 的 `ready/manifest` 数据体系。该层不替代原有 12 类必需环境数据，
也不改变 A 已有的采集、回放和 Bundle 生成逻辑。

## 2. 增加该数据层的必要性

两条研究航线位于高纬和北极航运区域，连续、开放、可直接下载的历史船舶通航记录较难获得。
部分 AIS 历史轨迹接口需要额外授权，公开统计报告多以年度、航次或区域摘要形式发布，难以直接
形成逐时、逐格网的训练输入。

因此，项目在保持真实环境数据来源可追溯的基础上，引入航道通行情况模拟模型，将有限的实时
AIS、公开统计信息和航线空间约束转化为与实时通航状态相近的网格化风险变量。该变量主要用于
向工作包 B 提供“船舶活动/拥挤/邻近船舶影响”的动态风险输入，使综合风险模型能够预留并验证
对实时通航情况的接收能力。

## 3. 数据来源与质量标记

- 输入来源：工作包 B 侧航道通行情况模拟模型输出的 NetCDF 文件；
- 主变量：`vessel_traffic_risk`；
- 变量含义：0 表示通航干扰风险较低，1 表示通航干扰风险较高；
- A 中数据类型：`vessel_traffic`；
- A 中数据类别：`dynamic`；
- 默认质量标记：`suspect`；
- 原因：该层是 AIS/统计校准后的模拟结果，不是权威机构直接发布的逐格网通航观测场。

## 4. 与 A 数据体系的统一方式

新增导入命令会读取通航模型输出目录中的 `vessel_traffic_risk_*.nc` 文件，通过
`IngestionPipeline.ingest_netcdf()` 进入 A 原有流程。导入后会自动完成：

- 变量规范化；
- 经纬度坐标规范化；
- `issue_time`、`valid_time`、`ingest_time` 登记；
- SHA-256 校验；
- `data/ready/<route_id>/vessel_traffic/...` 持久化；
- `data/manifest/manifest.sqlite3` 注册；
- 可供 A 的 `list`、`doctor` 和后续 B 侧窗口读取。

## 5. 航线名称映射

| 通航模型源航线 | 工作包 A 标准 route_id |
|---|---|
| `offshore_murmansk_to_offshore_dikson` | `offshore_murmansk_to_offshore_dikson` |
| `tromso_to_svalbard` | `tromso_to_isfjorden_outer` |

第二条航线在 A 中使用 `tromso_to_isfjorden_outer`，用于保持与工作包 A 已有场景、走廊和
接口命名一致。

## 6. 使用方法

默认读取本项目相邻 `my_model` 交付目录中的通航模型输出：

```bash
make import-vessel-traffic
```

也可以显式指定通航模型输出目录：

```bash
arctic-data import-vessel-traffic --source-dir /path/to/model_input --data-root data
```

导入完成后可检查 A 数据目录：

```bash
arctic-data list \
  --data-root data \
  --route-id offshore_murmansk_to_offshore_dikson \
  --data-type vessel_traffic \
  --start 2026-08-06T00:00:00Z \
  --end 2026-08-07T00:00:00Z \
  --as-of 2026-08-07T00:00:00Z
```

## 7. 对 B 工作包的衔接方式

B 工作包可把 `vessel_traffic` 作为综合风险模型的可选动态因子读取。建议 B 侧使用
`vessel_traffic_risk` 作为稳定主变量，并保留 `quality_flag`、`issue_time_evidence`、
`source_route_id` 等元数据。

若该层缺失，B 不应让 A 的 12 类必需层失败；若该层存在，B 可将其作为通航拥挤、邻近船舶
干扰和航道活跃程度的补充风险依据。

## 8. 交付边界

该功能只负责把通航模拟结果纳入 A 的统一数据管理体系，不负责生成真实 AIS 历史轨迹，不声明
通航观测的权威性，也不直接给出航线规划结论。其价值在于补齐 A→B 接口中对“航道通行情况”
动态因子的工程接收能力，并为后续综合风险评估提供统一、可追溯、可降级的数据输入。
