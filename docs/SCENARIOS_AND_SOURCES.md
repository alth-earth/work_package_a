# 场景、航区、14 类环境数据与四层规划说明

本文把导师截图、旧“获取数据”说明和 2026-08-12 的系统决策整理成一份当前真源。
它面向 A/B/C/D 开发者，也用于说明为什么“9 天”“14 项”“7 月历史数据”不能只按
文件夹名称理解。

> 本项目是科研演示系统，不是适航、法律或导航安全系统。公开船舶参数和所有算法参数
> 均未完成真实船舶标定。

## 1. 先看整体运行方式

```text
共享 Scenario + Corridor + Vessel
              │
              ▼
A：按完整航程的显式 UTC 窗下载、留证、规范化和归档
              │ DatasetBundle.v2（逐类型覆盖与 provenance 可重算）
              ▼
共享层：生成不可变 RunContext.v2
              │
              ▼
B：逐小时连续化、风险、置信度、环境速度影响
              │ RiskFrame v2
              ▼
C：四层航线、最终有效航速、ETA、重规划
              │ RoutePlan v2
              ▼
D：按同一个 run_id/config_digest 展示
```

每次正式演示都按 A → RunContext → B → C → D 的顺序重新运行，并使用同一个
`scenario_id`、`corridor_id`、`vessel_profile_id`、`config_digest` 和 `run_id`。
不能把 7 月 A、8 月 B 和旧 C 的产物按文件名拼在一起。

## 2. “9 天”不是固定参数

导师所说的 9 天应理解为“覆盖整个航程的全局参考线”，不是写死的采集长度。共享
`HorizonPolicy` 使用以下轻量、可解释的估算：

```text
design_distance = candidate_route_distance（已有候选线时）
                  else great_circle_distance × corridor_detour_factor
planning_speed = nominal_speed × conservative_environment_speed_factor
eta = design_distance / planning_speed
required = 向上取整到 24 h [eta + max(24 h, 20% × eta)]
```

当前资源预算内的场景边界为：

| 航区 | 默认 | 允许范围 | 超出处理 |
|---|---:|---:|---|
| 摩尔曼斯克外海—迪克森外海 | 168 h | 144–216 h | `forecast_coverage_insufficient` |
| 特罗姆瑟外海—伊斯峡湾外部入口 | 96 h | 72–144 h | `forecast_coverage_insufficient` |

不得把超出数据源覆盖的尾段静默截掉后仍称作“完整航程预测”。216 h 是当前 C 和个人
电脑演示的硬上限；以后若航速、候选距离或来源能力变化，应产生新场景版本和摘要。

公式已有可执行入口，不再只是文档建议：

```bash
cd /root/my_project/arctic_route_contracts
.venv/bin/arctic-route-context recommend-horizon \
  --corridor offshore_murmansk_to_offshore_dikson \
  --vessel nordic_odyssey_reference_v1 \
  --candidate-route-distance-nm 1250
```

在冻结模板采集时把同一距离传给 A：

```bash
.mamba-env/bin/uv run --extra acquisition --extra contracts arctic-data acquire-forecast \
  --shared-scenario murmansk_dikson_frozen_forecast_template_v1 \
  --shared-simulation-start 2026-08-12T00:00:00Z \
  --shared-candidate-route-distance-nm 1250 --sources gfs
```

物化后的场景 ID 会带非默认时域（如 `_h144`）。若没有候选线，则明确使用模板保守
默认值；若计算结果超过上限则在下载前报 `forecast_coverage_insufficient`。

## 3. 两条航区事实

全系统唯一事实保存在 `../arctic_route_contracts/configs/corridors/`。A 内 TOML 只保留
兼容映射；A 的历史 `route_id` 等同于共享 `corridor_id`。

### 3.1 主开发航区：摩尔曼斯克外海—迪克森外海

适合具有一定冰区加强能力的 6 万—8 万吨级重载散货船科研演示。

| 项目 | 经度 | 纬度 | 说明 |
|---|---:|---:|---|
| 摩尔曼斯克外海起点 | 33.60°E | 69.15°N | 避开港池内部 |
| 迪克森外海终点 | 80.40°E | 73.55°N | 避开港内及近岸浅冰 |
| 起点允许区 | 33.00–34.50°E | 68.90–69.40°N | 起点栅格自动修正范围 |
| 终点允许区 | 79.80–81.00°E | 73.30–73.80°N | 终点栅格自动修正范围 |

本航区用于冻结 B 风险参数和 C 规划参数。

### 3.2 迁移验证航区：特罗姆瑟外海—伊斯峡湾外部入口

| 项目 | 经度 | 纬度 | 说明 |
|---|---:|---:|---|
| 特罗姆瑟外海起点 | 19.00°E | 69.75°N | 验证算法起点 |
| 伊斯峡湾外部终点 | 13.00°E | 78.15°N | 气象导航算法终点 |
| 朗伊尔城参考点 | 15.65°E | 78.22°N | 仅用于 AIS 完整航次识别 |
| 起点允许区 | 18.00–20.50°E | 69.40–70.00°N | 起点匹配范围 |
| 终点允许区 | 12.00–16.50°E | 77.90–78.40°N | AIS 航次终点筛选范围 |

C 的优化终点止于伊斯峡湾外部。朗伊尔城及峡湾内部轨迹可用于 AIS 航次识别，但不纳入
本项目的气象航路优化评价。迁移验证必须复用主航区相同的 B 模型摘要和风险参数；若为
测试航区单独调参，就不能称为迁移能力验证。

## 4. 旧 ZIP 的“14 项”如何映射

旧 ZIP 实际是 13 类环境目录加 1 个船舶 CSV 目录。当前 A 正式注册 14 类环境数据：
在原 13 类基础上新增了更基础的 `land_sea_mask`。船舶信息改由独立共享包负责，因此
不会和环境帧混在一起。

| A `data_type` | 当前来源/处理 | 时空角色 | 本轮边界 |
|---|---|---|---|
| `wind_field` | GFS；历史用 NCEI 分析，冻结场景用预报 | dynamic | 真东/真北；历史 6 h、冻结预报 3 h |
| `temperature` | GFS 2 m 温度 | dynamic | K；历史 6 h、冻结预报 3 h |
| `visibility` | GFS 地面能见度 | dynamic | m；历史 6 h、冻结预报 3 h |
| `wave` | Copernicus wave | dynamic | 波向为 from true north clockwise，3 h |
| `ocean_current` | Copernicus 含潮总流优先；detided 后备 | slow | 逐小时输出，两个来源互斥、禁止相加 |
| `water_level` | Copernicus 海面高度 | slow | m |
| `sea_ice_concentration` | Copernicus | slow | 0..1 |
| `sea_ice_type` | neXtSIM 分量确定性派生 | slow/class | 未来产品；不是 ML 模型 |
| `sea_ice_edge` | `ice_concentration >= 0.15` 的冰侧四邻域边界 | slow/class | 与冰型共用一次 neXtSIM 下载 |
| `sea_ice_drift` | Copernicus | slow | 真东/真北，m/s |
| `sea_ice_thickness` | Copernicus | slow | m |
| `bathymetry` | GEBCO 2026 | static | 研究层；elevation 向上为正 |
| `land_sea_mask` | 同一 GEBCO elevation 确定性派生 | static/class | 海=1、陆/岸=0；不是可通航 mask |
| `long_term_restricted_area` | EMODnet 四类 WFS 证据 | event/policy | information；不自动 hard-mask |

“9 项”只表示 0.3.1 已完成真实原生长窗联合回放验收的数量，不是接口上限。0.4.0 已有
14 类规范注册和对应原生入口/派生规则，但还没有完成两航区、两模式、14 类的全部长窗
真实矩阵验收。

### 4.1 共享场景的 12 类必需层与 2 类可选层

当前共享 `ScenarioDefinition` 数据画像把以下 12 类列为正式运行必需层：
`wind_field`、`temperature`、`visibility`、`wave`、`ocean_current`、
`water_level`、`sea_ice_concentration`、`sea_ice_type`、`sea_ice_edge`、
`sea_ice_drift`、`sea_ice_thickness` 和 `land_sea_mask`。创建正式
`RunContext.v2` 时，A 的 `DatasetBundle.v2` 必须完整覆盖这 12 类，且不能包含场景
画像之外的类型。

`bathymetry` 与 `long_term_restricted_area` 是共享场景的可选研究/信息层：缺少它们
不会单独阻止正式运行上下文成立，但 A 已实现并保留两类规范接口、采集入口和来源证据，
完整 14 类验收仍应采集它们。“可选”不表示未实现，也不授予导航安全语义；水深当前
不是核心净水深硬约束，限制区也不得自动变成 `hard_mask`。

## 5. 四个容易混淆的数据决策

### 5.1 冰型属于 A 还是 B

A 负责下载 neXtSIM 连续未来窗，并把源分量按版本化确定性规则整理为冰型类别；B 只消费
A 发布的环境帧并形成风险。这里没有训练模型，不需要一天训练，也不能把 A 下载到的未来
冰型写成“B 预测冰型”。以后若确实训练时序预测模型，它的训练代码、权重、评估和
`model_config_digest` 才归 B。

### 5.2 冰缘为什么不训练模型

当前 MVP 按常用的 15% 冰密集度阈值形成冰区，再提取冰侧四邻域边界。它确定、快速、可
审计，并复用冰型下载。它是栅格派生，不是精细海图冰缘，也没有真实导航安全背书。

### 5.3 水深和陆海 mask 的区别

- `bathymetry` 是连续高程，可供研究浅水代价或以后计算净水深；当前缺少可信吃水裕量、
  潮位合成和误差模型，所以不作为核心安全硬约束。
- `land_sea_mask` 是正式表面分类，可作为 B 构建陆地硬约束的基础事实。
- `source_valid_mask` 只说明某个数据源在哪些格点有完整必需变量，不能替代前两者，也
  不能直接变成 `hard_mask`。

### 5.4 “长期禁航区”不是一个法律类别

A 分别保留 marine protected area、military area、maritime spatial plan 和
Natura 2000 site 的原始类别、来源属性、可获得的主管机关与生效时间。当前统一标为
`navigation_effect=information` 且 `automatic_hard_mask_allowed=false`。以后由 B/C 的
版本化政策规则决定 hard/soft/information；不能因为图层名称含“保护”或“军事”就自动
宣布禁航，也不能因缺少法律证据就自动当作安全。

## 6. 双场景历史语义

共享配置给每条航区各保留两个版本化场景：

1. `retrospective_best_estimate`：当前从历史档案下载的事后最佳估计。它适合算法开发和
   两航区同参对比，但不能声称严格还原 7 月当时真实发布、当时可见的预测。
2. `frozen_forecast`：演示前显式给出 UTC `simulation_start`，冻结当次可取得的预报
   周期与数据快照。禁止隐式使用 `latest`。

当前 7 月事后场景统一从 `2026-07-15T00:00:00Z` 开始。事后回放有两个时间：

```text
simulation_time：模拟船走到了哪一小时，决定放出哪个 valid_time
knowledge_as_of：允许使用截至何时已归档的 revision，决定 issue_time 门禁
```

例如 8 月才下载的 7 月最佳估计帧，其保守 `issue_time` 在 8 月；模拟时钟仍可以停在
7 月，但必须显式把 `knowledge_as_of` 设到实际下载之后，并把运行标成
`retrospective_best_estimate`。因果演示则始终令 `knowledge_as_of == simulation_time`。

## 7. 与导师四层 C 规划的关系

以下四层全部属于 C，A 只保证相同 RunContext 下的数据覆盖：

| 层级 | 时间范围 | 主要任务 | A/B/C 关系 |
|---|---|---|---|
| 全航程参考线 | 整个实际航程 | 起点到终点的总体航道、大尺度通道 | A 覆盖全窗；B 给全窗风险；C 生成参考线 |
| 主通道 | 24–72 h | 判断未来进入哪个冰区通道 | B 按时序风险更新；C 在参考线约束下选择通道 |
| 滚动优化 | 0–24 h | 高精度气象导航和冰区避险 | C 按新 RiskFrame 重规划 |
| 可执行线 | 0–6 h | 连续监控并形成实际执行建议 | C 分 0–2 h 高可信、2–4 h 推荐、4–6 h 预测 |

四层要传播同一个 `run_id/config_digest/generation_id`，并各自带 revision。当前 C 已
冻结 v2 合同和现有时间依赖 A*，四层是下一阶段增量路线图，不应在 A 中实现，也不应
把文档路线图写成已完成算法。

## 8. 推荐运行命令

先检查共享场景：

```bash
cd /root/my_project/work_package_a
.mamba-env/bin/uv run --extra contracts arctic-data shared-scenario \
  --scenario murmansk_dikson_july_2026_retrospective_v1
```

按 7 月事后场景采集，场景配置会唯一决定 bbox、起止时刻、时域和模式：

```bash
SCENARIO=murmansk_dikson_july_2026_retrospective_v1 make acquire-gfs
SCENARIO=murmansk_dikson_july_2026_retrospective_v1 make acquire-copernicus
SCENARIO=murmansk_dikson_july_2026_retrospective_v1 make acquire-static
```

冻结当前预报模板必须显式给锚点：

```bash
SCENARIO=murmansk_dikson_frozen_forecast_template_v1 \
SIMULATION_START=2026-08-12T00:00:00Z make acquire-gfs
```

不用共享场景时，仍可选择任意历史时间段，但四项都必须显式给出：

```bash
.mamba-env/bin/uv run --extra acquisition arctic-data acquire-forecast \
  --corridor offshore_murmansk_to_offshore_dikson \
  --start 2026-07-15T00:00:00Z --end 2026-07-22T00:00:00Z \
  --mode retrospective_best_estimate --sources gfs
```

历史 GFS 使用 NCEI 分析档案，Copernicus/GEBCO/EMODnet 使用各自可取得的历史或当前
版本化目录。它们共同形成“事后最佳估计”，不是严格的原始预测快照。

采集完成后先以对应模式回放并写 DatasetBundle，再绑定 RunContext。事后回放示例：

```bash
.mamba-env/bin/uv run arctic-data replay \
  --data-root data \
  --route-id offshore_murmansk_to_offshore_dikson \
  --at 2026-07-15T00:00:00Z \
  --mode retrospective_best_estimate \
  --knowledge-as-of 2026-08-12T14:00:00Z \
  --types wind_field temperature visibility wave ocean_current water_level \
          sea_ice_concentration sea_ice_type sea_ice_edge sea_ice_drift \
          sea_ice_thickness bathymetry land_sea_mask long_term_restricted_area \
  --horizon-hours 168 --minimum-horizon-hours 168 \
  --bundle-output data/output/bundles/murmansk-july-v1.json --summary-only

.mamba-env/bin/uv run --extra contracts arctic-data shared-scenario \
  --scenario murmansk_dikson_july_2026_retrospective_v1 \
  --dataset-bundle data/output/bundles/murmansk-july-v1.json \
  --run-context-output data/output/run-contexts/murmansk-july-v1.json
```

如果任何一层覆盖或 provenance 不完整，默认不会写 bundle。不要用
`--allow-incomplete` 生成正式联调输入。

## 9. 来源和已验证程度

实现依据和入口：

- [NOAA/NCEI GFS 历史档案说明](https://www.ncei.noaa.gov/products/weather-climate-models/global-forecast)
- [Copernicus 含潮北极海流产品](https://data.marine.copernicus.eu/product/ARCTIC_ANALYSISFORECAST_PHY_TIDE_002_015/description)
- [Copernicus neXtSIM 冰产品](https://data.marine.copernicus.eu/product/ARCTIC_ANALYSISFORECAST_PHY_ICE_002_011/description)
- [GEBCO 2026 网格](https://www.gebco.net/data-products-gridded-bathymetry-data/gebco2026-grid)
- [EMODnet Web Service 文档](https://emodnet.ec.europa.eu/en/emodnet-web-service-documentation)

2026-08-12 已真实联网 smoke：NCEI 单周期所需 byte-range、GEBCO 小区域、EMODnet
四类别查询，以及升级前同日 neXtSIM 路径的冰型/冰缘两小时。随后加入
`source_valid_mask.v2` 与单次下载复用，合同测试通过，但最新路径两次在 Toolbox 打开
数据集阶段失败，仍待源服务恢复后复验。上述证据不证明任意未来日期都覆盖，也不替代
完整长窗验收。0.3.1 的 9 类、168 h、1001 帧联合 bundle 仍作为历史基线保留。

## 10. 20 天/个人电脑的实施顺序

1. 先完成一个主航区、一个事后场景的 14 类完整 bundle 和 doctor；
2. B 先做确定性逐小时连续化、风险与置信度，不等待大模型训练；
3. C 先让全航程参考线跑通，再增量加入 24–72 h、0–24 h、0–6 h 三层；
4. 将同一 B/C 参数原样迁移到测试航区；
5. 最后冻结一次当前预报演示，并让 D 只展示同一 RunContext。

节省资源的具体措施包括：NCEI GRIB byte-range、Copernicus 总流下载前先选整点、冰型/
冰缘共用一次 neXtSIM 下载、静态层按走廊裁剪、所有场景时域有 216 h 上限。正式结果
仍须保存 snapshot、bundle、RunContext 和 doctor 证据，不能以节省时间为由补零或
跳过身份校验。
