# 游戏雷达 · 云端版

通过 GitHub Pages 展示游戏卡片与 HTML 日报，通过 GitHub Actions 每天北京时间 09:00 采集候选、调用 OpenAI 做有证据的初步分析。电脑关闭也能运行。GitHub 定时任务可能延迟，不保证准点送达。

## 首次开启

1. 在仓库 Settings → Pages → Build and deployment → Source 选择 **GitHub Actions**。
2. 在 Settings → Secrets and variables → Actions → New repository secret 添加 **OPENAI_API_KEY**。仅在 GitHub 的 Secret 输入框中填写真实密钥，勿放入代码、Issue 或聊天。
3. 可选添加 Repository variable **OPENAI_MODEL**；默认 `gpt-6-luna`，须是账户支持 Responses 和 Structured Outputs 的型号。
4. 打开 Actions → Daily game radar → Run workflow。成功后从部署结果进入网页；本仓库预计地址为 https://fool123.github.io/game-radar/，以实际部署结果为准。

没有 API 密钥时仍可采集并保存候选，页面明确显示 AI 未配置。模型请求失败也不会伪造评估。初始参考库不是当天发现的新推荐。

## 研究范围与当前边界

目标是发现传播效益好、玩法有趣、核心机制可复现的游戏。Demo、原型、试玩与新玩法信号都可进入候选；发售与好评率不是门槛。六维评分保留未知，证据不足允许没有重点推荐。

当前自动采集入口包括 Steam、itch.io、TapTap Maker；来源访问失败会展示缺口。Bilibili、YouTube、抖音的实机与传播查证目前没有自动接通，不能把来源列表理解为已经覆盖全部平台。

云端 OpenAI 当前分析官方描述，默认每轮最多 5 项。它不观看视频、不试玩，因此传播、实际趣味、完整体验三项保持未知，仅初步分析机制、钩子与变体。该阶段不会单凭文案产生重点推荐。完整说明见 [AI 证据分析](docs/cloud-ai.md)。OpenAI 按账户 API 用量收费，调用上限不等于金额预算。

## 反馈与矫正

游戏卡片中选择反馈 → 生成 GitHub Issue 草稿 → 在 GitHub 检查并提交。生成草稿不代表保存成功。Issue 内容公开；默认只有仓库所有者提交的反馈生效。已授权编辑者可通过变量 RADAR_EDITORS（逗号分隔登录名）加入，每日任务统一同步。

修改 Issue 中的 JSON 会替换该条反馈的有效版本，关闭 Issue 撤回其未来影响，重新打开可恢复。原有历史证据和日报不改写。长期偏好必须明确选定规则动作与标签，普通单项目纠错不会自动扩散成偏好。所有者修改 Issue 会触发同步；其他授权编辑者的即时触发需额外设置 RADAR_EDITOR_LOGINS_JSON（JSON 登录名数组），否则在下次每日任务同步。

## 飞书（可选）

默认不发送。在 Repository secrets 中添加 FEISHU_WEBHOOK_URL，可选 FEISHU_WEBHOOK_SECRET；然后将 Repository variable **RADAR_AUTO_SEND** 设为 `true` 才启用自动发送。应用机器人模式改设 RADAR_FEISHU_MODE=`app`，并配置 FEISHU_APP_ID、FEISHU_APP_SECRET、FEISHU_CHAT_ID。

发送前先把报告与发送占位记录提交到 cloud-state 分支，再请求飞书。结果未知或任务中断时不自动重发，需人工检查，避免重复消息。同一天重跑复用已生成的正式日报；新采集结果仍更新卡片，但不回写当日旧日报。

## 公开范围与持久化

本仓库及 Pages 公开，**cloud-state 分支也公开**。该分支只保存云端新建的数据库：公开来源、评估、公开 Issue 反馈与发送状态。网页按白名单导出。真实凭据仅由 Actions Secrets 提供，不进入数据快照。

不要上传本机 runtime/、config.json、.env、私人反馈或原始浏览器文件。发布包仅含应用、测试、参考种子和云端文档。云端使用独立 cloud_runtime，不导入本机数据库。

## 运维与验证

Actions 日志查看任务完成与来源缺口。首次成功后再停用旧本地定时任务以免重复运行。公开仓库长期没有活动可能被 GitHub 停用定时工作流，届时在 Actions 恢复。

本地验证：Python 3.12+，运行 `python -m unittest discover -s tests -q`。不需要安装第三方 Python 包。`python -m radar.cloud prepare --offline` 和 `python -m radar.cloud publish` 可生成独立静态预览，不调用真实 API、不发送飞书。

参考：[GitHub Pages 自定义工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)、[定时工作流](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。