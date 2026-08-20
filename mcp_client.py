#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v27.0 MCP HTTP 客户端（Streamable HTTP transport）
供工作台后端调用外部 MCP 服务（首个接入技能：iTop 工单）。

协议要点：
- initialize → 响应头返回 mcp-session-id
- 后续请求带 mcp-session-id 头
- 响应体为 SSE 格式（event: message / data: {...}）或纯 JSON
- 会话失效（-32000）时自动重建重试一次
"""
import json
import re
import threading

import requests


class MCPHTTPClient:
    def __init__(self, url, timeout=30, client_name='infra-workbench', client_version='27.0'):
        self.url = url
        self.timeout = timeout
        self.client_name = client_name
        self.client_version = client_version
        self._session_id = None
        self._lock = threading.Lock()
        self._next_id = 100

    # ---- 底层 POST ----
    def _post(self, payload, with_session=True):
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream',
        }
        if with_session and self._session_id:
            headers['mcp-session-id'] = self._session_id
        resp = requests.post(self.url, data=json.dumps(payload).encode('utf-8'),
                             headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        # SSE 无 charset 时 requests 默认 latin-1，会把 UTF-8 中文解出 \x85(NEL) 破坏 JSON
        if not resp.encoding or resp.encoding.lower() in ('iso-8859-1', 'ascii'):
            resp.encoding = 'utf-8'
        sid = resp.headers.get('mcp-session-id')
        if sid:
            self._session_id = sid
        return self._parse_body(resp.text)

    @staticmethod
    def _parse_body(body):
        """SSE data: 行优先；否则按纯 JSON 解析。只用 \r\n/\r/\n 切行，
        避免 str.splitlines 把 \x85(NEL)/\u2028 等 Unicode 行边界误当换行。"""
        lines = re.split(r'\r\n|\r|\n', body)
        data_lines = [ln[5:].strip() for ln in lines if ln.startswith('data:')]
        for d in reversed(data_lines):
            if d:
                try:
                    return json.loads(d)
                except ValueError:
                    continue
        try:
            return json.loads(body)
        except ValueError:
            return None

    # ---- 会话管理 ----
    def _ensure_init(self):
        if self._session_id:
            return
        result = self._post({
            'jsonrpc': '2.0', 'id': self._next_id, 'method': 'initialize',
            'params': {
                'protocolVersion': '2025-03-26',
                'capabilities': {},
                'clientInfo': {'name': self.client_name, 'version': self.client_version},
            },
        }, with_session=False)
        self._next_id += 1
        if not result or 'result' not in result:
            self._session_id = None
            raise RuntimeError(f'MCP initialize 失败: {str(result)[:200]}')
        self._post({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def reset(self):
        with self._lock:
            self._session_id = None

    # ---- 工具调用 ----
    def call_tool(self, name, arguments):
        """调用 tools/call，返回 (text, is_error)。会话失效自动重试一次。"""
        for attempt in (1, 2):
            with self._lock:
                self._ensure_init()
                result = self._post({
                    'jsonrpc': '2.0', 'id': self._next_id, 'method': 'tools/call',
                    'params': {'name': name, 'arguments': arguments},
                })
                self._next_id += 1
            if result is None:
                if attempt == 1:
                    self.reset()
                    continue
                raise RuntimeError(f'MCP {name} 无响应')
            err = result.get('error')
            if err:
                # 会话失效 → 重置后重试一次
                if attempt == 1 and err.get('code') == -32000:
                    self.reset()
                    continue
                raise RuntimeError(f'MCP {name} 错误: {err.get("message", err)}')
            payload = result.get('result') or {}
            text = ''.join(c.get('text', '') for c in payload.get('content', [])
                           if c.get('type') == 'text')
            return text, bool(payload.get('isError'))
        raise RuntimeError(f'MCP {name} 调用失败')

    def call_json(self, name, arguments):
        """调用并解析 JSON 文本结果；isError=True 时抛异常。"""
        text, is_err = self.call_tool(name, arguments)
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            data = None
        if is_err:
            raise RuntimeError(f'MCP {name} 返回错误: {str(text)[:300]}')
        return data
