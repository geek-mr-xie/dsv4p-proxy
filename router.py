"""router.py — 无状态会话阶段判定 + 任务分类（预留）"""
from __future__ import annotations


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


# ── v2 预留：任务分类（抄 router-core 正则，暂未启用）──

_REACT_RE = r"/(开发|创建|写一个|生成|从零|构建|新项目|实现|搭建|build|create|develop|generate|implement|make a|new project)/i"
_SPEC_RE = r"/(修复|调试|重构|维护|排查|报错|崩溃|优化|review|fix|debug|refactor|maintain|repair|broken)/i"


def classify_task(text: str) -> str:
    """返回 'react' | 'spec' | 'weak'（v2 路由用，v0.1 未启用）"""
    import re
    react = len(re.findall(_REACT_RE[1:-3], text, re.I))
    spec = len(re.findall(_SPEC_RE[1:-3], text, re.I))
    if react > spec:
        return "react"
    if spec > react:
        return "spec"
    return "weak"
