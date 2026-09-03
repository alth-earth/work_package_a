# 工作包 A 架构追踪与验收口径（v0.4.2）

本文只追踪 A。共享事实真源在 `arctic_route_contracts`；正式 B 实现在
`work_package_b/`；BC/CD 的合同真源在工作包 C。`work_package_b_handoff/` 保留设计约束和
旧 ZIP 审计，不再代表 B 尚未建立。

## 1. 场景、采集和时间

| 要求 | 实现 | 主要验证 |
|---|---|---|
| 禁止 implicit latest | corridor 模式强制 start + end/horizon + mode；共享模式从 Scenario 唯一解析 | CLI 正反测试 |
| 双历史语义不混称 | `AcquisitionMode`、共享 `ScenarioMode` | frozen/retrospective 冲突与模板物化测试 |
| 7 月事后回放允许后补档案 | simulation clock 与 `knowledge_as_of` 分离 | 因果/事后/倒退/seek 测试 |
| 动态航程窗 | 共享 `HorizonPolicy` + `recommend-horizon`；最小缓冲 48 h；默认 168/96 h、上限 216/144 h | shared contracts CLI/cap/fail + A shared-scenario 测试 |
| 两条航区事实唯一 | `arctic_route_contracts/configs/corridors`；A 只适配 `route_id` | shared-scenario CLI 与冲突测试 |
| GFS 未来窗 | NOMADS cycle/lead + 完整末端支撑 | 0.3.1 真实 168 h + 合同测试 |
| GFS 历史窗节省传输 | NCEI 6 h analysis、`.inv`、严格 HTTP Range；OA 入口失败时使用官方 THREDDS FileServer 且记录原因 | 2026-07-15 单周期真实 937,236 B smoke；两入口 Range 206/忽略 Range 拒绝测试 |
| Copernicus 凭据安全 | 严格 dotenv parser、mode 600、不 shell-source | 空/半配置/非法键/权限测试 |
| CARRA 凭据与历史窗 | 外部绝对 `.cdsapirc`、临时 `CDSAPI_RC`、UTC 3 h 边界、最大 216 h、East-domain 覆盖 | 环境恢复、权限、窗口、缓存损坏和发布测试；实源 smoke 单独记录 |
| 三时间 UTC 且防未来 | models/manifest/source/service | issue/valid/ingest、future 和 revision 测试 |
| 精确来源证据 | snapshot、request metadata、checksum/byte ranges | publisher、archive、doctor 测试 |
| CARRA 原始缓存 | 请求摘要 + size + SHA-256；损坏隔离；`.part` 原子下载并绑定 source snapshot | 缓存命中/损坏重取/正式证据路径测试 |

## 2. 14 类注册和新增来源

| 要求 | 实现 | 当前证据等级 |
|---|---|---|
| 14 类环境注册 | `DATA_TYPE_SPECS` 增加正式 `land_sea_mask` | registry/config/unit 测试 |
| 船舶不混入 A | 独立共享 `VesselProfile` | shared contracts 合同测试 |
| 冰型未来数据 | neXtSIM 三个浓度分量，A 确定性 dominant-class v1 | 升级前同日路径 2026-07-15 两小时真实发布；最新 v2 路径待源服务复验 |
| 冰缘 | `siconc >= 0.15` 的冰侧四邻域边界 | 同上；与冰型共用一次下载合同测试 |
| 含潮总流优先 | TIDE 15 min 源在 load 前严格抽整点 | 选择测试；detided fallback/never-sum 测试 |
| 水深 | GEBCO 2026 OPeNDAP，elevation positive-up | 小区域真实发布 |
| 正式陆海分类 | 同 GEBCO 网格 `elevation < 0` 派生 | 小区域真实发布；明确非 navigability |
| 法律图层分义 | EMODnet MPA/military/MSP/Natura2000 分类别注释 | 四层真实查询，小区域正式发布；全空拒绝测试 |

旧 ZIP 的 14 个文件夹是 13 类环境 + 船舶 CSV，不是 A 应维护 14 个相同语义的
下载器。0.3.1 的“9 类”只代表当时完成真实长窗联合验收的数量。

## 3. 规范化与内容 QC

| 要求 | 实现 | 主要验证 |
|---|---|---|
| CF 别名与规范变量 | `specs.py` | normalization/legacy 集成测试 |
| 单位白名单与物理范围 | `normalization.py` | 单位、NaN/inf、范围测试 |
| 风/流/冰漂真东/真北 | 矢量证据优先、明确 projected 才旋转 | double-rotation/ambiguous 拒绝测试 |
| 波向语义 | from true north clockwise | circular/方向证据测试 |
| 水深语义 | height above MSL、positive-up | up/down/冲突/猜测拒绝测试 |
| Copernicus 结构域 | `a.source-valid-mask.v2` 要求所有必需变量有有限域；v1 只读兼容 | 多变量差异域测试、snapshot 精确绑定 |
| 正式陆海事实 | 独立 `land_sea_mask` | 不允许 source-valid 替代 |
| 网格身份 | grid ID/digest/topology | rectilinear/curvilinear/unstructured 测试 |
| 限制区安全语义 | `navigation_effect=information`、`automatic_hard_mask_allowed=false` | GeoJSON/类别/空结果测试 |

## 4. 发布、归档和 manifest

| 要求 | 实现 | 主要验证 |
|---|---|---|
| 原子发布 | `.part → payload → sidecar` | publisher 集成测试 |
| payload/sidecar 精确绑定 | checksum、size、publication ID | 错配拒绝测试 |
| 不可变历史 | content-addressed ready + manifest revision | 冲突/恢复/迁移测试 |
| 路径不可逃逸 | identifier 与 resolved-path 校验 | traversal 测试 |
| watcher 恢复 | claim、quarantine、stale-claim | folder-watch 测试 |
| doctor 验证真实证据 | ready/raw/sidecar/snapshot 路径与 SHA-256 | archive doctor 测试 |

## 5. AB 缓存、回放和身份

| 要求 | 实现 | 主要验证 |
|---|---|---|
| 航区隔离 | `(route_id,data_type,variable)` 分区 | 两 route 不串帧 |
| revision 选择 | quality → issue → ingest → stable tie-break | manifest/cache 测试 |
| 双时钟门禁 | cache put 区分 simulation/knowledge；同代次知识不可倒退 | retrospective/seek 回归测试 |
| 动态目标窗 | 调用方显式 target/minimum；`CoverageReport` 分项 | start/end/gap/static/event 测试 |
| 完整状态 | `covers_requested_window && provenance_complete` | minimum-only、自报 provenance 拒绝 |
| 精确 A 输入 | `PreparedWindow + DatasetBundle.v2` | records/provenance/coverage digest、cadence/support/gaps 与逐 payload 语义证明独立复核；v1 只读 |
| 跨进程精确恢复 | `resolve_dataset_bundle_for_b()` | bundle 重验、按 data ID 精确解析、payload/provenance 复核、深快照/attestation、重建身份、generation/time 围栏 |
| seek/generation | reset generation、旧任务迟到拒绝 | tick/seek 竞态测试 |
| 内存边界 | static/slow/dynamic/event 分区、lease | limit/lease/expiry 测试 |

`PreparedWindow.as_of_time` 的字段名为 v1 兼容保留；从 0.4.1 起它表示
`knowledge_as_of`。因果模式下等于模拟时钟，事后模式下可以显式更晚。

## 6. 共享配置和跨包身份

| 要求 | 当前实现 |
|---|---|
| 场景/走廊/船舶唯一事实 | 独立 `arctic_route_contracts` frozen models + TOML + Schema |
| A 映射 | `shared_context.py` 将共享 corridor 映射到 `ManifestRecord.route_id` |
| 冻结模板 | 必须显式 `simulation_start`，不能 latest |
| A bundle 绑定运行 | `shared-scenario` 可创建不可覆盖的 `run-context.v2` |
| 运行态代次/时间 | 当前 clock snapshot 不塞入 RunContext；由编排/B 输入信封显式携带 generation、current simulation time、knowledge cutoff 和完整 bundle 文档 |
| 摘要分权 | 公共 `config_digest`；B `model_config_digest`；C `planner_config_digest` |
| 下游围栏 | RiskFrame/RoutePlan v2 传播 run/scenario/corridor/vessel/config/generation |

## 7. 真实验收层级与当前结论

验收报告必须区分：

1. 代码存在；
2. fixture/合同测试通过；
3. 指定日期/区域真实源 smoke；
4. 指定场景完整长窗 bundle + doctor；
5. 持续调度、重试和监控。

当前证据：

- 0.3.1 历史基线达到第 4 级：9 类、156 h replay、1001 帧，bundle
  `a-bundle-c8b2c039c50f92086e3953e6`，当时 doctor 1419 条零错误；
- 0.4.0 新来源达到第 3 级：NCEI byte-range、GEBCO、EMODnet、neXtSIM 冰型/冰缘；
  其中 `source-valid-mask.v2` 与冰型/冰缘单次下载复用是在实源成功后升级，最新路径
  合同测试已过，但两次复验在 Toolbox 打开数据集阶段空异常，仍待补实源 smoke；
- 主航区恰好 12 类的 168 h 主线仍未达到第 4 级，禁止把多个 smoke 或两类可选层
  拼成正式完整 bundle；`bathymetry`、`long_term_restricted_area` 独立报告且不阻塞；
- B `0.1.0` 工程基线已用正式公共接口完成 12 类、96/168/216 h 夹具验收，并有
  A 归档发布→重启→exact resolver→B 的跨进程篡改负例；这些属于第 2 级工程证据，
  不是指定场景真实源验收。
- 当前真实长窗仍是历史 v1/9 类/旧 corridor，无法创建正式 RunContext；因此
  A→B→C 真实端到端验收仍未完成。

## 8. A/B/C/D 边界

A 只发布环境标准帧、来源证据、DatasetBundle，并适配共享 RunContext。B 负责目标
网格、逐小时连续化、风险、置信度、硬约束规则和环境速度因子；C 负责最终船速、ETA、
导师提出的四层路线和重规划；D 只读展示。

具体当前字段不要从本文复制，见：

- `docs/AB_INTERFACE.md`
- `arctic_route_contracts/schemas/run-context-v2.schema.json`
- `work_package_c/docs/BC_CONTRACT.md`
- `work_package_c/docs/CD_CONTRACT.md`
- `work_package_b/README.md`
- `work_package_b_handoff/工作包B-v2正式开发交接书.md`
