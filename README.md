# 基础架构工作台 (Infrastructure Workbench)

基础架构团队的统一工作平台，集成钉钉数据同步、AI 工作建议、任务管理、团队概览、MCP Server 接入等功能。

## 功能特性

### 钉钉数据同步
- **聊天消息**：自动同步钉钉群聊/单聊消息，支持增量同步（2天窗口）与全量同步（30天）
- **待办事项**：同步钉钉待办任务，支持开始/完成状态流转
- **日程日历**：同步钉钉日历事件，支持周/月视图
- **听记纪要**：同步钉钉听记和会议纪要（含 AI 摘要）
- **用户隔离**：每个用户独立 DWS Token 目录，数据完全隔离
- **同步日志**：前端可查看每日同步状态与详细统计

### AI 工作建议
- 基于同步的钉钉数据（待办、日程、聊天）生成每日工作建议
- 智能识别任务优先级和时间冲突
- 提供日程安排优化建议
- **AI 润色**：完成任务/编辑工作内容时，可一键润色使描述更专业简洁

### 任务管理
- 工作项创建、分配、进度跟踪
- 支持周期性任务（每日/每周/每月）
- 协同人员管理（多人协作同一任务）
- 子任务与里程碑管理
- **多文件上传**：支持一次选择多个文件或整个文件夹（保留相对路径）
- 待办任务开始按钮绿色脉冲动效提示

### 团队概览
- 团队成员工作量统计（含协同者任务）
- 每员工 Token 今日/本月消耗 + MCP 接入状态
- 用户活跃度追踪（登录次数、页面访问、使用时长）
- 任务完成率看板
- 近7天完成趋势图

### MCP Server 接入
- **标准 SSE 协议**：兼容 QoderWork、WorkBuddy 等标准 MCP 客户端
- 每个 SSE 连接独立 sessionId，跨 worker 响应中转
- 支持多 Token（每个 AI 工具独立 Token）
- 可查看已接入工具列表与 Token 使用情况
- 一键生成部署文档（MD 格式下载）

### 认证
- LDAP/AD 域账号认证
- 钉钉扫码绑定（DWS unionId）

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端 | Flask 3.0.3 + Gunicorn (gthread 2w×8t) |
| 数据库 | MySQL 8.0（通过 _db_shim.py 兼容层，原 SQLite 代码无需修改） |
| 前端 | 单文件 SPA (vanilla JS, ~4500 行) |
| 部署 | Docker Compose |
| 认证 | LDAP/AD |
| 钉钉同步 | DWS CLI（per-user token isolation） |

## 快速开始

### 环境要求
- Docker & Docker Compose
- MySQL 8.0（容器 workbench-mysql）
- LDAP/AD 服务器（可选）
- DWS CLI（钉钉数据同步，安装于 /app/data/dws_bin/dws）

### 部署

```bash
# 克隆仓库
git clone https://github.com/your-github-user/infra-workbench.git
cd infra-workbench

# 构建并启动（含 MySQL 容器）
docker compose up -d --build

# 查看日志
docker logs -f infra-workbench
```

访问 http://<host>:9080

### 配置

主要配置项在 `app.py` 中（支持环境变量覆盖）：

```python
# LDAP 配置
LDAP_SERVER = os.environ.get('LDAP_SERVER', 'ldap://your-ldap-server')
LDAP_DOMAIN = os.environ.get('LDAP_DOMAIN', 'your-domain')
LDAP_BIND_USER = os.environ.get('LDAP_BIND_USER', r'domain\user')
LDAP_BIND_PASS = os.environ.get('LDAP_BIND_PASS', '')

# MySQL 配置
MYSQL_HOST = os.environ.get('MYSQL_HOST', '172.17.0.1')
MYSQL_PORT = int(os.environ.get('MYSQL_PORT', '3306'))
MYSQL_USER = os.environ.get('MYSQL_USER', 'root')
MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', '')
MYSQL_DATABASE = os.environ.get('MYSQL_DATABASE', 'workbench')

# 钉钉 DWS 路径
DWS_BIN = '/app/data/dws_bin/dws'
DWS_TOKEN_DIR = '/app/data/dws_tokens'  # per-user: <dir>/<user_id>
```

## 项目结构

```
infra-workbench/
├── app.py              # Flask 主应用 (~5400 行)
├── _db_shim.py         # MySQL 兼容层（datetime/date→str, close()幂等）
├── static/
│   └── index.html      # 单文件 SPA 前端 (~4500 行)
├── data/               # 数据目录（MySQL 数据在 workbench-mysql 容器）
│   ├── dws_bin/        # DWS CLI 二进制
│   ├── dws_tokens/     # per-user DWS token 目录
│   └── uploads/        # 用户上传文件
├── Dockerfile
├── docker-compose.yml
├── CHANGELOG.md        # 版本变更记录
└── requirements.txt
```

## 钉钉数据同步说明

### DWS CLI 约束
- `chat list-all-conversations --limit` 最大 100，需配合 `--cursor` 翻页
- `chat message list` 需 `--direction newer` 从给定时间往现在拉消息
- 消息分页 cursor 可能为 int，subprocess 调用需 str() 转换

### 同步模式
- **手动同步**：用户点击"立即同步"，拉取截止当前时间的所有数据
- **自动同步**：每天 08:00 自动增量同步（2天内消息）
- **全量同步**：首次绑定或数据丢失时，拉取30天内全量数据

### MySQL 迁移兼容
v26.x 从 SQLite 迁移至 MySQL 8.0。通过 `_db_shim.py` 兼容层，原有 SQLite 代码（`sqlite3.connect()`, `conn.execute()` 等）无需修改即可运行于 MySQL：
- `datetime`/`date`/`timedelta` 对象自动转为字符串（对齐 SQLite TEXT 行为）
- `close()` 幂等化（重复关闭不再抛错）
- `ON CONFLICT` 语法需手动改为 `ON DUPLICATE KEY UPDATE`（已在 app.py 中处理）
- 需补建唯一索引以支持 `ON DUPLICATE KEY UPDATE`

## 版本历史

详见 [CHANGELOG.md](CHANGELOG.md)

### v26.6 (当前版本)
- **修复 DWS 同步 0 条**：MySQL 迁移后 `ON CONFLICT` 语法导致写入失败（1064），改为 `ON DUPLICATE KEY UPDATE` + 补唯一索引
- **编辑工作内容弹窗**：新增 AI 润色 + 填入按钮

### v26.0 - v26.5
- MCP 标准 SSE 协议（兼容 QoderWork/WorkBuddy）
- 多文件/文件夹上传（保留相对路径）
- AI 润色填入、Token 复制修复
- 日报/周报/月报生成修复（MySQL datetime 兼容）
- 团队统计口径对齐（含协同者）
- AI 功能标签汉化、Token 消耗显示

### v25.x
- SQLite → MySQL 8.0 迁移
- DWS CLI 钉钉数据同步（per-user 隔离）
- AI 任务分解/描述生成
- MCP 多 Token 支持

## 许可证

内部项目，仅供基础架构团队使用。
