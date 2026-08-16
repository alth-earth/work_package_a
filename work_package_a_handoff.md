> **文档治理声明**
>
> - 本文件角色：当前工作包 A 的人类与 AI 统一交接入口。
> - 改造时间：2026-08-15（Asia/Shanghai）。
> - 原文件去向：[work_package_a_handoff_归档_20260815.md](work_package_a_handoff_归档_20260815.md)。
> - 改造原因：落实 A“预下载、预处理、持久化、离线演示直接调用”的挑战杯定位。

# 工作包 A 项目交接

> Status: CURRENT — RC1（2026-08-16）。精确身份（bundle/RunContext/TOPAZ 版本）见
> `docs/DEMO_RC1_BASELINE_20260816.md`；执行历史见 `data/output/golden/EXECUTION_LOG_*.md`。

## 1. 项目目标与边界

A 是挑战杯系统唯一环境数据入口，负责：下载/接收、来源与时间留证、拆帧、规范化、质量检查、
不可变持久化、manifest、模拟回放和向 B 发布标准制品。

本项目不要求比赛现场下载最新数据。A 的核心交付是提前准备一套与场景时间窗匹配、可离线重复
读取的数据，例如冻结连续数日的数据；具体长度按主走廊 168 h、迁移走廊 96 h 物化。比赛时
B/C/D 直接调用 A 已标准化和持久化的制品。

A 不负责风险、目标风险网格、最终 hard-mask 决策、最终船速、ETA、路线或可视化。B/C/D 不得
绕过 A 公共接口扫描其 SQLite、raw、ready 或下载目录。

## 2. 挑战杯统一口径

- 成功标准是离线数据能稳定驱动 B 风险、C 航线和 D 展示；数据是否最新不作为验收项。
- 历史回放仍要求 `issue_time <= simulation_time`；稳定演示读取预置数据但保留版本和代次。
- 科学校准不阻塞 A；海洋/气象、冰情和来源质量接口继续保留。
- 必需来源/参数优先采用可追溯公开资料；无法取得时可透明降级并标明演示用途。
- 项目负责人拥有 A 的全部决策权，不再等待不存在的领域负责人或签字流程。

系统级口径以 [ARCTIC_ROUTE_SYSTEM.md](../ARCTIC_ROUTE_SYSTEM.md) 为准。

## 3. 当前状态

| 维度 | 状态 | 当前含义 |
|---|---|---|
| 工程实现 | 已完成 | 0.4.2；采集、摄取、归档、回放、bundle v2 和 exact resolver 已实现 |
| 工程门禁 | 已通过快照 | 2026-08-14：Ruff、lock/sync、CLI 与 172 tests 通过 |
| 稳定演示数据 | 已完成 | `tromso_isfjorden_august_2026_demo_v1`：12 类齐全、144 h、`complete=true`，bundle/RunContext 已生成并双备份 |
| 完整实源长窗 | 未完成 | 主走廊 12 类/168 h 仍待补；当前 tromso 144 h 冻结数据用于比赛演示 |
| 科学校准 | 非阻塞 | A 保留来源/QC接口，不声明导航安全 |

## 4. 已完成清单

| 功能 | 路径 |
|---|---|
| 14 类数据注册、变量/单位/方向/QC | `src/arctic_route_data/specs.py`、`normalization.py`、`derivations.py` |
| GFS/NCEI、Copernicus、GEBCO、EMODnet 入口 | `forecast_acquisition.py`、`static_acquisition.py` |
| issue/valid/ingest 时间与证据 | `issue_time.py`、`models.py`、`docs/ISSUE_TIME_POLICY.md` |
| 原子发布、拆帧与摄取 | `publisher.py`、`temporal_split.py`、`ingestion.py` |
| manifest、revision、doctor | `manifest.py`、`doctor.py`、`schemas/` |
| 模拟时钟、generation 与 AB cache | `clock.py`、`cache.py`、`service.py` |
| DatasetBundle v2 与 exact resolver | `bundle.py`、`service.py` |
| 共享场景/RunContext 适配 | `shared_context.py`、`../arctic_route_contracts/` |
| 冻结演示数据集（2026-08-15） | `../arctic_route_contracts/configs/scenarios/tromso_isfjorden_august_2026_demo_v1.toml`、`data/output/bundles/*.bundle.json`、`*.run-context.json`、`docs/FROZEN_DEMO_DATASET_DELIVERY.md` |
| 公共接口与验收口径（源自：work_package_a_handoff_归档_20260815.md） | `docs/AB_INTERFACE.md`、`docs/ARCHITECTURE_TRACE.md` |
| 旧 A 兼容边界（源自：work_package_a_handoff_归档_20260815.md） | `src/arctic_route_data/legacy.py`、`legacy_downloaders.py`、`docs/LEGACY_MIGRATION.md` |

## 5. 未完成与待办

### P0：挑战杯主线

1. ~~选定并冻结演示场景~~ 已完成：`tromso_isfjorden_august_2026_demo_v1`（144 h）。
2. ~~生成标准帧、bundle/RunContext 与 doctor~~ 已完成：12 类 `complete=true`，doctor
   `errors=[]`，制品双备份。
3. 用同一冻结制品实际驱动 B、C、D，保存一次初始计划和一次重规划所需数据。
4. 比赛前至少两次断网复跑；禁止依赖现场凭据或临时目录。

### P1：工程增强

- 主走廊 `offshore_murmansk_to_offshore_dikson` 的 12 类/168 h 真实长窗留作 P1 增强，
  不阻塞当前 tromso 演示主线。
- 记录数据源失败和显式后备，不把缺测补零或伪装安全。
- 改进 watcher heartbeat、调度、失败重试和长期来源报告。
- 与 B 固定目标网格/覆盖策略；A 继续保存原生网格。

### P2：仅保留接口

- bathymetry、法规区、科学 QC 和精细来源质量分级；
- 五类专业人员未来可替换/审核的字段与版本入口；
- 第二走廊完整实源验收。

上述 P2 不属于挑战杯演示完成条件。

工程增强对照（源自：work_package_a_handoff_归档_20260815.md，供后续主线上完后再做）：

- 为 `offshore_murmansk_to_offshore_dikson` 的明确共享场景完成恰好 12 类、168 h 真实
  采集/回放，并保存 DatasetBundle v2 + RunContext v2 + source snapshots + doctor；
- 每类完整窗和 provenance 均为 true，并通过跨进程 exact resolver；
- 继续 fail closed：缺测、future、过期、未知单位/方向、错走廊或覆盖不足不得补零当安全；
- 源服务可用时补 latest v2 neXtSIM/mask 与 total-with-tide 小窗真实发布证据；若回退
  detided 必须显式记录，二者禁止相加；
- 为多 watcher 的长任务增加 heartbeat/可续期 lease，或维持单一摄取 owner。

## 6. 技术架构与关键决策

```text
公开源/历史档案
      ↓ 下载与发布时间证据
source snapshot → raw → 拆帧/规范化/QC → ready + manifest
                                              ↓
                                   DatasetBundle v2 / RunContext
                                              ↓
                                              B
```

关键不变量：

1. `issue_time`、`valid_time`、`ingest_time` 均为 UTC 且不可互换。
2. 稳定演示可使用冻结历史数据；历史回放仍不得提前看到未来发布数据。
3. seek/reset 提升 `generation_id`，旧任务不得写入当前代次。
4. 发布制品不可变且可通过摘要恢复；缺测和未知不得伪装成零风险。
5. A 保留源网格，B 负责风险目标网格。
6. `source_valid_mask`、陆海、水深、法规和通航 hard mask 是不同语义。

补充关键决策（源自：work_package_a_handoff_归档_20260815.md）：

- `complete = covers_requested_window && provenance_complete`；达到最低时域不等于完整窗。
- 归档发布不可变，sidecar 与 payload 通过大小、SHA-256、publication 和来源证据绑定。
- v2 bundle 绑定所有实际记录、逐类型 coverage/provenance 和 payload 语义证明；v1 只读。
- 旧 A 允许按文件名/mtime/最近时次、部分失败继续、全变量 nearest 和 quality/data-age 占位
  的路径均为 `legacy_unverified`，不得进入 formal 主线。

## 7. 航线与数据窗

| 走廊 | 角色 | 端点 | 默认/允许时域 |
|---|---|---|---|
| `offshore_murmansk_to_offshore_dikson` | 先完成 | 69.15°N, 33.60°E → 73.55°N, 80.40°E | 168 h / 144–216 h |
| `tromso_to_isfjorden_outer` | 后迁移；已交付 144 h 冻结演示数据 | 69.75°N, 19.00°E → 78.15°N, 13.00°E | 96 h / 72–144 h |

详细允许区域、朗伊尔城 AIS 参考点和栅格修正规则见
[SCENARIOS_AND_SOURCES.md](docs/SCENARIOS_AND_SOURCES.md)。旧 `tromso_to_svalbard` 只能用于历史审计。

## 8. 数据与位置

| 内容 | 路径/说明 |
|---|---|
| 配置 | `configs/work_package_a.toml` |
| 来源证据示例（源自：work_package_a_handoff_归档_20260815.md） | `configs/source_release.example.toml` |
| source snapshots | `data/source_snapshots/`，运行数据 |
| incoming/raw/ready | `data/incoming/`、`data/raw/`、`data/ready/` |
| manifest | `data/manifest/manifest.sqlite3` |
| quarantine（源自：work_package_a_handoff_归档_20260815.md） | `data/quarantine/` |
| bundle/RunContext/报告 | `data/output/` |
| Schema | `schemas/` |
| 公共包 | `src/arctic_route_data/` |

下载数据、缓存、凭据和运行输出不进 Git。演示冻结制品必须有清晰保管位置和恢复说明。

场景采集命令（源自：work_package_a_handoff_归档_20260815.md；先读
[SCENARIOS_AND_SOURCES.md](docs/SCENARIOS_AND_SOURCES.md)）：

```bash
SCENARIO=murmansk_dikson_july_2026_retrospective_v1 make acquire-gfs
SCENARIO=murmansk_dikson_july_2026_retrospective_v1 make acquire-copernicus
SCENARIO=murmansk_dikson_july_2026_retrospective_v1 make acquire-land-sea-mask
```

水深和限制区用独立可选目标采集，不得混入恰好 12 类的主线 bundle。

正式制品验收（源自：work_package_a_handoff_归档_20260815.md，实源主线补全后执行）：

- 使用场景精确 UTC 起止、corridor、模式和 knowledge cutoff；
- 12 类逐项覆盖完整且 provenance 可在磁盘独立复核；
- bundle v2、RunContext v2、generation 和 config digest 一致；
- exact resolver 不扫描下游私有目录且恢复完全相同帧；
- doctor 对 ready/raw/sidecar/snapshot 零错误；
- 输出明确标注数据模式、证据等级和 `navigation_use=prohibited`。

## 9. 工程演示验收

```bash
cd /root/my_project/work_package_a
make check
make doctor
```

挑战杯验收重点：冻结数据能无网读取；时间、走廊、变量和单位一致；B 能消费；模拟推进时不会
显示旧 generation；失败有清楚报告。完整科学 QC、最新数据和全套真实长窗不作为阻塞门槛。

## 10. 已知风险

- 主走廊 12 类/168 h 真实长窗证据仍未形成（tromso 144 h 冻结演示数据已交付）；
- 外部源、凭据、目录和产品会变化；
- 本地旧 v1/9 类数据不得冒充新正式制品；
- watcher 尚非成熟分布式队列；
- bathymetry/法规层没有资格自动成为 hard mask。

补充（源自：work_package_a_handoff_归档_20260815.md）：

- 当前本地历史 manifest/bundle 主要是旧走廊、v1、9 类证据，不能创建正式 RunContext；
- 新来源“小窗可下载”不等于指定场景的完整 168 h 可用；
- 保守 retrieval gate 能阻止未来泄漏，但不能用于精确统计生产者发布延迟；
- Copernicus 凭据、产品目录和服务可用性会变化；失败必须保留原始错误和请求证据；
- `FolderWatchSource.scan_once()` 适合单一 owner，不是成熟的分布式队列；
- 172 tests 是 2026-08-14 工程证据，不应被写成永久测试数量；
- `.env.copernicus` 含本地凭据且被 Git 忽略，不得复制进文档、fixture 或终端日志。

这些风险继续记录，但不推翻“预置可用数据完成挑战杯演示”的主线。

## 11. 相关入口

- [A README](README.md)
- [场景与来源](docs/SCENARIOS_AND_SOURCES.md)
- [A→B 接口](docs/AB_INTERFACE.md)
- [时间政策](docs/ISSUE_TIME_POLICY.md)
- [跨包边界](docs/BCD_HANDOFF.md)
- [系统权威](../ARCTIC_ROUTE_SYSTEM.md)
- [十日计划](../ABC_10_DAY_SPRINT.md)
- [冻结演示数据集交付说明](docs/FROZEN_DEMO_DATASET_DELIVERY.md)

Git 提交与同步由项目负责人在本会话结束后手动执行，本 handoff 不再主动提出提交/推送建议。
