"""router.py — 无状态会话阶段判定 + 任务分类路由（spec/react/weak，吸收自 dsh-router-standard v0.1.0）"""
from __future__ import annotations

from typing import Any


# ── 锚定判定 ──────────────────────────────────────────────────────────

def _chat_has_tool_call(messages: list[dict]) -> bool:
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            return True
    return False


def _responses_has_tool_call(input_: list) -> bool:
    for item in input_:
        if not isinstance(item, dict):
            continue
        # responses 格式：assistant 消息的 output 数组含 function_call
        if item.get("type") == "function_call":
            return True
        for out in item.get("output", []) or []:
            if isinstance(out, dict) and out.get("type") == "function_call":
                return True
    return False


def is_anchored(body: dict) -> bool:
    """无状态判定：请求历史含 assistant tool_call → 已进入执行阶段。

    chat/completions: body['messages']
    responses:        body['input']
    """
    msgs = body.get("messages")
    if msgs is not None:
        return _chat_has_tool_call(msgs)
    inp = body.get("input")
    if inp is not None:
        return _responses_has_tool_call(inp)
    return False


# ── 任务分类路由（吸收自 dsh-router-standard v0.1.0 的 router-core.mjs）──
# 三个实测行为带：spec（计划-集体）/ transition（陷阱，不自动选）/ react（执行者）；
# 平局或关键词无证据 → 'weak'（模型自路由）。

_REACT_RE = r"(?:开发|创建|写一个|生成|从零|做一个|游戏|网页|网站|构建|新项目|搭建|实现|做出|上线|落地|脚本|工具|应用|build|create|develop|generate|implement|make a|new project)"
_SPEC_RE = r"(?:修复|修一下|调试|重构|维护|排查|报错|出错|崩溃|优化|审查|为什么|异常|故障|迁移|升级|兼容|review|fix|debug|refactor|maintain|repair|broken|break)"

_COMPLEX_RE = r"(?:重构|架构|全面|详细|设计|系统|优化|分析|survey|overview|architecture|refactor|comprehensive|detailed|design|system|optimize|analyze)"


def _text_from(data: Any) -> str:
    """user 消息 content → 纯文本（str 或 part 数组）。"""
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return " ".join(c if isinstance(c, str) else (c.get("text", "") if isinstance(c, dict) else "")
                        for c in data)
    return ""


def classify_task(text: str) -> str:
    """返回 'react' | 'spec' | 'weak'。中英关键词计数：react>spec→react，
    spec>react→spec，平局/无证据→weak（模型自路由）。"""
    import re
    text = text or ""
    react = len(re.findall(_REACT_RE, text, re.I))
    spec = len(re.findall(_SPEC_RE, text, re.I))
    if react > spec:
        return "react"
    if spec > react:
        return "spec"
    return "weak"


def is_complex_task(text: str) -> bool:
    """复杂度启发：>120 字符或架构措辞 → 复杂（深度引导用）。"""
    import re
    return isinstance(text, str) and (len(text) > 120 or bool(re.search(_COMPLEX_RE, text, re.I)))


def session_mode(body: dict) -> str:
    """从请求历史第一条 user 消息分类（对应开源版 sessionMode：从 durable
    事件取首条 user/message）。会话首条不变 → mode 全程固定，无状态且 resume-safe。"""
    msgs = body.get("messages")
    if msgs is not None:
        for m in msgs:
            if isinstance(m, dict) and m.get("role") == "user":
                return classify_task(_text_from(m.get("content", "")))
        return "weak"
    inp = body.get("input")
    if inp is not None:
        for item in inp:
            if isinstance(item, dict) and item.get("role") == "user":
                return classify_task(_text_from(item.get("content", "")))
        return "weak"
    return "weak"


def last_user_text(body: dict) -> str:
    """最后一条真实 user 消息的纯文本（引导注入定位 + 复杂度判断用）。"""
    msgs = body.get("messages")
    if msgs is not None:
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role") == "user":
                return _text_from(m.get("content", ""))
        return ""
    inp = body.get("input")
    if inp is not None:
        for item in reversed(inp):
            if isinstance(item, dict) and item.get("role") == "user":
                return _text_from(item.get("content", ""))
    return ""
