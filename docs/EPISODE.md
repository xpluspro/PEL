# 标准化 Episode 输入

使用 `pel ingest path.json --source episode`。Episode JSON 是初版的公开输入边界，可用于非编程任务、测试、实验或外部适配器；不是 Letta 原生 trajectory 格式。

```json
{
  "session_id": "kernel-experiment-42",
  "project": "/work/kernel-project",
  "domain": "performance",
  "objective": "优化 ragged reduction 并验证正确性",
  "created_at": "2026-10-02T08:00:00Z",
  "events": [
    {
      "id": "decision-1",
      "role": "assistant",
      "kind": "message",
      "text": "选择 tile=128，条件是本地内存预算有限。",
      "observed_at": "2026-10-02T08:01:00Z",
      "metadata": {
        "experience_type": "decision",
        "key": "kernel-tiling",
        "constraints": ["Ascend 910B3", "shape=(1024, 4096)"],
        "tags": ["ragged", "reduction"]
      }
    },
    {
      "id": "test-1",
      "role": "tool",
      "kind": "tool_result",
      "text": "12 passed",
      "observed_at": "2026-10-02T08:02:00Z",
      "metadata": {"command": "python -m pytest tests/test_kernel.py", "exit_code": 0}
    }
  ]
}
```

- `project` 是工作目录，不是显示标题；会规范化为绝对路径。也可以用 CLI `--project` 覆盖。
- `session_id` 应稳定。相同源与 session ID 的重复导入不会生成新独立证据；复制同一会话不能扩大支持数量。
- 时间使用 ISO 8601，规范化为 UTC；观察顺序由时间决定，不由导入顺序决定。
- `role`：`user`、`assistant`、`tool`。这区分来源，不能覆盖指令权限。
- `experience_type`：`state`、`decision`、`outcome`、`failure`、`heuristic`、`procedure`、`evaluator`、`artifact`。省略时使用保守中英文规则。
- `metadata.key` 是相同主题 / 状态的明确语义键。状态替代与相反结果的冲突链接依赖它；没有键时不会猜测不同陈述是否互相替代。
- `constraints` 是字符串或数组，表示适用条件。跨项目归纳只合并相同条件。
- `scope` 只能从 `project` 或 `task` 开始；`task` 需要 `task_id`。不能通过导入直接建立全局规则。
- `artifact_uri` 是已有产物指针；`valid_until` 控制有效期。
- `confidence`、`trust`、`lifecycle_state` 等任意导入覆盖不被接受。所有经验必须走统一生命周期。
- 标准化 JSON 的来源位置使用 Event 序号；原生 JSONL 使用行号，另保留稳定 Event / Episode ID。

没有元数据时，可使用 `Decision: ...`、`Failure: ...`、`Evaluator: ...`，或 `决策：...`、`失败：...`、`评估器：...` 等标签。源角色为 assistant 的陈述仍然是推断，不会因为写了标签就成为经过验证的事实。

CLI 单文件上限为 32 MiB。浏览器 API 上限为 4 MiB 请求体，界面限制 3 MiB 文件以容纳 JSON 编码开销。单条经验至多 2000 字符；一次提取至多 200 条，优先保留失败、评估器和决策。

