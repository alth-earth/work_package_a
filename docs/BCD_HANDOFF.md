# B/C/D 接手契约与开发清单

本文用于后续开发者和 AI Agent 在不破坏工作包 A 时间语义的前提下继续实现 B、C、D。

状态说明：A→B 对象已经在代码中实现；下文的 BC/CD 对象是依据架构设计整理出的建议 `v1` 契约，尚未在本仓库实现。开始 B/C 开发时应把它们固化为 dataclass/Protocol、序列化 Schema 和合同测试。

## 1. 模块依赖方向

```text
A --StandardDataFrame--> B --RiskFrame--> C --RoutePlan--> D
│                       │               │               │
└─ 环境数据和来源证据    └─ 风险与置信度  └─ 路线与指标  └─ 只渲染
```

禁止反向调用：D 不调用 C 的求解器，C 不调用 B 的内部模型，B 不直接扫描 A 的 `incoming/raw`。模块之间只通过稳定对象和缓存接口通信。

## 2. 全链路共同语义

| 字段 | 规则 |
|---|---|
| `route_id` | A 中的数据裁剪/航线标识；必须保留到 B 的来源摘要 |
| `scenario_id` | 一次完整演示或试验的标识；A 当前未显式建模，由 B 任务创建并传播到 BC/CD |
| `generation_id` | 模拟跳转代次；任何模块都不得发布或显示旧代次迟到结果 |
| `valid_time` | 环境帧或风险帧所描述的模型时间 |
| `as_of_time` | 本次计算允许知道的数据截止时刻；所有输入 `issue_time <= as_of_time` |
| `generated_at` | 真实计算完成时间，仅用于性能和审计 |
| `schema_version` | 接口结构版本，例如 `bc.risk-frame.v1` |
| `model_version` | 算法和参数版本；与 `schema_version` 不同 |

所有时间使用 UTC ISO-8601；Python 对象使用带时区的 `datetime`。

## 3. A → B：已实现契约

### 3.1 输入对象

```python
@dataclass(frozen=True)
class StandardDataFrame:
    record: ManifestRecord
    payload: xarray.Dataset | dict
    generation_id: int
```

其中 `payload` 已经完成：

- 单 `valid_time` 拆分；
- 变量名和常用单位统一；
- 坐标清洗；
- 质量、来源和 SHA-256 登记；
- `issue_time <= as_of_time` 检查。

### 3.2 B 应调用的入口

| 入口 | 用途 |
|---|---|
| `WorkPackageA.prefetch(...)` | 围绕模拟时刻把指定类型加载到 AB |
| `WorkPackageA.latest_for_b(data_type)` | 读取某类最新缓存帧 |
| `WorkPackageA.window_for_b(...)` | 读取按 `valid_time` 排序的窗口 |
| `DataSource.get_bracketing(...)` | 获取目标时刻前后帧，用于插值 |
| `cache.lease(data_id)` | 保证计算期间帧不被回收 |
| `EventBus.subscribe(...)` | 订阅数据到达、缺测和代次变化 |

### 3.3 按数据类别处理

| A 分类 | B 默认策略 | 注意事项 |
|---|---|---|
| `static` | `PASSTHROUGH` | 不预测；转为基础图层或约束候选 |
| `slow` | `HOLD` | 默认零阶保持；有可靠物理模型后再替换 |
| `dynamic` | `INTERPOLATE/EXTRAPOLATE` | 设最大外推时长，超出后降低置信度或缺测 |
| `event` | 按生效/失效时间布尔叠加 | 不对几何边界做普通线性插值 |

### 3.4 来源摘要不能丢

B 每个风险帧至少保存如下输入追踪信息：

```json
{
  "data_id": "...",
  "data_type": "wind_field",
  "issue_time": "2026-07-15T06:00:00Z",
  "valid_time": "2026-07-15T12:00:00Z",
  "source": "NOAA GFS/NOMADS",
  "version": "...",
  "quality_flag": "good"
}
```

## 4. B → C：建议 `RiskFrame v1`

### 4.1 Python 形状

```python
@dataclass(frozen=True)
class RiskFrame:
    schema_version: str          # "bc.risk-frame.v1"
    risk_id: str
    scenario_id: str
    route_id: str
    generation_id: int
    valid_time: datetime
    as_of_time: datetime
    generated_at: datetime
    model_version: str
    payload: xarray.Dataset
    source_summary: tuple[dict, ...]
```

`payload` 建议使用单时次二维网格：

```text
coordinates:
  longitude / latitude，或 x / y + CRS

variables:
  risk_score    float32  [0, 1]   连续综合风险
  risk_level    uint8    [1, 5]   离散展示等级
  hard_mask     bool              True 表示不可通行
  confidence    float32  [0, 1]   B 的综合可信度

attributes:
  valid_time, as_of_time, generated_at,
  scenario_id, route_id, generation_id,
  schema_version, model_version, crs
```

若 B 需要保留可解释性，可增加 `risk_ice/risk_wave/risk_wind/...` 分量，但 C 只能依赖上面的四个必需变量。

### 4.2 BC 缓存协议

```python
class RiskSource(Protocol):
    def publish(self, frame: RiskFrame) -> None: ...
    def get_window(
        self,
        start: datetime,
        end: datetime,
        *,
        scenario_id: str,
        generation_id: int,
        as_of: datetime,
    ) -> Sequence[RiskFrame]: ...
    def latest_before(...) -> RiskFrame | None: ...
```

推荐行为：

- 按 `valid_time` 排序，默认至少覆盖未来 24 h；
- 同一 `valid_time` 有多个版本时，选择当前 `as_of_time` 可见的最新可靠版本；
- 发布后对象不可变；
- 达到容量上限时优先保留当前规划窗口；
- `generation_id` 不匹配时拒绝写入和读取。

### 4.3 C 使用风险帧的原则

C 估计船舶到达某网格的 ETA，再从相邻 `RiskFrame.valid_time` 选择或插值风险。不能用“当前风险图”覆盖整条未来航程。

```text
船舶预计 18:30 进入网格
        │
        ├── 读取 18:00 RiskFrame
        └── 读取 19:00 RiskFrame
                │
                ▼
       按 BC 策略得到 18:30 风险/硬约束
```

## 5. C → D：建议 `RoutePlan v1`

### 5.1 路线对象

```python
@dataclass(frozen=True)
class Waypoint:
    longitude: float
    latitude: float
    eta: datetime
    recommended_speed: float


@dataclass(frozen=True)
class RoutePlan:
    schema_version: str          # "cd.route-plan.v1"
    scenario_id: str
    generation_id: int
    route_id: str
    plan_version: str
    generated_at: datetime
    as_of_time: datetime
    start_time: datetime
    mode: str                    # shortest / low_risk / recommended / replanned
    waypoints: tuple[Waypoint, ...]
    distance_km: float
    eta_hours: float
    avg_risk: float
    max_risk: float
    compute_ms: float
    replan_reason: str
    source_risk_versions: tuple[str, ...]
```

序列化建议使用 GeoJSON `FeatureCollection`：路线为 `LineString`，每个航点的 ETA 和推荐速度可保存在并行属性数组或单独 Point features 中。指标放在顶层 `properties`。

### 5.2 CD 缓存协议

```python
class RouteResultSource(Protocol):
    def publish(self, plan: RoutePlan, candidates: Sequence[RoutePlan]) -> None: ...
    def latest(self, *, scenario_id: str, generation_id: int) -> RoutePlan | None: ...
```

CD 采用“最新值覆盖”而不是长队列，保留当前、上一版及候选路线即可。D 读取时不得阻塞 C。

## 6. D 的读取规则

D 展示至少包括：

- 当前模拟时间和播放状态；
- 当前/未来风险图层及其 `valid_time`；
- 当前路线、上一版路线和候选路线；
- 船位、航向、速度、已航段、剩余航段和 ETA；
- 平均/最大风险、计算耗时、重规划次数和原因；
- 数据/风险/路线更新时间、模型版本和质量提示。

若 CD 暂无新版本，D 继续显示最近有效路线并标注“计算中”或“最后更新时间”。若 `generation_id` 已变化，D 不得继续展示旧代次路线。

## 7. 缺测、过期与错误

- A 缺测：发布 `MissingDataAlert`，不制造全零环境场。
- B 缺测：按策略保持或降级，并降低 `confidence`；超过最大外推时长后明确标缺测。
- C 风险窗不完整：请求 B 补算，或采用低置信度保守规划；不得把空风险当安全。
- D 数据过期：继续显示最近结果，但必须醒目标注过期和更新时间。
- 任一模块发现 schema/generation/scenario 不匹配：拒绝消费并记录结构化错误。

## 8. B/C/D 最小验收清单

### B

- 输出间隔可配置，默认 1 h；
- 24 h 风险序列按 `valid_time` 连续排序；
- `risk_score/risk_level/hard_mask/confidence` 完整；
- `source_summary/model_version/as_of_time/generation_id` 可追溯；
- 历史测试证明没有使用未来才发布的 A 帧。

### C

- 按预计到达时间采样风险；
- 输出最短、低风险、综合推荐和重规划路线；
- 硬约束违规数为 0；
- 输出航程、ETA、平均/最大风险、耗时和重规划原因；
- 跳转时能够取消旧任务并拒绝旧代次结果。

### D

- 固定帧率渲染，不被 B/C 计算阻塞；
- 没有新路线时能继续显示最近有效结果；
- 正确显示版本、时间、质量和重规划原因；
- 跳转/重置后不显示旧代次内容。

## 9. 建议开发顺序

```text
1. RiskFrame + BC 缓存合同测试
2. B 的 PASSTHROUGH/HOLD/INTERPOLATE 最小实现
3. 简单可解释风险融合，生成未来 24 h 序列
4. RoutePlan + CD 最新值缓存合同测试
5. C 的时间依赖 A* 与三种基础模式
6. 重规划触发和 generation_id 取消机制
7. D 的只读渲染与状态提示
8. 联调、回放、性能和对比试验
```
