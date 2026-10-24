# liquipedia-cs-mcp
[English](README.md) | 简体中文

- 本地 MCP 服务器,用于在 [Liquipedia](https://liquipedia.net/counterstrike) 上查询反恐精英(Counter-Strike)选手:
- e.g. 个人资料、现队友,以及最近三支前队伍各自的队友。

## 安装

```bash
python -m venv .venv
.venv/Scripts/pip install -e .             # Windows;其他系统使用 .venv/bin/pip
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

在你的 MCP 客户端中注册 `http://127.0.0.1:8000/mcp`(Streamable HTTP):

- **ZCode** — `~/.zcode/cli/config.json` → `mcp.servers`
- **Claude Desktop / Cursor** — 客户端的 `mcpServers` 配置
- **Codex CLI** — `~/.codex/config.toml` → `[mcp_servers.liquipedia-cs-mcp]`,填入相同的 `url`
- **ChatGPT 桌面版** — 设置 → Connectors → Advanced → 开启 Developer mode → 添加连接器

```json
{ "liquipedia-cs-mcp": { "type": "http", "url": "http://127.0.0.1:8000/mcp" } }
```

> 不想手动配置?把下面这句话直接丢给你的 agent:
> "把本地 HTTP MCP 服务器 http://127.0.0.1:8000/mcp 注册到我的 MCP 客户端,并验证连接。"

## MCP 工具

| 工具 | 输入 | 输出 |
| --- | --- | --- |
| `search_players` | `query`、`limit`(1–5) | 候选选手列表,含 `player_id` |
| `get_player_network` | `player_id` | 缓存结果,或用于轮询的 `job_id` |
| `get_lookup_result` | `job_id` | 状态/进度 + 结果(含 `sources`、时间戳、CC BY-SA 3.0 许可信息、`warnings`;未知字段返回 `null` 并附原因) |

## 配置(环境变量,`LIQUIPEDIA_` 前缀)

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `LIQUIPEDIA_CONTACT` | *(在 `.env` 中设置)* | User-Agent 中必需的联系方式 |
| `LIQUIPEDIA_CACHE_TTL_SECONDS` | `86400` | 页面缓存有效期(秒) |
| `LIQUIPEDIA_PAGE_BUDGET_PER_JOB` | `30` | 每个查询任务的新页面抓取上限 |
| `LIQUIPEDIA_API_URL` | counterstrike wiki API | 上游接口地址 |

## 合规设计

- 唯一的串行调度器为所有上游请求设定随机 2.3–3.0 秒的间隔,高于 Liquipedia 官方 2 秒下限
- 计时状态持久化到 SQLite,服务重启后不会重置
- 429 响应遵守 `Retry-After`,页面预算保证每个任务都可断点续跑
