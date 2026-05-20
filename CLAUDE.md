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
| `conversation_summaries` | ~~L5 留底层~~ **已废弃 (B7)**：表仍存在于 DB 但不再写入，由 Qdrant `conversations` collection 替代 |

**⚠️ 层级命名约定（容易混淆）**：

| 层 | 正式名 | 时间范围 | importance |
|---|---|---|---|
| L1 | 永久层（Profile/Permanent） | 永久 | 5 |
| L2 | 中期层（**Events**，不叫 project！） | 1-5 年 | 4 |
| L3 | 短期层（事件快照） | 3个月-1年 | 3 |
| L4 | 碎片层（原子记忆） | 30天内 | 1-2 |
| L5 | 留底层（对话摘要） | 60天后自动清理 | — |

~~L5 是独立的 `conversation_summaries` 表~~ — **B7 已废弃**，相关函数和 API 路由已从代码中删除，对话历史现由 Qdrant `conversations` collection + RAG pipeline 提供。

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

> 来源：2026-05-14 对照三份 Notion 文档（整体架构框架 / Daily Life + 状态引擎执行文档 / 向量搜索集成 + 记忆系统修订方案）逐行审计。
> **原则：整体架构框架与另外两份文档冲突时，以另外两份为准。**

### 已修复 ✅

1. ~~memory_db.py L5 代码重复~~ → 已修复，l5 函数各只出现一次（行 2165/2189/2212/2225）
2. ~~main.py 重复导入~~ → 已修复，`memory_confirm_l1` / `memory_list_pending_l1` 只在行 76-77 导入一次
3. ~~pending-l1 路由顺序~~ → 已修复，`/api/admin/memories/pending-l1` 在 `/{memory_id}` 之前
4. ~~`_pal_row` double-decode~~ → 已修复（2026-05-20）：`memory_db._row_to_dict` 已把 `tags` 解析为 list，`_pal_row` 再次 `json.loads` 导致 500；改为 isinstance 检查
5. ~~`confirm-l1` 不同步 Qdrant~~ → 已修复（2026-05-20）：`pal_confirm_l1` 确认后补发 `_sync_memory_to_qdrant` 更新 payload 的 `confirmed=1`，RAG 才能检索到
6. ~~`memory_search` CJK 子串搜索失败~~ → 已修复（2026-05-20）：FTS5 `unicode61` tokenizer 将连续汉字视为单一 token，子串搜索失败；新增 `LIKE '%query%'` fallback

### 仍存在的问题

4. **Dedup Jaccard fallback 仍在**：`memory_db.py` 行 783-854，`dedup_check()` 当 `vector_match` 未提供时仍 fallback 到 Jaccard（FTS5 + bigram）。向量搜索文档 §6.2 要求统一用 cosine。**现状**：cosine 是主路径（`vector_match` 提供时），Jaccard 是无 Qdrant 时的降级方案——逻辑上合理，但阈值体系不统一（cosine 0.85/0.55/0.25 vs 文档要求 0.95/0.80/0.55）
5. **memory_search 仍有 FTS5 fallback**：`palimpsest_search()`（main.py 行 1486-1545）主路径走 Qdrant 向量搜索，但 Qdrant 不可用时 fallback 到 FTS5。向量搜索文档要求全面切 Qdrant。**现状**：作为降级方案可接受，但 MCP tool 描述（行 9936）仍写 "FTS5 full-text search" 需更新
6. **L5 仍在活跃使用**：`l5_write`/`l5_search`/`l5_list`/`l5_cleanup` 仍被 import 和调用（main.py 行 92-93, 7938-7947）。向量搜索文档 §3 要求废弃，由 conversations Qdrant collection 替代
7. **Embed 模型与文档描述不一致**：向量搜索文档 §1.3 推荐 `BAAI/bge-m3`（siliconflow），实际用 `nvidia/nv-embedqa-e5-v5`（NVIDIA NIM）。维度都是 1024，功能等价，但文档需更新
8. **Hybrid 搜索未实现**：向量搜索文档 §2.4 描述 Qdrant dense+sparse（BM25 + RRF 融合），当前只用 dense vector
9. **死脑筋模型兼容模式未实现**：执行文档 §12.4 要求 `MCP_STUBBORN_MODEL_COMPAT`（对不擅长 tool use 的模型预查询注入 system prompt），代码中无相关实现

---

## 十四、待实现功能（按文档优先级）

### 已完成 ✅

- ✅ **P0 API 线路分离**：`_DEFAULT_API_ROUTES` 三条线路（main.py 行 271-320）
- ✅ **P0 时区注入**：`build_time_context()` 注入角色+用户时区（行 356+）
- ✅ **P0 角色状态引擎 — 二维情绪**：`mood_valence` [-1,1] + `mood_energy` [-1,1]，`_mood_label_from_2d()` 象限映射（memory_db.py 行 1328/1531/1781），`mood_score` 保留为 `valence*100` 向后兼容
- ✅ **P0 Health MCP 监测**：`_health_monitor_loop()` 后台任务（main.py 行 3119, 4951-5167），含心率/步数/睡眠/经期四模块，每15min/1h/每天拉取，阈值告警 + 推送日志
- ✅ **P1 晨间推送**：`_morning_push_loop()` + 天气/日程/随机事件/新闻模块注入（行 3075, 3943-4317）
- ✅ **P1 事件条件标签**：`random_events` 表含 conditions 字段，事件过滤 pipeline
- ✅ **P1 对话中日程捕获**：`_capture_schedules_from_conversation()` + Todoist 集成
- ✅ **P1 Char MCP 三层检测**：keyword 预筛 + intent_check（analyzer 线路）+ MCP 执行（main.py 行 6569-6636）
- ✅ **P2 晚间推送**：`_evening_push_loop()` 三场景（角色先睡/用户已睡/同城）（行 3076, 4966-5080）
- ✅ **P2 用户入睡多信号加权**：`_estimate_user_sleep_probability()` 四路信号（no_message_45min 0.30 / screen_locked 0.40 / said_goodnight 0.80 / past_avg_sleep 0.30），综合 ≥0.50 判定已睡（行 5175-5182）
- ✅ **P2 场景B日记影响**：用户已睡时写 scene_note `"昨晚想跟你说晚安，看你应该睡了就没打扰"`（行 5320）
- ✅ **P2 反向识别**：`_REVERSE_KEYWORDS` + `_reverse_identify()`（行 6425-6641）
- ✅ **P2 新闻系统**：`_news_daily_loop()` RSS 拉取（BBC/澎湃skip_first/联合早报/Dezeen/It's Nice That/Designboom/小红书），`_NEWS_HARD_BLOCK` 政治关键词硬过滤，30% 概率晨间注入（行 3117, 4572-4700+）
- ✅ **P2 事件连锁反应**：完整实现 — `_maybe_schedule_chain()` 概率判定 → `_compute_fire_at()` 延迟（immediate/within_1h/within_12h/next_morning）→ `chain_event_schedule()` 入 DB → `_chain_event_loop()` 每5分钟检查 → `_process_due_chain_events()` 执行（×1.5 放大 + carry_over + 发消息）（行 5397-5555）
- ✅ **P2 物品追踪**：`_extract_items_promises()` 从对话提取（行 5570-5648）+ 注入时食物7天/其他30天自动过期 + 15%概率自然提起（行 6263-6294）
- ✅ **P2 承诺追踪**：`_check_promise_reminders()` 3/7/14/30天递增概率（10%/25%/40%/60%）+ 语气递增（随口一提→认真追问）（行 5651-5694）
- ✅ **B6 记忆分类修复**：`_validate_memory_layer()` 三层防御（prompt/代码校验/pending queue）
- ✅ **Qdrant memories+conversations collection**：双写 + RAG 注入 + 对话向量化
- ✅ **蓄水池/主动消息**：miss_you/low_mood/irritable 累积 + 阈值触发 + Telegram 推送
- ✅ **梦系统**：Agent 梦（L4→L3 整理 + Obsidian 节点）+ Character 梦（daily_events→梦境叙述）
- ✅ **知识图谱**：GitHub Obsidian 集成，节点路径 `memory-nodes/{agent_id}/{YYYY-MM-DD}.md`
- ✅ **P3 Timeline 可视化**：`/timeline/` 路由 + dashboard-data.json + timeline.js（main.py 行 10312-10696, static/js/timeline.js）
- ✅ **P3 推送日志/可观测性**：`push_log` 表 + `push_log_write()` 记录所有推送决策 + `/api/push-log` 端点 + engine.js 前端展示（memory_db.py 行 1368-1595, engine.js 行 861+）
- ✅ **R2 自动备份**：`_r2_upload()` 实现（main.py 行 10031-10063），nightly 任务上传 daily/ 对话+DB 到 R2（行 3771-3774），前端有 R2 开关（admin.html 行 329）
- ✅ **音乐推荐系统**：`_music_pick_and_send()` + `_music_recommend_loop()` + `music_history` 表，三种触发模式，chiaki 语气渲染 + Telegram 推送。2026-05-14 验证通过。

### 记忆系统待完成

- [x] **B4 Dedup cosine 阈值对齐**：`memory_write_smart()` cosine 路径已是 0.95/0.80/0.55（正确），0.85/0.55/0.25 是 Jaccard fallback 的阈值，两套算法各自独立，无需修改
- [x] **B7 L5 完全废弃**：删除 `l5_write`/`l5_search`/`l5_list`/`l5_cleanup` 导入（main.py）、`_generate_l5_summary` no-op 函数、3条 `/api/admin/l5` 路由、timeline 结构里的 L5 条目；memory.js 里删除 TIER_CFG l5 条目、路由分支、`loadDetailL5` 和 `delL5Summary` 函数。2026-05-14 已部署。
- ~~[ ] **Hybrid 搜索**~~：**决定不做** — dense-only 在 0.40 阈值下表现够用，Qdrant sparse index 基础设施改动大，收益边际低。
- [x] **A4 历史对话 JSON 导入**：`_detect_import_format()` + `_parse_import_conversations()` + `_run_conv_import_job()` 异步 job；支持 claude_ai / typingmind / gateway 三种格式；pipeline：DB INSERT → LLM distillation (120s cap) → Qdrant embed；`POST /admin/api/import/conversations`（multipart）返回 job_id，`GET /admin/api/import/conversations/status/{job_id}` 轮询进度；前端 backup.js 进度条 + 格式自动识别徽章。同步修复 `_call_llm_cheap` 新增 deepseek 官方 API fallback（NVIDIA 宕机时自动切换）。2026-05-14 完成。
- ~~[ ] **daily_life Qdrant collection**~~：**决定不做** — daily_events 已全量注入 `_process_character_mcp()`，语义检索意义有限。
- [x] **MCP tool 描述更新**：`palimpsest_search` 工具目录（行 9936）改为 "Semantic search (Qdrant vector, FTS5 fallback)"；HTTP endpoint docstring（行 9689）同步更新

### 音乐系统（全部完成）

- [x] **Cloud Music MCP 迁移到 Oracle VPS**：服务已从腾讯云迁移至 Oracle VPS（161.118.195.9），以 Docker 容器运行（端口 3011，内网绑定）。访问地址：`https://palimpsest.513129.xyz/cloud-music/mcp`。腾讯云已停止 cloud-music 容器并移除 nginx 块。2026-05-14 完成。
- [x] **音乐推荐集成到主动消息**：三种触发（scheduled 35%/day、keyword、mood_low/high）+ `_music_pick_and_send()` + `_music_recommend_loop()`（07:00 UTC）。chiaki 语气渲染 + Telegram 推送 + 48h 冷却。2026-05-14 完成，端到端验证通过。
- [x] **音乐记忆学习**：`music_history` 表（SQLite，含 song_id/agent_id/trigger_mode/reaction）+ `_music_hist_*` 函数 + 30天内推荐过的歌自动过滤去重。`/admin/api/music/history` 查询端点。2026-05-14 完成。

### 其他杂项

- [x] **死脑筋模型兼容模式**：`mcp_stubborn_compat` 布尔列已加入 `agent_settings`，admin UI 有 checkbox，chat_completions 时自动剥离 tool 相关键。
- [ ] **配置面板 UI 整合**（→ 见路线图 Phase A-1）
- [x] **RSSHub 自建实例**：`diygod/rsshub:latest` 加入 docker-compose.yml，内网访问（不暴露端口）；gateway 新增 `RSSHUB_URL: http://rsshub:1200`；`_RSSHUB_BASE` 读取 env，默认 fallback `rsshub.app`；澎湃路由在新版 RSSHub 已删除且 thepaper.com 对 VPS IP 返回 403，替换为虎嗅 `/huxiu/article`（200 ✅）；联合早报 `/zaobao/realtime/china` 同样 200 ✅。2026-05-14 完成。
- [x] **GitHub 代码自动同步**：`/opt/scripts/git-sync.sh`，cron `0 3 * * *`（03:00 CST）。`git add -A`（遵守 .gitignore）→ 有变更则 commit `chore: auto-sync YYYY-MM-DD (N file(s) changed)` → push。无变更静默退出 0。日志 `/var/log/git-sync.log`（自动轮转保留 500 行）。2026-05-14 已部署，首次 push 同时整理了 .gitignore（排除 build artifacts 和旧位置副本）。
- [x] **备份验证**：`_weekly_backup_verify_loop()` 每周一 01:00 UTC（09:00 CST）检查过去7天 R2 备份完整性，缺失或不完整时发 Telegram 告警。手动触发端点：`GET /admin/api/backup/r2/verify?days=7`。R2 未启用时自动跳过。2026-05-14 已部署。

### 文档冲突解决记录

以下整体架构框架的内容已被后续文档覆盖，以后续文档为准：

| 整体架构原文 | 修订 | 实际状态 |
|---|---|---|
| §备注「向量搜索不是刚需，FTS5 够用」 | 已引入 Qdrant，语义搜索统一走 Qdrant | ✅ 已实现，FTS5 保留为 fallback |
| §2.6 L5 留底层定义 | L5 标记 DEPRECATED，被 conversations collection 替代 | ⚠️ 代码仍在，待删除 |
| §2.7 时间范围速查表含 L5 | 删除 L5 行，改为四层 | ⚠️ 前端仍显示 L5 tab |
| §3.5 去重阈值 90/70/40（文本相似度） | 改用 cosine similarity 95/80/55 | ⚠️ cosine 主路径，但阈值是 0.85/0.55/0.25 待调 |
| §4 memory_search 用 FTS5 | 底层切到 Qdrant 向量搜索 | ✅ 已实现，FTS5 保留为 fallback |
| §7.9 情绪系统 mood 单维 | 改为二维 valence+energy，含象限标签 | ✅ 已实现（mood_valence + mood_energy + _mood_label_from_2d） |
| §八 Qdrant 三层映射表 | 更新为实际 collection 设计 | ✅ memories/conversations/book_chunks |

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

---

## 十七、路线图（2026-05 起）

> 顺序即优先级，每个阶段完成后再开始下一个。

### Phase A — 功能收尾（当前 session）

| # | 任务 | 状态 | 说明 |
|---|------|------|------|
| A-1 | **配置面板 UI 整合** | ✅ 完成 | 新增音乐推荐、蓄水池阈值、系统信息三块面板；新闻面板加硬过滤关键词编辑器；runtime 函数已改为从 DB 读取配置。2026-05-15 完成。 |
| A-2 | **死脑筋模型兼容** | ✅ 完成 | 每 agent 的 `mcp_stubborn_compat` 开关（DB 列 + admin UI checkbox）。开启时从发给 LLM 的 payload 中剥离 `tools/tool_choice/functions/function_call/tool_use` 键，防止不支持 tool use 的模型产生混乱输出。character agent 的 MCP proxy 本已走纯文本注入，agent 类型也因此受益。2026-05-15 完成。 |

### Phase B — 代码三端归档

| # | 任务 | 状态 | 说明 |
|---|------|------|------|
| B-1 | **代码推 GitHub main** | ✅ 完成 | Phase A 全部内容已推送 GitHub `main`，commit bbd7116。2026-05-15。 |
| B-2 | **同步到 Oracle VPS** | ✅ 完成 | Oracle VPS（161.118.195.9）`~/memory-gateway` 已初始化 git，`git reset --hard origin/main` 同步到最新。SSH 跳板：腾讯云 `~/.ssh/oci_instance` → `ubuntu@161.118.195.9`。以后 `git pull` 即可更新冷备。2026-05-15。 |

### Phase C — 新功能（需 Notion 文档先行）

| # | 任务 | 状态 | 说明 |
|---|------|------|------|
| C-1 | **TTS / 缓存命中** | 📄 文档阶段 | 先在 Claude.ai chat 整理需求 → 输出 Notion 执行文档 → 再按文档实现。候选方案：OpenAI TTS API / 本地 Edge-TTS / 流式 KV 缓存命中减少首字延迟。 |

### Phase D — 集成测试

| # | 任务 | 状态 | 说明 |
|---|------|------|------|
| D-1 | **创建测试 agents/chars** | ✅ 完成 | `test_agent1` / `test_agent2`（agent）、`test_char1`（character）通过脚本自动创建。 |
| D-2 | **功能完整性测试** | ✅ 完成 | `test_integration.py` — 21/21 通过。记忆蒸馏→层级分类→B6防御→L1确认→Qdrant同步→RAG召回→角色状态引擎全链路验证通过。2026-05-20。 |
| D-3 | **记忆池隔离验证** | ✅ 完成 | T4 验证 test_agent2 无法读到 test_agent1 的专有记忆（林小雨/福气等），SQLite + Qdrant 双侧 agent_id 过滤均生效。 |

### Phase E — 前端重建

| # | 任务 | 状态 | 说明 |
|---|------|------|------|
| E-1 | **前端重建** | ⏳ 待做 | 当前 admin.html + 散落 JS 文件结构混乱（tab 耦合、全局变量、无组件复用）。重建目标：模块化 vanilla JS（或轻量框架如 Petite-Vue）、统一路由、清晰 tab 分区、响应式布局。**最后做，范围最大。** |

### 搁置/不做

| 任务 | 理由 |
|------|------|
| Hybrid 搜索（BM25 + RRF） | dense-only 表现够用，基础设施改动大，收益边际低 |
| daily_life Qdrant collection | daily_events 已全量注入，语义检索价值有限 |
| RPG 世界映射 | 不急，等前端重建后再考虑 |
