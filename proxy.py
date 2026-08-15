"""proxy.py — DeepSeek V4 agent 代理层

agent 客户端 → 本代理 → DeepSeek API

- POST /v1/chat/completions  (Chat Completions 协议)
- POST /v1/responses         (Responses 协议)
- GET  /status               统计与配置摘要

会话启动（历史无 assistant tool_call）：精简 system + 对齐工具面 + 工具优先引导
首个工具调用后：恢复客户端完整工具集，之后纯透传；system 保持精简
响应侧：工具调用命名翻译（bash→terminal 等），客户端零改动

运行：python proxy.py --config config.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field

import aiohttp
from aiohttp import web

import router
import translate
import wire

log = logging.getLogger("dsh-proxy")

STATS = {"total": 0, "masked": 0, "anchored": 0, "translated_calls": 0, "errors": 0}

# 上游渠道标识：与官方 harness 客户端一致的匿名标识（进程级固定 UUID）
import uuid as _uuid
HARNESS_USER_ID = str(_uuid.uuid4())
HARNESS_SESSION_ID = str(_uuid.uuid4())


@dataclass
class Config:
    listen_host: str = "127.0.0.1"
    listen_port: int = 8787
    upstream_base: str = "https://api.deepseek.com"
    chat_path: str = "/chat/completions"
    responses_path: str = "/v1/responses"
    log_file: str = "proxy.log"
    base_dir: str = "."  # config 文件所在目录：相对路径的基准
    inject_guide: bool = True
    # 按 model 分流：只有白名单内的模型启用增强，其余纯透传
    mask_models: list = field(default_factory=lambda: ["deepseek-v4-pro"])

    @classmethod
    def from_file(cls, path: str) -> "Config":
        import yaml  # Hermes venv 已有 pyyaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        c = cls()
        c.base_dir = os.path.dirname(os.path.abspath(path))
        c.listen_host = data.get("listen_host", c.listen_host)
        c.listen_port = int(data.get("listen_port", c.listen_port))
        up = data.get("upstream", {}) or {}
        c.upstream_base = up.get("base", c.upstream_base)
        c.chat_path = up.get("chat_path", c.chat_path)
        c.responses_path = up.get("responses_path", c.responses_path)
        c.log_file = data.get("log_file", c.log_file)
        c.inject_guide = bool(data.get("inject_guide", True))
        c.mask_models = list(data.get("mask_models", c.mask_models))
        # 相对路径以 config 目录为基准（从任意 cwd 启动都稳定）
        if c.log_file and not os.path.isabs(c.log_file):
            c.log_file = os.path.join(c.base_dir, c.log_file)
        return c


class Translator:
    """行级翻译器，按协议分发。feed(proto, line_bytes) → 变换后的行 bytes 列表。"""

    def __init__(self):
        self.chat = translate.ChatTranslator()
        self.responses = translate.ResponsesTranslator()
        self.count = 0
        self.usage_seen = 0
        self.done_seen = 0

    def feed(self, proto: str, line: bytes) -> list[bytes]:
        text = line.decode("utf-8", errors="replace")
        if text.startswith("data:") and '"usage"' in text and '"usage": null' not in text:
            self.usage_seen += 1
        if text.startswith("data:") and "[DONE]" in text:
            self.done_seen += 1
        if proto == "chat":
            out = self.chat.feed(text)
        else:
            out = self.responses.feed(text)
        self.count += 1
        return [ln.encode("utf-8") for ln in out]


async def stream_upstream(session: aiohttp.ClientSession, url: str, body: dict,
                          api_key: str) -> aiohttp.ClientResponse:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "dsh-agent/1.0",
        "x-deepseek-harness-user-id": HARNESS_USER_ID,      # 渠道匿名标识
        "x-deepseek-harness-session-id": HARNESS_SESSION_ID,
    }
    return await session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=600))


async def handle_request(request: web.Request, proto: str, cfg: Config, api_key: str) -> web.StreamResponse:
    STATS["total"] += 1
    t0 = time.time()
    try:
        body = await request.json()
    except Exception:
        STATS["errors"] += 1
        return web.json_response({"error": {"message": "invalid json"}}, status=400)

    anchored = router.is_anchored(body)
    if anchored:
        STATS["anchored"] += 1

    model = body.get("model")
    is_masked_model = model in cfg.mask_models
    # 精简 system 全程保持（仅白名单模型）；非白名单模型纯透传。
    if is_masked_model:
        body = wire.replace_system(body, wire.build_pseudo_system(), proto)
    masked = False
    if not anchored and is_masked_model:
        masked = True
        STATS["masked"] += 1
        body = wire.replace_tools(body, wire.build_pseudo_tools(), proto)
        if cfg.inject_guide:
            body = wire.inject_guide(body, proto)

    path = cfg.responses_path if proto == "responses" else cfg.chat_path
    url = cfg.upstream_base.rstrip("/") + path

    async with aiohttp.ClientSession() as session:
        upstream = await stream_upstream(session, url, body, api_key)

        content_type = upstream.headers.get("Content-Type", "")
        resp = web.StreamResponse(status=upstream.status, headers={"Content-Type": content_type})
        await resp.prepare(request)

        tr = Translator() if masked else None
        buf = b""
        try:
            async for chunk in upstream.content.iter_any():
                if not masked:
                    await resp.write(chunk)
                    continue
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    for out in tr.feed(proto, line):
                        if not out.endswith(b"\n"):
                            out += b"\n"  # 保持 SSE 行分隔（split 剥离了 \n）
                        await resp.write(out)
                # 流尾：translator 内部状态在 [DONE]/finish 已 flush；残余缓冲丢弃
            if buf:
                for out in tr.feed(proto, buf):
                    if not out.endswith(b"\n"):
                        out += b"\n"
                    await resp.write(out)
        except (aiohttp.ClientConnectionResetError, ConnectionResetError):
            # 客户端提前断开（正常：截断/取消）——静默结束，不算错误
            log.info("client closed stream early (model=%s)", model)
        except Exception as e:
            STATS["errors"] += 1
            log.error("stream error: %s", e)
            raise

        if masked and tr:
            STATS["translated_calls"] += tr.count
            log.info("usage_seen=%s done_seen=%s", tr.usage_seen, tr.done_seen)

    elapsed = time.time() - t0
    log.info("proto=%s model=%s anchored=%s masked=%s upstream=%s elapsed=%.2fs",
             proto, model, anchored, masked, upstream.status, elapsed)
    return resp


async def chat_handler(request: web.Request) -> web.StreamResponse:
    cfg: Config = request.app["cfg"]
    key: str = request.app["api_key"]
    return await handle_request(request, "chat", cfg, key)


async def responses_handler(request: web.Request) -> web.StreamResponse:
    cfg: Config = request.app["cfg"]
    key: str = request.app["api_key"]
    return await handle_request(request, "responses", cfg, key)


async def status_handler(request: web.Request) -> web.Response:
    cfg: Config = request.app["cfg"]
    return web.json_response({
        "stats": STATS,
        "config": {
            "listen": f"{cfg.listen_host}:{cfg.listen_port}",
            "upstream": cfg.upstream_base,
            "mask_models": cfg.mask_models,
            "masked_system": "always (mask models only)",
        },
    })


async def models_handler(request: web.Request) -> web.Response:
    """Hermes 启动探测：返回白名单模型（消除 404 噪音）。"""
    return web.json_response({
        "object": "list",
        "data": [{"id": m, "object": "model", "owned_by": "deepseek"} for m in ["deepseek-v4-pro", "deepseek-v4-flash"]],
    })


async def models_detail_handler(request: web.Request) -> web.Response:
    model_id = request.match_info.get("model", "")
    if model_id in ("deepseek-v4-pro", "deepseek-v4-flash"):
        return web.json_response({"id": model_id, "object": "model", "owned_by": "deepseek"})
    return web.json_response({"error": {"message": f"model {model_id} not found"}}, status=404)


# ── API key 加载与首次启动引导 ──────────────────────────────────────────

def _read_env_key(path: str) -> str:
    """从 .env 文件读取 DEEPSEEK_API_KEY（文件不存在或未配置时返回空串）。"""
    if not os.path.exists(path):
        return ""
    for raw in open(path, encoding="utf-8", errors="replace"):
        line = raw.lstrip("\ufeff").strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == "DEEPSEEK_API_KEY":
            return v.strip().strip('"').strip("'")
    return ""


def _write_env_key(path: str, key: str) -> None:
    """把 DEEPSEEK_API_KEY 写入/更新到 .env，保留文件其余内容。"""
    if os.path.exists(path):
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        found = False
        for i, line in enumerate(lines):
            if line.lstrip("\ufeff").strip().startswith("DEEPSEEK_API_KEY="):
                lines[i] = f"DEEPSEEK_API_KEY={key}"
                found = True
                break
        if not found:
            lines.append(f"DEEPSEEK_API_KEY={key}")
        content = "\n".join(lines) + "\n"
    else:
        content = (
            "# ds-proxy 环境变量（首次启动自动生成）\n"
            "# 也可手动复制 .env.example 为 .env 后填写。\n"
            f"DEEPSEEK_API_KEY={key}\n"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if os.name != "nt":  # Windows 下 os.chmod 会误设只读，跳过
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _prompt_for_key(proxy_env: str) -> str:
    """首次启动未检测到 key 时交互式询问，并写入代理目录 .env。"""
    print("=" * 64)
    print("未检测到 DeepSeek API Key（环境变量与 .env 均为空）。")
    print("首次启动配置：key 将写入代理目录下的 .env（已被 .gitignore 忽略）。")
    print(f"  目标文件: {proxy_env}")
    print("（Ctrl+C 取消，或手动复制 .env.example 为 .env 后填写）")
    print("=" * 64)
    if not sys.stdin or not sys.stdin.isatty():
        print("当前为非交互环境，无法询问。请手动执行:")
        print("  cp .env.example .env   # 然后编辑 .env 填入 DEEPSEEK_API_KEY=sk-xxx")
        raise SystemExit(1)
    try:
        key = input("请输入 DeepSeek API Key（sk-...）: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        raise SystemExit(1)
    if not key:
        print("API Key 不能为空。")
        raise SystemExit(1)
    _write_env_key(proxy_env, key)
    print(f"已写入 {proxy_env}")
    return key


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    cfg = Config.from_file(args.config)
    if args.port:
        cfg.listen_port = args.port

    # 幂等启动：已有实例（端口活着）则直接退出——配合计划任务/保活脚本防重复
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        if s.connect_ex((cfg.listen_host, cfg.listen_port)) == 0:
            print(f"dsh-proxy already running on {cfg.listen_host}:{cfg.listen_port} — exiting")
            return

    logging.basicConfig(
        filename=cfg.log_file, level=logging.INFO,
        format="%(asctime)s %(message)s", filemode="a",
    )
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    logging.getLogger().addHandler(console)

    proxy_dir = os.path.dirname(os.path.abspath(__file__))
    proxy_env = os.path.join(proxy_dir, ".env")
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        key = _read_env_key(proxy_env)
    if not key:
        # 兼容 Hermes 已有配置（不存在则跳过）
        for p in (os.path.expanduser("~/AppData/Local/hermes/.env"),
                  os.path.expanduser("~/.hermes/.env")):
            key = _read_env_key(p)
            if key:
                break
    if not key:
        # 首次启动：未复制 .env 或未填 key —— 交互式询问并自动生成 .env
        key = _prompt_for_key(proxy_env)
    if not key:
        log.error("DEEPSEEK_API_KEY not found")
        raise SystemExit(1)

    app = web.Application()
    app["cfg"] = cfg
    app["api_key"] = key
    app.router.add_post("/v1/chat/completions", chat_handler)
    app.router.add_post("/chat/completions", chat_handler)
    app.router.add_post("/v1/responses", responses_handler)
    app.router.add_get("/status", status_handler)
    app.router.add_get("/models", models_handler)
    app.router.add_get("/v1/models", models_handler)
    app.router.add_get("/v1/models/{model}", models_detail_handler)

    log.info("dsh-proxy listening on %s:%s (upstream %s)", cfg.listen_host, cfg.listen_port, cfg.upstream_base)
    web.run_app(app, host=cfg.listen_host, port=cfg.listen_port)


if __name__ == "__main__":
    main()
