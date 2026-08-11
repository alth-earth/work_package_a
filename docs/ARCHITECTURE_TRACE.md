# 工作包 A 架构追踪与验收口径（v0.3.1）

本文只追踪 A。BC/CD 的合同真源已落在工作包 C；B 的完善任务在
`/root/my_project/work_package_b_handoff/`。

## 1. 采集与时间

| 要求 | 实现 | 主要验证 |
|---|---|---|
| GFS 从 `as_of` 覆盖完整未来窗 | `forecast_acquisition.NativeForecastAcquirer.acquire_gfs` | `test_cycle_selection_extends_lead_to_cover_horizon_from_as_of`、真实 168 h 下载 |
| Copernicus 产品/字段/经纬度 part 明确 | `COPERNICUS_FORECAST_SPECS`、`dataset_part="default"` | 合同测试与真实 168 h/902 条下载 |
| 缺 Copernicus 凭据立即失败 | `_copernicus_credentials_from_environment` | `test_copernicus_requires_complete_credentials_before_toolbox_call` |
| 本地凭据不进 Git/不被 shell 执行/不在 Make 配方回显 | 严格 dotenv parser、`.env.copernicus`、`make acquire-copernicus` | mode 600、允许键、字面值和空/半配置测试 |
| 三时间 UTC 且防未来 | `models.py`、`manifest.py`、`sources.py` | pipeline/manifest/issue-time 测试和 demo |
| Copernicus 服务同步不冒充生产发布时间 | `CopernicusCatalogueIssueTimeResolver` | `tests/unit/test_issue_time.py` |
| HTTP 证据逐帧绑定 | `HttpLastModifiedResolver` | GFS cycle+lead、错误响应和旧缓存绑定测试 |
| 动态数据无 valid time 不伪造 | `legacy.py`、`legacy_downloaders.py`、`temporal_split.py` | legacy runner 和 temporal split 测试 |
| 每批可追溯 source snapshot | `forecast_acquisition.py`、manifest metadata | snapshot identity 和完整 GFS 窗测试 |
| 原生帧声明真实 cadence | `metadata.nominal_interval_hours` | GFS 3 h、wave 3 h、Arctic current/water/ice 1 h 合同测试 |
| 来源插件不能绕过时间/身份/内容边界 | `service._validated_source_record/_validated_source_frame` | future、错 route/type/record/generation、空或错变量 payload 拒绝测试 |

## 2. 规范化与内容 QC

| 要求 | 实现 | 主要验证 |
|---|---|---|
| 正规 CF 别名与规范变量 | `specs.py` | normalization/legacy 集成测试 |
| 单位只在白名单内转换 | `normalization._canonicalize_unit` | 未知单位、温度/能见度/速度转换测试 |
| 物理范围、全缺测、inf 拒绝 | `_validate_values`、`_content_quality` | `test_normalization_semantics.py`、pipeline 测试 |
| 风/流/冰漂真东/真北 | `_component_frame`、`_normalize_vector_semantics` | 已是 east/north 不旋转、明确 projected 才旋转、模糊拒绝 |
| 波向统一为 from/真北/顺时针 | `_normalize_wave_direction` | to→from、east/CCW 转换、源属性保留、冲突/无证据拒绝 |
| 水深统一为正向上 elevation | `_normalize_bathymetry` | positive-down 转负；仅可信 CF/显式 positive；按变量名猜测和冲突拒绝 |
| 结构缺测与域内缺测分离 | `source_valid_mask`、`a.content-qc.v2` | 完整请求前置派生、布尔/语义、source path/checksum/dataset/time 绑定、与快照逐值/坐标/语义比较、普通 ingest 自报拒绝 |
| 网格身份与拓扑 | `_grid_identity`、normalization attrs | rectilinear/curvilinear/unstructured point 测试 |
| GeoJSON 几何和经纬度合法 | `_validate_geojson_features/_geometry` | 越界、NaN、缺 geometry、结构、历史 revision 测试 |
| 限制区不自动硬屏蔽 | `_constraint_summary`、`automatic_hard_mask_allowed=false` | `test_geojson_history.py` |

## 3. 原子发布、归档与 manifest

| 要求 | 实现 | 主要验证 |
|---|---|---|
| payload 与 sidecar 精确绑定 | `publisher.py`、sidecar schema、`ingest_sidecar` | checksum/size/publication ID 错配拒绝 |
| 多生产者文件名不碰撞 | UUID publication ID、唯一临时文件 | publisher 集成测试 |
| ready/raw 历史不可覆盖 | content-addressed ready、immutable manifest | GeoJSON/NetCDF revision、manifest conflict 测试 |
| 路径不可逃逸 | `validate_identifier`、resolved path 检查 | route/version/file traversal 测试 |
| v1→v2 原子迁移 | `ManifestStore._migrate_v1`、backup recovery | migration/immutable tests |
| watcher claim/故障恢复 | `FolderWatchSource` | pair quarantine、archive retry、stale claim 测试 |
| doctor 检查内容和孤儿 | `doctor.inspect_archive` | 空库、orphan、`.gitkeep`、真实档案检查 |
| doctor 校验生产者原始证据 | raw payload/sidecar + 精确 source snapshot path | 大小/checksum/publication/时间/来源绑定与 snapshot 篡改测试 |

## 4. AB 缓存与窗口

| 要求 | 实现 | 主要验证 |
|---|---|---|
| 航线隔离 | 分区 `(route_id,data_type,variable)` | 两 route 同时次不串帧 |
| 同时次 revision 解析 | logical key + quality/issue/ingest 排序 | 新 revision/质量优先测试 |
| 当前 latest 不返回最远未来 | `latest(...at_or_before)`、`latest_for_b` | cache/service 测试 |
| 156 h 目标、132 h 最低分开判定 | `CoverageReport.meets_minimum_horizon/covers_requested_window` | 下支撑、最低末端、请求末端、内部缺口测试 |
| “完整”同时要求全请求窗与来源完整 | `complete = covers_requested_window and provenance_complete` | 只达最低 132 h 或只有插件自报 metadata 时 `complete=false`；归档证据实际落盘验证 |
| 一次 AB 输入的精确不可变身份 | `PreparedWindow.as_of_time/dataset_bundle`、`bundle.py` | 顺序无关 digest、future/跨 corridor/重复拒绝、`from_dict` count/digest 验证 |
| 不完整窗口不能伪装成可交付 bundle | replay fail-closed、`--allow-incomplete` | 默认非零且不写文件；显式诊断才放行 |
| 帧 cadence 优先使用源声明 | `_resolve_expected_interval` | metadata 覆盖 legacy default、冲突声明拒绝 |
| seek/tick 一致性 | 单一 `ClockSnapshot` + 首尾时刻/代次校验 | seek 与普通 tick 竞态测试 |
| static 安全跨代 | `reset_generation(simulation_time=...)` | 向过去 seek、无时刻清空测试 |
| lease 与限额同时正确 | inactive leased entries | leased static/dynamic 分区测试 |
| event 随时钟过期 | `evict_expired_events` | 只推进时钟、无新 put 测试 |
| 消费者不能污染缓存 | `consumer_copy` + readonly arrays/deep metadata | cache 容器隔离测试 |
| 坏来源/坏订阅者不拖垮整批 | load failure event、EventBus handler isolation | service/events 测试 |

## 5. 配置和跨包身份

| 要求 | 当前实现 |
|---|---|
| A 配置指向采集走廊 | TOML `[corridors.*]`；`--corridor` 为主参数 |
| 兼容旧命名 | `--scenario` 和 `config.scenarios` 作为 0.2 只读别名 |
| A `route_id` 对齐全系统 | 明确映射到 `corridor_id`，不冒充 `scenario_id` |
| Tromsø bbox 与 C 对齐 | `[10.0, 68.5, 22.0, 79.5]` |
| 配置真实生效 | typed `config.py`、`config-show`、CLI/Makefile 读取 |

## 6. 真实验收层级

验收结论必须区分：

1. **代码存在**：仅表示入口已实现；
2. **fixture/合同测试通过**：表示结构与边界可重复；
3. **真实源 smoke 通过**：表示在某一日期、账户和区域实际下载成功；
4. **持续运行完成**：还需要调度、凭据、重试、监控和长期覆盖，当前未完成。

已有可追溯结论：

- GFS：级别 3，0.3.1 同窗 revision 180 条（3 类各 60，f000..f177）；
- Copernicus：级别 3，`2026-08-11T15:00Z .. 2026-08-18T15:00Z` 的 6 类
  902 条真实发布；与 GFS 联合 replay 选中 1001 条，9/9 complete，bundle
  `a-bundle-c8b2c039c50f92086e3953e6`；
- 归档：doctor 1419 条零错误/警告；Ruff、uv lock/sync、CLI help 通过，
  pytest 131 passed；
- 13 个 legacy：兼容链合同测试，不能整体写成级别 3。

## 7. A/B/C/D 边界

A 只发布环境标准帧及来源证据。B 负责时间处理、预测、目标网格、风险、置信度和
`environment_speed_factor`；C 负责最终有效航速和规划；D 只读展示。

当前项目内原生采集器仍缺 `sea_ice_type`、`sea_ice_edge`、
`bathymetry`、`long_term_restricted_area`。`source_valid_mask` 只是源数据有效域，
不填补这些导航/法律图层，也不是 B/C 的 `hard_mask`。

当前精确 BC/CD 字段不要从本文件复制，见：

- `work_package_c/docs/BC_CONTRACT.md`
- `work_package_c/docs/CD_CONTRACT.md`
- `work_package_c/schemas/*.json`
- `work_package_b_handoff/工作包B矛盾与完善开发交接书.md`
