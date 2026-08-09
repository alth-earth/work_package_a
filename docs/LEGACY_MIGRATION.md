# 原“获取数据”包迁移说明

## 定位变化

随资料提供的脚本是 A 的采集与预处理原型。它们的函数通常直接返回“最新三组 `route_id -> xarray.Dataset`”，原先缺少统一发布时间、逐时次发布和 manifest，因此不能直接充当 AB 接口。现在由 `LegacyDownloaderRunner` 把这些原型纳入 A，而不是把下载工作排除到 A 之外。

| 原目录 | 主入口 | A 的 `data_type` | 规范变量 |
|---|---|---|---|
| 冰密集度 | `download_recent_sea_ice_density` | `sea_ice_concentration` | `ice_concentration` |
| 冰型 | `download_recent_ice_type` | `sea_ice_type` | `ice_type` |
| 冰缘 | `download_recent_ice_edge` | `sea_ice_edge` | `ice_edge` |
| 冰漂 | `download_recent_sea_ice_drift_data` | `sea_ice_drift` | `ice_drift_u/v` |
| 海冰厚度 | `download_recent_sea_ice_thickness_data` | `sea_ice_thickness` | `ice_thickness` |
| 波浪 | `download_recent_wave_data` | `wave` | `significant_wave_height/mean_wave_direction/peak_wave_period` |
| 流场数据 | `download_recent_ocean_current_data` | `ocean_current` | `ocean_current_u/v` |
| 水位 | `download_recent_water_level_data` | `water_level` | `sea_surface_height` |
| 获取风向 | `download_recent_wind_field_data` | `wind_field` | `wind_u10/v10` |
| 获取温度 | `download_recent_temperature_data` | `temperature` | `air_temperature_2m` |
| 能见度 | `download_recent_visibility_data` | `visibility` | `visibility` |
| 水深数据 | `download_route_bathymetry_data` | `bathymetry` | `elevation` |
| 长期禁航区 | `download_long_term_restricted_areas` | `long_term_restricted_area` | `restricted_area` |

## 已实现的自动迁移链

1. 调用指定旧下载函数，并在调用期间截取该源站 HTTP 响应的 URL、参数、`Last-Modified` 和观测时刻。
2. Copernicus 数据按 `dataset_id` 查询官方 catalogue；其他源优先使用源站文件响应，最后才检查可信且时刻合理的 NetCDF 全局属性。
3. 从 `valid_time`、`forecast_time`、`time` 或 CFGRIB 的 `time + step` 读取所有有效时次。
4. 一个多时次 Dataset 自动拆成多个单时次 NetCDF；每帧 payload 先原子发布，sidecar 最后发布。
5. `FolderWatchSource.scan_once()` 自动规范化、质检、原始归档、写 `ready` 并登记 manifest。

```bash
uv run arctic-data legacy-run \
  --legacy-root "/path/to/获取数据/获取数据" \
  --data-root data \
  --downloader wind_field
```

严格模式无法取得权威证据时会报 `A102`，不会猜时间。只有显式添加 `--allow-conservative-retrieval`，才以成功获取时刻作为“最晚可用时间”，并将质量标为 `suspect`。

13 个资料内入口优先使用 `LegacyDownloaderRunner`。新加入但尚未登记的旧式函数，也可使用通用 `LegacyDownloaderAdapter`，由项目自己的目录解析器提供证据：

```python
adapter = LegacyDownloaderAdapter(
    module_path=".../冰漂/sea_ice_drift_module.py",
    function_name="download_recent_sea_ice_drift_data",
    data_type="sea_ice_drift",
    data_root="data",
    metadata_resolver=resolve_from_saved_catalog,
)
records = adapter.run()
```

解析器必须返回 `issue_time` 和 `source`；无时间坐标的静态数据还要提供 `valid_time`。多时次 NetCDF 的 `valid_time` 由文件内部自动读取和拆分。如果未提供解析器，适配器会报 `A102 MissingMetadataError`，这是有意的保护措施。

## 随包六个冰漂 NetCDF

文件内 `time` 可作为 `valid_time`，但其中部分 `bulletin_date/field_date` 与 2026 年有效时次明显不符，解析器会把这些陈旧属性拒绝掉。它们仍可用于变量、网格和拆帧兼容性测试；若没有保存当次 Copernicus 目录证据，就不能用本地修改时间或文件名补造历史 `issue_time`。

## 与现有 B 变量名兼容

现有 B 原型曾使用 `sea_ice_concentration_ice_conc` 等清洗后变量名，而 A 的规范变量采用短而稳定的语义名。B 应在单一适配层做映射，不要让源站变量名扩散：

```text
ice_concentration       -> sea_ice_concentration_ice_conc
ice_thickness           -> sea_ice_thickness_sithick
wind_u10 / wind_v10     -> wind_field_u10 / wind_field_v10
significant_wave_height -> wave_VHM0
ocean_current_u/v       -> ocean_current_uo/vo
visibility              -> visibility_vis
elevation               -> bathymetry_elevation
sea_surface_height      -> water_level_zos
```

这层映射应随 B 模型版本一起版本化；A 不复制 B 的风险权重、阈值或 POLARIS 逻辑。
