# 架构追踪与验收口径

| V2.0 架构要求 | 实现位置 | 验证 |
|---|---|---|
| A 负责下载与预处理，同时与主程序解耦 | `legacy_downloaders.py`、`publisher.py`、`folder_watch.py` | 13 个旧模块可加载；假下载器端到端测试覆盖取时、拆帧、sidecar、ready 和 manifest |
| 自动取得可审计的 `issue_time` | `issue_time.py` | Copernicus catalogue、HTTP `Last-Modified`、可信属性、严格拒绝和保守降级测试 |
| 多时次 NetCDF 拆成标准帧 | `temporal_split.py`、`publisher.py` | `time`、`valid_time`、CFGRIB `time + step`、重复时次拒绝测试 |
| 规范化 NetCDF/GeoJSON | `normalization.py`、`ingestion.py` | 别名、坐标、单位、空数据和逐帧摄取测试 |
| manifest 含类型、时刻、空间、质量、版本和路径 | `models.py`、`manifest.py` | SQLite 查询与 JSON 导出测试 |
| 防未来信息泄漏 | `ManifestStore.list_available/get_*`、`LocalArchiveSource.load_frame` | 单元测试和 demo 双层验证 |
| `LocalArchiveSource` | `sources.py` | 集成测试读取 ready 文件并验 SHA-256 |
| `FolderWatchSource` | `folder_watch.py` | payload/sidecar 发布协议与失败隔离 |
| 标准 AB 数据帧 | `ManifestRecord`、`StandardDataFrame` | UTC、路径、校验和、代次约束测试 |
| 类型→变量→时间缓存 | `cache.py` | 分区统计、窗口查询和顺序测试 |
| 静态/缓变/动态/事件回收 | `cache.py` | 类别保留量、过期和内存上限 |
| B 消费期间不回收 | `cache.lease()` | 引用计数测试 |
| 播放、暂停、倍速、跳转 | `clock.py` | 确定性时钟测试 |
| 跳转重建、迟到任务隔离 | `service.py`、`cache.reset_generation()`；static 仅在 `issue_time <= 新 simulation_time` 时跨代复用 | 向过去跳转回归、无时刻安全清空和旧代次拒绝测试 |
| 缺测明确告警 | `MissingDataAlert` | 不生成全零替代数据 |
| 输出来源摘要和质量 | `ManifestRecord` | `source/quality/version/checksum/metadata` |

## A/B/C 边界

下载、源站取时、预处理、拆帧、sidecar、归档和 manifest 都属于 A，并已进入统一执行链。A 发布的是可追溯的环境数据帧，不是风险图。

A→B 的已实现接口见 [AB_INTERFACE.md](AB_INTERFACE.md)；尚未实现的 BC/CD 建议契约和验收清单见 [BCD_HANDOFF.md](BCD_HANDOFF.md)。

以下截图问题由 B/C 解决，A 只提供其前置数据契约：

- 7 天/60 天风险产品不是逐小时序列；
- `route_cost_grid` 生成程序未交付；
- `hard_mask/confidence/model_version` 的 BC 契约不完整；
- 风险图未船型化、未实现 POLARIS/RIO；
- `sea_mask/passable_mask` 水深走廊范围待确认；
- 综合风险新旧权重、船舶交通变量不一致。

A 不会把这些风险层问题掩盖在数据获取代码里。它会向 B 提供版本明确、时间可追溯且无未来泄漏的环境帧，使 B 能在后续工作中可靠修正这些问题。
