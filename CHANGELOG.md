# 工作包 A 变更记录

本文件由项目原 README 改名而来，用于保留工作包 A 首次完整交付的实现说明。当前项目入口、运行方法和 B/C/D 接手指南请阅读 [README.md](README.md)。

## 0.3.1 - 2026-08-11：精确数据集身份、覆盖语义与实源证据加固

### AB 窗口和不可变身份

- `CoverageReport` 拆分为 `has_start_support`、`meets_minimum_horizon`、
  `covers_requested_window` 和 `provenance_complete`；`complete` 现在严格等于
  “完整请求窗已覆盖且 provenance 完整”，不再把只达到最低
  132 h 的窗口写成完整。
- `PreparedWindow` 显式固结 `as_of_time`，并附带
  `a.dataset-bundle.v1`。`DatasetBundle` 对 corridor、as-of、请求/最低窗、
  请求类型和全部实际选中记录的时间、版本、质量、checksum、源快照
  做内容寻址。
- 新增 `schemas/dataset-bundle-v1.schema.json`；`replay` 现在输出覆盖报告、
  选中 data ID 和 bundle，并可用 `--bundle-output` 原子保存 JSON；不完整窗口
  默认非零退出且不落 bundle；`--allow-incomplete` 仅改变诊断退出码，仍不落盘。
- `DatasetBundle.from_dict()` 校验字段、record count、规范排序、来源集合和完整
  digest；`--summary-only` 可避免长窗 records 淹没终端，同时保留完整文件。

### 节奏、来源边界和归档审计

- 原生采集记录显式保存 `nominal_interval_hours`：GFS 3 h、Copernicus
  wave 3 h、当前 Arctic current/water/ice 产品 1 h。服务优先使用记录声明，
  只对旧记录使用类型默认值；同窗混入冲突声明时拒绝。
- 不再默认信任第三方 `DataSource`：记录必须匹配请求的 route/type 且
  `issue_time <= as_of_time`；加载帧必须匹配精确 record、generation 和
  payload 变量/上下文/有效内容边界。provenance 也必须由归档型 DataSource 实际
  验证 ready 文件以及原生 source snapshot 或 raw publication 的磁盘证据，
  不再接受任意 truthy metadata 或仅有格式正确的自报 checksum。
- `doctor` 在 ready checksum 之外验证 raw payload/sidecar 的大小、checksum、
  publication/time/source/version/quality 绑定，以及已声明 source snapshot 的存在性和
  checksum 和新记录的精确 `source_snapshot_relative_path`；未声明这些字段的
  历史记录保持可读。

### 物理语义和结构缺测

- 波向必须有可信 CF `standard_name` 或明确的 from/to、true north/east、
  clockwise/counterclockwise 声明；转换后保留源属性，声明冲突或仅凭
  变量名时拒绝。
- 水深只接受明确 `positive=up/down` 或可信 CF `standard_name`；不再按
  `depth/bathymetry` 变量名猜符号，相互冲突的证据会被拒绝。
- 原生 Copernicus 在拆帧前从完整请求派生布尔
  `source_valid_mask`，内容 QC v2 分开“源结构无效域占比”与“有效域内残余
  缺测”。该 mask 没有导航、陆海分类或法律语义；无原生证据时不自动推断。
- 普通 ingest 自报 mask 会被拒绝；必须绑定已归档 Copernicus 快照的精确路径、
  SHA-256、dataset ID 与请求时域，并与快照 mask 的值、坐标和语义一致。

### 运行入口与保留边界

- `make acquire-copernicus` 从已 Git 忽略且权限为 600 的 `.env.copernicus`
  非回显加载凭据，并支持 `START/HORIZON_HOURS/TYPES/CORRIDOR`；dotenv 由 Python
  严格解析为数据而不执行 shell，凭据缺失时在启动下载前失败。
- 原生来源仍未覆盖 `sea_ice_type`、`sea_ice_edge`、`bathymetry`、
  `long_term_restricted_area`；它们只能通过可审计的 legacy/显式 ingest 进入。
- 本节不用 fixture/smoke 代替完整长窗真实验收；实际运行的时间、帧数、
  snapshot/bundle ID、coverage 和 doctor 结果应在完成后单独记录。

### 真实验收结果

- `2026-08-11T15:00Z .. 2026-08-18T15:00Z`：Copernicus 6 类 902 条、
  GFS 3 类 180 条，均零采集 warning；
- `as_of=2026-08-11T16:00Z` 的 156 h 联合回放：9/9 类 complete、无 gap，
  精确选中 1001 条；
- bundle：`a-bundle-c8b2c039c50f92086e3953e6`，完整 digest
  `c8b2c039c50f92086e3953e6b56858789bb5cbdb9c14e0151cbc70460f286f1d`；
- `DatasetBundle.from_dict()` 验证通过；doctor `1419 checked`、零错误/警告；
  Ruff、uv lock/sync、CLI help 通过，pytest `131 passed`。

## 0.3.0 - 2026-08-11：真实未来窗与数据合同大修

0.3.0 对 0.2.0 的“已完成”口径做了纠正：13 项 registry 是外部旧脚本的兼容入口，
不是 13 类完整未来预测均已真实联网验证。以下是本轮实际修改。

### 原生采集与完整时域

- 新增项目内 `forecast_acquisition.py`：GFS 风、2 m 气温、能见度按当前
  `as_of` 向未来覆盖目标 156 h，并按可用周期自动延伸到必要 lead（本次为 f162）。
- 新增 Copernicus 波浪、流、水位、冰密集度、冰漂、冰厚产品/字段映射，显式固定
  `dataset_part="default"`；缺少成对凭据时下载前失败。
- 保存 route/bbox/变量相关的 `source_snapshot_id`、源 GRIB/NetCDF、请求和 checksum；
  `data/source_snapshots` 加入 Git 忽略。
- typed TOML 正式生效；采集身份从含混的 scenario 改为 corridor，旧 CLI 名只作兼容。
- Tromsø 采集 bbox 与 C 配置统一为 `[10.0, 68.5, 22.0, 79.5]`。

### 时间与发布时间证据

- 修复旧入口固定 f000/向过去取帧不能覆盖未来的误解；动态/缓变数据缺 valid time
  时不再用 issue time 伪造。
- Copernicus `arco_updated_date` 改为非权威 service-sync 门禁；NOMADS filter
  `Last-Modified` 不再冒充模型生产发布时间。
- HTTP 证据按请求 URL、GFS cycle+lead 与 valid time 一一绑定；错误响应/其他周期不能
  污染缓存 payload。
- 修复 scalar `numpy.datetime64` 被 `.item()` 变成纳秒整数，以及同维 time+step
  应 pairwise 而非外积的问题。

### 规范化与内容 QC

- 扩展 CF aliases，严格执行单位白名单、转换、物理范围、finite 和缺测覆盖检查；未知
  单位不再只改标签。
- 风、流、冰漂统一为真东/真北分量；已有 eastward/northward 的 `vxsi/vysi`
  不再误旋转，模糊方向拒绝，明确 polar-stereographic X/Y 才旋转。
- 波向统一为 from true north clockwise；水深统一为 positive-up elevation。
- 新增 `grid_id/coordinate_digest/grid_topology/source_grid_mapping`；成对 point 坐标不再
  误标规则网格或分别排序。
- GeoJSON 严格验证 Feature/geometry/坐标嵌套、finite 和经纬范围；限制区默认禁止自动
  转硬掩膜。

### 发布、归档与 manifest

- sidecar 新增强制 `payload_sha256/payload_size_bytes/publication_id`，避免 payload 与
  其他任务证据串绑。
- ready 路径内容寻址；同 valid/issue/version 不再覆盖历史 revision。
- route/version/path 增加安全 token 与根目录检查；生产者保留字段不能覆盖 A 的
  normalization/source/upstream provenance。
- manifest 改为不可变登记和幂等一致重试；v1→v2 使用原子迁移、行数校验和中断恢复。
- Folder watcher 使用原子 claim、成对 quarantine、raw staging 和成功入库后的归档重试。
- `doctor` 新增空库、checksum、orphan ready、incoming 检查，并忽略仓库 `.gitkeep`。

### AB 缓存与服务

- 分区键加入 route_id，修复多航线串帧；逻辑帧按质量、issue、ingest、version 解析
  revision。
- `latest_for_b` 只返回模拟时刻及以前；另增显式 `latest_forecast_for_b`。
- slow/dynamic 默认容量统一为 256；租用帧超限时先退出活动集，lease 结束后删除。
- payload 容器和元数据隔离，消费者修改不再污染缓存。
- event 会随时钟推进过期；坏来源和坏事件订阅者被隔离并报告。
- 新增 `PreparedWindow/CoverageReport`，默认目标 156 h、最低完整 132 h，报告缺口与
  source snapshot。
- 准备窗口固定同一个 ClockSnapshot；普通 tick 或 seek 发生在处理中都会拒绝整个混合
  窗口，修复窗口级未来信息泄漏。
- static 跨代复用继续强制 `issue_time <= 新 simulation_time`；未传时刻安全清空。

### 真实运行证据

- 2026-08-11 从 NOAA/NOMADS 实际下载 Tromsø–Svalbard GFS 06Z，f000..f162，
  风/温度/能见度各 55 帧，共 165 条首轮 manifest 记录。
- valid time 为 `2026-08-11 06:00Z .. 2026-08-18 00:00Z`；首轮目录约 18 MB；
  doctor 165/165 通过，132 h 覆盖检查完整。
- 这些帧因使用保守 retrieval gate 标为 suspect；这是 availability 证据等级，不表示
  内容值已知错误。
- bbox 与 C 对齐到北界 79.5 后又真实采集 165 条新 revision；当前 330 条全部通过
  doctor，较新 as-of 会选择 snapshot `gfs-20260811T06Z-8840810b511f`。
- Copernicus 产品、变量、方向、目录末端时效与匿名数据块已核验，但官方 Toolbox 在
  当前机器无凭据时拒绝下载，因此没有声称正式入库成功；ARCO 分块可能产生数百 MB
  传输。

## Unreleased - 2026-08-09

- 将原项目说明迁移为本变更记录。
- 新增面向人类与 AI Agent 的项目入口 README。
- 新增 B/C/D 接手契约与 AI Agent 工程约束。

## 0.2.0 - 2026-08-08：工作包 A 首次完整交付

这是按《北极航线预测驱动动态规划系统架构设计与实施方案 V2.0》重建的工作包 A。A 就是完整的“获取数据”层：包括调用下载器、截取源站发布时间证据、预处理、拆分多时次文件、规范化、质检、原始归档和发布；同时提供 `manifest`、三类时间戳、历史回放、AB 有界缓存、跳转代次隔离和质量告警。

本包不计算风险、不插值预测、不规划航线，也不把 `route_cost_grid` 当成 A 的输出。

## 已解决的截图问题

| 截图指出的问题 | 本实现 |
|---|---|
| 没有统一 `manifest` | SQLite 主索引，可原子导出 JSON；支持类型、航线、有效时段和模拟时刻查询 |
| 没有 `issue_time` | 13 个旧下载器统一经源站解析器获取：Copernicus 官方目录、源站 HTTP `Last-Modified` 或可信 NetCDF 属性；sidecar 同时保存证据。严格模式取不到就拒绝发布 |
| 没有标准数据帧 | `ManifestRecord + StandardDataFrame` 固定字段、UTC、SHA-256、质量、版本与来源 |
| 没有模拟时钟/历史回放 | `SimulationClock` 提供播放、暂停、倍速、步进和跳转 |
| 可能使用未来观测 | 所有查询都强制 `issue_time <= simulation_time`；另有单元和集成测试 |
| 没有 AB 缓存 | 按“数据类型/变量”分区，静态/缓变/动态/事件分别回收，全局内存有界 |
| 快进/跳转后旧任务串入 | 每次 `seek()` 提升 `generation_id`，旧代次迟到帧会被拒绝；静态帧可复用 |
| 缺少质量、数据龄期和来源摘要 | manifest 保存 `quality_flag/source/version/checksum`；B 可由三类时间戳计算龄期 |
| 下载目录可能被读到半文件 | `incoming` 采用“payload 先原子改名、sidecar 最后发布”，A 写 `ready` 也使用临时文件替换 |
| 多源变量名不统一/一个文件含多个时次 | 13 类数据注册表统一坐标、变量名和常用单位；NetCDF 自动按 `valid_time` 拆成逐时次标准帧 |

## 项目边界与数据流

```text
旧下载器 / 新下载器
        │  Dataset/GeoJSON + 源站目录/HTTP 证据
        ▼
 issue_time 解析 ── 多时次拆帧 ── 自动写 sidecar
        │
        ▼
incoming ──规范化/质检/原子发布──► ready
                                      │
                              SQLite manifest
                                      │ issue_time <= 模拟时刻
                                      ▼
LocalArchiveSource / FolderWatchSource
                                      │
                                      ▼
             AB：类型 → 变量 → 时间有序标准数据帧
                                      │
                                      ▼
                  工作包 B（预测、插帧、风险融合）
```

`LegacyDownloaderRunner` 已登记原包的全部 13 个主入口。调用旧下载函数后，它会自动解析 `issue_time`、拆分时次、写 payload/sidecar、规范化并登记 manifest。项目不会把本地文件修改时间或文件名偷换成 `issue_time`。

## 用 Mamba + uv 建环境

Mamba 负责 Python 和 `eccodes/libnetcdf/HDF5` 等本地库，uv 负责 Python 包、锁文件和项目虚拟环境。
`Makefile` 还会把项目内 Mamba 环境作为 `ECCODES_DIR` 暴露给 uv 虚拟环境，避免 `cfgrib` 找不到动态库。

```bash
cd /root/my_project/work_package_a

# 首次创建；已有环境可改用 make env-update
make env-create
# 创建完整 A 环境（含 Copernicus/GRIB 旧下载器依赖）
make sync-all
make check

# 需要交互使用 uv 时再激活这个项目内前缀
mamba activate /root/my_project/work_package_a/.mamba-env
```

## 30 秒验证

```bash
uv run arctic-data demo --workspace data/demo-run --reset
uv run arctic-data doctor --data-root data/demo-run
```

演示会登记三帧海冰数据：分析帧、已发布的未来预报帧，以及在模拟时刻尚未发布的未来观测。输出的 `future_observation_hidden` 必须为 `true`。

## 运行原“获取数据”下载器

先安装 `acquisition` 扩展并按源站要求设置 Copernicus 凭据，然后选择一个已登记入口：

```bash
uv sync --locked --extra acquisition

export COPERNICUSMARINE_USERNAME="your-account"
export COPERNICUSMARINE_PASSWORD="your-password"

uv run arctic-data legacy-run \
  --legacy-root "/path/to/获取数据/获取数据" \
  --data-root data \
  --downloader sea_ice_drift
```

可选值覆盖海冰密集度、冰型、冰缘、冰漂、冰厚、波浪、流场、水位、风、温度、能见度、水深和长期禁航区，共 13 类。默认是严格模式：源站没有可审计的发布时间就停止，不生成貌似正确的数据。若实时业务宁可延迟使用，也可加 `--allow-conservative-retrieval`，以成功获取时刻作为安全上界；该帧会标为 `suspect`，不能冒充权威发布时间。

一次下载返回多个预报时次时，A 的处理如下：

```text
旧下载器返回一个 Dataset（例如 0h、6h、12h）
                     │
                     ▼
          读取 time / valid_time / time+step
                     │
         ┌───────────┼───────────┐
         ▼           ▼           ▼
       0h.nc       6h.nc       12h.nc
       +sidecar    +sidecar     +sidecar
         │           │           │
         └──────► ready + manifest ──────► B
```

## 摄取真实文件

手工导入模式要求操作者显式给出来源时间证据。若 NetCDF 内有一个或多个时间坐标，`--valid-time` 只是无时间坐标时的后备值：

```bash
uv run arctic-data ingest /path/to/file.nc \
  --data-root data \
  --data-type sea_ice_drift \
  --route-id tromso_to_svalbard \
  --issue-time 2026-07-15T03:00:00Z \
  --valid-time 2026-07-15T00:00:00Z \
  --source "Copernicus Marine catalog entry ..." \
  --issue-authority "Copernicus Marine" \
  --issue-reference "saved catalogue snapshot/path" \
  --version product-version
```

目录监控模式下，上游先把 `sample.nc.part` 写完并原子改名为 `sample.nc`，最后写 `sample.metadata.json`：

```json
{
  "file": "sample.nc",
  "data_type": "sea_ice_drift",
  "route_id": "tromso_to_svalbard",
  "issue_time": "2026-07-15T03:00:00Z",
  "valid_time": "2026-07-15T00:00:00Z",
  "source": "Copernicus Marine product/catalog identifier",
  "version": "20260715"
}
```

然后运行：

```bash
uv run arctic-data scan --data-root data
```

成功后，原始文件归档到 `raw/`，规范文件原子写到 `ready/`，记录写入 `manifest/manifest.sqlite3`。失败 sidecar 进入 `quarantine/`，不会伪装为有效数据。

## 按模拟时刻查询和回放

```bash
uv run arctic-data list \
  --data-root data \
  --route-id tromso_to_svalbard \
  --data-type sea_ice_drift \
  --start 2026-07-13T00:00:00Z \
  --end 2026-07-16T00:00:00Z \
  --as-of 2026-07-15T12:00:00Z

uv run arctic-data replay \
  --data-root data \
  --route-id tromso_to_svalbard \
  --at 2026-07-15T12:00:00Z \
  --types sea_ice_drift bathymetry \
  --horizon-hours 24
```

## 主要目录

```text
work_package_a/
├─ environment.yml             # Mamba：Python 与本地库
├─ pyproject.toml / uv.lock    # uv：Python 依赖与锁
├─ configs/                    # 场景、缓存和发布时间策略示例
├─ schemas/                    # incoming sidecar JSON Schema
├─ src/arctic_route_data/
│  ├─ models.py                # manifest/AB 稳定对象
│  ├─ normalization.py         # 坐标、变量、单位规范化
│  ├─ temporal_split.py         # 多时次 NetCDF 逐 valid_time 拆分
│  ├─ issue_time.py             # 源站发布时间解析与证据
│  ├─ publisher.py              # payload/sidecar 原子发布
│  ├─ legacy_downloaders.py     # 原包 13 个下载入口注册与运行
│  ├─ ingestion.py             # 质检与 ready 原子发布
│  ├─ manifest.py              # SQLite 时间/版本索引
│  ├─ sources.py               # DataSource / LocalArchiveSource
│  ├─ folder_watch.py          # FolderWatchSource
│  ├─ clock.py                 # SimulationClock
│  ├─ cache.py                 # 分区、有界、引用计数和代次隔离
│  └─ service.py               # A 编排和 AB 发布
├─ tests/                      # 单元、合同和端到端测试
└─ data/                       # raw/ready/manifest/incoming/quarantine/output
```

更具体的 B 接口见 [docs/AB_INTERFACE.md](docs/AB_INTERFACE.md)，发布时间规则见 [docs/ISSUE_TIME_POLICY.md](docs/ISSUE_TIME_POLICY.md)，旧“获取数据”迁移见 [docs/LEGACY_MIGRATION.md](docs/LEGACY_MIGRATION.md)，架构追踪见 [docs/ARCHITECTURE_TRACE.md](docs/ARCHITECTURE_TRACE.md)。

## 当前限制

- 自动取时已经接入，但源站若既不提供目录更新时间、HTTP `Last-Modified`，也没有可信文件属性，严格模式仍会拒绝该批数据。可选“成功获取时刻”只是保守上界，会明确标成 `suspect`。
- 当前测试覆盖全部 13 个旧模块的本地加载，以及不联网的完整适配链；真实下载仍取决于源站可用性、Copernicus 账号和产品权限。历史回放若要求“当时看到的目录状态”，应长期保存 sidecar/目录证据，不能事后用今天的目录重建。
- 规范化层统一语义、坐标、变量和单位，但不默认把不同源强行重采样到同一网格；目标网格与插值方法需由 A/B 联调确认后配置，避免制造伪精度。
- `FolderWatchSource.scan_once()` 是确定性的单次扫描接口。常驻守护进程与对象存储可在同一 `DataSource` 契约上扩展。
- 船型、POLARIS/RIO、逐小时预测、`hard_mask` 和 `model_version` 属于 B；A 保证把源数据和元数据无泄漏地交给 B。
