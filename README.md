# Infrastructure Workbench (基础架构工作台)

A unified team work platform for infrastructure operations, integrating DingTalk data sync, AI-powered work suggestions, task management, team overview, ITSM ticket integration (iTop via MCP), multi-team permission system, model provider management, and MCP Server access.

## Features

### Team & Permission System
- **Multi-team management** with team-scoped resources, members, and responsibility areas
- **Two-tier admin model**: Super admin (global access, non-degradable) + Team sub-admin (scope-isolated via `get_admin_scope()`)
- **AD field sync**: One-click backfill of employeeID / title / mail from domain controller

### Model Provider Management
- OpenAI-compatible API integration with user-selectable models
- Built-in support for Qwen3.6 (default) and Qwen3.8-27B
- **Thinking model compatibility**: Auto-retry with thinking disabled + 3x tokens when content is empty

### DingTalk Data Sync
- Chat messages, todos, calendar events, and meeting notes synced via DWS CLI
- Per-user token isolation with incremental (2-day) and full (30-day) sync modes
- Daily auto-sync with frontend sync status dashboard

### AI-Powered Features
- Daily work suggestions based on synced DingTalk data + ITSM tickets
- Smart task prioritization and schedule optimization
- One-click text polish for work descriptions
- AI job-role analysis from team member work patterns

### Task Management
- Work items with assignment, progress tracking, and periodic tasks (daily/weekly/monthly)
- Collaborator support, subtasks, and milestones
- Multi-file upload with folder structure preservation

### Team Overview
- Workload statistics per member (including collaborator tasks)
- Per-user AI token consumption tracking (today/monthly)
- MCP integration status and ITSM ticket statistics
- User activity tracking and task completion trend charts

### iTop ITSM Ticket Integration (via MCP)
- Generic MCP client (`mcp_client.py`) with Streamable HTTP transport, session management, SSE parsing, and auto-reconnect
- Scheduled sync of 4 ticket types (service requests / incidents / problems / changes)
- In-workbench ticket processing: add logs, execute transitions (write-back to iTop)
- Engineer mapping: iTop engineers to workbench users with auto-matching

### MCP Server Access
- Standard SSE protocol compatible with QoderWork, WorkBuddy, etc.
- Per-connection session IDs with cross-worker response relay
- Multi-token support with usage tracking

### Authentication
- LDAP/Active Directory domain authentication
- DingTalk QR code binding (DWS unionId)

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | Flask 3.0.3 + Gunicorn (gthread 2w x 8t) |
| Database | MySQL 8.0 (via `_db_shim.py` SQLite-compatible shim) |
| Frontend | Single-file SPA (vanilla JS, ~5400 lines) |
| Deploy | Docker Compose |
| Auth | LDAP/AD |
| DingTalk Sync | DWS CLI (per-user token isolation) |

## Quick Start

### Prerequisites
- Docker & Docker Compose
- MySQL 8.0 (external or container)
- LDAP/AD server (optional)
- DWS CLI for DingTalk sync (placed at `/app/data/dws_bin/dws`)

### Deploy

```bash
git clone https://github.com/wangdj1314/infra-workbench.git
cd infra-workbench

# Edit environment variables in docker-compose.yml
# Then build and start
docker compose up -d --build

# Check logs
docker logs -f infra-workbench
```

Access at `http://<host>:9080`

### Configuration

Key settings in `app.py` (all overridable via environment variables):

```python
# LDAP
LDAP_SERVER = os.environ.get('LDAP_SERVER', 'ldap://your-ldap-server:389')
LDAP_DOMAIN = os.environ.get('LDAP_DOMAIN', 'your-domain.local')
LDAP_BIND_USER = os.environ.get('LDAP_BIND_USER', r'domain\admin')
LDAP_BIND_PASS = os.environ.get('LDAP_BIND_PASS', '')

# MySQL
MYSQL_HOST = os.environ.get('MYSQL_HOST', '172.17.0.1')
MYSQL_PORT = int(os.environ.get('MYSQL_PORT', '3306'))
MYSQL_USER = os.environ.get('MYSQL_USER', 'root')
MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', '')
MYSQL_DATABASE = os.environ.get('MYSQL_DATABASE', 'workbench')

# AI Model
AI_BASE_URL = os.environ.get('AI_BASE_URL', 'http://your-ai-server:port/v1')
AI_MODEL = os.environ.get('AI_MODEL', 'qwen3.6')
```

## Project Structure

```
infra-workbench/
├── app.py              # Flask main app (~6900 lines)
├── _db_shim.py         # MySQL compat layer (datetime→str, close() idempotent)
├── mcp_client.py       # MCP HTTP client (Streamable HTTP transport)
├── static/
│   ├── index.html      # Single-file SPA frontend (~5400 lines)
│   └── favicon.ico
├── data/               # Data directory (DWS tokens, uploads)
├── Dockerfile
├── docker-compose.yml
├── CHANGELOG.md        # Version history
├── LICENSE
└── requirements.txt
```

## MySQL Migration Note

v26.x migrated from SQLite to MySQL 8.0. The `_db_shim.py` compatibility layer allows existing SQLite code to run on MySQL without changes:
- `datetime`/`date`/`timedelta` objects auto-converted to strings
- `close()` is idempotent (no error on repeated close)
- `ON CONFLICT` rewritten to `ON DUPLICATE KEY UPDATE` with proper unique indexes

## Version History

See [CHANGELOG.md](CHANGELOG.md) for detailed release notes.

## License

[MIT License](LICENSE)
