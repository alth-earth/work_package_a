# 原“获取数据”交付包迁移说明

## 1. 先看结论

交付包中的 13 个脚本是兼容输入，不是当前完整预测窗的正式实现：

- 旧 GFS 风/温度/能见度通常固定取 `f000`；
- 多数旧 Copernicus/海冰脚本取当前向过去的少量帧；
- 它们不能证明从模拟时刻向未来覆盖 132/156 h；
- 代码仍在用户 ZIP 中，不是本仓库自包含 source adapter；运行必须显式给
  `--legacy-root`；
- A 的 native GFS 入口才是本轮真实跑通的完整未来窗来源；native Copernicus
  入口已实现，但当前机器无账户，尚未正式入库。

不要因为 registry 有 13 项就写成“13 类实时预测均已完成”。

## 2. 入口映射

| 原目录 | 主入口 | A `data_type` | 规范变量 |
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

## 3. 什么时候用哪个入口

| 目标 | 应使用 |
|---|---|
| GFS 风/温度/能见度 156 h | `arctic-data acquire-forecast --sources gfs` |
| Copernicus 波浪/流/水位/海冰未来窗 | `acquire-forecast --sources copernicus`，提供账户并做磁盘预算 |
| 重放交付包已有文件 | `legacy-run` 或显式 `ingest` |
| 验证旧函数仍可调用 | `legacy-run`，但验收覆盖而不是只看函数返回成功 |
| 新生产来源 | 实现项目内 `DataSource/AcquisitionPublisher` 适配器，不继续增加不可审计脚本 |

## 4. 兼容链做了什么

`LegacyDownloaderRunner`：

1. 调用一个已登记旧函数并捕获 HTTP 交换；
2. 逐 payload/valid time 绑定可审计 issue-time 证据；
3. 读取 `valid_time/time/time+step` 并拆成单时次帧；
4. 生成带 checksum/size/publication ID 的 sidecar；
5. 通过 `FolderWatchSource` 规范化、质检、归档、写 ready 和 manifest。

动态/缓变数据没有内部时间坐标、也没有显式 `valid_time` 时会拒绝，不能再用
`issue_time` 伪造 valid time。static 可由适配器明确提供有效时刻。

```bash
ECCODES_DIR=/root/my_project/work_package_a/.mamba-env \
  .mamba-env/bin/uv run --extra acquisition arctic-data legacy-run \
  --legacy-root "/path/to/获取数据/获取数据" \
  --data-root data \
  --downloader sea_ice_drift
```

Makefile/CLI 会自动定位项目 Mamba 的 ecCodes；直接 Python 调 cfgrib 时仍要确保
`ECCODES_DIR` 已设置。

## 5. issue time 不是“目录更新时间”

- Copernicus `arco_updated_date` 只是服务副本同步时刻，作为
  `copernicus_service_sync` 使用且固定非权威；
- HTTP `Last-Modified` 只有与该帧 valid time 精确绑定时才使用；
- 无证据时默认拒绝；显式 `--allow-conservative-retrieval` 才用成功获取时刻兜底；
- 非权威证据不能产生 `quality_flag=good`。

详见 [ISSUE_TIME_POLICY.md](ISSUE_TIME_POLICY.md)。

## 6. 真实交付样本的方向陷阱

交付包六个冰漂 NetCDF 的 `vxsi/vysi` 虽然变量名像 X/Y，但属性明确是：

```text
eastward_sea_ice_velocity
northward_sea_ice_velocity
```

且样本已重采样到一维经纬度网格。因此它们已经是真东/真北分量，不能再次按极地
投影旋转。A 的判定优先级是：

1. CF `eastward/northward` → 地球坐标，禁止旋转；
2. 明确 `*_x_velocity/*_y_velocity` + 完整 polar stereographic 参数 → 旋转；
3. 只有模糊变量名 → 拒绝，不猜。

样本的坐标时间为 2026，而全局 `bulletin_date/field_date` 有 2024 冲突。A 以有效
时间坐标为准，把旧属性作为不可信候选拒绝，不让它覆盖时间轴。

## 7. 波浪与水深兼容注意

- `VMDR` 为 from direction；A 规范成“从真北来、顺时针”；
- `VTM02` 是均值周期，不等于 `VTPK` 峰值周期，A 不做错误替代；
- `depth` 若 `positive=down`，A 转成正向上 `elevation`，海底为负。

## 8. 旧 B 变量映射只能放适配层

旧 B 制品可能使用：

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

这是旧制品专用映射，不是正式 AB/BC 合同。B 应在一个版本化适配器中处理，正式来源
摘要仍保存 A 的 `data_id/source/version/checksum/issue_time/valid_time`。

## 9. 迁移验收

- 不仅断言函数可 import，还要检查实际 manifest 帧数、valid-time 范围和 132 h 覆盖；
- 每帧有独立、匹配的 issue evidence；
- source payload、raw sidecar、ready checksum 均可 doctor；
- 方向、单位、物理范围和网格身份通过规范化合同测试；
- 没有把缓存 mtime、文件名或错误 HTTP 响应当成发布时间；
- 不把部分失败的保护区集合标成完整硬禁航图。
