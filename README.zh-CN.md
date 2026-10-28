# liquipedia-cs-mcp
[English](README.md) | 简体中文

- 本地 MCP 服务器,用于在 [Liquipedia](https://liquipedia.net/counterstrike) 上查询反恐精英(Counter-Strike)战队与选手:
- 现役战队阵容(选手与教练/工作人员分开)、个人资料、现队友,以及最近三支前队伍各自的队友;
- 完整职业生涯履历(逐段带游戏项目标签),以及两名选手间的关系判定(经核验的共同效力、明文记载的亲属关系);
- 队伍搜索(自动过滤选手/赛事/消歧义页),以及一次调用完成最多五支战队的交叉关系矩阵(或显式选手列表),不产生逐对上游请求;
- 队伍完整赛事战绩(`/Results` 全量历史)与按级别汇总;另配 `search_wiki` / `read_wiki_page` 全站保底通道,专用工具未覆盖时仍可触达任意页面;
- 资源类型不匹配时(把队伍页传给选手工具,或反过来)返回 `isError`、机器可读错误码和正确的替代工具,不再返回"成功但为空"的结果。

## 安装

```bash
python -m venv .venv
.venv/Scripts/pip install -e .             # Windows;其他系统使用 .venv/bin/pip
```

注册为 **stdio** 服务器——客户端自行拉起并托管进程:不占端口、无需手动启动。把 `D:/src/liquipedia-mcp` 替换为你的克隆路径:

```json
{
  "liquipedia-cs-mcp": {
    "type": "stdio",
    "command": "D:/src/liquipedia-mcp/.venv/Scripts/python.exe",
    "args": ["-m", "app.stdio"]
  }
}
```

macOS/Linux 下 command 为 `<克隆路径>/.venv/bin/python`。

- **ZCode** — `~/.zcode/cli/config.json` → `mcp.servers`
- **Claude Desktop / Cursor** — 客户端的 `mcpServers` 配置
- **Codex CLI** — `~/.codex/config.toml` → `[mcp_servers.liquipedia-cs-mcp]`,填入相同的 `command`/`args`

进阶用法(多客户端共享一个常驻实例,也是 ChatGPT 桌面版连接器唯一可用的模式):运行
`.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`,改为注册 `{"type": "http", "url": "http://127.0.0.1:8000/mcp"}`。限流以单进程协调最佳,请避免 stdio 与 HTTP 实例同时高频使用。

> 不想手动配置?把下面这句话直接丢给你的 agent:
> "把 D:/src/liquipedia-mcp 下的 stdio MCP 服务器(command: .venv/Scripts/python.exe,args: -m app.stdio)注册到我的 MCP 客户端,并验证连接。"

## MCP 工具

| 工具 | 输入 | 输出 |
| --- | --- | --- |
| `search_players` | `query`、`limit`(1–5) | 候选选手列表,含 `player_id` |
| `search_teams` | `query`、`limit`(1–10) | 候选队伍列表,含 `team_id`;自动过滤选手/赛事/消歧义页,精确标题优先 |
| `get_team_roster` | `team_id`、`include_staff` | 现役选手(昵称/真名/国籍/年龄/角色/加入日期,`confirmed`/`substitute`)以及单独列出的教练与工作人员 |
| `get_player_network` | `player_id`、`wait_ms`(0–60000) | 缓存结果、等待后的终态结果,或用于轮询的 `job_id` |
| `get_lookup_result` | `job_id` | 状态/进度 + 结果(含 `sources`、时间戳、CC BY-SA 3.0 许可信息、`warnings`;失败任务带 `error.code`,如 `job_stalled`) |
| `get_player_career` | `player_id` | 完整战队履历:每段效力的游戏标签(CS / CS:GO / CS2)、原始精度的日期、身份标注、改名归一后的战队名 |
| `get_player_relationship` | `player_a`、`player_b`、`include_non_overlapping` | 有重叠的共同效力区间(`confirmed` / `unconfirmed`)、双方当时身份、明文记载的亲属关系;无重叠的碎片默认隐藏,仅计入 `omitted` |
| `get_relationship_matrix` | `teams`(2–5)或 `players`(2–20)、`include_staff`、`include_non_overlapping`、`max_pairs`(1–500) | 一次调用比较全部跨队 pair:输出 `connections[]`、`same_team_no_overlap[]`、`stats`、`skipped[]`;队伍无效则整次失败,个别选手页缺失返回 `partial` |
| `get_team_achievements` | `team_id`、`tier`(可选,如 `S-Tier` 或 `S`) | 队伍 `/Results` 子页的完整赛事战绩:每行含日期/名次/级别/赛事/比分/对手/奖金,并附按级别汇总(场次/冠军/亚军/四强/总奖金) |
| `search_wiki` | `query`、`limit`(1–20) | 不过滤页面类型的全站全文搜索——专用工具未覆盖时的保底入口 |
| `read_wiki_page` | `title`、`format`(`wikitext`/`html`)、`section` | 任意页面(含子页)的原始内容,上限 4 万字符;LPDB 生成的表格(成绩、统计)需用 `html` |

## 配置(环境变量,`LIQUIPEDIA_` 前缀)

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `LIQUIPEDIA_CONTACT` | *(在 `.env` 中设置)* | User-Agent 中必需的联系方式 |
| `LIQUIPEDIA_CACHE_TTL_SECONDS` | `86400` | 页面缓存有效期(秒) |
| `LIQUIPEDIA_PAGE_BUDGET_PER_JOB` | `30` | 每个查询任务的新页面抓取上限 |
| `LIQUIPEDIA_JOB_STALL_SECONDS` | `60` | 无心跳超过该秒数后,孤儿任务转为 `failed` / `job_stalled` |
| `LIQUIPEDIA_API_URL` | counterstrike wiki API | 上游接口地址 |

## 合规设计

- 唯一的串行调度器为所有上游请求设定随机 2.3–3.0 秒的间隔,高于 Liquipedia 官方 2 秒下限(渲染页 parse 请求使用更宽的 33–35 秒车道)
- 计时状态持久化到 SQLite,服务重启后不会重置
- 429 响应遵守 `Retry-After`,页面预算保证每个任务都可断点续跑
- 页面抓取按多标题批量执行(`titles=A|B|C`,单请求最多 10 个标题),一次完整关系网查询约 7 次上游请求,而非约 30 次
