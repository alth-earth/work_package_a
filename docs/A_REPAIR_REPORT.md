# 工作包 A 0.3.0 大修与 0.3.1 证据链加固报告

> 本文是 2026-08-11 的历史验收报告，其中“当前缺项”和固定 132/156 h 仅描述
> 0.3.1。当期结论不应覆盖 0.4.0；当前状态见
> [README](../README.md) 与 [场景/14 类说明](SCENARIOS_AND_SOURCES.md)。

日期：2026-08-11  
范围：`${ARCTIC_ROUTE_ROOT}/work_package_a`  
结论：A 已从“旧脚本兼容框架”提升为可真实采集 GFS 完整未来窗、可审计归档、可按
模拟时钟向 B 提供一致窗口的工作包。0.3.1 又修正了“最低窗当成完整窗”、
“单个 source snapshot 当成多源输入身份”、“源插件隐式可信”和“沿岸结构缺测
等于数据坏掉”等问题。Copernicus 凭据从本地忽略文件严格加载后，已完成一条走廊
168 h 的 6 类真实采集、9 类联合回放和完整证据验收；具体数字见 4.3。

## 1. 本轮为什么需要大修

审计旧实现后，发现几个会直接阻塞 B/C 的问题：

1. 旧 13 个入口没有真正生成完整未来窗；GFS 常为 f000，其他动态源多为过去少量帧。
2. 旧默认缓存 2/64 帧，加载完整窗后反而可能把当前支撑帧淘汰。
3. 缓存未按 route 隔离，同服务处理两条走廊可能串帧。
4. issue time 的部分来源语义错误，可能把服务同步时刻称为生产者发布时间。
5. 矢量参考系、波向、水深符号、单位和网格语义不足以支撑 B 正确融合。
6. sidecar 与 payload 未强绑定、ready 历史可覆盖、manifest 更新可破坏不可变性。
7. `prepare_window_for_b` 缺少 132/156 h 完整性和时钟快照一致性。
8. A/C 对同一 Tromsø 走廊 bbox 与 scenario/corridor 名称存在漂移。

本轮没有用全零、随机场或伪造 issue time 掩盖这些问题。

## 2. 关键设计决定

### 2.1 完整窗优先，暂不把短窗滚动当成完成

当前 C 不支持等待动作，最长演示航程约 5.5 天。因此 A 默认：

```text
target_horizon_hours = 156
minimum_complete_horizon_hours = 132
```

0.3.1 把两条验收线分开：

```text
meets_minimum_horizon   = 起点有支撑 + 到达最低末端 + 最低窗内无超限 gap
covers_requested_window = 起点有支撑 + 到达请求末端 + 全请求窗无超限 gap
complete                 = covers_requested_window and provenance_complete
```

所以只达到 132 h 时可以是 `meets_minimum_horizon=true`，但 156 h 请求下
`complete=false`。短窗滚动仍可作为未来部署优化，但不能用 24 h 结果
冒充完整航程输入。

### 2.2 A `route_id` 映射系统 `corridor_id`

A 的 manifest 字段暂不破坏性重命名，但语义冻结为：

```text
A ManifestRecord.route_id == shared scenario.corridor_id
```

配置改为 `[corridors.*]`，CLI 主参数改为 `--corridor`；`--scenario` 与
`config.scenarios` 只是 0.2 兼容别名。`scenario_id` 由共享场景/编排层负责。

### 2.3 A 保留地理网格，B 负责目标网格

A 不在缺少共享网格决策时制造统一伪精度。A 输出：

```text
coordinate_crs, source_grid_crs, grid_topology,
grid_id, coordinate_digest, longitude_wrap, source_grid_mapping
```

B 必须对共享场景目标网格做覆盖检查和明确重采样。当前 C v1 只接受 EPSG:4326、严格
递增的一维 latitude/longitude 二维风险网格。

### 2.4 限制区不自动等于硬禁航

A 校验 GeoJSON 和来源分类，但固定 `automatic_hard_mask_allowed=false`。保护区、规划
区、军事区等是否 hard/soft 必须由 B/C 的版本化场景政策决定。

## 3. 具体修改

### 3.1 原生未来窗采集

新增 `src/arctic_route_data/forecast_acquisition.py`：

- GFS 选择已完整发布的周期；目标从 `as_of` 而不是 cycle 计算；
- 必要时延伸到 f162，f120 后强制合法 3 h lead；
- 一份 GRIB 解析为风/温度/能见度三种标准帧；
- snapshot identity 包含周期、bbox 和变量，避免跨走廊复用错误文件；
- 保留源文件、HTTP 元数据和 checksum；
- Copernicus 产品/字段覆盖 wave/current/water level/ice concentration/drift/thickness；
- 显式 `dataset_part="default"`，防止未来默认切到 projected originalGrid；
- 同时支持官方与项目兼容的两组凭据变量，缺失/半配置 fail-fast。

### 3.2 issue time 和 valid time

- `copernicus_service_sync` 固定非权威；
- native NOMADS filter 使用成功获取时刻作为保守门禁；HTTP Last-Modified 仅审计；
- 旧 HTTP 证据必须能精确绑定 valid time，GFS 解析 cycle+lead；
- 动态/缓变 payload 无时间轴时不再用 issue time 伪造 valid time；
- 修复 scalar numpy datetime64 变成纳秒整数；
- 修复同一维 `time + step` 被错误外积；
- 无效/负 lead、非时间变量伪装 valid_time 明确拒绝。

### 3.3 规范化、方向和 QC

- 增加正式 CF aliases；`VTM02` 不错误映射为 `VTPK`；
- 未知单位拒绝；只做白名单转换；
- 校验全 NaN、inf、物理范围和 missing fraction；
- 风、流、冰漂统一真东/真北；eastward/northward standard name 优先，防止
  `vxsi/vysi` 二次旋转；
- 只有明确 projected X/Y + polar-stereographic 参数才旋转；
- 波向统一 from true north clockwise；
- bathymetry 统一 positive-up elevation；
- rectilinear、curvilinear、unstructured point 分开标识；成对 point 保持坐标和值顺序；
- GeoJSON 验证 Feature、geometry 类型、嵌套、finite、经纬范围、线/环基本结构。

### 3.4 原子发布与不可变档案

- sidecar 强制 checksum、size、publication ID；
- 临时文件和 publication ID 唯一，避免并发生产者互串；
- ready 文件内容寻址，同一逻辑时刻不同内容保留不同路径；
- route/version/publication ID 做路径安全校验；
- 计算出的 normalization/provenance 保留字段不能被 producer metadata 覆盖；
- raw 使用 staging 目录；失败 payload+sidecar 成对隔离；
- manifest 已登记但归档失败时保留输入供幂等重试。

### 3.5 manifest 和 doctor

- manifest insert-only；相同 data ID 仅允许逐字段一致的幂等重试；
- logical uniqueness 加入 source/checksum；
- v1→v2 迁移使用 `BEGIN IMMEDIATE`、行数校验、表交换和中断恢复；
- doctor 检查 checksum、缺文件、orphan ready、pending incoming 和空 manifest；
- 仓库 `.gitkeep` 不再被误判为坏数据。

### 3.6 AB 缓存与模拟时钟

- 分区键加入 route；
- 同 valid time revision 按质量、issue、ingest、version、data ID 确定性选择；
- `latest_for_b` 限制到模拟时刻；最远预报另设 `latest_forecast_for_b`；
- slow/dynamic 容量提高到 256；
- leased 旧帧可退出活动集，lease 结束后再物理删除；
- payload 容器和深层 metadata 隔离；
- event 在读取时随模拟时钟过期；
- prefetch 跳过已缓存 data ID，坏文件逐条隔离并发事件；
- EventBus 一个坏订阅者不再阻断其他订阅者和主流程；
- seek 时 static 只有 `issue_time <= 新时刻` 才跨代复用；
- `prepare_window_for_b` 整次固定同一 ClockSnapshot，处理中普通 tick/seek 均拒绝。

### 3.7 配置与环境

- `configs/work_package_a.toml` 由 typed loader 真正读取；
- `config-show` 输出有效配置；
- Mamba 负责 Python/ecCodes/NetCDF/HDF5，uv 负责 Python 锁；
- Makefile 和 CLI 自动设置项目内 `ECCODES_DIR`；
- 新增 `acquire-gfs`、`doctor` 目标；0.3.1 的 `acquire-copernicus` 从已 Git
  忽略且权限为 600 的 `.env.copernicus` 非回显加载凭据，并支持
  `START/HORIZON_HOURS/TYPES/CORRIDOR`；dotenv 被当作数据严格解析，不执行 shell；
- source snapshots 被 Git 忽略但保留在本地供 B/C 联调。

### 3.8 精确 AB bundle 与 cadence

- `PreparedWindow` 现在显式返回冻结的 `as_of_time`；
- 0.3.1 新增 `a.dataset-bundle.v1`；0.4.0 已升级正式出口为
  `a.dataset-bundle.v2`，对 corridor、as-of、请求/最低时域、请求类型和
  所有实际选中记录的时间、版本、质量、checksum、source snapshot 做确定性
  SHA-256，并逐类型绑定 records/provenance digest、cadence、缺口和全窗证明；
- 单个 `source_snapshot_id` 只代表源产品/模型周期及裁剪选择，相同
  GFS cycle+bbox+types 的不同长度采集可复用该 ID；精确执行和多源 B/C 联调必须
  保留 `bundle_id + bundle_digest`；
- `replay` 输出 coverage、选中 IDs 和 bundle，可用 `--bundle-output` 原子保存；
  不完整窗默认非零退出且不落 bundle，`--summary-only` 只精简 stdout；
- `DatasetBundle.from_dict()` 会复核 record count、规范排序、来源集合、coverage 和
  digest；共享包还会独立重算。v1 只读，不能创建正式 RunContext；
- 原生 GFS 记录声明 3 h，Copernicus wave 声明 3 h，当前 Arctic
  current/water/ice 原生产品声明 1 h。旧记录才使用类型后备值，冲突声明
  拒绝隐式选择。

### 3.9 来源插件、raw 和 snapshot 信任边界

- `DataSource.list_available()` 返回的 record 必须精确匹配 route/type，并满足
  `issue_time <= as_of_time`；
- `load_frame()` 返回的 record、generation、必需变量、上下文 attrs 和有效内容会
  二次校验，防止插件注入未来、空 payload、错走廊或旧代次数据；
- provenance 必须由归档型 DataSource 实际验证 ready 文件及原生 source snapshot
  或 raw publication 的磁盘 checksum/sidecar 证据；任意 truthy
  `source_snapshot_id`、格式正确但未落盘的自报 checksum 都不再算完整；
- 扩展来源只有实现并真正执行可选
  `verified_provenance_id(record) -> str | None` 能力，才可能把诊断窗口提升为
  `provenance_complete=true`；内置 `LocalArchiveSource` 已实现；
- doctor 在 ready checksum 之外核对 raw payload/sidecar 的大小、checksum、
  publication/time/source/version/quality 绑定；
- 已声明 `source_file_checksum` 的记录必须能定位到对应 source snapshot 且
  checksum 一致；新记录还核对精确 `source_snapshot_relative_path`，未声明新绑定
  字段的历史记录保持可读。

### 3.10 方向/符号证据和结构缺测

- 波向不再默认解读；必须有可信 CF `standard_name` 或明确 from/to、
  true north/east、clockwise/counterclockwise 声明，冲突或无证据时拒绝；
- 水深只根据 `positive=up/down` 或可信 CF `standard_name` 决定符号，不按
  `depth/bathymetry` 变量名猜测；
- 原生 Copernicus 在单时次拆帧前，从完整请求中“任一必需变量曾有
  finite 值”派生布尔 `source_valid_mask`；
- content QC v2 分别报告 `structural_mask_fraction` 和有效域内
  `valid_domain_missing_fraction`。该 mask 没有 navigation/classification 语义，不是
  陆海、可通航或法律 `hard_mask`；没有原生证据的旧数据不自动推断。
- 普通 direct/sidecar ingest 自报 mask 会被拒绝；必须绑定 Copernicus 快照的精确
  路径、SHA-256、dataset ID 与请求起止时刻，并与快照 mask 的值、坐标和语义一致。

## 4. 真实数据验收

### 4.1 已完成：NOAA GFS

本小节的数字是 0.3.0 当时的真实采集/回放记录；源文件仍可审计，但其
`complete` 布尔值使用旧语义。0.3.1 必须重新 replay，以分项 coverage 和
`DatasetBundle` 为准。

正式运行首轮：

```text
corridor: tromso_to_svalbard
cycle: 2026-08-11T06Z
leads: f000..f162, 3-hour step
snapshot: gfs-20260811T06Z-67361a2f294f
wind_field: 55
temperature: 55
visibility: 55
total: 165
valid range: 2026-08-11T06:00Z .. 2026-08-18T00:00Z
archive size: about 18 MB
doctor: 165 checked, no errors/warnings
```

以 `2026-08-11T11:17:29Z` 准备 B 窗口：三类各 53 个当时查询范围内的支撑/窗内帧，
`available_start=09:00Z`、`available_end=2026-08-17T21:00Z`，132 h 最低末端为
`2026-08-16T23:17:29Z`；三类在 0.3.0 旧语义下均 complete、无 gap、snapshot ID
一致。

质量为 `suspect`：原因是 availability evidence 为保守获取时刻，而不是已知内容错误。

随后将 A 的 Tromsø bbox 从旧 79.0 对齐 C 的 79.5，并执行第二次真实采集：

```text
snapshot: gfs-20260811T06Z-8840810b511f
records: 165
bbox: [10.0, 68.5, 22.0, 79.5]
usable_as_of: 2026-08-11T11:50:20.281323Z
```

0.3.0 第二次采集结束时 manifest 共 330 条，保留两版不可变 revision；doctor
检查 330 条，零错误/警告，
数据目录约 34 MB。以 `2026-08-11T11:50:21Z` 准备窗口会选中新 snapshot，三类
各 53 个支撑/窗内帧，available end 为 `2026-08-17T21:00Z`，minimum end 为
`2026-08-16T23:50:21Z`，在 0.3.0 旧语义下均 complete 且无 gap。0.3.1 会另取
请求末端上支撑帧，且只在 full-window + provenance 同时成立时返回
`complete=true`。

运行数据保存在 `work_package_a/data/`，由 `.gitignore` 排除但没有删除。

跨包时间边界：C 当前 `demo_tromso_to_svalbard_v1` 仍冻结在
`2026-07-31 .. 2026-08-03` 的旧 B 制品；本批 GFS 为
`2026-08-11 .. 2026-08-18`。它们不能直接联调。正确做法是新增场景版本（不能原地
修改旧 ID），让 `simulation_start/as_of >= 2026-08-11T11:50:20.281323Z`，并为
联调运行保留新 `DatasetBundle` 的 `bundle_id + bundle_digest`。若旧共享合同仅有
`dataset_snapshot_id`，必须在版本化适配中明确它与 bundle 的对应；不得把单个
`gfs-20260811T06Z-8840810b511f` 冒充之后的多源组合输入。

### 4.2 0.3.0 历史状态：Copernicus 当时未正式入库

已真实核验：

- dataset ID、变量和默认 part 正确；
- default part 的流/冰漂已为 eastward/northward，不应旋转；
- wave VMDR 是 from direction；
- 当前目录覆盖超过 156 h，末端数据块存在有限值；
- 匿名 describe/对象只读可行。

当时阻塞：`copernicusmarine.open_dataset/subset 2.4.1` 要求免费账户，0.3.0
验收时本机尚未提供凭据。
此外，区域子集可能触发数百 MB ARCO 分块传输，仅海流两变量实测估算可约 0.65 GB。
因此 0.3.0 当时没有伪造或用全零替代 Copernicus 数据。

### 4.3 已完成：0.3.1 Copernicus + GFS 实源长窗

凭据文件只经过不回显的严格 dotenv 解析，权限为 600，未进入 Git 或报告。真实采集：

```text
corridor: tromso_to_svalbard
requested: 2026-08-11T15:00Z .. 2026-08-18T15:00Z (168 h)

Copernicus:
  ocean_current              169 @ 1 h  cmems-7f520c5e78202c3a
  sea_ice_concentration      169 @ 1 h  cmems-d2c40fc13f2956fb
  sea_ice_drift              169 @ 1 h  cmems-58b83b075b1a18bd
  sea_ice_thickness          169 @ 1 h  cmems-df2a21c3dfbf42e3
  water_level                169 @ 1 h  cmems-bbfae11c2f955ef5
  wave                        57 @ 3 h  cmems-f6c31def2e3ba937
  subtotal: 902 records; warnings: 0

GFS v0.3.1 revision:
  cycle/source snapshot: gfs-20260811T06Z-8840810b511f
  f000..f177; wind/temperature/visibility 各 60 @ 3 h
  subtotal: 180 records; warnings: 0
```

Copernicus current/water/ice 的源结构无效域占比为 `0.2233868569729408`，
有效域内最大缺测为 0；wave 的结构占比为 `0.1798807363235675`，全窗单帧最大
有效域缺测约 `0.035091`。它们没有被误当成导航 mask。全部新帧最终为
`suspect`，原因是 availability 使用保守获取时刻；这不表示内容已知错误。

以 `as_of=2026-08-11T16:00Z` 请求 156 h、最低 132 h 的 9 类联合回放：

```text
requested end: 2026-08-18T04:00Z
minimum end:   2026-08-17T04:00Z
GFS 3 类 + wave: 每类选中 54 条
current/water/ice 5 类: 每类选中 157 条
selected total: 1001
all 9 types: no gaps; meets_minimum=true; covers_requested=true;
             provenance_complete=true; complete=true
cache: 171125694 / 536870912 bytes
```

联合身份：

```text
bundle_id:     a-bundle-c8b2c039c50f92086e3953e6
bundle_digest: c8b2c039c50f92086e3953e6b56858789bb5cbdb9c14e0151cbc70460f286f1d
bundle records: 1001（全部 normalizer 0.3.1）
bundle file: data/output/bundles/tromso-native-20260811T1600Z.json
file SHA-256: 9a41120ff222c818f9a65a48e52f6cb78b5687541294c619d6aa64c2540be0a0
```

`DatasetBundle.from_dict()` 已重新读取并验证该文件。归档最终 `doctor`：
`1419 checked, 0 errors, 0 warnings`；运行目录约 443 MB，其中 source snapshots
约 132 MB、raw 208 MB、ready 97 MB。该数据保留在 `work_package_a/data/` 供 B/C
联调，但被 Git 忽略。

## 5. 对 B/C 的直接影响

B 现在可以不等“所有数据源完美”就继续开发：

- 使用 A 已保留的真实 GFS + Copernicus 9 类完整窗；
- 通过 `PreparedWindow` 得到快照一致的来源集、冻结 `as_of_time`、覆盖报告和
  精确 `DatasetBundle.v2`（旧实源基线 v1 仅作回归，不得直接创建正式 RunContext）；
- 分别根据 minimum/full/provenance 判定继续、降级或拒绝，不再仅看一个含混
  的 `complete`；
- 按 `record.route_id == scenario.corridor_id` 校验；
- 对 A 原网格做显式目标网格处理；
- 保存完整 SourceReference；
- 输出正式 RiskFrame 必须带 `environment_speed_factor`。

C 继续只消费 B 的 RiskFrame，不直接读 A。有效航速责任保持：B 给环境影响系数，C 结合
船型静水速度计算最终值。

## 6. 兼容性变化

- 配置主键 `[scenarios.*]` → `[corridors.*]`；loader 仍接受旧键；
- CLI 主参数 `--scenario` → `--corridor`；旧参数仍可用；
- `window_for_b` 必须传 `(route_id, data_type)`；
- 默认 horizon 24 h → 156 h；
- `CoverageReport.complete` 从“最低 132 h”收紧为“完整请求窗 + provenance”；
- `PreparedWindow` 新增 `as_of_time` 和 `dataset_bundle`；固定位置构造/序列化
  `PreparedWindow` 的下游测试需同步更新；
- 原生帧新增 `nominal_interval_hours/source_snapshot_relative_path`；
- sidecar 新增三个强制 payload identity 字段及完整 evidence.issue_time；
- 非权威 issue evidence 不能配 `quality_flag=good`；
- manifest 由可更新改为不可变；同 ID 不同内容会拒绝。

## 7. 尚未解决且不能淡化的问题

1. 项目内原生采集器仍缺 `sea_ice_type`、`sea_ice_edge`、`bathymetry`
   和 `long_term_restricted_area`；legacy/显式 ingest 可接入，但不能冒充原生完整
   未来窗。
2. 没有额外潮流源；当前 Arctic current 是 detided。
3. A 不做共享目标网格；B 必须完成重采样和覆盖验收。
4. `source_valid_mask` 仅是源数据结构有效域，不提供陆海、通航性或法律
   `hard_mask`。
5. 13 个旧脚本仍依赖外部 ZIP，不能视为生产级自包含 source plugins。
6. 部分源没有精确生产者发布时间；保守门禁防泄漏，但不支持精确延迟分析。
7. `FolderWatchSource` 建议单一 owner；300 s claim 没有 heartbeat，多 watcher 极慢任务
   仍可能争抢。
8. 没有常驻调度、对象存储、生产监控、凭据管理和长期失败重试服务。
9. 限制区法律效力不完整，不能自动成为 hard mask。
10. 每条走廊和时域均需独立实源验收；一次 smoke 不能证明长期持续可用。
11. 156 h 是当前规划目标，不是对任意更长航程的永久保证；调用者必须看 coverage。
12. C 现有演示场景时域与历史真实 GFS 批次不同；正式联调前要新增
    共享场景版本和 digest，并保留 A `DatasetBundle` 身份。

## 8. 验收命令

```bash
cd ${ARCTIC_ROUTE_ROOT}/work_package_a
make check
make doctor
git diff --check
```

0.3.1 最终验收：

```text
Ruff: passed
pytest: 131 passed
uv lock/sync check: passed
CLI help smoke: passed
doctor: 1419 checked, 0 errors, 0 warnings
联合 replay: 9/9 complete, 1001 selected records
DatasetBundle.from_dict: passed
git diff --check: passed
```

下列是 0.3.0 当时的历史结果，仅用于对照：

```text
Ruff: passed
pytest: 87 passed
uv lock/sync check: passed
CLI help smoke: passed
doctor: 330 checked, 0 errors, 0 warnings
cached real GRIB parse: wind/temperature/visibility, 45 x 49 aligned grid
git diff --check: passed
```

0.3.1 真实采集命令：

```bash
START=2026-08-11T15:00:00Z HORIZON_HOURS=168 make acquire-copernicus
START=2026-08-11T15:00:00Z HORIZON_HOURS=168 make acquire-gfs
```

失败仍必须保持为空/缺测，不得生成替代“真实数据”。

## 9. 后续优先顺序

1. B 用共享场景网格实现明确重采样、时间处理、confidence 和速度因子，并以
   上述 bundle 建立新的 2026-08-11 场景版本；
2. 为 `sea_ice_type/edge`、`bathymetry`、`long_term_restricted_area` 增加项目内
   原生、可审计来源；
3. 增加第二条走廊 coverage acceptance 与自动调度；
4. 明确限制区政策数据和潮流来源；
5. 若需要多 watcher，增加可续期 lease/heartbeat 和 publication 状态查询；
6. 将共享 scenario/vessel/contracts 迁到统一目录，保持 ID/version/digest 不变。
