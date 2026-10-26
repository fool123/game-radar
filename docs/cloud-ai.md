# 云端 AI 证据分析

`radar.cloud_research.enrich(service, config=None, now=None)` 在云端采集后运行，返回 `{status, evaluated, message}`，并把来源状态保存为 `AI evidence analysis`。有效评估通过现有 `Store.evaluate(..., author='assistant')` 保存；人工评估不会被自动覆盖。

在 GitHub 仓库的 **Settings → Secrets and variables → Actions** 中添加 Secret `OPENAI_API_KEY`。不要把真实密钥写进仓库、网页、Issue 或聊天。运行环境提供密钥；此模块不查找本机凭据，也不加载 `.env`。缺少密钥时返回 `unconfigured`，不请求作品页或模型，日报仍可展示采集候选。

默认模型为 `gpt-6-luna`，使用固定 OpenAI Responses API 地址和严格 JSON Schema。可通过环境变量 `OPENAI_MODEL` 选择账户可用且支持 Responses / Structured Outputs 的模型；错误模型、权限不足、余额不足、拒答、截断或格式无效均降级，不伪造成功。2026-10-26 核对的[官方模型说明](https://developers.openai.com/api/docs/models/gpt-6-luna)列明该型号支持 Responses 与结构化输出，适合聚焦的高吞吐任务；默认关闭推理以保留短输出预算。账户实际访问权限和计费以控制台为准，此处不承诺价格或免费额度。[结构化输出官方文档](https://developers.openai.com/api/docs/guides/structured-outputs)说明 `text.format` 和严格 Schema，以及仍需处理拒答及未完成响应。

每轮默认分析 5 个最近 48 小时采集的非参考候选，最多 10 个；隐藏、事实争议、排除规则命中、已由人工评估或本次观察已经评估的项目会跳过。只接受已有采集器支持的 Steam、itch.io 和 TapTap 作品 URL，先重新取得公开描述：Steam 使用公开商店资料接口，其他页面读取明确的 description 元数据。标题不足、受限或没有足够描述时跳过模型请求。模型只接收本次公开摘记与项目名，不接收本地反馈、评论私记、凭据或原始页面。

调用者可通过 `config` 的 `max_candidates`（1–10，默认 5）、`request_timeout`（1–30 秒，默认 25）和 `total_timeout`（1–240 秒，默认 150）调低预算。公开页面每次最多 8 秒，含重定向最多 20 次请求，沿用来源模块的主机白名单、公开地址检查和 2 MB 响应上限。每个候选至多一次模型请求，不自动重试；输出上限 2,000 tokens、响应上限 200 KB，模型端不跟随重定向。总时间预算在每次连接和读取前检查；操作系统域名解析的耗时仍受系统解析器影响。请求数量/token 上限不是货币支出上限，应同时在 OpenAI 项目中设置适合自己的预算。

模型没有浏览、视频或执行工具。网页文字作为不可信资料处理；返回值需通过字段、长度、整数评分、理由和证据链接校验。证据 URL 必须与本次实际提供的链接完全一致；不接受任意新链接、额外字段、HTML 或明显虚称试玩的文本。**传播、实际趣味、完整体验三项强制保持 `null`**；最多仅对可复现机制、文字钩子和变体作初步推断，并固定注明未试玩、未观看实机。这使覆盖率最多 40%，不会因此进入重点推荐。验证器只能约束输出，不能证明模型推断正确，仍需人工与实机核验。

模块只新增官方摘记、摘要和评估，不创建或更改游戏事件时间。采集时间不会充当发行或传播发生时间。状态为 `idle` 表示没有适用候选，`partial` 表示部分失败或已达范围/时间上限，`failed` 表示本轮没有取得有效分析。状态消息仅含计数与固定说明，不记录异常原文、密钥或模型原始响应。

本次开发验证使用模拟网络响应，没有发送真实付费 API 请求。配置密钥后的真实账户权限、来源持续可达性和研究质量仍需在实际运行中观察。运行测试：`python -m unittest discover -s tests`。
