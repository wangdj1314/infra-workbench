#!/usr/bin/env python3
"""批量添加基础架构团队成员（幂等：已存在则更新）"""
import sqlite3

conn = sqlite3.connect('/app/data/workbench.db')
conn.row_factory = sqlite3.Row

# 1) 确保新列存在（旧表升级）
cols = [r['name'] for r in conn.execute('PRAGMA table_info(users)')]
for col in ['employee_id', 'email']:
    if col not in cols:
        conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT ''")
        print(f'ADD COLUMN {col}')

# 2) 成员数据：AD账号 / 姓名 / 工号 / 邮箱 / 职位(板块) / 是否管理员
members = [
    ('zhangjz', '张津泽',  'R-68917', 'zhangjz@risen.com',  '基础架构工程师',     0),
    ('wujj2',   '伍俊杰',  'R-70434', 'wujj2@risen.com',    '基础架构工程师',     0),
    ('zhongjy', '钟建宇',  'R-69807', 'zhongjy@risen.com',  '网络高级工程师',     0),
    ('xums',    '许明胜',  'R-69341', 'xums@risen.com',     '网络高级工程师',     0),
    ('wangdj',  '汪德嘉',  'R-69034', 'wangdj@risen.com',   '副经理',             1),
    ('liupc',   '刘鹏程',  'R-72789', 'liupc@risen.com',    '基础架构工程师',     0),
    ('wangtc',  '王腾川',  'R-69142', 'wangtc@risen.com',   '基础架构高级工程师', 0),
]

for ad, name, emp, email, section, admin in members:
    exists = conn.execute('SELECT id FROM users WHERE ad_username = ?', (ad,)).fetchone()
    if exists:
        conn.execute(
            'UPDATE users SET display_name=?, section_name=?, employee_id=?, email=?, is_admin=? WHERE ad_username=?',
            (name, section, emp, email, admin, ad))
        print(f'UPDATE {ad} (已存在，更新资料)')
    else:
        conn.execute(
            'INSERT INTO users (ad_username, display_name, section_name, employee_id, email, is_admin) VALUES (?,?,?,?,?,?)',
            (ad, name, section, emp, email, admin))
        print(f'INSERT {ad}')

conn.commit()

print('=== final users ===')
for r in conn.execute('SELECT id, ad_username, display_name, section_name, employee_id, email, is_admin FROM users ORDER BY id'):
    print(dict(r))
conn.close()
