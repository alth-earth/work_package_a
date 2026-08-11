# 北极航线预测驱动动态规划系统：工作包 A

工作包 A 是全系统唯一的环境数据入口：从工作包 A 已配置的网站/API 或历史文件取得数据，保存来源证据，拆成单时次标准帧，规范化并质检，然后按模拟时钟安全地交给 B。

当前版本为 `0.3.1`。这是科研演示数据管线，不是航行安全系统。

第一次接手建议依次阅读：

1. 本 README；
2. [A→B 接口](docs/AB_INTERFACE.md)；
3. [本轮大修报告](docs/A_REPAIR_REPORT.md)；
4. [发布时间政策](docs/ISSUE_TIME_POLICY.md)；
5. AI 开发时再读 [AGENTS.md](AGENTS.md)。

## 1. 当前能做什么

| 能力 | 当前状态 | 准确边界 |
|---|---|---|
| NOAA GFS 未来窗 | 已实现并真实联网验证 | 风、2 m 气温、能见度；本次真实采集 168 h |
| Copernicus 未来窗 | 已实现并真实联网验证 | 6 类原生数据类型本次真实采集 168 h/902 条；详见第 8 节 |
| 13 类旧下载器接入 | 已有兼容注册表和适配链 | 旧代码仍位于用户交付 ZIP，且多数只取最近/当前帧，不能冒充 156 h 预测源 |
| NetCDF/GeoJSON 摄取 | 已实现 | 必须携带可审计 `issue_time_evidence`；非法单位、坐标、几何或内容会拒绝/降级 |
| raw/ready/manifest | 已实现 | 内容寻址、SHA-256、不可变 revision、恢复和 doctor 检查 |
| AB 缓存/回放 | 已实现 | 航线隔离、版本选择、代次隔离、严格窗口覆盖报告、防未来信息泄漏、精确输入 `DatasetBundle.v1` |
| 统一目标风险网格 | 未实现 | A 保留地理网格身份；B 负责按共享场景网格重采样和融合 |
| 风险、船速、规划、展示 | 不属于 A | 分别由 B、C、D 完成 |

A 不生成 `risk_score`、`risk_level`、`hard_mask`、`confidence` 或
`environment_speed_factor`，也不生成路线。B 根据 A 的环境帧生成风险和环境速度影响；C 把 B 的环境因子应用到船型基准速度，计算最终有效航速。

## 2. 全系统数据来源原则

本项目没有自行布设观测设备。正式输入就是 A 从已配置公开网站/API 下载并保存的数据。禁止 B/C/D 绕过 A，再从本地文件名、文件修改时间或今天查询到的目录状态补造历史数据。

```text
公开网站/API/历史制品
        │
        ▼
工作包 A：采集证据 → 拆帧 → 规范化/QC → raw + ready + manifest
        │ StandardDataFrame；issue_time <= simulation_time
        ▼
工作包 B：时间处理/预测/风险融合/environment_speed_factor
        │ RiskFrame
        ▼
工作包 C：最终有效航速、时间依赖规划、滚动重规划
        │ RoutePlan
        ▼
工作包 D：只读展示
```

身份约定：A 历史字段名 `ManifestRecord.route_id` 在当前系统中等同于
`corridor_id`（数据裁剪/允许航区）。`scenario_id` 是一次完整演示配置，由共享场景层创建，不能拿具体 `plan_id` 替代。A 的 TOML 因此使用 `[corridors.*]`；CLI 的旧参数 `--scenario` 只作为兼容别名保留。

## 3. 三个时间和预测窗

| 字段 | 含义 | 用途 |
|---|---|---|
| `issue_time` | 从哪个 UTC 时刻起，模拟系统才允许知道该帧 | 防止历史回放偷看未来 |
| `valid_time` | 数据描述的环境 UTC 时刻 | B 插值/预测、C 按 ETA 采样 |
| `ingest_time` | A 实际登记时刻 | 运维和审计，不替代前两者 |

所有读取同时满足：

```text
frame.record.route_id == requested_corridor_id
frame.record.issue_time <= as_of_time
frame.generation_id == current_generation_id
```

默认窗口不是旧文档中的 24 h：

- 目标覆盖 `156 h`；
- 最低可用线 `132 h`，对应最长约 5.5 天演示航程；
- `prepare_window_for_b()` 返回每类 `CoverageReport`：`meets_minimum_horizon`
  只表示达到 132 h 最低线，`covers_requested_window` 表示覆盖整个
  156 h 请求窗，`provenance_complete` 表示归档型 DataSource 已在磁盘上实际
  复核每条选中帧的 ready 文件，以及原生 source snapshot 或 raw
  publication/checksum 证据（static/event 也不例外）；普通插件自报 metadata
  只能用于诊断，不能授予正式完整状态；
- `complete = covers_requested_window and provenance_complete`，因此“达到最低
  132 h”不再被写成“完整窗”；内部缺口超过声明 cadence 的 1.5 倍也会
  使对应覆盖判定失败；
- 准备窗口期间若模拟时钟推进或跳转，整次操作抛
  `StaleGenerationError`，调用方应重试，不能混用两个 `as_of_time`。

原生采集帧在 manifest 中声明 `nominal_interval_hours`：GFS 为 3 h、
Copernicus wave 为 3 h，当前 Arctic current/water/ice 原生产品为 1 h。
显式调用参数优先，其次是 manifest 声明；仅旧帧缺少声明时才使用
`service.py` 的兼容后备值。同一窗口混入互相冲突的 cadence 会被拒绝。

## 4. 规范字段和物理语义

| `data_type` | 规范变量 | 分类/关键语义 |
|---|---|---|
| `wind_field` | `wind_u10`, `wind_v10` | dynamic；真东/真北，`m s-1` |
| `temperature` | `air_temperature_2m` | dynamic；K |
| `visibility` | `visibility` | dynamic；m |
| `wave` | `significant_wave_height`, `mean_wave_direction`, `peak_wave_period` | dynamic；波向为“从真北来、顺时针”，必须圆周插值 |
| `ocean_current` | `ocean_current_u`, `ocean_current_v` | slow；真东/真北，`m s-1`；当前 Copernicus 产品为 `detided`，不含潮流 |
| `water_level` | `sea_surface_height` | slow；m |
| `sea_ice_concentration` | `ice_concentration` | slow；0..1 |
| `sea_ice_type` | `ice_type` | slow；分类变量 |
| `sea_ice_edge` | `ice_edge` | slow；分类变量 |
| `sea_ice_drift` | `ice_drift_u`, `ice_drift_v` | slow；真东/真北，`m s-1` |
| `sea_ice_thickness` | `ice_thickness` | slow；m |
| `bathymetry` | `elevation` | static；相对海平面向上为正，海底通常为负 |
| `long_term_restricted_area` | `restricted_area` | event；GeoJSON 分类来源，不自动等于硬禁航 |

矢量旋转的规则是“证据优先”：`eastward/northward` 的 CF `standard_name`
明确表示已经是真东/真北，禁止再次旋转；只有明确的投影 X/Y 分量且极地投影参数完整时才旋转。变量名 `vxo/vyo/vxsi/vysi` 本身不足以证明坐标系。

A 生成 `grid_id`、`coordinate_digest`、`grid_topology` 和来源投影摘要，但不把异构源强行重采样到一个网格。B 应选择共享场景目标网格：连续标量用合适的连续插值，分类/掩膜用 nearest，矢量在真东/真北分量上插值，波向用 `sin/cos` 圆周插值。

原生 Copernicus 会在拆成单时次帧前，从“完整请求时域内任一必需源变量
曾出现 finite 值”派生布尔 `source_valid_mask`。它只表示源数据的空间
有效域，用来把沿岸/陆地等结构缺测与有效域内残余缺测分开。它明确
`navigation_semantics=none` 且 `classification_semantics=none`，不是陆海掩膜、
可通航掩膜或 `hard_mask`。没有这类原生证据的旧帧不会被 A 自动推断结构掩膜。
普通 direct/sidecar ingest 也不能只靠自报 attrs 启用该 mask；必须把它绑定到
`source_snapshots/copernicus` 中的精确相对路径、SHA-256、dataset ID 和请求时域，
并与快照内 mask 逐值、逐坐标、逐语义一致。

## 5. 目录和不可变发布

```text
data/
├── source_snapshots/   # 下载的 GRIB/NetCDF 与 HTTP 证据；运行数据，不进 Git
├── incoming/           # payload 完成后，sidecar 最后出现
├── raw/                # payload + sidecar 原始归档
├── ready/              # 规范化、内容寻址的单时次帧
├── manifest/           # SQLite 主索引
├── quarantine/         # 无效输入
└── output/             # 运行输出，可包含 DatasetBundle JSON
```

发布顺序固定为：

```text
payload.part → payload → payload.metadata.json
```

sidecar 必须把内容和证据绑定起来。简化示例：

```json
{
  "file": "wind.nc",
  "payload_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "payload_size_bytes": 12345,
  "publication_id": "gfs-20260811T06Z-f003-wind",
  "data_type": "wind_field",
  "route_id": "tromso_to_svalbard",
  "issue_time": "2026-08-11T11:16:10Z",
  "valid_time": "2026-08-11T09:00:00Z",
  "source": "NOAA GFS/NOMADS",
  "version": "gfs-20260811T06Z-example",
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
    "forecast_lead_hours": 3
  }
}
```

完整结构见 [incoming-sidecar.schema.json](schemas/incoming-sidecar.schema.json)。非权威证据不能把 payload 标成 `good`。

`doctor` 现在同时校验 ready/manifest checksum、raw payload/sidecar 的大小与
publication/time/source/version/quality 绑定，以及已声明 source snapshot 的存在性和
checksum；新记录还按 `source_snapshot_relative_path` 核对精确路径，而不是只按
basename 猜测。未声明新绑定字段的历史记录保持可读；一旦声明，缺失或篡改就是错误。

自定义 `DataSource` 也不是信任边界。A 在入缓存前验证 record 类型、
route/type、`issue_time <= as_of_time`、加载帧与请求 record/generation 一致性。
Dataset payload 还要匹配 manifest 的必需变量、route/type/issue/valid attrs 且含
有效内容，GeoJSON 必须是 FeatureCollection；不依赖插件自觉过滤未来信息。

## 6. Mamba + uv 环境

Mamba 提供 Python、ecCodes、NetCDF/HDF5 本地库；uv 安装并锁定 Python 包。
直接运行 cfgrib 时必须能找到 Mamba 的 ecCodes；Makefile 和 CLI 已自动处理项目内 `.mamba-env`。

```bash
cd /root/my_project/work_package_a
make env-create
make sync-all
make check
```

常用命令：

```bash
# 查看经过类型校验的有效配置
make doctor
.mamba-env/bin/uv run arctic-data config-show

# 采集默认走廊的 156 h GFS；也可 CORRIDOR=offshore_murmansk_to_offshore_dikson
make acquire-gfs

# 从本地、已 Git 忽略的 .env.copernicus 严格解析凭据（不执行 shell），配方不回显
make acquire-copernicus

# 可选固定时域和类型；TYPES 为空时采集该源默认全部原生类型
START=2026-08-11T15:00:00Z HORIZON_HOURS=156 \
  TYPES="wave ocean_current" make acquire-copernicus

# 等价的显式命令
.mamba-env/bin/uv run --extra acquisition arctic-data acquire-forecast \
  --data-root data \
  --corridor tromso_to_svalbard \
  --sources gfs \
  --types wind_field temperature visibility \
  --horizon-hours 156

# 按历史时刻回放；as-of 必须不早于所需帧的 issue_time
.mamba-env/bin/uv run arctic-data replay \
  --data-root data \
  --route-id tromso_to_svalbard \
  --at 2026-08-11T11:17:29Z \
  --types wind_field temperature visibility \
  --horizon-hours 156 \
  --minimum-horizon-hours 132 \
  --bundle-output data/output/bundles/tromso-example.json \
  --summary-only
```

Copernicus 官方 Toolbox 需要免费账户，使用以下任一成对环境变量：

```text
COPERNICUSMARINE_SERVICE_USERNAME / COPERNICUSMARINE_SERVICE_PASSWORD
COPERNICUSMARINE_USERNAME         / COPERNICUSMARINE_PASSWORD
```

缺失或只配置一半会在下载前失败，不进入交互式登录。代码显式请求
`dataset_part="default"`（经纬度重网格）并保存该选择。Copernicus 的 ARCO
分块传输可能远大于最终子集；先跑一条走廊并预留数 GB 空间。

推荐把上述任一对变量只写入项目根目录 `.env.copernicus`，限制文件
权限为 `600`，然后用 `make acquire-copernicus`。CLI 把 dotenv 当普通数据严格
解析，只接受上述四个键，不会 shell-source。该文件已被 `.gitignore` 覆盖；
不要把凭据写入 README、命令行历史、测试 fixture 或终端输出。

## 7. A→B 最小调用

```python
from arctic_route_data.cache import PartitionedABCache
from arctic_route_data.clock import SimulationClock
from arctic_route_data.service import WorkPackageA
from arctic_route_data.sources import LocalArchiveSource

a = WorkPackageA(
    source=LocalArchiveSource("data"),
    clock=SimulationClock(as_of_time),
    cache=PartitionedABCache(max_memory_mb=512),
)
prepared = a.prepare_window_for_b(
    route_id="tromso_to_svalbard",  # 等同共享配置 corridor_id
    data_types=["wind_field", "temperature", "visibility"],
    target_horizon_hours=156,
    minimum_complete_horizon_hours=132,
)

for data_type, report in prepared.coverage.items():
    if not report.complete:
        raise RuntimeError(f"{data_type} 覆盖不足: {report}")
    for frame in prepared.frames[data_type]:
        assert frame.record.issue_time <= as_of_time
        assert frame.record.route_id == "tromso_to_svalbard"

assert prepared.as_of_time == as_of_time
print(prepared.dataset_bundle.bundle_id)
print(prepared.dataset_bundle.bundle_digest)
```

`PreparedWindow.dataset_bundle` 是 `a.dataset-bundle.v1`：它对 corridor、as-of、请求/
最低窗、所有请求类型，以及本次真正暴露给 B 的每条记录（包括边界
支撑帧）的时间、版本、质量、checksum 和源快照做 SHA-256。单个
`source_snapshot_id` 只能识别一个源产品/模型周期及其裁剪选择，且相同
GFS cycle+bbox+types 的不同长度采集可能复用该 ID，不能识别一次精确执行或多源规划输入；
B/C 应保留 `bundle_id + bundle_digest`。Schema 见
[`dataset-bundle-v1.schema.json`](schemas/dataset-bundle-v1.schema.json)。
跨进程读取时用 `DatasetBundle.from_dict()` 同时核对字段、record count、规范排序、
来源 ID 集合、完整 SHA-256 和 `bundle_id`，不能只做 JSON Schema 形状校验。
`replay` 默认在任一必需层 `complete=false` 时返回非零且不写 bundle；只有诊断时才
显式使用 `--allow-incomplete`，但不完整 bundle 即使在诊断模式也不会持久化。

精确缓存、revision、租用和跳转语义见 [AB_INTERFACE.md](docs/AB_INTERFACE.md)。

## 8. 0.3.1 本机真实全窗验收（2026-08-11）

已固定采集 `tromso_to_svalbard` 的真实 168 h 窗：

```text
requested: 2026-08-11T15:00Z .. 2026-08-18T15:00Z
Copernicus: 902 records, 0 warnings
  ocean_current / concentration / drift / thickness / water_level: 各 169 @ 1 h
  wave: 57 @ 3 h
GFS v0.3.1 revision: 180 records, 0 warnings
  wind / temperature / visibility: 各 60 @ 3 h，f000..f177
```

快照 ID：

```text
gfs-20260811T06Z-8840810b511f
cmems-7f520c5e78202c3a   ocean_current
cmems-bbfae11c2f955ef5   water_level
cmems-d2c40fc13f2956fb   sea_ice_concentration
cmems-58b83b075b1a18bd   sea_ice_drift
cmems-df2a21c3dfbf42e3   sea_ice_thickness
cmems-f6c31def2e3ba937   wave
```

以 `as_of=2026-08-11T16:00Z` 回放 156 h（最低 132 h），9 类均无 gap，
`meets_minimum_horizon/covers_requested_window/provenance_complete/complete`
全部为 true。GFS 三类与 wave 各选 54 条，五类小时产品各选 157 条，共
1001 条；缓存占用 `171125694 / 536870912` bytes。

```text
bundle_id:     a-bundle-c8b2c039c50f92086e3953e6
bundle_digest: c8b2c039c50f92086e3953e6b56858789bb5cbdb9c14e0151cbc70460f286f1d
bundle file:   data/output/bundles/tromso-native-20260811T1600Z.json
file SHA-256:  9a41120ff222c818f9a65a48e52f6cb78b5687541294c619d6aa64c2540be0a0
```

该文件已由 `DatasetBundle.from_dict()` 重新验证；1001 条记录全部来自
`arctic-route-data/0.3.1`。最终 `doctor` 为 `1419 checked`、零错误/警告，
`make check` 为 Ruff 通过、`131 passed`、uv lock/sync 与 CLI help 通过。
运行目录约 443 MB，保留在 `data/` 且不进 Git。

全部 bundle 记录为 `suspect`，原因是源服务 availability 采用保守成功获取时刻，
不是已知内容错误。Copernicus current/water/ice 的结构域外占比约 22.34%，域内
最大缺测为 0；wave 结构域外约 17.99%，域内单帧最大缺测约 3.51%。这些数值只做
数据完整度/QC，不能转成导航 mask。

这份 bundle 可供 B 建立新的 2026-08-11 场景版本；仍不能替代下文列出的
`sea_ice_type/edge`、水深、限制区和共享目标网格。

## 9. 0.3.0 本机真实运行基线（历史）

已实际从 NOAA/NOMADS 下载两版不可变快照并保留在本仓库运行目录 `data/`：

- 周期：`2026-08-11 06Z`；
- 时效：`f000..f162`，每 3 h；
- `wind_field`、`temperature`、`visibility` 各 55 帧，共 165 条首轮记录；
- `valid_time`：`2026-08-11 06:00Z .. 2026-08-18 00:00Z`；
- 首轮旧 bbox 快照：`gfs-20260811T06Z-67361a2f294f`，165 条；
- 与 C 对齐到 bbox `[10.0,68.5,22.0,79.5]` 后的新快照：
  `gfs-20260811T06Z-8840810b511f`，165 条；
- 0.3.0 验收时 manifest 共保留 330 条历史 revision，数据目录约 34 MB；
- `doctor`：330 条全部通过，零错误/警告；
- 0.3.0 当时以 `2026-08-11T11:50:21Z` 回放选中新 bbox 快照，三类各
  53 个当时查询范围内的支撑/窗内帧，无内部缺口且达到 132 h 最低线；
  该历史输出的 `complete=true` 使用了 0.3.0 旧语义。0.3.1 会额外取请求末端
  上支撑帧，并按 minimum/full/provenance 分项判定；应以当前 `replay`
  重新产生的 coverage 和 bundle 为准，不复用旧布尔值。

这些帧标为 `suspect`，不是因为内容已知错误，而是 NOMADS filter 响应的
`Last-Modified` 不能安全等同于模型生产发布时间；A 采用成功获取时刻作为保守门禁。运行数据已保留但被 `.gitignore` 排除，不会随代码提交。

本节记录的是本机实测快照，不代表持续更新服务。重新采集会形成新的不可变 revision；
较新的 `as_of` 会按 revision 规则选中 bbox=79.5 的新版本，旧记录仍可审计。

注意：当前 C 的既有 `demo_tromso_to_svalbard_v1` 场景时域是
`2026-07-31 .. 2026-08-03`，而这批真实 GFS 是 `2026-08-11 .. 2026-08-18`。
二者空间走廊一致、时间并不一致，不能静默拼接。B/C 联调应新增一个版本化场景快照，
使用 `as_of/simulation_start >= 2026-08-11T11:50:20.281323Z`，并保留该次
`PreparedWindow` 的 `bundle_id + bundle_digest`；不要用单个 GFS
`source_snapshot_id` 冒充之后的多源组合输入，也不要修改旧场景后仍沿用旧
ID/digest。

## 10. 已知未解决项

- 原生 Copernicus 当前覆盖 wave、ocean current、water level、sea-ice
  concentration/drift/thickness；`sea_ice_type`、`sea_ice_edge`、`bathymetry`
  和 `long_term_restricted_area` 仍只能经 legacy/显式 ingest 进入，没有项目内原生未来窗采集器；
- Copernicus 海流产品是 `detided`，项目目前没有额外潮流源；
- A 未实现跨源统一目标网格，B 必须做覆盖检查和明确重采样；
- 旧 13 类脚本不是本仓库自包含的正式未来窗采集器；详情见
  [LEGACY_MIGRATION.md](docs/LEGACY_MIGRATION.md)；
- `FolderWatchSource.scan_once()` 当前推荐单一摄取 owner。多 watcher 在极慢任务超过
  300 s 时没有 heartbeat/租约续期；不要把它部署成无协调的多消费者队列；
- 保护区只做几何、来源和法律效果字段校验；A 永远不因图层名自动生成硬掩膜；
- 精确源产品发布时间在部分服务不可得，保守获取时刻保证“不偷看未来”，但不能用于精确延迟统计；
- 每条走廊、每个时域的真实源结果都要单独采集验收；不能用一次
  smoke 或 fixture 通过替代完整窗的帧数、时间范围、coverage 和 doctor 记录。
- `source_valid_mask` 不包含导航或法律语义；B/C 仍需独立的陆海、通航性和
  限制区政策层。
- 真实 GFS 与 C 当前旧演示场景的时域不同；需要新增共享场景版本后才能做正式
  A→B→C 联调。

完整修复清单、测试证据和保留风险见 [A_REPAIR_REPORT.md](docs/A_REPAIR_REPORT.md)。

## 11. 文档导航

- [A→B 接口](docs/AB_INTERFACE.md)
- [本轮大修报告](docs/A_REPAIR_REPORT.md)
- [`issue_time` 政策](docs/ISSUE_TIME_POLICY.md)
- [旧下载器迁移边界](docs/LEGACY_MIGRATION.md)
- [架构追踪](docs/ARCHITECTURE_TRACE.md)
- [跨 B/C/D 边界摘要](docs/BCD_HANDOFF.md)
- [变更记录](CHANGELOG.md)
