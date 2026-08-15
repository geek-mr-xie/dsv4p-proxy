"""wire.py — 请求整形：精简 system/对齐工具面构造、模式 persona、分档引导注入

system 全程保持精简（仅 tools 在会话启动阶段裁剪）。
v0.3：吸收 dsh-router-standard v0.1.0 的注入手段——按任务模式（spec/react/weak）
选 persona + 分档引导（react=produce-verify-fix；weak=近场引导，复杂度自适应）。

锚定安全边界：行为指令默认走 user 通道（guide），system 只换身份句；
完整 band persona 由 persona_full_band 开关显式启用。
"""
from __future__ import annotations

import os

# ── persona 常量（router-standard router-core.mjs 原文）──

SPEC_PERSONA = "You are a helpful software engineer assistant."

MIXED_PERSONA = (
    "You are a helpful software engineer assistant.\n"
    "Work directly: prefer writing or editing code over describing plans. "
    "Verify your changes by reading and running them."
)

REACT_PERSONA = (
    "You are a hands-on software engineer who delivers working output fast.\n"
    "Work directly: write or edit code, then verify it by reading and running. "
    "Keep the loop tight — produce, verify, fix — and do not build test "
    "harnesses, scaffolding, or ceremony the user did not ask for. "
    "Finish with a usable deliverable and a short summary."
)

# weak（内部路由）persona：Pro 最优 = spec 句 + classify 指令（P11 w6, +5.00）。
# Flash 的 w7（neutral + anchors）不移植——本地只整形 Pro（Flash 纯透传）。
WEAK_PRO = (
    "You are a helpful software engineer assistant.\n"
    "Before acting, decide the task type (build or fix) and adopt the matching "
    "style: build → hands-on production; fix → inspect-and-plan."
)

# 完整 band persona（persona_full_band=True 时用；行为指令进 system）
BAND_PERSONAS = {
    "spec": SPEC_PERSONA,
    "react": REACT_PERSONA,
    "weak": WEAK_PRO,
    "mixed": MIXED_PERSONA,  # transition 陷阱带——不自动选，仅显式配置
}
# 默认：只换身份句（精简底线）。react 身份句取 REACT_PERSONA 首句。
IDENTITY_PERSONAS = {
    "spec": SPEC_PERSONA,
    "react": "You are a hands-on software engineer who delivers working output fast.",
    "weak": WEAK_PRO.split("\n")[0],
    "mixed": SPEC_PERSONA,
}

# ── 引导（user 通道，实测锚定无损）──

# spec：inspect-first
GUIDE_SPEC = (
    "\n\nInspect the repository before answering. Determine its structure first, "
    "then use the available tools before guessing. Use the available tools first."
)

# react：produce-verify-fix（REACT_PERSONA 行为段，从 system 挪到 user 通道）
GUIDE_REACT = (
    "\n\nWork directly: write or edit code, then verify it by reading and running. "
    "Keep the loop tight — produce, verify, fix — and do not build test harnesses, "
    "scaffolding, or ceremony the user did not ask for. "
    "Finish with a usable deliverable and a short summary."
)

# weak 近场引导（router-bootstrap.mjs GUIDE_WEAK / GUIDE_DEEP 原文）
GUIDE_WEAK = (
    "\nRouter: classify this task (build or fix) now, then adopt the matching style "
    "— build: direct production; fix: inspect-first. Think deeply first, then commit and act."
)

GUIDE_DEEP = (
    "\nRouter: classify this task (build or fix) now, then adopt the matching style "
    "— build: direct production; fix: inspect-first. Think deeply about the architecture, "
    "edge cases, and integration points. Do not spend reasoning on the environment or "
    "tooling. Produce when your information is complete. End each reasoning block with "
    "a decision or an information need."
)

GUIDE_TOOLS_FIRST = GUIDE_SPEC  # 兼容旧引用

# we-steer：思维链轨迹矫正。触发条件：响应 reasoning 中 "We need" 计数
# < "Let me" 计数（滑向个体执行轨迹）→ 下一轮启动阶段请求注入。
WE_STEER_TEXT = "\n\nWe DeepSeek: We think IN English, start 'we need'."


def guide_for(mode: str, text: str, deep_guide: bool = True) -> str:
    """按模式选引导。weak 按复杂度分流（>120 字符或架构词 → 深度引导）。"""
    import router
    if mode == "weak":
        return GUIDE_DEEP if (deep_guide and router.is_complex_task(text)) else GUIDE_WEAK
    if mode == "react":
        return GUIDE_REACT
    return GUIDE_SPEC


# ── 与官方 harness 一致的 bash 工具描述（行为约束：持久状态、无网络、大输出规避）──

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


def build_pseudo_system(rules_text: str = "", mode: str = "spec", full_band: bool = False) -> str:
    """精简 system：按模式选 persona。默认 identity 句；full_band=True 用完整 band persona。"""
    persona = BAND_PERSONAS.get(mode, SPEC_PERSONA) if full_band else IDENTITY_PERSONAS.get(mode, SPEC_PERSONA)
    if rules_text and rules_text.strip():
        return persona + "\n\n" + rules_text.strip()
    return persona


def load_rules(path: str | None) -> str:
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


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


def inject_guide(body: dict, proto: str, guide: str = GUIDE_SPEC) -> dict:
    """把模式引导追加到最后一条真实 user 消息（启动阶段每轮注入）。"""
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
