#!/usr/bin/env python3
"""
基础架构工作台 - Flask 后端
LDAP/AD 认证 + 团队管理 + 工作项 CRUD + 周期任务 + AI 原生功能
+ AI Token 计数 + 文件上传 + 分类管理 + 快捷链接 + API Key + 报告 + 外部同步
"""
import os
import re
import json
import _db_shim as sqlite3
import calendar
import threading
import time
import uuid
import secrets
import hashlib
from datetime import datetime, date, timedelta
from functools import wraps
from urllib.parse import quote
from mcp_client import MCPHTTPClient

# v17 修复：强制东八区时区（容器默认 UTC 会导致报告周期/生成时间错位 8 小时）
os.environ['TZ'] = 'Asia/Shanghai'
try:
    time.tzset()  # Linux 容器生效
except (AttributeError, Exception):
    pass  # Windows 开发环境无 tzset，datetime.now() 已返回本机时区

import requests
from flask import (
    Flask, request, jsonify, send_from_directory, session, g,
    send_file, abort, Response, stream_with_context
)
from ldap3 import Server, Connection, ALL
from ldap3.utils.conv import escape_filter_chars  # v29.2：LDAP 过滤器转义，防注入
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder='static', static_url_path='')
app.secret_key = os.environ.get('SECRET_KEY', '')
if not app.secret_key or 'YOUR_SECRET_KEY' in app.secret_key:
    # v29.1：fail-fast，避免占位符/缺失 SECRET_KEY 导致 session 可被伪造
    raise RuntimeError('SECRET_KEY 未配置或仍为占位符，拒绝启动')
app.permanent_session_lifetime = 86400  # 24h
# v29.3：显式声明会话 Cookie 安全属性（防 CSRF 与脚本窃读，不依赖浏览器默认行为）
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB upload limit

@app.after_request
def add_no_cache_headers(response):
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# ---- 配置 ----
LDAP_SERVER = os.environ.get('LDAP_SERVER', 'ldap://YOUR_DC_IP:389')
LDAP_DOMAIN = os.environ.get('LDAP_DOMAIN', 'your-domain.local')
LDAP_BIND_USER = os.environ.get('LDAP_BIND_USER', r'YOUR_DOMAIN\YOUR_ADMIN_USER')
LDAP_BIND_PASS = os.environ.get('LDAP_BIND_PASS', '')
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'wangdj')
# MySQL 配置（通过环境变量覆盖）
MYSQL_HOST = os.environ.get('MYSQL_HOST', '172.17.0.1')
MYSQL_PORT = int(os.environ.get('MYSQL_PORT', '3308'))
MYSQL_USER = os.environ.get('MYSQL_USER', 'root')
MYSQL_PASS = os.environ.get('MYSQL_PASS', 'YOUR_DB_PASSWORD')
MYSQL_DB = os.environ.get('MYSQL_DB', 'workbench')
_sqlite3 = sqlite3  # alias for isinstance checks
_sqlite3.configure(host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER, password=MYSQL_PASS, database=MYSQL_DB)
UPLOAD_DIR = os.environ.get('UPLOAD_DIR', '/app/data/uploads')

# AI 配置（Xinference / OpenAI 兼容接口）
AI_BASE_URL = os.environ.get('AI_BASE_URL', 'http://YOUR_AI_IP:9997/v1')
AI_MODEL = os.environ.get('AI_MODEL', 'qwen3.6')

# 钉钉 DWS CLI 配置（纯 CLI 方案，无需创建钉钉应用）
# 每用户在服务器上执行 dws auth login 完成授权，DWS_CONFIG_DIR 按用户隔离
DWS_TOKENS_DIR = os.environ.get('DWS_TOKENS_DIR', '/app/data/dws_tokens')

LDAP_BASE = ','.join(f'dc={p}' for p in LDAP_DOMAIN.split('.') if p)

RECURRING_TYPES = ('', 'daily', 'weekly', 'monthly')
RECURRING_LABEL = {'daily': '每日', 'weekly': '每周', 'monthly': '每月'}

DEFAULT_CATEGORIES = [
    '日常运维', '项目实施', '故障处理', '变更管理', '巡检检查', '其他'
]

# ====================================================================
# 数据库
# ====================================================================
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect()
    return db


@app.teardown_appcontext
def close_db(error):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def _ensure_column(conn, table, column, ddl):
    pass  # MySQL 迁移后所有列已存在，无需动态添加


def _now_str():
    """本地时间统一格式（YYYY-MM-DD HH:MM:SS，空格分隔，与 CURRENT_TIMESTAMP 区分）"""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def init_db():
    """MySQL 初始化：表已由迁移脚本创建，仅做种子数据和连接验证。"""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    try:
        conn = sqlite3.connect()
        # 验证连接
        conn.execute('SELECT 1')
        # v28.0：组织架构 - teams 表
        conn.execute('''CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTO_INCREMENT,
            name VARCHAR(100) NOT NULL UNIQUE,
            parent_id INTEGER DEFAULT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        # v28.0：users 表增加 team_id 列（子管理员/团队成员归属）
        try:
            conn.execute('ALTER TABLE users ADD COLUMN team_id INTEGER DEFAULT NULL')
        except Exception:
            pass  # 列已存在
        # v28.1：responsibility_areas 表增加 team_id 列（板块按团队分组）
        try:
            conn.execute('ALTER TABLE responsibility_areas ADD COLUMN team_id INTEGER DEFAULT NULL')
        except Exception:
            pass  # 列已存在
        # v28.1：初始化默认团队（幂等）
        for _tname, _tdesc in [('基础架构团队', 'IT基础架构运维团队'), ('信息安全团队', '信息安全团队')]:
            try:
                conn.execute('INSERT INTO teams (name, description) VALUES (?, ?)', (_tname, _tdesc))
            except Exception:
                pass  # 已存在则跳过
        # 初始化默认板块（若表为空）
        try:
            area_count = conn.execute('SELECT COUNT(*) as c FROM responsibility_areas').fetchone()['c']
            if area_count == 0:
                for a in ['IT基础设施','网络运维','系统运维','信息安全','数据中心','云运维','终端运维','应用运维']:
                    try:
                        conn.execute('INSERT INTO responsibility_areas (name) VALUES (?)', (a,))
                    except sqlite3.IntegrityError:
                        pass
        except Exception:
            pass
        # 初始化默认分类
        existing = conn.execute('SELECT COUNT(*) as c FROM categories').fetchone()['c']
        if existing == 0:
            for i, name in enumerate(DEFAULT_CATEGORIES):
                conn.execute('INSERT INTO categories (name, sort_order) VALUES (?, ?)', (name, i))
        # v17：基础架构分工 → 成员岗位描述（幂等，只填空值）
        try:
            job_map = [
                ('团队成员', '负责终端桌管（联软）、加密软件、AI 应用开发与 AI 运维。'),
                ('团队成员', '负责邮件系统、堡垒机、联想网盘、公有云运维及部门杂项。'),
                ('团队成员', '负责数据中心硬件运维、操作系统运维、虚拟化运维。'),
                ('团队成员', '负责操作系统、虚拟化、数据库及高级系统运维。'),
                ('团队成员', '负责终端网络运维、AP 无线运维、园区网络运维、VPN 与跨国线路。'),
                ('团队成员', '负责数据中心网络、网络架构、广域网、云网络。'),
                ('团队成员', '负责数据中心网络、网络架构、广域网、云网络。'),
            ]
            for _kw, _desc in job_map:
                conn.execute(
                    "UPDATE users SET job_description = ? WHERE display_name LIKE '%' || ? || '%' AND (job_description IS NULL OR job_description = '')",
                    (_desc, _kw))
        except Exception:
            pass
        # v28.2：模型供应商表
        conn.execute('''CREATE TABLE IF NOT EXISTS model_providers (
            id INTEGER PRIMARY KEY AUTO_INCREMENT,
            name VARCHAR(100) NOT NULL,
            base_url VARCHAR(500) NOT NULL,
            api_key VARCHAR(500) DEFAULT '',
            model VARCHAR(100) NOT NULL,
            is_default INTEGER DEFAULT 0,
            created_by INTEGER DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        # v28.2：users 表增加 preferred_provider_id 列
        try:
            conn.execute('ALTER TABLE users ADD COLUMN preferred_provider_id INTEGER DEFAULT NULL')
        except Exception:
            pass  # 列已存在
        # v28.2：初始化默认模型供应商（幂等）
        try:
            pc = conn.execute('SELECT COUNT(*) as c FROM model_providers').fetchone()['c']
            if pc == 0:
                conn.execute(
                    'INSERT INTO model_providers (name, base_url, api_key, model, is_default) VALUES (?, ?, ?, ?, ?)',
                    ('Qwen3.6 (默认)', 'http://YOUR_AI_IP:9997/v1', '', 'qwen3.6', 1))
                conn.execute(
                    'INSERT INTO model_providers (name, base_url, api_key, model, is_default) VALUES (?, ?, ?, ?, ?)',
                    ('Qwen3.8-27B', 'http://YOUR_AI_IP:8001/v1', '', 'qwen3.8-27b', 0))
        except Exception:
            pass
        # v28.2：分配基础架构团队成员
        try:
            infra_team = conn.execute("SELECT id FROM teams WHERE name = '基础架构团队'").fetchone()
            if infra_team:
                infra_id = infra_team['id']
                infra_names = ['团队成员', '团队成员', '团队成员', '团队成员', '团队成员', '团队成员', '团队成员', '团队成员']
                for _nm in infra_names:
                    conn.execute(
                        "UPDATE users SET team_id = ? WHERE display_name LIKE '%' || ? || '%' AND (team_id IS NULL OR team_id != ?)",
                        (infra_id, _nm, infra_id))
        except Exception:
            pass
        # v28.3：主管理员标志（is_super=1 表示全局主管理员，可同时属于某团队）
        try:
            conn.execute('ALTER TABLE users ADD COLUMN is_super INTEGER DEFAULT 0')
        except Exception:
            pass  # 列已存在
        try:
            _infra = conn.execute("SELECT id FROM teams WHERE name = '基础架构团队'").fetchone()
            if _infra:
                # wangdj = 主管理员 + 基础架构团队成员（双角色）
                conn.execute(
                    "UPDATE users SET is_super = 1, is_admin = 1, team_id = ? WHERE ad_username = 'wangdj'",
                    (_infra['id'],))
                # 旧板块（team_id=NULL）全部归入基础架构团队
                conn.execute(
                    'UPDATE responsibility_areas SET team_id = ? WHERE team_id IS NULL',
                    (_infra['id'],))
        except Exception:
            pass
        # v28.3：修正团队成员域账号 chenx → chenxin6
        try:
            conn.execute(
                "UPDATE users SET ad_username = 'chenxin6' WHERE display_name = '团队成员' AND ad_username = 'chenx'")
        except Exception:
            pass
        # v28.3：安全团队账号与域控核对修正（池铭航=chimh；团队成员安全岗=zhengxy3，zhengxy 是物流部重名者）
        try:
            conn.execute(
                "UPDATE users SET email = 'chenxin6@example.com' WHERE ad_username = 'chenxin6' AND email != 'chenxin6@example.com'")
            conn.execute(
                "UPDATE users SET ad_username = 'chimh', display_name = '池铭航', email = 'chimh@example.com' WHERE ad_username = 'chimx'")
            conn.execute(
                "UPDATE users SET ad_username = 'zhengxy3', email = 'zhengxy3@example.com' WHERE ad_username = 'zhengxy' AND team_id = (SELECT id FROM teams WHERE name = '信息安全团队')")
        except Exception:
            pass
        # v28.4：信息安全团队默认板块（幂等）
        try:
            sec_team = conn.execute("SELECT id FROM teams WHERE name = '信息安全团队'").fetchone()
            if sec_team:
                _sec_id = sec_team['id']
                for _a in ['安全运营监控', '漏洞管理', '渗透测试', '等保合规', '应急响应',
                           '防火墙与安全设备运维', '数据安全', '邮件与终端安全']:
                    try:
                        conn.execute('INSERT INTO responsibility_areas (name, team_id) VALUES (?, ?)', (_a, _sec_id))
                    except Exception:
                        pass  # 已存在
        except Exception:
            pass
        conn.commit()
        conn.close()
        print('[init_db] MySQL connection OK, seed data checked.')
    except Exception as e:
        print(f'[init_db] WARNING: MySQL init failed: {e}')

# ====================================================================
# 周期任务
# ====================================================================
def _calc_next_run(recurring, from_date_str):
    if recurring not in RECURRING_TYPES or not recurring:
        return ''
    d = None
    try:
        d = date.fromisoformat(from_date_str) if from_date_str else date.today()
    except Exception:
        d = date.today()
    if recurring == 'daily':
        return (d + timedelta(days=1)).isoformat()
    if recurring == 'weekly':
        return (d + timedelta(days=7)).isoformat()
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day).isoformat()


_DAILY_SYNC_MARKER = '/app/data/last_daily_sync.txt'


def _daily_sync_due():
    """每日钉钉同步是否到期（今天未同步且已过 08:00）"""
    try:
        if os.path.exists(_DAILY_SYNC_MARKER):
            with open(_DAILY_SYNC_MARKER) as f:
                if f.read().strip() == date.today().isoformat():
                    return False
    except Exception:
        pass
    return datetime.now().hour >= 8


def _mark_daily_sync_done():
    try:
        os.makedirs(os.path.dirname(_DAILY_SYNC_MARKER), exist_ok=True)
        with open(_DAILY_SYNC_MARKER, 'w') as f:
            f.write(date.today().isoformat())
    except Exception:
        pass


_SCHED_LOCK_CONN = None  # 持有命名锁的专用连接（连接存活期间锁持续有效）


def _acquire_scheduler_lock():
    """v29.1：gunicorn 多 worker 防重 —— 用 MySQL 命名锁选主，
    仅抢到锁的实例进入调度循环；进程退出/连接断开时锁自动释放，
    另一 worker 在下一次尝试中接管。"""
    global _SCHED_LOCK_CONN
    while True:
        try:
            conn = sqlite3.connect()
            got = conn.execute("SELECT GET_LOCK('infra_workbench_scheduler', 0) as got").fetchone()['got']
            if int(got or 0) == 1:
                _SCHED_LOCK_CONN = conn  # 保持连接存活即持锁
                print('[scheduler] 已获取调度锁，本实例负责周期任务')
                return
            conn.close()
        except Exception as e:
            print(f'[scheduler] 抢锁异常: {e}')
        time.sleep(30)


def scheduler_loop():
    _acquire_scheduler_lock()
    while True:
        try:
            conn = sqlite3.connect()
            today = date.today().isoformat()
            # 每日钉钉自动同步（每天 08:00 后触发一次，重启后当天仍可补跑）
            if _daily_sync_due():
                _mark_daily_sync_done()
                _daily_dingtalk_sync()
            # v27.0：iTop 工单同步（工作时间每小时/非工时每天一次，异步不阻塞主循环）
            if _itop_sync_due():
                _mark_itop_sync_done()
                threading.Thread(target=_sync_itop_tickets_safe, args=('incremental',), daemon=True).start()
            try:
                conn.execute('START TRANSACTION')
                templates = conn.execute(
                    "SELECT * FROM work_items WHERE recurring != '' AND next_run_at != '' AND next_run_at <= ?",
                    (today,)
                ).fetchall()
                for tpl in templates:
                    new_next = _calc_next_run(tpl['recurring'], tpl['next_run_at'])
                    if not new_next:
                        continue
                    conn.execute(
                        'UPDATE work_items SET next_run_at = ? WHERE id = ?',
                        (new_next, tpl['id'])
                    )
                    cur = conn.execute("""
                        INSERT INTO work_items
                        (user_id, title, description, category, priority, status, due_date, created_by, recurring, next_run_at, source_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        tpl['user_id'], tpl['title'], tpl['description'] or '',
                        tpl['category'], tpl['priority'], 'pending',
                        today, 'auto', '', '', tpl['id']
                    ))
                    conn.execute(
                        'INSERT INTO work_logs (user_id, action, item_id, detail) VALUES (?, ?, ?, ?)',
                        (tpl['user_id'], 'auto_created', cur.lastrowid,
                         f'周期任务自动生成（{RECURRING_LABEL.get(tpl["recurring"], tpl["recurring"])}）: {tpl["title"]}')
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        except Exception as e:
            print(f'[scheduler] {e}')
        time.sleep(60)


def _start_scheduler():
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()


# ====================================================================
# LDAP 认证
# ====================================================================
def _ldap_admin_conn():
    server = Server(LDAP_SERVER, connect_timeout=10)
    return Connection(server, user=LDAP_BIND_USER, password=LDAP_BIND_PASS,
                      authentication='SIMPLE', auto_bind=True)


def ldap_authenticate(username, password):
    if not LDAP_BIND_PASS:
        print('[LDAP] 未配置 LDAP_BIND_PASS')
        return False, None
    try:
        conn = _ldap_admin_conn()
        display_name = username
        user_dn = None
        try:
            conn.search(
                search_base=LDAP_BASE,
                # v29.2：转义特殊字符，防止 LDAP 过滤器注入
                search_filter=f'(&(objectClass=user)(sAMAccountName={escape_filter_chars(username)}))',
                attributes=['displayName', 'cn', 'mail', 'userPrincipalName']
            )
            if conn.entries:
                entry = conn.entries[0]
                user_dn = entry.entry_dn
                if entry.displayName:
                    display_name = str(entry.displayName.value)
                elif entry.cn:
                    display_name = str(entry.cn.value)
        except Exception as e:
            print(f'[LDAP] 用户搜索异常: {e}')
        finally:
            conn.unbind()

        if not user_dn:
            print(f'[LDAP] 未找到用户 {username} 的 DN')
            return False, None

        server = Server(LDAP_SERVER, connect_timeout=10)
        user_conn = Connection(server, user=user_dn, password=password,
                               authentication='SIMPLE', auto_bind=True)
        if user_conn.bound:
            user_conn.unbind()
            return True, display_name
        return False, None
    except Exception as e:
        print(f"[LDAP] 认证异常: {e}")
        return False, None


# ====================================================================
# 权限装饰器
# ====================================================================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': '未登录或会话已过期'}), 401
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': '未登录'}), 401
        if not session.get('is_admin'):
            return jsonify({'error': '需要管理员权限'}), 403
        return f(*args, **kwargs)
    return decorated


def get_admin_scope():
    """v28.3：返回当前管理员的团队作用域。
    - 主管理员（is_super=1）→ 返回 None，无论是否属于某团队，均为全局权限
    - 全局管理员（team_id=None）→ 返回 None，表示可看所有
    - 团队子管理员（team_id=X）→ 返回 X，表示只能看该团队
    """
    if not session.get('is_admin'):
        return None
    if session.get('is_super'):
        return None  # v28.3：主管理员始终全局
    return session.get('team_id')  # None = 全局管理员, int = 团队子管理员


def _can_view_user(target_user):
    """v29.2：员工数据查看鉴权 —— 本人 / 全局管理员 / 目标所在团队的子管理员。
    修复此前子管理员（team_id=X）可越权查看任意员工详情的横向越权问题。"""
    if target_user['id'] == session['user_id']:
        return True
    if not session.get('is_admin'):
        return False
    scope = get_admin_scope()
    if scope is None:
        return True  # 主管理员 / 全局管理员
    return dict(target_user).get('team_id') == scope


# v29.2：登录限速 —— 同 IP+用户名连续失败 5 次锁定 15 分钟（内存态，每 gunicorn worker 独立）
_LOGIN_LOCK = threading.Lock()
_LOGIN_STATE = {}  # 'ip|username' -> {'fails': int, 'until': ts}
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 900


def _login_client_ip():
    # v29.3：默认只信任 TCP 对端 IP，避免攻击者伪造 X-Forwarded-For 绕过登录限速；
    # 仅当部署在可信反向代理之后（TRUST_PROXY=1）才取 XFF 首段
    if os.environ.get('TRUST_PROXY') == '1':
        xff = request.headers.get('X-Forwarded-For', '')
        if xff:
            return xff.split(',')[0].strip()
    return request.remote_addr or ''


def _login_locked_seconds(ip, username):
    key = f'{ip}|{username}'
    with _LOGIN_LOCK:
        st = _LOGIN_STATE.get(key)
        if st and st.get('until', 0) > time.time():
            return int(st['until'] - time.time())
    return 0


def _login_record_fail(ip, username):
    key = f'{ip}|{username}'
    with _LOGIN_LOCK:
        st = _LOGIN_STATE.setdefault(key, {'fails': 0, 'until': 0})
        st['fails'] += 1
        if st['fails'] >= LOGIN_MAX_FAILS:
            st['until'] = time.time() + LOGIN_LOCK_SECONDS
            st['fails'] = 0
        # 条目过多时顺手清理已过锁定期且无失败计数的记录
        if len(_LOGIN_STATE) > 2000:
            now = time.time()
            for k in [k for k, v in _LOGIN_STATE.items()
                      if v.get('until', 0) + LOGIN_LOCK_SECONDS < now and v['fails'] == 0]:
                _LOGIN_STATE.pop(k, None)


def _login_record_ok(ip, username):
    with _LOGIN_LOCK:
        _LOGIN_STATE.pop(f'{ip}|{username}', None)


def api_key_required(f):
    """外部 API 认证：通过 X-API-Key 请求头认证"""
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get('X-API-Key', '')
        if not api_key:
            return jsonify({'error': '缺少 API Key'}), 401
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        conn = sqlite3.connect()
        conn
        row = conn.execute(
            'SELECT * FROM api_keys WHERE key_hash = ?', (key_hash,)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({'error': '无效的 API Key'}), 401
        # 更新最后使用时间
        conn.execute(
            'UPDATE api_keys SET last_used_at = ? WHERE id = ?',
            (datetime.now().isoformat(), row['id'])
        )
        conn.commit()
        conn.close()
        g.api_user_id = row['user_id']
        return f(*args, **kwargs)
    return decorated


@app.route('/api/health')
def health_check():
    return jsonify({'status': 'ok'}), 200


# ====================================================================
# AI 能力（Xinference / OpenAI 兼容）—— 带 Token 计数
# ====================================================================
def ai_chat(system, user, max_tokens=800, feature='unknown', provider=None):
    """调用 AI 并记录 token 消耗。provider 可选: {'base_url','api_key','model'}"""
    if provider is None:
        provider = _get_user_provider()
    base_url = provider['base_url'] if provider else AI_BASE_URL
    model = provider['model'] if provider else AI_MODEL
    api_key = (provider.get('api_key', '') if provider else '')
    headers = {'Content-Type': 'application/json'}
    if api_key and str(api_key).lower() != 'none':
        headers['Authorization'] = f'Bearer {api_key}'

    def _post(mt, extra=None):
        body = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user}
            ],
            'temperature': 0.3,
            'max_tokens': mt,
            'stream': False
        }
        if extra:
            body.update(extra)
        r = requests.post(
            f'{base_url}/chat/completions',
            headers=headers,
            json=body,
            timeout=300
        )
        r.raise_for_status()
        d = r.json()
        ch = (d.get('choices') or [{}])[0]
        m = ch.get('message') or {}
        return (
            (m.get('content') or '').strip(),
            ch.get('finish_reason'),
            (m.get('reasoning_content') or '').strip(),
            d.get('usage') or {}
        )

    content, finish, reasoning, usage = _post(max_tokens)
    # v28.8：思考型模型（如 Qwen3.8-27B）思考链可能耗尽全部 max_tokens 导致 content 为空
    # 实测该场景思考+正文需 4000+ token；重试策略：关闭思考(vLLM chat_template_kwargs)+3倍token，
    # 后端不支持该参数(400)时退化为纯加大 token
    if not content:
        _mt = min(max_tokens * 3, 16000)
        for extra in ({'chat_template_kwargs': {'enable_thinking': False}}, None):
            try:
                content, finish, reasoning, usage = _post(_mt, extra)
            except requests.HTTPError:
                continue
            if content:
                break
    if not content:
        raise ValueError('AI 返回空内容（思考型模型 token 不足或响应异常），请重试或切换模型')

    # 记录 token 消耗
    try:
        uid = session.get('user_id', 0)
        conn = sqlite3.connect()
        conn.execute(
            'INSERT INTO ai_usage (user_id, feature, prompt_tokens, completion_tokens, total_tokens, model) VALUES (?, ?, ?, ?, ?, ?)',
            (uid, feature,
             usage.get('prompt_tokens', 0),
             usage.get('completion_tokens', 0),
             usage.get('total_tokens', 0),
             model)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[ai_usage] 记录失败: {e}')
    return content


def _get_user_provider():
    """获取当前用户偏好的模型供应商，返回 dict 或 None（使用默认）"""
    try:
        uid = session.get('user_id')
        if not uid:
            return None
        conn = sqlite3.connect()
        user = conn.execute('SELECT preferred_provider_id FROM users WHERE id = ?', (uid,)).fetchone()
        conn.close()
        if not user or not user['preferred_provider_id']:
            return None
        conn = sqlite3.connect()
        p = conn.execute('SELECT base_url, api_key, model FROM model_providers WHERE id = ?',
                         (user['preferred_provider_id'],)).fetchone()
        conn.close()
        if p:
            return {'base_url': p['base_url'], 'api_key': p.get('api_key', ''), 'model': p['model']}
    except Exception:
        pass
    return None


@app.route('/api/ai/describe', methods=['POST'])
@login_required
def ai_describe():
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': '缺少工作标题'}), 400
    category = (data.get('category') or '').strip()
    system = (
        '你是基础架构运维团队的 AI 助手。根据用户给出的工作标题，生成一段 100~180 字的中文工作描述，'
        '内容涵盖：背景说明、具体任务、执行要点、产出要求。'
        '直接输出描述正文，不要标题、不要序号、不要客套话。'
    )
    try:
        content = ai_chat(system, f'工作标题：{title}\n分类：{category or "日常运维"}\n',
                          feature='describe')
        return jsonify({'description': content})
    except Exception as e:
        print(f'[AI] describe 异常: {e}')
        return jsonify({'error': f'AI 服务调用失败: {e}'}), 502


@app.route('/api/ai/decompose', methods=['POST'])
@login_required
def ai_decompose():
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': '缺少工作标题'}), 400
    description = (data.get('description') or '').strip()
    system = (
        '你是基础架构运维团队的 AI 助手。把给定的工作内容分解为 3~6 个具体、可执行的子任务。'
        '严格只输出 JSON 数组，格式：[{"title":"子任务标题","description":"子任务说明"}]，'
        '不要输出任何其他文字、注释或 Markdown 代码块标记。'
    )
    try:
        content = ai_chat(system, f'工作标题：{title}\n工作描述：{description or "无"}\n',
                          max_tokens=1500, feature='decompose')
        start, end = content.find('['), content.rfind(']')
        if start == -1 or end == -1:
            return jsonify({'error': 'AI 返回格式无法解析，请重试'}), 502
        tasks = json.loads(content[start:end + 1])
        if not isinstance(tasks, list):
            raise ValueError('not a list')
        tasks = [
            {'title': str(t.get('title', '')).strip(), 'description': str(t.get('description', '')).strip()}
            for t in tasks if isinstance(t, dict) and str(t.get('title', '')).strip()
        ]
        if not tasks:
            raise ValueError('empty list')
        return jsonify({'subtasks': tasks})
    except Exception as e:
        print(f'[AI] decompose 异常: {e}')
        return jsonify({'error': f'AI 服务调用失败: {e}'}), 502


@app.route('/api/ai/usage')
@login_required
def ai_usage_stats():
    """AI Token 消耗统计"""
    db = get_db()
    today = date.today().isoformat()
    month_start = today[:7] + '-01'

    if session.get('is_admin') and request.args.get('team'):
        # v28.5 子管理员：团队统计限定本团队成员
        scope_team_id = get_admin_scope()
        if scope_team_id is not None:
            where = 'WHERE user_id IN (SELECT id FROM users WHERE team_id = ?)'
            params = [scope_team_id]
        else:
            where = ''
            params = []
    else:
        where = 'WHERE user_id = ?'
        params = [session['user_id']]

    total = db.execute(f'SELECT SUM(total_tokens) as t FROM ai_usage {where}', params).fetchone()
    today_usage = db.execute(
        f"SELECT SUM(total_tokens) as t FROM ai_usage {where + ' AND' if where else 'WHERE'} created_at >= ?",
        params + [today]).fetchone()

    by_feature = db.execute(f"""
        SELECT feature, SUM(total_tokens) as tokens, COUNT(*) as calls
        FROM ai_usage {where}
        GROUP BY feature ORDER BY tokens DESC
    """, params).fetchall()

    by_user = []
    if session.get('is_admin') and request.args.get('team'):
        _ujoin_where = ('WHERE u.team_id = ?' if scope_team_id is not None else '')
        by_user = db.execute(f"""
            SELECT u.display_name, u.ad_username, SUM(a.total_tokens) as tokens, COUNT(*) as calls
            FROM ai_usage a JOIN users u ON a.user_id = u.id
            {_ujoin_where}
            GROUP BY u.id ORDER BY tokens DESC
        """, ([scope_team_id] if scope_team_id is not None else [])).fetchall()

    return jsonify({
        'total_tokens': total['t'] or 0,
        'today_tokens': today_usage['t'] or 0,
        'by_feature': [dict(f) for f in by_feature],
        'by_user': [dict(u) for u in by_user]
    })


@app.route('/api/ai/suggest', methods=['POST'])
@login_required
def ai_daily_suggest():
    """根据当前用户的工作情况，AI 生成工作建议（结合工作效率、聊天记录、待办、日程）
    v25.7：工作项包含协同任务；聊天记录过滤群聊噪音"""
    db = get_db()
    uid = session['user_id']
    today = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    collab_like = f'%,{uid},%'
    items = db.execute(
        "SELECT title, status, priority, due_date, category, actual_duration_minutes FROM work_items "
        "WHERE user_id=? OR (',' || collaborators || ',') LIKE ? ORDER BY priority, due_date",
        (uid, collab_like)
    ).fetchall()
    if not items:
        return jsonify({'suggestion': '暂无工作内容，建议先添加近期任务。'})
    pending = [i for i in items if i['status'] != 'completed']
    overdue = [i for i in pending if i['due_date'] and i['due_date'] < today]
    completed = [i for i in items if i['status'] == 'completed']
    # 效率数据
    avg_dur = sum(i['actual_duration_minutes'] or 0 for i in completed) / max(len(completed), 1)
    fast_count = sum(1 for i in completed if (i['actual_duration_minutes'] or 9999) <= 60)
    slow_count = sum(1 for i in completed if (i['actual_duration_minutes'] or 0) >= 480)
    # 知识库素材：聊天记录主题、待办、日程（近7天）
    # v25.7：聊天记录过滤——单聊保留，群聊只保留自己发送的
    chat_rows = db.execute(
        "SELECT title, content, event_time FROM user_knowledge WHERE user_id=? AND source='dingtalk_chat' AND occur_date>=? ORDER BY event_time DESC LIMIT 30",
        (uid, week_ago)
    ).fetchall()
    chat_rows = _filter_chat_records(chat_rows, uid, db)
    # v29.7：主题标注收发方向（我发出=本人发送，收到=接收），让 AI 区分是谁的诉求
    my_row = db.execute('SELECT display_name FROM users WHERE id = ?', (uid,)).fetchone()
    my_name = (my_row['display_name'] or '').strip() if my_row else ''
    chat_topics = []
    for r in chat_rows:
        if not r['title']:
            continue
        tag = '我发出' if _is_my_message(r.get('content') or '', my_name) else '收到'
        chat_topics.append(f"{r['title'][:30]}（{tag}）")
    chat_topics = list(dict.fromkeys(chat_topics))[:10]
    # v25.7-fix：待办过滤已完成的（钉钉待办 content 含「状态：已完成」）
    todo_rows = db.execute(
        "SELECT title, content FROM user_knowledge WHERE user_id=? AND source='dingtalk_todo' AND occur_date>=? ORDER BY event_time DESC LIMIT 15",
        (uid, week_ago)
    ).fetchall()
    todo_titles = [r['title'][:40] for r in todo_rows if r['title'] and '状态：已完成' not in (r['content'] or '')][:8]
    # v25.7-fix：日程输出携带具体日期时间，便于 AI 基于会议日期做时间优化建议
    cal_rows = db.execute(
        "SELECT title, event_time FROM user_knowledge WHERE user_id=? AND source='dingtalk_calendar' AND occur_date>=? ORDER BY event_time DESC LIMIT 15",
        (uid, week_ago)
    ).fetchall()
    def _fmt_cal(title, event_time):
        if not event_time:
            return title
        m = re.match(r'(\d{4})-(\d{2})-(\d{2})[T ](\d{2}:\d{2})', str(event_time))
        if not m:
            return title
        year, mon, day, hm = m.groups()
        try:
            dt = datetime(int(year), int(mon), int(day))
            weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
            weekday = weekday_names[dt.weekday()]
        except Exception:
            weekday = ''
        return f"{int(mon)}月{int(day)}日({weekday}) {hm} {title}"
    cal_titles = [_fmt_cal(r['title'][:40], r['event_time']) for r in cal_rows if r['title']][:8]

    # v27.2：iTop ITSM 工单（当前处理中 + 近7天已关闭）
    itop_active = db.execute(
        "SELECT title, status, SUBSTRING(last_update,1,10) as updated FROM itop_tickets "
        "WHERE user_id=? AND status NOT IN ('resolved','closed','reject') ORDER BY last_update DESC LIMIT 10",
        (uid,)
    ).fetchall()
    itop_recent = db.execute(
        "SELECT title, SUBSTRING(close_date,1,10) as closed FROM itop_tickets "
        "WHERE user_id=? AND status IN ('resolved','closed') AND close_date>=? ORDER BY close_date DESC LIMIT 5",
        (uid, week_ago)
    ).fetchall()

    summary = f"总任务{len(items)}条，待处理{len(pending)}条，逾期{len(overdue)}条，已完成{len(completed)}条。\n"
    summary += f"效率指标：平均完成耗时{round(avg_dur,1)}分钟，1小时内完成{fast_count}条，超8小时完成{slow_count}条。\n"
    if chat_topics:
        summary += f"近期高频对话主题（近7天，方向：我发出=本人发送/收到=接收）：{'；'.join(chat_topics)}\n"
    if todo_titles:
        summary += f"近期待办（近7天）：{'；'.join(todo_titles)}\n"
    if cal_titles:
        summary += f"近期日程（近7天）：{'；'.join(cal_titles)}\n"
    # v27.2：ITSM 工单
    if itop_active:
        summary += f"ITSM 工单（iTop）当前处理中 {len(itop_active)} 张："
        for t in itop_active[:5]:
            summary += f"- [{t['status']}] {t['title']}（更新{t['updated']}）\n"
    if itop_recent:
        summary += f"近7天已关闭 {len(itop_recent)} 张："
        for t in itop_recent:
            summary += f"- {t['title']}（关闭{t['closed']}）\n"
    summary += "待处理列表：\n"
    for p in pending[:10]:
        summary += f"- [{p['priority']}] {p['title']}（截止{p['due_date'] or '无'}）\n"
    system = (
        '你是基础架构运维团队的工作建议助手。根据用户当前的工作项、效率指标、近期聊天记录主题、待办和日程密度，给出 3~5 条具体、可操作的工作建议。'
        '对话主题中的方向标注（我发出/收到）表示该消息是本人发送还是接收到的，分析时注意区分是谁的诉求。'
        '如果平均耗时较长或逾期较多，重点给出时间管理和任务拆分建议；如果日程密集，建议预留缓冲时间，并对周期性会议（如周例会）基于其具体日期给出会议时间优化建议。'
        '直接输出建议，每条用序号标注，不要客套话。'
    )
    try:
        suggestion = ai_chat(system, summary, max_tokens=600, feature='suggest')
        return jsonify({'suggestion': suggestion})
    except Exception as e:
        return jsonify({'error': f'AI 生成建议失败: {e}'}), 502


@app.route('/api/ai/comm-suggest', methods=['POST'])
@login_required
def ai_comm_suggest():
    """v29.6：沟通建议——分析用户近期钉钉聊天内容，给出会话话术建议"""
    db = get_db()
    uid = session['user_id']
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    chat_rows = db.execute(
        "SELECT title, content, event_time FROM user_knowledge WHERE user_id=? AND source='dingtalk_chat' AND occur_date>=? ORDER BY event_time DESC LIMIT 20",
        (uid, week_ago)
    ).fetchall()
    if not chat_rows:
        return jsonify({'suggestion': '暂无近期聊天记录，请先在「知识库」绑定钉钉并同步后再试。'})
    # v29.7：标注消息收发方向，避免 AI 把对方的话当成用户的承诺
    my_row = db.execute('SELECT display_name FROM users WHERE id = ?', (uid,)).fetchone()
    my_name = (my_row['display_name'] or '').strip() if my_row else ''
    # 组装会话素材：会话名 + 时间 + 方向 + 内容（单条截断、总量控制，防止超 token）
    parts = []
    total = 0
    for r in chat_rows:
        content = (r['content'] or '').strip()
        if not content:
            continue
        if len(content) > 500:
            content = content[:500] + '…（已截断）'
        direction = '我发出' if _is_my_message(content, my_name) else '对方发出'
        block = f"【{r['title'] or '未知会话'}】{r['event_time'] or ''}（{direction}）\n{content}"
        if total + len(block) > 6000:
            break
        parts.append(block)
        total += len(block)
    if not parts:
        return jsonify({'suggestion': '聊天记录内容为空，无法分析。'})
    system = (
        f'你是企业内部沟通教练。当前用户是「{my_name or "该用户"}」。下面是用户近期从钉钉同步的聊天记录，每条均标注了方向：'
        '「我发出」= 用户本人发送的消息，「对方发出」= 用户接收到的消息；内容含发送人与消息正文，部分可能被截断。'
        '分析时务必区分哪些话是用户说的、哪些是对方说的，不要把对方的表态当成用户的承诺或任务。请分析沟通现状：'
        '1) 识别待回复/待确认/有潜在风险的事项（重点是对方发给用户、但用户尚未回应的问题与请求）；'
        '2) 针对每个事项给出可直接使用的话术建议（如何开场、要点表达、收尾诉求），语气专业简洁；'
        '3) 如发现可改进的沟通习惯（表达方式、回复时机等）附一条简短建议。直接按序号输出，不要客套话。'
    )
    try:
        suggestion = ai_chat(system, '\n\n'.join(parts), max_tokens=800, feature='comm_suggest')
        return jsonify({'suggestion': suggestion})
    except Exception as e:
        return jsonify({'error': f'AI 生成沟通建议失败: {e}'}), 502


@app.route('/api/ai/search-items', methods=['POST'])
@login_required
def ai_search_items():
    """v29.7：AI 语义搜索工作项——SQL 关键词粗筛候选后，由 AI 判断语义相关性，返回匹配 id 与理由"""
    data = request.get_json(silent=True) or {}
    q = (data.get('q') or '').strip()
    if not q:
        return jsonify({'error': '请输入搜索内容'}), 400
    db = get_db()
    scope = data.get('scope') or ''
    query = ('SELECT w.id, w.title, w.description, w.category, w.priority, w.status, u.display_name '
             'FROM work_items w JOIN users u ON w.user_id = u.id WHERE 1=1')
    params = []
    if scope == 'personal' or not session.get('is_admin'):
        # 个人工作台视角：可见范围与 /api/work-items?scope=personal 一致
        query += " AND (w.user_id = ? OR (',' || w.collaborators || ',') LIKE ?)"
        params.append(session['user_id'])
        params.append(f'%,{session["user_id"]},%')
        if session.get('is_admin'):
            query += ' AND (w.transferred_to IS NULL OR w.transferred_to != ?)'
            params.append(session['user_id'])
    elif get_admin_scope():
        # 子管理员：强制本团队
        query += ' AND w.user_id IN (SELECT id FROM users WHERE team_id = ?)'
        params.append(get_admin_scope())
    # 关键词粗筛：任一子词命中标题/描述/分类即入候选，控制 AI 输入量
    words = [w for w in re.split(r'[\s,，、;；]+', q) if w][:6]
    if words:
        conds = []
        for w in words:
            conds.append('(w.title LIKE ? OR w.description LIKE ? OR w.category LIKE ?)')
            like = f'%{w}%'
            params.extend([like, like, like])
        query += ' AND (' + ' OR '.join(conds) + ')'
    query += ' ORDER BY w.updated_at DESC LIMIT 120'
    rows = db.execute(query, params).fetchall()
    if not rows:
        return jsonify({'ids': [], 'matches': [], 'total': 0, 'message': '未找到关键词相关的工作项，可换个说法试试。'})
    status_map = {'pending': '待处理', 'in_progress': '进行中', 'completed': '已完成'}
    lines = '\n'.join(
        f"id={r['id']} | [{r['priority']}] {r['title']} | 分类:{r['category']} | 状态:{status_map.get(r['status'], r['status'])} | 负责人:{r['display_name']} | 描述:{(r['description'] or '')[:80]}"
        for r in rows
    )
    system = (
        '你是工作项语义搜索助手。根据用户的搜索意图，从候选工作项中挑出确实相关的事项。'
        '需理解同义表述、近义概念与模糊描述（如“上周那个服务器的事”）。'
        '只输出 JSON：{"matches":[{"id":数字,"reason":"15字内理由"}]}，按相关度从高到低，最多 15 条，没有相关的输出空数组。'
    )
    try:
        result = _ai_json(system, f"搜索意图：{q}\n\n候选工作项：\n{lines}", max_tokens=800, feature='search')
    except Exception as e:
        return jsonify({'error': f'AI 搜索失败: {e}'}), 502
    valid_ids = {r['id'] for r in rows}
    matches = []
    for m in (result.get('matches') or []):
        if isinstance(m, dict) and str(m.get('id') or '').isdigit() and int(m['id']) in valid_ids:
            matches.append({'id': int(m['id']), 'reason': str(m.get('reason') or '')})
    matches = matches[:15]
    return jsonify({'ids': [m['id'] for m in matches], 'matches': matches, 'total': len(rows)})


@app.route('/api/ai/chat', methods=['POST'])
@login_required
def ai_chat_route():
    """通用 AI 聊天接口，支持润色、改写等"""
    data = request.get_json(silent=True) or {}
    prompt = (data.get('prompt') or '').strip()
    if not prompt:
        return jsonify({'error': '缺少 prompt'}), 400
    system = data.get('system') or '你是基础架构运维团队的 AI 助手。'
    try:
        reply = ai_chat(
            system,
            prompt,
            max_tokens=data.get('max_tokens', 1200),
            feature=data.get('feature', 'chat')
        )
        return jsonify({'reply': reply})
    except Exception as e:
        print(f'[AI] chat 异常: {e}')
        return jsonify({'error': f'AI 服务调用失败: {e}'}), 502


# ====================================================================
# 静态页面
# ====================================================================
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


# ====================================================================
# 认证 API
# ====================================================================
@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip().lower()
    password = data.get('password') or ''

    if not username or not password:
        return jsonify({'error': '请输入用户名和密码'}), 400

    # v29.2：用户名字符集白名单（AD sAMAccountName 合法字符），非法输入直接拒绝
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', username):
        return jsonify({'error': '用户名不合法'}), 400

    # v29.2：登录限速 —— 连续失败锁定，防暴力破解
    ip = _login_client_ip()
    locked = _login_locked_seconds(ip, username)
    if locked > 0:
        return jsonify({'error': f'连续失败次数过多，请 {locked // 60 + 1} 分钟后再试'}), 429

    ok, display_name = ldap_authenticate(username, password)
    if not ok:
        _login_record_fail(ip, username)
        return jsonify({'error': 'LDAP 认证失败，请检查用户名和密码'}), 401
    _login_record_ok(ip, username)

    db = get_db()
    user = db.execute('SELECT * FROM users WHERE ad_username = ?', (username,)).fetchone()

    if user:
        if display_name and display_name != user['display_name']:
            db.execute('UPDATE users SET display_name = ? WHERE id = ?', (display_name, user['id']))
            db.commit()
        session.permanent = True
        session['user_id'] = user['id']
        session['ad_username'] = user['ad_username']
        session['is_admin'] = bool(user['is_admin'])
        session['is_super'] = bool(dict(user).get('is_super', 0))
        session['team_id'] = user['team_id']
        session['display_name'] = display_name or user['display_name']
    elif username == ADMIN_USERNAME.lower():
        db.execute(
            'INSERT INTO users (ad_username, display_name, is_admin, is_super) VALUES (?, ?, 1, 1)',
            (username, display_name or username)
        )
        db.commit()
        user = db.execute('SELECT * FROM users WHERE ad_username = ?', (username,)).fetchone()
        session.permanent = True
        session['user_id'] = user['id']
        session['ad_username'] = username
        session['is_admin'] = True
        session['is_super'] = True  # ADMIN_USERNAME 首次创建即为主管理员
        session['team_id'] = user.get('team_id')  # 全局管理员通常为 None
        session['display_name'] = display_name or username
    else:
        return jsonify({'error': '您不在基础架构团队成员名单中，请联系管理员添加'}), 403

    return jsonify({
        'id': session['user_id'],
        'username': session['ad_username'],
        'display_name': session['display_name'],
        'is_admin': session['is_admin'],
        'is_super': session.get('is_super', False),
        'team_id': session.get('team_id')
    })


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'message': '已退出登录'})


@app.route('/api/auth/me')
@login_required
def me():
    return jsonify({
        'id': session['user_id'],
        'username': session['ad_username'],
        'display_name': session['display_name'],
        'is_admin': session.get('is_admin', False),
        'is_super': session.get('is_super', False),
        'team_id': session.get('team_id')
    })


# ====================================================================
# 成员管理（管理员）
# ====================================================================
@app.route('/api/admin/users')
@admin_required
def list_users():
    db = get_db()
    scope_team_id = get_admin_scope()
    # v28.1：子管理员只能看到本团队成员
    if scope_team_id:
        users = db.execute('SELECT * FROM users WHERE team_id = ? ORDER BY id', (scope_team_id,)).fetchall()
    else:
        users = db.execute('SELECT * FROM users ORDER BY id').fetchall()
    result = []
    for u in users:
        total = db.execute('SELECT COUNT(*) as c FROM work_items WHERE user_id = ?', (u['id'],)).fetchone()['c']
        pending = db.execute(
            "SELECT COUNT(*) as c FROM work_items WHERE user_id = ? AND status != 'completed'", (u['id'],)
        ).fetchone()['c']
        completed = db.execute(
            "SELECT COUNT(*) as c FROM work_items WHERE user_id = ? AND status = 'completed'", (u['id'],)
        ).fetchone()['c']
        overdue = db.execute(
            "SELECT COUNT(*) as c FROM work_items WHERE user_id = ? AND status != 'completed' AND due_date != '' AND due_date < ?",
            (u['id'], date.today().isoformat())
        ).fetchone()['c']
        result.append({
            'id': u['id'],
            'ad_username': u['ad_username'],
            'display_name': u['display_name'],
            'section_name': u['section_name'] or '',
            'employee_id': u['employee_id'] or '',
            'email': u['email'] or '',
            'is_admin': bool(u['is_admin']),
            'is_super': bool(dict(u).get('is_super', 0)),
            'team_id': u['team_id'],
            'job_description': dict(u).get('job_description') or '',
            'responsibilities': dict(u).get('responsibilities') or '',
            'total_items': total,
            'pending_items': pending,
            'completed_items': completed,
            'overdue_items': overdue,
            'created_at': u['created_at']
        })
    return jsonify(result)


@app.route('/api/admin/users', methods=['POST'])
@admin_required
def add_user():
    data = request.get_json(silent=True) or {}
    ad_username = (data.get('ad_username') or '').strip().lower()
    display_name = data.get('display_name', '').strip()
    section_name = data.get('section_name', '').strip()
    employee_id = (data.get('employee_id') or '').strip()
    email = (data.get('email') or '').strip().lower()
    job_description = (data.get('job_description') or '').strip()
    responsibilities = (data.get('responsibilities') or '').strip()
    # v28.1：team_id — 子管理员强制自己的 team_id，全局管理员可指定
    team_id = data.get('team_id')
    scope_team_id = get_admin_scope()
    if scope_team_id:
        team_id = scope_team_id

    if not ad_username or not display_name:
        return jsonify({'error': 'AD 用户名和显示名不能为空'}), 400

    db = get_db()
    try:
        db.execute(
            'INSERT INTO users (ad_username, display_name, section_name, employee_id, email, job_description, responsibilities, team_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (ad_username, display_name, section_name, employee_id, email, job_description, responsibilities, team_id)
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({'error': '该 AD 用户名已存在'}), 400

    user = db.execute('SELECT * FROM users WHERE ad_username = ?', (ad_username,)).fetchone()
    return jsonify({
        'id': user['id'],
        'ad_username': user['ad_username'],
        'display_name': user['display_name'],
        'section_name': user['section_name'],
        'employee_id': user['employee_id'] or '',
        'email': user['email'] or '',
        'job_description': dict(user).get('job_description') or '',
        'is_admin': bool(user['is_admin'])
    }), 201


@app.route('/api/admin/users/sync-ad', methods=['POST'])
@admin_required
def admin_users_sync_ad():
    """v28.6：从域控批量回填成员工号/岗位/邮箱（仅填空缺字段，不覆盖已填内容）；
    子管理员只同步本团队成员"""
    db = get_db()
    scope_team_id = get_admin_scope()
    if scope_team_id:
        users = db.execute(
            'SELECT id, ad_username, display_name, employee_id, email, job_description '
            'FROM users WHERE ad_username IS NOT NULL AND ad_username != "" AND team_id = ?',
            (scope_team_id,)).fetchall()
    else:
        users = db.execute(
            'SELECT id, ad_username, display_name, employee_id, email, job_description '
            'FROM users WHERE ad_username IS NOT NULL AND ad_username != ""').fetchall()
    if not users:
        return jsonify({'updated': 0, 'total': 0, 'details': []})

    by_ad = {u['ad_username'].lower(): u for u in users}
    details, updated = [], 0
    conn = None
    try:
        conn = _ldap_admin_conn()
        ads = list(by_ad.keys())
        for i in range(0, len(ads), 50):
            batch = ads[i:i + 50]
            flt = '(&(objectClass=user)(|' + ''.join(
                '(sAMAccountName=%s)' % escape_filter_chars(a) for a in batch) + '))'
            conn.search(search_base=LDAP_BASE, search_filter=flt,
                        attributes=['sAMAccountName', 'displayName', 'employeeID', 'title', 'mail'])
            for e in conn.entries:
                ad = str(e.sAMAccountName.value or '').strip().lower()
                u = by_ad.get(ad)
                if not u:
                    continue
                emp = str(e.employeeID.value or '').strip() if e.employeeID else ''
                title = str(e.title.value or '').strip() if e.title else ''
                mail = str(e.mail.value or '').strip() if e.mail else ''
                sets, vals, filled = [], [], []
                if emp and not (u['employee_id'] or '').strip():
                    sets.append('employee_id = ?'); vals.append(emp); filled.append('工号')
                if title and not (u['job_description'] or '').strip():
                    sets.append('job_description = ?'); vals.append(title); filled.append('岗位')
                if mail and not (u['email'] or '').strip():
                    sets.append('email = ?'); vals.append(mail); filled.append('邮箱')
                if sets:
                    vals.append(u['id'])
                    db.execute('UPDATE users SET ' + ', '.join(sets) + ' WHERE id = ?', vals)
                    updated += 1
                    details.append({'ad_username': ad, 'display_name': u['display_name'], 'filled': filled})
        db.commit()
    except Exception as e:
        return jsonify({'error': f'AD 同步失败: {e}'}), 502
    finally:
        if conn:
            try:
                conn.unbind()
            except Exception:
                pass
    return jsonify({'updated': updated, 'total': len(users), 'details': details})


@app.route('/api/admin/users/<int:user_id>', methods=['PUT'])
@admin_required
def update_user(user_id):
    # v28.1：子管理员只能更新自己团队的用户
    scope_team_id = get_admin_scope()
    if scope_team_id:
        target = get_db().execute('SELECT team_id FROM users WHERE id = ?', (user_id,)).fetchone()
        if not target or target['team_id'] != scope_team_id:
            return jsonify({'error': '无权修改其他团队的成员'}), 403
    data = request.get_json(silent=True) or {}
    db = get_db()
    updates = []
    params = []
    for field in ['display_name', 'section_name', 'employee_id', 'email', 'job_description', 'responsibilities']:
        if field in data:
            updates.append(f'{field} = ?')
            params.append(data[field])
    if 'is_admin' in data:
        updates.append('is_admin = ?')
        params.append(1 if data['is_admin'] else 0)
    if not updates:
        return jsonify({'error': '没有可更新的字段'}), 400
    params.append(user_id)
    db.execute(f'UPDATE users SET {", ".join(updates)} WHERE id = ?', params)
    db.commit()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    return jsonify({
        'id': user['id'],
        'ad_username': user['ad_username'],
        'display_name': user['display_name'],
        'section_name': user['section_name'],
        'employee_id': user['employee_id'] or '',
        'email': user['email'] or '',
        'job_description': dict(user).get('job_description') or '',
        'is_admin': bool(user['is_admin'])
    })


@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@admin_required
def delete_user(user_id):
    # v28.1：子管理员只能删除自己团队的用户
    scope_team_id = get_admin_scope()
    if scope_team_id:
        target = get_db().execute('SELECT team_id FROM users WHERE id = ?', (user_id,)).fetchone()
        if not target or target['team_id'] != scope_team_id:
            return jsonify({'error': '无权删除其他团队的成员'}), 403
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    if user['is_admin']:
        return jsonify({'error': '不能删除管理员账号'}), 400
    db.execute('DELETE FROM work_items WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM work_logs WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM quick_links WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM api_keys WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM reports WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM ai_usage WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    return jsonify({'message': '已删除'})


# ====================================================================
# 负责板块自定义管理（v16）
# ====================================================================
@app.route('/api/responsibility-areas', methods=['GET'])
@login_required
def list_responsibility_areas():
    db = get_db()
    scope_team_id = get_admin_scope()
    # v28.1：子管理员看到本团队板块 + 全局板块（team_id=NULL）；全局管理员看到全部
    if scope_team_id:
        rows = db.execute(
            'SELECT id, name, team_id, created_at FROM responsibility_areas WHERE team_id IS NULL OR team_id = ? ORDER BY id',
            (scope_team_id,)
        ).fetchall()
    else:
        rows = db.execute('SELECT id, name, team_id, created_at FROM responsibility_areas ORDER BY id').fetchall()
    return jsonify([{'id': r['id'], 'name': r['name'], 'team_id': r['team_id'], 'created_at': r['created_at']} for r in rows])


@app.route('/api/responsibility-areas', methods=['POST'])
@admin_required
def add_responsibility_area():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': '板块名称不能为空'}), 400
    # v28.1：子管理员创建的板块自动归属其团队；全局管理员可指定 team_id
    scope_team_id = get_admin_scope()
    team_id = data.get('team_id')
    if scope_team_id:
        team_id = scope_team_id  # 子管理员强制使用自己的 team_id
    db = get_db()
    try:
        cur = db.execute('INSERT INTO responsibility_areas (name, team_id) VALUES (?, ?)', (name, team_id))
        db.commit()
        return jsonify({'id': cur.lastrowid, 'name': name, 'team_id': team_id})
    except sqlite3.IntegrityError:
        return jsonify({'error': '板块名称已存在'}), 400


@app.route('/api/responsibility-areas/<int:area_id>', methods=['PUT'])
@admin_required
def update_responsibility_area(area_id):
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': '板块名称不能为空'}), 400
    db = get_db()
    # v28.1：子管理员只能修改本团队板块
    scope_team_id = get_admin_scope()
    if scope_team_id:
        area = db.execute('SELECT * FROM responsibility_areas WHERE id = ?', (area_id,)).fetchone()
        if not area:
            return jsonify({'error': '板块不存在'}), 404
        if area['team_id'] and area['team_id'] != scope_team_id:
            return jsonify({'error': '无权修改其他团队的板块'}), 403
    try:
        db.execute('UPDATE responsibility_areas SET name = ? WHERE id = ?', (name, area_id))
        db.commit()
        return jsonify({'id': area_id, 'name': name})
    except sqlite3.IntegrityError:
        return jsonify({'error': '板块名称已存在'}), 400


@app.route('/api/responsibility-areas/<int:area_id>', methods=['DELETE'])
@admin_required
def delete_responsibility_area(area_id):
    db = get_db()
    # v28.1：子管理员只能删除本团队板块
    scope_team_id = get_admin_scope()
    if scope_team_id:
        area = db.execute('SELECT * FROM responsibility_areas WHERE id = ?', (area_id,)).fetchone()
        if not area:
            return jsonify({'error': '板块不存在'}), 404
        if area['team_id'] and area['team_id'] != scope_team_id:
            return jsonify({'error': '无权删除其他团队的板块'}), 403
    db.execute('DELETE FROM responsibility_areas WHERE id = ?', (area_id,))
    db.commit()
    return jsonify({'message': '已删除'})


@app.route('/api/team/members', methods=['GET'])
@login_required
def team_members_simple():
    """所有登录用户均可查看团队成员简表（用于转办/协同选择）"""
    db = get_db()
    rows = db.execute(
        'SELECT id, display_name, ad_username, section_name, team_id, responsibilities FROM users ORDER BY display_name'
    ).fetchall()
    return jsonify([{'id': r['id'], 'display_name': r['display_name'], 'ad_username': r['ad_username'],
                     'section_name': r['section_name'] or '', 'team_id': r['team_id'],
                     'responsibilities': dict(r).get('responsibilities') or ''} for r in rows])


# ====================================================================
# 员工详情 + AI 工作分析（团队概览点击进入）
# ====================================================================
@app.route('/api/team/<int:user_id>/details')
@login_required
def team_member_details(user_id):
    """查看指定员工的工作详情"""
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        return jsonify({'error': '用户不存在'}), 404

    # v29.2：本人 / 全局管理员 / 本团队子管理员（修复子管理员跨团队越权）
    if not _can_view_user(user):
        return jsonify({'error': '无权查看'}), 403

    today = date.today().isoformat()
    # v26.4：统计口径与团队概览卡片/个人工作台对齐 —— 本人任务 + 作为协同者的任务
    collab_like = f'%,{user_id},%'
    items = db.execute("""
        SELECT w.*, u.display_name, u.ad_username, u.section_name
        FROM work_items w JOIN users u ON w.user_id = u.id
        WHERE w.user_id = ? OR (',' || w.collaborators || ',') LIKE ?
        ORDER BY CASE w.priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                 w.created_at DESC
    """, (user_id, collab_like)).fetchall()

    total = len(items)
    # v26.4：待处理只统计 status='pending'，与团队卡片/主区域口径一致（进行中不计入）
    pending = sum(1 for i in items if i['status'] == 'pending')
    completed = sum(1 for i in items if i['status'] == 'completed')
    overdue = sum(1 for i in items if i['status'] != 'completed' and i['due_date'] and i['due_date'] < today)

    # 分类统计
    by_category = db.execute("""
        SELECT category, COUNT(*) as count,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed
        FROM work_items WHERE user_id = ? OR (',' || collaborators || ',') LIKE ? GROUP BY category
    """, (user_id, collab_like)).fetchall()

    # 最近7天完成趋势
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    recent_completed = db.execute("""
        SELECT date(completed_at) as day, COUNT(*) as count
        FROM work_items WHERE (user_id = ? OR (',' || collaborators || ',') LIKE ?) AND status = 'completed' AND completed_at >= ?
        GROUP BY date(completed_at) ORDER BY day
    """, (user_id, collab_like, week_ago)).fetchall()

    # 交付物
    deliverables = db.execute("""
        SELECT d.*, w.title as work_title FROM deliverables d
        JOIN work_items w ON d.work_item_id = w.id
        WHERE d.user_id = ? ORDER BY d.created_at DESC LIMIT 20
    """, (user_id,)).fetchall()

    return jsonify({
        'user': dict(user),
        'stats': {
            'total': total,
            'pending': pending,
            'completed': completed,
            'overdue': overdue,
            'completion_rate': round(completed / total * 100) if total > 0 else 0
        },
        'items': [dict(i) for i in items],
        'by_category': [dict(c) for c in by_category],
        'recent_completed': [dict(r) for r in recent_completed],
        'deliverables': [dict(d) for d in deliverables]
    })


@app.route('/api/team/<int:user_id>/analysis', methods=['POST'])
@login_required
def team_member_analysis(user_id):
    """AI 分析指定员工的工作情况（结合工作效率）"""
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    # v29.2：本人 / 全局管理员 / 本团队子管理员（修复子管理员跨团队越权）
    if not _can_view_user(user):
        return jsonify({'error': '无权操作'}), 403

    today = date.today().isoformat()
    items = db.execute(
        "SELECT title, status, priority, due_date, category, description, actual_duration_minutes, completed_at FROM work_items WHERE user_id=?",
        (user_id,)
    ).fetchall()

    if not items:
        return jsonify({'analysis': '该员工暂无工作内容，无法分析。'})

    pending = [i for i in items if i['status'] != 'completed']
    overdue = [i for i in pending if i['due_date'] and i['due_date'] < today]
    completed = [i for i in items if i['status'] == 'completed']
    # 效率指标
    avg_dur = sum(i['actual_duration_minutes'] or 0 for i in completed) / max(len(completed), 1)
    fast_count = sum(1 for i in completed if (i['actual_duration_minutes'] or 9999) <= 60)
    slow_count = sum(1 for i in completed if (i['actual_duration_minutes'] or 0) >= 480)
    recent_completed = [i for i in completed if i['completed_at'] and i['completed_at'][:10] >= (date.today() - timedelta(days=7)).isoformat()]

    # v27.2：iTop ITSM 工单
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    itop_active = db.execute(
        "SELECT title, status, SUBSTRING(last_update,1,10) as updated FROM itop_tickets "
        "WHERE user_id=? AND status NOT IN ('resolved','closed','reject') ORDER BY last_update DESC LIMIT 10",
        (user_id,)
    ).fetchall()
    itop_recent = db.execute(
        "SELECT title, SUBSTRING(close_date,1,10) as closed FROM itop_tickets "
        "WHERE user_id=? AND status IN ('resolved','closed') AND close_date>=? ORDER BY close_date DESC LIMIT 5",
        (user_id, week_ago)
    ).fetchall()

    summary = f"员工：{user['display_name']}（{user['section_name']}）\n"
    summary += f"总任务{len(items)}条，已完成{len(completed)}条，待处理{len(pending)}条，逾期{len(overdue)}条。\n"
    summary += f"效率指标：平均完成耗时{round(avg_dur,1)}分钟，1小时内完成{fast_count}条，超8小时完成{slow_count}条，近7日完成{len(recent_completed)}条。\n"
    summary += "\n待处理任务：\n"
    for p in pending[:15]:
        summary += f"- [{p['priority']}] {p['title']}（分类：{p['category']}，截止：{p['due_date'] or '无'}）\n"
    summary += "\n已完成任务（近5条）：\n"
    for c in completed[:5]:
        dur = f"，耗时{c['actual_duration_minutes']}分钟" if c['actual_duration_minutes'] else ""
        summary += f"- {c['title']}{dur}\n"
    # v27.2：ITSM 工单
    if itop_active:
        summary += f"\nITSM 工单（iTop）当前处理中 {len(itop_active)} 张：\n"
        for t in itop_active[:5]:
            summary += f"- [{t['status']}] {t['title']}（更新{t['updated']}）\n"
    if itop_recent:
        summary += f"近7天已关闭 {len(itop_recent)} 张：\n"
        for t in itop_recent:
            summary += f"- {t['title']}（关闭{t['closed']}）\n"

    system = (
        '你是基础架构运维团队的管理顾问。根据员工的工作数据和效率指标，给出专业的工作意见分析，包括：'
        '1) 工作负荷评估；2) 效率分析（平均耗时、快速完成率）；3) 优先级管理建议；4) 逾期风险提示；'
        '5) 个人能力发展方向；6) 具体改进建议。输出 300~500 字，分点叙述，语言简洁专业。'
    )
    try:
        analysis = ai_chat(system, summary, max_tokens=1000, feature='analysis')
        return jsonify({'analysis': analysis})
    except Exception as e:
        return jsonify({'error': f'AI 分析失败: {e}'}), 502


@app.route('/api/team/<int:user_id>/job-analysis', methods=['POST'])
@login_required
def team_member_job_analysis(user_id):
    """根据岗位描述判断工作合理性，推荐协同人员或转办建议"""
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    # v29.2：本人 / 全局管理员 / 本团队子管理员（修复子管理员跨团队越权）
    if not _can_view_user(user):
        return jsonify({'error': '无权操作'}), 403

    job_desc = (dict(user).get('job_description') or '').strip()
    if not job_desc:
        return jsonify({'error': '该员工尚未填写岗位描述，请在成员管理中补充'}), 400

    items = db.execute(
        "SELECT title, status, priority, due_date, category, description, actual_duration_minutes FROM work_items WHERE user_id=? AND status != 'completed'",
        (user_id,)
    ).fetchall()
    all_items = db.execute(
        "SELECT title, status, priority, due_date, category, actual_duration_minutes FROM work_items WHERE user_id=?",
        (user_id,)
    ).fetchall()
    completed_items = [i for i in all_items if i['status'] == 'completed']
    avg_dur = sum(i['actual_duration_minutes'] or 0 for i in completed_items) / max(len(completed_items), 1)
    fast_count = sum(1 for i in completed_items if (i['actual_duration_minutes'] or 9999) <= 60)
    slow_count = sum(1 for i in completed_items if (i['actual_duration_minutes'] or 0) >= 480)

    week_ago = (date.today() - timedelta(days=7)).isoformat()
    # v25.7：聊天记录过滤——单聊保留，群聊只保留自己发送的
    chat_rows = db.execute(
        "SELECT title, content, event_time FROM user_knowledge WHERE user_id=? AND source='dingtalk_chat' AND occur_date>=? ORDER BY event_time DESC LIMIT 20",
        (user_id, week_ago)
    ).fetchall()
    chat_rows = _filter_chat_records(chat_rows, user_id, db)
    # v29.7：主题标注收发方向，让 AI 区分是员工本人的诉求还是接收到的消息
    my_name = (user['display_name'] or '').strip()
    chat_topics = []
    for r in chat_rows:
        if not r['title']:
            continue
        tag = '我发出' if _is_my_message(r.get('content') or '', my_name) else '收到'
        chat_topics.append(f"{r['title'][:30]}（{tag}）")
    chat_topics = list(dict.fromkeys(chat_topics))[:8]
    # v25.7-fix：待办过滤已完成的；日程携带日期时间
    todo_rows = db.execute(
        "SELECT title, content FROM user_knowledge WHERE user_id=? AND source='dingtalk_todo' AND occur_date>=? ORDER BY event_time DESC LIMIT 10",
        (user_id, week_ago)
    ).fetchall()
    todo_titles = [r['title'][:40] for r in todo_rows if r['title'] and '状态：已完成' not in (r['content'] or '')][:6]
    cal_rows = db.execute(
        "SELECT title, event_time FROM user_knowledge WHERE user_id=? AND source='dingtalk_calendar' AND occur_date>=? ORDER BY event_time DESC LIMIT 10",
        (user_id, week_ago)
    ).fetchall()
    def _fmt_cal(title, event_time):
        if not event_time:
            return title
        m = re.match(r'(\d{4})-(\d{2})-(\d{2})[T ](\d{2}:\d{2})', str(event_time))
        if not m:
            return title
        year, mon, day, hm = m.groups()
        try:
            dt = datetime(int(year), int(mon), int(day))
            weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
            weekday = weekday_names[dt.weekday()]
        except Exception:
            weekday = ''
        return f"{int(mon)}月{int(day)}日({weekday}) {hm} {title}"
    cal_titles = [_fmt_cal(r['title'][:40], r['event_time']) for r in cal_rows if r['title']][:6]

    # v27.2：iTop ITSM 工单
    itop_active = db.execute(
        "SELECT title, status, SUBSTRING(last_update,1,10) as updated FROM itop_tickets "
        "WHERE user_id=? AND status NOT IN ('resolved','closed','reject') ORDER BY last_update DESC LIMIT 10",
        (user_id,)
    ).fetchall()
    itop_recent = db.execute(
        "SELECT title, SUBSTRING(close_date,1,10) as closed FROM itop_tickets "
        "WHERE user_id=? AND status IN ('resolved','closed') AND close_date>=? ORDER BY close_date DESC LIMIT 5",
        (user_id, week_ago)
    ).fetchall()

    # v20：只分析该成员本人的工作，不再引入其他成员/推荐协同转办
    work_summary = '\n'.join(
        f"- [{i['priority']}] {i['title']}（分类：{i['category']}）"
        for i in items
    ) or '当前无待处理工作'

    prompt = (
        f"员工：{user['display_name']}（{user['section_name']}）\n"
        f"岗位描述：{job_desc}\n"
        f"负责板块：{dict(user).get('responsibilities') or '未配置'}\n\n"
        f"当前待处理工作（{len(items)}条）：\n{work_summary}\n\n"
        f"工作效率：总任务{len(all_items)}条，已完成{len(completed_items)}条，平均耗时{round(avg_dur,1)}分钟，"
        f"1小时内完成{fast_count}条，超8小时{slow_count}条。\n"
    )
    if chat_topics:
        prompt += f"近期高频对话主题（近7天，方向：我发出=本人发送/收到=接收）：{'；'.join(chat_topics)}\n"
    if todo_titles:
        prompt += f"近期待办：{'；'.join(todo_titles)}\n"
    if cal_titles:
        prompt += f"近期日程：{'；'.join(cal_titles)}\n"
    # v27.2：ITSM 工单
    if itop_active:
        prompt += f"ITSM 工单（iTop）当前处理中 {len(itop_active)} 张："
        for t in itop_active[:5]:
            prompt += f"- [{t['status']}] {t['title']}（更新{t['updated']}）\n"
    if itop_recent:
        prompt += f"近7天已关闭 {len(itop_recent)} 张："
        for t in itop_recent:
            prompt += f"- {t['title']}（关闭{t['closed']}）\n"
    prompt += (
        f"\n请只针对该员工本人，根据其岗位描述、负责板块及近期工作上下文，分析其当前工作状况，给出："
        f"1) 工作负荷评估（待处理数量、优先级分布、是否存在积压）；"
        f"2) 岗位匹配度分析（哪些工作与岗位描述一致，哪些不一致及原因）；"
        f"3) 工作状态问题（逾期项、低效项、重复性事务占比）；"
        f"4) 优化建议（时间管理、优先级调整、流程改进等，只针对该员工本人可执行的措施）。"
        f"不要涉及其他成员，不要推荐协同或转办。"
        f"输出格式为 Markdown 列表，语言简洁专业。"
    )
    system = '你是企业组织架构与岗位匹配顾问，擅长根据岗位描述、负责板块、工作效率及沟通上下文判断工作分配合理性并给出优化建议。'
    try:
        analysis = ai_chat(system, prompt, max_tokens=1200, feature='job_analysis')
        return jsonify({'analysis': analysis})
    except Exception as e:
        return jsonify({'error': f'AI 分析失败: {e}'}), 502


# ====================================================================
# v28.0：组织架构管理
# ====================================================================
@app.route('/api/org/teams')
@login_required
def list_teams():
    """获取所有团队及其成员（v28.1：子管理员只看自己的团队）"""
    db = get_db()
    scope_team_id = get_admin_scope()
    if scope_team_id:
        teams = db.execute('SELECT * FROM teams WHERE id = ? ORDER BY id', (scope_team_id,)).fetchall()
    else:
        teams = db.execute('SELECT * FROM teams ORDER BY id').fetchall()
    result = []
    for t in teams:
        members = db.execute(
            'SELECT id, ad_username, display_name, section_name, is_admin, is_super, email, team_id '
            'FROM users WHERE team_id = ? ORDER BY display_name',
            (t['id'],)
        ).fetchall()
        result.append({
            'id': t['id'],
            'name': t['name'],
            'parent_id': t['parent_id'],
            'description': t['description'],
            'member_count': len(members),
            'members': [dict(m) for m in members]
        })
    return jsonify(result)


@app.route('/api/org/teams', methods=['POST'])
@admin_required
def create_team():
    """创建团队"""
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': '团队名称不能为空'}), 400
    desc = (data.get('description') or '').strip()
    parent_id = data.get('parent_id')
    db = get_db()
    try:
        db.execute('INSERT INTO teams (name, description, parent_id) VALUES (?, ?, ?)',
                   (name, desc, parent_id))
        db.commit()
        return jsonify({'ok': True, 'id': db.execute('SELECT LAST_INSERT_ID() as id').fetchone()['id']})
    except Exception as e:
        if 'Duplicate' in str(e):
            return jsonify({'error': f'团队 "{name}" 已存在'}), 409
        return jsonify({'error': str(e)}), 500


@app.route('/api/org/teams/<int:team_id>', methods=['PUT'])
@admin_required
def update_team(team_id):
    """更新团队信息"""
    scope_team_id = get_admin_scope()
    if scope_team_id and scope_team_id != team_id:
        return jsonify({'error': '无权操作其他团队'}), 403
    data = request.get_json(silent=True) or {}
    db = get_db()
    team = db.execute('SELECT * FROM teams WHERE id = ?', (team_id,)).fetchone()
    if not team:
        return jsonify({'error': '团队不存在'}), 404
    name = (data.get('name') or '').strip()
    desc = (data.get('description') or '').strip()
    parent_id = data.get('parent_id')
    try:
        db.execute('UPDATE teams SET name = ?, description = ?, parent_id = ? WHERE id = ?',
                   (name, desc, parent_id, team_id))
        db.commit()
        return jsonify({'ok': True})
    except Exception as e:
        if 'Duplicate' in str(e):
            return jsonify({'error': f'团队名称已存在'}), 409
        return jsonify({'error': str(e)}), 500


@app.route('/api/org/teams/<int:team_id>', methods=['DELETE'])
@admin_required
def delete_team(team_id):
    """删除团队（成员 team_id 置空）"""
    scope_team_id = get_admin_scope()
    if scope_team_id and scope_team_id != team_id:
        return jsonify({'error': '无权操作其他团队'}), 403
    db = get_db()
    team = db.execute('SELECT * FROM teams WHERE id = ?', (team_id,)).fetchone()
    if not team:
        return jsonify({'error': '团队不存在'}), 404
    # 检查子团队
    children = db.execute('SELECT COUNT(*) as c FROM teams WHERE parent_id = ?', (team_id,)).fetchone()['c']
    if children > 0:
        return jsonify({'error': f'该团队下有 {children} 个子团队，请先删除子团队'}), 400
    # 成员 team_id 置空
    db.execute('UPDATE users SET team_id = NULL WHERE team_id = ?', (team_id,))
    db.execute('DELETE FROM teams WHERE id = ?', (team_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/org/teams/<int:team_id>/members', methods=['POST'])
@admin_required
def add_team_member(team_id):
    """添加成员到团队（设置用户的 team_id）"""
    scope_team_id = get_admin_scope()
    if scope_team_id and scope_team_id != team_id:
        return jsonify({'error': '无权操作其他团队'}), 403
    data = request.get_json(silent=True) or {}
    user_id = data.get('user_id')
    is_team_admin = data.get('is_team_admin', False)
    if not user_id:
        return jsonify({'error': '缺少 user_id'}), 400
    db = get_db()
    team = db.execute('SELECT * FROM teams WHERE id = ?', (team_id,)).fetchone()
    if not team:
        return jsonify({'error': '团队不存在'}), 404
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    # 设置 team_id 和 is_admin（如果是子管理员）
    db.execute('UPDATE users SET team_id = ?, is_admin = ? WHERE id = ?',
               (team_id, 1 if is_team_admin else user['is_admin'], user_id))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/org/teams/<int:team_id>/members/<int:user_id>', methods=['DELETE'])
@admin_required
def remove_team_member(team_id, user_id):
    """从团队移除成员（team_id 置空，如果是子管理员则降级为普通用户）"""
    scope_team_id = get_admin_scope()
    if scope_team_id and scope_team_id != team_id:
        return jsonify({'error': '无权操作其他团队'}), 403
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    if dict(user).get('is_super', 0):
        return jsonify({'error': '主管理员不可移除出团队'}), 403
    if user['team_id'] != team_id:
        return jsonify({'error': '该用户不在此团队中'}), 400
    # team_id 置空；如果是此团队的子管理员，降为普通用户
    db.execute('UPDATE users SET team_id = NULL WHERE id = ?', (user_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/org/teams/<int:team_id>/members/<int:user_id>', methods=['PUT'])
@admin_required
def update_team_member_role(team_id, user_id):
    """更新团队成员角色（子管理员/普通成员）"""
    scope_team_id = get_admin_scope()
    if scope_team_id and scope_team_id != team_id:
        return jsonify({'error': '无权操作其他团队'}), 403
    data = request.get_json(silent=True) or {}
    is_team_admin = data.get('is_team_admin', False)
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    if dict(user).get('is_super', 0):
        return jsonify({'error': '主管理员角色不可变更'}), 403
    if user['team_id'] != team_id:
        return jsonify({'error': '该用户不在此团队中'}), 400
    db.execute('UPDATE users SET is_admin = ? WHERE id = ?',
               (1 if is_team_admin else 0, user_id))
    db.commit()
    return jsonify({'ok': True})


# ====================================================================
# 工作项
# ====================================================================
@app.route('/api/work-items')
@login_required
def list_work_items():
    db = get_db()
    user_filter = request.args.get('user_id')
    status_filter = request.args.get('status')
    scope = request.args.get('scope')  # 'personal' = 个人工作台（仅本人任务）

    query = 'SELECT w.*, u.display_name, u.ad_username, u.section_name FROM work_items w JOIN users u ON w.user_id = u.id WHERE 1=1'
    params = []

    if scope == 'personal':
        # v18/v25.4：个人工作台 = 本人任务 + 被协同给自己的任务，不再混入全员数据（内容管理才是全员视图）
        # - 管理员：本人任务 + 被协同给管理员的任务；继续排除 transferred_to == 本人的转办任务（归「工作内容管理」）
        # - 普通成员：本人任务 + 作为协同者的任务（协同任务归属人不变，仍需可见）
        # 使用 ',' || collaborators || ',' LIKE '%,id,%' 避免子串误匹配（如 1 匹配到 10/21）
        my_id = session['user_id']
        if session.get('is_admin'):
            query += " AND (w.user_id = ? OR (',' || w.collaborators || ',') LIKE ?) AND (w.transferred_to IS NULL OR w.transferred_to != ?)"
            params.append(my_id)
            params.append(f'%,{my_id},%')
            params.append(my_id)
        else:
            query += " AND (w.user_id = ? OR (',' || w.collaborators || ',') LIKE ?)"
            params.append(my_id)
            params.append(f'%,{my_id},%')
    elif session.get('is_admin') and user_filter:
        query += ' AND w.user_id = ?'
        params.append(user_filter)
    elif session.get('is_admin') and request.args.get('team_id'):
        # v28.4：工作内容管理按团队筛选
        team_filter = int(request.args.get('team_id'))
        scope_team_id = get_admin_scope()
        if scope_team_id and team_filter != scope_team_id:
            team_filter = scope_team_id  # 子管理员强制本团队
        query += ' AND w.user_id IN (SELECT id FROM users WHERE team_id = ?)'
        params.append(team_filter)
    elif session.get('is_admin') and get_admin_scope():
        # v28.1：子管理员无指定用户时，只看自己团队成员的工作项
        scope_team_id = get_admin_scope()
        query += ' AND w.user_id IN (SELECT id FROM users WHERE team_id = ?)'
        params.append(scope_team_id)
    elif not session.get('is_admin'):
        # 普通用户：查看自己的任务 + 作为协同者的任务
        query += " AND (w.user_id = ? OR (',' || w.collaborators || ',') LIKE ?)"
        params.append(session['user_id'])
        params.append(f'%,{session["user_id"]},%')

    if status_filter and status_filter != 'all':
        query += ' AND w.status = ?'
        params.append(status_filter)

    # v29.7：分类/优先级筛选（工作台与工作内容管理共用）
    category_filter = request.args.get('category')
    priority_filter = request.args.get('priority')
    if category_filter:
        query += ' AND w.category = ?'
        params.append(category_filter)
    if priority_filter:
        query += ' AND w.priority = ?'
        params.append(priority_filter)

    query += ' ORDER BY CASE w.priority WHEN "P0" THEN 0 WHEN "P1" THEN 1 WHEN "P2" THEN 2 ELSE 3 END, w.sort_order, w.created_at DESC'
    items = db.execute(query, params).fetchall()
    return jsonify([dict(i) for i in items])


def _insert_work_item(db, user_id, data, created_by):
    title = (data.get('title') or '').strip()
    if not title:
        return None
    recurring = data.get('recurring', '')
    if recurring not in RECURRING_TYPES:
        recurring = ''
    due_date = (data.get('due_date') or '').strip()
    next_run = _calc_next_run(recurring, due_date) if recurring else ''
    parent_id = data.get('parent_id')
    if parent_id is not None:
        parent_id = int(parent_id)
        # 校验父任务存在
        p = db.execute('SELECT id FROM work_items WHERE id = ?', (parent_id,)).fetchone()
        if not p:
            parent_id = None
    cur = db.execute("""
        INSERT INTO work_items (user_id, title, description, category, priority, status, due_date, created_by, recurring, next_run_at, parent_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id, title,
        (data.get('description') or '').strip(),
        data.get('category', '日常运维'),
        data.get('priority', 'P2'),
        data.get('status', 'pending'),
        due_date,
        created_by,
        recurring,
        next_run,
        parent_id,
        _now_str()
    ))
    db.execute('INSERT INTO work_logs (user_id, action, item_id, detail) VALUES (?, ?, ?, ?)',
               (user_id, 'created', cur.lastrowid, title))
    return cur.lastrowid


@app.route('/api/work-items', methods=['POST'])
@login_required
def add_my_work_item():
    data = request.get_json(silent=True) or {}
    if not (data.get('title') or '').strip():
        return jsonify({'error': '请填写工作标题'}), 400
    db = get_db()
    item_id = _insert_work_item(db, session['user_id'], data, session['ad_username'])
    db.commit()
    item = db.execute(
        'SELECT w.*, u.display_name, u.ad_username, u.section_name FROM work_items w JOIN users u ON w.user_id = u.id WHERE w.id = ?',
        (item_id,)
    ).fetchone()
    return jsonify(dict(item)), 201


@app.route('/api/work-items/batch', methods=['POST'])
@login_required
def batch_add_my_work_items():
    data = request.get_json(silent=True) or {}
    items = data.get('items') or []
    if not isinstance(items, list) or not items or len(items) > 20:
        return jsonify({'error': '批量数据不合法（1~20 条）'}), 400
    db = get_db()
    ids = []
    for it in items:
        item_id = _insert_work_item(db, session['user_id'], it, session['ad_username'])
        if item_id:
            ids.append(item_id)
    db.commit()
    return jsonify({'created': len(ids), 'ids': ids}), 201


@app.route('/api/admin/work-items', methods=['POST'])
@admin_required
def add_work_item():
    data = request.get_json(silent=True) or {}
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({'error': '必须选择成员'}), 400
    # v28.1：子管理员只能给自己团队的成员创建工作项
    scope_team_id = get_admin_scope()
    if scope_team_id:
        target_user = get_db().execute('SELECT team_id FROM users WHERE id = ?', (int(user_id),)).fetchone()
        if not target_user or target_user['team_id'] != scope_team_id:
            return jsonify({'error': '无权为其他团队的成员创建工作项'}), 403
    if not (data.get('title') or '').strip():
        return jsonify({'error': '必须填写标题'}), 400
    db = get_db()
    item_id = _insert_work_item(db, int(user_id), data, session['ad_username'])
    if not item_id:
        return jsonify({'error': '必须填写标题'}), 400
    db.commit()
    item = db.execute(
        'SELECT w.*, u.display_name, u.ad_username, u.section_name FROM work_items w JOIN users u ON w.user_id = u.id WHERE w.id = ?',
        (item_id,)
    ).fetchone()
    return jsonify(dict(item)), 201


@app.route('/api/work-items/<int:item_id>', methods=['PUT'])
@login_required
def update_work_item(item_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    item = db.execute('SELECT * FROM work_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        return jsonify({'error': '工作项不存在'}), 404

    is_owner = session.get('is_admin') or item['user_id'] == session['user_id']
    if not is_owner:
        return jsonify({'error': '无权修改他人的工作项'}), 403

    now = _now_str()
    updates, params = [], []

    for field in ['title', 'description', 'category', 'priority', 'due_date', 'completion_note']:
        if field in data:
            updates.append(f'{field} = ?')
            params.append(data[field])

    if 'status' in data:
        ns = data['status']
        if ns not in ('pending', 'in_progress', 'completed'):
            return jsonify({'error': '无效的状态值'}), 400
        updates.append('status = ?')
        params.append(ns)
        updates.append('completed_at = ?')
        params.append(now if ns == 'completed' else None)
        # 时效管理
        if ns == 'in_progress' and item['status'] != 'in_progress':
            updates.append('started_at = ?')
            params.append(now)
        elif ns == 'completed':
            try:
                # v26.7：若无 started_at（未点开始直接完成），用 created_at 兜底
                started_str = item['started_at'] or item['created_at']
                started = datetime.fromisoformat(started_str)
                completed = datetime.fromisoformat(now)
                duration = int((completed - started).total_seconds() / 60)
                updates.append('actual_duration_minutes = ?')
                params.append(max(duration, 1))
            except Exception:
                pass
        elif ns == 'pending':
            # 重置为待办时清除开始时间和耗时
            updates.append('started_at = ?')
            params.append(None)
            updates.append('actual_duration_minutes = ?')
            params.append(0)

    if 'recurring' in data:
        rec = data['recurring']
        if rec not in RECURRING_TYPES:
            rec = ''
        updates.append('recurring = ?')
        params.append(rec)
        due = data.get('due_date') or item['due_date'] or date.today().isoformat()
        updates.append('next_run_at = ?')
        params.append(_calc_next_run(rec, due))

    if session.get('is_admin') and 'user_id' in data and data['user_id']:
        updates.append('user_id = ?')
        params.append(data['user_id'])

    if updates:
        updates.append('updated_at = ?')
        params.append(now)
        params.append(item_id)
        db.execute(f'UPDATE work_items SET {", ".join(updates)} WHERE id = ?', params)
        db.execute('INSERT INTO work_logs (user_id, action, item_id, detail) VALUES (?, ?, ?, ?)',
                   (session['user_id'], 'updated', item_id, '更新工作项'))
        db.commit()

    item = db.execute(
        'SELECT w.*, u.display_name, u.ad_username, u.section_name FROM work_items w JOIN users u ON w.user_id = u.id WHERE w.id = ?',
        (item_id,)
    ).fetchone()
    return jsonify(dict(item))


@app.route('/api/work-items/<int:item_id>/move', methods=['POST'])
@login_required
def move_work_item(item_id):
    """上下移动子任务排序"""
    data = request.get_json(silent=True) or {}
    direction = data.get('direction')  # 'up' or 'down'
    if direction not in ('up', 'down'):
        return jsonify({'error': 'direction 须为 up 或 down'}), 400
    db = get_db()
    item = db.execute('SELECT * FROM work_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        return jsonify({'error': '工作项不存在'}), 404
    if not session.get('is_admin') and item['user_id'] != session['user_id']:
        return jsonify({'error': '无权操作'}), 403
    parent_id = item['parent_id']
    # 仅子任务支持排序；无主任务也允许自身排序
    scope = 'parent_id = ?' if parent_id else 'parent_id IS NULL AND user_id = ?'
    params = (parent_id,) if parent_id else (item['user_id'],)
    siblings = db.execute(
        f'SELECT id, sort_order FROM work_items WHERE {scope} ORDER BY sort_order, id',
        params
    ).fetchall()
    idx = [i['id'] for i in siblings].index(item_id)
    if direction == 'up' and idx > 0:
        other = siblings[idx - 1]
    elif direction == 'down' and idx < len(siblings) - 1:
        other = siblings[idx + 1]
    else:
        return jsonify({'message': '已在边界'})
    # 交换 sort_order
    cur_order = item['sort_order'] or 0
    other_order = other['sort_order'] or 0
    db.execute('UPDATE work_items SET sort_order = ? WHERE id = ?', (other_order, item_id))
    db.execute('UPDATE work_items SET sort_order = ? WHERE id = ?', (cur_order, other['id']))
    db.commit()
    return jsonify({'message': '已移动'})


@app.route('/api/work-items/<int:item_id>', methods=['DELETE'])
@login_required
def delete_work_item(item_id):
    db = get_db()
    item = db.execute('SELECT * FROM work_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        return jsonify({'error': '工作项不存在'}), 404
    if not session.get('is_admin') and item['user_id'] != session['user_id']:
        return jsonify({'error': '无权删除他人的工作项'}), 403
    # 删除关联交付物文件
    dels = db.execute('SELECT filepath FROM deliverables WHERE work_item_id = ?', (item_id,)).fetchall()
    for d in dels:
        try:
            if os.path.exists(d['filepath']):
                os.remove(d['filepath'])
        except Exception:
            pass
    db.execute('DELETE FROM deliverables WHERE work_item_id = ?', (item_id,))
    db.execute('DELETE FROM work_items WHERE id = ?', (item_id,))
    db.execute('DELETE FROM work_logs WHERE item_id = ?', (item_id,))
    db.commit()
    return jsonify({'message': '已删除'})


@app.route('/api/work-items/<int:item_id>/transfer', methods=['POST'])
@login_required
def transfer_work_item(item_id):
    """转办任务给另一个用户"""
    db = get_db()
    item = db.execute('SELECT * FROM work_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        return jsonify({'error': '工作项不存在'}), 404
    if not session.get('is_admin') and item['user_id'] != session['user_id']:
        return jsonify({'error': '无权操作'}), 403
    data = request.get_json() or {}
    target_uid = data.get('user_id')
    if not target_uid:
        return jsonify({'error': '请选择转办目标用户'}), 400
    target = db.execute('SELECT * FROM users WHERE id = ?', (target_uid,)).fetchone()
    if not target:
        return jsonify({'error': '目标用户不存在'}), 400
    now = _now_str()
    db.execute(
        'UPDATE work_items SET user_id = ?, transferred_to = ?, updated_at = ? WHERE id = ?',
        (target_uid, target_uid, now, item_id)
    )
    db.execute('INSERT INTO work_logs (user_id, action, item_id, detail) VALUES (?, ?, ?, ?)',
               (session['user_id'], 'transferred', item_id, f"转办给 {target['display_name']}({target['ad_username']})"))
    db.commit()
    return jsonify({'message': f'已转办给 {target["display_name"]}'})


@app.route('/api/work-items/<int:item_id>/collaborators', methods=['POST'])
@login_required
def add_collaborator(item_id):
    """添加协同用户：支持系统内用户(user_id)或手动输入的外部协同人(external_name)"""
    db = get_db()
    item = db.execute('SELECT * FROM work_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        return jsonify({'error': '工作项不存在'}), 404
    if not session.get('is_admin') and item['user_id'] != session['user_id']:
        return jsonify({'error': '无权操作'}), 403
    data = request.get_json() or {}
    collab_uid = data.get('user_id')
    # 外部姓名/供应商名称中如有英文逗号，统一替换为中文逗号，避免与分隔符冲突
    external_name = (data.get('external_name') or '').strip().replace(',', '，')

    if not collab_uid and not external_name:
        return jsonify({'error': '请选择协同用户或填写外部协同人'}), 400

    now = _now_str()
    # 内部协同者
    if collab_uid:
        target = db.execute('SELECT * FROM users WHERE id = ?', (collab_uid,)).fetchone()
        if not target:
            return jsonify({'error': '用户不存在'}), 400
        current = item['collaborators'] or ''
        collab_list = [int(x) for x in current.split(',') if x.strip().isdigit()] if current else []
        if int(collab_uid) in collab_list:
            return jsonify({'error': '该用户已是协同者'}), 400
        collab_list.append(int(collab_uid))
        new_val = ','.join(str(x) for x in collab_list)
        db.execute('UPDATE work_items SET collaborators = ?, updated_at = ? WHERE id = ?', (new_val, now, item_id))
        db.execute('INSERT INTO work_logs (user_id, action, item_id, detail) VALUES (?, ?, ?, ?)',
                   (session['user_id'], 'add_collaborator', item_id, f"添加协同 {target['display_name']}({target['ad_username']})"))

    # 外部协同人
    if external_name:
        current_ext = item['external_collaborators'] or ''
        ext_list = [x.strip() for x in current_ext.split(',') if x.strip()] if current_ext else []
        if external_name in ext_list:
            return jsonify({'error': '该外部协同人已存在'}), 400
        ext_list.append(external_name)
        new_ext_val = ','.join(ext_list)
        db.execute('UPDATE work_items SET external_collaborators = ?, updated_at = ? WHERE id = ?', (new_ext_val, now, item_id))
        db.execute('INSERT INTO work_logs (user_id, action, item_id, detail) VALUES (?, ?, ?, ?)',
                   (session['user_id'], 'add_external_collaborator', item_id, f"添加外部协同 {external_name}"))

    db.commit()
    return jsonify({'message': '协同人已添加'})


@app.route('/api/work-items/<int:item_id>/collaborators', methods=['DELETE'])
@login_required
def remove_collaborator(item_id):
    """移除协同用户：支持移除系统内用户(user_id)或外部协同人(external_name)"""
    db = get_db()
    item = db.execute('SELECT * FROM work_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        return jsonify({'error': '工作项不存在'}), 404
    if not session.get('is_admin') and item['user_id'] != session['user_id']:
        return jsonify({'error': '无权操作'}), 403
    data = request.get_json() or {}
    collab_uid = data.get('user_id')
    # 外部姓名/供应商名称中如有英文逗号，统一替换为中文逗号，避免与分隔符冲突
    external_name = (data.get('external_name') or '').strip().replace(',', '，')

    if not collab_uid and not external_name:
        return jsonify({'error': '请指定要移除的协同人'}), 400

    now = _now_str()
    if collab_uid:
        current = item['collaborators'] or ''
        collab_list = [int(x) for x in current.split(',') if x.strip().isdigit()] if current else []
        if int(collab_uid) not in collab_list:
            return jsonify({'error': '该用户不在协同列表中'}), 400
        collab_list.remove(int(collab_uid))
        new_val = ','.join(str(x) for x in collab_list)
        db.execute('UPDATE work_items SET collaborators = ?, updated_at = ? WHERE id = ?', (new_val, now, item_id))

    if external_name:
        current_ext = item['external_collaborators'] or ''
        ext_list = [x.strip() for x in current_ext.split(',') if x.strip()] if current_ext else []
        if external_name not in ext_list:
            return jsonify({'error': '该外部协同人不存在'}), 400
        ext_list.remove(external_name)
        new_ext_val = ','.join(ext_list)
        db.execute('UPDATE work_items SET external_collaborators = ?, updated_at = ? WHERE id = ?', (new_ext_val, now, item_id))

    db.commit()
    return jsonify({'message': '已移除协同人'})


# ====================================================================
# 交付物（文件上传）
# ====================================================================
@app.route('/api/work-items/<int:item_id>/deliverables', methods=['POST'])
@login_required
def upload_deliverable(item_id):
    """完成任务时上传交付物文件"""
    db = get_db()
    item = db.execute('SELECT * FROM work_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        return jsonify({'error': '工作项不存在'}), 404
    if not session.get('is_admin') and item['user_id'] != session['user_id']:
        return jsonify({'error': '无权操作'}), 403

    if 'file' not in request.files:
        return jsonify({'error': '请选择文件'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': '文件名为空'}), 400

    # v26.1：支持文件夹上传——relpath 保留相对路径（逐段净化防穿越），
    # 展示名保留目录结构（dir/sub/file.txt），磁盘存储名拍平为 dir_sub_file.txt
    rel = (request.form.get('relpath') or '').strip()
    pretty_name = secure_filename(f.filename)
    if rel and rel != f.filename:
        segs = [secure_filename(s) for s in rel.replace('\\', '/').split('/')
                if s and s not in ('.', '..') and secure_filename(s)]
        if segs:
            pretty_name = '/'.join(segs)
    # 避免重名
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    stored_name = f"{ts}_{pretty_name.replace('/', '_')}"
    item_dir = os.path.join(UPLOAD_DIR, str(item_id))
    os.makedirs(item_dir, exist_ok=True)
    filepath = os.path.join(item_dir, stored_name)
    f.save(filepath)
    filesize = os.path.getsize(filepath)

    cur = db.execute("""
        INSERT INTO deliverables (work_item_id, user_id, filename, filepath, filesize, mimetype)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (item_id, session['user_id'], pretty_name, filepath, filesize,
          f.mimetype or 'application/octet-stream'))
    db.commit()
    return jsonify({
        'id': cur.lastrowid,
        'filename': pretty_name,
        'filesize': filesize,
        'mimetype': f.mimetype or 'application/octet-stream'
    }), 201


@app.route('/api/work-items/<int:item_id>/deliverables')
@login_required
def list_deliverables(item_id):
    """列出某工作项的交付物"""
    db = get_db()
    item = db.execute('SELECT * FROM work_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        return jsonify({'error': '工作项不存在'}), 404
    if not session.get('is_admin') and item['user_id'] != session['user_id']:
        return jsonify({'error': '无权查看'}), 403
    dels = db.execute(
        'SELECT * FROM deliverables WHERE work_item_id = ? ORDER BY created_at DESC',
        (item_id,)
    ).fetchall()
    return jsonify([dict(d) for d in dels])


@app.route('/api/deliverables/<int:del_id>/download')
@login_required
def download_deliverable(del_id):
    """下载交付物文件"""
    db = get_db()
    d = db.execute('SELECT * FROM deliverables WHERE id = ?', (del_id,)).fetchone()
    if not d:
        return jsonify({'error': '文件不存在'}), 404
    item = db.execute('SELECT user_id FROM work_items WHERE id = ?', (d['work_item_id'],)).fetchone()
    if item and not session.get('is_admin') and item['user_id'] != session['user_id']:
        return jsonify({'error': '无权下载'}), 403
    if not os.path.exists(d['filepath']):
        return jsonify({'error': '文件已被删除'}), 404
    return send_file(d['filepath'], as_attachment=True, download_name=d['filename'])


@app.route('/api/deliverables/<int:del_id>', methods=['DELETE'])
@login_required
def delete_deliverable(del_id):
    db = get_db()
    d = db.execute('SELECT * FROM deliverables WHERE id = ?', (del_id,)).fetchone()
    if not d:
        return jsonify({'error': '文件不存在'}), 404
    if not session.get('is_admin') and d['user_id'] != session['user_id']:
        return jsonify({'error': '无权删除'}), 403
    try:
        if os.path.exists(d['filepath']):
            os.remove(d['filepath'])
    except Exception:
        pass
    db.execute('DELETE FROM deliverables WHERE id = ?', (del_id,))
    db.commit()
    return jsonify({'message': '已删除'})


# ====================================================================
# 任务里程碑（进行中的关键节点状态登记）
# ====================================================================
def _check_item_access(item_id, require_owner=True):
    """校验当前用户对指定工作项的访问/管理权限"""
    db = get_db()
    item = db.execute('SELECT * FROM work_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        return None, (jsonify({'error': '工作项不存在'}), 404)
    if session.get('is_admin'):
        return item, None
    if item['user_id'] == session['user_id']:
        return item, None
    # 非所有者仅查看（协同者/转办目标）
    if not require_owner and item['collaborators']:
        collab_list = [int(x) for x in str(item['collaborators']).split(',') if x.strip().isdigit()]
        if session['user_id'] in collab_list:
            return item, None
    return None, (jsonify({'error': '无权操作'}), 403)


@app.route('/api/work-items/<int:item_id>/milestones')
@login_required
def list_milestones(item_id):
    """列出某工作项的里程碑状态记录"""
    item, err = _check_item_access(item_id, require_owner=False)
    if err:
        return err
    db = get_db()
    rows = db.execute(
        'SELECT m.*, u.display_name FROM work_item_milestones m JOIN users u ON m.user_id = u.id WHERE m.work_item_id = ? ORDER BY m.created_at DESC',
        (item_id,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/work-items/<int:item_id>/milestones', methods=['POST'])
@login_required
def add_milestone(item_id):
    """为工作项登记一个里程碑状态（owner/管理员/协同人员均可）"""
    item, err = _check_item_access(item_id, require_owner=False)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    status_label = (data.get('status_label') or '').strip()
    if not status_label:
        return jsonify({'error': '请填写里程碑状态'}), 400
    note = (data.get('note') or '').strip()
    now = _now_str()
    db = get_db()
    cur = db.execute(
        'INSERT INTO work_item_milestones (work_item_id, user_id, created_by, status_label, note, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (item_id, session['user_id'], session.get('display_name') or session.get('ad_username') or '', status_label, note, now)
    )
    # 联动刷新工作项更新时间，便于工作台排序感知到变化
    db.execute('UPDATE work_items SET updated_at = ? WHERE id = ?', (now, item_id))
    db.execute('INSERT INTO work_logs (user_id, action, item_id, detail) VALUES (?, ?, ?, ?)',
               (session['user_id'], 'milestone', item_id, f'登记里程碑：{status_label}'))
    db.commit()
    row = db.execute(
        'SELECT m.*, u.display_name FROM work_item_milestones m JOIN users u ON m.user_id = u.id WHERE m.id = ?',
        (cur.lastrowid,)
    ).fetchone()
    return jsonify(dict(row)), 201


@app.route('/api/work-items/<int:item_id>/milestones/<int:ms_id>', methods=['PUT'])
@login_required
def update_milestone(item_id, ms_id):
    """v29.6：编辑里程碑记录（管理员或登记人本人）"""
    db = get_db()
    ms = db.execute('SELECT * FROM work_item_milestones WHERE id = ? AND work_item_id = ?', (ms_id, item_id)).fetchone()
    if not ms:
        return jsonify({'error': '里程碑记录不存在'}), 404
    if not session.get('is_admin') and ms['user_id'] != session['user_id']:
        return jsonify({'error': '无权编辑'}), 403
    data = request.get_json(silent=True) or {}
    status_label = (data.get('status_label') or '').strip()
    if not status_label:
        return jsonify({'error': '请填写里程碑状态'}), 400
    note = (data.get('note') or '').strip()
    now = _now_str()
    db.execute('UPDATE work_item_milestones SET status_label = ?, note = ? WHERE id = ?', (status_label, note, ms_id))
    # 联动刷新工作项更新时间，与登记里程碑行为保持一致
    db.execute('UPDATE work_items SET updated_at = ? WHERE id = ?', (now, item_id))
    db.execute('INSERT INTO work_logs (user_id, action, item_id, detail) VALUES (?, ?, ?, ?)',
               (session['user_id'], 'milestone', item_id, f'编辑里程碑：{status_label}'))
    db.commit()
    row = db.execute(
        'SELECT m.*, u.display_name FROM work_item_milestones m JOIN users u ON m.user_id = u.id WHERE m.id = ?',
        (ms_id,)
    ).fetchone()
    return jsonify(dict(row))


@app.route('/api/work-items/<int:item_id>/milestones/<int:ms_id>', methods=['DELETE'])
@login_required
def delete_milestone(item_id, ms_id):
    """删除里程碑记录（管理员或登记人本人）"""
    db = get_db()
    ms = db.execute('SELECT * FROM work_item_milestones WHERE id = ? AND work_item_id = ?', (ms_id, item_id)).fetchone()
    if not ms:
        return jsonify({'error': '里程碑记录不存在'}), 404
    if not session.get('is_admin') and ms['user_id'] != session['user_id']:
        return jsonify({'error': '无权删除'}), 403
    db.execute('DELETE FROM work_item_milestones WHERE id = ?', (ms_id,))
    db.commit()
    return jsonify({'message': '已删除'})


@app.route('/api/work-items/<int:item_id>')
@login_required
def get_work_item(item_id):
    """获取单个工作项详情"""
    item, err = _check_item_access(item_id, require_owner=False)
    if err:
        return err
    return jsonify(dict(item))


# ====================================================================
# 分类管理
# ====================================================================
@app.route('/api/categories')
@login_required
def list_categories():
    db = get_db()
    cats = db.execute('SELECT * FROM categories ORDER BY sort_order, id').fetchall()
    return jsonify([dict(c) for c in cats])


@app.route('/api/categories', methods=['POST'])
@admin_required
def add_category():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': '分类名不能为空'}), 400
    color = data.get('color', '').strip()
    db = get_db()
    try:
        cur = db.execute('INSERT INTO categories (name, color) VALUES (?, ?)', (name, color))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({'error': '分类名已存在'}), 400
    cat = db.execute('SELECT * FROM categories WHERE id = ?', (cur.lastrowid,)).fetchone()
    return jsonify(dict(cat)), 201


@app.route('/api/categories/<int:cat_id>', methods=['PUT'])
@admin_required
def update_category(cat_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    updates, params = [], []
    for field in ['name', 'color', 'sort_order']:
        if field in data:
            updates.append(f'{field} = ?')
            params.append(data[field])
    if not updates:
        return jsonify({'error': '没有可更新的字段'}), 400
    params.append(cat_id)
    try:
        db.execute(f'UPDATE categories SET {", ".join(updates)} WHERE id = ?', params)
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({'error': '分类名已存在'}), 400
    cat = db.execute('SELECT * FROM categories WHERE id = ?', (cat_id,)).fetchone()
    return jsonify(dict(cat))


@app.route('/api/categories/<int:cat_id>', methods=['DELETE'])
@admin_required
def delete_category(cat_id):
    db = get_db()
    # 将使用该分类的工作项改为"其他"
    cat = db.execute('SELECT name FROM categories WHERE id = ?', (cat_id,)).fetchone()
    if not cat:
        return jsonify({'error': '分类不存在'}), 404
    db.execute("UPDATE work_items SET category = '其他' WHERE category = ?", (cat['name'],))
    db.execute('DELETE FROM categories WHERE id = ?', (cat_id,))
    db.commit()
    return jsonify({'message': '已删除，相关工作项分类已改为「其他」'})


# ====================================================================
# 网页图标代理（常用链接 favicon 自动获取）
# ====================================================================
import re as _re

_FAVICON_CACHE = '/app/data/favicons'


def _validate_icon_url(url):
    """v29.2：SSRF 防护 —— 仅 http(s)，且解析后的 IP 不得为环回/链路本地/元数据等敏感地址
    （内网业务地址保留放行）；校验通过返回 urlparse 结果，否则 None"""
    import socket, ipaddress
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except Exception:
        return None
    if u.scheme not in ('http', 'https') or not u.hostname:
        return None
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == 'https' else 80),
                                   proto=socket.IPPROTO_TCP)
    except Exception:
        return None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return None
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            return None
    return u


def _try_fetch_icon_bytes(url):
    if not _validate_icon_url(url):
        return None
    try:
        # v29.2：禁跳转（防绕过目标校验）+ 限响应体大小
        resp = requests.get(url, timeout=4, headers={'User-Agent': 'Mozilla/5.0'},
                            allow_redirects=False, stream=True)
        ctype = resp.headers.get('Content-Type', '')
        if resp.status_code != 200 or 'text/html' in ctype:
            resp.close()
            return None
        chunks, size = [], 0
        for chunk in resp.iter_content(8192):
            size += len(chunk)
            if size > 512 * 1024:  # 图标不应超过 512KB
                resp.close()
                return None
            chunks.append(chunk)
        resp.close()
        data = b''.join(chunks)
        return data or None
    except Exception:
        pass
    return None


def _fetch_page_icon(page_url, host):
    """解析页面 <link rel=icon>，兜底 Google favicon 服务（仅外网域名）"""
    if not _validate_icon_url(page_url):  # v29.2：SSRF 防护
        return None
    try:
        resp = requests.get(page_url, timeout=5,
                            headers={'User-Agent': 'Mozilla/5.0'}, verify=False,
                            allow_redirects=False, stream=True)
        if resp.status_code != 200:
            resp.close()
            return None
        text = resp.raw.read(512 * 1024, decode_content=True).decode('utf-8', 'ignore')
        resp.close()
        m = _re.search(r'<link[^>]+rel=["\']?(?:shortcut\s+)?icon["\']?[^>]*>', text, _re.I)
        if m:
            href_m = _re.search(r'href=["\']([^"\']+)["\']', m.group(0), _re.I)
            if href_m:
                icon_path = href_m.group(1).strip()
                if icon_path.startswith('http://') or icon_path.startswith('https://'):
                    return _try_fetch_icon_bytes(icon_path)
                if icon_path.startswith('//'):
                    return _try_fetch_icon_bytes('https:' + icon_path)
                from urllib.parse import urljoin
                return _try_fetch_icon_bytes(urljoin(page_url, icon_path))
    except Exception:
        pass
    # 兜底：Google favicon 服务（内网 IP 域名无效）
    if not host.replace('.', '').isdigit():
        return _try_fetch_icon_bytes(f'https://www.google.com/s2/favicons?domain={host}&sz=64')
    return None


@app.route('/api/favicon')
@login_required
def favicon_proxy():
    """根据链接 URL 获取网页图标（带磁盘缓存）"""
    from urllib.parse import urlparse
    url = (request.args.get('url') or '').strip()
    if not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'URL 不合法'}), 400
    try:
        host = urlparse(url).netloc
        scheme = urlparse(url).scheme
    except Exception:
        return jsonify({'error': 'URL 解析失败'}), 400
    if not host:
        return jsonify({'error': 'URL 缺少域名'}), 400
    if not _validate_icon_url(url):  # v29.2：SSRF 防护（环回/链路本地/元数据地址拒绝）
        return jsonify({'error': 'URL 不合法'}), 400
    os.makedirs(_FAVICON_CACHE, exist_ok=True)
    cache_path = os.path.join(_FAVICON_CACHE, host.replace('/', '_').replace(':', '_') + '.ico')
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        return send_file(cache_path, mimetype='image/x-icon')
    icon_bytes = _try_fetch_icon_bytes(f'{scheme}://{host}/favicon.ico')
    if not icon_bytes:
        icon_bytes = _fetch_page_icon(url, host)
    if icon_bytes:
        try:
            with open(cache_path, 'wb') as f:
                f.write(icon_bytes)
            return send_file(cache_path, mimetype='image/x-icon')
        except Exception:
            pass
    return jsonify({'error': '未找到图标'}), 404


# ====================================================================
# 快捷链接（每用户自定义）
# ====================================================================
@app.route('/api/quick-links')
@login_required
def list_quick_links():
    """v25.7：实时同步公共链接池。
    对 pool_id>0 的记录 LEFT JOIN tile_link_pool，用池子最新字段覆盖 quick_links
    的 title/url/icon/color/description，前台一拉就拿到池子最新值，无需手动刷新。
    用户私有字段：sort_order, pool_id, user_id, created_at 保留。
    """
    db = get_db()
    rows = db.execute(
        'SELECT ql.*, '
        '       tlp.title AS pool_title, tlp.url AS pool_url, '
        '       tlp.icon AS pool_icon, tlp.color AS pool_color, '
        '       tlp.updated_at AS pool_updated_at '
        'FROM quick_links ql '
        'LEFT JOIN tile_link_pool tlp ON ql.pool_id = tlp.id '
        'WHERE ql.user_id = ? '
        'ORDER BY ql.sort_order, ql.id',
        (session['user_id'],)
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if d.get('pool_id') and d.get('pool_title'):
            # 来自公共池：用池子最新字段覆盖
            d['title'] = d['pool_title'] or d.get('title', '')
            d['url'] = d['pool_url'] or d.get('url', '')
            d['icon'] = d['pool_icon'] or d.get('icon', '')
            d['color'] = d['pool_color'] or d.get('color', '')
            d['_pool_synced'] = True
            d['_pool_updated_at'] = d.get('pool_updated_at', '') or ''
        else:
            d['_pool_synced'] = False
        # 清理临时字段
        for k in ('pool_title', 'pool_url', 'pool_icon', 'pool_color', 'pool_updated_at'):
            d.pop(k, None)
        result.append(d)
    return jsonify(result)


@app.route('/api/quick-links', methods=['POST'])
@login_required
def add_quick_link():
    """v25.7：添加常用链接支持两种模式：
    1) pool_id 指定：从公共链接池选择，复制到个人 quick_links
    2) 无 pool_id：新建并同时写入公共池 tile_link_pool + 个人 quick_links"""
    data = request.get_json(silent=True) or {}
    db = get_db()
    uid = session['user_id']
    pool_id = data.get('pool_id')
    if pool_id:
        pool = db.execute('SELECT * FROM tile_link_pool WHERE id = ?', (pool_id,)).fetchone()
        if not pool:
            return jsonify({'error': '公共池中不存在该链接'}), 404
        # 检查是否已添加
        existing = db.execute(
            'SELECT id FROM quick_links WHERE user_id = ? AND pool_id = ?',
            (uid, pool_id)
        ).fetchone()
        if existing:
            return jsonify({'error': '该链接已在你的常用链接中'}), 400
        cur = db.execute(
            'INSERT INTO quick_links (user_id, title, url, icon, color, pool_id) VALUES (?, ?, ?, ?, ?, ?)',
            (uid, pool['title'], pool['url'], pool['icon'], pool['color'], pool_id)
        )
        db.execute('UPDATE tile_link_pool SET use_count = use_count + 1 WHERE id = ?', (pool_id,))
        db.commit()
        link = db.execute('SELECT * FROM quick_links WHERE id = ?', (cur.lastrowid,)).fetchone()
        return jsonify(dict(link)), 201

    title = (data.get('title') or '').strip()
    url = (data.get('url') or '').strip()
    if not title or not url:
        return jsonify({'error': '标题和URL不能为空'}), 400
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    icon = data.get('icon', '').strip()
    color = data.get('color', '').strip()
    # 同时写入公共池
    cur_pool = db.execute(
        'INSERT INTO tile_link_pool (title, url, icon, color, created_by) VALUES (?, ?, ?, ?, ?)',
        (title, url, icon, color, uid)
    )
    db.execute('UPDATE tile_link_pool SET use_count = use_count + 1 WHERE id = ?', (cur_pool.lastrowid,))
    cur = db.execute(
        'INSERT INTO quick_links (user_id, title, url, icon, color, pool_id) VALUES (?, ?, ?, ?, ?, ?)',
        (uid, title, url, icon, color, cur_pool.lastrowid)
    )
    db.commit()
    link = db.execute('SELECT * FROM quick_links WHERE id = ?', (cur.lastrowid,)).fetchone()
    return jsonify(dict(link)), 201


@app.route('/api/quick-links/<int:link_id>', methods=['PUT'])
@login_required
def update_quick_link(link_id):
    """v25.7 扩展：支持 unlink_pool 参数把 pool_id 置 0，从此链接独立（不再被池子更新覆盖）。"""
    data = request.get_json(silent=True) or {}
    db = get_db()
    link = db.execute('SELECT * FROM quick_links WHERE id = ?', (link_id,)).fetchone()
    if not link:
        return jsonify({'error': '链接不存在'}), 404
    if link['user_id'] != session['user_id']:
        return jsonify({'error': '无权操作'}), 403
    updates, params = [], []
    for field in ['title', 'url', 'icon', 'sort_order']:
        if field in data:
            updates.append(f'{field} = ?')
            params.append(data[field])
    # 用户主动"分离"：把 pool_id 置 0，从此独立
    if data.get('unlink_pool'):
        updates.append('pool_id = 0')
    if updates:
        params.append(link_id)
        db.execute(f'UPDATE quick_links SET {", ".join(updates)} WHERE id = ?', params)
        db.commit()
    link = db.execute('SELECT * FROM quick_links WHERE id = ?', (link_id,)).fetchone()
    return jsonify(dict(link))


@app.route('/api/quick-links/<int:link_id>', methods=['DELETE'])
@login_required
def delete_quick_link(link_id):
    db = get_db()
    link = db.execute('SELECT * FROM quick_links WHERE id = ?', (link_id,)).fetchone()
    if not link:
        return jsonify({'error': '链接不存在'}), 404
    if link['user_id'] != session['user_id']:
        return jsonify({'error': '无权操作'}), 403
    db.execute('DELETE FROM quick_links WHERE id = ?', (link_id,))
    db.commit()
    return jsonify({'message': '已删除'})


# ====================================================================
# 右侧小工具（磁贴 + iframe 自定义）
# ====================================================================
# 内置小工具 kind：todo(待办) / calendar(日程) / chat(聊天) / minutes(听记) / zabbix(告警，v29.6)
# 自定义小工具 kind=iframe，url 为用户填写的 iframe 地址
BUILTIN_WIDGETS = [
    {'kind': 'todo', 'title': '待办列表', 'icon': '📌'},
    {'kind': 'calendar', 'title': '今日日程', 'icon': '🗓️'},
    {'kind': 'chat', 'title': '最近聊天', 'icon': '💬'},
    {'kind': 'minutes', 'title': '听记摘要', 'icon': '🎙️'},
    {'kind': 'zabbix', 'title': 'Zabbix 告警', 'icon': '🚨'},
]
BUILTIN_KINDS = ('todo', 'calendar', 'chat', 'minutes', 'zabbix')

# 内置磁贴 → 知识库来源 & 磁贴默认摘要条数
WIDGET_SOURCE = {
    'todo': 'dingtalk_todo',
    'calendar': 'dingtalk_calendar',
    'chat': 'dingtalk_chat',
    'minutes': 'dingtalk_minutes',
}
WIDGET_PREVIEW_N = {'todo': 4, 'calendar': 4, 'chat': 4, 'minutes': 2}


def _widget_preview(db, uid, kind, today):
    """拉取内置磁贴的摘要条目：
    - 优先当日发生(occur_date=today)按实际时间倒序
    - 日历：今日无日程时回退「今天起最近的日程」（升序），仍无则回退最近条目
    - 其他类型：无当日数据回退最近条目（按实际时间倒序）"""
    source = WIDGET_SOURCE.get(kind)
    if not source:
        return []
    n = WIDGET_PREVIEW_N.get(kind, 3)
    rows = db.execute(
        "SELECT id, title, content, external_id, created_at, event_time, occur_date "
        "FROM user_knowledge WHERE user_id = ? AND source = ? AND occur_date = ? "
        "ORDER BY event_time DESC, created_at DESC LIMIT ?",
        (uid, source, today, n)
    ).fetchall()
    if not rows and kind == 'calendar':
        # 回退1：今天起最近的日程（升序，展示最近要发生的）
        rows = db.execute(
            "SELECT id, title, content, external_id, created_at, event_time, occur_date "
            "FROM user_knowledge WHERE user_id = ? AND source = ? AND event_time >= ? "
            "ORDER BY event_time ASC, created_at DESC LIMIT ?",
            (uid, source, today + ' 00:00', n)
        ).fetchall()
    if not rows:
        # 回退2：最近条目
        rows = db.execute(
            "SELECT id, title, content, external_id, created_at, event_time, occur_date "
            "FROM user_knowledge WHERE user_id = ? AND source = ? "
            "ORDER BY event_time DESC, created_at DESC LIMIT ?",
            (uid, source, n)
        ).fetchall()
    return [dict(r) for r in rows]


def _parse_widget_config(cfg_str):
    """解析磁贴 config（JSON），返回 {size: 's'|'m'|'l', ...}"""
    try:
        cfg = json.loads(cfg_str or '{}')
        if isinstance(cfg, dict):
            return cfg
    except Exception:
        pass
    return {}


@app.route('/api/widgets')
@login_required
def list_widgets():
    """返回内置 + 自定义磁贴小工具，内置磁贴附带摘要预览（按实际发生时间）"""
    uid = session['user_id']
    db = get_db()
    today = datetime.now().strftime('%Y-%m-%d')
    custom = db.execute(
        "SELECT * FROM user_widgets WHERE user_id = ? AND enabled = 1 ORDER BY sort_order, id",
        (uid,)
    ).fetchall()
    # 内置小工具：以配置表为准合并（配置里 kind 为内置时覆盖默认图标/标题/尺寸）
    builtin_cfg = {w['kind']: w for w in custom if w['kind'] in BUILTIN_KINDS}
    builtins = []
    for b in BUILTIN_WIDGETS:
        cfg = builtin_cfg.get(b['kind'])
        if cfg:
            item = dict(cfg)
            item['config'] = _parse_widget_config(cfg['config'])
        else:
            item = {'id': None, 'user_id': uid, 'kind': b['kind'], 'title': b['title'],
                    'icon': b['icon'], 'url': '', 'config': {}, 'enabled': 1}
        item['preview'] = _widget_preview(db, uid, b['kind'], today) if b['kind'] != 'zabbix' else []
        # v29.6：zabbix 告警磁贴附带未恢复告警数（供磁贴角标/预览；拉取失败不阻塞整体列表）
        if b['kind'] == 'zabbix':
            try:
                item['zabbix_count'] = len(_zbx_problems(limit=30))
                item['zabbix_ok'] = True
            except Exception:
                item['zabbix_count'] = 0
                item['zabbix_ok'] = False
        item['size'] = item.get('config', {}).get('size', 'm')
        builtins.append(item)
    iframes = []
    # v25.9：公共池关联——pool_id>0 的工具标题/类型/地址/图标跟随公共池实时更新
    pool_rows = db.execute('SELECT * FROM tile_tool_pool').fetchall()
    pool_map = {p['id']: p for p in pool_rows}
    for w in custom:
        if w['kind'] not in BUILTIN_KINDS:
            item = dict(w)
            pid = item.get('pool_id') or 0
            pool = pool_map.get(pid) if pid else None
            if pool:
                item['title'] = pool['title']
                item['kind'] = pool['kind'] or item.get('kind') or 'iframe'
                item['url'] = pool['url'] or item.get('url') or ''
                item['icon'] = pool['icon'] or item.get('icon') or ''
                item['_pool_synced'] = True
                item['_pool_updated_at'] = pool['updated_at'] or ''
            else:
                item['_pool_synced'] = False
            item['config'] = _parse_widget_config(w['config'])
            item['size'] = item['config'].get('size', 'm')
            iframes.append(item)
    return jsonify({'builtins': builtins, 'custom': iframes, 'today': today})


@app.route('/api/widgets', methods=['POST'])
@login_required
def add_widget():
    data = request.get_json(silent=True) or {}
    uid = session['user_id']
    db = get_db()
    config = data.get('config') or {}
    if isinstance(config, dict):
        config_str = json.dumps(config, ensure_ascii=False)
    elif isinstance(config, str):
        config_str = config
    else:
        config_str = ''
    # v25.9：模式一——从公共池添加（pool_id），复制池配置为个人实例
    pool_id = int(data.get('pool_id') or 0)
    if pool_id:
        pool = db.execute('SELECT * FROM tile_tool_pool WHERE id = ?', (pool_id,)).fetchone()
        if not pool:
            return jsonify({'error': '公共池工具不存在'}), 404
        exists = db.execute(
            'SELECT id FROM user_widgets WHERE user_id = ? AND pool_id = ?', (uid, pool_id)
        ).fetchone()
        if exists:
            return jsonify({'error': '该工具已在你的侧边栏中'}), 400
        cur = db.execute(
            'INSERT INTO user_widgets (user_id, title, kind, icon, url, config, pool_id) VALUES (?,?,?,?,?,?,?)',
            (uid, pool['title'], pool['kind'] or 'iframe', pool['icon'] or '', pool['url'] or '',
             config_str or (pool['config'] or ''), pool_id)
        )
        db.execute('UPDATE tile_tool_pool SET use_count = use_count + 1 WHERE id = ?', (pool_id,))
        db.commit()
        w = db.execute('SELECT * FROM user_widgets WHERE id = ?', (cur.lastrowid,)).fetchone()
        return jsonify(dict(w)), 201
    # 模式二——自定义新建：写入个人表，同时自动加入公共池（参照链接管理模式）
    title = (data.get('title') or '').strip()
    kind = (data.get('kind') or 'iframe').strip()
    url = (data.get('url') or '').strip()
    icon = (data.get('icon') or '').strip()
    if not title:
        return jsonify({'error': '标题不能为空'}), 400
    if kind == 'iframe' and not url:
        return jsonify({'error': 'iframe 地址不能为空'}), 400
    if url and not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    # 内置 kind 允许覆盖默认标题/图标
    if kind in BUILTIN_KINDS:
        db.execute(
            "DELETE FROM user_widgets WHERE user_id = ? AND kind = ?",
            (uid, kind)
        )
        cur = db.execute(
            'INSERT INTO user_widgets (user_id, title, kind, icon, url, config) VALUES (?, ?, ?, ?, ?, ?)',
            (uid, title, kind, icon, url, config_str)
        )
        db.commit()
        w = db.execute('SELECT * FROM user_widgets WHERE id = ?', (cur.lastrowid,)).fetchone()
        return jsonify(dict(w)), 201
    # iframe 自定义工具：同步进公共池，方便团队复用
    pcur = db.execute(
        'INSERT INTO tile_tool_pool (title, kind, url, icon, config, created_by, use_count) VALUES (?,?,?,?,?,?,1)',
        (title, kind, url, icon, config_str, uid)
    )
    cur = db.execute(
        'INSERT INTO user_widgets (user_id, title, kind, icon, url, config, pool_id) VALUES (?,?,?,?,?,?,?)',
        (uid, title, kind, icon, url, config_str, pcur.lastrowid)
    )
    db.commit()
    w = db.execute('SELECT * FROM user_widgets WHERE id = ?', (cur.lastrowid,)).fetchone()
    return jsonify(dict(w)), 201


@app.route('/api/widgets/<int:widget_id>', methods=['PUT'])
@login_required
def update_widget(widget_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    w = db.execute('SELECT * FROM user_widgets WHERE id = ?', (widget_id,)).fetchone()
    if not w:
        return jsonify({'error': '小工具不存在'}), 404
    if w['user_id'] != session['user_id']:
        return jsonify({'error': '无权操作'}), 403
    updates, params = [], []
    # v25.9：解除公共池关联（从此独立，不再跟随池更新）
    if data.get('unlink_pool'):
        updates.append('pool_id = 0')
    for field in ['title', 'kind', 'icon', 'url', 'sort_order', 'enabled']:
        if field in data:
            updates.append(f'{field} = ?')
            params.append(data[field])
    if 'config' in data:
        cfg = data['config']
        if isinstance(cfg, dict):
            cfg = json.dumps(cfg, ensure_ascii=False)
        elif not isinstance(cfg, str):
            cfg = ''
        updates.append('config = ?')
        params.append(cfg)
    if updates:
        params.append(widget_id)
        db.execute(f'UPDATE user_widgets SET {", ".join(updates)} WHERE id = ?', params)
        db.commit()
    w = db.execute('SELECT * FROM user_widgets WHERE id = ?', (widget_id,)).fetchone()
    return jsonify(dict(w))


@app.route('/api/widgets/<int:widget_id>', methods=['DELETE'])
@login_required
def delete_widget(widget_id):
    db = get_db()
    w = db.execute('SELECT * FROM user_widgets WHERE id = ?', (widget_id,)).fetchone()
    if not w:
        return jsonify({'error': '小工具不存在'}), 404
    if w['user_id'] != session['user_id']:
        return jsonify({'error': '无权操作'}), 403
    db.execute('DELETE FROM user_widgets WHERE id = ?', (widget_id,))
    db.commit()
    return jsonify({'message': '已删除'})


# ====================================================================
# 轻量磁贴广场（v25.6）
# ====================================================================
# 数据模型：
# - tile_link_pool / tile_tool_pool：公共池，所有人可见、可复用
# - user_tiles：用户个人磁贴实例（引用 pool_id + pool_type，可覆盖标题/图标/URL）
# ====================================================================

def _normalize_url(url):
    url = (url or '').strip()
    if url and not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    return url


@app.route('/api/tile-links')
@login_required
def list_tile_links():
    """公共链接池列表（按使用次数和创建时间倒序）"""
    db = get_db()
    rows = db.execute(
        'SELECT p.*, u.display_name as creator_name FROM tile_link_pool p '
        'LEFT JOIN users u ON u.id = p.created_by '
        'ORDER BY p.use_count DESC, p.created_at DESC'
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/tile-links', methods=['POST'])
@login_required
def add_tile_link():
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    url = _normalize_url(data.get('url'))
    if not title or not url:
        return jsonify({'error': '标题和URL不能为空'}), 400
    icon = (data.get('icon') or '').strip()
    color = (data.get('color') or '').strip()
    db = get_db()
    cur = db.execute(
        'INSERT INTO tile_link_pool (title, url, icon, color, created_by) VALUES (?, ?, ?, ?, ?)',
        (title, url, icon, color, session['user_id'])
    )
    db.commit()
    row = db.execute(
        'SELECT p.*, u.display_name as creator_name FROM tile_link_pool p '
        'LEFT JOIN users u ON u.id = p.created_by WHERE p.id = ?', (cur.lastrowid,)
    ).fetchone()
    return jsonify(dict(row)), 201


@app.route('/api/tile-links/<int:link_id>', methods=['PUT'])
@login_required
def update_tile_link(link_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    link = db.execute('SELECT * FROM tile_link_pool WHERE id = ?', (link_id,)).fetchone()
    if not link:
        return jsonify({'error': '链接不存在'}), 404
    updates, params = [], []
    for field in ['title', 'icon', 'color']:
        if field in data:
            updates.append(f'{field} = ?')
            params.append(data[field])
    if 'url' in data:
        url = _normalize_url(data['url'])
        if not url:
            return jsonify({'error': 'URL不能为空'}), 400
        updates.append('url = ?')
        params.append(url)
    if updates:
        # v25.7 同步：编辑公共池链接时自动刷新 updated_at，前台 GET /api/quick-links 据此识别需破坏缓存
        updates.append('updated_at = CURRENT_TIMESTAMP')
        params.append(link_id)
        db.execute(f'UPDATE tile_link_pool SET {", ".join(updates)} WHERE id = ?', params)
        db.commit()
    row = db.execute(
        'SELECT p.*, u.display_name as creator_name FROM tile_link_pool p '
        'LEFT JOIN users u ON u.id = p.created_by WHERE p.id = ?', (link_id,)
    ).fetchone()
    return jsonify(dict(row))


@app.route('/api/tile-links/<int:link_id>', methods=['DELETE'])
@login_required
def delete_tile_link(link_id):
    db = get_db()
    link = db.execute('SELECT * FROM tile_link_pool WHERE id = ?', (link_id,)).fetchone()
    if not link:
        return jsonify({'error': '链接不存在'}), 404
    db.execute('DELETE FROM user_tiles WHERE pool_type = ? AND pool_id = ?', ('link', link_id))
    db.execute('DELETE FROM tile_link_pool WHERE id = ?', (link_id,))
    db.commit()
    return jsonify({'message': '已删除'})


@app.route('/api/tile-tools')
@login_required
def list_tile_tools():
    """公共小工具池列表"""
    db = get_db()
    rows = db.execute(
        'SELECT p.*, u.display_name as creator_name FROM tile_tool_pool p '
        'LEFT JOIN users u ON u.id = p.created_by '
        'ORDER BY p.use_count DESC, p.created_at DESC'
    ).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        try:
            item['config'] = json.loads(item.get('config') or '{}')
        except Exception:
            item['config'] = {}
        result.append(item)
    return jsonify(result)


@app.route('/api/tile-tools', methods=['POST'])
@login_required
def add_tile_tool():
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    kind = (data.get('kind') or 'iframe').strip()
    url = _normalize_url(data.get('url'))
    if not title:
        return jsonify({'error': '标题不能为空'}), 400
    if kind == 'iframe' and not url:
        return jsonify({'error': 'iframe 地址不能为空'}), 400
    icon = (data.get('icon') or '').strip()
    config = data.get('config') or {}
    if isinstance(config, dict):
        config = json.dumps(config, ensure_ascii=False)
    elif not isinstance(config, str):
        config = ''
    db = get_db()
    cur = db.execute(
        'INSERT INTO tile_tool_pool (title, kind, url, icon, config, created_by) VALUES (?, ?, ?, ?, ?, ?)',
        (title, kind, url, icon, config, session['user_id'])
    )
    db.commit()
    row = db.execute(
        'SELECT p.*, u.display_name as creator_name FROM tile_tool_pool p '
        'LEFT JOIN users u ON u.id = p.created_by WHERE p.id = ?', (cur.lastrowid,)
    ).fetchone()
    item = dict(row)
    try:
        item['config'] = json.loads(item.get('config') or '{}')
    except Exception:
        item['config'] = {}
    return jsonify(item), 201


@app.route('/api/tile-tools/<int:tool_id>', methods=['PUT'])
@login_required
def update_tile_tool(tool_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    tool = db.execute('SELECT * FROM tile_tool_pool WHERE id = ?', (tool_id,)).fetchone()
    if not tool:
        return jsonify({'error': '小工具不存在'}), 404
    updates, params = [], []
    for field in ['title', 'kind', 'icon']:
        if field in data:
            updates.append(f'{field} = ?')
            params.append(data[field])
    if 'url' in data:
        url = _normalize_url(data['url'])
        updates.append('url = ?')
        params.append(url)
    if 'config' in data:
        cfg = data['config']
        if isinstance(cfg, dict):
            cfg = json.dumps(cfg, ensure_ascii=False)
        elif not isinstance(cfg, str):
            cfg = ''
        updates.append('config = ?')
        params.append(cfg)
    if updates:
        # v25.9 同步：编辑公共池工具时刷新 updated_at，前台据此识别需更新的用户实例
        updates.append('updated_at = CURRENT_TIMESTAMP')
        params.append(tool_id)
        db.execute(f'UPDATE tile_tool_pool SET {", ".join(updates)} WHERE id = ?', params)
        db.commit()
    row = db.execute(
        'SELECT p.*, u.display_name as creator_name FROM tile_tool_pool p '
        'LEFT JOIN users u ON u.id = p.created_by WHERE p.id = ?', (tool_id,)
    ).fetchone()
    item = dict(row)
    try:
        item['config'] = json.loads(item.get('config') or '{}')
    except Exception:
        item['config'] = {}
    return jsonify(item)


@app.route('/api/tile-tools/<int:tool_id>', methods=['DELETE'])
@login_required
def delete_tile_tool(tool_id):
    db = get_db()
    tool = db.execute('SELECT * FROM tile_tool_pool WHERE id = ?', (tool_id,)).fetchone()
    if not tool:
        return jsonify({'error': '小工具不存在'}), 404
    db.execute('DELETE FROM user_tiles WHERE pool_type = ? AND pool_id = ?', ('tool', tool_id))
    # v25.9：级联删除所有用户侧边栏中关联此池工具的个人实例
    db.execute('DELETE FROM user_widgets WHERE pool_id = ?', (tool_id,))
    db.execute('DELETE FROM tile_tool_pool WHERE id = ?', (tool_id,))
    db.commit()
    return jsonify({'message': '已删除'})


@app.route('/api/user-tiles')
@login_required
def list_user_tiles():
    """当前用户的磁贴广场（把 pool 数据与用户覆盖字段合并）"""
    uid = session['user_id']
    db = get_db()
    rows = db.execute(
        'SELECT * FROM user_tiles WHERE user_id = ? ORDER BY sort_order, id',
        (uid,)
    ).fetchall()
    result = []
    for t in rows:
        item = dict(t)
        if item['pool_type'] == 'link':
            pool = db.execute('SELECT * FROM tile_link_pool WHERE id = ?', (item['pool_id'],)).fetchone()
            if pool:
                item['title'] = item['title'] or pool['title']
                item['url'] = item['url'] or pool['url']
                item['icon'] = item['icon'] or pool['icon']
                item['color'] = pool['color']
        else:
            pool = db.execute('SELECT * FROM tile_tool_pool WHERE id = ?', (item['pool_id'],)).fetchone()
            if pool:
                item['title'] = item['title'] or pool['title']
                item['url'] = item['url'] or pool['url']
                item['icon'] = item['icon'] or pool['icon']
                item['kind'] = pool['kind']
                try:
                    item['config'] = json.loads(item.get('config') or pool['config'] or '{}')
                except Exception:
                    item['config'] = {}
        result.append(item)
    return jsonify(result)


@app.route('/api/user-tiles', methods=['POST'])
@login_required
def add_user_tile():
    data = request.get_json(silent=True) or {}
    uid = session['user_id']
    pool_type = (data.get('pool_type') or '').strip()
    pool_id = data.get('pool_id')
    create_new = data.get('create_new')

    db = get_db()

    # 模式 A：直接新建并添加到广场
    if create_new:
        if pool_type == 'link':
            title = (data.get('title') or '').strip()
            url = _normalize_url(data.get('url'))
            if not title or not url:
                return jsonify({'error': '标题和URL不能为空'}), 400
            icon = (data.get('icon') or '').strip()
            color = (data.get('color') or '').strip()
            cur = db.execute(
                'INSERT INTO tile_link_pool (title, url, icon, color, created_by) VALUES (?, ?, ?, ?, ?)',
                (title, url, icon, color, uid)
            )
            pool_id = cur.lastrowid
            pool_type = 'link'
        else:
            title = (data.get('title') or '').strip()
            kind = (data.get('kind') or 'iframe').strip()
            url = _normalize_url(data.get('url'))
            if not title:
                return jsonify({'error': '标题不能为空'}), 400
            if kind == 'iframe' and not url:
                return jsonify({'error': 'iframe 地址不能为空'}), 400
            icon = (data.get('icon') or '').strip()
            config = data.get('config') or {}
            if isinstance(config, dict):
                config = json.dumps(config, ensure_ascii=False)
            cur = db.execute(
                'INSERT INTO tile_tool_pool (title, kind, url, icon, config, created_by) VALUES (?, ?, ?, ?, ?, ?)',
                (title, kind, url, icon, config, uid)
            )
            pool_id = cur.lastrowid
            pool_type = 'tool'
        db.commit()

    if pool_type not in ('link', 'tool') or not pool_id:
        return jsonify({'error': '参数错误'}), 400

    # 检查 pool 是否存在
    pool_table = 'tile_link_pool' if pool_type == 'link' else 'tile_tool_pool'
    pool = db.execute(f'SELECT * FROM {pool_table} WHERE id = ?', (pool_id,)).fetchone()
    if not pool:
        return jsonify({'error': '池子中不存在该条目'}), 404

    # 防重复添加
    existing = db.execute(
        'SELECT id FROM user_tiles WHERE user_id = ? AND pool_type = ? AND pool_id = ?',
        (uid, pool_type, pool_id)
    ).fetchone()
    if existing:
        return jsonify({'error': '该磁贴已存在'}), 400

    # 更新使用计数
    db.execute(
        f'UPDATE {pool_table} SET use_count = use_count + 1 WHERE id = ?', (pool_id,)
    )
    max_order = db.execute(
        'SELECT COALESCE(MAX(sort_order), 0) as m FROM user_tiles WHERE user_id = ?', (uid,)
    ).fetchone()['m']
    cur = db.execute(
        'INSERT INTO user_tiles (user_id, pool_type, pool_id, sort_order) VALUES (?, ?, ?, ?)',
        (uid, pool_type, pool_id, max_order + 1)
    )
    db.commit()
    return jsonify({'id': cur.lastrowid, 'pool_type': pool_type, 'pool_id': pool_id}), 201


@app.route('/api/user-tiles/<int:tile_id>', methods=['PUT'])
@login_required
def update_user_tile(tile_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    tile = db.execute('SELECT * FROM user_tiles WHERE id = ?', (tile_id,)).fetchone()
    if not tile:
        return jsonify({'error': '磁贴不存在'}), 404
    if tile['user_id'] != session['user_id']:
        return jsonify({'error': '无权操作'}), 403
    updates, params = [], []
    for field in ['title', 'icon', 'url', 'sort_order']:
        if field in data:
            updates.append(f'{field} = ?')
            params.append(data[field])
    if 'config' in data:
        cfg = data['config']
        if isinstance(cfg, dict):
            cfg = json.dumps(cfg, ensure_ascii=False)
        elif not isinstance(cfg, str):
            cfg = ''
        updates.append('config = ?')
        params.append(cfg)
    if updates:
        params.append(tile_id)
        db.execute(f'UPDATE user_tiles SET {", ".join(updates)} WHERE id = ?', params)
        db.commit()
    return jsonify({'message': '已更新'})


@app.route('/api/user-tiles/<int:tile_id>', methods=['DELETE'])
@login_required
def delete_user_tile(tile_id):
    db = get_db()
    tile = db.execute('SELECT * FROM user_tiles WHERE id = ?', (tile_id,)).fetchone()
    if not tile:
        return jsonify({'error': '磁贴不存在'}), 404
    if tile['user_id'] != session['user_id']:
        return jsonify({'error': '无权操作'}), 403
    db.execute('DELETE FROM user_tiles WHERE id = ?', (tile_id,))
    db.commit()
    return jsonify({'message': '已删除'})


# ====================================================================
# API Key 管理（每用户独立，用于 MCP/外部接入）
# ====================================================================
@app.route('/api/api-keys')
@login_required
def list_api_keys():
    db = get_db()
    keys = db.execute(
        'SELECT id, key_prefix, label, created_at, last_used_at FROM api_keys WHERE user_id = ? ORDER BY id DESC',
        (session['user_id'],)
    ).fetchall()
    return jsonify([dict(k) for k in keys])


@app.route('/api/api-keys', methods=['POST'])
@login_required
def generate_api_key():
    data = request.get_json(silent=True) or {}
    label = (data.get('label') or '').strip() or '默认'
    raw_key = 'iw_' + secrets.token_hex(24)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:12] + '...'
    db = get_db()
    cur = db.execute(
        'INSERT INTO api_keys (user_id, key_hash, key_prefix, label) VALUES (?, ?, ?, ?)',
        (session['user_id'], key_hash, key_prefix, label)
    )
    db.commit()
    return jsonify({
        'id': cur.lastrowid,
        'key': raw_key,
        'key_prefix': key_prefix,
        'label': label,
        'message': '请妥善保存此 API Key，关闭后将不再显示完整 Key'
    }), 201


@app.route('/api/api-keys/<int:key_id>', methods=['DELETE'])
@login_required
def delete_api_key(key_id):
    db = get_db()
    key = db.execute('SELECT * FROM api_keys WHERE id = ?', (key_id,)).fetchone()
    if not key:
        return jsonify({'error': 'API Key 不存在'}), 404
    if key['user_id'] != session['user_id']:
        return jsonify({'error': '无权操作'}), 403
    db.execute('DELETE FROM api_keys WHERE id = ?', (key_id,))
    db.commit()
    return jsonify({'message': '已撤销'})


# ====================================================================
# 侧边栏统计（日历维度 + AI 建议）
# ====================================================================
@app.route('/api/sidebar/stats')
@login_required
def sidebar_stats():
    """左侧边栏：时间 + 日历维度统计 + AI 建议"""
    db = get_db()
    uid = session['user_id']
    today = date.today()
    today_str = today.isoformat()

    # 本月每日工作项统计
    month_start = today.replace(day=1).isoformat()
    month_end = today.replace(day=calendar.monthrange(today.year, today.month)[1]).isoformat()
    daily_counts = db.execute("""
        SELECT date(created_at) as day, COUNT(*) as total,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as done
        FROM work_items
        WHERE user_id = ? AND created_at >= ? AND created_at <= ?
        GROUP BY date(created_at) ORDER BY day
    """, (uid, month_start, month_end + ' 23:59:59')).fetchall()

    # 今日统计
    today_items = db.execute(
        "SELECT COUNT(*) as c FROM work_items WHERE user_id = ? AND (due_date = ? OR created_at >= ?)",
        (uid, today_str, today_str + ' 00:00:00')
    ).fetchone()

    # 本周统计
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    week_items = db.execute(
        "SELECT COUNT(*) as total, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as done FROM work_items WHERE user_id = ? AND created_at >= ?",
        (uid, week_start)
    ).fetchone()

    # v25.9 修复：待处理只统计 status='pending'，与主区域 /api/stats 口径一致
    # 同时纳入协同任务（与 /api/stats 个人视图对齐）
    collab_like = f'%,{uid},%'
    pending = db.execute(
        "SELECT COUNT(*) as c FROM work_items WHERE (user_id=? OR (',' || collaborators || ',') LIKE ?) AND status='pending'",
        (uid, collab_like)
    ).fetchone()['c']

    # 逾期数（同样纳入协同任务）
    overdue = db.execute(
        "SELECT COUNT(*) as c FROM work_items WHERE (user_id=? OR (',' || collaborators || ',') LIKE ?) AND status!='completed' AND due_date!='' AND due_date < ?",
        (uid, collab_like, today_str)
    ).fetchone()['c']

    return jsonify({
        'date': today_str,
        'weekday': ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][today.weekday()],
        'today_count': today_items['c'],
        'week_total': week_items['total'] or 0,
        'week_done': week_items['done'] or 0,
        'overdue': overdue,
        'pending': pending,
        'daily_counts': [dict(d) for d in daily_counts]
    })


# ====================================================================
# 报告生成（日报/周报/月报 AI 自动总结）
# v9：素材 = work_items + 知识库（MCP 回传），输出结构化 HTML + 统计图表
# ====================================================================
def _report_materials(db, uid, start, end):
    """聚合报告素材：work_items + 知识库（钉钉/MCP 回传），返回统计字典
    v17：统计口径整合 —— 工作台事项 + 钉钉待办按「标题+截止日」去重；
    子任务全部完成视为父任务完成。"""
    # v25.5：报告素材包含本人任务 + 作为协同者的任务（完成双算）
    collab_like = f'%,{uid},%'
    items = [dict(i) for i in db.execute("""
        SELECT * FROM work_items
        WHERE (user_id = ? OR (',' || collaborators || ',') LIKE ?) AND (
            (created_at >= ? AND created_at <= ?)
            OR (completed_at >= ? AND completed_at <= ?)
            OR (due_date >= ? AND due_date <= ?)
            OR (status = 'in_progress')
        )
        ORDER BY created_at DESC
    """, (uid, collab_like, start + ' 00:00:00', end + ' 23:59:59',
          start + ' 00:00:00', end + ' 23:59:59',
          start, end)).fetchall()]

    # v26.4 修复：MySQL 驱动对 timestamp 列返回 datetime 对象，与字符串比较/切片会崩溃，统一规整
    def _norm_dt(v):
        if isinstance(v, datetime):
            return v.strftime('%Y-%m-%d %H:%M:%S')
        if isinstance(v, date):
            return v.strftime('%Y-%m-%d')
        return v
    for _i in items:
        for _f in ('created_at', 'completed_at', 'updated_at', 'started_at'):
            if _i.get(_f) is not None:
                _i[_f] = _norm_dt(_i[_f])

    # v17：子任务完成情况 —— 父任务下所有子任务均完成 → 父任务视为完成
    parent_all_done = {}
    sub_map = {}
    for s in db.execute(
            "SELECT parent_id, status FROM work_items WHERE (user_id=? OR (',' || collaborators || ',') LIKE ?) AND parent_id IS NOT NULL",
            (uid, collab_like)).fetchall():
        sub_map.setdefault(s['parent_id'], []).append(s['status'])
    for pid, sts in sub_map.items():
        if sts and all(st == 'completed' for st in sts):
            parent_all_done[pid] = True

    s0, e1 = start + ' 00:00:00', end + ' 23:59:59'
    # v27.0：iTop 工单素材（周期内新建/解决/关闭）
    itop_rows = db.execute('''
        SELECT ticket_ref, ticket_class, title, status, priority, agent_name,
               start_date, resolution_date, close_date, time_spent
        FROM itop_tickets
        WHERE user_id = ? AND (
            (start_date != '' AND start_date BETWEEN ? AND ?)
            OR (resolution_date != '' AND resolution_date BETWEEN ? AND ?)
            OR (close_date != '' AND close_date BETWEEN ? AND ?)
        )
        ORDER BY COALESCE(NULLIF(resolution_date, ''), NULLIF(close_date, ''), start_date) DESC
        LIMIT 100
    ''', (uid, s0, e1, s0, e1, s0, e1)).fetchall()
    created_items = [i for i in items if i['created_at'] and s0 <= i['created_at'] <= e1]
    completed_items = [i for i in items if
                       (i['status'] == 'completed' and i['completed_at'] and s0 <= i['completed_at'] <= e1)
                       or (i['status'] != 'completed' and parent_all_done.get(i['id']))]
    due_items = [i for i in items if i['due_date'] and start <= i['due_date'] <= end]
    in_progress_items = [i for i in items if i['status'] == 'in_progress' and not parent_all_done.get(i['id'])]
    pending_items = [i for i in items if i['status'] == 'pending' and not parent_all_done.get(i['id'])]
    overdue_items = [i for i in items if i['due_date'] and i['due_date'] < start
                     and i['status'] != 'completed' and not parent_all_done.get(i['id'])]

    # 分类分布
    cat_counts = {}
    for i in items:
        c = i['category'] or '未分类'
        cat_counts[c] = cat_counts.get(c, 0) + 1
    cat_sorted = sorted(cat_counts.items(), key=lambda x: -x[1])

    # 周期内按天分布（新建）
    day_counts = {}
    d = datetime.strptime(start, '%Y-%m-%d')
    end_d = datetime.strptime(end, '%Y-%m-%d')
    while d <= end_d:
        day_counts[d.strftime('%Y-%m-%d')] = 0
        d += timedelta(days=1)
    for i in created_items:
        cd = (i['created_at'] or '')[:10]
        if cd in day_counts:
            day_counts[cd] += 1

    # 知识库素材（MCP 回传：钉钉待办/日程/聊天/听记）
    kb = {}
    kb_titles = {}
    for src, key in (('dingtalk_todo', 'todo'), ('dingtalk_calendar', 'calendar'),
                     ('dingtalk_chat', 'chat'), ('dingtalk_minutes', 'minutes')):
        rows = db.execute(
            "SELECT * FROM user_knowledge WHERE user_id=? AND source=? AND occur_date>=? AND occur_date<=? "
            "ORDER BY event_time DESC LIMIT 50",
            (uid, src, start, end)).fetchall()
        # v25.7：聊天记录过滤——单聊保留，群聊只保留自己发送的
        if src == 'dingtalk_chat':
            rows = _filter_chat_records(rows, uid, db)
        kb[key] = len(rows)
        kb_titles[key] = [{'title': r['title'], 'event_time': r['event_time']} for r in rows[:6]]

    # v15：周期内对话原文（供 AI 提取关键工作内容），最多 40 条
    # v25.7：群聊仅保留自己发送的内容
    chat_rows = db.execute(
        "SELECT * FROM user_knowledge WHERE user_id=? AND source='dingtalk_chat' "
        "AND occur_date>=? AND occur_date<=? ORDER BY event_time DESC LIMIT 40",
        (uid, start, end)).fetchall()
    chat_rows = _filter_chat_records(chat_rows, uid, db)
    chat_content = []
    for r in chat_rows:
        body = (r['content'] or '').replace('会话：', '').replace('发送人：', '').replace('时间：', '').replace('内容：', '')
        chat_content.append({
            'title': (r['title'] or '')[:60],
            'body': body[:150],
            'event_time': r['event_time'] or '',
        })

    # v15：周期内待办明细（供时效统计），最多 80 条
    todo_rows = [dict(r) for r in db.execute(
        "SELECT title, content, event_time FROM user_knowledge WHERE user_id=? AND source='dingtalk_todo' "
        "AND occur_date>=? AND occur_date<=? ORDER BY event_time DESC LIMIT 80",
        (uid, start, end)).fetchall()]
    # v17：整合统计（工作台事项 + 钉钉待办去重 + 子任务完成归并）
    todo_stats = _calc_merged_todo_stats(items, todo_rows, end, parent_all_done)

    return {
        'items': items,
        'created_items': created_items, 'completed_items': completed_items,
        'due_items': due_items, 'in_progress_items': in_progress_items,
        'pending_items': pending_items, 'overdue_items': overdue_items,
        'cat_sorted': cat_sorted, 'day_counts': day_counts,
        'kb': kb, 'kb_titles': kb_titles,
        'chat_content': chat_content, 'todo_rows': todo_rows, 'todo_stats': todo_stats,
        'itop_tickets': [dict(r) for r in itop_rows],
    }


def _calc_merged_todo_stats(items, todo_rows, period_end, parent_all_done):
    """v17 整合统计（替代原 _calc_todo_stats）：
    - 工作台主任务（排除子任务，子任务归并到父任务）+ 钉钉待办（按「标题+截止日」与工作台去重）
    - 完成判定：工作台 status=completed 或子任务全部完成；钉钉 content 含「状态：已完成」
    - 逾期 = 截止日 < 周期末 且未完成
    返回 total/done/undone/overdue/done_rate + 工作台/钉钉细分（work_items_total 等）。"""
    import re as _re
    mains = [i for i in items if not i.get('parent_id')]
    # 工作台完成/逾期
    done_ids, over_ids = set(), set()
    for i in mains:
        is_done = i['status'] == 'completed' or parent_all_done.get(i['id'])
        if is_done:
            done_ids.add(i['id'])
        elif i['due_date'] and i['due_date'] < str(period_end):
            over_ids.add(i['id'])
    # 工作台去重键（标题 + 截止日）
    w_keys = set()
    for i in mains:
        w_keys.add(((i['title'] or '').strip(), (i['due_date'] or '')[:10]))
    # 钉钉待办去重：与工作台同标题+同截止日的视为重复，不再计数
    dt_done = dt_over = 0
    dt_items = []
    for t in todo_rows:
        c = t.get('content') or ''
        is_done = ('状态：已完成' in c) or ('状态:已完成' in c)
        due = ''
        m = _re.search(r'截止：([^\n]*)', c)
        if m:
            due = m.group(1).strip()
        title = (t.get('title') or '').replace('📌', '').replace('🗓️', '').strip()
        if (title, due[:10]) in w_keys:
            continue  # 与工作台重复，去重
        dt_items.append(t)
        if is_done:
            dt_done += 1
        else:
            d10 = due[:10]
            if d10 and re.match(r'^\d{4}-\d{2}-\d{2}$', d10) and d10 < str(period_end):
                dt_over += 1
    total = len(mains) + len(dt_items)
    done = len(done_ids) + dt_done
    undone = total - done
    overdue = len(over_ids) + dt_over
    rate = round(done * 100.0 / total, 1) if total else 0.0
    return {
        'total': total, 'done': done, 'undone': undone,
        'overdue': overdue, 'done_rate': rate,
        'work_items_total': len(mains), 'work_items_done': len(done_ids),
        'dingtalk_total': len(dt_items), 'dingtalk_done': dt_done,
    }


def _ai_json(system, user, max_tokens=1500, feature='report'):
    """调用 AI 并解析 JSON 输出（自动剥离 markdown 围栏）"""
    raw = ai_chat(system, user, max_tokens=max_tokens, feature=feature)
    txt = raw.strip()
    if txt.startswith('```'):
        txt = txt.strip('`')
        if txt.lower().startswith('json'):
            txt = txt[4:]
        txt = txt.strip()
    try:
        return json.loads(txt)
    except Exception:
        # 尝试提取第一个 { ... } 块
        i, j = txt.find('{'), txt.rfind('}')
        if 0 <= i < j:
            try:
                return json.loads(txt[i:j + 1])
            except Exception:
                pass
        raise ValueError(f'AI 输出非 JSON: {raw[:200]}')


def _esc(s):
    """HTML 转义"""
    return (str(s or '')
            .replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;').replace("'", '&#39;'))


def _report_donut(status_counts):
    """SVG 环形图：进行中/已完成/待处理/超期"""
    colors = [('#58a6ff', '进行中'), ('#3fb950', '已完成'), ('#8b949e', '待处理'), ('#f85149', '超期')]
    total = sum(status_counts.values()) or 1
    circles, legend = [], []
    offset = 0
    for color, label in colors:
        cnt = status_counts.get(label, 0)
        pct = cnt * 100.0 / total
        if cnt > 0:
            circles.append(
                f'<circle cx="60" cy="60" r="42" fill="none" stroke="{color}" stroke-width="14" '
                f'pathLength="100" stroke-dasharray="{pct:.1f} 100" '
                f'stroke-dashoffset="{-offset:.1f}" transform="rotate(-90 60 60)"/>'
            )
            legend.append(
                f'<span class="rp-lg"><i style="background:{color}"></i>{label} {cnt}</span>'
            )
        offset += pct
    if not circles:
        circles = ['<circle cx="60" cy="60" r="42" fill="none" stroke="#2d333d" stroke-width="14"/>']
        legend = ['<span class="rp-lg"><i style="background:#2d333d"></i>暂无数据</span>']
    return (f'<svg viewBox="0 0 120 120" width="132" height="132" class="rp-donut">'
            f'<circle cx="60" cy="60" r="42" fill="none" stroke="#2d333d" stroke-width="14"/>{"" .join(circles)}</svg>'
            f'<div class="rp-legend">{"" .join(legend)}</div>')


def _report_cat_bars(cat_sorted):
    """CSS 横向条形图：事项分类分布"""
    total = sum(c for _, c in cat_sorted) or 1
    bars = []
    for name, cnt in cat_sorted[:8]:
        pct = cnt * 100.0 / total
        bars.append(
            f'<div class="rp-bar"><span class="rp-bar-l">{_esc(name)}</span>'
            f'<div class="rp-bar-track"><div class="rp-bar-fill" style="width:{pct:.1f}%"></div></div>'
            f'<span class="rp-bar-v">{cnt}</span></div>'
        )
    return ''.join(bars) or '<div class="rp-empty">暂无分类数据</div>'


def _report_day_chart(day_counts):
    """CSS 柱状图：周期内每日新建"""
    vals = list(day_counts.values())
    mx = max(vals) or 1
    cells = []
    for day, cnt in day_counts.items():
        h = max(cnt * 100.0 / mx, 2 if cnt else 0)
        label = day[5:]  # MM-DD
        cells.append(
            f'<div class="rp-day"><div class="rp-day-bar" style="height:{h:.0f}%">'
            f'{"<b>" + str(cnt) + "</b>" if cnt else ""}</div>'
            f'<span class="rp-day-l">{label}</span></div>'
        )
    return f'<div class="rp-daychart">{"" .join(cells)}</div>'


def _report_kb_block(kb, kb_titles):
    """知识库素材（MCP 回传）统计卡"""
    labels = {'todo': '📌 待办', 'calendar': '🗓️ 日程', 'chat': '💬 聊天', 'minutes': '🎙️ 听记'}
    cards = []
    for key in ('todo', 'calendar', 'chat', 'minutes'):
        titles = kb_titles.get(key, [])
        t_list = ''.join(
            f'<div class="rp-kb-t"><span class="rp-kb-tm">{_esc(t["event_time"][11:16] if t["event_time"] else "")}</span>'
            f'<span class="rp-kb-tt">{_esc((t["title"] or "").replace("💬", "").replace("📌", "").replace("🗓️", "").replace("🎙️", "").strip()[:38])}</span></div>'
            for t in titles[:5]
        )
        cards.append(
            f'<div class="rp-kb-card"><div class="rp-kb-h"><span>{labels[key]}</span><b>{kb.get(key, 0)}</b></div>'
            f'<div class="rp-kb-list">{"<div class=\"rp-empty\">周期内无</div>" if not t_list else t_list}</div></div>'
        )
    return f'<div class="rp-kbgrid">{"".join(cards)}</div>'


def _build_report_html(period_label, user, mat, ai):
    """组装结构化 HTML 报告（统计图表 + AI 总结区块）"""
    sc = {
        '进行中': len(mat['in_progress_items']),
        '已完成': len(mat['completed_items']),
        '待处理': len(mat['pending_items']),
        '超期': len(mat['overdue_items']),
    }
    donut = _report_donut(sc)
    cat_bars = _report_cat_bars(mat['cat_sorted'])
    day_chart = _report_day_chart(mat['day_counts'])
    kb_block = _report_kb_block(mat['kb'], mat['kb_titles'])
    # v27.0：ITSM 工单块
    _it = mat.get('itop_tickets') or []
    _it_done = [x for x in _it if x['status'] in ('resolved', 'closed', 'reject')]
    _it_open = [x for x in _it if x['status'] not in ('resolved', 'closed', 'reject')]
    _it_list = ''.join(
        '<li>' + _esc(x['ticket_ref']) + ' &middot; ' + _esc((x['title'] or '')[:50])
        + '（' + _esc(x['status']) + '）</li>'
        for x in (_it_done[:8] + _it_open[:8]))
    itop_block = (
        '<div class="rp-todo-kpis">'
        + '<div class="rp-kpi"><b>' + str(len(_it)) + '</b><span>本期涉及工单</span></div>'
        + '<div class="rp-kpi ok"><b>' + str(len(_it_done)) + '</b><span>已解决/关闭</span></div>'
        + '<div class="rp-kpi warn"><b>' + str(len(_it_open)) + '</b><span>处理中</span></div>'
        + '</div>'
        + ('<ul class="rp-list ok" style="margin-top:8px">' + _it_list + '</ul>' if _it_list
           else '<div class="rp-empty">本期无工单</div>')
    )

    def li_list(items, cls):
        if not items:
            return '<div class="rp-empty">本期无</div>'
        return '<ul class="rp-list ' + cls + '">' + ''.join(f'<li>{_esc(x)}</li>' for x in items) + '</ul>'

    # v17：待办时效统计卡（工作台 + 钉钉整合去重，子任务完成归并）
    ts = mat.get('todo_stats') or {'total': 0, 'done': 0, 'undone': 0, 'overdue': 0, 'done_rate': 0,
                                   'work_items_total': 0, 'work_items_done': 0,
                                   'dingtalk_total': 0, 'dingtalk_done': 0}
    todo_block = (
        f'<div class="rp-todo-kpis">'
        f'<div class="rp-kpi"><b>{ts.get("total", 0)}</b><span>涉及待办</span></div>'
        f'<div class="rp-kpi ok"><b>{ts.get("done", 0)}</b><span>已完成</span></div>'
        f'<div class="rp-kpi warn"><b>{ts.get("undone", 0)}</b><span>未完成</span></div>'
        f'<div class="rp-kpi bad"><b>{ts.get("overdue", 0)}</b><span>逾期未完成</span></div>'
        f'<div class="rp-kpi prog"><b>{ts.get("done_rate", 0)}%</b><span>完成率</span></div>'
        f'</div>'
        f'<div style="font-size:11px;color:var(--muted);margin-top:6px">'
        f'口径：工作台事项（含子任务归并）{ts.get("work_items_total", 0)} 项（完成 {ts.get("work_items_done", 0)}）'
        f' + 钉钉待办（与工作台去重后）{ts.get("dingtalk_total", 0)} 项（完成 {ts.get("dingtalk_done", 0)}），按「标题+截止日」去重合并计数</div>'
    )
    # v15：AI 关键工作内容 / 对话主题
    ai_block = f'''
  <div class="rp-card"><div class="rp-card-t">💼 关键工作内容（AI 从对话/待办/工作项提炼）</div>{li_list(ai.get("key_work", []), "ok")}</div>
  <div class="rp-card"><div class="rp-card-t">🗣️ 对话主题分析</div>{li_list(ai.get("chat_topics", []), "plan")}</div>
  <div class="rp-card"><div class="rp-card-t">⏱️ 待办处理与时效统计</div>{todo_block}</div>
  <div class="rp-card"><div class="rp-card-t">🎫 ITSM 工单（iTop，MCP 同步）</div>{itop_block}</div>'''

    html = f'''
<div class="rp-wrap">
  <div class="rp-head">
    <div class="rp-title">{_esc(period_label)}</div>
    <div class="rp-sub">{_esc(user)} · {_esc(datetime.now().strftime("%Y-%m-%d %H:%M"))} 生成</div>
  </div>
  <div class="rp-kpis">
    <div class="rp-kpi"><b>{len(mat["created_items"])}</b><span>本期新建</span></div>
    <div class="rp-kpi ok"><b>{len(mat["completed_items"])}</b><span>本期完成</span></div>
    <div class="rp-kpi warn"><b>{len(mat["due_items"])}</b><span>本期截止</span></div>
    <div class="rp-kpi prog"><b>{len(mat["in_progress_items"])}</b><span>进行中</span></div>
  </div>
  <div class="rp-grid2">
    <div class="rp-card"><div class="rp-card-t">工作状态分布</div>{donut}</div>
    <div class="rp-card"><div class="rp-card-t">事项分类分布</div>{cat_bars}</div>
  </div>
  <div class="rp-card"><div class="rp-card-t">每日动态（新建事项）</div>{day_chart}</div>
  <div class="rp-card"><div class="rp-card-t">知识库素材（钉钉 / MCP 回传）</div>{kb_block}</div>
  {ai_block}
  <div class="rp-ai"><div class="rp-card-t">📝 本期总结</div><p class="rp-summary">{_esc(ai.get("summary", "本期暂无总结"))}</p></div>
  <div class="rp-card"><div class="rp-card-t">✅ 主要成果</div>{li_list(ai.get("highlights", []), "ok")}</div>
  <div class="rp-card"><div class="rp-card-t">⚠️ 待解决问题</div>{li_list(ai.get("issues", []), "warn")}</div>
  <div class="rp-card"><div class="rp-card-t">🎯 下期计划</div>{li_list(ai.get("plan", []), "plan")}</div>
</div>'''
    return html


def _report_text(period_label, user, mat, ai):
    """纯文本 Markdown 版（兼容旧客户端）"""
    def b(items):
        return ''.join(f'- {x}\n' for x in items) if items else '（无）\n'
    ts = mat.get('todo_stats') or {}
    t = f"# {period_label}\n\n"
    t += f"## 本期总结\n{ai.get('summary', '本期暂无总结')}\n\n"
    t += f"## 关键工作内容（AI 提炼）\n{b(ai.get('key_work', []))}"
    t += f"## 对话主题分析\n{b(ai.get('chat_topics', []))}"
    t += ("## 待办处理与时效统计（工作台+钉钉整合去重，子任务完成归并）\n"
          f"- 涉及待办 {ts.get('total', 0)} 条：已完成 {ts.get('done', 0)} / 未完成 {ts.get('undone', 0)}，"
          f"完成率 {ts.get('done_rate', 0)}%，逾期未完成 {ts.get('overdue', 0)} 条\n"
          f"- 其中：工作台事项 {ts.get('work_items_total', 0)} 项（完成 {ts.get('work_items_done', 0)}），"
          f"钉钉待办去重后 {ts.get('dingtalk_total', 0)} 项（完成 {ts.get('dingtalk_done', 0)}）\n\n")
    _it = mat.get('itop_tickets') or []
    _it_done = [x for x in _it if x['status'] in ('resolved', 'closed', 'reject')]
    _it_open = [x for x in _it if x['status'] not in ('resolved', 'closed', 'reject')]
    if _it:
        t += "## ITSM 工单（iTop）\n"
        t += "- 本期涉及工单 " + str(len(_it)) + " 张：已解决/关闭 " + str(len(_it_done)) + " 张，处理中 " + str(len(_it_open)) + " 张\n"
        for x in _it[:15]:
            t += "  - [" + str(x['ticket_ref']) + "] " + str(x['title'] or '')[:60] + "（" + str(x['status']) + "）\n"
        t += "\n"
    t += f"## 主要成果\n{b(ai.get('highlights', []))}\n"
    t += f"## 待解决问题\n{b(ai.get('issues', []))}\n"
    t += f"## 下期计划\n{b(ai.get('plan', []))}\n"
    t += "## 数据统计\n"
    t += f"- 新建 {len(mat['created_items'])} / 完成 {len(mat['completed_items'])} / 截止 {len(mat['due_items'])} / 进行中 {len(mat['in_progress_items'])} / 超期 {len(mat['overdue_items'])}\n"
    t += f"- 知识库素材：待办 {mat['kb'].get('todo', 0)} · 日程 {mat['kb'].get('calendar', 0)} · 聊天 {mat['kb'].get('chat', 0)} · 听记 {mat['kb'].get('minutes', 0)}\n"
    return t


def _report_scope_denied(db, report):
    """v28.5 子管理员只能操作本团队成员的报告；主管理员放行"""
    if not session.get('is_admin'):
        return report['user_id'] != session['user_id']
    scope_team_id = get_admin_scope()
    if scope_team_id is None:
        return False
    owner = db.execute('SELECT team_id FROM users WHERE id = ?', (report['user_id'],)).fetchone()
    return (not owner) or owner['team_id'] != scope_team_id


@app.route('/api/reports')
@login_required
def list_reports():
    """查看报告列表（管理员可看所有人）"""
    db = get_db()
    user_filter = request.args.get('user_id')
    type_filter = request.args.get('type')

    query = """
        SELECT r.*, u.display_name, u.ad_username
        FROM reports r JOIN users u ON r.user_id = u.id WHERE 1=1
    """
    params = []

    if session.get('is_admin'):
        scope_team_id = get_admin_scope()
        if scope_team_id is not None:
            # v28.5 子管理员：只看本团队报告
            query += ' AND r.user_id IN (SELECT id FROM users WHERE team_id = ?)'
            params.append(scope_team_id)
            if user_filter:
                in_team = db.execute('SELECT id FROM users WHERE id = ? AND team_id = ?',
                                     (user_filter, scope_team_id)).fetchone()
                if not in_team:
                    return jsonify({'error': '无权查看该成员的报告'}), 403
                query += ' AND r.user_id = ?'
                params.append(user_filter)
        elif user_filter:
            query += ' AND r.user_id = ?'
            params.append(user_filter)
    else:
        query += ' AND r.user_id = ?'
        params.append(session['user_id'])

    if type_filter:
        query += ' AND r.type = ?'
        params.append(type_filter)

    query += ' ORDER BY r.created_at DESC LIMIT 100'
    reports = db.execute(query, params).fetchall()
    return jsonify([dict(r) for r in reports])


@app.route('/api/reports/generate', methods=['POST'])
@login_required
def generate_report():
    """AI 生成日报/周报/月报：素材 = work_items + 知识库(MCP 回传)，输出结构化 HTML + 图表"""
    data = request.get_json(silent=True) or {}
    report_type = data.get('type', 'daily')
    if report_type not in ('daily', 'weekly', 'monthly'):
        return jsonify({'error': '无效的报告类型'}), 400

    target_user_id = data.get('user_id', session['user_id'])
    if session.get('is_admin') and target_user_id:
        target_user_id = int(target_user_id)
    else:
        target_user_id = session['user_id']

    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (target_user_id,)).fetchone()
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    # v28.5 子管理员作用域：只能为本团队成员生成报告
    if session.get('is_admin'):
        scope_team_id = get_admin_scope()
        if scope_team_id is not None and user['team_id'] != scope_team_id:
            return jsonify({'error': '无权为该成员生成报告'}), 403

    today = date.today()
    if report_type == 'daily':
        start = today.isoformat()
        end = start
        period_label = f'{start} 日报'
    elif report_type == 'weekly':
        start = (today - timedelta(days=today.weekday())).isoformat()
        end = today.isoformat()
        period_label = f'{start} ~ {end} 周报'
    else:
        start = today.replace(day=1).isoformat()
        end = today.isoformat()
        period_label = f'{start[:7]} 月报'

    mat = _report_materials(db, target_user_id, start, end)
    n = len(mat['items'])

    # AI 素材
    def bullet(items, lim=15):
        return ''.join(f"- [{i['priority']}] {i['title']}（{i.get('category') or '未分类'}）\n"
                       for i in items[:lim])
    material = f"员工：{user['display_name']}（{user['section_name']}）\n"
    material += f"报告周期：{period_label}\n"
    material += (f"统计：新建 {len(mat['created_items'])} 条，完成 {len(mat['completed_items'])} 条，"
                 f"截止 {len(mat['due_items'])} 条，进行中 {len(mat['in_progress_items'])} 条，"
                 f"待处理 {len(mat['pending_items'])} 条，超期 {len(mat['overdue_items'])} 条。\n\n")
    material += "【本周期新建】\n" + (bullet(mat['created_items']) or '（无）\n')
    material += "\n【本周期完成】\n" + (bullet(mat['completed_items']) or '（无）\n')
    material += "\n【进行中】\n" + (bullet(mat['in_progress_items']) or '（无）\n')
    # v17：待办处理（工作台+钉钉整合去重，子任务完成归并）
    ts = mat['todo_stats']
    material += (f"\n【待办处理（工作台+钉钉整合，时效统计）】\n"
                 f"- 涉及待办 {ts['total']} 条：已完成 {ts['done']} 条 / 未完成 {ts['undone']} 条，"
                 f"完成率 {ts['done_rate']}%，逾期未完成 {ts['overdue']} 条\n"
                 f"- 细分：工作台事项 {ts['work_items_total']} 项（完成 {ts['work_items_done']}），"
                 f"钉钉待办去重后 {ts['dingtalk_total']} 项（完成 {ts['dingtalk_done']}）\n")
    for t in mat['todo_rows'][:20]:
        c = (t.get('content') or '').replace('\n', ' ')
        material += f"- {c[:100]}\n"
    # v27.0：iTop 工单素材
    _it = mat.get('itop_tickets') or []
    material += "\n【iTop 工单（ITSM，MCP 同步）】本期涉及 " + str(len(_it)) + " 张："
    material += ("已解决/关闭 " + str(sum(1 for x in _it if x['status'] in ('resolved', 'closed', 'reject'))) + " 张，"
                 "处理中 " + str(sum(1 for x in _it if x['status'] not in ('resolved', 'closed', 'reject'))) + " 张\n")
    for x in _it[:15]:
        material += ("- [" + str(x['ticket_ref']) + "] " + str(x['title'] or '')[:60]
                     + "（" + str(x['status']) + "，工程师 " + str(x.get('agent_name') or '未指派') + "）\n")
    if not _it:
        material += "（本期无关联工单）\n"
    # v15：周期内对话（关键工作内容来源）
    material += "\n【本周期对话记录（钉钉聊天，供提取关键工作内容）】\n"
    if mat['chat_content']:
        for c in mat['chat_content'][:30]:
            material += f"- [{c['event_time'][:16]}] {c['title'][:40]}：{c['body'][:120]}\n"
    else:
        material += '（周期内无同步聊天记录，可引导用户在知识库中手动同步）\n'
    material += "\n【知识库素材（钉钉 / MCP 回传）】\n"
    kb_l = {'todo': '待办', 'calendar': '日程', 'chat': '聊天', 'minutes': '听记'}
    for key, label in kb_l.items():
        material += f"- {label} {mat['kb'].get(key, 0)} 条："
        material += '；'.join((t['title'] or '').replace('📌', '').replace('🗓️', '').replace('💬', '').replace('🎙️', '').strip()[:30]
                              for t in mat['kb_titles'].get(key, [])[:5]) + '\n'
    if not n:
        material += '\n注：本周期工作项较少，报告以知识库动态为主。\n'

    system = (
        f'你是基础架构运维团队的报告撰写助手。根据给定的数据素材，生成一份{report_type}报告。'
        '必须严格只输出 JSON 对象（不要 markdown 代码块、不要注释），结构如下：\n'
        '{"key_work": ["从对话/待办/工作项中提炼的关键工作内容1", "关键工作内容2", "关键工作内容3"], '
        '"chat_topics": ["对话涉及主题1", "对话涉及主题2"], '
        '"summary": "本期工作总结（80~150字概括性段落，突出进展与价值）", '
        '"highlights": ["主要成果1", "主要成果2"], '
        '"issues": ["待解决问题1", "待解决问题2"], '
        '"plan": ["下期计划1", "下期计划2", "下期计划3"]}\n'
        '要求：只依据给定素材，不得虚构；key_work 3~6条、chat_topics 1~4条、highlights 3~5条、'
        'issues 0~3条、plan 2~4条，每条约15~30字。'
    )
    try:
        ai = _ai_json(system, material, max_tokens=1800, feature=f'report_{report_type}')
        for k in ('summary', 'highlights', 'issues', 'plan', 'key_work', 'chat_topics'):
            if k not in ai:
                ai[k] = [] if k != 'summary' else '本期暂无总结'
            elif not isinstance(ai[k], list):
                ai[k] = [ai[k]] if k != 'summary' else str(ai[k])
        content_html = _build_report_html(period_label, f"{user['display_name']} · {user['section_name']}", mat, ai)
        content = _report_text(period_label, user['display_name'], mat, ai)
        stats = json.dumps({
            'created': len(mat['created_items']), 'completed': len(mat['completed_items']),
            'due': len(mat['due_items']), 'in_progress': len(mat['in_progress_items']),
            'pending': len(mat['pending_items']), 'overdue': len(mat['overdue_items']),
            'kb': mat['kb'], 'cat': dict(mat['cat_sorted']), 'days': mat['day_counts'],
            'todo_stats': mat['todo_stats'],
        }, ensure_ascii=False)
        # v15：内置 AI 分析字段（关键工作内容/对话主题/待办时效），供列表与后续使用
        ai_insights = json.dumps({
            'key_work': ai.get('key_work', []),
            'chat_topics': ai.get('chat_topics', []),
            'todo_stats': mat['todo_stats'],
        }, ensure_ascii=False)
        cur = db.execute(
            'INSERT INTO reports (user_id, type, content, content_html, stats, ai_insights, period_start, period_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (target_user_id, report_type, content, content_html, stats, ai_insights, start, end)
        )
        db.commit()
        report = db.execute(
            'SELECT r.*, u.display_name, u.ad_username FROM reports r JOIN users u ON r.user_id = u.id WHERE r.id = ?',
            (cur.lastrowid,)
        ).fetchone()
        return jsonify(dict(report)), 201
    except Exception as e:
        return jsonify({'error': f'AI 报告生成失败: {e}'}), 502


@app.route('/api/reports/<int:report_id>')
@login_required
def get_report(report_id):
    db = get_db()
    report = db.execute(
        'SELECT r.*, u.display_name FROM reports r JOIN users u ON r.user_id = u.id WHERE r.id = ?',
        (report_id,)
    ).fetchone()
    if not report:
        return jsonify({'error': '报告不存在'}), 404
    if _report_scope_denied(db, report):
        return jsonify({'error': '无权查看'}), 403
    return jsonify(dict(report))


@app.route('/api/reports/<int:report_id>', methods=['DELETE'])
@login_required
def delete_report(report_id):
    db = get_db()
    report = db.execute('SELECT * FROM reports WHERE id = ?', (report_id,)).fetchone()
    if not report:
        return jsonify({'error': '报告不存在'}), 404
    if _report_scope_denied(db, report):
        return jsonify({'error': '无权删除'}), 403
    db.execute('DELETE FROM reports WHERE id = ?', (report_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/reports/<int:report_id>', methods=['PUT'])
@login_required
def update_report(report_id):
    db = get_db()
    report = db.execute('SELECT * FROM reports WHERE id = ?', (report_id,)).fetchone()
    if not report:
        return jsonify({'error': '报告不存在'}), 404
    if _report_scope_denied(db, report):
        return jsonify({'error': '无权编辑'}), 403

    data = request.get_json(silent=True) or {}
    new_content = data.get('content', '')
    if not new_content:
        return jsonify({'error': '内容不能为空'}), 400

    user = db.execute('SELECT * FROM users WHERE id = ?', (report['user_id'],)).fetchone()
    mat = _report_materials(db, report['user_id'], report['period_start'], report['period_end'])

    system = (
        f'你是基础架构运维团队的报告撰写助手。员工已手动编辑了报告内容，请根据编辑后的内容重新整理并生成结构化的 JSON。'
        '必须严格只输出 JSON 对象（不要 markdown 代码块、不要注释），结构如下：\n'
        '{"key_work": ["关键工作内容1", "关键工作内容2"], '
        '"chat_topics": ["对话主题1"], '
        '"summary": "本期工作总结（80~150字概括性段落）", '
        '"highlights": ["主要成果1", "主要成果2"], '
        '"issues": ["待解决问题1"], '
        '"plan": ["下期计划1", "下期计划2"]}\n'
        '要求：基于员工编辑的内容提炼；key_work 3~6条、chat_topics 1~4条、highlights 3~5条、issues 0~3条、plan 2~4条，每条约15~30字。'
    )
    try:
        ai = _ai_json(system, new_content, max_tokens=1800, feature=f'report_edit')
        for k in ('summary', 'highlights', 'issues', 'plan', 'key_work', 'chat_topics'):
            if k not in ai:
                ai[k] = [] if k != 'summary' else '本期暂无总结'
            elif not isinstance(ai[k], list):
                ai[k] = [ai[k]] if k != 'summary' else str(ai[k])
        period_label = f"{report['period_start']} ~ {report['period_end']} {report['type']}"
        content_html = _build_report_html(period_label, f"{user['display_name']} · {user['section_name']}", mat, ai)
        # 编辑后同步刷新内置 AI 分析字段（待办时效沿用素材重算）
        old_insights = {}
        try:
            old_insights = json.loads(report['ai_insights'] or '{}')
        except Exception:
            pass
        ai_insights = json.dumps({
            'key_work': ai.get('key_work', old_insights.get('key_work', [])),
            'chat_topics': ai.get('chat_topics', old_insights.get('chat_topics', [])),
            'todo_stats': mat.get('todo_stats') or old_insights.get('todo_stats', {}),
        }, ensure_ascii=False)
        db.execute(
            'UPDATE reports SET content = ?, content_html = ?, ai_insights = ? WHERE id = ?',
            (new_content, content_html, ai_insights, report_id)
        )
        db.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': f'AI 重新生成失败: {e}'}), 502


# ====================================================================
# 统计
# ====================================================================
@app.route('/api/stats')
@login_required
def stats():
    db = get_db()
    today = date.today().isoformat()

    if session.get('is_admin') and request.args.get('team'):
        # v28.5：子管理员只统计本团队；主管理员/全局管理员看全部
        scope_team_id = get_admin_scope()
        _wteam = ('WHERE w.user_id IN (SELECT id FROM users WHERE team_id = %d)' % scope_team_id) if scope_team_id else ''
        total = db.execute(f'SELECT COUNT(*) as c FROM work_items w {_wteam}').fetchone()['c']
        completed = db.execute(
            f"SELECT COUNT(*) as c FROM work_items w {_wteam}{' AND' if _wteam else ' WHERE'} w.status='completed'").fetchone()['c']
        pending = db.execute(
            f"SELECT COUNT(*) as c FROM work_items w {_wteam}{' AND' if _wteam else ' WHERE'} w.status='pending'").fetchone()['c']
        in_progress = db.execute(
            f"SELECT COUNT(*) as c FROM work_items w {_wteam}{' AND' if _wteam else ' WHERE'} w.status='in_progress'").fetchone()['c']
        overdue = db.execute(
            f"SELECT COUNT(*) as c FROM work_items w {_wteam}{' AND' if _wteam else ' WHERE'} w.status!='completed' AND w.due_date!='' AND w.due_date < ?",
            (today,)).fetchone()['c']

        _uteam = ('WHERE u.team_id = %d' % scope_team_id) if scope_team_id else ''
        user_stats = db.execute(f"""
            SELECT u.id, u.display_name, u.section_name, u.ad_username,
                u.employee_id, u.email, u.job_description,
                COUNT(w.id) as total,
                SUM(CASE WHEN w.status='completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN w.status='pending' THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN w.status='in_progress' THEN 1 ELSE 0 END) as in_progress,
                SUM(CASE WHEN w.status!='completed' AND w.due_date!='' AND w.due_date < ? THEN 1 ELSE 0 END) as overdue
            FROM users u
            LEFT JOIN work_items w ON w.user_id = u.id
                OR (',' || w.collaborators || ',') LIKE '%,' || u.id || ',%'
            {_uteam}
            GROUP BY u.id ORDER BY u.id
        """, (today,)).fetchall()

        # v25.9：附加用户活动统计
        week_start = (date.today() - timedelta(days=date.today().weekday())).isoformat()
        activity_stats = {}
        for u in user_stats:
            uid = u['id']
            today_count = db.execute(
                "SELECT COUNT(*) as c FROM user_activity WHERE user_id=? AND DATE(created_at)=?",
                (uid, today)
            ).fetchone()['c']
            week_count = db.execute(
                "SELECT COUNT(*) as c FROM user_activity WHERE user_id=? AND DATE(created_at)>=?",
                (uid, week_start)
            ).fetchone()['c']
            total_count = db.execute(
                "SELECT COUNT(*) as c FROM user_activity WHERE user_id=?",
                (uid,)
            ).fetchone()['c']
            last_visit = db.execute(
                "SELECT MAX(created_at) as t FROM user_activity WHERE user_id=?",
                (uid,)
            ).fetchone()['t'] or ''
            activity_stats[uid] = {
                'today_visits': today_count,
                'week_visits': week_count,
                'total_visits': total_count,
                'last_visit': last_visit
            }

        # v26.5：附加每员工 Token 消耗（今日/本月）与 MCP 接入情况
        month_start = date.today().replace(day=1).isoformat()
        token_stats = {r['user_id']: r for r in db.execute("""
            SELECT user_id,
                SUM(CASE WHEN DATE(created_at)=? THEN total_tokens ELSE 0 END) as today_tokens,
                SUM(CASE WHEN DATE(created_at)>=? THEN total_tokens ELSE 0 END) as month_tokens
            FROM ai_usage GROUP BY user_id
        """, (today, month_start)).fetchall()}
        for _uk, _tr in token_stats.items():
            _tr['today_tokens'] = int(_tr['today_tokens'] or 0)
            _tr['month_tokens'] = int(_tr['month_tokens'] or 0)
        mcp_stats = {r['user_id']: r for r in db.execute(
            "SELECT user_id, COUNT(*) as cnt, MAX(last_used_at) as last_used FROM mcp_tokens WHERE enabled=1 GROUP BY user_id"
        ).fetchall()}
        # v27.0：iTop 工单统计（按工程师）
        _itop_month = today[:7]
        itop_stats = {r['user_id']: r for r in db.execute(
            """SELECT user_id,
                SUM(CASE WHEN status NOT IN ('resolved','closed','reject') THEN 1 ELSE 0 END) as itop_active,
                SUM(CASE WHEN SUBSTRING(close_date,1,7)=? OR SUBSTRING(resolution_date,1,7)=? THEN 1 ELSE 0 END) as itop_month_closed
            FROM itop_tickets WHERE user_id IS NOT NULL GROUP BY user_id""",
            (_itop_month, _itop_month)
        ).fetchall()}

        # v25.9：附加钉钉绑定状态
        bound_rows = db.execute('SELECT user_id FROM dingtalk_bindings').fetchall()
        bound_ids = set(r['user_id'] for r in bound_rows)

        user_stats_list = []
        for u in user_stats:
            d = dict(u)
            # v26.4：SUM 经 shim 返回字符串，统一转 int（与详情接口类型一致）
            for k in ('total', 'completed', 'pending', 'in_progress', 'overdue'):
                d[k] = int(d.get(k) or 0)
            d['activity'] = activity_stats.get(u['id'], {})
            d['dingtalk_bound'] = u['id'] in bound_ids
            _ts = token_stats.get(u['id']) or {}
            d['today_tokens'] = _ts.get('today_tokens', 0)
            d['month_tokens'] = _ts.get('month_tokens', 0)
            _ms = mcp_stats.get(u['id'])
            d['mcp_count'] = int(_ms['cnt']) if _ms else 0
            d['mcp_last_used'] = (_ms.get('last_used') or '') if _ms else ''
            _is = itop_stats.get(u['id'])
            d['itop_active'] = int(_is['itop_active'] or 0) if _is else 0
            d['itop_month_closed'] = int(_is['itop_month_closed'] or 0) if _is else 0
            user_stats_list.append(d)

        return jsonify({
            'total': total, 'completed': completed, 'pending': pending,
            'in_progress': in_progress, 'overdue': overdue,
            'user_stats': user_stats_list
        })
    else:
        uid = session['user_id']
        # v25.5：个人工作台统计计入本人任务 + 作为协同者的任务；完成时双算（负责人和协同人都计数）
        collab_like = f'%,{uid},%'
        total = db.execute(
            "SELECT COUNT(*) as c FROM work_items WHERE user_id=? OR (',' || collaborators || ',') LIKE ?",
            (uid, collab_like)
        ).fetchone()['c']
        completed = db.execute(
            "SELECT COUNT(*) as c FROM work_items WHERE (user_id=? OR (',' || collaborators || ',') LIKE ?) AND status='completed'",
            (uid, collab_like)
        ).fetchone()['c']
        pending = db.execute(
            "SELECT COUNT(*) as c FROM work_items WHERE (user_id=? OR (',' || collaborators || ',') LIKE ?) AND status='pending'",
            (uid, collab_like)
        ).fetchone()['c']
        in_progress = db.execute(
            "SELECT COUNT(*) as c FROM work_items WHERE (user_id=? OR (',' || collaborators || ',') LIKE ?) AND status='in_progress'",
            (uid, collab_like)
        ).fetchone()['c']
        overdue = db.execute(
            "SELECT COUNT(*) as c FROM work_items WHERE (user_id=? OR (',' || collaborators || ',') LIKE ?) AND status!='completed' AND due_date!='' AND due_date < ?",
            (uid, collab_like, today)
        ).fetchone()['c']
        # 时效统计：平均耗时只统计本人任务（避免协同任务耗时被重复计入多人）
        avg_duration = db.execute(
            "SELECT AVG(actual_duration_minutes) as avg FROM work_items WHERE user_id=? AND status='completed' AND actual_duration_minutes > 0",
            (uid,)
        ).fetchone()['avg'] or 0
        # v27.0：iTop 工单统计（本人名下）
        _itop_month_p = today[:7]
        itop_active = db.execute(
            "SELECT COUNT(*) as c FROM itop_tickets WHERE user_id=? AND status NOT IN ('resolved','closed','reject')",
            (uid,)
        ).fetchone()['c']
        itop_today_closed = db.execute(
            "SELECT COUNT(*) as c FROM itop_tickets WHERE user_id=? AND (SUBSTRING(close_date,1,10)=? OR SUBSTRING(resolution_date,1,10)=?)",
            (uid, today, today)
        ).fetchone()['c']
        itop_month_closed = db.execute(
            "SELECT COUNT(*) as c FROM itop_tickets WHERE user_id=? AND (SUBSTRING(close_date,1,7)=? OR SUBSTRING(resolution_date,1,7)=?)",
            (uid, _itop_month_p, _itop_month_p)
        ).fetchone()['c']
        overdue_rate = round((overdue / max(pending + in_progress + overdue, 1)) * 100, 1)
        today_completed = db.execute(
            "SELECT COUNT(*) as c FROM work_items WHERE (user_id=? OR (',' || collaborators || ',') LIKE ?) AND status='completed' AND date(completed_at) = ?",
            (uid, collab_like, today)
        ).fetchone()['c']
        return jsonify({
            'total': total, 'completed': completed, 'pending': pending,
            'in_progress': in_progress, 'overdue': overdue,
            'avg_duration_minutes': round(avg_duration, 1),
            'overdue_rate': overdue_rate,
            'today_completed': today_completed, 'itop_active': itop_active, 'itop_today_closed': itop_today_closed, 'itop_month_closed': itop_month_closed
        })


@app.route('/api/today-overview')
@login_required
def today_overview():
    """返回今日待办数量和日程数量（用于左侧边栏日历角标）"""
    db = get_db()
    uid = session['user_id']
    today = date.today().isoformat()
    todo_count = db.execute(
        "SELECT COUNT(*) as c FROM user_knowledge WHERE user_id = ? AND source = 'dingtalk_todo' AND occur_date = ?",
        (uid, today)
    ).fetchone()['c']
    calendar_count = db.execute(
        "SELECT COUNT(*) as c FROM user_knowledge WHERE user_id = ? AND source = 'dingtalk_calendar' AND occur_date = ?",
        (uid, today)
    ).fetchone()['c']
    # 同时返回今日 work_items 数量（v25.5：包含协同任务）
    work_todo = db.execute(
        "SELECT COUNT(*) as c FROM work_items WHERE (user_id = ? OR (',' || collaborators || ',') LIKE ?) AND status != 'completed' AND due_date = ?",
        (uid, f'%,{uid},%', today)
    ).fetchone()['c']
    return jsonify({
        'todo_count': todo_count,
        'calendar_count': calendar_count,
        'work_todo': work_todo,
        'today': today
    })


# ====================================================================
# 工作日志
# ====================================================================
@app.route('/api/work-logs')
@login_required
def work_logs():
    db = get_db()
    user_filter = request.args.get('user_id')
    query = """
        SELECT l.*, u.display_name FROM work_logs l
        LEFT JOIN users u ON l.user_id = u.id
    """
    params = []
    if not session.get('is_admin'):
        query += ' WHERE l.user_id = ?'
        params.append(session['user_id'])
    elif user_filter:
        # v29.4：子管理员须确认目标用户在自己团队作用域内，防止跨团队横向越权
        target = db.execute('SELECT id, team_id FROM users WHERE id = ?', (user_filter,)).fetchone()
        if target is None or not _can_view_user(dict(target)):
            return jsonify({'error': '无权查看该用户的工作日志'}), 403
        query += ' WHERE l.user_id = ?'
        params.append(user_filter)
    else:
        # v29.4：子管理员不带过滤条件时只返回本团队日志，防止全量浏览越权
        scope = get_admin_scope()
        if scope is not None:
            query += ' WHERE u.team_id = ?'
            params.append(scope)
    query += ' ORDER BY l.created_at DESC LIMIT 200'
    logs = db.execute(query, params).fetchall()
    return jsonify([dict(l) for l in logs])


# ====================================================================
# 外部同步 API（API Key 认证）—— WorkBuddy / Qoder / MCP 接入
# ====================================================================
@app.route('/ext/api/work-items', methods=['GET'])
@api_key_required
def ext_list_work_items():
    """外部 API：获取当前 API Key 对应用户的工作项"""
    db = get_db()
    uid = g.api_user_id
    items = db.execute("""
        SELECT w.*, u.display_name FROM work_items w
        JOIN users u ON w.user_id = u.id WHERE w.user_id = ?
        ORDER BY w.created_at DESC
    """, (uid,)).fetchall()
    return jsonify([dict(i) for i in items])


@app.route('/ext/api/work-items', methods=['POST'])
@api_key_required
def ext_add_work_item():
    """外部 API：同步工作事项（WorkBuddy/Qoder 对话内容自动同步到工作台）"""
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': '缺少工作标题'}), 400
    uid = g.api_user_id
    source = data.get('source', 'external')

    db = sqlite3.connect()
    recurring = data.get('recurring', '')
    if recurring not in RECURRING_TYPES:
        recurring = ''
    due_date = (data.get('due_date') or '').strip()
    next_run = _calc_next_run(recurring, due_date) if recurring else ''
    cur = db.execute("""
        INSERT INTO work_items (user_id, title, description, category, priority, status, due_date, created_by, recurring, next_run_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        uid, title,
        (data.get('description') or '').strip(),
        data.get('category', '外部同步'),
        data.get('priority', 'P2'),
        data.get('status', 'pending'),
        due_date,
        source,
        recurring,
        next_run
    ))
    db.execute(
        'INSERT INTO external_sync_log (user_id, source, action, item_id, detail) VALUES (?, ?, ?, ?, ?)',
        (uid, source, 'created', cur.lastrowid, title)
    )
    db.execute(
        'INSERT INTO work_logs (user_id, action, item_id, detail) VALUES (?, ?, ?, ?)',
        (uid, 'ext_created', cur.lastrowid, f'外部同步({source}): {title}')
    )
    db.commit()
    item = db.execute(
        'SELECT w.*, u.display_name FROM work_items w JOIN users u ON w.user_id = u.id WHERE w.id = ?',
        (cur.lastrowid,)
    ).fetchone()
    db.close()
    return jsonify(dict(item)), 201


@app.route('/ext/api/work-items/<int:item_id>', methods=['PUT'])
@api_key_required
def ext_update_work_item(item_id):
    """外部 API：更新工作项状态"""
    data = request.get_json(silent=True) or {}
    uid = g.api_user_id
    db = sqlite3.connect()
    item = db.execute('SELECT * FROM work_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        db.close()
        return jsonify({'error': '工作项不存在'}), 404
    if item['user_id'] != uid:
        db.close()
        return jsonify({'error': '无权操作'}), 403

    now = _now_str()
    updates, params = [], []
    for field in ['title', 'description', 'category', 'priority', 'due_date', 'status']:
        if field in data:
            updates.append(f'{field} = ?')
            params.append(data[field])
    if 'status' in data and data['status'] == 'completed':
        updates.append('completed_at = ?')
        params.append(now)
    if updates:
        updates.append('updated_at = ?')
        params.append(now)
        params.append(item_id)
        db.execute(f'UPDATE work_items SET {", ".join(updates)} WHERE id = ?', params)
        db.execute(
            'INSERT INTO external_sync_log (user_id, source, action, item_id, detail) VALUES (?, ?, ?, ?, ?)',
            (uid, data.get('source', 'external'), 'updated', item_id, json.dumps(data, ensure_ascii=False))
        )
        db.commit()
    item = db.execute(
        'SELECT w.*, u.display_name FROM work_items w JOIN users u ON w.user_id = u.id WHERE w.id = ?',
        (item_id,)
    ).fetchone()
    db.close()
    return jsonify(dict(item))


@app.route('/ext/api/stats')
@api_key_required
def ext_stats():
    """外部 API：获取统计"""
    uid = g.api_user_id
    db = sqlite3.connect()
    today = date.today().isoformat()
    total = db.execute('SELECT COUNT(*) as c FROM work_items WHERE user_id=?', (uid,)).fetchone()['c']
    completed = db.execute("SELECT COUNT(*) as c FROM work_items WHERE user_id=? AND status='completed'", (uid,)).fetchone()['c']
    pending = db.execute("SELECT COUNT(*) as c FROM work_items WHERE user_id=? AND status!='completed'", (uid,)).fetchone()['c']
    overdue = db.execute(
        "SELECT COUNT(*) as c FROM work_items WHERE user_id=? AND status!='completed' AND due_date!='' AND due_date < ?",
        (uid, today)
    ).fetchone()['c']
    db.close()
    return jsonify({'total': total, 'completed': completed, 'pending': pending, 'overdue': overdue})


@app.route('/ext/api/whoami')
@api_key_required
def ext_whoami():
    """外部 API：验证 API Key 并返回用户信息"""
    db = sqlite3.connect()
    user = db.execute('SELECT id, ad_username, display_name, section_name FROM users WHERE id = ?', (g.api_user_id,)).fetchone()
    db.close()
    return jsonify(dict(user) if user else {'error': 'not found'})


# ====================================================================
# MCP 服务端（Model Context Protocol）
# 每用户独立鉴权，为 WorkBuddy / Cline / Claude Desktop 等接入做准备
# ====================================================================

MCP_SERVER_NAME = os.environ.get('MCP_SERVER_NAME', 'infra-workbench-mcp')
MCP_SERVER_VERSION = os.environ.get('MCP_SERVER_VERSION', '26.3.0')


def _mcp_auth_token():
    """从请求头提取 MCP Bearer Token"""
    auth = request.headers.get('Authorization', '')
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    return request.args.get('token', '')


def _mcp_verify_token(token_hash):
    """验证 MCP token，返回对应 user_id 或 None

    存库时为 sha256(token) 的十六进制摘要，因此这里先哈希再比对。
    （v21 修复：此前直接用原始 token 比对哈希列，导致永远 401。）
    v25.9：支持多 Token——优先查 mcp_tokens（每工具一个 token），
    命中后记录 last_used_at；兼容旧表 mcp_configs。
    """
    if not token_hash:
        return None
    db = sqlite3.connect()
    digest = hashlib.sha256(token_hash.encode()).hexdigest()
    row = db.execute(
        'SELECT id, user_id FROM mcp_tokens WHERE auth_token_hash = ? AND enabled = 1', (digest,)
    ).fetchone()
    if row:
        try:
            db.execute('UPDATE mcp_tokens SET last_used_at = ? WHERE id = ?', (_now_str(), row['id']))
            db.commit()
        except Exception:
            pass
        db.close()
        return row['user_id']
    # 兼容旧库：迁移前的单 token 仍可通过旧表验证
    row = db.execute('SELECT user_id FROM mcp_configs WHERE auth_token_hash = ? AND enabled = 1', (digest,)).fetchone()
    db.close()
    return row['user_id'] if row else None


def mcp_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = _mcp_auth_token()
        uid = _mcp_verify_token(token)
        if not uid:
            return jsonify({'error': 'MCP 鉴权失败：token 无效或已禁用'}), 401
        g.mcp_user_id = uid
        return f(*args, **kwargs)
    return decorated


@app.route('/api/mcp/config', methods=['GET', 'POST'])
@login_required
def mcp_config():
    """管理当前用户的 MCP 接入配置（v25.9：多 Token，每个 AI 工具一个）"""
    db = get_db()
    uid = session['user_id']
    if request.method == 'GET':
        # v25.9：返回全部 token 列表（脱敏）+ 已接入工具
        token_rows = db.execute(
            'SELECT * FROM mcp_tokens WHERE user_id = ? AND enabled = 1 ORDER BY created_at', (uid,)
        ).fetchall()
        tokens = []
        for t in token_rows:
            prefix = t['auth_token_prefix'] or ''
            tokens.append({
                'id': t['id'],
                'label': t['label'] or '默认',
                'token_prefix': prefix,
                'token_masked': f"{prefix}****" if prefix else '',
                'created_at': t['created_at'],
                'last_used_at': t['last_used_at'] or ''
            })
        # 从 work_items 查询通过 MCP 同步的工具标签（去重）
        tool_rows = db.execute(
            "SELECT DISTINCT tool_label FROM work_items WHERE user_id=? AND source='ai' AND tool_label != '' ORDER BY tool_label",
            (uid,)
        ).fetchall()
        tools = [r['tool_label'] for r in tool_rows]
        enabled = len(tokens) > 0
        return jsonify({
            'enabled': enabled,
            'tokens': tokens,
            # 兼容旧前端字段：取第一个 token
            'token_prefix': tokens[0]['token_prefix'] if tokens else '',
            'token_masked': tokens[0]['token_masked'] if tokens else '',
            'allowed_tools': '*',
            'endpoint': f"{request.host_url.rstrip('/')}/mcp/sse",
            'created_at': tokens[0]['created_at'] if tokens else '',
            'tools': tools
        })
    # POST: 生成新 token（v25.9：多 Token，新增不覆盖旧 token；label 标记所属工具）
    data = request.get_json(silent=True) or {}
    label = (data.get('label') or data.get('tool') or '').strip()[:32] or '默认'
    new_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(new_token.encode()).hexdigest()
    prefix = new_token[:8]
    cur = db.execute(
        'INSERT INTO mcp_tokens (user_id, label, auth_token_hash, auth_token_prefix) VALUES (?,?,?,?)',
        (uid, label, token_hash, prefix))
    # 同步维护旧表（兼容仍依赖 mcp_configs 的逻辑，如部署文档生成）
    existing = db.execute('SELECT id FROM mcp_configs WHERE user_id = ?', (uid,)).fetchone()
    if existing:
        db.execute(
            'UPDATE mcp_configs SET auth_token_hash=?, auth_token_prefix=?, updated_at=? WHERE user_id=?',
            (token_hash, prefix, _now_str(), uid))
    else:
        db.execute(
            'INSERT INTO mcp_configs (user_id, auth_token_hash, auth_token_prefix, allowed_tools, updated_at) VALUES (?,?,?,?,?)',
            (uid, token_hash, prefix, '*', _now_str()))
    db.commit()
    return jsonify({
        'id': cur.lastrowid,
        'label': label,
        'token': new_token,
        'token_prefix': prefix,
        'endpoint': f"{request.host_url.rstrip('/')}/mcp/sse",
        'hint': '请妥善保存此 Token，系统只显示一次。每个 AI 工具（WorkBuddy / Qoder 等）建议使用独立 Token，互不影响。'
    })


@app.route('/api/mcp/config/<int:token_id>', methods=['DELETE'])
@login_required
def mcp_token_delete(token_id):
    """吊销单个 MCP Token（v25.9 多 Token）"""
    db = get_db()
    uid = session['user_id']
    row = db.execute('SELECT * FROM mcp_tokens WHERE id = ? AND user_id = ?', (token_id, uid)).fetchone()
    if not row:
        return jsonify({'error': 'Token 不存在'}), 404
    db.execute('DELETE FROM mcp_tokens WHERE id = ?', (token_id,))
    # 若删的是旧表里同步的那条，清理旧表
    db.execute('DELETE FROM mcp_configs WHERE user_id = ? AND auth_token_hash = ?', (uid, row['auth_token_hash']))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/mcp/config', methods=['DELETE'])
@login_required
def mcp_config_delete():
    """禁用当前用户的全部 MCP 接入（吊销所有 Token）"""
    db = get_db()
    db.execute('DELETE FROM mcp_tokens WHERE user_id = ?', (session['user_id'],))
    db.execute('DELETE FROM mcp_configs WHERE user_id = ?', (session['user_id'],))
    db.commit()
    return jsonify({'ok': True})


# ====================================================================
# v25.9：用户活动统计（登录/页面打开/使用时长）
# ====================================================================
@app.route('/api/activity/log', methods=['POST'])
@login_required
def activity_log():
    """记录用户活动（登录/页面打开/心跳）"""
    db = get_db()
    uid = session['user_id']
    data = request.get_json(silent=True) or {}
    action = data.get('action', 'page_view')
    page = data.get('page', '')
    db.execute('INSERT INTO user_activity (user_id, action, page) VALUES (?, ?, ?)', (uid, action, page))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/activity/stats', methods=['GET'])
@login_required
def activity_stats():
    """获取用户活动统计（今日/本周/总计）"""
    db = get_db()
    uid = session.get('user_id')
    target_uid = request.args.get('user_id', type=int)
    if target_uid and target_uid != uid:
        # v29.4：查看他人活动统计须通过作用域校验（本人/全局管理员/本团队子管理员）
        target = db.execute('SELECT id, team_id FROM users WHERE id = ?', (target_uid,)).fetchone()
        if target is None or not _can_view_user(dict(target)):
            return jsonify({'error': '无权查看他人活动记录'}), 403
        uid = target_uid

    today = date.today().isoformat()
    week_start = (date.today() - timedelta(days=date.today().weekday())).isoformat()

    # 今日访问次数
    today_count = db.execute(
        "SELECT COUNT(*) as c FROM user_activity WHERE user_id=? AND DATE(created_at)=?",
        (uid, today)
    ).fetchone()['c']

    # 本周访问次数
    week_count = db.execute(
        "SELECT COUNT(*) as c FROM user_activity WHERE user_id=? AND DATE(created_at)>=?",
        (uid, week_start)
    ).fetchone()['c']

    # 总访问次数
    total_count = db.execute(
        "SELECT COUNT(*) as c FROM user_activity WHERE user_id=?",
        (uid,)
    ).fetchone()['c']

    # 首次访问时间
    first_visit = db.execute(
        "SELECT MIN(created_at) as t FROM user_activity WHERE user_id=?",
        (uid,)
    ).fetchone()['t'] or ''

    # 最近访问时间
    last_visit = db.execute(
        "SELECT MAX(created_at) as t FROM user_activity WHERE user_id=?",
        (uid,)
    ).fetchone()['t'] or ''

    # 按天统计（近7天）
    daily = db.execute(
        "SELECT DATE(created_at) as d, COUNT(*) as c FROM user_activity WHERE user_id=? AND DATE(created_at)>=? GROUP BY DATE(created_at) ORDER BY d",
        (uid, (date.today() - timedelta(days=6)).isoformat())
    ).fetchall()

    return jsonify({
        'today_count': today_count,
        'week_count': week_count,
        'total_count': total_count,
        'first_visit': first_visit,
        'last_visit': last_visit,
        'daily': [{'date': r['d'], 'count': r['c']} for r in daily]
    })


# ====================================================================
# v29.6：Zabbix 告警磁贴（后端走 Zabbix JSON-RPC API，绕开 X-Frame-Options 禁止嵌入的限制）
# 需环境变量：ZABBIX_API_URL（…/api_jsonrpc.php）/ ZABBIX_API_USER / ZABBIX_API_PASS
# ====================================================================
ZABBIX_API_URL = os.environ.get('ZABBIX_API_URL', '')
ZABBIX_API_USER = os.environ.get('ZABBIX_API_USER', '')
ZABBIX_API_PASS = os.environ.get('ZABBIX_API_PASS', '')
# v29.6：也支持直接配 API Token（Zabbix 5.4+ 管理界面生成），无需用户名密码
ZABBIX_API_TOKEN = os.environ.get('ZABBIX_API_TOKEN', '')
ZABBIX_WEB_URL = os.environ.get('ZABBIX_WEB_URL', 'https://zabbix.risen.com')
ZBX_SEVERITY = {0: '未分类', 1: '信息', 2: '警告', 3: '一般严重', 4: '严重', 5: '灾难'}
_ZBX = {'auth': None, 'until': 0.0, 'ver': None}  # 登录态缓存（进程内，每 gunicorn worker 独立）
_ZBX_LOCK = threading.Lock()


def _zbx_call(method, params, auth=None, timeout=10):
    payload = {'jsonrpc': '2.0', 'method': method, 'params': params, 'id': 1}
    if auth:
        payload['auth'] = auth
    r = requests.post(ZABBIX_API_URL, json=payload, timeout=timeout, verify=False)
    r.raise_for_status()
    return r.json()


def _zbx_login():
    """登录取 token；Zabbix 6.0+ 参数名 username，旧版 user，两种都试"""
    last_err = '登录失败'
    for key in (('username', 'user') if _ZBX['ver'] in (None, 'new') else ('user', 'username')):
        try:
            res = _zbx_call('user.login', {key: ZABBIX_API_USER, 'password': ZABBIX_API_PASS})
            if res.get('result'):
                _ZBX['ver'] = 'new' if key == 'username' else 'old'
                return res['result']
            last_err = (res.get('error') or {}).get('data') or (res.get('error') or {}).get('message') or '登录失败'
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(last_err)


def _zbx_rpc(method, params, timeout=10):
    """带登录态缓存与自动重登的 RPC；未配置凭据时抛异常由路由转 503"""
    # Token 模式：直接用，无需登录（token 失效时报错提示重新生成）
    if ZABBIX_API_TOKEN:
        if not ZABBIX_API_URL:
            raise RuntimeError('未配置 ZABBIX_API_URL')
        res = _zbx_call(method, params, auth=ZABBIX_API_TOKEN, timeout=timeout)
        if res.get('error'):
            err = res['error']
            raise RuntimeError(f"Zabbix API Token 调用失败（可能已失效/权限不足）：{(err.get('message') or '')} {(err.get('data') or '')}".strip())
        return res.get('result')
    if not (ZABBIX_API_URL and ZABBIX_API_USER and ZABBIX_API_PASS):
        raise RuntimeError('未配置 Zabbix API（ZABBIX_API_URL + ZABBIX_API_TOKEN，或 ZABBIX_API_USER/PASS）')
    with _ZBX_LOCK:
        if not _ZBX['auth'] or time.time() >= _ZBX['until']:
            _ZBX['auth'] = _zbx_login()
            _ZBX['until'] = time.time() + 1500  # 25 分钟，早于 Zabbix 会话过期重登
        auth = _ZBX['auth']
    res = _zbx_call(method, params, auth=auth, timeout=timeout)
    err = res.get('error')
    if err:
        msg = f"{(err.get('message') or '')} {(err.get('data') or '')}".strip()
        # 会话失效类错误：清缓存重登重试一次
        if any(k in msg for k in ('authoris', 'authoriz', 'Session terminated', 're-login', '登录')):
            with _ZBX_LOCK:
                _ZBX['auth'], _ZBX['until'] = None, 0.0
                auth = _zbx_login()
                _ZBX['auth'], _ZBX['until'] = auth, time.time() + 1500
            res = _zbx_call(method, params, auth=auth, timeout=timeout)
            err = res.get('error')
        if err:
            raise RuntimeError(f"{(err.get('message') or '')} {(err.get('data') or '')}".strip())
    return res.get('result')


def _zbx_problems(limit=20):
    """拉取当前未恢复的告警（按时间倒序），附带主机名。
    6.0 的 problem 无 hostid 字段，需经 objectid(triggerid) → trigger.get → hostid → host.get 链式解析"""
    problems = _zbx_rpc('problem.get', {
        'output': 'extend',
        'recent': False,          # 仅未恢复的活跃告警
        'sortfield': ['eventid'], 'sortorder': 'DESC', 'limit': limit,  # 6.0 仅允许按 eventid 排序（递增即时间递增）
    }) or []
    # triggerid → hosts（6.0 的 trigger 无 hostid 字段，须用 selectHosts 带出所属主机）
    triggerids = list({p['objectid'] for p in problems if p.get('object') == '0' and p.get('objectid')})
    trig_host = {}
    if triggerids:
        for t in (_zbx_rpc('trigger.get', {'output': ['triggerid'], 'triggerids': triggerids,
                                            'selectHosts': ['hostid', 'name']}) or []):
            hs = t.get('hosts') or []
            if hs:
                trig_host[t['triggerid']] = hs[0].get('name') or hs[0].get('host') or ''
    return [{
        'eventid': p.get('eventid'),
        'name': p.get('name'),
        'severity': int(p.get('severity') or 0),
        'severity_name': ZBX_SEVERITY.get(int(p.get('severity') or 0), '未知'),
        'clock': int(p.get('clock') or 0),
        'host': trig_host.get(p.get('objectid'), '')
    } for p in problems]


@app.route('/api/zabbix/problems')
@login_required
def zabbix_problems():
    """Zabbix 当前告警列表（供告警磁贴展示）"""
    try:
        items = _zbx_problems(limit=30)
    except RuntimeError as e:
        msg = str(e)
        if msg.startswith('未配置'):
            return jsonify({'error': msg}), 503
        return jsonify({'error': f'Zabbix API 调用失败：{msg}'}), 502
    except Exception as e:
        return jsonify({'error': f'Zabbix API 调用失败：{e}'}), 502
    return jsonify({'problems': items, 'web_url': ZABBIX_WEB_URL})


# ====================================================================
# v25：MCP 部署文档（M 文件）AI 生成
# 预制框架 + AI 填充，输出给 WorkBuddy / Qoder 等 AI 工具作为系统提示词，
# 实现：企业工作内容自动导入工作台 + 排除个人使用 AI 工具的数据
# v25 新增：「周期性自动触发机制」章节（会话开始/工作单元完成/每日定时/周度复盘 四级触发 + 手动兜底）
# ====================================================================

MCP_DEPLOY_DOC_FRAMEWORK = """\
# 基础架构工作台 · AI 工具接入部署文档（{tool}）

> 生成时间：{time} ｜ 用户：{user} ｜ MCP Token：{prefix}****（每用户唯一）
> 使用方式：将本文件内容粘贴为 {tool} 的【自定义指令 / 系统提示词】，或在每次会话开始时发送"请按 MCP-部署文档工作"

## 一、角色设定

你是企业基础架构团队的 AI 工作助手（绑定用户：{user}），通过「基础架构工作台 MCP」与团队工作台保持同步：
把与企业工作相关的活动、任务、资料自动沉淀到工作台，让团队与领导实时看到你的工作产出。
**最高硬性要求：严格区分「企业工作内容」与「个人使用 AI 工具的数据」，个人数据一律不得写入工作台。**

## 二、MCP 连接配置

| 项目 | 值 |
|------|-----|
| SSE 端点 | {sse_url} |
| 鉴权方式 | Authorization: Bearer + 个人 Token（见工作台「API 接入」页，每用户独立） |
| 协议 | MCP SSE transport（JSON-RPC 2.0） |

可用工具（9 个）：
- `list_work_items` / `get_work_item`：查询工作台任务
- `create_work_item`：创建任务（source=ai，tool={tool}）
- `update_work_item_status`：回写任务状态（AI 完成阶段后调用，实现 AI 工具 ↔ 工作台状态同步）
- `log_ai_activity`：把 AI 会话/活动归档为任务（最常用）
- `sync_work_items`：增量同步工作台任务（会话开始时调用）
- `save_knowledge`：把整理的资料/问答/方案沉淀到个人知识库
- `get_stats` / `get_user_knowledge`：统计与知识库查询

## 三、内容判定规则（优先级最高，先判定再动作）

### ✅ 属于「企业工作内容」→ 必须归档
1. 公司项目/平台开发（如工作台、内部系统功能开发与改造）
2. 基础设施运维：服务器、网络、安全、存储、监控、容灾
3. IT 技术问题排查、故障处理、工单响应
4. 项目资料整理、技术方案设计、文档编写、代码评审
5. 与同事/团队的协作事项、会议跟进、审批流转
6. 使用公司系统（钉钉、运维平台、监控、CMDB、AD 等）开展的工作

### ❌ 属于「个人使用」→ 禁止归档
1. 与工作无关的闲聊、娱乐、八卦
2. 个人生活事务、家庭、健康、隐私内容
3. 与公司业务无关的个人学习、副业、投资理财
4. 用户明确表示"不要记录 / 私聊"的内容

**判定口诀：内容是否与公司业务 / 团队 / 系统 / 资产相关？是 → 归档；否 → 跳过。**

## 四、工作流约定（每次会话执行）

1. **会话开始**：调用 `sync_work_items(since=上次返回的 server_time)` 了解工作台任务现状；无上次游标则 since 留空。
2. **工作过程**：每完成一个功能 / 阶段 / 咨询：
   - 简单活动 → `log_ai_activity(title=会话主题, description=做了什么, category="AI 协作", tool="{tool}")`
   - 需要长期跟踪 / 有截止时间 → `create_work_item(title, description, category, priority, due_date, source="ai", tool="{tool}")`
3. **状态变化**：任务完成 → `update_work_item_status(item_id, "completed")`；开始处理 → 回写 "in_progress"。
4. **资料沉淀**：整理出可复用的方案/结论/问答 → `save_knowledge(title, content)`。
5. **会话结束前**：自查是否遗漏未归档的企业工作内容；个人内容一律不处理、不归档。

## 五、周期性自动触发机制（接入不是一次性的，★核心）

本机制保证：即使某次会话忘记归档、或工作跨越多个会话，企业工作内容也会**自动**沉淀到工作台。
共四级触发 + 手动兜底，任何一级失败都会由下一级补齐：

### 触发级别 1：会话开始（每次必做）
- 调用 `sync_work_items(since=上次返回的 server_time)` 增量同步工作台任务（无上次游标则 since 留空）。
- 目的：了解自己名下的任务现状（含领导分配、转办进来的任务），避免重复创建、掌握最新状态。

### 触发级别 2：工作单元完成（实时）
- 每完成一个功能 / 阶段 / 咨询，立即 `log_ai_activity` 或 `create_work_item` 归档；状态变化立即 `update_work_item_status` 回写。
- 目的：实时反映工作产出，领导随时看到最新进展。

### 触发级别 3：每日定时归档（推荐 17:30 下班前 或 21:00 晚间）
- 在 AI 工具中配置「每日定时任务」（WorkBuddy 自动化 / Qoder 定时任务均可），固定执行：
  1. `sync_work_items(since=最近一次归档的 server_time)` 拉取今日增量；
  2. 按「内容判定规则」逐个检查：属于企业内容且未归档的 → `log_ai_activity` / `create_work_item` 补齐归档；
  3. 检查工作台任务中「今日应完成但状态仍为 pending/in_progress」的 → 已完成则 `update_work_item_status(item_id, "completed")` 回写；
  4. 输出归档小结（归档 N 条 / 回写 M 条 / 跳过个人内容 X 条），发给自己或团队群。
- **可直接复制为定时任务指令**：
  「执行每日工作归档：调用 sync_work_items 增量同步，把今日企业工作内容按部署文档规则归档到工作台（log_ai_activity / create_work_item），并将已完成任务状态回写为 completed，最后输出归档小结。」

### 触发级别 4：周度复盘（每周五 17:30）
- 聚合本周工作：`list_work_items(status="")` + `get_stats` + `get_user_knowledge`，生成周报式总结（本周归档 N 条 / 完成 M 条 / 进行中 K 条 / 知识库沉淀 X 条）。
- 检查跨周遗漏的企业工作内容并补录；可复用方案/结论 → `save_knowledge` 沉淀。

### 兜底：手动触发
- 任何时候（定时任务未运行 / 工具未开启）说「把今天的工作归档到工作台」→ 立即执行级别 3 完整流程。
- 定时任务失败不丢数据：下次会话开始（级别 1）的增量同步会自动补上。

## 六、字段规范

| 字段 | 说明 |
|------|------|
| title | 简明标题，如「开发 MCP 双协议兼容修复」 |
| description | 做了什么、产出什么（供领导与同事查看） |
| category | 默认「AI 协作」，可按实际调整（如「运维」「开发」「文档」） |
| priority | P0 紧急 / P1 高 / P2 正常 / P3 低 |
| status | pending / in_progress / completed |
| tool | {tool}（工作台显示「{tool} MCP 同步」来源徽标） |

## 七、使用示例

- 会话主题「排查 177 服务器磁盘告警」→ `log_ai_activity(title="排查 177 服务器磁盘告警", description="定位 / 分区占用 95%，清理日志并扩容，已恢复", category="运维", status="completed", tool="{tool}")`
- 会话主题「开发工作台 v25 周期自动触发机制」→ `create_work_item(title="工作台 v25 周期自动触发机制", description="M 文件新增每日定时归档/周度复盘四级触发", priority="P1", due_date="2026-08-20", source="ai", tool="{tool}")`
- 整理了「MCP 接入规范」→ `save_knowledge(title="MCP 接入规范（SSE + Bearer）", content="端点 /mcp/sse，Bearer Token 鉴权，JSON-RPC 2.0……")`
- 每日定时任务 → 执行级别 3 指令（见「五、周期性自动触发机制」）自动归档今日工作并回写状态。

## 八、注意事项

- Token 是个人唯一凭证，绝不泄露、不出现在任何归档内容中。
- 归档粒度：一个完整工作单元一条任务，避免碎片化。
- 拿不准时：企业内容倾向记录（漏记损失更大），明显个人内容绝不记录。
- 本文件由工作台 AI 生成，仅用于辅助接入；实际行为以工作台服务端鉴权为准。
"""


def _mcp_deploy_doc_fallback(tool, user_name, sse_url, prefix):
    """AI 不可用时的降级模板（框架本身即完整可用）"""
    return MCP_DEPLOY_DOC_FRAMEWORK.format(
        tool=tool, time=_now_str(), user=user_name, prefix=prefix, sse_url=sse_url)


@app.route('/api/mcp/deploy-doc', methods=['POST'])
@login_required
def mcp_deploy_doc():
    """AI 生成 MCP 部署文档（M 文件）

    输入给 WorkBuddy / Qoder 等 AI 工具作为系统提示词：
    - 企业工作内容自动导入工作台（任务 + 知识库）
    - 排除个人使用 AI 工具的数据
    返回 markdown 文本；AI 不可用时降级返回预制框架。
    """
    db = get_db()
    uid = session['user_id']
    user = db.execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()
    mcp = db.execute('SELECT * FROM mcp_configs WHERE user_id = ?', (uid,)).fetchone()
    data = request.get_json(silent=True) or {}
    tool = (data.get('tool') or '').strip() or 'WorkBuddy'
    tool = tool[:32]
    extra = (data.get('extra') or '').strip()
    user_name = user['display_name'] if user else f'用户{uid}'
    prefix = (mcp['auth_token_prefix'] if mcp else '') or ''
    sse_url = f"{request.host_url.rstrip('/')}/mcp/sse"

    system = (
        '你是企业数字化工作台的部署文档生成专家。你负责基于给定的【文档框架】生成一份完整的、可直接使用的'
        'AI 工具接入部署文档（Markdown 格式，中文）。要求：\n'
        '1. 严格遵守框架的章节结构与核心内容，不得删除或弱化「内容判定规则」「工作流约定」「周期性自动触发机制」章节；\n'
        '2. 将框架中的占位符（{tool}、{user}、{sse_url}、{prefix}）替换为真实值；\n'
        '3. 可以补充 2-3 个贴合该用户岗位的「使用示例」，示例要具体、真实感强；\n'
        '4. 语言精炼专业，直接输出 Markdown 全文，不要输出多余解释。'
    )
    user_msg = (
        f'请生成部署文档。\n'
        f'- 目标 AI 工具：{tool}\n'
        f'- 绑定用户：{user_name}\n'
        f'- SSE 端点：{sse_url}\n'
        f'- MCP Token 前缀：{prefix}****\n'
        f'- 用户补充要求：{extra if extra else "无"}'
        f'\n\n以下是文档框架（请按此框架生成完整文档）：\n\n'
        f'{MCP_DEPLOY_DOC_FRAMEWORK}'
    )

    fallback = False
    try:
        doc = ai_chat(system, user_msg, max_tokens=4000, feature='mcp_doc')
        if not doc or len(doc.strip()) < 200:
            raise ValueError('AI 输出过短')
        # v25：AI 不知道当前时间，头部的生成时间强制回填真实值
        import re as _re
        doc = _re.sub(r'^> 生成时间：[^\n]*', f'> 生成时间：{_now_str()}', doc, count=1, flags=_re.M)
    except Exception as e:
        doc = _mcp_deploy_doc_fallback(tool, user_name, sse_url, prefix)
        fallback = True

    return jsonify({'ok': True, 'tool': tool, 'user': user_name, 'fallback': fallback, 'doc': doc})


# MCP 工具定义
MCP_TOOLS = [
    {
        'name': 'list_work_items',
        'description': '列出当前用户的工作事项（支持按状态筛选）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'status': {'type': 'string', 'enum': ['pending', 'in_progress', 'completed', ''], 'description': '筛选状态'},
                'limit': {'type': 'integer', 'default': 20}
            }
        }
    },
    {
        'name': 'get_work_item',
        'description': '获取单个工作事项的详细信息',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'item_id': {'type': 'integer', 'description': '工作项 ID'}
            },
            'required': ['item_id']
        }
    },
    {
        'name': 'create_work_item',
        'description': '创建新的工作事项（通过 MCP 创建时请传 source=ai 与 tool=工具名，工作台将显示「xxx MCP 同步」来源徽标）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'title': {'type': 'string', 'description': '标题'},
                'description': {'type': 'string', 'description': '描述'},
                'category': {'type': 'string', 'description': '分类'},
                'priority': {'type': 'string', 'enum': ['P0', 'P1', 'P2', 'P3'], 'description': '优先级'},
                'due_date': {'type': 'string', 'description': '截止日期 YYYY-MM-DD'},
                'source': {'type': 'string', 'enum': ['manual', 'ai', 'dingtalk'], 'description': '来源标记（默认 manual；AI 工具创建请传 ai）'},
                'tool': {'type': 'string', 'description': '来源工具标识（如 workbuddy / qoder / cline），工作台任务卡片将显示「{tool} MCP 同步」'}
            },
            'required': ['title']
        }
    },
    {
        'name': 'update_work_item_status',
        'description': '更新工作事项状态（AI 完成任务阶段后回写状态，实现 AI 工具 ↔ 工作台状态同步）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'item_id': {'type': 'integer'},
                'status': {'type': 'string', 'enum': ['pending', 'in_progress', 'completed']}
            },
            'required': ['item_id', 'status']
        }
    },
    {
        'name': 'log_ai_activity',
        'description': '将 AI 工具中的会话/工作活动归档为工作台任务（标题=会话主题，摘要=做了什么，来源标记 AI 协作）。AI 完成一个功能、阶段或咨询后调用，实现 AI 工作自动沉淀到工作台。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'title': {'type': 'string', 'description': '活动标题（如「开发 MCP 双协议兼容修复」）'},
                'description': {'type': 'string', 'description': '活动摘要（做了什么、产出什么）'},
                'category': {'type': 'string', 'description': '分类（默认 AI 协作）'},
                'priority': {'type': 'string', 'enum': ['P0', 'P1', 'P2', 'P3'], 'description': '优先级'},
                'status': {'type': 'string', 'enum': ['pending', 'in_progress', 'completed'], 'description': '归档时的状态（默认 in_progress）'},
                'due_date': {'type': 'string', 'description': '截止日期 YYYY-MM-DD'},
                'tool': {'type': 'string', 'description': '来源工具标识（如 workbuddy / qoder / cline），工作台任务卡片将显示「{tool} MCP 同步」'}
            },
            'required': ['title']
        }
    },
    {
        'name': 'sync_work_items',
        'description': '增量同步工作台任务（AI 会话开始时调用，传入上次的 server_time 作为 since，获取自该时间以来新增/变更的任务；返回本次 server_time 供下次使用）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'since': {'type': 'string', 'description': '上次同步的 server_time（YYYY-MM-DD HH:MM:SS），空则返回全部'},
                'status': {'type': 'string', 'enum': ['pending', 'in_progress', 'completed', ''], 'description': '筛选状态'},
                'limit': {'type': 'integer', 'default': 200}
            }
        }
    },
    {
        'name': 'save_knowledge',
        'description': '将 AI 工具整理的项目资料、技术问答、方案结论沉淀到用户个人知识库（来源标记 AI 协作）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'title': {'type': 'string', 'description': '资料标题'},
                'content': {'type': 'string', 'description': '资料内容（正文/结论/要点）'}
            },
            'required': ['title', 'content']
        }
    },
    {
        'name': 'get_stats',
        'description': '获取当前用户的工作统计（总任务、已完成、逾期、平均耗时等）',
        'inputSchema': {'type': 'object', 'properties': {}}
    },
    {
        'name': 'get_user_knowledge',
        'description': '获取当前用户的个人知识库内容（钉钉同步数据）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'source': {'type': 'string', 'enum': ['dingtalk_todo', 'dingtalk_calendar', 'dingtalk_chat', 'dingtalk_minutes', ''], 'description': '来源筛选'},
                'limit': {'type': 'integer', 'default': 10}
            }
        }
    }
]


def _mcp_call_tool(user_id, tool_name, args, token_prefix=''):
    """执行 MCP 工具调用（内部实现）

    token_prefix：发起请求的 MCP Token 前 8 位（每用户唯一），写入任务的 mcp_token_prefix 列，
    用于追溯"谁通过哪个工具创建了这条工作事项"。
    """
    db = sqlite3.connect()
    today = date.today().isoformat()
    try:
        if tool_name == 'list_work_items':
            status = (args or {}).get('status', '')
            limit = (args or {}).get('limit', 20)
            sql = ('SELECT id, title, status, priority, due_date, category, actual_duration_minutes, '
                   'source, tool_label, mcp_token_prefix FROM work_items WHERE user_id = ?')
            params = [user_id]
            if status:
                sql += ' AND status = ?'
                params.append(status)
            sql += ' ORDER BY updated_at DESC LIMIT ?'
            params.append(limit)
            rows = db.execute(sql, params).fetchall()
            return {'content': [{'type': 'text', 'text': json.dumps([dict(r) for r in rows], ensure_ascii=False)}]}

        if tool_name == 'get_work_item':
            item_id = (args or {}).get('item_id')
            row = db.execute('SELECT * FROM work_items WHERE id = ? AND user_id = ?', (item_id, user_id)).fetchone()
            return {'content': [{'type': 'text', 'text': json.dumps(dict(row) if row else {'error': 'not found'}, ensure_ascii=False)}]}

        if tool_name == 'create_work_item':
            a = args or {}
            title = a.get('title', '').strip()
            if not title:
                return {'content': [{'type': 'text', 'text': json.dumps({'error': 'title required'}, ensure_ascii=False)}], 'isError': True}
            source = a.get('source', 'manual')
            if source not in ('manual', 'ai', 'dingtalk'):
                source = 'manual'
            tool_label = (a.get('tool') or '').strip()[:32]
            cur = db.execute(
                'INSERT INTO work_items (user_id, title, description, category, priority, due_date, created_by, source, tool_label, mcp_token_prefix, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (user_id, title, a.get('description', ''), a.get('category', '日常运维'), a.get('priority', 'P2'), a.get('due_date', ''), 'mcp', source, tool_label, token_prefix, _now_str())
            )
            db.commit()
            return {'content': [{'type': 'text', 'text': json.dumps({'ok': True, 'item_id': cur.lastrowid, 'source': source, 'tool_label': tool_label}, ensure_ascii=False)}]}

        if tool_name == 'log_ai_activity':
            a = args or {}
            title = a.get('title', '').strip()
            if not title:
                return {'content': [{'type': 'text', 'text': json.dumps({'error': 'title required'}, ensure_ascii=False)}], 'isError': True}
            status = a.get('status', 'in_progress')
            if status not in ('pending', 'in_progress', 'completed'):
                status = 'in_progress'
            tool_label = (a.get('tool') or '').strip()[:32]
            now = _now_str()
            completed_at = now if status == 'completed' else None
            cur = db.execute(
                'INSERT INTO work_items (user_id, title, description, category, priority, due_date, created_by, source, status, tool_label, mcp_token_prefix, created_at, updated_at, completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (user_id, title, a.get('description', ''), a.get('category', 'AI 协作'), a.get('priority', 'P2'),
                 a.get('due_date', ''), 'mcp', 'ai', status, tool_label, token_prefix, now, now, completed_at)
            )
            db.commit()
            return {'content': [{'type': 'text', 'text': json.dumps(
                {'ok': True, 'item_id': cur.lastrowid, 'source': 'ai', 'tool_label': tool_label,
                 'hint': f'已归档到工作台，来源标记「{tool_label} MCP 同步」' if tool_label else '已归档到工作台，来源标记「AI 协作」'}, ensure_ascii=False)}]}

        if tool_name == 'sync_work_items':
            a = args or {}
            since = a.get('since', '')
            status = a.get('status', '')
            limit = int(a.get('limit', 200) or 200)
            sql = ('SELECT id, title, description, category, priority, status, due_date, source, tool_label, mcp_token_prefix, '
                   'created_at, updated_at, completed_at, actual_duration_minutes FROM work_items WHERE user_id = ?')
            params = [user_id]
            if since:
                sql += ' AND updated_at > ?'
                params.append(since)
            if status:
                sql += ' AND status = ?'
                params.append(status)
            sql += ' ORDER BY updated_at ASC LIMIT ?'
            params.append(limit)
            rows = db.execute(sql, params).fetchall()
            now = _now_str()
            return {'content': [{'type': 'text', 'text': json.dumps(
                {'items': [dict(r) for r in rows], 'count': len(rows), 'server_time': now,
                 'hint': '将 server_time 保存为下次调用的 since，即可实现增量同步'}, ensure_ascii=False)}]}

        if tool_name == 'save_knowledge':
            a = args or {}
            title = a.get('title', '').strip()
            content = a.get('content', '')
            if not title:
                return {'content': [{'type': 'text', 'text': json.dumps({'error': 'title required'}, ensure_ascii=False)}], 'isError': True}
            cur = db.execute(
                'INSERT INTO user_knowledge (user_id, source, title, content, created_at) VALUES (?,?,?,?,?)',
                (user_id, 'ai', title, content, _now_str())
            )
            db.commit()
            return {'content': [{'type': 'text', 'text': json.dumps(
                {'ok': True, 'knowledge_id': cur.lastrowid, 'source': 'ai',
                 'hint': '已保存到个人知识库（AI 协作来源）'}, ensure_ascii=False)}]}

        if tool_name == 'update_work_item_status':
            a = args or {}
            item_id = a.get('item_id')
            status = a.get('status')
            item = db.execute('SELECT * FROM work_items WHERE id = ? AND user_id = ?', (item_id, user_id)).fetchone()
            if not item:
                return {'content': [{'type': 'text', 'text': json.dumps({'error': 'not found'}, ensure_ascii=False)}], 'isError': True}
            now = _now_str()
            updates, params = ['status = ?', 'updated_at = ?'], [status, now]
            if status == 'completed':
                updates.append('completed_at = ?')
                params.append(now)
                if item['started_at']:
                    try:
                        dur = int((datetime.fromisoformat(now) - datetime.fromisoformat(item['started_at'])).total_seconds() / 60)
                        updates.append('actual_duration_minutes = ?')
                        params.append(max(dur, 1))
                    except Exception:
                        pass
            elif status == 'in_progress' and item['status'] != 'in_progress':
                updates.append('started_at = ?')
                params.append(now)
            params.append(item_id)
            db.execute(f"UPDATE work_items SET {', '.join(updates)} WHERE id = ?", params)
            db.commit()
            return {'content': [{'type': 'text', 'text': json.dumps({'ok': True}, ensure_ascii=False)}]}

        if tool_name == 'get_stats':
            total = db.execute('SELECT COUNT(*) as c FROM work_items WHERE user_id=?', (user_id,)).fetchone()['c']
            completed = db.execute("SELECT COUNT(*) as c FROM work_items WHERE user_id=? AND status='completed'", (user_id,)).fetchone()['c']
            pending = db.execute("SELECT COUNT(*) as c FROM work_items WHERE user_id=? AND status!='completed'", (user_id,)).fetchone()['c']
            overdue = db.execute("SELECT COUNT(*) as c FROM work_items WHERE user_id=? AND status!='completed' AND due_date!='' AND due_date < ?", (user_id, today)).fetchone()['c']
            avg = db.execute("SELECT AVG(actual_duration_minutes) as avg FROM work_items WHERE user_id=? AND status='completed' AND actual_duration_minutes>0", (user_id,)).fetchone()['avg'] or 0
            # v29.1：MySQL AVG() 返回 Decimal，json.dumps 无法序列化，统一转 float
            return {'content': [{'type': 'text', 'text': json.dumps({
                'total': total, 'completed': completed, 'pending': pending, 'overdue': overdue,
                'avg_duration_minutes': round(float(avg), 1)
            }, ensure_ascii=False)}]}

        if tool_name == 'get_user_knowledge':
            a = args or {}
            source = a.get('source', '')
            limit = a.get('limit', 10)
            sql = 'SELECT id, source, title, content, created_at FROM user_knowledge WHERE user_id = ?'
            params = [user_id, limit]
            if source:
                sql += ' AND source = ?'
                params = [user_id, source, limit]
            sql += ' ORDER BY created_at DESC LIMIT ?'
            rows = db.execute(sql, params).fetchall()
            return {'content': [{'type': 'text', 'text': json.dumps([dict(r) for r in rows], ensure_ascii=False)}]}

        return {'content': [{'type': 'text', 'text': json.dumps({'error': f'unknown tool {tool_name}'}, ensure_ascii=False)}], 'isError': True}
    finally:
        db.close()


# ---- v26.0：MCP 会话通道（跨 gunicorn worker，基于 /app/data/mcp_sessions 文件系统） ----
def _mcp_sessions_root():
    d = os.environ.get('MCP_SESSIONS_ROOT', '/app/data/mcp_sessions')
    os.makedirs(d, exist_ok=True)
    return d


def _mcp_session_create(uid=None):
    """创建新会话目录；顺带清理 1 小时以上的残留会话。
    v29.2：写入 uid 凭据文件，/mcp/message 可仅凭 sessionId 鉴权（token 不再出现在 URL）"""
    root = _mcp_sessions_root()
    now = time.time()
    try:
        for name in os.listdir(root):
            p = os.path.join(root, name)
            try:
                if os.path.isdir(p) and now - os.path.getmtime(p) > 3600:
                    import shutil as _sh
                    _sh.rmtree(p, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass
    sid = uuid.uuid4().hex[:16]
    sdir = os.path.join(root, sid)
    os.makedirs(sdir, exist_ok=True)
    if uid is not None:
        try:
            with open(os.path.join(sdir, 'uid'), 'w') as f:
                f.write(str(uid))
        except Exception:
            pass
    return sid, sdir


def _mcp_session_uid(sid):
    """v29.2：按 sessionId 读取会话属主（严格 hex 格式校验防路径穿越）"""
    if not sid or not re.fullmatch(r'[0-9a-f]{16}', sid):
        return None
    try:
        with open(os.path.join(_mcp_sessions_root(), sid, 'uid')) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _mcp_session_dir(sid):
    """校验会话 ID 并返回目录（不存在返回 None）；sid 仅允许十六进制，防路径穿越"""
    if not sid or not re.fullmatch(r'[0-9a-f]{16}', sid):
        return None
    d = os.path.join(_mcp_sessions_root(), sid)
    return d if os.path.isdir(d) else None


def _mcp_session_push(sid, payload):
    """向会话通道写入一条待下发消息（原子写：tmp -> rename）"""
    d = _mcp_session_dir(sid)
    if not d:
        return False
    try:
        seq = time.time_ns()
        tmp = os.path.join(d, str(seq) + '.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(d, str(seq) + '.json'))
        return True
    except Exception as e:
        print('[mcp] 会话推送失败 sid=%s: %s' % (sid, e))
        return False


def _mcp_session_drain(sdir):
    """取走并删除会话目录内全部待下发消息（按写入顺序）"""
    out = []
    try:
        for fn in sorted(os.listdir(sdir)):
            if not fn.endswith('.json'):
                continue
            p = os.path.join(sdir, fn)
            try:
                with open(p, encoding='utf-8') as f:
                    out.append(json.load(f))
                os.remove(p)
            except Exception:
                pass
    except Exception:
        pass
    return out


def _handle_mcp_jsonrpc(uid):
    """处理 MCP JSON-RPC 请求（initialize / tools/list / tools/call）"""
    req = request.get_json(force=True, silent=True) or {}
    req_id = req.get('id')
    method = req.get('method', '')
    params = req.get('params', {})

    # v26.0：JSON-RPC 通知（无 id，如 notifications/initialized）不产生响应，
    # 由调用方直接回 202；修复此前返回 -32601 导致标准客户端握手中断的问题
    if req_id is None:
        return None

    if method == 'ping':
        return jsonify({'jsonrpc': '2.0', 'id': req_id, 'result': {}})

    if method == 'initialize':
        return jsonify({
            'jsonrpc': '2.0', 'id': req_id,
            'result': {
                'protocolVersion': '2024-11-05',
                'capabilities': {'tools': {}},
                'serverInfo': {'name': MCP_SERVER_NAME, 'version': MCP_SERVER_VERSION}
            }
        })

    if method == 'tools/list':
        return jsonify({'jsonrpc': '2.0', 'id': req_id, 'result': {'tools': MCP_TOOLS}})

    if method == 'tools/call':
        name = params.get('name', '')
        args = params.get('arguments', {})
        # v24：透传发起请求的 MCP Token 前缀（每用户唯一），写入任务用于溯源来源工具
        token_prefix = _mcp_auth_token()[:8]
        result = _mcp_call_tool(uid, name, args, token_prefix)
        return jsonify({'jsonrpc': '2.0', 'id': req_id, 'result': result})

    return jsonify({'jsonrpc': '2.0', 'id': req_id, 'error': {'code': -32601, 'message': f'Method not found: {method}'}})


@app.route('/mcp/sse', methods=['GET', 'POST'])
def mcp_sse():
    """MCP 端点（兼容 SSE transport 与 streamableHttp 探测）

    - GET：标准 SSE transport。服务端先发 `event: endpoint` 告知 message URL，
      客户端随后 POST JSON-RPC 到 /mcp/message?sessionId=xxx，并保持长连接（20s keepalive）。
      （v29.2：token 不再拼入 endpoint URL，改由 sessionId 会话凭据鉴权，避免长期令牌泄露到日志）
    - POST：兼容 WorkBuddy 等客户端对端点的 POST 探测/JSON-RPC 直连
      （v22 修复：WorkBuddy 5.3.12 信任后会 POST 本端点，此前 405 导致连接报错）。
    """
    token = _mcp_auth_token()
    uid = _mcp_verify_token(token)
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401

    # POST：带 JSON body 时按 JSON-RPC 处理（兼容 streamableHttp 语义）
    if request.method == 'POST':
        if request.data and request.data.strip():
            ct = (request.headers.get('Content-Type') or '').lower()
            if 'json' in ct or request.data[:1] in (b'{', b'['):
                r = _handle_mcp_jsonrpc(uid)
                if r is None:
                    return '', 202
                return r

    # v26.0：标准 SSE transport —— 每个连接独立会话，POST 的 JSON-RPC 响应
    # 经由本事件流以 event: message 下发（QoderWork 等标准客户端依赖此通道），
    # 跨 gunicorn worker 通过 /app/data/mcp_sessions 文件通道中转
    sid, sdir = _mcp_session_create(uid)
    base = request.host_url.rstrip('/')
    endpoint = f'{base}/mcp/message?sessionId={sid}'  # v29.2：不再携带 token

    def generate():
        import shutil as _sh
        try:
            yield f'event: endpoint\ndata: {endpoint}\n\n'
            idle = 0.0
            while True:
                msgs = _mcp_session_drain(sdir)
                if msgs:
                    idle = 0.0
                    for m in msgs:
                        yield 'event: message\ndata: ' + json.dumps(m, ensure_ascii=False) + '\n\n'
                else:
                    time.sleep(0.3)
                    idle += 0.3
                    if idle >= 20:
                        idle = 0.0
                        yield ': keepalive\n\n'
        finally:
            _sh.rmtree(sdir, ignore_errors=True)

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/mcp/message', methods=['POST'])
def mcp_message():
    """MCP JSON-RPC 消息处理端点（v26.0：标准 SSE transport 双通道兼容）

    - 带 sessionId：响应推送至对应 SSE 会话通道（标准客户端收 event: message），
      HTTP 回 202 并附带响应体（简化版客户端仍可直接读 body，双向兼容）
    - 不带 sessionId：沿用旧行为，响应直接放 HTTP body（200）
    - 通知（无 id）：202 无响应体
    """
    token = _mcp_auth_token()
    uid = _mcp_verify_token(token)
    if not uid:
        # v29.2：兼容 SSE endpoint 不再携带 token 后的会话凭据鉴权；
        # 旧客户端带 ?token= 的调用仍走上面的 token 校验，双向兼容
        uid = _mcp_session_uid(request.args.get('sessionId', ''))
    if not uid:
        return jsonify({'jsonrpc': '2.0', 'error': {'code': -32001, 'message': 'Unauthorized'}, 'id': None}), 401
    resp = _handle_mcp_jsonrpc(uid)
    if resp is None:
        return '', 202
    body = resp.get_data(as_text=True)
    sid = request.args.get('sessionId', '')
    if sid and _mcp_session_push(sid, json.loads(body)):
        return Response(body, status=202, mimetype='application/json')
    return Response(body, status=200, mimetype='application/json')


# ====================================================================
# 钉钉数据同步（纯 DWS CLI 方案，按用户严格隔离）
# ====================================================================

def _dws_exe():
    """查找 dws 可执行文件路径，未找到返回 None"""
    import shutil
    exe = shutil.which('dws')
    if not exe:
        for p in ('/app/dws', '/app/data/dws_bin/dws', '/usr/local/bin/dws', '/usr/bin/dws', '/root/.local/bin/dws'):
            if os.path.exists(p):
                exe = p
                break
    return exe


def _dws_token_dir(user_id):
    """每用户 DWS 配置目录（严格隔离）"""
    d = os.path.join(DWS_TOKENS_DIR, str(user_id))
    os.makedirs(d, exist_ok=True)
    return d


def _dws_command(user_id, args_list):
    """通过 DWS CLI 执行钉钉命令（每用户独立 token 目录隔离）。
    隔离方式：设置 HOME=<token_dir>，同时隔离 ~/.dws（配置）
    与 ~/.local/share/dws-cli（加密 token 数据），防止跨用户串号。
    未安装 dws 时返回 None。"""
    import subprocess
    exe = _dws_exe()
    if not exe:
        return None
    token_dir = _dws_token_dir(user_id)
    env = dict(os.environ)
    env['HOME'] = token_dir  # 完整隔离：~/.dws + ~/.local/share/dws-cli
    try:
        r = subprocess.run([exe] + args_list, capture_output=True, text=True, timeout=90, env=env)
        out = (r.stdout or '').strip() or (r.stderr or '').strip()
        if not out:
            return None
        # v25.9-debug: log raw output for chat commands
        if 'chat' in args_list and 'list-all-conversations' in args_list:
            print(f'[dws-debug] user {user_id}: stdout_len={len(r.stdout or "")}, stderr_len={len(r.stderr or "")}, rc={r.returncode}')
            if r.stderr:
                print(f'[dws-debug] user {user_id}: stderr={r.stderr[:500]}')
        try:
            return json.loads(out)
        except Exception:
            return {'_text': out[:20000]}
    except Exception as e:
        print(f'[dws] user {user_id} 执行异常: {e}')
        return None


def _dws_find_union_id(user_id):
    """通过 dws auth status 获取当前已授权用户的 unionId"""
    out = _dws_command(user_id, ['auth', 'status'])
    if not out:
        return None
    if isinstance(out, dict):
        # dws auth status 可能返回用户信息
        uid = out.get('unionId') or out.get('union_id') or out.get('userId') or out.get('user_id')
        if uid:
            return str(uid)
        # 有些版本返回嵌套结构
        user = out.get('user') or out.get('data') or {}
        uid = user.get('unionId') or user.get('union_id') or user.get('userId') or user.get('user_id')
        if uid:
            return str(uid)
    # 尝试解析文本输出
    if isinstance(out, dict) and out.get('_text'):
        text = out['_text']
        import re
        m = re.search(r'unionId["\']?\s*[:=]\s*["\']?([\w-]+)', text, re.I)
        if m:
            return m.group(1)
    return None


def _dws_authed(user_id):
    """检查该用户是否已完成 dws 授权（token 有效）"""
    out = _dws_command(user_id, ['auth', 'status'])
    if isinstance(out, dict):
        if out.get('authenticated') is True:
            return True
        if out.get('_text') and '"authenticated": true' in out['_text']:
            return True
    return False


def _dws_authed_user_id(user_id):
    """已授权时返回 dws user_id（钉钉企业内唯一 ID），未授权返回 None"""
    out = _dws_command(user_id, ['auth', 'status'])
    if not isinstance(out, dict) or out.get('authenticated') is not True:
        return None
    return str(out.get('user_id') or out.get('userId') or '').strip() or None


# ---- 每用户设备码授权（多用户并发安全）----
# 设计：每个用户独立 DWS_CONFIG_DIR（token 文件隔离）+ 最多一个
# 后台 device-login 进程；标志文件记录 pid/user_code，进程退出即授权结束。
# 多个用户同时授权互不影响；同一用户重复点击复用进行中的授权码。
_device_login_lock = threading.Lock()

def _device_login_flag(user_id):
    """读取当前用户的设备码授权标志文件，返回 dict 或 None"""
    flag_path = os.path.join(_dws_token_dir(user_id), 'device_login.json')
    if not os.path.exists(flag_path):
        return None
    try:
        with open(flag_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _device_login_alive(flag):
    """检查标志文件中的进程是否还存活"""
    pid = flag.get('pid')
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _kill_device_login(user_id):
    """终止当前用户的设备码授权进程并清理标志文件"""
    flag = _device_login_flag(user_id)
    if flag and flag.get('pid'):
        try:
            os.kill(flag['pid'], 9)
        except Exception:
            pass
    try:
        os.remove(os.path.join(_dws_token_dir(user_id), 'device_login.json'))
    except Exception:
        pass


def _start_device_login(user_id):
    """启动 dws auth login --device 后台进程（每用户独立），
    返回 (user_code, verify_url, expires_in) 或 None。
    线程锁保护：避免同一用户并发点击时重复启动进程。"""
    with _device_login_lock:
        return _start_device_login_locked(user_id)


def _start_device_login_locked(user_id):
    import subprocess
    import re
    exe = _dws_exe()
    if not exe:
        return None
    token_dir = _dws_token_dir(user_id)
    now = datetime.now()

    # 已有进行中的授权 → 复用
    flag = _device_login_flag(user_id)
    if flag and _device_login_alive(flag):
        try:
            expires = datetime.fromisoformat(flag.get('expires_at'))
            if expires > now and flag.get('user_code'):
                left = int((expires - now).total_seconds())
                return flag['user_code'], flag.get('verify_url') or '', max(left, 30)
        except Exception:
            pass
    # 过期/失效 → 清理后重新启动
    _kill_device_login(user_id)

    env = dict(os.environ)
    env['HOME'] = token_dir  # 完整隔离：~/.dws + ~/.local/share/dws-cli
    log_path = os.path.join(token_dir, 'device_login.log')
    try:
        logf = open(log_path, 'w', encoding='utf-8')
        proc = subprocess.Popen([exe, 'auth', 'login', '--device'],
                                stdout=logf, stderr=subprocess.STDOUT,
                                env=env, start_new_session=True)
    except Exception as e:
        print(f'[dws] 启动 device-login 失败 user={user_id}: {e}')
        return None

    # 轮询日志等待 user_code（最多 30 秒）
    user_code = verify_url = None
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(1)
        try:
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                text = f.read()
            m = re.search(r'authorization code:\s*([A-Z0-9-]+)', text)
            if m:
                user_code = m.group(1)
            m2 = re.search(r'user_code=([A-Z0-9-]+)', text)
            if not user_code and m2:
                user_code = m2.group(1)
            m3 = re.search(r'(https://login\.dingtalk\.com/oauth2/device/verify\.htm[^\s"\')\]]*)', text)
            if m3:
                verify_url = m3.group(1)
            if user_code:
                break
        except Exception:
            pass

    if not user_code:
        try:
            os.kill(proc.pid, 9)
        except Exception:
            pass
        print(f'[dws] 30 秒内未获取到 user_code user={user_id}')
        return None
    if not verify_url:
        verify_url = f'https://login.dingtalk.com/oauth2/device/verify.htm?user_code={user_code}'

    # 写标志文件（840s 内有效，提前 60s 便于过期重试）
    flag = {
        'pid': proc.pid,
        'user_code': user_code,
        'verify_url': verify_url,
        'started_at': now.isoformat(),
        'expires_at': (now + timedelta(seconds=840)).isoformat(),
    }
    try:
        with open(os.path.join(token_dir, 'device_login.json'), 'w', encoding='utf-8') as f:
            json.dump(flag, f, ensure_ascii=False)
    except Exception:
        pass
    return user_code, verify_url, 840


def _auto_bind_if_authed(user_id):
    """若用户已 dws 授权但未写绑定表，自动写入 dingtalk_bindings，返回 union_id 或 None"""
    if not _dws_authed(user_id):
        return None
    dus = _dws_authed_user_id(user_id)
    if not dus:
        return None
    db = get_db()
    row = db.execute('SELECT union_id FROM dingtalk_bindings WHERE user_id = ?', (user_id,)).fetchone()
    if row and row['union_id'] == dus:
        return dus
    db.execute('''REPLACE INTO dingtalk_bindings
        (user_id, union_id, access_token, refresh_token, expires_at, updated_at)
        VALUES (?, ?, '', '', '', ?)''',
        (user_id, dus, datetime.now().isoformat()))
    db.commit()
    return dus


def _save_knowledge(user_id, source, external_id, title, content, raw=None, event_time='', occur_date='', conversation_type=''):
    """写入个人知识库（同源同外部ID 更新，严格按用户隔离）
    event_time: 实际发生时间（ISO 文本，用于今日维度排序）
    occur_date: 发生日期 YYYY-MM-DD（冗余便于查询）
    conversation_type: 聊天记录会话类型（p2p 单聊 / group 群聊）
    """
    conn = sqlite3.connect()
    try:
        conn.execute("""
            INSERT INTO user_knowledge (user_id, source, title, content, raw_data, external_id, created_at, event_time, occur_date, conversation_type)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON DUPLICATE KEY UPDATE
                title = VALUES(title), content = VALUES(content), raw_data = VALUES(raw_data),
                event_time = VALUES(event_time), occur_date = VALUES(occur_date),
                conversation_type = VALUES(conversation_type)
        """, (user_id, source, title, content,
              json.dumps(raw, ensure_ascii=False) if raw else None,
              external_id, datetime.now().isoformat(), event_time, occur_date, conversation_type))
        conn.commit()
    finally:
        conn.close()


def _ms2iso(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return ''


def _is_group_chat_from_row(row):
    """判断一条聊天记录是否来自群聊：优先使用 conversation_type 字段，其次 fallback 标题/内容中的「群」字"""
    ct = (row.get('conversation_type') or '').strip()
    if ct == 'group':
        return True
    if ct == 'p2p':
        return False
    # fallback：会话标题含「群」
    title = (row.get('title') or '').strip()
    if '群' in title:
        return True
    # fallback：内容中的会话名含「群」
    content = (row.get('content') or '').strip()
    m = re.search(r'会话：([^\n]+)', content)
    if m and '群' in m.group(1):
        return True
    return False


def _chat_sender(content):
    """从 content 中提取发送人"""
    m = re.search(r'发送人：([^\n]+)', content or '')
    return m.group(1).strip() if m else ''


def _is_my_message(content, my_name):
    """v29.7：判断一条聊天记录是否当前用户本人发出（供 AI 分析区分收发方向）"""
    if not my_name:
        return False
    return _chat_sender(content or '') == my_name


def _filter_chat_records(chat_rows, user_id, db=None):
    """v25.7 聊天记录过滤：
    - 单聊（p2p）全部保留
    - 群聊（group）只保留当前用户自己发送的消息
    """
    if not chat_rows:
        return []
    my_name = ''
    if db is not None:
        row = db.execute('SELECT display_name FROM users WHERE id = ?', (user_id,)).fetchone()
        if row:
            my_name = (row['display_name'] or '').strip()
    filtered = []
    for r in chat_rows:
        if isinstance(r, dict):
            r = dict(r)
        is_group = _is_group_chat_from_row(r)
        if not is_group:
            filtered.append(r)
            continue
        sender = _chat_sender(r.get('content') or '')
        # 自己发送：sender 等于自己的 display_name
        if my_name and sender == my_name:
            filtered.append(r)
    return filtered


def _norm_dt(v):
    """把任意时间格式（毫秒时间戳/ISO/日期串）归一为 'YYYY-MM-DD HH:MM'，失败返回 ''"""
    if v is None:
        return ''
    s = str(v).strip()
    if not s:
        return ''
    # 纯毫秒时间戳
    if s.isdigit() and len(s) >= 10:
        iso = _ms2iso(s)
        return iso
    # ISO 8601: 2026-08-13T09:30:00+08:00 或带 T 的
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})', s)
    if m:
        return f'{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}'
    # 纯日期
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return f'{m.group(1)}-{m.group(2)}-{m.group(3)} 00:00'
    return ''


def sync_dingtalk_user(user_id, types, mode='incremental'):
    """同步指定用户的钉钉数据到个人知识库（todo/calendar/chat/minutes）
    mode: 'full' 首次全量（30 天聊天/更多会话与消息）；'incremental' 增量（近 2 天，快）"""
    union_id = _dws_find_union_id(user_id)
    if not union_id:
        return {'error': '该用户尚未在服务器上完成 dws 授权，请先执行 dws auth login'}
    full = (mode == 'full')
    results = {}
    for st in types:
        try:
            if st == 'todo':
                results[st] = _sync_dt_todo(user_id, full)
            elif st == 'calendar':
                results[st] = _sync_dt_calendar(user_id, full)
            elif st == 'chat':
                results[st] = _sync_dt_chat(user_id, full)
            elif st == 'minutes':
                results[st] = _sync_dt_minutes(user_id, full)
            else:
                results[st] = {'status': 'skip', 'message': f'未知类型 {st}'}
        except Exception as e:
            results[st] = {'status': 'error', 'message': str(e)}
    return results


def _sync_dt_todo(user_id, full=False):
    """待办：dws todo task list（分页拉取，结构 result.todoCards）
    full：拉 8 页（约 160 条）首次全量；增量只拉 2 页（约 40 条）"""
    all_items = []
    pages = 8 if full else 2
    for page in range(1, pages + 1):
        out = _dws_command(user_id, ['todo', 'task', 'list', '--page', str(page), '--size', '20', '-y'])
        if out is None:
            break
        res = out.get('result') or {}
        items = res.get('todoCards') or res.get('todos') or res.get('tasks') or res.get('items') or out.get('todoCards') or out.get('todos')
        if isinstance(out, list):
            items = out
        if not isinstance(items, list) or not items:
            break
        all_items.extend(items)
        if not res.get('hasMore'):
            break
    if not all_items:
        return {'status': 'ok', 'count': 0, 'message': '无待办可同步'}
    n = 0
    for t in all_items:
        tid = str(t.get('taskId') or t.get('id') or '')
        if not tid:
            continue
        title = t.get('subject') or t.get('title') or '未命名待办'
        due_ms = t.get('dueTime')
        due = _ms2iso(due_ms) if due_ms else (t.get('dueDate') or '')
        detail = t.get('description') or t.get('content') or ''
        done = t.get('finalStatusStage') == 2 or t.get('done') or t.get('isDone') or t.get('status') == 'done'
        content = f"待办：{title}\n状态：{'已完成' if done else '未完成'}\n截止：{due or '无'}\n说明：{detail}"
        ev = _norm_dt(due)
        _save_knowledge(user_id, 'dingtalk_todo', tid, f'📌 {title}', content, t,
                        event_time=ev, occur_date=ev[:10])
        n += 1
    # 删除本地已存在但钉钉已删除的待办（仅在全量同步时清理）
    if full:
        all_tids = [str(t.get('taskId') or t.get('id') or '') for t in all_items if (t.get('taskId') or t.get('id'))]
        try:
            conn = sqlite3.connect()
            if all_tids:
                placeholders = ','.join('?' * len(all_tids))
                conn.execute(
                    f'DELETE FROM user_knowledge WHERE user_id=? AND source=? AND external_id NOT IN ({placeholders})',
                    (user_id, 'dingtalk_todo') + tuple(all_tids))
            else:
                conn.execute('DELETE FROM user_knowledge WHERE user_id=? AND source=?', (user_id, 'dingtalk_todo'))
            conn.commit()
            conn.close()
        except Exception:
            pass
    return {'status': 'ok', 'count': n, 'message': f'同步待办 {n} 条'}


def _sync_dt_calendar(user_id, full=False):
    """日程：dws calendar event list（必须带时间窗口，否则报错）
    v19：full 首次全量严格回看 30 天（与聊天 30 天窗口一致）；增量：回看 7 天"""
    now = datetime.now()
    lookback = 30 if full else 7
    start_iso = (now - timedelta(days=lookback)).strftime('%Y-%m-%dT00:00:00+08:00')
    end_iso = (now + timedelta(days=90)).strftime('%Y-%m-%dT23:59:59+08:00')
    out = _dws_command(user_id, ['calendar', 'event', 'list', '--start', start_iso, '--end', end_iso, '--limit', '100', '-y'])
    if out is None:
        return {'status': 'skip', 'message': 'DWS 日程同步无返回，跳过'}
    items = None
    if isinstance(out, list):
        items = out
    elif isinstance(out, dict):
        res = out.get('result') or {}
        items = res.get('events') or res.get('items') or out.get('events') or out.get('items') or out.get('list') or out.get('data')
        if items is None and out.get('_text'):
            _save_knowledge(user_id, 'dingtalk_calendar', 'dws_full', '🗓️ 钉钉日程',
                            out['_text'], out)
            return {'status': 'ok', 'count': 1, 'message': '同步日程 1 份（原始导出）'}
    if not isinstance(items, list) or not items:
        return {'status': 'ok', 'count': 0, 'message': '无日程可同步'}
    n = 0
    for evt in items:
        eid = str(evt.get('id') or evt.get('eventId') or '')
        if not eid:
            continue
        title = evt.get('summary') or evt.get('title') or evt.get('subject') or '日程'
        start = evt.get('start', {}) or {}
        s_str = start.get('dateTime') or start.get('date') or evt.get('startTime') or ''
        end = evt.get('end', {}) or {}
        e_str = end.get('dateTime') or end.get('date') or ''
        loc = evt.get('location') or ''
        desc = evt.get('description') or evt.get('content') or ''
        attendees = evt.get('attendees') or []
        who = '、'.join([a.get('displayName', '') for a in attendees if a.get('displayName')]) or '无'
        content = (f"日程：{title}\n开始：{s_str}\n结束：{e_str or '无'}\n"
                   f"地点：{loc or '无'}\n参与人：{who}\n说明：{desc or '无'}")
        ev = _norm_dt(s_str)
        _save_knowledge(user_id, 'dingtalk_calendar', eid, f'🗓️ {title}', content, evt,
                        event_time=ev, occur_date=ev[:10])
        n += 1
    # 删除本地已存在但钉钉已删除的日程（仅在全量同步时清理）
    if full:
        all_eids = [str(evt.get('id') or evt.get('eventId') or '') for evt in items if (evt.get('id') or evt.get('eventId'))]
        try:
            conn = sqlite3.connect()
            if all_eids:
                placeholders = ','.join('?' * len(all_eids))
                conn.execute(
                    f'DELETE FROM user_knowledge WHERE user_id=? AND source=? AND external_id NOT IN ({placeholders})',
                    (user_id, 'dingtalk_calendar') + tuple(all_eids))
            else:
                conn.execute('DELETE FROM user_knowledge WHERE user_id=? AND source=?', (user_id, 'dingtalk_calendar'))
            conn.commit()
            conn.close()
        except Exception:
            pass
    return {'status': 'ok', 'count': n, 'message': f'同步日程 {n} 条'}


def _sync_dt_chat(user_id, full=False):
    """聊天：dws chat list-all-conversations 拉会话列表，再按会话拉消息
    v19：full（首次全量）严格 30 天窗口，每会话 cursor 无上限翻页（不再有 500 条/会话限制），后台执行
    incremental（每日增量）：50 会话 × 2 天 × cursor 分页拉取（每页30条），快速完成
    v25.9-fix：添加 --direction newer 确保从给定时间往现在拉消息（默认可能是 older）
    v25.9-fix2：DWS --limit 最大 100，需配合 --cursor 翻页获取全部会话"""
    # 1. 拉会话列表（DWS --limit 最大 100，需分页）
    all_convs = []
    cursor = None
    page_guard = 0
    while True:
        page_guard += 1
        if page_guard > 100:
            break
        args = ['chat', 'list-all-conversations', '--limit', '100', '-y']
        if cursor:
            args.extend(['--cursor', str(cursor)])
        out = _dws_command(user_id, args)
        convs_page = []
        if isinstance(out, list):
            convs_page = out
        elif isinstance(out, dict):
            res = out.get('result') or {}
            convs_page = res.get('conversations') or res.get('conversationList') or out.get('conversations') or out.get('conversationList') or out.get('data') or []
        if isinstance(convs_page, list) and convs_page:
            all_convs.extend(convs_page)
        # 提取下一页 cursor
        next_cursor = None
        if isinstance(out, dict):
            res = out.get('result') or {}
            next_cursor = res.get('nextCursor') or out.get('nextCursor') or res.get('cursor') or out.get('cursor')
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    convs = all_convs
    print(f'[chat-sync] user {user_id}: got {len(convs)} conversations from DWS (paginated)')
    if not isinstance(convs, list) or not convs:
        return {'status': 'ok', 'count': 0, 'message': '无会话可同步'}
    # 按最近消息时间排序，取最近有消息的前 N 个会话
    def _last_ts(c):
        t = c.get('lastMsgCreateAt') or c.get('createAt') or ''
        return str(t)
    convs.sort(key=_last_ts, reverse=True)
    top_n = 50 if full else 15
    convs = [c for c in convs if c.get('lastMsgCreateAt') or c.get('unreadPoint', 0) > 0][:top_n]
    print(f'[chat-sync] user {user_id}: after filter, {len(convs)} conversations')
    n = 0
    now = datetime.now()
    days = 30 if full else 2
    since = (now - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    per_conv = 100 if full else 30
    print(f'[chat-sync] user {user_id}: since={since}, days={days}')
    for c in convs:
        cid = c.get('openConversationId') or ''
        title = c.get('title') or c.get('conversationName') or '会话'
        if not cid:
            continue
        # v25.7：判断会话类型（单聊/群聊）
        conv_type = 'p2p'
        ct = str(c.get('conversationType') or '').lower()
        if ct in ('group', '2', 'chatgroup'):
            conv_type = 'group'
        elif '群' in str(title):
            conv_type = 'group'
        # 2. 拉该会话消息（支持 cursor 分页，无上限翻页；防御：最大 1000 页 + cursor 不推进即停）
        # v25.9-fix：添加 --direction newer 确保从 since 时间往现在拉消息
        cursor = None
        page_guard = 0
        while True:
            page_guard += 1
            if page_guard > 1000:
                break
            args = ['chat', 'message', 'list', '--group', cid, '--time', since, '--direction', 'newer', '--limit', str(per_conv), '-y']
            if cursor:
                args.extend(['--cursor', str(cursor)])
            mout = _dws_command(user_id, args)
            msgs = []
            if isinstance(mout, list):
                msgs = mout
            elif isinstance(mout, dict):
                res = mout.get('result') or {}
                msgs = res.get('messages') or mout.get('messages') or mout.get('items') or []
            print(f'[chat-sync] user {user_id} conv {title}: got {len(msgs)} messages (page {page_guard})')
            if isinstance(msgs, list) and msgs:
                for m in msgs:
                    mid = str(m.get('openMessageId') or m.get('messageId') or m.get('id') or '')
                    if not mid:
                        continue
                    sender = m.get('sender') or m.get('senderNick') or m.get('nick') or ''
                    text = m.get('content') or m.get('text') or m.get('msgContent') or ''
                    created = m.get('createTime') or m.get('createAt') or m.get('createdAt') or ''
                    if not text:
                        continue
                    ctext = str(text)[:2000]
                    title2 = f'[{title}] {sender}: {ctext[:30]}'
                    content = f"会话：{title}\n发送人：{sender}\n时间：{created}\n内容：{ctext}"
                    ev = _norm_dt(created)
                    m_ext = dict(m) if isinstance(m, dict) else {'_raw': m}
                    m_ext['_conversationId'] = cid
                    m_ext['_conversationTitle'] = title
                    _save_knowledge(user_id, 'dingtalk_chat', mid, f'💬 {title2}', content, m_ext,
                                    event_time=ev, occur_date=ev[:10], conversation_type=conv_type)
                    n += 1
            # 提取下一页 cursor
            next_cursor = None
            if isinstance(mout, dict):
                res = mout.get('result') or {}
                next_cursor = res.get('nextCursor') or mout.get('nextCursor') or res.get('cursor') or mout.get('cursor')
            if not next_cursor or next_cursor == cursor:
                break  # 无下一页，或 cursor 未推进（防死循环）
            cursor = next_cursor
    return {'status': 'ok', 'count': n, 'message': f'同步聊天记录 {n} 条（{len(convs)} 个会话）'}


def _sync_dt_minutes(user_id, full=False):
    """听记：dws minutes list all 拉列表，再逐条 get summary 获取摘要
    full：100 条；增量：20 条"""
    limit = 100 if full else 20
    out = _dws_command(user_id, ['minutes', 'list', 'all', '--limit', str(limit), '-y'])
    if out is None:
        return {'status': 'skip', 'message': 'DWS 听记同步无返回，跳过'}
    items = None
    if isinstance(out, list):
        items = out
    elif isinstance(out, dict):
        res = out.get('result') or {}
        items = res.get('itemList') or res.get('items') or res.get('minutes') or out.get('items') or out.get('minutes') or out.get('list') or out.get('data')
        if items is None and out.get('_text'):
            _save_knowledge(user_id, 'dingtalk_minutes', 'dws_full', '🎙️ 钉钉听记',
                            out['_text'], out)
            return {'status': 'ok', 'count': 1, 'message': '同步听记 1 份（原始导出）'}
    if not isinstance(items, list) or not items:
        return {'status': 'ok', 'count': 0, 'message': '无听记可同步'}
    n = 0
    for it in items:
        mid = str(it.get('uuid') or it.get('minuteId') or it.get('id') or '')
        if not mid:
            continue
        title = it.get('title') or it.get('name') or '听记'
        st = it.get('startTime') or it.get('createTime') or ''
        # 列表接口不含摘要，需单独调用 get summary
        summary = ''
        try:
            sout = _dws_command(user_id, ['minutes', 'get', 'summary', '--id', mid, '-y'])
            if isinstance(sout, dict):
                res = sout.get('result') or sout
                summary = res.get('fullSummary') or res.get('summary') or res.get('content') or ''
            elif isinstance(sout, str):
                summary = sout
        except Exception:
            summary = ''
        s_str = _ms2iso(st) if str(st).isdigit() else (st or '')
        if summary:
            content = f"听记：{title}\n时间：{s_str}\n\n{str(summary)[:3000]}"
        else:
            content = f"听记：{title}\n时间：{s_str}\n（无摘要，可手动查看详情）"
        ev = _norm_dt(s_str)
        _save_knowledge(user_id, 'dingtalk_minutes', mid, f'🎙️ {title}', content, it,
                        event_time=ev, occur_date=ev[:10])
        n += 1
    return {'status': 'ok', 'count': n, 'message': f'同步听记 {n} 条（含摘要）'}


# ---- 后台全量同步（首次 30 天，不阻塞页面）----
# 每用户一把锁，防止重复触发；状态写入 dingtalk_bindings.sync_status
_sync_locks = {}
_sync_locks_guard = threading.Lock()


def _user_sync_lock(user_id):
    with _sync_locks_guard:
        if user_id not in _sync_locks:
            _sync_locks[user_id] = threading.Lock()
        return _sync_locks[user_id]


def _set_sync_status(user_id, status):
    try:
        conn = sqlite3.connect()
        conn.execute("UPDATE dingtalk_bindings SET sync_status = ? WHERE user_id = ?",
                     (status, user_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[sync-status] user {user_id} 更新失败: {e}')


def _bg_full_sync(user_id, types):
    """后台线程：执行全量同步，完成后记录 full_synced_at"""
    lock = _user_sync_lock(user_id)
    if not lock.acquire(blocking=False):
        print(f'[bg-sync] user {user_id} 已有同步任务进行中，跳过')
        return
    try:
        _set_sync_status(user_id, 'running')
        results = sync_dingtalk_user(user_id, types, mode='full')
        counts = {k: v.get('count', 0) for k, v in results.items() if isinstance(v, dict)}
        total = sum(counts.values())
        detail = '；'.join(f'{k}:{counts.get(k, 0)}' for k in counts)
        _set_sync_status(user_id, f'ok|{datetime.now().strftime("%Y-%m-%d %H:%M")}|{total} 条({detail})')
        try:
            conn = sqlite3.connect()
            conn.execute('UPDATE dingtalk_bindings SET full_synced_at = ?, updated_at = ? WHERE user_id = ?',
                         (datetime.now().isoformat(), datetime.now().isoformat(), user_id))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f'[bg-sync] user {user_id} 记录 full_synced_at 失败: {e}')
        print(f'[bg-sync] user {user_id} 全量同步完成，共 {total} 条')
    except Exception as e:
        _set_sync_status(user_id, f'error|{datetime.now().strftime("%Y-%m-%d %H:%M")}|{e}')
        print(f'[bg-sync] user {user_id} 全量同步异常: {e}')
    finally:
        lock.release()


def _daily_dingtalk_sync():
    """每天早上自动增量同步所有已绑定用户的钉钉数据（增量模式，快速）
    v25.7：写入 dingtalk_sync_log，便于前端查看每日同步状态"""
    today = date.today().isoformat()
    log = {
        'sync_date': today,
        'sync_at': datetime.now().isoformat(),
        'user_count': 0,
        'chat_count': 0,
        'todo_count': 0,
        'calendar_count': 0,
        'minutes_count': 0,
        'status': 'ok',
        'detail': ''
    }
    try:
        conn = sqlite3.connect()
        conn
        users = conn.execute('SELECT user_id FROM dingtalk_bindings').fetchall()
        conn.close()
        log['user_count'] = len(users)
        errors = []
        for u in users:
            try:
                results = sync_dingtalk_user(u['user_id'], ['todo', 'calendar', 'chat', 'minutes'], mode='incremental')
                if isinstance(results, dict):
                    log['todo_count'] += results.get('todo', {}).get('count', 0)
                    log['calendar_count'] += results.get('calendar', {}).get('count', 0)
                    log['chat_count'] += results.get('chat', {}).get('count', 0)
                    log['minutes_count'] += results.get('minutes', {}).get('count', 0)
            except Exception as e:
                errors.append(f"user {u['user_id']}: {e}")
                print(f'[daily-sync] user {u["user_id"]}: {e}')
        detail = f"todo={log['todo_count']},calendar={log['calendar_count']},chat={log['chat_count']},minutes={log['minutes_count']}"
        if errors:
            log['status'] = 'partial'
            log['detail'] = detail + '; errors=' + '; '.join(errors)
        else:
            log['detail'] = detail
        print(f'[daily-sync] {datetime.now().strftime("%Y-%m-%d %H:%M")} 完成，共处理 {len(users)} 个绑定用户，{detail}')
    except Exception as e:
        log['status'] = 'error'
        log['detail'] = str(e)
        print(f'[daily-sync] 异常: {e}')
    finally:
        try:
            conn = sqlite3.connect()
            conn.execute("""
                INSERT INTO dingtalk_sync_log (sync_date, sync_at, user_count, chat_count, todo_count, calendar_count, minutes_count, status, detail)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON DUPLICATE KEY UPDATE
                    sync_at=VALUES(sync_at), user_count=VALUES(user_count),
                    chat_count=VALUES(chat_count), todo_count=VALUES(todo_count),
                    calendar_count=VALUES(calendar_count), minutes_count=VALUES(minutes_count),
                    status=VALUES(status), detail=VALUES(detail)
            """, (log['sync_date'], log['sync_at'], log['user_count'], log['chat_count'],
                  log['todo_count'], log['calendar_count'], log['minutes_count'], log['status'], log['detail']))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f'[daily-sync] 写入日志失败: {e}')


# ---- DWS 绑定（手动输入 unionId，验证通过后绑定）----
@app.route('/api/dingtalk/config')
@login_required
def dingtalk_config():
    """查询 dws 是否已安装（决定前端展示绑定按钮还是提示安装）"""
    installed = bool(_dws_exe())
    return jsonify({'installed': installed})


@app.route('/api/dingtalk/status')
@login_required
def dingtalk_status():
    """查询当前用户的 DWS 绑定状态（已授权但未写绑定表时自动补绑）"""
    uid = session['user_id']
    # 已授权但绑定表缺失 → 自动补绑（如 wangdj 已授权场景）
    _auto_bind_if_authed(uid)
    db = get_db()
    row = db.execute('SELECT union_id, updated_at, full_synced_at, sync_status FROM dingtalk_bindings WHERE user_id = ?',
                     (uid,)).fetchone()
    if row:
        return jsonify({
            'bound': True,
            'union_id': row['union_id'],
            'updated_at': row['updated_at'],
            'full_synced_at': row['full_synced_at'] or '',
            'sync_status': row['sync_status'] or '',
            'running': row['sync_status'] == 'running',
        })
    return jsonify({'bound': False})


@app.route('/api/dingtalk/daily-sync-log')
@login_required
def dingtalk_daily_sync_log():
    """查询最近 N 天的每日自动同步日志（默认 7 天），用于确认今早 8 点是否已同步"""
    days = min(int(request.args.get('days') or 7), 30)
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    db = get_db()
    rows = db.execute(
        "SELECT sync_date, sync_at, user_count, chat_count, todo_count, calendar_count, minutes_count, status, detail "
        "FROM dingtalk_sync_log WHERE sync_date >= ? ORDER BY sync_date DESC LIMIT ?",
        (since, days)
    ).fetchall()
    today_str = date.today().isoformat()
    today_row = db.execute(
        "SELECT sync_date, sync_at, status, detail FROM dingtalk_sync_log WHERE sync_date = ?",
        (today_str,)
    ).fetchone()
    today_synced = bool(today_row) and today_row['status'] in ('ok', 'partial')
    # v25.7 兜底：升级首日 sync_log 可能还没数据，但 last_daily_sync.txt 已标记当天
    if not today_synced:
        try:
            with open('/app/data/last_daily_sync.txt', 'r', encoding='utf-8') as f:
                today_synced = f.read().strip() == today_str
        except Exception:
            pass
    return jsonify({
        'today_synced': today_synced,
        'today': today_row and dict(today_row),
        'logs': [dict(r) for r in rows]
    })


@app.route('/api/dingtalk/device-login', methods=['POST'])
@login_required
def dingtalk_device_login():
    """为当前登录用户生成钉钉设备码授权（每用户独立进程 + 独立 token 目录）。
    已授权用户直接返回绑定状态；未授权返回 user_code + verify_url 供扫码。"""
    uid = session['user_id']
    # 已授权 → 自动绑定
    bound_uid = _auto_bind_if_authed(uid)
    if bound_uid:
        return jsonify({'bound': True, 'union_id': bound_uid})
    # 未授权 → 启动（或复用）设备码授权
    r = _start_device_login(uid)
    if not r:
        return jsonify({'error': '启动钉钉设备码授权失败：服务器未安装 dws 或 dws 不可执行'}), 500
    user_code, verify_url, expires_in = r
    return jsonify({
        'bound': False,
        'user_code': user_code,
        'verify_url': verify_url,
        'expires_in': expires_in
    })


@app.route('/api/dingtalk/device-status')
@login_required
def dingtalk_device_status():
    """轮询当前用户的设备码授权状态；授权成功后自动写入绑定表"""
    uid = session['user_id']
    bound_uid = _auto_bind_if_authed(uid)
    if bound_uid:
        # 顺带清理已完成/失效的授权进程
        _kill_device_login(uid)
        return jsonify({'bound': True, 'union_id': bound_uid})
    flag = _device_login_flag(uid)
    if flag and _device_login_alive(flag):
        return jsonify({'bound': False, 'pending': True,
                        'user_code': flag.get('user_code'),
                        'expires_in': 60})
    return jsonify({'bound': False, 'pending': False})


@app.route('/api/dingtalk/bind', methods=['POST'])
@login_required
def dingtalk_bind():
    """手动绑定 unionId：验证该 unionId 在当前用户的 dws token 下是否有效"""
    data = request.get_json() or {}
    union_id = (data.get('unionId') or data.get('union_id') or '').strip()
    if not union_id:
        return jsonify({'error': '缺少 unionId'}), 400
    # 验证：尝试用该用户的 dws 获取 unionId，看是否匹配
    detected = _dws_find_union_id(session['user_id'])
    if not detected:
        return jsonify({'error': '该用户尚未在服务器上完成 dws 授权。请在服务器上执行：dws auth login --device'}), 400
    if detected != union_id:
        return jsonify({'error': f'unionId 不匹配。dws 检测到的 unionId 为 {detected}，请填写正确的 unionId'}), 400
    db = get_db()
    db.execute('''REPLACE INTO dingtalk_bindings
        (user_id, union_id, access_token, refresh_token, expires_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)''',
        (session['user_id'], union_id, '', '', '', datetime.now().isoformat()))
    db.commit()
    return jsonify({'message': '钉钉账号绑定成功', 'union_id': union_id})


@app.route('/api/dingtalk/unbind', methods=['DELETE'])
@login_required
def dingtalk_unbind():
    """解绑钉钉账号"""
    db = get_db()
    db.execute('DELETE FROM dingtalk_bindings WHERE user_id = ?', (session['user_id'],))
    db.execute('DELETE FROM user_knowledge WHERE user_id = ? AND source LIKE "dingtalk_%"', (session['user_id'],))
    db.commit()
    return jsonify({'message': '已解绑并清除同步数据'})


@app.route('/api/dingtalk/sync', methods=['POST'])
@login_required
def dingtalk_sync():
    """触发钉钉数据同步（待办/日程/聊天/听记）→ 个人知识库
    mode=full：后台全量同步（首次 30 天），立即返回，前端轮询 /api/dingtalk/sync-status
    mode=incremental（默认）：前台增量同步（近 2 天），快速完成"""
    db = get_db()
    binding = db.execute('SELECT * FROM dingtalk_bindings WHERE user_id = ?',
                         (session['user_id'],)).fetchone()
    if not binding:
        return jsonify({'error': '未绑定钉钉账号，请先绑定'}), 400

    data = request.get_json() or {}
    valid_types = ('todo', 'calendar', 'chat', 'minutes')
    sync_types = data.get('types') or ['todo', 'calendar', 'chat', 'minutes']
    sync_types = [t for t in sync_types if t in valid_types] or ['todo', 'calendar', 'chat', 'minutes']
    mode = data.get('mode', 'incremental')

    if mode == 'full':
        # 后台全量同步：立即返回，不阻塞页面
        lock = _user_sync_lock(session['user_id'])
        if not lock.acquire(blocking=False):
            return jsonify({'message': '已有全量同步正在进行中，请稍候查看状态', 'background': True, 'running': True})
        lock.release()
        t = threading.Thread(target=_bg_full_sync, args=(session['user_id'], sync_types), daemon=True)
        t.start()
        return jsonify({
            'message': '已启动后台全量同步（首次拉取约 30 天数据），完成后会通知，可继续操作',
            'background': True,
            'running': True,
            'types': sync_types
        })

    # 增量同步（前台快速执行）
    results = sync_dingtalk_user(session['user_id'], sync_types, mode='incremental')
    if 'error' in results:
        return jsonify(results), 400
    counts = {k: v.get('count', 0) for k, v in results.items() if isinstance(v, dict)}
    total = sum(counts.values())
    detail = '；'.join(f'{k}:{counts.get(k, 0)}' for k in counts)
    _set_sync_status(session['user_id'], f'ok|{datetime.now().strftime("%Y-%m-%d %H:%M")}|{total} 条({detail})')
    try:
        db.execute('UPDATE dingtalk_bindings SET updated_at = ? WHERE user_id = ?',
                   (datetime.now().isoformat(), session['user_id']))
        db.commit()
    except Exception:
        pass
    return jsonify({
        'message': f'同步完成，共更新 {total} 条',
        'types': sync_types,
        'results': results
    })


@app.route('/api/dingtalk/sync-status')
@login_required
def dingtalk_sync_status():
    """查询当前用户的同步状态（全量是否进行中 / 上次全量时间 / 最近结果）"""
    db = get_db()
    row = db.execute('SELECT full_synced_at, sync_status, updated_at FROM dingtalk_bindings WHERE user_id = ?',
                     (session['user_id'],)).fetchone()
    if not row:
        return jsonify({'bound': False})
    status = dict(row)
    running = bool(status.get('sync_status') == 'running')
    # 兼容旧数据：sync_status 为空但 full_synced_at 有值
    full_done = bool(status.get('full_synced_at'))
    return jsonify({
        'bound': True,
        'running': running,
        'full_synced': full_done,
        'full_synced_at': status.get('full_synced_at') or '',
        'sync_status': status.get('sync_status') or '',
        'updated_at': status.get('updated_at') or '',
    })


@app.route('/api/knowledge')
@login_required
def list_knowledge():
    """获取当前用户知识库内容（严格隔离），支持 source/类型/搜索/日期过滤
    sort=time 时按实际发生时间(event_time)倒序，否则按同步时间(created_at)
    date=YYYY-MM-DD 时只返回该日发生的数据（今日维度用）
    """
    db = get_db()
    source = request.args.get('source', '')
    search = (request.args.get('search') or '').strip()
    sort = request.args.get('sort', '')
    fdate = (request.args.get('date') or '').strip()
    # v19：取消 500 条查询上限（首次全量同步 30 天聊天远超 500 条，查询截断会显示不全）
    # 默认 50000，硬上限 100000 仅防恶意参数
    limit = request.args.get('limit', '50000')
    try:
        limit = min(int(limit), 100000)
    except Exception:
        limit = 50000
    query = 'SELECT id, source, title, content, external_id, created_at, event_time, occur_date FROM user_knowledge WHERE user_id = ?'
    params = [session['user_id']]
    if source:
        query += ' AND source = ?'
        params.append(source)
    if search:
        query += ' AND (title LIKE ? OR content LIKE ?)'
        params.append(f'%{search}%')
        params.append(f'%{search}%')
    if fdate:
        query += ' AND occur_date = ?'
        params.append(fdate)
    if sort == 'time':
        # 按实际发生时间倒序，无实际时间的按同步时间
        query += ' ORDER BY CASE WHEN event_time != \'\' THEN 1 ELSE 0 END DESC, event_time DESC, created_at DESC'
    else:
        query += ' ORDER BY created_at DESC'
    query += ' LIMIT ?'
    params.append(limit)
    rows = db.execute(query, params).fetchall()
    return jsonify([dict(r) for r in rows])



# ====================================================================
# v27.0：iTop ITSM 集成（MCP over Streamable HTTP）
# 数据来源: itop-mcp 服务（run_oql / add_ticket_log / apply_stimulus）
# ====================================================================
ITOP_MCP_URL = os.environ.get('ITOP_MCP_URL', 'http://YOUR_SERVER_IP:8003/mcp')
# v29.7.2：iTop Web UI 地址/账号（实时抓取工单当前状态可用流转动作，适配定制版状态机）
ITOP_WEB_URL = os.environ.get('ITOP_WEB_URL', '')
ITOP_WEB_USER = os.environ.get('ITOP_WEB_USER', '')
ITOP_WEB_PASS = os.environ.get('ITOP_WEB_PASS', '')
ITOP_CLASSES = ('UserRequest', 'Incident', 'Problem', 'Change')
ITOP_CLOSED_STATUSES = ('resolved', 'closed', 'reject')
# v29.7.2：静态映射仅作 iTop Web 抓取失败时的兜底；Incident 为 10.10.11.161 定制版
# 状态机实测结果（assigned 只能 ev_process 接单，无 ev_startworking），
# 权威来源是 /api/itop/tickets/<cls>/<ref>/transitions 实时抓取。
ITOP_STIMULUS_LABEL = {
    'ev_assign': '分配', 'ev_startworking': '开始处理', 'ev_process': '接单',
    'ev_pending': '待定', 'ev_resume': '恢复', 'ev_resolve': '标记为已解决',
    'ev_manual_resolve': '手动关闭', 'ev_repair': '委外维修', 'ev_reassign': '重新分配',
    'ev_close': '关闭', 'ev_reopen': '重新打开', 'ev_return': '退回',
    'ev_withdraw': '撤回', 'ev_submit_approval': '提交审批', 'ev_reject': '拒绝',
    'ev_update': '恢复处理',
}
ITOP_STATE_STIMULI = {
    'UserRequest': {
        'new': ['ev_assign', 'ev_reject'],
        'assigned': ['ev_startworking', 'ev_reassign', 'ev_pending'],
        'in_progress': ['ev_resolve', 'ev_pending', 'ev_reassign'],
        'pending': ['ev_update', 'ev_resolve', 'ev_reassign'],
        'resolved': ['ev_close', 'ev_reopen'],
        'closed': [],
        'reject': ['ev_reopen'],
    },
    'Incident': {
        'new': ['ev_assign', 'ev_resolve', 'ev_submit_approval'],
        'assigned': ['ev_process', 'ev_return', 'ev_withdraw'],
        'in_process': ['ev_pending', 'ev_resolve', 'ev_reassign', 'ev_repair', 'ev_manual_resolve'],
        'pending': ['ev_resume'],
        'waiting_for_approval': ['ev_withdraw'],
        'resolved': ['ev_close', 'ev_reopen'],
        'closed': [],
    },
}
# v29.0 工单流转可提交字段白名单（各工单类字段不同，iTop 端会二次校验）
ITOP_STIMULUS_FIELD_WHITELIST = frozenset({
    'resolution_code', 'solution', 'difficulty_level', 'pending_reason',
    'team_id', 'agent_id', 'servicefamily_id', 'service_id', 'servicesubcategory_id',
    'caller_id', 'org_id', 'approver_id', 'receiver_id', 'time_spent',
    'user_satisfaction', 'user_comment', 'return_reason', 'assigne_reason',
    'withdraw_reason', 'notes', 'comment', 'handling_method', 'is_repair',
    'symptom', 'repair_notes', 'resolution_date', 'close_date',
})
# v29.8：itop-mcp 专用流转工具映射（内置字段校验，推荐优先于通用 apply_stimulus）；
# None = 定制动作无专用工具，走通用 apply_stimulus + 字段自适应重试
ITOP_STIMULUS_TOOL = {
    'ev_assign': 'assign_ticket', 'ev_reassign': 'reassign_ticket',
    'ev_process': 'process_ticket', 'ev_resolve': 'resolve_ticket',
    'ev_pending': 'pending_ticket', 'ev_close': 'close_ticket',
}
# v29.8：枚举选项（itop-mcp 内置校验定义；解决方式 Incident 用枚举串、UserRequest 兼容 assistance）
ITOP_RESOLUTION_CODES = [
    ('assistance', '日常运维/协助'), ('fix_applied', '已修复'), ('workaround', '临时方案'),
    ('known_error', '已知错误'), ('other', '其他'),
]
ITOP_PENDING_REASONS = [
    ('awaiting_caller', '等待用户反馈'), ('awaiting_change', '等待变更'),
    ('awaiting_supplier', '等待供应商'), ('other', '其他'),
]
# v29.8：下拉选项缓存（团队/人员/服务目录，避免每次开弹窗都查 iTop）
_itop_opts_cache = {'ts': 0, 'data': None}
_itop_opts_lock = threading.Lock()


def _itop_options():
    """团队/处理工程师/服务族/服务/服务子类别下拉数据（10 分钟缓存）"""
    now = time.time()
    with _itop_opts_lock:
        if _itop_opts_cache['data'] and now - _itop_opts_cache['ts'] < 600:
            return _itop_opts_cache['data']
    client = _get_itop_client()
    out = {}
    # itop-mcp run_oql 单页上限 200 条，子类别超千条需分页拉全；
    # persons 限定团队成员（全量 active 超 8000 含会议室等噪声，团队在编仅数百）
    for k, oql in {
        'teams': "SELECT Team",
        'persons': ("SELECT Person AS p JOIN lnkPersonToTeam AS l ON l.person_id=p.id "
                    "WHERE p.status = 'active'"),
        'service_families': "SELECT ServiceFamily",
        'services': "SELECT Service",
        'subcategories': "SELECT ServiceSubcategory",
    }.items():
        # 服务/子类别带上父级外键，供默认补全时选配套值（族→服务→子类别）
        outf = 'id,friendlyname,servicefamily_id' if k == 'services' \
            else ('id,friendlyname,service_id' if k == 'subcategories' else 'id,friendlyname')
        try:
            rows, page = [], 1
            while page <= 20:
                res = client.call_json('run_oql', {
                    'oql': oql, 'output_fields': outf,
                    'limit': 200, 'page': page})
                batch = res if isinstance(res, list) else []
                rows.extend(batch)
                if len(batch) < 200:
                    break
                page += 1
            items, seen = [], set()
            for r in rows:
                if not isinstance(r, dict):
                    continue
                rid, name = str(r.get('id') or ''), str(r.get('friendlyname') or '').strip()
                if rid and rid not in seen:
                    seen.add(rid)
                    it = {'id': rid, 'name': name}
                    if k == 'services':
                        it['servicefamily_id'] = str(r.get('servicefamily_id') or '')
                    elif k == 'subcategories':
                        it['service_id'] = str(r.get('service_id') or '')
                    items.append(it)
            items.sort(key=lambda x: x['name'])
            out[k] = items
        except Exception:
            out[k] = []
    with _itop_opts_lock:
        _itop_opts_cache['ts'] = now
        _itop_opts_cache['data'] = out
    return out


def _itop_default_service_triple():
    """v29.8 同日补丁：服务族/服务/子类别默认值（配套选取）。
    env ITOP_DEFAULT_SERVICEFAMILY_ID/ITOP_DEFAULT_SERVICE_ID 优先；
    否则选「事件处理/日常运维」族（取第一个含下属服务的族），服务取该族首条，子类别取该服务首条。"""
    fam = os.environ.get('ITOP_DEFAULT_SERVICEFAMILY_ID', '').strip()
    svc = os.environ.get('ITOP_DEFAULT_SERVICE_ID', '').strip()
    sub = os.environ.get('ITOP_DEFAULT_SERVICESUBCATEGORY_ID', '').strip()
    try:
        opts = _itop_options()
    except Exception:
        return fam or None, svc or None, sub or None
    fams, svcs, subs = opts.get('service_families') or [], opts.get('services') or [], opts.get('subcategories') or []
    if not fam:
        picked_f = None
        for want in ('事件处理', '日常运维'):
            picked_f = next((f for f in fams if want in f['name']), None)
            if picked_f:
                break
        if not picked_f:
            picked_f = next((f for f in fams
                             if any(s.get('servicefamily_id') == f['id'] for s in svcs)), None) or (fams[0] if fams else None)
        fam = picked_f['id'] if picked_f else ''
    if fam and not svc:
        cand = [s for s in svcs if s.get('servicefamily_id') == fam] or svcs
        svc = cand[0]['id'] if cand else ''
    if svc and not sub:
        cand = [s for s in subs if s.get('service_id') == svc] or subs
        sub = cand[0]['id'] if cand else ''
    return fam or None, svc or None, sub or None


def _itop_autofill_fields(need, fields, row):
    """v29.8 同日补丁：iTop 必填字段默认值自动补全，补全后直接重试不再拦截。
    服务族/服务/子类别：用户已选 > 工单原值（raw_data）> 默认配套值；
    枚举类给安全默认（解决方式=日常运维/协助、挂起原因=其他）。
    返回 (autofill_dict, unfilled_list)。"""
    raw = {}
    try:
        raw = json.loads((row or {}).get('raw_data') or '{}')
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}
    triple = None
    autofill, unfilled = {}, []
    svc_keys = ('servicefamily_id', 'service_id', 'servicesubcategory_id')
    for f in need:
        if fields.get(f) not in (None, '', 0):
            continue
        if f in svc_keys:
            if triple is None:
                triple = _itop_default_service_triple()
            v = str(raw.get(f) or '').strip()
            if not v:
                v = {'servicefamily_id': triple[0], 'service_id': triple[1],
                     'servicesubcategory_id': triple[2]}[f] or ''
            if v:
                autofill[f] = v
            else:
                unfilled.append(f)
        elif f == 'resolution_code':
            autofill[f] = 'assistance'
        elif f == 'pending_reason':
            autofill[f] = 'other'
        else:
            v = raw.get(f)
            if v not in (None, '', 0):
                autofill[f] = v
            else:
                unfilled.append(f)
    return autofill, unfilled


_ITOP_MARK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

_itop_mcp = None
_itop_mcp_lock = threading.Lock()
_itop_sync_lock = threading.Lock()
_ITOP_SYNC_STATE = {
    'running': False, 'mode': '', 'started_at': '', 'finished_at': '',
    'last': None, 'error': ''
}


def _get_itop_client():
    # MCPHTTPClient 单例（懒初始化，线程安全）
    global _itop_mcp
    with _itop_mcp_lock:
        if _itop_mcp is None:
            _itop_mcp = MCPHTTPClient(ITOP_MCP_URL, timeout=60)
        return _itop_mcp


_itop_trans_cache = {}
_itop_trans_cache_lock = threading.Lock()


def _itop_web_transitions(cls, key, status=''):
    """v29.7.2：登录 iTop Web UI 抓取工单详情页，提取当前状态允许的流转动作。
    定制版 iTop 状态机与标准版不同（如 assigned 只能 ev_process 接单），
    REST API 不暴露可用 stimulus，工单详情页的工具栏按钮是权威来源。
    结果按 (cls, key, status) 缓存 60s；任何异常返回 None（调用方降级静态映射/透传）。"""
    if not (ITOP_WEB_URL and ITOP_WEB_USER):
        return None
    ckey = (cls, key, status or '')
    now = time.time()
    with _itop_trans_cache_lock:
        hit = _itop_trans_cache.get(ckey)
        if hit and now - hit[0] < 60:
            return hit[1]
    import urllib.request as _ureq
    import urllib.parse as _uparse
    import http.cookiejar as _hcj
    base = ITOP_WEB_URL.rstrip('/') + '/'
    try:
        key_i = int(key)
        opener = _ureq.build_opener(_ureq.HTTPCookieProcessor(_hcj.CookieJar()))
        opener.addheaders = [('User-Agent', 'infra-workbench/29')]
        data = _uparse.urlencode({'auth_user': ITOP_WEB_USER, 'auth_pwd': ITOP_WEB_PASS,
                                  'login_mode': 'form', 'loginop': 'login'}).encode('utf-8')
        req = _ureq.Request(base + 'pages/UI.php', data=data,
                            headers={'Content-Type': 'application/x-www-form-urlencoded',
                                     'User-Agent': 'infra-workbench/29'})
        with opener.open(req, timeout=15) as r:
            r.read()
        url = base + 'pages/UI.php?' + _uparse.urlencode(
            {'operation': 'details', 'class': cls, 'id': key_i})
        req = _ureq.Request(url, headers={'User-Agent': 'infra-workbench/29'})
        with opener.open(req, timeout=20) as r:
            body = r.read().decode('utf-8', 'replace')
        pairs = re.findall(
            r'operation=stimulus&amp;stimulus=(ev_[a-z0-9_]+)&amp;class=%s&amp;id=%d"[^>]*>\s*([^<]+?)\s*</a>'
            % (cls, key_i), body)
        seen, out = set(), []
        for code, label in pairs:
            if code not in seen:
                seen.add(code)
                out.append({'code': code, 'label': label.strip()})
        with _itop_trans_cache_lock:
            if len(_itop_trans_cache) > 200:
                _itop_trans_cache.clear()
            _itop_trans_cache[ckey] = (now, out or None)
        return out or None
    except Exception:
        return None


def _init_itop_tables():
    # v27.0 建表（幂等）：工单表 + 工程师映射表
    conn = sqlite3.connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS itop_tickets (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT,
                ticket_class VARCHAR(20) NOT NULL,
                ticket_ref VARCHAR(32) NOT NULL,
                ticket_key INT,
                title VARCHAR(512) NOT NULL DEFAULT '',
                description MEDIUMTEXT,
                solution MEDIUMTEXT,
                status VARCHAR(24) NOT NULL DEFAULT '',
                priority VARCHAR(8) DEFAULT '',
                caller_name VARCHAR(128) DEFAULT '',
                org_name VARCHAR(128) DEFAULT '',
                team_name VARCHAR(128) DEFAULT '',
                service_name VARCHAR(191) DEFAULT '',
                agent_name VARCHAR(128) DEFAULT '',
                start_date VARCHAR(19) DEFAULT '',
                resolution_date VARCHAR(19) DEFAULT '',
                close_date VARCHAR(19) DEFAULT '',
                last_update VARCHAR(19) DEFAULT '',
                time_spent INT DEFAULT 0,
                raw_data MEDIUMTEXT,
                created_at VARCHAR(19) DEFAULT '',
                updated_at VARCHAR(19) DEFAULT '',
                UNIQUE KEY uniq_class_ref (ticket_class, ticket_ref),
                KEY idx_user (user_id),
                KEY idx_status (status),
                KEY idx_last_update (last_update)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS itop_user_map (
                id INT AUTO_INCREMENT PRIMARY KEY,
                itop_agent_name VARCHAR(128) NOT NULL,
                user_id INT,
                updated_at VARCHAR(19) DEFAULT '',
                UNIQUE KEY uniq_agent (itop_agent_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        conn.commit()
    finally:
        conn.close()


def _extract_itop_objects(res):
    # 兼容 itop-mcp 多种返回结构，统一为字段 dict 列表
    if isinstance(res, list):
        rows = res
    elif isinstance(res, dict):
        rows = None
        for k in ('objects', 'results', 'items', 'data', 'tickets'):
            if k in res:
                rows = res[k]
                break
        if rows is None:
            rows = [res] if ('ref' in res or 'title' in res) else []
    else:
        return []
    out = []
    if isinstance(rows, dict):
        for _k, obj in rows.items():
            if isinstance(obj, dict):
                row = obj.get('fields') if isinstance(obj.get('fields'), dict) else obj
                row = dict(row)
                row.setdefault('key', _k)
                out.append(row)
    elif isinstance(rows, list):
        for obj in rows:
            if isinstance(obj, dict):
                row = obj.get('fields') if isinstance(obj.get('fields'), dict) else obj
                out.append(dict(row))
    return out


def _itop_map_user(conn, agent_name):
    # agent_name -> user_id：itop_user_map 优先，否则按 display_name 自动匹配
    if not agent_name:
        return None
    row = conn.execute('SELECT user_id FROM itop_user_map WHERE itop_agent_name = ?',
                       (agent_name,)).fetchone()
    if row and row.get('user_id'):
        return int(row['user_id'])
    u = conn.execute(
        "SELECT id FROM users WHERE display_name = ? OR display_name LIKE CONCAT('%-', ?)",
        (agent_name, agent_name)).fetchone()
    return int(u['id']) if u else None


def _upsert_itop_rows(conn, cls, rows):
    # 批量 upsert（ON DUPLICATE KEY UPDATE）
    upserted = 0
    for row in rows:
        try:
            ref = str(row.get('ref') or '').strip()
            if not ref:
                continue
            agent_name = str(row.get('agent_name') or '').strip()
            mapped_uid = _itop_map_user(conn, agent_name)
            try:
                key = int(row.get('key') or row.get('id') or 0)
            except (TypeError, ValueError):
                key = 0

            def _s(name, lim=512):
                v = row.get(name)
                if v is None:
                    return ''
                if isinstance(v, (dict, list)):
                    return json.dumps(v, ensure_ascii=False, default=str)[:lim]
                return str(v)[:lim]

            def _big(name):
                v = row.get(name)
                if v is None:
                    return None
                if isinstance(v, (dict, list)):
                    return json.dumps(v, ensure_ascii=False, default=str)
                return str(v)

            try:
                ts = int(row.get('time_spent') or 0)
            except (TypeError, ValueError):
                ts = 0
            now = _now_str()
            conn.execute("""
                INSERT INTO itop_tickets
                (user_id, ticket_class, ticket_ref, ticket_key, title, description, solution,
                 status, priority, caller_name, org_name, team_name, service_name, agent_name,
                 start_date, resolution_date, close_date, last_update, time_spent, raw_data,
                 created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON DUPLICATE KEY UPDATE
                 user_id=VALUES(user_id), ticket_key=VALUES(ticket_key), title=VALUES(title),
                 description=VALUES(description), solution=VALUES(solution), status=VALUES(status),
                 priority=VALUES(priority), caller_name=VALUES(caller_name), org_name=VALUES(org_name),
                 team_name=VALUES(team_name), service_name=VALUES(service_name), agent_name=VALUES(agent_name),
                 start_date=VALUES(start_date), resolution_date=VALUES(resolution_date),
                 close_date=VALUES(close_date), last_update=VALUES(last_update),
                 time_spent=VALUES(time_spent), raw_data=VALUES(raw_data), updated_at=VALUES(updated_at)
            """, (
                mapped_uid, cls, ref, key, _s('title'), _big('description'), _big('solution'),
                _s('status', 24), _s('priority', 8), _s('caller_name', 128), _s('org_name', 128),
                _s('team_name', 128), _s('service_name', 191), agent_name[:128],
                _s('start_date', 19), _s('resolution_date', 19), _s('close_date', 19),
                _s('last_update', 19), ts,
                json.dumps(row, ensure_ascii=False, default=str),
                now, now
            ))
            upserted += 1
        except Exception as e:
            print('[itop-sync] upsert %s fail: %s' % (cls, e))
    return upserted


def _sync_itop_tickets(mode='incremental'):
    # 同步 iTop 四类工单：incremental=近2天 / full=近90天（分页拉取）
    if not _itop_sync_lock.acquire(blocking=False):
        return {'skipped': 'sync already running'}
    _ITOP_SYNC_STATE.update({'running': True, 'mode': mode, 'started_at': _now_str(), 'error': ''})
    try:
        days = 2 if mode == 'incremental' else 90
        since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
        client = _get_itop_client()
        result = {'mode': mode, 'since': since, 'classes': {}, 'fetched': 0, 'upserted': 0}
        conn = sqlite3.connect()
        try:
            for cls in ITOP_CLASSES:
                page, cls_fetched, cls_upserted = 1, 0, 0
                while page <= 60:
                    oql = "SELECT %s WHERE last_update >= '%s'" % (cls, since)
                    res = client.call_json('run_oql', {
                        'oql': oql, 'output_fields': '*', 'limit': 100, 'page': page})
                    items = _extract_itop_objects(res)
                    if not items:
                        break
                    cls_fetched += len(items)
                    cls_upserted += _upsert_itop_rows(conn, cls, items)
                    if len(items) < 100:
                        break
                    page += 1
                result['classes'][cls] = {'fetched': cls_fetched, 'upserted': cls_upserted}
                result['fetched'] += cls_fetched
                result['upserted'] += cls_upserted
            conn.commit()
            result['unmapped'] = [r['agent_name'] for r in conn.execute(
                "SELECT DISTINCT agent_name FROM itop_tickets "
                "WHERE user_id IS NULL AND agent_name != ''").fetchall()]
            result['total'] = conn.execute('SELECT COUNT(*) as c FROM itop_tickets').fetchone()['c']
        finally:
            conn.close()
        result['finished_at'] = _now_str()
        _ITOP_SYNC_STATE.update({'running': False, 'finished_at': result['finished_at'], 'last': result})
        print('[itop-sync] done: ' + json.dumps(result, ensure_ascii=False)[:500])
        return result
    except Exception as e:
        _ITOP_SYNC_STATE.update({'running': False, 'error': str(e)})
        print('[itop-sync] error: %s' % e)
        return {'error': str(e)}
    finally:
        _itop_sync_lock.release()


def _sync_itop_tickets_safe(mode):
    try:
        _sync_itop_tickets(mode)
    except Exception as e:
        print('[itop-sync] safe wrapper: %s' % e)


def _itop_sync_key():
    # 工作时间（周一~周五 08-18 点）每小时一次，其余每天一次
    now = datetime.now()
    if now.weekday() < 5 and 8 <= now.hour <= 18:
        return now.strftime('%Y%m%d_%H')
    return now.strftime('%Y%m%d')


def _itop_sync_due():
    try:
        os.makedirs(_ITOP_MARK_DIR, exist_ok=True)
        return not os.path.exists(os.path.join(_ITOP_MARK_DIR, 'itop_sync_%s.done' % _itop_sync_key()))
    except Exception:
        return False


def _mark_itop_sync_done():
    try:
        os.makedirs(_ITOP_MARK_DIR, exist_ok=True)
        open(os.path.join(_ITOP_MARK_DIR, 'itop_sync_%s.done' % _itop_sync_key()), 'w').write(_now_str())
        cutoff = time.time() - 3 * 86400
        for fn in os.listdir(_ITOP_MARK_DIR):
            if fn.startswith('itop_sync_') and fn.endswith('.done'):
                p = os.path.join(_ITOP_MARK_DIR, fn)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                except OSError:
                    pass
    except Exception as e:
        print('[itop-sync] mark fail: %s' % e)


def _refresh_itop_ticket(cls, ref):
    # 写回后从 iTop 拉取单张工单并 upsert（同步最新状态）
    client = _get_itop_client()
    oql = "SELECT %s WHERE ref = '%s'" % (cls, ref)
    res = client.call_json('run_oql', {'oql': oql, 'output_fields': '*', 'limit': 5, 'page': 1})
    items = _extract_itop_objects(res)
    if not items:
        return None
    conn = sqlite3.connect()
    try:
        n = _upsert_itop_rows(conn, cls, items)
        conn.commit()
    finally:
        conn.close()
    return items[0] if n else None


@app.route('/api/itop/status')
@login_required
def itop_status():
    conn = get_db()
    # v28.5 子管理员作用域：统计口径限定本团队映射用户
    scope_team_id = get_admin_scope() if session.get('is_admin') else None
    if scope_team_id is not None:
        scope_cond = 'user_id IN (SELECT id FROM users WHERE team_id = %d)' % scope_team_id
    else:
        scope_cond = 'user_id IS NOT NULL'
    by_class = {}
    for r in conn.execute(
            'SELECT ticket_class, status, COUNT(*) as c FROM itop_tickets '
            'WHERE ' + scope_cond + ' GROUP BY ticket_class, status').fetchall():
        by_class.setdefault(r['ticket_class'], {'total': 0, 'active': 0, 'closed': 0})
        by_class[r['ticket_class']]['total'] += r['c']
        if r['status'] in ITOP_CLOSED_STATUSES:
            by_class[r['ticket_class']]['closed'] += r['c']
        else:
            by_class[r['ticket_class']]['active'] += r['c']
    # v27.1：口径限定本团队（映射到工作台用户的工单）；mapped_users = 已映射工程师数
    mapped_users = conn.execute(
        'SELECT COUNT(DISTINCT user_id) as c FROM itop_tickets WHERE ' + scope_cond).fetchone()['c']
    total = conn.execute(
        'SELECT COUNT(*) as c FROM itop_tickets WHERE ' + scope_cond).fetchone()['c']
    last_data = conn.execute(
        'SELECT MAX(updated_at) as t FROM itop_tickets WHERE ' + scope_cond).fetchone()['t'] or ''
    return jsonify({
        'enabled': True, 'mcp_url': ITOP_MCP_URL,
        'total': total, 'by_class': by_class,
        'mapped_users': mapped_users, 'last_data_at': last_data,
        'sync_state': _ITOP_SYNC_STATE
    })


def _itop_ticket_denied(conn, row):
    """v28.5 工单访问校验：普通用户仅本人；子管理员限本团队映射工单；主管理员放行"""
    if not session.get('is_admin'):
        return row['user_id'] != session['user_id']
    scope_team_id = get_admin_scope()
    if scope_team_id is None:
        return False
    if not row['user_id']:
        return True
    owner = conn.execute('SELECT team_id FROM users WHERE id = ?', (row['user_id'],)).fetchone()
    return (not owner) or owner['team_id'] != scope_team_id


@app.route('/api/itop/tickets')
@login_required
def itop_tickets_list():
    # 本人工单列表（管理员可 user_id= / scope=team 查全部）
    conn = get_db()
    where, params = [], []
    if session.get('is_admin') and request.args.get('scope') == 'team':
        scope_team_id = get_admin_scope()
        if scope_team_id is not None:
            # v28.5 子管理员：团队范围 = 本团队映射用户
            where.append('user_id IN (SELECT id FROM users WHERE team_id = ?)')
            params.append(scope_team_id)
        else:
            where.append('user_id IS NOT NULL')
    elif session.get('is_admin') and request.args.get('user_id'):
        target_uid = int(request.args.get('user_id'))
        scope_team_id = get_admin_scope()
        if scope_team_id is not None:
            in_team = conn.execute('SELECT id FROM users WHERE id = ? AND team_id = ?',
                                   (target_uid, scope_team_id)).fetchone()
            if not in_team:
                return jsonify({'error': '无权查看该成员的工单'}), 403
        where.append('user_id = ?')
        params.append(target_uid)
    else:
        where.append('user_id = ?')
        params.append(session['user_id'])
    cls = request.args.get('ticket_class', '')
    if cls in ITOP_CLASSES:
        where.append('ticket_class = ?')
        params.append(cls)
    st = request.args.get('status', '')
    if st == 'active':
        where.append("status NOT IN ('resolved','closed','reject')")
    elif st == 'closed':
        where.append("status IN ('resolved','closed','reject')")
    elif st:
        where.append('status = ?')
        params.append(st)
    q = (request.args.get('q') or '').strip()
    if q:
        where.append('(title LIKE ? OR ticket_ref LIKE ?)')
        params.extend(['%' + q + '%', '%' + q + '%'])
    wsql = (' WHERE ' + ' AND '.join(where)) if where else ''
    total = conn.execute('SELECT COUNT(*) as c FROM itop_tickets' + wsql, params).fetchone()['c']
    try:
        limit = min(int(request.args.get('limit', 50) or 50), 200)
    except ValueError:
        limit = 50
    try:
        offset = max(int(request.args.get('offset', 0) or 0), 0)
    except ValueError:
        offset = 0
    rows = conn.execute(
        'SELECT * FROM itop_tickets' + wsql + ' ORDER BY last_update DESC LIMIT %d OFFSET %d' % (limit, offset),
        params).fetchall()
    return jsonify({'total': total, 'items': [dict(r) for r in rows]})


@app.route('/api/itop/tickets/<cls>/<ref>')
@login_required
def itop_ticket_detail(cls, ref):
    if cls not in ITOP_CLASSES:
        return jsonify({'error': '无效的工单类型'}), 400
    conn = get_db()
    row = conn.execute('SELECT * FROM itop_tickets WHERE ticket_class = ? AND ticket_ref = ?',
                       (cls, ref)).fetchone()
    if not row:
        return jsonify({'error': '工单不存在'}), 404
    row = dict(row)
    if _itop_ticket_denied(conn, row):
        return jsonify({'error': '无权访问'}), 403
    try:
        row['raw'] = json.loads(row.get('raw_data') or '{}')
    except Exception:
        row['raw'] = {}
    return jsonify(row)


@app.route('/api/itop/tickets/<cls>/<ref>/log', methods=['POST'])
@login_required
def itop_ticket_add_log(cls, ref):
    # 工单加日志（MCP add_ticket_log 写回 iTop）
    if cls not in ITOP_CLASSES:
        return jsonify({'error': '无效的工单类型'}), 400
    if not re.match(r'^[A-Za-z0-9\-_]+$', ref):
        return jsonify({'error': '无效的工单号'}), 400
    data = request.get_json(silent=True) or {}
    message = (data.get('message') or '').strip()
    if not message:
        return jsonify({'error': '日志内容不能为空'}), 400
    conn = get_db()
    row = conn.execute('SELECT * FROM itop_tickets WHERE ticket_class = ? AND ticket_ref = ?',
                       (cls, ref)).fetchone()
    if not row:
        return jsonify({'error': '工单不存在，请先同步'}), 404
    if _itop_ticket_denied(conn, row):
        return jsonify({'error': '只能操作本人名下工单'}), 403
    try:
        key = int(row['ticket_key'] or 0)
        if not key:
            return jsonify({'error': '工单缺少 iTop key，无法写回'}), 400
        who_row = conn.execute('SELECT display_name FROM users WHERE id = ?',
                               (session['user_id'],)).fetchone()
        who = (who_row or {}).get('display_name') or ''
        client = _get_itop_client()
        res = client.call_json('add_ticket_log', {
            'ticket_class': cls, 'key': key,
            'message': ('[' + who + '] ' if who else '') + message,
            'log_field': 'private_log' if data.get('private') else 'public_log',
            'comment': 'infra-workbench v27'
        })
        _refresh_itop_ticket(cls, ref)
        return jsonify({'ok': True, 'result': res})
    except Exception as e:
        return jsonify({'error': 'iTop 写回失败：%s' % e}), 502


@app.route('/api/itop/options')
@login_required
def itop_options():
    # v29.8：流转弹窗下拉数据（团队/处理工程师/服务族/服务/子类别，10 分钟缓存）
    try:
        return jsonify(_itop_options())
    except Exception as e:
        return jsonify({'error': '获取 iTop 选项失败：%s' % e}), 502


@app.route('/api/itop/tickets/<cls>/<ref>/transitions')
@login_required
def itop_ticket_transitions(cls, ref):
    # v29.7.2：实时获取工单当前状态可用的流转动作（登录 iTop Web 抓详情页按钮，适配定制版状态机）
    if cls not in ITOP_CLASSES:
        return jsonify({'error': '无效的工单类型'}), 400
    if not re.match(r'^[A-Za-z0-9\-_]+$', ref):
        return jsonify({'error': '无效的工单号'}), 400
    conn = get_db()
    row = conn.execute('SELECT * FROM itop_tickets WHERE ticket_class = ? AND ticket_ref = ?',
                       (cls, ref)).fetchone()
    if not row:
        return jsonify({'error': '工单不存在，请先同步'}), 404
    if _itop_ticket_denied(conn, row):
        return jsonify({'error': '无权访问'}), 403
    key = int(row['ticket_key'] or 0)
    status = (row['status'] or '').strip()
    trans = _itop_web_transitions(cls, key, status) if key else None
    source = 'itop_web'
    if trans is None:
        # 兜底：静态映射（定制版实测结果）
        source = 'static'
        codes = (ITOP_STATE_STIMULI.get(cls) or {}).get(status)
        trans = [{'code': c, 'label': ITOP_STIMULUS_LABEL.get(c, c)} for c in (codes or [])]
        if codes is None and cls not in ITOP_STATE_STIMULI:
            source = 'none'  # 未映射类：不限制，交 iTop 校验
    return jsonify({'status': status, 'source': source, 'transitions': trans,
                    'resolution_codes': ITOP_RESOLUTION_CODES,
                    'pending_reasons': ITOP_PENDING_REASONS})


@app.route('/api/itop/tickets/<cls>/<ref>/stimulus', methods=['POST'])
@login_required
def itop_ticket_apply_stimulus(cls, ref):
    # 工单流转（MCP apply_stimulus 写回 iTop）
    if cls not in ITOP_CLASSES:
        return jsonify({'error': '无效的工单类型'}), 400
    if not re.match(r'^[A-Za-z0-9\-_]+$', ref):
        return jsonify({'error': '无效的工单号'}), 400
    data = request.get_json(silent=True) or {}
    stimulus = (data.get('stimulus') or '').strip()
    if not re.match(r'^ev_[a-z0-9_]+$', stimulus):
        return jsonify({'error': '无效的流转动作（须为 ev_xxx）'}), 400
    conn = get_db()
    row = conn.execute('SELECT * FROM itop_tickets WHERE ticket_class = ? AND ticket_ref = ?',
                       (cls, ref)).fetchone()
    if not row:
        return jsonify({'error': '工单不存在，请先同步'}), 404
    if _itop_ticket_denied(conn, row):
        return jsonify({'error': '只能操作本人名下工单'}), 403
    try:
        key = int(row['ticket_key'] or 0)
        if not key:
            return jsonify({'error': '工单缺少 iTop key，无法写回'}), 400
        # v29.7.2：优先从 iTop Web 实时拿当前状态可用动作预校验（定制版状态机权威来源）；
        # 抓取失败才降级静态映射，都没有则透传 iTop 校验，避免误拦。
        state = (row['status'] or '').strip()
        dyn = _itop_web_transitions(cls, key, state)
        if dyn is not None:
            codes = [t['code'] for t in dyn]
            if stimulus not in codes:
                if codes:
                    names = '、'.join(f"{t['label']}（{t['code']}）" for t in dyn)
                    return jsonify({'error': f'工单当前状态「{state}」不支持「{stimulus}」，可用动作：{names}'}), 400
                return jsonify({'error': f'工单当前状态「{state}」无可用流转动作（可先点同步刷新状态）'}), 400
        else:
            allowed = (ITOP_STATE_STIMULI.get(cls) or {}).get(state)
            if allowed is not None and stimulus not in allowed:
                if allowed:
                    names = '、'.join(f'{s}（{ITOP_STIMULUS_LABEL.get(s, s)}）' for s in allowed)
                    return jsonify({'error': f'工单当前状态「{state}」不支持「{stimulus}」，可用动作：{names}'}), 400
                return jsonify({'error': f'工单当前状态「{state}」无可用流转动作（如刚同步可先点同步刷新状态）'}), 400
        # v29.0 流转字段白名单（防误传/注入未知字段；iTop 各工单类字段不同）
        fields = data.get('fields')
        if isinstance(fields, dict):
            fields = {k: v for k, v in fields.items() if k in ITOP_STIMULUS_FIELD_WHITELIST}
        else:
            fields = {}
        client = _get_itop_client()
        comment = (data.get('comment') or 'infra-workbench v29')
        # v29.8：已知动作优先走 itop-mcp 专用工具（内置字段校验，报错更准）
        tool = ITOP_STIMULUS_TOOL.get(stimulus)
        if tool:
            targs = {'ticket_class': cls, 'key': key, 'comment': comment}
            if tool in ('assign_ticket', 'reassign_ticket'):
                for fk in ('team_id', 'agent_id'):
                    if not fields.get(fk):
                        return jsonify({'error': '该动作需要选择处理团队和处理工程师'}), 400
                    targs[fk] = int(fields[fk])
            elif tool == 'resolve_ticket':
                if not fields.get('solution'):
                    return jsonify({'error': '请填写解决方案'}), 400
                targs['solution'] = fields['solution']
                for fk in ('resolution_code', 'servicesubcategory_id', 'difficulty_level'):
                    if fields.get(fk) not in (None, '', 0):
                        targs[fk] = fields[fk]
            elif tool == 'pending_ticket':
                if not fields.get('pending_reason'):
                    return jsonify({'error': '请选择挂起原因'}), 400
                targs['pending_reason'] = fields['pending_reason']
            try:
                res = client.call_json(tool, targs)
                _refresh_itop_ticket(cls, ref)
                return jsonify({'ok': True, 'result': res})
            except Exception as e:
                msg = str(e)
                m2 = re.search(r"Missing mandatory attribute\(s\)[^:]*:\s*(.+?)\.?\s*$", msg)
                if m2:
                    need = [x.strip() for x in m2.group(1).split(',') if x.strip()]
                    # v29.8 同日补丁：默认值自动补全后降级通用 apply_stimulus 重试（专用工具参数面窄，
                    # 服务族/服务类字段直接走 fields 提交更稳）；补不齐才让前端动态补填
                    autofill, unfilled = _itop_autofill_fields(need, fields, row)
                    if not unfilled:
                        fields = dict(fields, **autofill)
                        comment = comment + '（必填字段由工作台默认值自动补全：%s）' % '、'.join(sorted(autofill))
                    else:
                        return jsonify({'ok': False, 'need_fields': need,
                                        'error': 'iTop 要求补充以下必填字段后才能执行该流转：%s' % '、'.join(need)}), 400
                # 专用工具失败（参数不符/旧版 MCP 无此工具/已补默认字段）→ 降级通用 apply_stimulus
        res, last_err = None, None
        _autofilled_once = set()  # 已自动补全过的缺失字段组合，再报同样缺失则不再重试
        for _attempt in range(3):
            args = {'iop_class': cls, 'key': key, 'stimulus': stimulus,
                    'comment': comment}
            if fields:
                args['fields'] = fields
            try:
                res = client.call_json('apply_stimulus', args)
                break
            except Exception as e:
                msg = str(e)
                # v29.0 工单类型字段差异自适应：剔除该类型不存在的字段后自动重试
                # iTop 报错格式 "difficulty_level: Unknown attribute"（字段在前），兼容反向格式
                m = (re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*Unknown attribute", msg)
                     or re.search(r"Unknown attribute\s*:\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)", msg))
                if m and m.group(1) in fields:
                    fields.pop(m.group(1))
                    last_err = msg
                    continue
                # v29.0 必填字段动态感知；v29.8 同日补丁：先用默认值自动补全重试，补不齐才返回前端补填
                m2 = re.search(r"Missing mandatory attribute\(s\)[^:]*:\s*(.+?)\.?\s*$", msg)
                if m2:
                    need = [x.strip() for x in m2.group(1).split(',') if x.strip()]
                    nkey = frozenset(need)
                    autofill, unfilled = _itop_autofill_fields(need, fields, row)
                    if not unfilled and autofill and nkey not in _autofilled_once:
                        _autofilled_once.add(nkey)
                        fields = dict(fields, **autofill)
                        comment = comment + '（必填字段由工作台默认值自动补全：%s）' % '、'.join(sorted(autofill))
                        last_err = msg
                        continue
                    return jsonify({'ok': False, 'need_fields': need,
                                    'error': 'iTop 要求补充以下必填字段后才能执行该流转：%s' % '、'.join(need)}), 400
                # v29.0 状态不匹配转友好提示
                m3 = re.search(r"Invalid stimulus: '(\w+)' on the object (\S+) in state '(\w+)'", msg)
                if m3:
                    return jsonify({'error': "工单当前状态「%s」不支持流转动作「%s」，请在 iTop 中核对可用操作"
                                    % (m3.group(3), m3.group(1))}), 400
                return jsonify({'error': 'iTop 流转失败：%s' % msg}), 502
        if res is None:
            return jsonify({'error': 'iTop 流转失败：%s' % (last_err or '未知错误')}), 502
        _refresh_itop_ticket(cls, ref)
        return jsonify({'ok': True, 'result': res})
    except Exception as e:
        return jsonify({'error': 'iTop 流转失败：%s' % e}), 502


@app.route('/api/itop/sync', methods=['POST'])
@login_required
def itop_sync_now():
    if not session.get('is_admin'):
        return jsonify({'error': '需要管理员权限'}), 403
    if _ITOP_SYNC_STATE.get('running'):
        return jsonify({'status': 'already_running', 'state': _ITOP_SYNC_STATE})
    data = request.get_json(silent=True) or {}
    mode = data.get('mode') or 'incremental'
    if mode not in ('incremental', 'full'):
        mode = 'incremental'
    threading.Thread(target=_sync_itop_tickets, args=(mode,), daemon=True).start()
    return jsonify({'status': 'started', 'mode': mode})


@app.route('/api/itop/user-map', methods=['GET'])
@login_required
def itop_user_map_list():
    if not session.get('is_admin'):
        return jsonify({'error': '需要管理员权限'}), 403
    conn = get_db()
    maps = conn.execute("""
        SELECT m.itop_agent_name, m.user_id, m.updated_at, u.display_name
        FROM itop_user_map m LEFT JOIN users u ON m.user_id = u.id
        ORDER BY m.itop_agent_name
    """).fetchall()
    # v27.1：工程师列表改为按关键字搜索（前端手动选择映射），默认不返回全量
    q = (request.args.get('q') or '').strip()
    if q:
        agents = conn.execute("""
            SELECT agent_name, COUNT(*) as c FROM itop_tickets
            WHERE agent_name != '' AND agent_name LIKE ?
            GROUP BY agent_name ORDER BY c DESC LIMIT 20
        """, ('%' + q + '%',)).fetchall()
    else:
        agents = []
    return jsonify({'mappings': [dict(r) for r in maps], 'agents': [dict(r) for r in agents]})


@app.route('/api/itop/user-map', methods=['POST'])
@login_required
def itop_user_map_set():
    if not session.get('is_admin'):
        return jsonify({'error': '需要管理员权限'}), 403
    data = request.get_json(silent=True) or {}
    name = (data.get('itop_agent_name') or '').strip()
    uid = data.get('user_id')
    if not name:
        return jsonify({'error': '缺少 itop_agent_name'}), 400
    conn = get_db()
    if uid in (None, '', 0):
        conn.execute('DELETE FROM itop_user_map WHERE itop_agent_name = ?', (name,))
    else:
        conn.execute('REPLACE INTO itop_user_map (itop_agent_name, user_id, updated_at) VALUES (?,?,?)',
                     (name, int(uid), _now_str()))
    new_uid = _itop_map_user(conn, name)
    conn.execute('UPDATE itop_tickets SET user_id = ? WHERE agent_name = ?', (new_uid, name))
    conn.commit()
    n = 0
    if new_uid:
        n = conn.execute('SELECT COUNT(*) as c FROM itop_tickets WHERE agent_name = ? AND user_id = ?',
                         (name, new_uid)).fetchone()['c']
    return jsonify({'ok': True, 'user_id': new_uid, 'remapped': n})


@app.route('/api/team/member/<int:uid>/itop')
@login_required
def team_member_itop(uid):
    if not session.get('is_admin'):
        return jsonify({'error': '需要管理员权限'}), 403
    conn = get_db()
    rows = conn.execute(
        'SELECT ticket_ref, ticket_class, title, status, priority, agent_name, start_date, '
        'resolution_date, close_date, last_update, time_spent '
        'FROM itop_tickets WHERE user_id = ? ORDER BY last_update DESC LIMIT 100',
        (uid,)).fetchall()
    return jsonify([dict(r) for r in rows])


# ====================================================================
# v28.2：模型供应商管理
# ====================================================================
@app.route('/api/model-providers')
@login_required
def list_model_providers():
    """获取所有模型供应商（所有用户可见）"""
    db = get_db()
    rows = db.execute('SELECT * FROM model_providers ORDER BY is_default DESC, id ASC').fetchall()
    providers = []
    for r in rows:
        p = dict(r)
        # 隐藏完整 api_key，只显示前4位
        if p.get('api_key') and len(p['api_key']) > 8:
            p['api_key_masked'] = p['api_key'][:4] + '****'
        else:
            p['api_key_masked'] = '(无)'
        providers.append(p)
    # 获取当前用户偏好
    uid = session.get('user_id')
    preferred_id = None
    if uid:
        u = db.execute('SELECT preferred_provider_id FROM users WHERE id = ?', (uid,)).fetchone()
        if u:
            preferred_id = u['preferred_provider_id']
    return jsonify({'providers': providers, 'preferred_id': preferred_id})


@app.route('/api/model-providers', methods=['POST'])
@admin_required
def create_model_provider():
    """创建模型供应商（管理员）"""
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    base_url = (data.get('base_url') or '').strip()
    api_key = (data.get('api_key') or '').strip()
    model = (data.get('model') or '').strip()
    if not name or not base_url or not model:
        return jsonify({'error': '名称、Base URL、模型不能为空'}), 400
    db = get_db()
    db.execute(
        'INSERT INTO model_providers (name, base_url, api_key, model, created_by) VALUES (?, ?, ?, ?, ?)',
        (name, base_url, api_key, model, session.get('user_id')))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/model-providers/<int:pid>', methods=['PUT'])
@admin_required
def update_model_provider(pid):
    """更新模型供应商（管理员）"""
    data = request.get_json(silent=True) or {}
    db = get_db()
    p = db.execute('SELECT * FROM model_providers WHERE id = ?', (pid,)).fetchone()
    if not p:
        return jsonify({'error': '供应商不存在'}), 404
    name = (data.get('name') or p['name']).strip()
    base_url = (data.get('base_url') or p['base_url']).strip()
    model = (data.get('model') or p['model']).strip()
    api_key = data.get('api_key')  # None = 不修改
    if api_key is None:
        api_key = p['api_key']
    db.execute(
        'UPDATE model_providers SET name=?, base_url=?, api_key=?, model=? WHERE id=?',
        (name, base_url, api_key, model, pid))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/model-providers/<int:pid>', methods=['DELETE'])
@admin_required
def delete_model_provider(pid):
    """删除模型供应商（管理员）"""
    db = get_db()
    p = db.execute('SELECT * FROM model_providers WHERE id = ?', (pid,)).fetchone()
    if not p:
        return jsonify({'error': '供应商不存在'}), 404
    if p['is_default']:
        return jsonify({'error': '默认供应商不能删除'}), 400
    # 清除引用该供应商的用户偏好
    db.execute('UPDATE users SET preferred_provider_id = NULL WHERE preferred_provider_id = ?', (pid,))
    db.execute('DELETE FROM model_providers WHERE id = ?', (pid,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/model-providers/preference', methods=['PUT'])
@login_required
def set_model_provider_preference():
    """设置当前用户偏好的模型供应商"""
    data = request.get_json(silent=True) or {}
    provider_id = data.get('provider_id')  # None = 使用默认
    db = get_db()
    if provider_id is not None:
        p = db.execute('SELECT id FROM model_providers WHERE id = ?', (provider_id,)).fetchone()
        if not p:
            return jsonify({'error': '供应商不存在'}), 404
    db.execute('UPDATE users SET preferred_provider_id = ? WHERE id = ?',
               (provider_id, session['user_id']))
    db.commit()
    return jsonify({'ok': True})


# ====================================================================
# 初始化
# ====================================================================
init_db()
_init_itop_tables()
_start_scheduler()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
