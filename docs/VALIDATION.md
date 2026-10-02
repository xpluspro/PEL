# v0.1 验证结果

2026-10-02，Windows / Python 3.12.7。

| 检查 | 结果 |
| --- | --- |
| `python -m unittest discover -s tests -q` | 56 项通过 |
| `node --check pel/web/app.js` | 通过 |
| `python -m compileall -q pel` | 通过 |
| `pip wheel --no-build-isolation --no-deps .` | 构建成功 |
| 隔离目录安装 wheel 与 UI 资源检查 | 通过 |
| Codex / Claude 原生样例导入 | 通过 |
| 两种代理 subprocess 包装器 / stdin / JSONL 回收 | 离线可执行 fixture 通过 |
| Claude Hooks 安装幂等、保留配置、未 flush 最终回复 | 通过 |
| 离线闭环：避开陷阱、复用脚本、采用 oracle | 合成场景全部通过 |
| 生命周期哈希链 | 校验通过 |
| 6 个并发 SQLite 导入 | 无重复，日志连续 |
| 浏览器：项目 / 类型筛选、详情、保存反馈、文件导入、编译 | 通过 |
| 浏览器 console | 0 errors / 0 warnings |
| 390 px 视口 | 页面无横向溢出，移动端编译导航可访问 |

Playwright 截图位于 `output/playwright/experience-library.png`、`experience-detail.png`、`context-compiler.png`、`mobile-library.png`、`mobile-compiler.png`。最终演示库为 `.pel/demo.sqlite3`，调试期间的合成数据单独保留，未改动用户的原始文档或原生会话。

最终构建位于 `.pel/final-build/personal_experience_layer-0.1.0-py3-none-any.whl`。

没有调用真实模型执行双代理 A/B 测试。离线 fixture 与确定性模拟器验证的是产品数据流，不是模型能力提升。后续实测方法见 `BENCHMARK.md`。
