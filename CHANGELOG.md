# 基础架构工作台 Changelog

版本规则：主版本号跟随年份迭代（v25.x → v26.0），功能变更在本文件登记。

## v29.6（2026-08-20）

### 新增：Zabbix 告警清单磁贴（内置小工具）
- Zabbix 服务端下发 `X-Frame-Options: SAMEORIGIN` 禁止被 iframe 嵌入，改走 **Zabbix JSON-RPC API**：后端新增 `/api/zabbix/problems`（支持 API Token 或 `user.login` 登录态缓存 25 分钟自动重登，兼容新旧版参数名；`problem.get` 拉未恢复告警，经 `trigger.get selectHosts` 解析主机名——6.0 的 problem/trigger 均无 hostid 字段），需环境变量 `ZABBIX_API_URL` + `ZABBIX_API_TOKEN`（或 `ZABBIX_API_USER`/`ZABBIX_API_PASS`，未配置时磁贴提示、路由 503）
- 磁贴瓦片显示当前未恢复告警数（≥5 条标红），点击弹层展示告警清单（严重度色点/主机名/时间），条目可点击新窗口直达 Zabbix 问题视图；弹层带手动刷新按钮；添加小工具表单新增「内置：Zabbix 告警清单」类型
- 排版紧凑化（同日补丁）：弹窗内列表解除 300px 限高、随弹层整体滚动，行距/字号收紧，同屏可多显示约一倍条目

### 新增：iframe 磁贴「兼容嵌入」打开方式
- MCP 平台等 SPA 系统在严格沙箱（无同源）下 localStorage/Cookie 不可用导致空白；`open_mode` 新增 `compat`（恢复 allow-same-origin），仅限可信内网系统使用，默认仍为严格沙箱；新建/编辑表单均可选

## v29.5（2026-08-20）

### 修复：工具磁贴 iframe 嵌入兼容性（OA / Zabbix）
- **磁贴支持「新窗口打开」模式**：OA 类系统首次加载带 SSO 跳转，在沙箱 iframe 内首次 404（刷新后才可用）；Zabbix 默认下发 X-Frame-Options 禁止被嵌入，iframe 弹窗空白。工具磁贴新增 `config.open_mode`（iframe / window），选「新窗口打开」后点击磁贴直接新开标签页，不受沙箱与 X-Frame-Options 限制
- **嵌入弹窗增加「↗ 新窗口」逃生按钮**：内嵌空白/404 时可一键改用新窗口打开
- **磁贴角标**：新窗口模式的工具磁贴标题旁显示 ↗ 角标；新建/编辑工具表单均支持选择打开方式（打开方式为个人配置，不受公共池同步影响）

## v29.4（2026-08-20）

### 安全：三轮审计发现的 2 项低危越权修复
- **活动统计越权（`/api/activity/stats`）**：管理员传 `user_id=X` 时无团队 scope 校验，子管理员可查任意用户的登录/页面活动统计；现改用 `_can_view_user` 作用域校验（本人 / 全局管理员 / 目标所在团队的子管理员），越权返回 403
- **工作日志越权（`/api/work-logs`）**：同一模式——子管理员带 `user_id=X` 可读任意用户操作日志，且不带参数时返回全量用户日志；现带参数时经 `_can_view_user` 校验，不带参数时子管理员仅返回本团队日志

## v29.3（2026-08-20）

### 安全：二次代码审计发现的 4 项修复
- **前端存储型 XSS（onclick 内联属性）**：4 处 `onclick="fn('${escapeHtml(x)}')"` 模式中，`&#39;` 会被浏览器解码还原为 `'` 再交给 JS，转义失效；新增 `jsStr()`（JSON.stringify + escapeHtml 双重防护）并替换 iTop 工程师姓名、板块名、MCP Token 标签 4 处插值
- **iframe 磁贴 sandbox 失效**：工具磁贴 iframe 的 `allow-scripts + allow-same-origin` 组合允许 iframe 自行移除 sandbox 属性，等同无沙箱；移除 allow-same-origin
- **登录限速 XFF 伪造绕过**：`_login_client_ip` 不再无条件信任 X-Forwarded-For，默认取 TCP 对端 IP；仅 `TRUST_PROXY=1` 时才取 XFF 首段
- **会话 Cookie 属性显式化**：新增 `SESSION_COOKIE_HTTPONLY=True`、`SESSION_COOKIE_SAMESITE='Lax'`，不再依赖浏览器默认行为

## v29.2（2026-08-20）

### 安全：代码审查发现的 5 项中危漏洞修复
- **子管理员横向越权**：`/api/team/<id>/details`、`/analysis`、`/job-analysis` 三个路由此前仅判 is_admin，子管理员可查任意员工；现按 `_can_view_user` 鉴权（本人 / 全局管理员 / 目标所在团队的子管理员）
- **SSRF（favicon 代理）**：新增 `_validate_icon_url` —— 仅 http(s)，解析后 IP 拒绝环回/链路本地（169.254 元数据）/多播/保留段（内网业务地址保留放行）；禁跳转防绕过；响应体限 512KB
- **LDAP 注入**：登录与 AD 同步的用户名均改用 ldap3 `escape_filter_chars` 转义；登录接口增加 sAMAccountName 字符集白名单校验
- **MCP token 泄露到 URL**：SSE `event: endpoint` 不再拼入 token，改由 sessionId 会话凭据鉴权（会话目录写入 uid，会话结束/超 1h 自动失效）；旧客户端带 `?token=` 的调用双向兼容
- **登录限速**：同 IP+用户名连续失败 5 次锁定 15 分钟（429）；内存态，每 gunicorn worker 独立计数

## v29.1（2026-08-20）

### 修复：线上 Decimal 500 与调度器防重
- **Decimal JSON 序列化**：`get_stats` 中 MySQL `AVG()` 返回 Decimal 导致 `/mcp/message` 报 `TypeError: Object of type Decimal is not JSON serializable`，统一转 float
- **调度器多实例防重**：gunicorn 2 worker 各起一个 scheduler，现用 MySQL 命名锁（`GET_LOCK`）选主，仅抢到锁的实例运行周期任务，进程退出自动接管
- **SECRET_KEY fail-fast**：未配置或仍为占位符时拒绝启动
- **部署规范**：生产敏感配置从 docker-compose.yml 拆至 `.env`（env_file 引用，600 权限，git 忽略）

## v29.0（2026-08-19）

### 修复：iTop 工单流转与日志全链路（重大重构）
- **根因一（部署）**：容器重建后 `ITOP_MCP_URL` 环境变量丢失，所有写回（流转/日志/同步）报 `Failed to resolve 'your_server_ip'`；已在 compose 中固化该变量
- **根因二（字段）**：定制版 iTop 各工单类必填字段与枚举值不同——`ev_assign` 需 team_id/agent_id/servicefamily_id/service_id 四件套；`ev_pending` 需 pending_reason；Incident 的 `ev_resolve` 需 resolution_code/solution/difficulty_level（且随工单字段现值动态变化，如 servicesubcategory_id 为空时一并要求）；UserRequest 无 difficulty_level 字段（v28.9 的三件套对其反致 `Unknown attribute`）；resolution_code 枚举为定制值（Incident：1=远程解决/2=现场解决；UserRequest：assistance=日常运维），标准 iTop 值（solved 等）全部非法
- **后端自适应**：流转字段白名单过滤；`Unknown attribute` 自动剔除并重试；`Missing mandatory` 解析为 `need_fields` 返回（前端动态补填）；`Invalid stimulus` 转友好中文提示；已测试全部路径（含 UserRequest 三件套自动剔除、Incident in_process 动态四件套、状态机 new→assigned→resolved→closed 全链）
- **前端动态化**：流转弹窗按动作渲染字段（解决方式按工单类定制枚举、挂起原因、指派四件套预填工单现值）；iTop 返回缺失必填字段时弹窗内动态出现补填输入框；流转成功显示新状态
- 依赖：requirements.txt 补 pymysql/cryptography（MySQL 后端必需，此前仅服务器手工安装）

## v28.9（2026-08-18）

### 修复：iTop ev_resolve 必填字段
- Incident 工单"标记解决"时补传 resolution_code/solution/difficulty_level（v29.0 进一步修复该组合对 UserRequest 的误伤，并更正枚举值为定制版）

## v28.8（2026-08-18）

### 修复：思考型模型重试策略针对性调整
- **实测**：Qwen3.8-27B 生成建议时思考+正文实际需 4000+ token，v28.7 的 6000 上限在复杂素材下仍不足
- **重试策略升级**：content 为空时改用「关闭思考（vLLM `chat_template_kwargs.enable_thinking=false`）+ 3 倍 token（上限 16000）」重试；后端不支持该参数（HTTP 400）时自动退化为纯加大 token
- 请求超时 180s → 300s

## v28.7（2026-08-18）

### 修复：思考型模型（Qwen3.8-27B）生成报错
- **根因**：思考型模型思考链耗尽 `max_tokens` 时接口返回 `content=null`（finish_reason=length），`ai_chat` 直接 `.strip()` 抛 `'NoneType' object has no attribute 'strip'`
- **修复**：content 为空且思考耗尽时自动加倍 `max_tokens` 重试一次（上限 6000）；仍为空则返回明确错误提示
- **附带**：`api_key` 为 `none`（字面量）时不再发送 Authorization 头

## v28.6（2026-08-18）

### 新增：AD 字段同步 + 团队概览岗位/工号展示
- **AD 同步**：成员管理页新增「从 AD 同步工号/岗位/邮箱」按钮，批量从域控（employeeID/title/mail）回填空缺字段，不覆盖已填内容；子管理员仅同步本团队成员
- **团队概览**：成员卡片副标题显示「岗位 · 工号」；`user_stats` 接口返回工号/邮箱/岗位描述；员工详情头部改为 岗位 | 工号 | 邮箱
- **AI 岗位分析**：团队概览成员卡片新增「AI 岗位分析」按钮（有岗位描述时显示）；信息安全团队同步域控 title 后按钮即可用

## v28.5（2026-08-18）

### 修复：子管理员权限与主管理员划分
- **报告**：子管理员报告列表默认限定本团队；生成/查看/编辑/删除校验目标成员属于本团队（越权返回 403）；前端成员筛选下拉仅显示本团队成员
- **工单（iTop）**：子管理员 `scope=team` 统计与列表限定本团队映射工单；按 user_id 查询校验目标在团队内；详情/加日志/流转按工单属主团队校验；工单映射管理与同步控制仅主管理员可见
- **团队概览**：`/api/stats?team=1` 子管理员聚合计数与成员统计限定本团队
- **AI 用量**：`/api/ai/usage?team=1` 子管理员 token 统计（总量/今日/按功能/按用户）限定本团队
- **成员简表**：`/api/team/members` 返回 `team_id` 字段供前端筛选

## v28.4（2026-08-18）

### 新增：工作内容管理团队筛选 + 信息安全团队板块
- **团队筛选**：工作内容管理页在「全部成员」前新增「全部团队」下拉，选择团队后：成员下拉自动收窄为该团队成员，工作项列表按团队过滤（后端 `/api/work-items` 新增 `team_id` 参数；子管理员强制本团队范围）
- **安全板块**：信息安全团队预置 8 个板块 — 安全运营监控、漏洞管理、渗透测试、等保合规、应急响应、防火墙与安全设备运维、数据安全、邮件与终端安全（幂等种子）
- **数据修正**：修正团队成员邮箱格式

## v28.3（2026-08-18）

### 修复：合并重复的基础架构团队分栏 + 主管理员双角色
- **分栏合并**：成员管理页删除写死的「基础架构团队」默认区，全部团队（含基础架构团队）统一由 teams 表动态渲染，不再出现两个同名分栏
- **主管理员（is_super）**：`users` 表新增 `is_super` 列。管理员用户 = 主管理员 + 基础架构团队成员双角色：属于基础架构团队（成员列表/组织架构/团队概览均可见），同时保留全局管理权限（`get_admin_scope()` 对 is_super 始终返回全局）
- **徽章区分**：成员/组织架构页显示「主管理员」（红金渐变）/「子管理员」（金色）徽章；主管理员不可被移出团队或降级（前后端双重守卫）
- **板块迁移**：旧的 team_id=NULL 板块（15 个）全部自动迁移至基础架构团队名下
- **数据修正**：修正团队成员域账号

## v28.2（2026-08-18）

### 新增：团队概览分栏 + 模型供应商管理
- **团队概览分栏**：团队概览页按团队分组显示成员卡片，每个团队独立区块，未分配团队的用户归入「未分配团队」区
- **模型供应商管理**：API 设置页新增「模型供应商」区块，支持 OpenAI 兼容接口的模型接入
  - 默认预置 Qwen3.6（系统默认）和 Qwen3.8-27B 两个供应商
  - 管理员可添加/删除自定义供应商（名称、Base URL、API Key、模型名）
  - 所有用户可选择自己偏好的模型，AI 调用自动使用所选模型
- **数据模型**：新增 `model_providers` 表（id/name/base_url/api_key/model/is_default/created_by），`users` 表新增 `preferred_provider_id` 列
- **API**：`GET/POST/PUT/DELETE /api/model-providers`、`PUT /api/model-providers/preference`
- **基础架构团队**：init_db 自动分配基础架构团队成员种子数据

## v28.1（2026-08-18）

### 增强：权限隔离与团队分栏
- **成员管理分栏**：成员管理页按团队分栏显示（基础架构团队 + 各子团队），全局管理员可见所有团队，子管理员只看本团队
- **负责板块分栏**：负责板块配置按团队分组，基础架构板块（team_id=NULL）与团队专属板块独立管理
- **权限隔离**：子管理员（`is_admin=1 + team_id=X`）所有管理 API 均限制在本团队范围（用户列表、工作项、组织架构、负责板块的增删改查）
- **数据模型**：`responsibility_areas` 表新增 `team_id` 列，板块可按团队归属
- **认证**：登录接口返回 `team_id`，前端据此判断全局管理员/子管理员并控制 UI 可见性

## v28.0（2026-08-18）

### 新增：组织架构管理
- **团队管理**：新增「组织架构」Tab（管理员可见），支持创建/编辑/删除团队，团队支持层级关系（`parent_id`）
- **成员管理**：团队卡片内展示成员列表，支持添加/移除成员，可设置团队子管理员（`is_admin=1 + team_id`）
- **数据模型**：新增 `teams` 表（id/name/parent_id/description），`users` 表新增 `team_id` 列；`init_db()` 自动建表 + 幂等 ALTER
- **API**：`GET/POST/PUT/DELETE /api/org/teams`、`POST/DELETE/PUT /api/org/teams/<id>/members`
- **初始数据**：创建「信息安全团队」种子数据

## v27.2（2026-08-18）

### 增强：AI 功能接入 ITSM 工单数据
- **工作建议**（`ai_daily_suggest`）：AI 生成每日建议时，输入新增当前处理中 iTop 工单 + 近7天已关闭工单
- **岗位分析**（`team_member_analysis`）：AI 分析员工工作情况时，输入新增该员工的 iTop 工单数据
- **岗位匹配分析**（`team_member_job_analysis`）：AI 判断岗位匹配度时，输入新增该员工的 iTop 工单数据

## v27.1（2026-08-18）

### 调整：ITSM 统计口径与映射交互
- **范围限定本团队**：`/api/itop/status` 统计、管理员 `scope=team` 工单列表均只统计**映射到工作台用户**的工单（外部门工程师的工单不再进入视图与统计）；ITSM 页统计卡改为「团队工单 / 处理中 / 已完结 / 已映射工程师」
- **映射改为搜索式**：映射区不再罗列全部 108 位 iTop 工程师，改为「搜索 iTop 工程师姓名 → 点选 → 选择工作台用户 → 建立映射」；已建映射列表支持一键清除；`GET /api/itop/user-map` 支持 `q=` 关键字搜索（默认不返回全量）
- **数据修正**：清理错误的工程师映射记录

## v27.0（2026-08-18）

### 新增：iTop ITSM 工单集成（首个 MCP 技能接入）
- **MCP 调用能力**：新增 `mcp_client.py`（Streamable HTTP transport 通用客户端，会话管理 + SSE 解析 + 失效自动重连），工作台后端可直接调用外部 MCP 服务；首版接入 itop-mcp（`http://YOUR_SERVER_IP:8003/mcp`，`ITOP_MCP_URL` 可配）
- **工单同步**：定时拉取 iTop 四类工单（服务请求/事件/问题/变更），增量=近2天、全量=近90天，分页拉取 + `ON DUPLICATE KEY UPDATE` 幂等入库（`itop_tickets` 表）；调度策略：工作时间（周一~五 08-18 点）每小时一次，非工时每天一次；管理端可手动触发增量/全量同步
- **ITSM 工单页**：新顶级 Tab，支持状态（处理中/已完结）、工单类型、关键字筛选，管理员可切换查看全团队；工单详情弹窗展示描述/解决方案/处理日志
- **工作台处理工单（写回 iTop）**：详情弹窗可直接添加处理日志（公开/私有），并支持流转（指派/挂起/解决/关闭/重开/转派等，动作合法性由 iTop 状态机校验），写回后自动刷新单据状态
- **工程师映射**：iTop 工程师 → 工作台用户，默认按姓名自动匹配（支持 "EN-中文名" 格式），管理端可手动映射/清除（`itop_user_map` 表），映射变更即时重算归属
- **统计整合**：个人仪表盘新增「处理中工单 / 今日完成工单 / 本月完成工单」卡片；团队概览成员卡新增 ITSM 行；报告（日报/周报/月报）素材与 HTML/文本/AI 输入新增 ITSM 工单段
- **新 API**：`/api/itop/status|tickets|tickets/<cls>/<ref>[/log|/stimulus]|sync|user-map`、`/api/team/member/<uid>/itop`

### 修复
- MCP SSE 响应无 charset 时按 latin-1 解码，UTF-8 中文解出 NEL(\x85) 会被 `splitlines` 误当换行切断 JSON —— 客户端强制 utf-8 并只按 `\r\n/\r/\n` 切行

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
