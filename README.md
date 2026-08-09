# 北极航线系统工作包 A

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
