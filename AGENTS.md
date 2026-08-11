# AI Agent 开发约束

本仓库以架构契约和可复现测试为准。修改前依次阅读：

1. `README.md`
2. `docs/ARCHITECTURE_TRACE.md`
3. 与任务相关的接口文档：`docs/AB_INTERFACE.md` 或 `docs/BCD_HANDOFF.md`

必须保持以下不变量：

- A 负责采集、预处理、索引、回放和 AB 发布；风险、规划、渲染分别属于 B、C、D。
- `issue_time`、`valid_time`、`ingest_time` 都是 UTC，语义不可混用。
- 查询和回放必须满足 `issue_time <= simulation_time`；不得从文件名或本地 mtime 猜发布时间。
- 进入 AB 的 NetCDF 每帧只能有一个 `valid_time`。
- payload/sidecar 使用 `.part → payload → sidecar` 的原子发布顺序。
- sidecar 必须用 `payload_sha256`、`payload_size_bytes` 和唯一
  `publication_id` 精确绑定 payload；禁止跨生产任务复用证据。
- 发布后的 AB/BC/CD 对象视为不可变。
- AB 分区必须包含 `route_id`；A 的 `route_id` 对应全系统
  `corridor_id`，不能冒充 `scenario_id`。
- 同一逻辑时次的新 revision 按显式质量/issue/ingest 规则选择；不得覆盖已发布历史。
- 模拟跳转后必须通过 `generation_id` 拒绝旧任务迟到结果。
- 完整窗默认目标 156 h、最低 132 h；旧快照脚本不能证明覆盖。
- 缺测要显式报告，禁止用全零数组伪装有效数据。
- 风、流、冰漂规范为真东/真北分量；波向规范为 from true north
  clockwise；水深规范为正向上 elevation。缺单位或方向证据时拒绝，不按变量名猜。
- A 保存源网格及 `grid_id`，B 负责共享目标网格；A 的限制区不得按图层名称自动转成
  `hard_mask`。
- 不提交账号、密码、密钥、下载数据、缓存、虚拟环境或运行输出。
- 运行数据可留在 `.gitignore` 覆盖的 `data/` 供 B/C 联调，但不能提交。
- `FolderWatchSource` 当前按单一摄取 owner 部署；未实现 heartbeat 前不要启动无协调的
  多 watcher。

修改数据或接口契约时，同步更新：代码、测试、相关 Schema、README/接口文档。完成后运行：

```bash
make check
```

采集链发生实质变化时，还要记录一次可复现的真实源 smoke 结果；若因凭据、源站或网络
无法执行，必须在交付说明中明确，不得把 fixture 通过写成真实下载完成。
