> **文档治理声明**
>
> - 本文件角色：当前工作包 A 短入口。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 原文归档：[README.archive-20260814-pre-governance.md](README.archive-20260814-pre-governance.md)。
> - 改造原因：把快速导航与详细状态、历史验收和操作手册分离，降低人和 AI 误读旧状态的风险。

# 北极航线预测驱动动态规划系统：工作包 A

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
cd /root/my_project/work_package_a
make check
make doctor
```

`make check` 证明工程门禁，不证明真实 12 类长窗、科学有效性或导航适用性。正式交付必须
另行保存 DatasetBundle、RunContext、source snapshot 和 doctor 证据。

## 航道通行情况可选层

工作包 A 已补充 `vessel_traffic` 可选动态数据层，用于接收航道通行情况模拟模型生成的
NetCDF 文件，并将其统一导入 A 既有的 `ready/manifest` 数据体系。该层不改变原有 12 类
必需环境数据，不参与 A 主线完整性判定；当该层存在时，工作包 B 可将
`vessel_traffic_risk` 作为综合风险模型中的通航拥挤、邻近船舶干扰和航道活跃程度输入。

由于两条北极研究航线的连续历史 AIS 通航轨迹难以直接开放获取，部分历史 AIS 接口需要额外
授权，公开资料又多以年度报告、航次统计或区域摘要形式发布，因此项目采用“真实环境数据 +
通航情况模拟层”的方式补齐 B 侧需要的动态通航风险输入。导入方法见
[航道通行情况模拟数据接入说明](docs/VESSEL_TRAFFIC_INTEGRATION.md)。
