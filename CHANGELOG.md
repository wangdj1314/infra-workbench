# 基础架构工作台 Changelog

版本规则：主版本号跟随年份迭代（v25.x → v26.0），功能变更在本文件登记。

## v26.7（2026-08-18）

- **修复平均耗时统计为 0**：完成任务时若未点击「开始」（`started_at` 为空），耗时不计算导致平均耗时显示 0 分钟。改为用 `created_at` 兜底作为起始时间；同时回刷历史已完成任务的耗时数据

## v26.6（2026-08-18）

- **修复 DWS 同步 0 条**：MySQL 迁移后 `_save_knowledge`/每日同步日志使用 SQLite `ON CONFLICT` 语法导致写入全部失败（1064），改为 `ON DUPLICATE KEY UPDATE`，并补建 `user_knowledge(user_id,source,external_id)`、`dingtalk_sync_log(sync_date)` 唯一索引；聊天消息分页 cursor 统一 str() 防 subprocess 类型错误。数据从上次成功同步点起补齐至当前时刻
- **编辑工作内容弹窗**：新增 AI 润色 + 填入按钮（与完成弹窗一致），润色结果可直接填入工作描述；Token 用量标签新增「编辑润色」

## v26.5（2026-08-18）

### 新增
- 团队概览成员卡片新增一行：每人 **Token 今日/本月消耗**（取自 ai_usage，格式化为 k/万）与 **MCP 接入情况**（启用中的 Token 数 + 最近使用日期，未接入灰色显示）。

### 修复
- AI Token 统计「按功能」中英混杂：补全 `mcp_doc→MCP部署文档`、`job_analysis→岗位分析`、`report_edit→报告润色`、`complete_polish→完成说明润色` 等中文标签，未知名回落「其他」。
- 版本徽标 v26.5。

## v26.4（2026-08-18）

### Bug 修复
- **日报/周报/月报生成恢复**：MySQL 迁移后 `created_at`/`completed_at` 等 timestamp 列由驱动返回 `datetime` 对象，与字符串边界比较时抛 `TypeError` 导致 500（前端表现为"报告生成失败: Unexpected token '<'"）。`_report_materials` 现统一将时间字段规整为字符串后再参与比较/切片。
- **团队概览与员工详情统计口径对齐**：概览卡片统计含「作为协同者」的任务，而员工详情弹窗只按 `user_id` 统计，导致缩略卡与点开数字对不上。详情接口（工作项列表/分类统计/近7天完成趋势）现与卡片口径一致（本人 + 协同）。
- 详情弹窗「待处理」改为只统计 `status='pending'`（原按未完成统计、把进行中也计入），与卡片/主区域口径一致；团队 user_stats 的 SUM 结果统一转 int（shim 经 Decimal 返回字符串）。
- `_db_shim` 连接 `close()` 幂等化（重复关闭不再抛 pymysql Error）。
- **「AI 工作意见分析」修复**：`team_member_analysis` 中 `completed_at[:10]` 对 datetime 对象切片抛 TypeError → 500 HTML（前端同样报 "Unexpected token '<'"）。根因修复：shim 行出口统一将 DATE/DATETIME/TIMESTAMP 转为字符串（对齐 SQLite TEXT 行为），一次消灭同类崩溃。
- 版本徽标 v26.4。

## v26.3（2026-08-18）

### 修复与优化
- 完成任务弹窗：AI 润色成功后，润色按钮右侧出现「📥 填入说明」按钮，一键将润色结果填入完成说明。
- MCP Token 生成弹窗复制按钮修复：HTTP 非安全上下文下 `navigator.clipboard` 不可用导致复制无响应，新增 `execCommand` 降级方案（`_copyText` 统一助手，部署文档「复制全文」同步修复）。
- MCP 服务端接入：SSE / Message 端点 URL 由只读输入框改为纯文本展示（Token 弹窗内端点同步调整）。
- 部署文档标签修复：「M 文件」错别字统一更正为「MD文件」；下载按钮更醒目（⬇ 下载 MD 文件）。
- 页面版本徽标 v26.3，MCP serverInfo 版本对齐 26.3.0。

## v26.2（2026-08-18）

### 上传界面优化
- 交付物上传改为「单选框 + 右侧➕添加按钮」布局（替代双选框）：默认一个文件选择框，「添加」弹出菜单可继续追加文件或整个文件夹。
- 已选文件以清单展示（相对路径 + 大小），可逐个移除，自动去重，底部显示已选数量。
- 版本徽标更新 v26.2。

## v26.1（2026-08-18）

### 交付物多文件与文件夹上传
- 「完成任务」弹窗与「上传交付物」弹窗支持一次选择多个文件（Ctrl/Shift 多选）。
- 支持选择整个文件夹（webkitdirectory），逐文件上传并保留相对路径（如 `方案/文档/README.md`），按钮显示「上传中 n/m」进度。
- 服务端新增 relpath 处理：逐段净化防路径穿越，展示名保留目录结构，磁盘存储名拍平加时间戳防重名。
- 版本徽标更新 v26.1。

## v26.0（2026-08-18）

### MCP 服务端标准化（重点修复）
- **修复 QoderWork 等标准 MCP 客户端无法连接的问题**：
  - POST `/mcp/message` 的 JSON-RPC 响应改为经由 SSE 通道以 `event: message` 下发（标准 SSE transport 行为），此前直接放 HTTP 响应体导致标准客户端等待 SSE 超时；
  - 每个 SSE 连接分配独立 `sessionId`（endpoint 事件携带），跨 gunicorn worker 通过 `/app/data/mcp_sessions` 文件通道中转响应；
  - 支持 `notifications/initialized` 等 JSON-RPC 通知（无 id 消息回 202、不再返回 -32601），新增 `ping` 方法；
  - 兼容性双保险：带 sessionId 的 POST 返回 202 + 响应体，WorkBuddy 等简化客户端直接 POST `/mcp/sse` 的路径保持不变（200 + body），**两类客户端均可正常使用**。
- gunicorn 切换 gthread worker（2 worker x 8 线程），长连 SSE 不再被 sync worker 超时机制中断。
- MCP serverInfo 版本号对齐 26.0.0。

### 协同与子任务逻辑
- 协同人员可提交里程碑：`add_milestone` 权限放宽（require_owner=False），任务卡片对协同人显示「+ 里程碑」按钮。
- 子任务支持协同：子任务卡片新增「协同/管理协同」按钮与协同人徽标，复用现有协同管理弹窗。
- 「添加子任务」弹窗内置 ✨ AI 任务分解：按父任务标题+描述（含弹窗内手动输入）生成建议子任务，勾选后批量创建，继承弹窗所选分类/优先级/截止日期。
- 「登记里程碑状态」弹窗补充说明旁新增 AI 补充按钮，按任务上下文+当前状态生成说明。

### 其他
- 每个标签页独立 URL 路径（hash 路由），刷新不跳回主页。
- 新增 `GET /api/work-items/<id>` 单任务详情接口。
- 页面版本徽标更新为 v26.0。

## v25.9 及更早
- v25.9：开始按钮绿色光效、公共链接池、用户活动记录、MCP 多 Token（mcp_tokens 表）。
- v25.x：SQLite → MySQL 8.0 迁移（_db_shim.py 兼容层）、DWS CLI 钉钉数据同步按用户隔离、AI 任务分解/描述生成。
