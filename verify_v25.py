# -*- coding: utf-8 -*-
"""v25 验证脚本（在 177 容器内运行）：
1. 校验框架含「周期性自动触发机制」章节（四级触发 + 手动兜底）
2. AI 生成 WorkBuddy 版部署文档，输出到 /tmp/MCP-部署文档-WorkBuddy-v25.md
"""
import json
import subprocess
import sys

PY = sys.executable


def run(code):
    r = subprocess.run([PY, '-c', code], capture_output=True, text=True, cwd='/app')
    return r.stdout.strip() or r.stderr.strip()


# 1. 框架章节完整性
code1 = '''
import app
fw = app.MCP_DEPLOY_DOC_FRAMEWORK
checks = {
    "周期性自动触发机制": "五、周期性自动触发机制" in fw,
    "触发级别1-会话开始": "触发级别 1：会话开始" in fw,
    "触发级别2-工作单元完成": "触发级别 2：工作单元完成" in fw,
    "触发级别3-每日定时归档": "触发级别 3：每日定时归档" in fw,
    "触发级别4-周度复盘": "触发级别 4：周度复盘" in fw,
    "兜底手动触发": "兜底：手动触发" in fw,
    "可直接复制指令": "可直接复制为定时任务指令" in fw,
    "章节八注意事项": "## 八、注意事项" in fw,
    "章节七使用示例": "## 七、使用示例" in fw,
    "章节六字段规范": "## 六、字段规范" in fw,
}
bad = [k for k, v in checks.items() if not v]
print("CHECK:", "ALL_OK" if not bad else "MISSING " + ",".join(bad))
print("FRAMEWORK_LEN:", len(fw))
'''
print('=== 1. v25 框架章节校验 ===')
print(run(code1))

# 2. AI 生成 WorkBuddy 版部署文档并落盘
code2 = '''
import app, os
tool, user, prefix = "WorkBuddy", "王东杰", "bmUiuji5"
sse = "http://10.10.9.177:9080/mcp/sse"
system = (
    "你是企业数字化工作台的部署文档生成专家。你负责基于给定的【文档框架】生成一份完整的、可直接使用的"
    "AI 工具接入部署文档（Markdown 格式，中文）。要求：\\n"
    "1. 严格遵守框架的章节结构与核心内容，不得删除或弱化「内容判定规则」「工作流约定」「周期性自动触发机制」章节；\\n"
    "2. 将框架中的占位符（{tool}、{user}、{sse_url}、{prefix}）替换为真实值；\\n"
    "3. 可以补充 2-3 个贴合该用户岗位的「使用示例」，示例要具体、真实感强；\\n"
    "4. 语言精炼专业，直接输出 Markdown 全文，不要输出多余解释。"
).format(tool=tool, user=user, sse_url=sse, prefix=prefix)
user_msg = (
    f"请生成部署文档。\\n- 目标AI工具：{tool}\\n- 绑定用户：{user}\\n- SSE端点：{sse}\\n- Token前缀：{prefix}****\\n"
    f"- 用户补充：无\\n\\n以下是文档框架（请按此框架生成完整文档）：\\n\\n{app.MCP_DEPLOY_DOC_FRAMEWORK}"
)
try:
    doc = app.ai_chat(system, user_msg, max_tokens=4000, feature="mcp_doc")
    if not doc or len(doc.strip()) < 200:
        raise ValueError("AI 输出过短")
    with open("/tmp/MCP-部署文档-WorkBuddy-v25.md", "w", encoding="utf-8") as f:
        f.write(doc)
    has_sched = "周期性自动触发机制" in doc and "每日定时" in doc
    print("AI_OK len=", len(doc), "HAS_SCHED=", has_sched)
    print(doc[:500])
    print("...\\n[截断]")
except Exception as e:
    print("AI_FAIL:", e)
    doc = app._mcp_deploy_doc_fallback(tool, user, sse, prefix)
    with open("/tmp/MCP-部署文档-WorkBuddy-v25.md", "w", encoding="utf-8") as f:
        f.write(doc)
    print("FALLBACK len=", len(doc), "HAS_SCHED=", ("周期性自动触发机制" in doc and "每日定时" in doc))
'''
print('\n=== 2. AI 生成 WorkBuddy 版 M 文件 ===')
print(run(code2))
