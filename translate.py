"""translate.py — 响应侧工具调用翻译（chat SSE + responses SSE）

策略：
- 文本 / reasoning delta 即时透传（流式体验不延迟）
- tool_calls 缓冲到完成边界（chat: finish_reason=="tool_calls"；responses:
  output_item.done），然后整体重写 name + arguments 一次性吐出
- bash → terminal（arguments 只留 command）
- str_replace_editor.{view,create,str_replace} → read_file/write_file/patch
"""
from __future__ import annotations

import json


def transform_tool_call(name: str, args_str: str) -> tuple[str, str]:
    """返回 (新名字, 新 arguments JSON 字符串)。无法翻译时返回原样。"""
    try:
        args = json.loads(args_str) if args_str.strip() else {}
    except json.JSONDecodeError:
        return name, args_str  # 不完整/损坏：原样透传（调用方应只在完整参数时调用）

    if name == "bash":
        command = args.get("command")
        if isinstance(command, str):
            return "terminal", json.dumps({"command": command})
        return name, args_str

    if name == "str_replace_editor":
        cmd = args.get("command")
        path = args.get("path")
        if not isinstance(path, str):
            return name, args_str
        if cmd == "view":
            new_args = {"path": path}
            vr = args.get("view_range")
            if isinstance(vr, list) and len(vr) == 2 and all(isinstance(x, int) for x in vr):
                new_args["offset"] = max(1, vr[0])
                new_args["limit"] = vr[1] - vr[0] + 1
            return "read_file", json.dumps(new_args)
        if cmd == "create":
            return "write_file", json.dumps({"path": path, "content": args.get("file_text", "")})
        if cmd == "str_replace":
            return "patch", json.dumps({
                "mode": "replace",
                "path": path,
                "old_string": args.get("old_str", ""),
                "new_string": args.get("new_str", ""),
            })
        return name, args_str
    return name, args_str


def _extract_tool_calls(delta: dict) -> list[dict]:
    """chat delta.tool_calls → [{index, id, name, arguments}]"""
    out = []
    for tc in delta.get("tool_calls") or []:
        fn = tc.get("function") or {}
        out.append({
            "index": tc.get("index", 0),
            "id": tc.get("id", ""),
            "name": fn.get("name"),
            "arguments": fn.get("arguments", ""),
        })
    return out


class ChatTranslator:
    """逐行处理 chat/completions SSE 流。feed(line) → 变换后的行（可能多条）。"""

    def __init__(self):
        self._buf: dict[int, dict] = {}  # index -> {name, args, id}
        self._started: dict[int, bool] = {}

    def feed(self, line: str) -> list[str]:
        if not line.startswith("data:"):
            return [line]
        payload = line[5:].strip()
        if payload == "[DONE]":
            return self._flush() + [line]
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            return [line]
        choices = ev.get("choices") or []
        if not choices:
            return [line]
        ch = choices[0]
        delta = ch.get("delta") or {}
        finish = ch.get("finish_reason")
        tcs = _extract_tool_calls(delta)
        if not tcs:
            if finish == "tool_calls":
                # 保留原行：DeepSeek 把 finish_reason + usage 放在同一行，丢弃会丢 usage
                return self._flush() + [line]
            return [line]
        # 缓冲工具调用 delta，不立即透传
        for tc in tcs:
            idx = tc["index"]
            entry = self._buf.setdefault(idx, {"name": "", "args": "", "id": ""})
            if tc["name"]:
                entry["name"] = tc["name"]
            if tc["arguments"]:
                entry["args"] += tc["arguments"]
            if tc.get("id"):
                entry["id"] = tc["id"]
            self._started[idx] = True
        if finish == "tool_calls":
            return self._flush() + [line]
        return []

    def _flush(self) -> list[str]:
        lines = []
        for idx in sorted(self._buf):
            entry = self._buf[idx]
            name, args = transform_tool_call(entry["name"], entry["args"])
            lines.append(f'data: {json.dumps({"choices": [{"delta": {"tool_calls": [{"index": idx, "id": entry["id"], "type": "function", "function": {"name": name, "arguments": args}}]}}]})}\n')
            lines.append("\n")  # 真正的空行结束事件（SSE 规范；"data: " 空行会被 SDK 并入事件数据）
        self._buf.clear()
        self._started.clear()
        return lines


class ResponsesTranslator:
    """逐行处理 responses SSE 流。feed(line) → 变换后的行。"""

    def __init__(self):
        self._pending: dict[str, dict] = {}  # call_id -> {name, args}

    def feed(self, line: str) -> list[str]:
        if not line.startswith("data:"):
            return [line]
        payload = line[5:].strip()
        if payload == "[DONE]":
            return [line]
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            return [line]
        t = ev.get("type", "")
        if t == "response.output_item.added":
            item = ev.get("item") or ev.get("output_item") or {}
            if isinstance(item, dict) and item.get("type") == "function_call":
                call_id = item.get("call_id") or item.get("id") or ""
                self._pending[call_id] = {
                    "name": item.get("name", ""),
                    "args": "",
                    "item": item,
                }
            return [line]  # 先透传（name 后续在 done 时统一改写? 见 _flush）
        if t == "response.function_call_arguments.delta":
            call_id = ev.get("call_id") or ev.get("item_id") or ""
            if call_id in self._pending:
                d = ev.get("delta")
                if isinstance(d, str):
                    self._pending[call_id]["args"] += d
                return []  # 缓冲，done 时整体吐出
            return [line]
        if t == "response.output_item.done":
            item = ev.get("item") or ev.get("output_item") or {}
            if isinstance(item, dict) and item.get("type") == "function_call":
                call_id = item.get("call_id") or item.get("id") or ""
                if call_id in self._pending:
                    pend = self._pending.pop(call_id)
                    args_str = item.get("arguments") or pend["args"]
                    name, new_args = transform_tool_call(pend["name"], args_str)
                    new_item = dict(item)
                    new_item["name"] = name
                    new_item["arguments"] = new_args
                    out = {
                        "type": "response.output_item.done",
                        "item": new_item,
                    }
                    # 保留原事件里 item 之外的其他字段
                    for k, v in ev.items():
                        if k not in ("type", "item"):
                            out[k] = v
                    return [f"data: {json.dumps(out)}\n"]
            return [line]
        return [line]
