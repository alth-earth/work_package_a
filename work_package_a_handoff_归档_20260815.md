> **二次文档治理归档声明**
>
> - 本文件角色：2026-08-15 改造前的 A handoff 快照，仅供历史追溯。
> - 归档时间：2026-08-15（Asia/Shanghai）。
> - 现行文件：[work_package_a_handoff.md](work_package_a_handoff.md)。
> - 归档原因：落实 A“预下载、预处理、持久化、演示时离线调用”的挑战杯定位。
>
> <!-- ORIGINAL CONTENT START -->

> **文档治理声明**
>
> - 本文件角色：当前工作包 A 的人类与 AI 统一交接入口。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 原文归档：[README.archive-20260814-pre-governance.md](README.archive-20260814-pre-governance.md)；旧混合版本说明见 [CHANGELOG.archive-20260814-pre-governance.md](CHANGELOG.archive-20260814-pre-governance.md)。
> - 改造原因：以统一模板集中当前状态、已完成能力、优先级、风险、操作和验收，避免从历史运行段落推断现状。

# 工作包 A 项目交接

## 1. 项目目标与边界

A 是正式环境数据的唯一入口，负责：

- 从已配置公开网站/API 或历史档案获取数据并保存请求/发布时间证据；
- 把多时次文件拆为标准单时次帧，规范变量、单位、坐标、方向和质量；
- 通过 raw/ready/source snapshot/SQLite manifest 保存不可变、可 doctor 的证据链；
- 用 SimulationClock、knowledge cutoff 和 generation 实现无未来信息泄漏的回放；
- 向 B 提供公共 `PreparedWindow`、`StandardDataFrame`、`DatasetBundle v2` 和精确恢复入口。

A 不负责目标风险网格、风险/置信度、hard-mask 政策、最终船速、ETA、路线或展示。B/C/D
不得绕过 A 私有 SQLite、raw、ready 或下载目录补造正式输入。

## 2. 当前状态

| 维度 | 状态 | 截止 2026-08-14 的准确含义 |
|---|---|---|
| 工程实现 | 已完成 | 版本 0.4.2；采集、摄取、归档、回放、bundle v2 和 exact resolver 已实现 |
| 工程验收 | 已完成 | 标准 `make check`：Ruff、lock/sync、CLI 通过，pytest 172 passed |
| 实源主线 | 进行中 | 新来源有小窗 smoke；主走廊真实 12 类、168 h 正式制品未完成 |
| 跨包验收 | 待评审 | 夹具链已通过；真实 A→B→C 端到端未完成 |
| 科学校准 | 未完成/不适用 | A 保存环境事实，不声明风险或导航安全 |
| 文档治理 | 已完成 | 短入口、纯 CHANGELOG、稳定场景文档和本 handoff 已建立 |

## 3. 已完成清单

| 功能 | 对应路径 |
|---|---|
| 14 类环境注册、变量/单位/方向/QC 规范 | `src/arctic_route_data/specs.py`、`normalization.py`、`derivations.py` |
| GFS/NCEI、Copernicus、GEBCO、EMODnet 采集入口 | `src/arctic_route_data/forecast_acquisition.py`、`src/arctic_route_data/static_acquisition.py` |
| issue-time 证据与三时间 UTC | `src/arctic_route_data/issue_time.py`、`src/arctic_route_data/models.py`、`docs/ISSUE_TIME_POLICY.md` |
| 原子 payload/sidecar 发布与拆帧摄取 | `src/arctic_route_data/publisher.py`、`src/arctic_route_data/temporal_split.py`、`src/arctic_route_data/ingestion.py` |
| 不可变 manifest/revision 与 doctor | `src/arctic_route_data/manifest.py`、`src/arctic_route_data/doctor.py`、`schemas/incoming-sidecar.schema.json` |
| 模拟时钟、generation、分区 AB cache | `src/arctic_route_data/clock.py`、`src/arctic_route_data/cache.py`、`src/arctic_route_data/service.py` |
| DatasetBundle v2、payload attestation、精确恢复 | `src/arctic_route_data/bundle.py`、`src/arctic_route_data/service.py`、`schemas/dataset-bundle-v2.schema.json` |
| 共享场景/RunContext 适配 | `src/arctic_route_data/shared_context.py`、`../arctic_route_contracts/` |
| 公共接口与验收口径 | `docs/AB_INTERFACE.md`、`docs/ARCHITECTURE_TRACE.md` |
| 旧 A 兼容边界 | `src/arctic_route_data/legacy.py`、`src/arctic_route_data/legacy_downloaders.py`、`docs/LEGACY_MIGRATION.md` |

## 4. 未完成与待办

### P0

- 为 `offshore_murmansk_to_offshore_dikson` 的一个明确共享场景完成恰好 12 类、168 h
  真实采集/回放。
- 保存并复核 `DatasetBundle v2 + RunContext v2 + source snapshots + doctor`；每类完整窗和
  provenance 均为 true，并通过跨进程 exact resolver。
- 用上述同一 RunContext 驱动 B committed window 和 C v2/v3；fixture 不得替代实源验收。
- 继续 fail closed：缺测、future、过期、未知单位/方向、错走廊或覆盖不足不得补零当安全。

### P1

- 在源服务可用时补 latest v2 neXtSIM/mask 与 total-with-tide 小窗真实发布证据；若回退
  detided，必须显式记录，二者禁止相加。
- 与 B 冻结目标网格、覆盖检查和按类型插值；A 继续保留源网格，不在本包制造统一伪精度。
- 为多 watcher 的长任务增加 heartbeat/可续期 lease，或维持单一摄取 owner。
- 增加持续调度、失败重试、阶段心跳和长期来源可用性报告。

### P2

- bathymetry 在取得可信吃水、潮位、净空和不确定性前仅保留研究接口。
- 法律图层保持类别与来源证据，待政策负责人版本化判定 hard/soft/information。
- 对第二走廊和不同冻结场景分别执行真实覆盖验收；不能外推一次 smoke。

## 5. 技术架构与关键决策

```text
公开源/历史档案
      │ 请求与发布时间证据
      ▼
source_snapshots → incoming payload+sidecar
      │
      ▼
raw 不可变归档 → 拆帧/规范化/QC → ready 内容寻址帧
                                      │
                                  SQLite manifest
                                      │ SimulationClock
                                      ▼
                         PreparedWindow + DatasetBundle v2
                                      │ public API / exact resolver
                                      ▼
                                      B
```

关键决定：

1. `issue_time`、`valid_time`、`ingest_time` 全部为带时区 UTC，语义不能互换。
2. `complete = covers_requested_window && provenance_complete`；达到最低时域不等于完整窗。
3. 归档发布不可变，sidecar 与 payload 通过大小、SHA-256、publication 和来源证据绑定。
4. seek 提升 generation；迟到旧代次任务不得污染新运行。
5. v2 bundle 绑定所有实际记录、逐类型 coverage/provenance 和 payload 语义证明；v1 只读。
6. A 保留源网格；B 负责目标风险网格和插值。
7. source-valid、land-sea、bathymetry、法律限制和通航 hard mask 是不同语义。

与历史架构的主要差异：旧 A 允许按文件名/mtime/最近时次、部分失败继续、全变量 nearest
和 quality/data-age 占位；这些路径均为 `legacy_unverified`，不得进入 formal 主线。

## 6. 已知问题、坑与风险

- 当前本地历史 manifest/bundle 主要是旧走廊、v1、9 类证据，不能创建正式 RunContext。
- 新来源“小窗可下载”不等于指定场景的完整 168 h 可用。
- 保守 retrieval gate 能阻止未来泄漏，但不能用于精确统计生产者发布延迟。
- Copernicus 凭据、产品目录和服务可用性会变化；失败必须保留原始错误和请求证据。
- `source_valid_mask` 不是陆海或通航 mask；法律图层名称也不是自动禁航依据。
- `FolderWatchSource.scan_once()` 当前适合单一 owner，不是成熟的分布式队列。
- 172 tests 是 2026-08-14 工程证据，不应被写成永久测试数量。
- `.env.copernicus` 含本地凭据且被 Git 忽略；不得复制进文档、fixture 或终端日志。

## 7. 数据、配置与模型位置

| 内容 | 路径/说明 |
|---|---|
| A 配置 | `configs/work_package_a.toml` |
| 来源证据示例 | `configs/source_release.example.toml` |
| source snapshots | `data/source_snapshots/`，运行数据，不进 Git |
| incoming/raw/ready | `data/incoming/`、`data/raw/`、`data/ready/` |
| manifest | `data/manifest/manifest.sqlite3` |
| quarantine | `data/quarantine/` |
| bundle/RunContext/报告 | `data/output/`，运行制品，不进 Git |
| Schema | `schemas/` |
| 公共 Python 包 | `src/arctic_route_data/` |

A 没有风险模型或规划模型权重。旧 ZIP、历史 v1 bundle 和 ignored data 只能作为审计/迁移
输入，不能因“文件存在”升级为正式现状。

## 8. 操作与验收

### 工程门禁

```bash
cd /root/my_project/work_package_a
make check
make doctor
```

截至 2026-08-14，`make check` 为 172 tests passed；每次改动仍应重新运行，不能沿用该数字。

### 场景与采集

先读取 [SCENARIOS_AND_SOURCES.md](docs/SCENARIOS_AND_SOURCES.md)，再使用共享场景：

```bash
SCENARIO=murmansk_dikson_july_2026_retrospective_v1 make acquire-gfs
SCENARIO=murmansk_dikson_july_2026_retrospective_v1 make acquire-copernicus
SCENARIO=murmansk_dikson_july_2026_retrospective_v1 make acquire-land-sea-mask
```

水深和限制区用独立可选目标采集，不得混入恰好 12 类的主线 bundle。

### 正式制品验收

- 使用场景精确 UTC 起止、corridor、模式和 knowledge cutoff；
- 12 类逐项覆盖完整且 provenance 可在磁盘独立复核；
- bundle v2、RunContext v2、generation 和 config digest 一致；
- exact resolver 不扫描下游私有目录且恢复完全相同帧；
- doctor 对 ready/raw/sidecar/snapshot 零错误；
- 输出明确标注数据模式、证据等级和 `navigation_use=prohibited`。

## 9. 下一步计划与建议

1. 先完成一个主走廊真实 12 类基线，不并行扩张可选层或第二走廊。
2. 固化 bundle、RunContext、doctor 和 exact resolver 证据后再启动 B/C 长联调。
3. 将 Copernicus 复验、B 网格和 watcher 改进作为独立 P1，不篡改 P0 的完成定义。
4. A 的任何接口/Schema 变更必须同步代码、测试、AB 文档和共享合同版本。

## 10. 顶层与相关文档索引

- 当前短入口：[README.md](README.md)
- 治理前 README：[README.archive-20260814-pre-governance.md](README.archive-20260814-pre-governance.md)
- 当前版本记录：[CHANGELOG.md](CHANGELOG.md)
- 治理前混合版本记录：[CHANGELOG.archive-20260814-pre-governance.md](CHANGELOG.archive-20260814-pre-governance.md)
- 场景与来源：[docs/SCENARIOS_AND_SOURCES.md](docs/SCENARIOS_AND_SOURCES.md)
- A→B 接口：[docs/AB_INTERFACE.md](docs/AB_INTERFACE.md)
- 时间政策：[docs/ISSUE_TIME_POLICY.md](docs/ISSUE_TIME_POLICY.md)
- 架构/证据追踪：[docs/ARCHITECTURE_TRACE.md](docs/ARCHITECTURE_TRACE.md)
- 跨包边界：[docs/BCD_HANDOFF.md](docs/BCD_HANDOFF.md)
- 旧 A 迁移：[docs/LEGACY_MIGRATION.md](docs/LEGACY_MIGRATION.md)
- 历史修复报告：[docs/A_REPAIR_REPORT.md](docs/A_REPAIR_REPORT.md)
- 共享契约交接：[arctic_route_contracts_handoff.md](../arctic_route_contracts/arctic_route_contracts_handoff.md)
- 编排器交接：[arctic_route_orchestrator_handoff.md](../arctic_route_orchestrator/arctic_route_orchestrator_handoff.md)
- 系统权威：[ARCTIC_ROUTE_SYSTEM.md](../ARCTIC_ROUTE_SYSTEM.md)
- 当前冲刺：[ABC_10_DAY_SPRINT.md](../ABC_10_DAY_SPRINT.md)
- 梳理报告：[项目梳理报告.md](../项目梳理报告.md)
