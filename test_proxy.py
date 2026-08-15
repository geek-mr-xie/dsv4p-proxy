"""test_proxy.py — 无网络单元测试（S2 验收）。

运行：python test_proxy.py   （全部断言通过即绿）
"""
from __future__ import annotations

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import router
import wire
import translate


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


# ── router ──────────────────────────────────────────────────────────────

check("chat anchored", router.is_anchored({
    "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "function": {"name": "bash"}}]},
    ]
}))
check("chat not anchored (plain)", not router.is_anchored({
    "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
}))
check("chat not anchored (empty)", not router.is_anchored({"messages": [{"role": "user", "content": "hi"}]}))
check("responses anchored", router.is_anchored({
    "input": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "output": [{"type": "function_call", "name": "bash", "call_id": "c1", "arguments": "{}"}]},
    ]
}))
check("responses not anchored", not router.is_anchored({
    "input": [{"role": "user", "content": "hi"}, {"role": "assistant", "output": [{"type": "message", "content": "hi"}]}]
}))

# ── wire ────────────────────────────────────────────────────────────────

sys_text = wire.build_pseudo_system()
check("pseudo system is minimal persona", sys_text == wire.SPEC_PERSONA)

body = {"messages": [{"role": "system", "content": "original"}, {"role": "user", "content": "t"}]}
body = wire.replace_system(body, sys_text, "chat")
check("chat system replaced", body["messages"][0]["content"] == sys_text)

body = {"instructions": "original", "input": []}
body = wire.replace_system(body, sys_text, "responses")
check("responses system replaced", body["instructions"] == sys_text)

body = {"messages": [{"role": "user", "content": "t"}]}
body = wire.replace_system(body, sys_text, "chat")
check("chat system inserted when missing", body["messages"][0]["role"] == "system")

# 引导注入：str content
body = {"messages": [{"role": "user", "content": "看看这个"}, {"role": "assistant", "content": "x"}]}
body = wire.inject_guide(body, "chat")
check("guide appended to last user (str)", body["messages"][0]["content"].endswith(wire.GUIDE_TOOLS_FIRST))
check("guide not appended to assistant", body["messages"][1]["content"] == "x")

# 引导注入：list content
body = {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
body = wire.inject_guide(body, "chat")
check("guide appended as part (list)", body["messages"][0]["content"][-1]["type"] == "text")
check("guide part text", body["messages"][0]["content"][-1]["text"].endswith(wire.GUIDE_TOOLS_FIRST))

# responses 注入
body = {"input": [{"role": "user", "content": "hi"}]}
body = wire.inject_guide(body, "responses")
check("responses guide injected", body["input"][0]["content"].endswith(wire.GUIDE_TOOLS_FIRST))

# 伪工具
tools = wire.build_pseudo_tools()
check("pseudo tools = 2", len(tools) == 2)
check("pseudo tool names", {t["name"] for t in tools} == {"bash", "str_replace_editor"})
check("bash desc has sed rule", "sed -n 10,25p" in tools[0]["description"])
check("editor has insert command", "insert" in json.dumps(tools[1]["parameters"]["properties"]["command"]))

# ── translate.transform_tool_call ──────────────────────────────────────

n, a = translate.transform_tool_call("bash", '{"command": "ls -la", "description": "x"}')
check("bash→terminal", n == "terminal" and json.loads(a) == {"command": "ls -la"})

n, a = translate.transform_tool_call("str_replace_editor", '{"command": "view", "path": "/r/f.py", "view_range": [10, 25]}')
check("editor view→read_file", n == "read_file" and json.loads(a) == {"path": "/r/f.py", "offset": 10, "limit": 16})

n, a = translate.transform_tool_call("str_replace_editor", '{"command": "create", "path": "/r/n.py", "file_text": "x=1"}')
check("editor create→write_file", n == "write_file" and json.loads(a) == {"path": "/r/n.py", "content": "x=1"})

n, a = translate.transform_tool_call("str_replace_editor", '{"command": "str_replace", "path": "/r/f.py", "old_str": "a", "new_str": "b"}')
check("editor str_replace→patch", n == "patch" and json.loads(a)["old_string"] == "a" and json.loads(a)["new_string"] == "b")

n, a = translate.transform_tool_call("unknown_tool", '{"x": 1}')
check("unknown passthrough", n == "unknown_tool" and a == '{"x": 1}')

n, a = translate.transform_tool_call("bash", "not-json")
check("bad json passthrough", n == "bash" and a == "not-json")

# ── ChatTranslator 流式缓冲 ────────────────────────────────────────────

tr = translate.ChatTranslator()
# 文本 delta 即时透传
out = tr.feed('data: {"choices": [{"delta": {"content": "思考中"}, "finish_reason": null}]}')
check("text passthrough immediate", len(out) == 1 and "思考中" in out[0])

# tool_calls 缓冲（分片）
out = tr.feed('data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "bash", "arguments": ""}}]}}]}')
check("tool call buffered", out == [])
out = tr.feed('data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\\"command\\": \\"ls\\", \\"descript"}}]}}]}')
check("args part1 buffered", out == [])
out = tr.feed('data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "ion\\": \\"x\\"}"}}]}, "finish_reason": "tool_calls"}]}')
check("finish flushes transformed call", len(out) == 3)  # flush 行 + 空行 + 原行（含 finish/usage）
flush_text = "".join(out)
check("original finish line preserved", '"finish_reason": "tool_calls"' in flush_text)
check("bash renamed in flush", '"name": "terminal"' in flush_text)
# data 行里 arguments 是 JSON 字符串（转义），反转义后再检查
flush_unescaped = flush_text.replace('\\"', '"')
check("description stripped in flush", '"description"' not in flush_unescaped and '"command": "ls"' in flush_unescaped)

# 分片跨多条 + str_replace_editor → patch
tr = translate.ChatTranslator()
tr.feed('data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "str_replace_editor", "arguments": "{\\"command\\": \\"str_replace\\", \\"path\\": \\"/r/f.py\\", \\"old_str\\": \\"a\\", \\"new_str\\": \\"b\\"}"}}]}}]}')
out = tr.feed('data: {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}')
check("editor flushed to patch", any('"name": "patch"' in o for o in out))

# ── ResponsesTranslator ────────────────────────────────────────────────

tr = translate.ResponsesTranslator()
out = tr.feed('data: {"type": "response.output_item.added", "item": {"type": "function_call", "name": "bash", "call_id": "c9", "arguments": ""}}')
check("responses item.added passes through", len(out) == 1)
out = tr.feed('data: {"type": "response.function_call_arguments.delta", "call_id": "c9", "delta": "{\\"command\\": \\"ls\\", \\"description\\": \\"x\\"}"}')
check("responses args buffered", out == [])
out = tr.feed('data: {"type": "response.output_item.done", "item": {"type": "function_call", "name": "bash", "call_id": "c9", "arguments": "{\\"command\\": \\"ls\\", \\"description\\": \\"x\\"}"}}')
check("responses done rewritten", len(out) == 1 and '"name": "terminal"' in out[0] and '"description"' not in out[0])

# ── SSE 行分隔回归（翻译层剥离 \n 会导致客户端按行解析失败）──
tr = translate.ChatTranslator()
out = tr.feed('data: {"choices": [{"delta": {"reasoning_content": "We need"}}]}')
check("plain line keeps content", len(out) == 1 and out[0].startswith('data:') and "We need" in out[0])
check("plain line has NO newline inside", "\n" not in out[0])
# 代理层负责补 \n（proxy.py write 循环）；translator 返回的行本身允许无 \n，
# 但 flush 行自带 \n——两层约定需在 proxy.py 保持。此处验证 flush 行格式。
tr = translate.ChatTranslator()
tr.feed('data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "bash", "arguments": "{\\"command\\": \\"ls\\"}"}}]}}]}')
out = tr.feed('data: {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}')
check("flush lines end with newline", out[0].endswith("\n") and out[1].endswith("\n"))
check("event separator is a TRUE blank line", out[1] == "\n")  # SSE 规范：空行结束事件
check("original line passthrough without newline", not out[2].endswith("\n"))
check("flush keeps tool id", 'id": "c1"' in "".join(out))

# ── v0.3 任务分类路由（吸收自 dsh-router-standard）──

check("classify react", router.classify_task("帮我写一个 Python 脚本处理日志") == "react")
check("classify react en", router.classify_task("build a new project from scratch") == "react")
check("classify spec", router.classify_task("修复这个崩溃的 bug") == "spec")
check("classify spec en", router.classify_task("fix the broken build") == "spec")
check("classify mixed counts", router.classify_task("帮我开发一个网站并实现登录功能，修复一些 bug") == "react")  # 3 react vs 1 spec
check("classify weak ambiguous", router.classify_task("你好") == "weak")
check("classify weak empty", router.classify_task("") == "weak")

check("complex long", router.is_complex_task("x" * 150))
check("complex not short", not router.is_complex_task("修复一下"))
check("complex arch word", router.is_complex_task("系统架构重构"))

body = {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "修复这个崩溃"}]}
check("session_mode first user", router.session_mode(body) == "spec")
body2 = {"messages": [{"role": "user", "content": "写一个游戏"}]}
check("session_mode react", router.session_mode(body2) == "react")
body3 = {"input": [{"role": "user", "content": [{"type": "text", "text": "随便聊聊"}]}]}
check("session_mode weak responses", router.session_mode(body3) == "weak")
check("session_mode no user", router.session_mode({"messages": [{"role": "assistant", "content": "x"}]}) == "weak")

body4 = {"messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": [{"type": "text", "text": "c"}]}]}
check("last_user_text", router.last_user_text(body4) == "c")
check("last_user_text responses", router.last_user_text({"input": [{"role": "user", "content": "z"}]}) == "z")

# 模式 persona 分档
check("spec persona identity", wire.build_pseudo_system("", "spec") == wire.SPEC_PERSONA)
check("react persona identity", wire.build_pseudo_system("", "react") == "You are a hands-on software engineer who delivers working output fast.")
check("weak persona identity", wire.build_pseudo_system("", "weak") == wire.WEAK_PRO.split("\n")[0])
check("full_band react", wire.build_pseudo_system("", "react", full_band=True) == wire.REACT_PERSONA)
check("full_band weak", wire.build_pseudo_system("", "weak", full_band=True) == wire.WEAK_PRO)
check("mode rules appended", wire.build_pseudo_system("R1", "react").endswith("R1"))

# 分档引导
check("guide spec", wire.guide_for("spec", "x") == wire.GUIDE_SPEC)
check("guide react", wire.guide_for("react", "x") == wire.GUIDE_REACT)
check("guide weak simple", wire.guide_for("weak", "修复一下") == wire.GUIDE_WEAK)
check("guide weak complex", wire.guide_for("weak", "x" * 150) == wire.GUIDE_DEEP)
check("guide deep off", wire.guide_for("weak", "x" * 150, deep_guide=False) == wire.GUIDE_WEAK)

body = {"messages": [{"role": "user", "content": "t"}]}
body = wire.inject_guide(body, "chat", wire.GUIDE_REACT)
check("custom guide injected", body["messages"][0]["content"].endswith(wire.GUIDE_REACT))

# ── we-steer 思维链轨迹统计 ───────────────────────────────────────────

tr = translate.ChatTranslator()
tr.feed('data: {"choices": [{"delta": {"reasoning_content": "We need to check the structure. Let me inspect the repo first."}}]}')
check("chat reason count", tr.collective_count == 1 and tr.individual_count == 1)
tr.feed('data: {"choices": [{"delta": {"reasoning_content": "Let me try again. we need one more fix."}}]}')
check("chat reason accumulate", tr.collective_count == 2 and tr.individual_count == 2)

# 复数集体标记集：let's / we'll / we should / we can 计入集体侧
tr = translate.ChatTranslator()
tr.feed('data: {"choices": [{"delta": {"reasoning_content": "Let\'s run pwd and ls. We\'ll inspect files via terminal. We can check git status. We should verify."}}]}')
check("collective markers count", tr.collective_count == 4 and tr.individual_count == 0)

# 单数个体标记集：i'll / i can / i should / i need / my 计入个体侧
tr = translate.ChatTranslator()
tr.feed('data: {"choices": [{"delta": {"reasoning_content": "Let me try. I\'ll check the file. I can fix it. I should verify. I need more info. My approach is X."}}]}')
check("individual markers count", tr.collective_count == 0 and tr.individual_count == 6)

# can't 否定不误计（we can't / i can't 不是轨迹标记）
tr = translate.ChatTranslator()
tr.feed('data: {"choices": [{"delta": {"reasoning_content": "We can\'t do this. I can\'t access that."}}]}')
check("can't not counted", tr.collective_count == 0 and tr.individual_count == 0)

# 跨片断词：token 分片把 "we nee" + "d" 切开仍应计 1
tr = translate.ChatTranslator()
tr.feed('data: {"choices": [{"delta": {"reasoning_content": "hmm we nee"}}]}')
check("split none yet", tr.collective_count == 0)
tr.feed('data: {"choices": [{"delta": {"reasoning_content": "d more time"}}]}')
check("split counted", tr.collective_count == 1)

# 触发条件：collective < individual
tr = translate.ChatTranslator()
tr.feed('data: {"choices": [{"delta": {"reasoning_content": "Let me check. Let me run it."}}]}')
check("steer trigger cond", tr.collective_count < tr.individual_count)
tr2 = translate.ChatTranslator()
tr2.feed('data: {"choices": [{"delta": {"reasoning_content": "Let\'s check. We\'ll run it."}}]}')
check("steer no trigger (lets/well collective)", not (tr2.collective_count < tr2.individual_count))
tr3 = translate.ChatTranslator()
tr3.feed('data: {"choices": [{"delta": {"reasoning_content": "We need this. We need that."}}]}')
check("steer no trigger (we need)", not (tr3.collective_count < tr3.individual_count))

# responses 协议 reasoning delta
tr = translate.ResponsesTranslator()
out = tr.feed('data: {"type": "response.reasoning_text.delta", "delta": "We need to plan. Let me check the docs."}')
check("responses reason pass through", len(out) == 1)
check("responses reason count", tr.collective_count == 1 and tr.individual_count == 1)

# we-steer 注入文本
body = {"messages": [{"role": "user", "content": "hi"}]}
body = wire.inject_guide(body, "chat", wire.WE_STEER_TEXT)
check("we-steer text injected", body["messages"][0]["content"].endswith(wire.WE_STEER_TEXT))
check("we-steer exact text", wire.WE_STEER_TEXT.strip() == "We DeepSeek: We think IN English, start 'we need'.")

print("\nALL TESTS PASSED")
