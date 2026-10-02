# Personal Experience Layer · v0.1

把过去与 AI 完成的工作，变成下一位代理可以继承的经验。

本地优先的初版实现：**会话 → Episode → 带证据的经验 → 合并 / 演进 → 任务上下文 → 代理结果回收**。支持 Codex 与 Claude Code，默认不需要 API Key、模型服务、Node.js 或云端存储。Python 3.11+ 即可运行核心功能。

## 立即体验

在项目目录执行：

```powershell
python -m pel demo --db .pel/demo.sqlite3
python -m pel serve --db .pel/demo.sqlite3 --port 8765 --open
```

打开 http://127.0.0.1:8765。界面包含经验筛选、工作片段、证据与生命周期详情、反馈纠正、会话导入和上下文编译。演示数据包含性能优化、研究、课程作业三个领域；它们是合成样例，不是真实用户记录或真实模型效率测试。

`demo` 的离线验收会对比一个确定性模拟器是否避开已知失败、复用已有脚本、采用正确性评估。它验证系统的数据闭环，**不能用于声称真实代理效率提升**。

可选安装命令行入口：

```powershell
python -m pip install -e .
pel --help
```

默认数据库为 `~/.pel/experience.sqlite3`，跨项目共享。可通过 `--db` 使用独立数据库，或通过 `PEL_HOME` 指定存储目录。不要在真实使用中复用演示数据库。

## 接入过去的工作

原生 Codex rollout JSONL、`codex exec --json` 输出、Claude Code transcript / stream-json，以及标准化 Episode JSON 均可导入：

```powershell
python -m pel ingest examples/codex-session.jsonl --db .pel/sample.sqlite3 --domain performance
python -m pel ingest examples/claude-session.jsonl --db .pel/sample.sqlite3 --domain performance
python -m pel ingest examples/research-episode.json --db .pel/sample.sqlite3
```

缺少工作目录的源文件需要 `--project`。来源默认自动识别，也可用 `--source codex|claude|episode`。经验自动持久化，无需逐条确认。

持续监听指定会话目录：

```powershell
python -m pel watch "$env:USERPROFILE/.codex/sessions" --source codex --domain performance
python -m pel watch "$env:USERPROFILE/.claude/projects" --source claude
```

监听只读取你指定的目录；不修改源会话。保留原始会话中的项目目录，除非显式提供 `--project`。目录混合多个领域时不要统一指定 `--domain`；应分别监听或导入。`--once` 执行一轮扫描，适合定时任务。末尾未写完的 JSONL 会延后处理；中间损坏的记录会报错，不会跳过后伪造完整导入。

## 让下一位代理自动继承经验

通过 PEL 启动任务，会先编译上下文，再调用原有代理 CLI，结束后自动回收 JSONL 结果：

```powershell
python -m pel run codex --task "优化 ragged reduction，复用基准脚本并验证正确性" --project D:/work/kernel --domain performance
python -m pel run claude --task "调查相似 kernel 的性能回退" --project D:/work/kernel --domain performance
```

代理额外参数放在 `--` 后面，例如 `-- --model <你的模型>`。PEL 不替你修改代理的权限或沙箱设置。启动真实任务需要已安装并登录的对应 CLI。Codex 仍执行自身的 Git 仓库检查；非 Git 项目可显式传入 `-- --skip-git-repo-check`。

`--dry-run` 查看实际命令与注入内容，不调用模型、不保存编译记录。`--executable` 可指定代理可执行文件。Windows 下标准 npm `.cmd` shim 会解析为直接的 Node.js 调用，任务内容通过 stdin 传递，避免 shell 字符串解释。任务的原始 stdout 日志保存在数据库同目录的 `runs/`；这些日志可能包含任务中的原始内容，应像原生代理日志一样管理。

### Claude Code 的日常交互模式

在选定项目安装本地 Hooks，之后正常使用 `claude` 即可：

```powershell
python -m pel install-claude --project D:/work/kernel --domain performance
```

安装器合并 `.claude/settings.local.json`，保留原有设置并备份，不修改全局配置。重复安装相同配置不会产生重复 Hook。

- `UserPromptSubmit`：按当前用户任务自动检索和注入经验。
- `Stop`：抓取会话，包含尚未写入 transcript 的最后回复。
- `SessionEnd`：再次回收完整 transcript，重复证据保持幂等。

Hook 接入失败只写 stderr，不阻断正常工作。卸载时删除对应的三个 PEL Hook 条目，或在没有其他后续配置修改的情况下恢复安装器返回的备份文件。移动 PEL 的安装目录或更换 Python 后需更新 Hook 的绝对路径。

Codex 当前采用 `pel run codex` 自动注入，普通 Codex 会话可由 `watch` 被动接入。未自动更改 Codex 的全局配置。

接入方式核对自 [Codex 非交互模式官方文档](https://learn.chatgpt.com/docs/non-interactive-mode)、[Claude Code Hooks](https://code.claude.com/docs/en/hooks) 和 [Claude Code 程序化运行](https://code.claude.com/docs/en/headless)，日期为 2026-10-02。原生持久化日志字段同时参考本机 Codex 日志结构；这些内部格式可能随 CLI 版本变化。

## 检索、编译与反馈

```powershell
python -m pel brief --task "Optimize ragged reduction latency and validate correctness" --project D:/work/kernel --domain performance --agent claude --budget 4096
python -m pel brief --task "Verify ragged reduction correctness" --project D:/work/kernel --format evaluator --output .pel/evaluator.md
python -m pel list
python -m pel inspect <experience-id>
python -m pel feedback <experience-id> --action strengthen --reason "在第二个形状上复测，正确性和延迟均符合预期"
python -m pel feedback <experience-id> --action correct --statement "短行工作负载优先测试 tile=64" --reason "tile=128 对短行不适用"
python -m pel feedback <experience-id> --action narrow --scope task --scope-key kernel-task-12 --reason "仅适用于这一形状"
python -m pel export --output .pel/experience-export.json
python -m pel verify
```

四种输出是带来源的 Markdown 参考：`brief`、`rule`、`skill`、`evaluator`。`skill` 是流程参考，尚不是自动安装的 Agent Skills 包；`evaluator` 是验证方法与命令引用，不会自动执行历史命令。

预算使用 UTF-8 字节数作为保守 token 上界，包括标题、来源和警告。它会比模型的实际 tokenizer 更保守，中文也不会因英语字符估算而超出限制。`--json` 返回选择的经验 ID、检索依据和被预算排除的数量。

反馈支持 `strengthen`、`weaken`、`correct`、`supersede`、`narrow`、`pin`、`unpin`、`promote`、`archive`、`reject`。纠正与缩小范围会保留旧版本；重新导入旧说法不会取消人工治理。固定历史状态不会使其重新变为最新状态。

## 初版的信任与生命周期规则

- 八类经验：State、Decision、Outcome、Failure、Heuristic、Procedure、Evaluator、Artifact Reference。
- 代理报告是 `agent_inference`，默认候选；测试 / 运行结果与明确的用户反馈有独立证据类型。代理重复自述只合并来源，不增加置信度。
- 跨项目归纳要求同领域、相同经验和条件、至少两个项目与三个独立 Episode；宽范围经验仍是候选。不同条件不会静默合并，不自动泛化为个人全局规则。
- 新状态只有在显式语义键相同且观察时间更新时才替代旧状态。State 默认 30 天后不参与检索；可提供 `valid_until`。
- 相同语义键的相反经验保留双方与冲突链接，简报显示争议；过期、归档、被替代、拒绝、隔离的经验不进入正常上下文。
- 来源、原始片段、Episode、证据、生命周期变化和编译选择可检查。审计日志有哈希链，可发现已记录内容的修改；它不是带外签名，也不能证明未被重写或尾部截断。
- 导入内容始终是源数据，不能成为系统 / 开发者指令。已识别的指令覆盖与凭据窃取内容会被隔离，常见密钥模式会被脱敏。规则不能穷尽所有攻击和秘密格式；原始轨迹仍由用户持有。

## 开发与验证

```powershell
python -m unittest discover -s tests -v
node --check pel/web/app.js
```

测试使用 Python 标准库，覆盖原生适配器、提取、证据、重复导入、外部反馈、范围隔离、跨项目归纳、支持撤回、时间与版本、预算、注入隔离、审计、并发、CLI、代理包装器、Hooks 与本地 HTTP API。代理包装器测试使用明确标注的离线可执行 fixture，不调用真实模型。

代码划分：

```text
pel/adapters.py       原生轨迹 → Episode
pel/extractor.py      可替换的离线经验提取器
pel/repository.py     Repository 接口与 SQLite 实现
pel/engine.py         合并、归纳、反馈、检索、编译
pel/integrations.py   代理包装器与 Claude Hooks
pel/watcher.py        原生会话监听
pel/server.py         仅监听 loopback 的查看 API
pel/web/             无构建依赖的查看界面
```

更详细的设计与需求对应关系见 [初版开发与验收记录](docs/MVP.md)，标准化输入见 [Episode 格式](docs/EPISODE.md)，真实模型评估方案见 [基准方案](docs/BENCHMARK.md)。

## 当前边界

首版使用可替换的保守中英文规则提取与词项相关性检索；它不能理解所有隐含决策、因果关系和同义表达。显式标签或标准化元数据能提高准确性。没有接入 LLM 提取、embedding、Letta 原生 trajectory、WorkGraph、第三方记忆后端、MCP 或云端服务。

真实 Codex / Claude CLI 的接口已核对，完整包装器与 Hooks 使用离线 fixture 验证。本次没有运行收费模型调用；真实双代理回放、效率基准与检索精度需要用用户的实际任务继续评估。

