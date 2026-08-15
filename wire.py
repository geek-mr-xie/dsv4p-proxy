"""wire.py — 请求整形：精简 system/对齐工具面构造、引导注入

system 全程保持精简（仅 tools 在会话启动阶段裁剪）。
"""
from __future__ import annotations

import os

SPEC_PERSONA = "You are a helpful software engineer assistant."

# 与官方 harness 一致的 bash 工具描述（行为约束：持久状态、无网络、大输出规避）
DSH_BASH_DESC = (
    "Run commands in a bash shell\n"
    '* When invoking this tool, the contents of the "command" parameter does NOT need to be XML-escaped.\n'
    "* You don't have access to the internet via this tool.\n"
    "* You do have access to a mirror of common linux and python packages via apt and pip.\n"
    "* State is persistent across command calls and discussions with the user.\n"
    "* To inspect a particular line range of a file, e.g. lines 10-25, try 'sed -n 10,25p /path/to/the/file'.\n"
    "* Please avoid commands that may produce a very large amount of output.\n"
    "* Please run long lived commands in the background, e.g. 'sleep 10 &' or start a server in the background."
)

# 工具优先引导（会话启动阶段注入最后一条 user 消息）
GUIDE_TOOLS_FIRST = (
    "\n\nInspect the repository before answering. Determine its structure first, "
    "then use the available tools before guessing. Use the available tools first."
)

DSH_EDITOR_TOOL = {
    "type": "function",
    "name": "str_replace_editor",
    "description": (
        "Create, view, and edit files. The `path` parameter must be an absolute path; "
        "commands are view, create, str_replace, insert."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["view", "create", "str_replace", "insert"],
                "description": "The commands to run. Allowed options are: `view`, `create`, `str_replace`, `insert`.",
            },
            "path": {"type": "string", "description": "Absolute path to file or directory."},
            "file_text": {"type": "string", "description": "Required parameter of `create` command."},
            "old_str": {"type": "string", "description": "Required parameter of `str_replace` command."},
            "new_str": {"type": "string", "description": "Required parameter of `str_replace` command."},
            "insert_line": {"type": "integer", "description": "Required parameter of `insert` command."},
            "view_range": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Optional [start, end] line range for `view`.",
            },
        },
        "required": ["command", "path"],
    },
}

DSH_BASH_TOOL = {
    "type": "function",
    "name": "bash",
    "description": DSH_BASH_DESC,
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The bash command to run"},
            "description": {"type": "string", "description": "A short description of the command"},
        },
        "required": ["command", "description"],
    },
}


def build_pseudo_tools() -> list[dict]:
    return [DSH_BASH_TOOL, DSH_EDITOR_TOOL]


def build_pseudo_system() -> str:
    """精简 system（恒定文本，缓存友好）。"""
    return SPEC_PERSONA


# ── body 级变换（chat + responses 双协议）──

def replace_system(body: dict, system_text: str, proto: str) -> dict:
    if proto == "chat":
        msgs = body.get("messages") or []
        if msgs and msgs[0].get("role") == "system":
            msgs = list(msgs)
            msgs[0] = {**msgs[0], "content": system_text}
            body = {**body, "messages": msgs}
        else:
            body = {**body, "messages": [{"role": "system", "content": system_text}, *msgs]}
    else:  # responses
        body = {**body, "instructions": system_text}
    return body


def replace_tools(body: dict, tools: list[dict], proto: str) -> dict:
    """tools 内部表示是扁平格式（responses 风格）；chat 协议需要 function 嵌套。"""
    if proto == "chat":
        chat_tools = [
            {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools
        ]
        body = {**body, "tools": chat_tools}
    else:
        body = {**body, "tools": tools}
    return body


def _append_text(content, guide: str):
    """content 可能是 str 或 part 数组；返回追加后的 content。"""
    if isinstance(content, str):
        return content + guide
    if isinstance(content, list):
        return list(content) + [{"type": "text", "text": guide}]
    return content


def inject_guide(body: dict, proto: str, guide: str = GUIDE_TOOLS_FIRST) -> dict:
    """把工具优先引导追加到最后一条真实 user 消息（会话启动阶段每轮注入）。"""
    if proto == "chat":
        msgs = body.get("messages") or []
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "user":
                msgs = list(msgs)
                msgs[i] = {**msgs[i], "content": _append_text(msgs[i].get("content", ""), guide)}
                return {**body, "messages": msgs}
    else:  # responses: input 数组最后 user item
        inp = list(body.get("input") or [])
        for i in range(len(inp) - 1, -1, -1):
            item = inp[i]
            if isinstance(item, dict) and item.get("role") == "user":
                item = {**item, "content": _append_text(item.get("content", ""), guide)}
                inp[i] = item
                return {**body, "input": inp}
    return body
