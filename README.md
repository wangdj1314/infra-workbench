# 基础架构工作台 (Infrastructure Workbench)

基础架构团队的统一工作平台，集成钉钉数据同步、AI 工作建议、任务管理、团队概览等功能。

## 功能特性

### 钉钉数据同步
- **聊天消息**：自动同步钉钉群聊/单聊消息，支持增量同步
- **待办事项**：同步钉钉待办任务，支持开始/完成状态流转
- **日程日历**：同步钉钉日历事件，支持周/月视图
- **听记纪要**：同步钉钉听记和会议纪要

### AI 工作建议
- 基于同步的钉钉数据（待办、日程、聊天）生成每日工作建议
- 智能识别任务优先级和时间冲突
- 提供日程安排优化建议

### 任务管理
- 工作项创建、分配、进度跟踪
- 支持周期性任务（每日/每周/每月）
- 待办任务开始按钮绿色脉冲动效提示

### 团队概览
- 团队成员工作量统计
- 用户活跃度追踪（登录次数、页面访问、使用时长）
- 任务完成率看板

### MCP Server 接入
- 支持 MCP (Model Context Protocol) Server 接入
- SSE 端点 + Bearer Token 认证
- 可查看已接入工具列表（如 workbuddy、qoder）

### 认证
- LDAP/AD 域账号认证
- 钉钉扫码绑定

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端 | Flask 3.0.3 + Gunicorn |
| 数据库 | SQLite (WAL 模式) |
| 前端 | 单文件 SPA (vanilla JS) |
| 部署 | Docker Compose |
| 认证 | LDAP/AD |

## 快速开始

### 环境要求
- Docker & Docker Compose
- LDAP/AD 服务器（可选）
- DWS CLI（钉钉数据同步）

### 部署

```bash
# 克隆仓库
git clone https://github.com/your-github-user/infra-workbench.git
cd infra-workbench

# 构建并启动
docker compose up -d --build

# 查看日志
docker logs -f infra-workbench
```

访问 http://<host>:9080

### 配置

主要配置项在 `app.py` 中：

```python
# LDAP 配置
LDAP_SERVER = 'ldap://your-ldap-server'
LDAP_BASE_DN = 'dc=example,dc=com'

# 钉钉 DWS 路径
DWS_PATH = '/usr/local/bin/dws'
```

## 项目结构

```
infra-workbench/
├── app.py              # Flask 主应用 (~5400 行)
├── static/
│   └── index.html      # 单文件 SPA 前端 (~4000 行)
├── data/               # SQLite 数据库目录
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 钉钉数据同步说明

### DWS CLI 约束
- `chat list-all-conversations --limit` 最大 100，需配合 `--cursor` 翻页
- `chat message list` 需 `--direction newer` 从给定时间往现在拉消息

### 同步模式
- **手动同步**：用户点击"立即同步"，拉取截止当前时间的所有数据
- **自动同步**：每天 08:00 自动增量同步（2天内消息）

## 版本历史

### v25.9 (当前版本)
- 修复 MCP 接入状态显示
- 修复 AI 日程日期识别（含星期）
- 修复聊天同步分页问题
- 新增用户活跃度统计
- 新增待办开始按钮动效

## 许可证

内部项目，仅供基础架构团队使用。
