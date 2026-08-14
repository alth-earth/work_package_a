> **文档治理声明**
>
> - 本文件角色：文档治理前的工作包 A README 原文归档，不再作为当前入口。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 现行文件去向：[README.md](README.md)；详细交接见 [work_package_a_handoff.md](work_package_a_handoff.md)。
> - 改造原因：原文件混合当前入口、操作说明、历史验收和待办；治理后拆分为短入口、项目 handoff 与专项真值文档。

<!-- ORIGINAL CONTENT START -->

# 北极航线预测驱动动态规划系统：工作包 A

工作包 A 是全系统唯一的环境数据入口：从工作包 A 已配置的网站/API 或历史文件取得数据，保存来源证据，拆成单时次标准帧，规范化并质检，然后按模拟时钟安全地交给 B。

当前版本为 `0.4.2`。这是科研演示数据管线，不是航行安全系统。

第一次接手建议依次阅读：

1. 本 README；
2. [场景、两条航区、14 类数据与四层规划说明](docs/SCENARIOS_AND_SOURCES.md)；
3. [A→B 接口](docs/AB_INTERFACE.md)；
4. [发布时间政策](docs/ISSUE_TIME_POLICY.md)；
5. [本轮大修报告](docs/A_REPAIR_REPORT.md)；
6. AI 开发时再读 [AGENTS.md](AGENTS.md)。

## 1. 当前能做什么

| 能力 | 当前状态 | 准确边界 |
|---|---|---|
| 显式时间窗与双场景 | 已实现 | `retrospective_best_estimate` 与显式锚定的 `frozen_forecast`，禁止隐式 latest |
| GFS 窗口 | 已实现并真实联网验证 | 未来预报；历史优先用 NCEI 分析档案 GRIB byte-range，失败时只回退到官方 THREDDS FileServer |
| Copernicus 窗口 | 已实现；部分实源 smoke | 波浪、含潮总流优先、海面高度、海冰浓度/漂移/厚度/冰型/冰缘；最新 v2 路径和含潮总流仍待补实源复验 |
| GEBCO/EMODnet | 已实现并真实联网 smoke | 水深、正式陆海分类；四类限制区证据但不自动 hard-mask |
| 14 类环境注册表 | 已实现 | 旧 ZIP 是 13 类环境 + 船舶；船舶事实已移至共享包，不属于 A 环境层 |
| 共享场景/船型/运行身份 | 已实现 | 接入 `arctic_route_contracts`，可由精确 A bundle 创建 `RunContext.v2` |
| NetCDF/GeoJSON 摄取 | 已实现 | 必须携带可审计 `issue_time_evidence`；非法单位、坐标、几何或内容会拒绝/降级 |
| raw/ready/manifest | 已实现 | 内容寻址、SHA-256、不可变 revision、恢复和 doctor 检查 |
| AB 缓存/回放 | 已实现 | 航线隔离、版本选择、代次隔离、严格窗口覆盖报告、防未来信息泄漏、可独立复核的 `DatasetBundle.v2` 与逐 payload 语义证明 |
| 统一目标风险网格 | 未实现 | A 保留地理网格身份；B 负责按共享场景网格重采样和融合 |
| 风险、船速、规划、展示 | 不属于 A | 分别由 B、C、D 完成 |

A 不生成 `risk_score`、`risk_level`、`hard_mask`、`confidence` 或
`environment_speed_factor`，也不生成路线。B 根据 A 的环境帧生成风险和环境速度影响；
C 把 B 的环境因子应用到船型基准速度，计算最终有效航速及导师提出的四层航线。

## 2. 全系统数据来源原则

本项目没有自行布设观测设备。正式输入就是 A 从已配置公开网站/API 下载并保存的数据。禁止 B/C/D 绕过 A，再从本地文件名、文件修改时间或今天查询到的目录状态补造历史数据。

```text
共享 Scenario / Corridor / Vessel
        │
        ▼
公开网站/API/历史制品 → 工作包 A
        │
        ▼
采集证据 → 拆帧 → 规范化/QC → raw + ready + manifest
        │ StandardDataFrame + DatasetBundle
        ▼
共享 RunContext.v2
        │
        ▼
工作包 B：时间处理/预测/风险融合/environment_speed_factor
        │ RiskFrame v2
        ▼
工作包 C：最终有效航速、时间依赖规划、滚动重规划
        │ RoutePlan v2
        ▼
工作包 D：只读展示
```

身份约定：A 历史字段名 `ManifestRecord.route_id` 在当前系统中等同于
`corridor_id`（数据裁剪/允许航区）。`scenario_id` 是一次完整演示配置，由共享包
创建，不能拿具体 `plan_id` 替代。A 的 TOML 因此使用 `[corridors.*]`；CLI 的旧
`--scenario` 只作为 corridor 兼容别名，新的完整场景入口是 `--shared-scenario`。

## 3. 时间语义和动态航程窗口

| 字段 | 含义 | 用途 |
|---|---|---|
| `issue_time` | 从哪个 UTC 时刻起，模拟系统才允许知道该帧 | 防止历史回放偷看未来 |
| `valid_time` | 数据描述的环境 UTC 时刻 | B 插值/预测、C 按 ETA 采样 |
| `ingest_time` | A 实际登记时刻 | 运维和审计，不替代前两者 |

回放还要区分 `simulation_time` 与 `knowledge_as_of`。因果模式中二者相同；7 月
`retrospective_best_estimate` 允许模拟时钟停在 7 月，同时显式用更晚的
`knowledge_as_of` 读取事后才归档的最佳估计。后者绝不能写成“严格还原 7 月当时发布
的预测”。

所有读取同时满足：

```text
frame.record.route_id == requested_corridor_id
frame.record.issue_time <= knowledge_as_of
frame.generation_id == current_generation_id
```

窗口不再固定为 156 h、7 天或 9 天。当前共享事实为：主航区默认 168 h、允许
144–216 h；迁移航区默认 96 h、允许 72–144 h。场景可按航程距离、保守航速和缓冲
动态选择，超出上限明确返回 `forecast_coverage_insufficient`。详见
[场景说明](docs/SCENARIOS_AND_SOURCES.md)。

可先执行共享评估，再让 A 用完全相同的候选距离物化并采集冻结场景：

```bash
arctic-route-context recommend-horizon \
  --corridor offshore_murmansk_to_offshore_dikson \
  --vessel nordic_odyssey_reference_v1 \
  --candidate-route-distance-nm 1137

.mamba-env/bin/uv run --extra acquisition --extra contracts arctic-data acquire-forecast \
  --shared-scenario murmansk_dikson_frozen_forecast_template_v1 \
  --shared-simulation-start 2026-08-12T00:00:00Z \
  --shared-candidate-route-distance-nm 1137 --sources gfs
```

所选时域写入新的具体 `scenario_id/version/config_digest`；不会在原模板上原地改值。

- `prepare_window_for_b()` 接受调用方显式给出的目标和最低时域，并返回每类
  `CoverageReport`；`meets_minimum_horizon` 只表示达到最低线，
  `covers_requested_window` 才表示覆盖整个请求窗；
- `provenance_complete` 表示归档型 DataSource 已在磁盘上实际
  复核每条选中帧的 ready 文件，以及原生 source snapshot 或 raw
  publication/checksum 证据（static/event 也不例外）；普通插件自报 metadata
  只能用于诊断，不能授予正式完整状态；
- `complete = covers_requested_window and provenance_complete`，因此“达到最低线”
  不得被写成“完整窗”；内部间隔只要超过声明 cadence 就会
  使对应覆盖判定失败；
- 准备窗口期间若模拟时钟推进或跳转，整次操作抛
  `StaleGenerationError`，调用方应重试，不能混用两个 `as_of_time`。

原生采集帧在 manifest 中声明 `nominal_interval_hours`：冻结预报 GFS 和 wave 为
3 h，NCEI 历史 GFS 分析为 6 h，当前 Arctic current/water/ice 原生产品为 1 h。
显式调用参数优先，其次是 manifest 声明；仅旧帧缺少声明时才使用
`service.py` 的兼容后备值。同一窗口混入互相冲突的 cadence 会被拒绝。

## 4. 规范字段和物理语义

| `data_type` | 规范变量 | 分类/关键语义 |
|---|---|---|
| `wind_field` | `wind_u10`, `wind_v10` | dynamic；真东/真北，`m s-1` |
| `temperature` | `air_temperature_2m` | dynamic；K |
| `visibility` | `visibility` | dynamic；m |
| `wave` | `significant_wave_height`, `mean_wave_direction`, `peak_wave_period` | dynamic；波向为“从真北来、顺时针”，必须圆周插值 |
| `ocean_current` | `ocean_current_u`, `ocean_current_v` | slow；含潮总流优先、detided 后备；互斥且禁止相加 |
| `water_level` | `sea_surface_height` | slow；m |
| `sea_ice_concentration` | `ice_concentration` | slow；0..1 |
| `sea_ice_type` | `ice_type` | slow；neXtSIM 分量确定性派生，不是训练模型 |
| `sea_ice_edge` | `ice_edge` | slow；冰密集度 ≥15% 的冰侧四邻域边界 |
| `sea_ice_drift` | `ice_drift_u`, `ice_drift_v` | slow；真东/真北，`m s-1` |
| `sea_ice_thickness` | `ice_thickness` | slow；m |
| `bathymetry` | `elevation` | static；GEBCO 2026，向上为正；研究层、非核心硬约束 |
| `land_sea_mask` | `land_sea_mask` | static；同一 GEBCO 网格派生；不是可通航掩膜 |
| `long_term_restricted_area` | `restricted_area` | event；四类法律证据分开保存，不自动等于硬禁航 |

共享场景的正式运行画像要求其中 12 类完整；`bathymetry` 与
`long_term_restricted_area` 分别作为可选研究层和信息层。可选只影响正式
`RunContext.v2` 的最低数据集合，不表示 A 未实现这两类接口，也不表示它们可以自动
生成 `hard_mask`。当前 A–B–C 主线 bundle 固定为恰好 12 类；两类可选层另行采集、
另行报告，失败不得阻塞基线。精确清单见
[场景说明](docs/SCENARIOS_AND_SOURCES.md#41-共享场景的-12-类必需层与-2-类可选层)。

矢量旋转的规则是“证据优先”：`eastward/northward` 的 CF `standard_name`
明确表示已经是真东/真北，禁止再次旋转；只有明确的投影 X/Y 分量且极地投影参数完整时才旋转。变量名 `vxo/vyo/vxsi/vysi` 本身不足以证明坐标系。

A 生成 `grid_id`、`coordinate_digest`、`grid_topology` 和来源投影摘要，但不把异构源强行重采样到一个网格。B 应选择共享场景目标网格：连续标量用合适的连续插值，分类/掩膜用 nearest，矢量在真东/真北分量上插值，波向用 `sin/cos` 圆周插值。

原生 Copernicus 会在拆成单时次帧前，从“完整请求时域内所有必需源变量均曾出现
finite 值”派生 `a.source-valid-mask.v2` 布尔 `source_valid_mask`；旧 v1 的
“任一变量”记录仍可审计读取，但新采集不再扩大结构有效域。这个 mask 只表示源数据的空间
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
route/type、`issue_time <= knowledge_as_of`、加载帧与请求 record/generation 一致性。
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
# 查看 A 配置和归档健康
make doctor
.mamba-env/bin/uv run arctic-data config-show

# 默认使用 2026-07-15 起的 Tromsø–Isfjorden 事后最佳估计场景
make acquire-gfs
make acquire-copernicus
make acquire-land-sea-mask  # 12 类基线必需；acquire-static 是兼容别名

# 两类可选研究/信息层独立执行；失败不得阻塞 12 类基线
make acquire-bathymetry
make acquire-emodnet

# 切换主航区；场景唯一决定 bbox、UTC 时域和模式
SCENARIO=murmansk_dikson_july_2026_retrospective_v1 make acquire-gfs

# 冻结预测模板必须显式锚定 UTC 起点，禁止 implicit latest
SCENARIO=murmansk_dikson_frozen_forecast_template_v1 \
SIMULATION_START=2026-08-12T00:00:00Z make acquire-copernicus

# 不使用共享场景时，可显式选择任意源站支持的历史时间段
.mamba-env/bin/uv run --extra acquisition arctic-data acquire-forecast \
  --data-root data \
  --corridor offshore_murmansk_to_offshore_dikson \
  --start 2026-07-15T00:00:00Z --end 2026-07-22T00:00:00Z \
  --mode retrospective_best_estimate \
  --sources gfs --types wind_field temperature visibility

# 事后回放：先把选中 12 类记录的最大 issue_time 写入 KNOWLEDGE_AS_OF，禁止复制固定日期
.mamba-env/bin/uv run arctic-data replay \
  --data-root data \
  --route-id offshore_murmansk_to_offshore_dikson \
  --at 2026-07-15T00:00:00Z \
  --mode retrospective_best_estimate \
  --knowledge-as-of "$KNOWLEDGE_AS_OF" \
  --types wind_field temperature visibility \
  --horizon-hours 168 --minimum-horizon-hours 168 \
  --bundle-output data/output/bundles/murmansk-july.json \
  --summary-only
```

12 类主线、两类可选层的独立命令、RunContext 绑定和两个航区参数见
[SCENARIOS_AND_SOURCES.md](docs/SCENARIOS_AND_SOURCES.md)。

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
    clock=SimulationClock(simulation_start),
    cache=PartitionedABCache(max_memory_mb=512),
)
prepared = a.prepare_window_for_b(
    route_id="offshore_murmansk_to_offshore_dikson",
    data_types=["wind_field", "temperature", "visibility"],
    target_horizon_hours=168,
    minimum_complete_horizon_hours=168,
    knowledge_as_of=knowledge_as_of,
)

for data_type, report in prepared.coverage.items():
    if not report.complete:
        raise RuntimeError(f"{data_type} 覆盖不足: {report}")
    for frame in prepared.frames[data_type]:
        assert frame.record.issue_time <= knowledge_as_of
        assert frame.record.route_id == "offshore_murmansk_to_offshore_dikson"

assert prepared.as_of_time == knowledge_as_of
print(prepared.dataset_bundle.bundle_id)
print(prepared.dataset_bundle.bundle_digest)
print(prepared.payload_attestations)
```

`PreparedWindow.dataset_bundle` 是 `a.dataset-bundle.v2`：它对 corridor、as-of、请求/
最低窗、所有请求类型，以及本次真正暴露给 B 的每条记录（包括边界
支撑帧）的时间、版本、质量、checksum 和源快照做 SHA-256。单个
`source_snapshot_id` 只能识别一个源产品/模型周期及其裁剪选择，且相同
GFS cycle+bbox+types 的不同长度采集可能复用该 ID，不能识别一次精确执行或多源规划输入；
B/C 应保留 `bundle_id + bundle_digest`。v2 还逐类型绑定 records/provenance digest、
正式 cadence、起点支撑、缺口和 complete。Schema 见
[`dataset-bundle-v2.schema.json`](schemas/dataset-bundle-v2.schema.json)。共享包会独立
重算这些证明；v1 只读，不得创建正式 RunContext。跨进程读取不能只做 JSON Schema
形状校验。
`replay` 默认在任一必需层 `complete=false` 时返回非零且不写 bundle；只有诊断时才
显式使用 `--allow-incomplete`，但不完整 bundle 即使在诊断模式也不会持久化。

跨进程或重启后，B 不扫描 A 的 SQLite/ready/raw。由编排层重建相同模拟时钟并显式
提供运行代次和知识截止时间，再调用：

```python
restored = a.resolve_dataset_bundle_for_b(
    persisted_bundle_mapping,
    generation_id=b_input.generation_id,
    knowledge_as_of=b_input.knowledge_as_of,
)
```

该公共入口只接受逐类型 `complete=true` 的 `a.dataset-bundle.v2`，重验规范 JSON、
bundle ID/digest、正式 cadence、当前 simulation/generation、每条精确 manifest revision、
ready payload checksum 和绑定的 raw/source-snapshot provenance；随后从实际归档记录重建
bundle 并要求完全相等。`RunContext.v2` 不包含 `generation_id` 或
`knowledge_as_of`；这些运行态字段必须由编排/B 输入信封显式携带。v1 继续可审计读取，
但不能通过这个正式恢复入口。

`PreparedWindow.frames` 是交给消费者的深拷贝快照；
`PreparedWindow.payload_attestations[data_id]` 是 A 对完整 manifest record 与规范化 payload
（维度、坐标、变量、dtype、shape、值和 attrs）计算的 SHA-256。B 在建立输入信封和真正
build 前都会用公共 `semantic_payload_digest()` 独立复核并再次深拷贝，因此调用者持有的
xarray 别名在 A 校验后发生变化时，不能继续沿用旧 checksum/attestation 生成正式风险帧。

精确缓存、revision、租用和跳转语义见 [AB_INTERFACE.md](docs/AB_INTERFACE.md)。

## 8. 0.4.0 新来源真实 smoke（2026-08-12）

本轮真实联网结果如下。它们证明新来源入口、解析、规范化和正式发布链能够工作，但
只覆盖小区域/短窗口，不冒充“两条走廊 × 两种场景 × 14 类”的完整长窗验收。

| 来源 | 实测内容 | 结果 |
|---|---|---|
| NCEI GFS 历史分析 | `2026-07-15 00Z` 的 `.inv` + 所需 GRIB byte ranges | 传输 937,236 bytes，解析并裁剪出风、2 m 温度、能见度；HTTP Range 证据已保存 |
| GEBCO 2026 | 小区域 OPeNDAP | `bathymetry`、`land_sea_mask` 各 1 条；快照 `gebco-2026-9c8f3b47132c-4b0ee977` |
| EMODnet | 四个独立 Human Activities WFS 图层 | 正式发布 1 条、2 个保护区要素；`suspect`、information、禁止自动 hard-mask |
| Copernicus neXtSIM | `2026-07-15T00:00Z..01:00Z` 冰区 | `sea_ice_type` 与 `sea_ice_edge` 各 2 条，均 `suspect` |

neXtSIM 快照分别为 `cmems-db0abb57475a2680`（冰型）和
`cmems-138bd58a655a2f08`（冰缘）。首次在无冰/无有限分量的小区域测试时按设计失败；
扩大到有冰区域后成功。由此验证了“缺数据不补零”的边界。

NCEI smoke 只完成真实 byte-range 下载、解析和裁剪，没有在该临时目录形成正式
manifest 记录；完整历史场景仍须通过 `acquire-forecast` 全窗发布、回放 bundle 和
`doctor` 后才能交给 B。GEBCO、EMODnet 和 neXtSIM smoke 已写入各自临时 manifest。

升级 `source_valid_mask.v2` 和单次下载复用后，又进行了两次相同 neXtSIM 实源回归；
Copernicus Toolbox 均在打开数据集阶段返回空消息异常，A 在发布前 fail closed。故上表
实源成功证据来自升级前同日代码，最新 v2 路径由合同测试覆盖，仍待源服务恢复后补一次
真实 smoke。两次失败均未生成环境帧或伪装为成功。

## 9. 0.3.1 本机真实全窗验收（2026-08-11，历史基线）

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

这份 bundle 保留为旧 9 类长窗回归基线。0.4.0 已实现冰型、冰缘、水深、限制区和
陆海分类入口，但不能把上述 smoke 静默附到这个旧 bundle；必须按同一新场景重跑 A。

## 10. 0.3.0 本机真实运行基线（历史）

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

历史注意：当时 C 的 `demo_tromso_to_svalbard_v1` 场景时域是
`2026-07-31 .. 2026-08-03`，而这批真实 GFS 是 `2026-08-11 .. 2026-08-18`。
二者空间走廊一致、时间并不一致，不能静默拼接。当前 0.4.0 已新增版本化共享场景，
但要正式联调仍须按该共享场景的精确 UTC 时域重跑 A，并保留该次
`PreparedWindow` 的 `bundle_id + bundle_digest`；不要用单个 GFS
`source_snapshot_id` 冒充之后的多源组合输入，也不要修改旧场景后仍沿用旧
ID/digest。

## 11. 已知未解决项

- 主航区恰好 12 类的 168 h 正式 bundle、RunContext 和 doctor 证据尚未完成；
  `bathymetry`、`long_term_restricted_area` 的可选实源结果须另行报告，不进入本轮
  主线 bundle，也不得阻塞主线；
- 含潮总流已经是首选，detided 只作显式后备；来源可用时仍需记录是否发生降级，
  禁止把两者相加；含潮总流的整点选择已有测试，但尚未补最新代码的小窗真实发布证据；
- A 未实现跨源统一目标网格，B 必须做覆盖检查和明确重采样；
- 旧 13 类脚本不是本仓库自包含的正式未来窗采集器；详情见
  [LEGACY_MIGRATION.md](docs/LEGACY_MIGRATION.md)；
- `FolderWatchSource.scan_once()` 当前推荐单一摄取 owner。多 watcher 在极慢任务超过
  300 s 时没有 heartbeat/租约续期；不要把它部署成无协调的多消费者队列；
- EMODnet 当前目录不是 7 月法律状态的历史重建；保护区/军事区/规划区/Natura 2000
  分开留证，但 A 永远不因图层名自动生成硬掩膜；
- 精确源产品发布时间在部分服务不可得，保守获取时刻保证“不偷看未来”，但不能用于精确延迟统计；
- 每条走廊、每个时域的真实源结果都要单独采集验收；不能用一次
  smoke 或 fixture 通过替代完整窗的帧数、时间范围、coverage 和 doctor 记录。
- `source_valid_mask` 不包含导航或法律语义；正式 `land_sea_mask` 已新增，但
  通航性和限制区 hard/soft 政策仍属于 B/C；
- 水深保留为研究静态层；尚未结合可信吃水、潮位、净空余量与不确定性形成核心安全约束；
- `work_package_b/` 的 `demo_unvalidated` 工程基线及 A→B→C 公共接口夹具链已经交付；
  但现存真实长窗 bundle 仍是旧 v1、旧走廊且只有 9 类，不能用于正式 RunContext，故
  A→B→C **实源**端到端验收仍未完成。

完整修复清单、测试证据和保留风险见 [A_REPAIR_REPORT.md](docs/A_REPAIR_REPORT.md)。

## 12. 文档导航

- [A→B 接口](docs/AB_INTERFACE.md)
- [场景、两条航区、14 类数据与四层规划说明](docs/SCENARIOS_AND_SOURCES.md)
- [本轮大修报告](docs/A_REPAIR_REPORT.md)
- [`issue_time` 政策](docs/ISSUE_TIME_POLICY.md)
- [旧下载器迁移边界](docs/LEGACY_MIGRATION.md)
- [架构追踪](docs/ARCHITECTURE_TRACE.md)
- [跨 B/C/D 边界摘要](docs/BCD_HANDOFF.md)
- [变更记录](CHANGELOG.md)
