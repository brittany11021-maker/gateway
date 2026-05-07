# Memory Gateway — CLAUDE.md（给 Claude Code / DeepSeek 读的）

> 放在项目根目录 `memory-gateway/CLAUDE.md`。  
> 服务器：43.159.56.67，SSH：`ssh -i ~/.ssh/claude_code_key root@43.159.56.67`

---

## 一、项目是什么

自托管 AI 记忆网关，OpenAI 兼容代理。用户（Iris）通过前端 / SillyTavern / Telegram 和 agent 对话，网关在对话前注入记忆，对话后做蒸馏存储。

---

## 二、文件结构

```
memory-gateway/
├── docker-compose.yml        # gateway / postgres / qdrant / screentime / sillytavern
├── .env                      # 所有密钥和环境变量
└── gateway/
    ├── main.py               # FastAPI 主程序，~10100 行，所有路由 + 后台任务
    ├── memory_db.py          # SQLite 记忆系统（L1-L5 + projects/npcs/daily），~1783 行
    ├── requirements.txt
    └── static/
        ├── admin.html        # 前端入口（单页应用）
        └── js/
            ├── api.js        # 共享：S{key,tab}、api()、allAgents、toast()
            ├── memory.js     # 记忆 tab UI（含回收站）
            ├── books.js      # 读书 tab UI
            ├── worldbook.js  # 世界书 tab
            ├── daily.js      # 日常生活 tab
            ├── providers.js  # LLM provider 管理
            ├── mcp.js        # MCP工具管理
            ├── user.js       # 用户配置
            └── backup.js     # 备份/恢复
```

### 部署规则

| 改了什么 | 要做什么 |
|---|---|
| `main.py` 或 `memory_db.py` | `docker compose build gateway && docker compose up -d gateway` |
| `static/` 下任意文件 | 直接生效（volume mount），刷新浏览器即可 |
| 加了新 JS 文件引用 | 在 `admin.html` 里手动 bump 版本号（`?v=11` → `?v=12`） |

---

## 三、数据库

### PostgreSQL（asyncpg 连接池）

连接：`postgresql://memory_user:0y4drZreqjk6@postgres:5432/memory_db`（容器内用 `postgres` hostname）

**主要表**：

| 表 | 用途 |
|---|---|
| `agent_settings` | agent 配置：`agent_type`(agent\|character), `llm_model`, `api_chain`, `system_prompt`, `prompt_inject_mode`(always\|skip_if_system_present) |
| `providers` | LLM 提供商：`name`, `base_url`, `api_key`, `is_embed` |
| `gateway_config` | KV 配置（`default_chain` 等） |
| `conversations` | 对话历史 |
| `worldbook_books` / `worldbook_entries` | 世界书，条目有 `embedding JSONB`（向量缓存） |
| `books` / `book_pages` / `annotations` | 读书系统，`annotations.color='chat'` 是对话气泡 |
| `user_profiles` | 用户信息（注入系统提示） |

### SQLite（aiosqlite，via memory_db.py）

**路径：`/app/data/memory.db`**（不是 palimpsest.db！）

**表清单**：

| 表 | 用途 |
|---|---|
| `memories` | 主记忆，L1-L4 层，有 FTS5 索引、version 快照、archived 软删除 |
| `memory_versions` | 记忆改写历史 |
| `pending_dedup` | 相似度检测队列（需人工 review） |
| `character_state` | 角色状态：mood_score, mood_label, fatigue, scene, cooldown |
| `daily_events` | 角色每日事件流（daily life generator 用） |
| `random_events` | 随机事件概率表 |
| `npcs` | NPC 关系网（name, relationship, affinity, notes） |
| `projects` | 项目追踪（active → completed → archived → 写入 L1） |
| `conversation_summaries` | **L5 留底层**：对话摘要 + `#关键词`，FTS5 搜索 |

**⚠️ 层级命名约定（容易混淆）**：

| 层 | 正式名 | 时间范围 | importance |
|---|---|---|---|
| L1 | 永久层（Profile/Permanent） | 永久 | 5 |
| L2 | 中期层（**Events**，不叫 project！） | 1-5 年 | 4 |
| L3 | 短期层（事件快照） | 3个月-1年 | 3 |
| L4 | 碎片层（原子记忆） | 30天内 | 1-2 |
| L5 | 留底层（对话摘要） | 60天后自动清理 | — |

L5 是独立的 `conversation_summaries` 表，不在主 `memories` 表里，用 `l5_write/search/list/cleanup` 操作。

**memories 表关键字段**：
```
id, agent_id, content, layer(L1-L4), importance(1-5), type
access_count    -- >= 5 时免自动清理（touch 机制）
read_by_agent   -- 0=未读，1=已读
archived        -- 1=软删除（进入回收站）
confirmed       -- 0=L1待确认，1=已确认（默认）
status          -- new / pending_l1 / updated / related / potential_duplicate
```

### Qdrant（向量 DB）

**3个 Collection**：

| Collection | 用途 | 维度 |
|---|---|---|
| `book_chunks` | 书籍语义搜索，按 book_id/page 分片 | 1024 |
| `memories` | 记忆向量索引，和 SQLite memories 表双写 | 1024 |
| `conversations` | 对话 exchange-pair 向量，用于 RAG 历史检索 | 1024 |

**Payload schema**（memories collection）：
- `agent_id` (keyword), `layer` (keyword), `confirmed` (integer), `archived` (integer)

**Payload schema**（conversations collection）：
- `agent_id` (keyword), `conversation_id` (keyword), `role` (keyword)

**Embed 模型**：`nvidia/nv-embedqa-e5-v5`（1024 dims），通过 NVIDIA NIM API，由 `_embed_mem()` 调用。

---

## 四、向量搜索 & RAG 架构（新增）

### Helper 函数（main.py）

```python
_embed_mem(text)           # 调 _embed() 用 EMBED_PROVIDER，返回 1024-dim float list
_mem_id_to_qdrant(mem_id)  # UUID → str（UUID hex，去掉 '-'，前36字符）
_sync_memory_to_qdrant(mem)  # 写/更新 memories Qdrant collection 单条记忆
_validate_memory_layer(content, layer, importance)  # B6: L1 误分类防御
_chunk_exchange_pairs(messages)  # 对话切分为 user/assistant exchange pairs
_ingest_conv_to_qdrant(agent_id, messages, conversation_id)  # 对话向量化写入
_build_rag_context(user_query, agent_id)  # 语义检索，返回注入字符串
```

### B6：L1 误分类三层防御

**问题**：蒸馏时 LLM 把时效性记忆（"最近在玩XX游戏"）错误分类为 L1 永久层。

**防御机制**：
1. **Prompt 约束**：蒸馏 prompt RULE 3 明确禁止含时间词/状态词的记忆写入 L1
2. **代码校验**（`_validate_memory_layer`）：检测以下模式，强制降级到 L2：
   - 时间词：`\d{1,2}月\d{1,2}[日号]`、`最近`、`目前`、`正在`、`这几天`、`上周/这周/下周`、`昨天/今天/明天`、`上个月/这个月`
   - 状态词：`正处于`、`正在经历`、`正在进行`、`需要处理`、`尚未解决`、`持续中`
3. **confirmed=0 待确认队列**：所有 L1 记忆写入时先设 `confirmed=0`，需在前端确认后生效

### RAG 注入流程

每次对话前（仅 agent 类型），在 wakeup 记忆注入之后，执行：
1. 在 `memories` collection 向量搜索（score_threshold=0.40，top-5，过滤 `confirmed=1 AND archived=0`）
2. 在 `conversations` collection 向量搜索（score_threshold=0.40，top-3）
3. 结果合并去重，格式化后注入为 `[语义检索]` 系统消息

### 对话向量化（`_ingest_conv_to_qdrant`）

`_post_conversation_tasks()` 完成后，将对话切分为 exchange pairs（user+assistant），每对作为一个 Qdrant point 写入 `conversations` collection。

### 现有记忆回填

已对 SQLite 73 条记忆执行过一次 Qdrant 回填（2025年5月），用以下脚本：
```python
# /tmp/backfill.py —— 仅首次需要，已执行
```

---

## 五、LLM 模型架构（A/B/C 三层）

### A 层：网关模型（后台蒸馏/自动任务用）

用 `_call_llm_cheap()` 调用，fallback 顺序：
```
A1: nvidia-llm（主，偶尔宕机）
A2: deepseek（备用，稳定）
A3: （留空）
```
首选模型由 `DISTILL_MODEL` 环境变量控制，当前 = `google/gemma-4-31b-it`。

### B 层：agent/character 的 provider 链

每个 agent 在 `agent_settings.api_chain` 配置（逗号分隔）：

**当前各 agent 配置**：

| agent | 类型 | 模型 | 链路 |
|---|---|---|---|
| default | agent | moonshotai/kimi-k2.5 | nvidia-llm,deepseek,openrouter |
| test | agent | deepseek-v4-flash | deepseek |
| chiaki | character | moonshotai/kimi-k2.5 | nvidia-llm,deepseek,openrouter |
| luna | agent | openai/gpt-4o-2024-11-20 | openrouter |

> **test agent 是测试用**，接 DeepSeek 官方 API（稳定），NVIDIA 不稳定时用它测试。

### C 层：具体模型名格式（填错返回 400）

| Provider | 格式 | 示例 |
|---|---|---|
| deepseek（官方） | 无前缀 | `deepseek-v4-flash`、`deepseek-v4-pro` |
| openrouter | `厂商/模型` | `deepseek/deepseek-v4-flash`、`openai/gpt-4o` |
| nvidia-llm | `厂商/模型` | `deepseek-ai/deepseek-v4-pro`、`moonshotai/kimi-k2.5` |

---

## 六、核心流程：`POST /v1/chat/completions`

```
1. 解析 X-Agent-ID header（优先）→ body.agent_id → "default"
2. 加载 agent 配置（agent_type: agent | character）
3. 注入系统提示（prompt_inject_mode 控制：always / skip_if_system_present）
4. 注入用户信息（user_profiles 表）
5. 解析世界书（_resolve_worldbook，keyword/vector/constant 触发模式）
6. 若 character：调 _process_character_mcp() 获取角色上下文（含 character_state/daily_events/npcs）
7. 若 agent：
   a. memory_wakeup() 拿 L1-L4 记忆 + L5 关键词搜索
   b. 【新】_build_rag_context() 语义检索 memories+conversations → 注入 [语义检索] 系统消息
8. 按 api_chain 调 LLM（stream=true：event_stream SSE；stream=false：直接返回）
9. 后台 asyncio.create_task：_post_conversation_tasks()
   → 蒸馏 → _validate_memory_layer → 写记忆 → dedup_check → _sync_memory_to_qdrant
   → _ingest_conv_to_qdrant（对话向量化）
```

**流式超时**：`httpx.Timeout(connect=8.0, read=60.0, write=10.0, pool=5.0)`

**错误处理**：
- 流中途断连（`RemoteProtocolError`/`ReadError`）：已有内容时 yield `[DONE]`，无内容时 fallback 下一 provider
- Telegram webhook 调用失败：发 `⚠️ 出错了，稍后再试` 消息给用户

---

## 七、记忆系统关键函数（memory_db.py）

```python
memory_write(agent_id, content, layer, importance, type_)  # 直接写
memory_write_smart(...)       # 写入前先做 dedup_check
memory_wakeup(agent_id)       # agent 对话前调：L1 anchor + 高 importance + 未读
memory_surface(agent_id)      # 轻量版 wakeup，只拿未读
memory_cleanup(agent_id)      # 按 importance + age 清理旧记忆
dedup_check(agent_id, content)  # 相似度检测，写入 pending_dedup 队列
dedup_resolve(pending_id, action)  # "merge"/"keep_both"/"discard"

# L1 保护
memory_confirm_l1(id)         # 确认待定的 L1 更新生效
memory_list_pending_l1(agent_id)  # 列出待确认的 L1

# L5
l5_write(agent_id, summary, keywords)
l5_search(agent_id, query)    # FTS5 关键词搜索
l5_cleanup(agent_id, days=60)

# Projects（L2 轨）
project_upsert / project_list / project_complete / project_archive

# NPCs
npc_upsert / npc_list / npc_delete

# Character state
state_get / state_set / state_touch / state_cooldown_active / state_mood_drift
event_roll / event_list / event_add / event_delete
```

---

## 八、回收站（Trash）系统

记忆删除是**软删除**（`archived=1`），可通过回收站还原或彻底删除。

### REST API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/admin/memories/trash?agent_id=X` | 列出已删除记忆 |
| `POST` | `/api/admin/memories/trash/{id}/restore` | 还原记忆（archived→0，重新同步 Qdrant） |
| `DELETE` | `/api/admin/memories/trash?agent_id=X` | 清空回收站（彻底删除 SQLite + Qdrant） |
| `DELETE` | `/api/admin/memories/{id}?hard=true` | 彻底删除单条（从回收站界面调用） |

> **⚠️ 路由顺序**：trash 路由必须注册在 `/{memory_id}` catchall 之前（FastAPI 按顺序匹配）。

### 前端（memory.js）

- 新增 **Trash** tab 在 agent detail 页面（所有 agent/character 都有）
- 删除键（✕）→ 软删除，toast "Moved to Trash 🗑"
- Trash tab 显示回收站列表：
  - ↩ 还原按钮 → `restoreMemory(id, aid)`
  - ✕ 彻底删除 → `hardDeleteMemory(id, aid)`
  - "清空回收站"按钮 → `emptyTrash(aid)`

---

## 九、记忆写入规范

### 人称规则
```
✅ {user}的生日是5月13日
✅ 我和{user}聊到了...（"我"指 agent）
❌ 你的生日、他的生日、用户的生日
```

### 层级判断
- 一年后还重要吗？→ L1（但含时间词/状态词会被 B6 自动降级到 L2）
- 三个月后还重要吗？→ L2（Events）
- 一个月后还重要吗？→ L3
- 三天后可能就忘了 → L4
- 聊天摘要备查 → L5

### 记忆类型标签
| 类型 | 用途 |
|---|---|
| anchor | 身份规则、核心设定，importance 必须=5 |
| diary | 日常杂项 |
| treasure | 真正珍贵的瞬间 |
| message | 互相留的话 |

---

## 十、Telegram 集成

- Webhook：`POST /telegram/webhook`（secret header 验证）
- 会话：`_tg_agents` dict + `_tg_sessions` dict（内存，重启清空）
- 路由：调 `http://localhost:8000/v1/chat/completions`，timeout=45s
- 指令：`/start [agent]`, `/switch <agent>`, `/list`, `/clear`
- 失败时发错误消息：`⚠️ 出错了，稍后再试`

---

## 十一、读书系统

- 书：`books` 表，页：`book_pages` 表，`default_agent` 存在 `books` 表
- 前端：`books.js`，`sendBookChat()` 调 `/v1/chat/completions`（SSE 流式）
- 选文字 → popup → "💬 和{agent}聊" → `openBookChat()` → overlay 聊天
- `color='chat'` 批注 = 对话记录，渲染为气泡，不参与文字高亮

---

## 十二、前端架构

单页应用，vanilla JS，无框架。

- **全局**（`api.js`）：`S{key,tab,night}`，`allAgents`，`api(path,opts)` 封装请求
- **主题**：CSS 变量 `--surface` / `--surface2` / `--accent` / `--muted`，dark/light 切换
- **Tab 列表**：user / agent / character / **memory** / **project** / conversations / books / mcp / daily / world
  - `memory` tab：agent 类型记忆（原 `tab-recent`）
  - `project` tab：project 类型记忆（含 L2 Events）
- **agent detail 子 tab**：L1(Profile) / L2(Events) / L3(Recent) / L4(Atomic) / L5(Archive) / History / Daily(character专用) / **Trash(回收站)**
- 版本号：admin.html 里 `?v=N`，改了 JS 需手动 +1 强制刷新

---

## 十三、⚠️ 已知代码问题（待修）

1. **memory_db.py L5 代码重复**：`l5_write/search/list` 等函数在文件里出现了两遍（约第 1537 行和第 1648 行），需删掉前一份
2. **main.py 重复导入**：`memory_confirm_l1` 和 `memory_list_pending_l1` 在 70-73 行和第 74 行各导入一次
3. **pending-l1 路由顺序**：`/api/admin/memories/pending-l1` 在 `/{memory_id}` catchall 之后注册（已有，UI 用 list+filter 绕过），需移前（低优先级）

---

## 十四、待实现功能（按优先级）

### 记忆架构

- **B4 余弦相似度去重**：用向量相似度替换 `_mem_dedup_check` 里的 Jaccard 算法（Qdrant nearest-neighbor 查询）
- **B7 L5 弃用**：L5 的对话摘要功能由 `conversations` Qdrant collection + RAG 替代，可逐步停用 `l5_write`
- **A4 历史对话导入**：将用户提供的历史对话 JSON 批量写入 `conversations` collection

### Daily Life 系统（character 用）

- **每日骨架生成器**：凌晨 cron，便宜模型生成 `daily_events` + 写 `character_state`
- **天气/Todoist 数据注入**：角色 wakeup 时注入当日天气/用户日程
- **主动发消息引擎**：触景生情 + 冷却控制，via Telegram

### 梦系统 ✅

- ✅ **Agent 梦**：每日 02:00 UTC，L4碎片→LLM归类→L3记忆 + GitHub Obsidian markdown节点
- ✅ **Character 梦**：daily_events→梦境叙述→character_state.dream_text，30% 概率注入对话
  - 手动触发：`POST /admin/api/agents/{id}/dream`

### 知识图谱 ✅

- ✅ **GitHub Obsidian 集成**：`GITHUB_TOKEN` + `GITHUB_OBSIDIAN_REPO=brittany11021-maker/obsidian`
  - 节点路径：`memory-nodes/{agent_id}/{YYYY-MM-DD}.md`（含 frontmatter + 梦境叙述 + L3 cluster）

---

## 十五、环境变量速查（.env）

```
GATEWAY_API_KEY=mgw-h3Lbg0HtBcTEdKsfG6hVk6i1ta
LLM_API_KEY=nvapi-...              # NVIDIA NIM
POSTGRES_PASSWORD=0y4drZreqjk6
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
TELEGRAM_CHARACTER_ID=chiaki
GATEWAY_PUBLIC_URL=https://memory.513129.xyz
DISTILL_MODEL=google/gemma-4-31b-it
GITHUB_TOKEN=github_pat_xxxx   # 服务器 .env 里已配置，不要提交到 git
GITHUB_OBSIDIAN_REPO=brittany11021-maker/obsidian
EMBED_PROVIDER=nvidia            # 控制 _embed_mem() 用哪个 provider
```

---

## 十六、常用命令

```bash
# 改了 Python 文件后重新构建
docker compose build gateway && docker compose up -d gateway

# 看日志（过滤爬虫噪声）
docker compose logs gateway --tail=50 -f | grep -v phpunit

# 进 PostgreSQL
docker exec memory-gateway-postgres-1 psql -U memory_user -d memory_db

# 进 SQLite（在容器里用 python）
docker exec memory-gateway-gateway-1 python3 -c "
import sqlite3; conn=sqlite3.connect('/app/data/memory.db')
# ...
"

# 推静态文件到服务器
scp gateway/static/js/memory.js root@43.159.56.67:~/memory-gateway/gateway/static/js/memory.js
scp gateway/static/admin.html   root@43.159.56.67:~/memory-gateway/gateway/static/admin.html

# 推 Python 文件并 rebuild
scp gateway/main.py root@43.159.56.67:~/memory-gateway/gateway/main.py
ssh root@43.159.56.67 "cd ~/memory-gateway && docker compose build gateway && docker compose up -d gateway"

# 查看 Qdrant 状态
curl http://43.159.56.67:6333/collections

# 回填现有记忆到 Qdrant（仅首次）
# 见 /tmp/backfill.py（已于2025-05执行过，73条）
```
