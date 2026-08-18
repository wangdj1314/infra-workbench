"""
SQLite → MySQL 兼容层
为 Flask 应用提供 sqlite3 风格的接口，底层使用 pymysql。
- ? 占位符自动转 %s
- || 字符串拼接通过 PIPES_AS_CONCAT 兼容
- DictCursor 返回 dict-like 行对象
- lastrowid / fetchone / fetchall / executemany 全兼容
"""
import pymysql
import pymysql.cursors

# ---- 异常别名 ----
IntegrityError = pymysql.err.IntegrityError
OperationalError = pymysql.err.OperationalError
DataError = pymysql.err.DataError
ProgrammingError = pymysql.err.ProgrammingError

# Row 类型别名 —— pymysql DictCursor 返回 dict 子类
Row = dict

import datetime as _dt

def _norm_val(v):
    """v26.4：MySQL 对 DATE/DATETIME/TIMESTAMP 列返回 date/datetime 对象，
    SQLite（TEXT）时代的应用代码按字符串切片/比较，直接返回会崩溃。
    统一在行出口规整为字符串，行为与 SQLite 完全一致。"""
    if isinstance(v, _dt.datetime):
        return v.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(v, _dt.date):
        return v.strftime('%Y-%m-%d')
    if isinstance(v, _dt.timedelta):
        return str(v)
    return v

# ---- MySQL 连接配置（环境变量覆盖） ----
_MYSQL_HOST = None
_MYSQL_PORT = 3306
_MYSQL_USER = 'root'
_MYSQL_PASS = ''
_MYSQL_DB = 'workbench'


def configure(host=None, port=3306, user='root', password='', database='workbench'):
    global _MYSQL_HOST, _MYSQL_PORT, _MYSQL_USER, _MYSQL_PASS, _MYSQL_DB
    _MYSQL_HOST = host
    _MYSQL_PORT = port
    _MYSQL_USER = user
    _MYSQL_PASS = password
    _MYSQL_DB = database


def _replace_qmarks(sql):
    """
    将 SQL 中的 ? 占位符替换为 %s，同时转义字面量中的 % 为 %%。
    智能识别单/双引号内的字符串字面量，不替换其中的 ?。
    """
    parts = []
    in_sq = False
    in_dq = False
    current = []
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_dq:
            in_sq = not in_sq
            current.append(ch)
        elif ch == '"' and not in_sq:
            in_dq = not in_dq
            current.append(ch)
        elif ch == '?' and not in_sq and not in_dq:
            parts.append(''.join(current))
            current = []
        else:
            current.append(ch)
        i += 1

    if not current and not parts:
        return sql, ()

    parts.append(''.join(current))

    if len(parts) == 1:
        # 没有 ? 占位符，仍需转义字面量 %
        return parts[0].replace('%', '%%'), ()

    # 非占位符部分转义 %，占位符部分保持 %s
    result_parts = []
    for idx, part in enumerate(parts):
        result_parts.append(part.replace('%', '%%'))
        if idx < len(parts) - 1:
            result_parts.append('%s')
    return ''.join(result_parts), None


class _Cursor:
    """包装 pymysql DictCursor，提供 sqlite3 cursor 兼容接口。"""

    def __init__(self, cursor):
        self._cur = cursor

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount

    def execute(self, sql, params=None):
        new_sql, _ = _replace_qmarks(sql)
        if params:
            self._cur.execute(new_sql, params)
        else:
            self._cur.execute(new_sql)
        return self

    def executemany(self, sql, params_list):
        new_sql, _ = _replace_qmarks(sql)
        self._cur.executemany(new_sql, params_list)
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        return {k: _norm_val(v) for k, v in dict(row).items()}

    def fetchall(self):
        return [{k: _norm_val(v) for k, v in dict(r).items()} for r in self._cur.fetchall()]

    def close(self):
        self._cur.close()

    def __iter__(self):
        for row in self._cur:
            yield {k: _norm_val(v) for k, v in dict(row).items()}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Connection:
    """包装 pymysql Connection，提供 sqlite3 Connection 兼容接口。"""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        cur = self._conn.cursor(pymysql.cursors.DictCursor)
        new_sql, _ = _replace_qmarks(sql)
        if params:
            cur.execute(new_sql, params)
        else:
            cur.execute(new_sql)
        return _Cursor(cur)

    def executemany(self, sql, params_list):
        cur = self._conn.cursor(pymysql.cursors.DictCursor)
        new_sql, _ = _replace_qmarks(sql)
        cur.executemany(new_sql, params_list)
        return _Cursor(cur)

    def executescript(self, sql):
        """执行多条 SQL（按 ; 分割）。"""
        cur = self._conn.cursor(pymysql.cursors.DictCursor)
        for stmt in sql.split(';'):
            stmt = stmt.strip()
            if stmt:
                new_sql, _ = _replace_qmarks(stmt)
                cur.execute(new_sql)
        return _Cursor(cur)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass  # v26.4：幂等关闭，避免重复 close 在 teardown 抛错

    def cursor(self):
        return _Cursor(self._conn.cursor(pymysql.cursors.DictCursor))

    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, value):
        pass  # DictCursor 已返回 dict，无需设置

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def connect(db_path=None, timeout=None, **kwargs):
    """创建 MySQL 连接，返回 sqlite3 兼容的 Connection 对象。"""
    conn = pymysql.connect(
        host=_MYSQL_HOST,
        port=_MYSQL_PORT,
        user=_MYSQL_USER,
        password=_MYSQL_PASS,
        database=_MYSQL_DB,
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=timeout or 10,
        # 关键：让 || 作为字符串拼接（兼容 SQLite 的 || 运算符）
        sql_mode='PIPES_AS_CONCAT,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION',
        autocommit=False,
    )
    return Connection(conn)
