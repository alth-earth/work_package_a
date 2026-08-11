# 工作包 A 架构追踪与验收口径（v0.3）

本文只追踪 A。BC/CD 的合同真源已落在工作包 C；B 的完善任务在
`/root/my_project/work_package_b_handoff/`。

## 1. 采集与时间

| 要求 | 实现 | 主要验证 |
|---|---|---|
| GFS 从 `as_of` 覆盖完整未来窗 | `forecast_acquisition.NativeForecastAcquirer.acquire_gfs` | `test_cycle_selection_extends_lead_to_cover_horizon_from_as_of`、真实 156 h 下载 |
| Copernicus 产品/字段/经纬度 part 明确 | `COPERNICUS_FORECAST_SPECS`、`dataset_part="default"` | `test_copernicus_explicitly_requests_default_lat_lon_part`、真实目录/匿名块只读核验 |
| 缺 Copernicus 凭据立即失败 | `_copernicus_credentials_from_environment` | `test_copernicus_requires_complete_credentials_before_toolbox_call` |
| 三时间 UTC 且防未来 | `models.py`、`manifest.py`、`sources.py` | pipeline/manifest/issue-time 测试和 demo |
| Copernicus 服务同步不冒充生产发布时间 | `CopernicusCatalogueIssueTimeResolver` | `tests/unit/test_issue_time.py` |
| HTTP 证据逐帧绑定 | `HttpLastModifiedResolver` | GFS cycle+lead、错误响应和旧缓存绑定测试 |
| 动态数据无 valid time 不伪造 | `legacy.py`、`legacy_downloaders.py`、`temporal_split.py` | legacy runner 和 temporal split 测试 |
| 每批可追溯 source snapshot | `forecast_acquisition.py`、manifest metadata | snapshot identity 和完整 GFS 窗测试 |

## 2. 规范化与内容 QC

| 要求 | 实现 | 主要验证 |
|---|---|---|
| 正规 CF 别名与规范变量 | `specs.py` | normalization/legacy 集成测试 |
| 单位只在白名单内转换 | `normalization._canonicalize_unit` | 未知单位、温度/能见度/速度转换测试 |
| 物理范围、全缺测、inf 拒绝 | `_validate_values`、`_content_quality` | `test_normalization_semantics.py`、pipeline 测试 |
| 风/流/冰漂真东/真北 | `_component_frame`、`_normalize_vector_semantics` | 已是 east/north 不旋转、明确 projected 才旋转、模糊拒绝 |
| 波向统一为 from/真北/顺时针 | `_normalize_wave_direction` | to→from 与冲突声明测试 |
| 水深统一为正向上 elevation | `_normalize_bathymetry` | positive-down 转负 elevation 测试 |
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

## 4. AB 缓存与窗口

| 要求 | 实现 | 主要验证 |
|---|---|---|
| 航线隔离 | 分区 `(route_id,data_type,variable)` | 两 route 同时次不串帧 |
| 同时次 revision 解析 | logical key + quality/issue/ingest 排序 | 新 revision/质量优先测试 |
| 当前 latest 不返回最远未来 | `latest(...at_or_before)`、`latest_for_b` | cache/service 测试 |
| 156 h 目标、132 h 最低完整 | `prepare_window_for_b`、`CoverageReport` | 下支撑、末端、内部缺口测试 |
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

本轮达到：

- GFS：级别 3，Tromsø–Svalbard 156 h/165 首轮记录真实发布；
- Copernicus：产品/变量/目录和末端时效只读核验，Toolbox 正式下载因无凭据未达级别 3；
- 13 个 legacy：兼容链合同测试，不能整体写成级别 3。

## 7. A/B/C/D 边界

A 只发布环境标准帧及来源证据。B 负责时间处理、预测、目标网格、风险、置信度和
`environment_speed_factor`；C 负责最终有效航速和规划；D 只读展示。

当前精确 BC/CD 字段不要从本文件复制，见：

- `work_package_c/docs/BC_CONTRACT.md`
- `work_package_c/docs/CD_CONTRACT.md`
- `work_package_c/schemas/*.json`
- `work_package_b_handoff/工作包B矛盾与完善开发交接书.md`
