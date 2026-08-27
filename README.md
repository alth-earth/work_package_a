---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
  - PLANNED
Document Role: CANONICAL
Scope: work package A entrypoint and public boundary
Branch: research-validation-system
Last Verified: 2026-08-22
---

> **文档治理声明**
>
> - 本文件角色：当前工作包 A 短入口。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 原文归档：[README.archive-20260814-pre-governance.md](README.archive-20260814-pre-governance.md)。
> - 改造原因：把快速导航与详细状态、历史验收和操作手册分离，降低人和 AI 误读旧状态的风险。

> **路径约定（2026-08-24）**：本文件中 `${ARCTIC_ROUTE_ROOT}` 为工作区根占位符，
> 指向包含各工作包目录（`arctic_route_contracts/`、`work_package_a/` 等）的公共根。
> 解析优先级：环境变量 > 当前所在目录 > `$HOME`。完整定义见
> `arctic_route_governance/README.md` 的"路径约定"章节。

# 北极航线预测驱动动态规划系统：工作包 A

## Research Validation 定位（2026-08-21 23:18）

A 的阶段角色为 Environmental Data Acquisition。既有 `PreparedWindow` /
`DatasetBundle.v2` 和 provenance 边界保持兼容；下一研究目标是建立真实冬季 12-type
artifact。当前只有夏季正式证据，不能把通用海冰采集接口写成冬季验证已完成。

Winter research has the February 2026 scenario plus eight newly acquired,
provenance-complete Copernicus data types. The cached static land/sea mask also
passes the explicit retrospective coverage diagnostic, so 9/12 rows are ready.
Wind, temperature and visibility remain unpublished. The exact February NCEI
direct paths are absent, while Round4 official-catalogue validation identifies
C3S CARRA as a 3-hour, three-variable candidate pending source-policy approval,
CDS credentials/terms, and a projection-aware wind-vector adapter. A's formal
bundle already accepts record-declared 3 h or 6 h meteorology; the previous
“6 h versus 3 h gate” wording was too broad. No winter `DatasetBundle.v2` has
been persisted; status is `PARTIAL / BLOCKED_WITH_DECISION`.

> 2026-08-18：新增 `src/arctic_route_data/causal_replay.py`（SourceRecord 全
> revision 身份 + 可见性/支撑扫描），供 orchestrator `causal_replay_preflight.py`
> 与 `causal_replay_mvp.py` 使用；A 生产路径无改动，RC1/RC2 frozen 不变。

工作包 A 是全系统唯一的环境数据入口，负责下载/接收、来源留证、时间拆帧、规范化、
质检、不可变归档、manifest、模拟回放和向 B 发布 `DatasetBundle v2`。当前版本为
`0.4.2`。

## 当前状态

- 工程基线：已实现（2026-08-14 `make check`：Ruff、lock/sync、CLI 通过，172 passed）。
- RC1 实源状态（2026-08-16）：主走廊 `murmansk_dikson_august_2026_demo_v1` 12 类齐全、
  144 h、`complete=true`；TOPAZ 5 类由 `originalGrid` 原生曲网格重建
  （`cmems-origg-97062ef099c4`，20 km 阈值）；bundle `a-bundle-32cafad4…`、
  RunContext run …0b0005、corridor 2.2.0。
- 跨包状态：真实 A→B→C→D 端到端已由 orchestrator r6/r7 跑通（Demo RC1）。
- 历史：`tromso_isfjorden_august_2026_demo_v1` 仍保留为独立场景证据。
- 使用边界：科研演示；不生成风险、最终船速或路线，不得用于真实导航。

## 接手顺序

1. [工作包 A 项目交接](work_package_a_handoff.md)
2. [场景、走廊与来源](docs/SCENARIOS_AND_SOURCES.md)
3. [A→B 接口真值](docs/AB_INTERFACE.md)
4. [`issue_time` 政策](docs/ISSUE_TIME_POLICY.md)
5. [架构追踪与证据等级](docs/ARCHITECTURE_TRACE.md)
6. AI 修改前阅读 [AGENTS.md](AGENTS.md)

完整历史原文见
[治理前 README](README.archive-20260814-pre-governance.md)；旧 A 下载器的使用边界见
[LEGACY_MIGRATION.md](docs/LEGACY_MIGRATION.md)。

## 快速校验

```bash
cd ${ARCTIC_ROUTE_ROOT}/work_package_a
make check
make doctor
```

`make check` 证明工程门禁，不证明真实 12 类长窗、科学有效性或导航适用性。正式交付必须
另行保存 DatasetBundle、RunContext、source snapshot 和 doctor 证据。


### Vessel traffic simulation layer

This branch adds a generated `vessel_traffic` layer for the two study corridors. It is used when complete historical AIS traffic data cannot be redistributed or fetched with ordinary permissions. The layer reads calibrated parameters from `configs/vessel_traffic_model.toml`, generates the latest 144 hours at a 3-hour cadence, and exposes `traffic_density`, `traffic_count`, `traffic_risk` and `traffic_confidence` through the same Work Package A source contract used by the existing environmental datasets.

The traffic layer is designed as an A-to-B handoff feature: Work Package B can request it as `vessel_traffic` together with the other dynamic and static factors, while existing A-package data collection and replay behaviour remains unchanged for all other data types.
