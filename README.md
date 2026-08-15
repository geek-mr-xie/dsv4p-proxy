# ds-proxy — DeepSeek V4 agent 代理层

一个轻量本地代理，位于 agent 客户端与 DeepSeek API 之间，为 V4 系列模型提供
harness 兼容的请求整形与工具调用适配。

- 首轮使用对齐工具面启动 agent 会话，首个工具调用后自动恢复完整工具集
- 自动翻译工具调用命名（bash → terminal、editor → read/write/patch），
  客户端侧零改动
- **任务分类路由**（v0.3）：按首条用户消息分类 spec/react/weak，选对应
  persona 与分档引导（inspect-first / produce-verify-fix / 模型自路由）
- **we-steer 思维链矫正**：流式观察 reasoning 轨迹——复数集体标记
  （we need/let's/we'll/we can/we should）计数小于单数个体标记
  （let me/i'll/i can/i should/i need/my）时，下一轮注入
  `We DeepSeek: We think IN English, start 'we need'.`
- 按模型白名单启用（默认仅 `deepseek-v4-pro`；Flash 等模型纯透传）
- 双协议：OpenAI Chat Completions 与 Responses API
- 无状态、无数据库、可随时重启

## 安装

```bash
pip install -r requirements.txt
```

## 配置

首次启动时如果检测不到 API Key，程序会**交互式询问**你的 DeepSeek API Key，
并自动生成代理目录下的 `.env`（已被 `.gitignore` 忽略，不会误提交）。
也可以提前手动复制模板：

```bash
cp .env.example .env
# 然后编辑 .env 填入：DEEPSEEK_API_KEY=sk-xxx
```

API key 读取顺序（任选其一即可）：

```bash
# 1. 环境变量
export DEEPSEEK_API_KEY=sk-xxx

# 2. 代理目录下的 .env 文件（模板：.env.example）
echo "DEEPSEEK_API_KEY=sk-xxx" > .env

# 3. Hermes 用户：直接复用 ~/.hermes/.env 或 ~/AppData/Local/hermes/.env（自动兼容）
```

非交互环境（如计划任务/服务方式启动）无法询问，请提前手动执行
`cp .env.example .env` 并填入 key。

`config.yaml`（复制 `config.yaml.example`）主要项：

| 项 | 说明 |
|---|---|
| `listen_host` / `listen_port` | 监听地址（默认 127.0.0.1:8787） |
| `upstream.base` | 上游 API 地址（默认官方端点） |
| `mask_models` | 启用增强的模型白名单 |
| `inject_guide` | 会话启动阶段是否注入工具优先引导 |
| `router.enabled` | 任务分类路由（spec/react/weak persona + 分档引导） |
| `router.persona_full_band` | 实验开关：完整 band persona（行为指令进 system） |
| `router.we_steer` | we-steer 思维链矫正（默认开） |

> **建议**：客户端侧将 reasoning 档位设为 `high`（如 Hermes 的 `/reasoning high`），
> 配合本代理可显著提升规划式思维链（"We need ..."）的触发率。代理透传该字段，
> 不强制、不覆盖。

## 使用

```bash
python proxy.py --config config.yaml
```

PowerShell 管理（可选）：

```powershell
.\manage_proxy.ps1 -Action start|stop|restart|status|logs
```

接入 Hermes / 任何 OpenAI 兼容客户端：

```bash
# 以 Hermes 为例
hermes config set model.default deepseek-v4-pro
hermes config set model.base_url http://127.0.0.1:8787
```

验证：`curl http://127.0.0.1:8787/status`

## 测试

```bash
python test_proxy.py   # 无网络单测（wire/路由/翻译/SSe 边界）
```

## 许可证

MIT（见 LICENSE）。
