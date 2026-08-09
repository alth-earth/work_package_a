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
- 发布后的 AB/BC/CD 对象视为不可变。
- 模拟跳转后必须通过 `generation_id` 拒绝旧任务迟到结果。
- 缺测要显式报告，禁止用全零数组伪装有效数据。
- 不提交账号、密码、密钥、下载数据、缓存、虚拟环境或运行输出。

修改数据或接口契约时，同步更新：代码、测试、相关 Schema、README/接口文档。完成后运行：

```bash
make check
```
