# 北极航线预测驱动动态规划系统：工作包 A

工作包 A 是整个系统的“环境数据入口”。它负责获取和接收多源数据，把不同来源、变量名、单位和时间维度的数据整理成可追溯的标准帧，再按模拟时钟安全地提供给工作包 B。

当前版本：`0.2.0`。当前实现面向离线回放，同时保留目录监控和旧下载器接入方式。

> 第一次接手项目：先读本 README，再读 [A→B 接口](docs/AB_INTERFACE.md) 和 [B/C/D 接手契约](docs/BCD_HANDOFF.md)。AI Agent 还应遵守 [AGENTS.md](AGENTS.md) 中的工程约束。

## 1. A 做什么，不做什么

| 工作包 | 职责 | 当前仓库是否实现 |
|---|---|---|
| A | 下载/接收、发布时间取证、拆帧、规范化、质检、归档、manifest、模拟时钟、AB 缓存 | 是 |
| B | 时间插值/保持/外推、船型风险分量、逐时刻风险融合、生成 BC 风险场 | 否，A 只提供输入契约 |
| C | 读取 BC 风险序列，执行时间依赖路径规划和滚动重规划，写入 CD | 否 |
| D | 读取 CD 最新路线和指标，展示风险、航线、船位和模拟时间 | 否 |

A 不计算 `risk_score`、`hard_mask`、`confidence`，不生成 `route_cost_grid`，也不实现 POLARIS/RIO、A* 或界面渲染。不要把这些逻辑塞进 A 的下载或规范化代码。

## 2. 整体数据流

```text
网站/API/历史文件
        │
        │  NetCDF / GRIB→xarray / GeoJSON
        ▼
旧下载器或新数据源
        │  同时保留源站目录、HTTP 时间和产品 ID 证据
        ▼
┌────────────────────────────── 工作包 A ──────────────────────────────┐
│ issue_time 解析 → 多时次拆帧 → 自动 sidecar → incoming 原子发布      │
│       → 变量/坐标/单位规范化 → 质检 → raw 归档 + ready 发布          │
│       → SQLite manifest → 按模拟时刻过滤 → AB 有界缓存               │
└──────────────────────────────────────────────────────────────────────┘
                                  │ StandardDataFrame
                                  ▼
工作包 B：时间处理、预测、风险融合 → BC：RiskFrame 时间序列
                                  │
                                  ▼
工作包 C：时间依赖规划、滚动重规划 → CD：RoutePlan 最新版本
                                  │
                                  ▼
工作包 D：地图、风险图层、路线、船位、指标与时间轴
```

最重要的隔离规则是：B/C/D 不直接读取旧下载器输出，也不扫描 A 的 `incoming`。下游只消费稳定对象或其序列化形式。

## 3. 一份数据在 A 中经历什么

以一个包含 `0h/6h/12h` 三个预报时次的 NetCDF 为例：

1. `LegacyDownloaderRunner` 调用旧下载器，并记录数据源、产品 ID 和成功 HTTP 响应。
2. `SourceIssueTimeResolver` 从 Copernicus catalogue、源站 `Last-Modified` 或可信文件属性取得 `issue_time`；无可靠证据时严格拒绝。
3. `split_dataset_by_valid_time()` 读取 `valid_time`、`forecast_time`、`time` 或 `time + step`，拆成三个单时次 Dataset。
4. `AcquisitionPublisher` 为每一帧先写 `.part`，再原子改名为 payload，最后写 sidecar。
5. `FolderWatchSource` 只处理 payload 与 sidecar 都完整的项目；失败项进入 `quarantine/`。
6. `IngestionPipeline` 统一变量名、经纬度、单位和 UTC 时间，写入 `ready/`，同时计算 SHA-256。
7. `ManifestStore` 保存时间、空间、来源、版本、质量、路径和校验值。
8. `WorkPackageA` 按模拟时刻加载允许使用的数据，写入 AB 缓存并发布到达/缺测事件。

```text
一个三时次 Dataset
        │
        ├── valid_time=00:00 ──► frame_00.nc + sidecar
        ├── valid_time=06:00 ──► frame_06.nc + sidecar
        └── valid_time=12:00 ──► frame_12.nc + sidecar
                                      │
                                      ▼
                        ready 文件 + 3 条 manifest 记录
```

## 4. 三个时间必须分清

| 字段 | 浅显含义 | 谁使用 |
|---|---|---|
| `issue_time` | 从什么时候起，系统才被允许知道这份资料 | A 过滤未来信息；B/C 必须继续携带 |
| `valid_time` | 这份资料描述哪个环境时刻 | B 的插值/预测轴；C 的预计通过时刻 |
| `ingest_time` | A 实际在什么时候收到并登记资料 | 审计、下载延迟和运维 |

所有时间均为 UTC。任何历史回放查询都必须满足：

```text
record.issue_time <= simulation_time
```

禁止用本地文件修改时间或文件名猜测缺失的 `issue_time`。详细策略见 [ISSUE_TIME_POLICY.md](docs/ISSUE_TIME_POLICY.md)。

## 5. 支持的数据类型

下游应使用规范变量名，不要继续传播源站变量名。

| `data_type` | 规范变量 | 分类 |
|---|---|---|
| `sea_ice_concentration` | `ice_concentration` | slow |
| `sea_ice_type` | `ice_type` | slow |
| `sea_ice_edge` | `ice_edge` | slow |
| `sea_ice_drift` | `ice_drift_u`, `ice_drift_v` | slow |
| `sea_ice_thickness` | `ice_thickness` | slow |
| `wave` | `significant_wave_height`, `mean_wave_direction`, `peak_wave_period` | dynamic |
| `ocean_current` | `ocean_current_u`, `ocean_current_v` | slow |
| `water_level` | `sea_surface_height` | slow |
| `wind_field` | `wind_u10`, `wind_v10` | dynamic |
| `temperature` | `air_temperature_2m` | dynamic |
| `visibility` | `visibility` | dynamic |
| `bathymetry` | `elevation` | static |
| `long_term_restricted_area` | `restricted_area`（GeoJSON） | event |

规范变量、别名和单位的唯一代码来源是 `src/arctic_route_data/specs.py`。

## 6. 项目结构

```text
work_package_a/
├── README.md                         # 项目入口与 B/C/D 接手说明
├── CHANGELOG.md                      # 原 README 与首次交付记录
├── AGENTS.md                         # AI Agent 修改本仓库时的强约束
├── environment.yml                   # Mamba：Python、ecCodes、NetCDF、HDF5
├── pyproject.toml / uv.lock          # uv：Python 依赖、CLI、测试和锁文件
├── Makefile                          # 环境、测试、静态检查、demo
├── configs/
│   ├── work_package_a.toml           # 缓存、时钟和场景示例
│   └── source_release.example.toml   # 数据源发布时间策略示例
├── schemas/
│   └── incoming-sidecar.schema.json  # 上游 sidecar 结构
├── docs/
│   ├── AB_INTERFACE.md               # A→B 已实现接口
│   ├── BCD_HANDOFF.md                # B→C→D 建议契约和开发清单
│   ├── ISSUE_TIME_POLICY.md          # issue_time 证据与降级规则
│   ├── LEGACY_MIGRATION.md           # 13 个旧下载器的迁移方式
│   └── ARCHITECTURE_TRACE.md         # 架构要求到代码/测试的对应关系
├── src/arctic_route_data/
│   ├── legacy_downloaders.py         # 13 个旧入口注册、调用和 HTTP 元数据捕获
│   ├── issue_time.py                 # 发布时间解析和证据对象
│   ├── temporal_split.py             # 多时次 Dataset 拆帧
│   ├── publisher.py                  # payload + sidecar 原子发布
│   ├── normalization.py              # 坐标、变量和单位标准化
│   ├── ingestion.py                  # 质检、SHA-256、ready 发布
│   ├── manifest.py                   # SQLite 索引和防未来查询
│   ├── sources.py                    # DataSource / LocalArchiveSource
│   ├── folder_watch.py               # incoming 扫描、归档和隔离失败项
│   ├── models.py                     # ManifestRecord / StandardDataFrame
│   ├── clock.py                      # 模拟时钟和 generation_id
│   ├── cache.py                      # 类型→变量→时间的 AB 有界缓存
│   ├── events.py                     # 到达、缺测和跳转事件
│   ├── service.py                    # A 的预取与下游读取入口
│   └── cli.py                        # arctic-data 命令行
├── tests/                             # 单元与端到端测试
└── data/
    ├── incoming/                      # 等待摄取；sidecar 最后出现
    ├── raw/                           # payload 与证据 sidecar 的原始归档
    ├── ready/                         # B 可读取的规范化帧
    ├── manifest/manifest.sqlite3      # 主索引
    ├── quarantine/                    # 失败数据，不参与计算
    └── output/                        # 运行输出；不进入 Git
```

## 7. A 提供给 B 的真实接口

A 的稳定输出是 `StandardDataFrame`：

```python
@dataclass(frozen=True)
class StandardDataFrame:
    record: ManifestRecord      # 时间、空间、来源、版本、质量、文件位置
    payload: xr.Dataset | dict  # 单 valid_time 的环境帧或 GeoJSON
    generation_id: int          # 模拟跳转代次
```

`ManifestRecord` 的关键字段：

```text
data_id, data_type, category, route_id, variables,
issue_time, valid_time, ingest_time,
bbox, crs, resolution,
source, quality_flag, version, checksum,
relative_path, media_type, metadata
```

B 的最小接入代码：

```python
from datetime import UTC, datetime

from arctic_route_data.cache import PartitionedABCache
from arctic_route_data.clock import SimulationClock
from arctic_route_data.service import WorkPackageA
from arctic_route_data.sources import LocalArchiveSource

clock = SimulationClock(datetime(2026, 7, 15, 12, tzinfo=UTC))
a = WorkPackageA(
    source=LocalArchiveSource("data"),
    clock=clock,
    cache=PartitionedABCache(max_memory_mb=512),
)

a.prefetch(
    route_id="tromso_to_svalbard",
    data_types=["wind_field", "wave", "sea_ice_concentration", "bathymetry"],
    horizon_hours=24,
)

wind_frames = a.window_for_b("wind_field", hours_before=48, hours_after=24)
for frame in wind_frames:
    assert frame.record.issue_time <= clock.now
    dataset = frame.payload
    valid_time = frame.record.valid_time
```

B 开发时必须做到：

- 只使用 `issue_time <= clock.now` 的帧；A 已过滤，B 仍应保留断言。
- 按 `valid_time` 做 `PASSTHROUGH/HOLD/INTERPOLATE/EXTRAPOLATE`，不要按文件顺序计算。
- 使用 `quality_flag`、数据龄期和预测时长计算 B 自己的 `confidence`。
- 把 `source/version/data_id` 汇总进风险帧的 `source_summary`。
- 计算任务携带 `generation_id`；模拟跳转后丢弃旧代次结果。
- 不把 A 的 `quality_flag` 直接当成风险模型置信度。

模拟跳转时 A 会清空动态、缓变和事件帧。static 帧只有在其
`issue_time <= 新 simulation_time` 时才会改挂到新代次继续复用；向过去跳转不会保留当时尚未发布的静态资料。直接调用缓存重置接口却不提供新模拟时刻时，缓存会安全清空 static，而不是假定它可见。

精确查询方法和缓存租用方式见 [AB_INTERFACE.md](docs/AB_INTERFACE.md)。

## 8. C 和 D 应拿到什么

当前仓库没有实现 BC/CD 缓存，但为了让后续工作能直接开展，建议接口已经固定在 [BCD_HANDOFF.md](docs/BCD_HANDOFF.md)。以下是摘要。

### 8.1 B → C：`RiskFrame`

```text
schema_version, scenario_id, generation_id,
valid_time, as_of_time, generated_at, model_version,
grid, risk_score, risk_level, hard_mask,
confidence, source_summary
```

C 读取覆盖规划时域的 `RiskFrame` 序列，根据船舶预计进入网格的时刻选择/插值风险，而不是始终使用当前风险图。`hard_mask` 是不可扩展节点；`risk_score` 是软代价的一部分。

### 8.2 C → D：`RoutePlan`

```text
schema_version, scenario_id, generation_id,
route_id, plan_version, generated_at, as_of_time, start_time, mode,
waypoints[{longitude, latitude, eta, recommended_speed}],
distance_km, eta_hours, avg_risk, max_risk,
compute_ms, replan_reason, source_risk_versions
```

D 只读取 CD 中最新的不可变 `RoutePlan`，新路线没算完时继续显示上一版并标注更新时间。D 可以按需读取 BC 风险切片和静态底图，但不直接调用 A/B/C 内部计算函数，也不能持有计算锁。

AB 已携带 `route_id` 和 `generation_id`，但当前尚无独立 `scenario_id` 字段；B 创建计算任务时应补充 `scenario_id`，并将它和 `generation_id` 一起传播到 BC/CD。模拟时钟跳转会提升 `generation_id`，B、C、D 必须丢弃旧代次迟到结果。

## 9. 数据目录与 sidecar

下载器不得直接把半成品放进 `ready/`。发布顺序必须是：

```text
payload.nc.part ──写完──► payload.nc ──最后──► payload.metadata.json
```

最小 sidecar：

```json
{
  "file": "sample.nc",
  "data_type": "sea_ice_drift",
  "route_id": "tromso_to_svalbard",
  "issue_time": "2026-07-15T03:00:00Z",
  "valid_time": "2026-07-15T06:00:00Z",
  "source": "Copernicus Marine",
  "version": "product-version",
  "quality_flag": "good",
  "metadata": {
    "issue_time_evidence": {
      "method": "copernicus_catalogue",
      "authority": "Copernicus Marine Data Store",
      "reference": "saved catalogue field path",
      "observed_at": "2026-07-15T03:10:00Z",
      "raw_value": "2026-07-15T03:00:00Z",
      "authoritative": true
    }
  }
}
```

完整 JSON Schema 在 `schemas/incoming-sidecar.schema.json`。

## 10. 环境、验证与常用命令

Mamba 管理 Python 与 ecCodes/NetCDF/HDF5 本地库；uv 管理 Python 包和锁文件。

```bash
cd /root/my_project/work_package_a

make env-create     # 创建项目内 .mamba-env
make sync-all       # 按 uv.lock 安装核心、开发和采集依赖
make check          # Ruff、全部测试、锁文件检查和 CLI 检查
make demo           # 生成小型场景并验证未来信息不会提前泄漏
```

其他常用命令：

```bash
# 校验归档文件、路径和 SHA-256
.mamba-env/bin/uv run arctic-data doctor --data-root data

# 按模拟时刻查询可用帧
.mamba-env/bin/uv run arctic-data list \
  --data-root data \
  --route-id tromso_to_svalbard \
  --data-type wind_field \
  --start 2026-07-15T00:00:00Z \
  --end 2026-07-16T00:00:00Z \
  --as-of 2026-07-15T12:00:00Z

# 运行一个资料包内的旧下载器；必须经过 A 正式入口才会自动写 sidecar
.mamba-env/bin/uv run --extra acquisition arctic-data legacy-run \
  --legacy-root "/path/to/获取数据/获取数据" \
  --data-root data \
  --downloader sea_ice_drift
```

Copernicus 旧下载器使用环境变量 `COPERNICUSMARINE_USERNAME` 和 `COPERNICUSMARINE_PASSWORD`。不要把真实凭据写入代码、README、配置文件或 Git。

## 11. AI Agent 继续开发时的最短路径

### 开发 B

1. 阅读 `docs/AB_INTERFACE.md` 和 `src/arctic_route_data/specs.py`。
2. 先实现按数据类别选择时间策略的处理器，再实现风险分量。
3. 按 `docs/BCD_HANDOFF.md` 发布不可变 `RiskFrame`。
4. 用历史回放测试证明没有使用 `issue_time > simulation_time` 的数据。

### 开发 C

1. 只依赖 BC 契约，不读取 A 的 SQLite 或内部缓存。
2. 首版实现时间依赖 A*，按预计到达时刻采样 `RiskFrame`。
3. 输出多种路线模式和可解释指标，再增加滚动重规划。
4. 传播 `scenario_id/generation_id/source_risk_versions`。

### 开发 D

1. 只消费 CD 最新值，并允许没有新结果时继续渲染旧结果。
2. 将计算时间和渲染时间分离；不要在 UI 线程运行 B/C。
3. 显示模拟时间、结果更新时间、数据质量、路线版本和重规划原因。

### 修改 A

1. 不改变三个时间的语义，不从文件名/mtime 猜 `issue_time`。
2. 新数据类型先修改 `specs.py`，再补规范化、sidecar/manifest 和端到端测试。
3. 任何接口字段变化都要同步 JSON Schema、相关文档和合同测试。
4. 完成后运行 `make check`。

## 12. 当前完成度与已知边界

已完成：13 类旧下载入口、发布时间证据、自动拆帧、sidecar、规范化、原始归档、ready 发布、SQLite manifest、防未来查询、模拟时钟、AB 缓存、质量/缺测事件和测试环境。

尚未实现：统一目标网格重采样、常驻守护进程、对象存储、B 风险模型、BC/CD 缓存、C 路径规划和 D 界面。真实联网下载仍受源站可用性、账号及产品权限影响；源站没有可审计时间时，严格模式会拒绝，保守模式会以成功获取时刻发布并标为 `suspect`。

## 13. 文档导航

- [CHANGELOG.md](CHANGELOG.md)：工作包 A 首次交付和历史实现说明
- [docs/AB_INTERFACE.md](docs/AB_INTERFACE.md)：A 已实现的查询、缓存和时间接口
- [docs/BCD_HANDOFF.md](docs/BCD_HANDOFF.md)：B/C/D 建议对象、边界和验收清单
- [docs/ISSUE_TIME_POLICY.md](docs/ISSUE_TIME_POLICY.md)：发布时间证据和安全降级
- [docs/LEGACY_MIGRATION.md](docs/LEGACY_MIGRATION.md)：原“获取数据”脚本接入方式
- [docs/ARCHITECTURE_TRACE.md](docs/ARCHITECTURE_TRACE.md)：架构要求、代码位置和测试证据
