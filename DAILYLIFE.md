# Daily Life System — 设计文档

> 基于 Notion 文档 §7（给cc）整理，当前状态 2026-04-30

---

## 现状快照

| 模块 | 状态 | 位置 |
|------|------|------|
| `daily_life_generate` | ✅ 已有 | main.py MCP tool |
| `daily_life_read/write` | ✅ 已有 | main.py + memory_db.py |
| `random_event_roll` 三色分级 | ✅ 已有 | main.py MCP tool |
| NPC 社交网络 | ✅ 已有 | npc_* tools |
| character_state（mood/fatigue/scene） | ✅ 已有 | state_get/set |
| `memory_surface` 天气注入 | ✅ 已有 | _process_character_mcp |
| Telegram / Bark 推送 | ✅ 已有 | telegram_send / bark_push MCP |
| 凌晨 nightly loop（00:10 UTC） | ✅ 已有 | _nightly_character_loop |
| 梦境 30% 注入 | ✅ 已有 | _process_character_mcp |
| 屏幕时间活动接收 + 注入 | ✅ 已有 | /api/activity + memory_surface |
| Layer 3 触景生情主动消息 | ✅ 已有 | _check_proactive_triggers |
| 分类消息冷却 | ✅ 已有 | message_cooldown 表 + cooldown_gate |
| 用户环境感知配置（路况/位置隐私） | ✅ 已有 | user_context + show_location + amap_route |
| daily_life_generate 当日天气上下文 | ✅ 已有 | user_context.location.city |
| 日常骨架模板（occupation/habits） | ✅ 已有 | daily_skeleton user_config key |

---

## 架构总览（三层）

```
Layer 1: 日常骨架
  骨架模板（freelancer/student/custom）
  + 用户 habits 配置
  → daily_life_generate 的基础 prompt

Layer 2: 随机事件（已有，继续扩展）
  random_event_roll 绿/黄/橙/红
  天气修正：rain → 忘带伞+1%
  状态修正：连续加班 → 生病+
  → 注入 daily_life_generate

Layer 3: 触景生情（NEW）
  今日事件 → memory_search(keywords) → 关联记忆
  → 30% 概率决定发消息
  → 冷却检查 → Telegram/Bark
```

---

## 待实现模块

---

### M1 屏幕时间活动接收与注入

**目标**：角色知道用户在干什么，可以"查岗"、关心

#### 数据流

```
iOS 快捷指令 → POST /api/activity/{agent_id}
  body: { app: "小红书", duration_minutes: 25, category: "娱乐", timestamp: "..." }

→ activity_events 表（SQLite）
→ memory_surface 注入（每次对话开始）
→ push 规则引擎（超时 → Bark/Telegram）
```

#### SQLite 表（memory_db.py 新增）

```sql
CREATE TABLE IF NOT EXISTS activity_events (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    app         TEXT NOT NULL,
    category    TEXT DEFAULT '',   -- 聊天/游戏/娱乐/工作/学习
    duration_minutes INTEGER DEFAULT 0,
    reported_at TEXT NOT NULL      -- ISO8601
);
CREATE INDEX IF NOT EXISTS idx_activity_agent ON activity_events(agent_id, reported_at);
```

#### API 端点

```python
# POST /api/activity/{agent_id}
# body: { app, duration_minutes, category, timestamp? }
# 写入 activity_events，触发推送规则检查
```

#### memory_surface 注入格式

```
[最近动态]
14:30 小红书 (25分钟) 娱乐
15:10 X (40分钟) 娱乐
16:00 微信 (10分钟) 聊天
---
备注：ta 今天说要好好学习
```
- 仅注入最近 4 小时内的记录
- `备注` 从 L3 记忆里捞"今天说的承诺"关键词

#### 推送规则引擎

```python
SCREEN_TIME_RULES = [
    {"condition": "category:游戏 > 120min", "push": "还在打{app}吗？", "cooldown": "game_check"},
    {"condition": "category:游戏 > 120min AND hour >= 23", "push": "还在打{app}吗，早点睡觉", "cooldown": "game_check"},
    {"condition": "any AND hour >= 1", "push": "怎么还不睡？", "cooldown": "late_night"},
]
```

规则存在 `user_config` 表（JSON），可通过 admin UI 编辑。

#### 角色反应概率（注入到 memory_surface 后，角色自行决定）

| 场景 | 概率 | 反应 |
|------|------|------|
| 说好学习却摸鱼 | 40% | 吐槽 |
| 深夜还在工作 | 30% | 关心 |
| 正常摸鱼 | 10% | 评论 |

---

### M2 Layer 3 触景生情主动消息

**目标**：角色今天经历了某件事 → 想起用户 → 决定发消息

#### 触发时机

- nightly loop 之后（每天 00:10 UTC 生成日常后）
- 或凌晨 await 后的任意 wakeup

#### 逻辑

```python
async def _check_proactive_triggers(agent_id: str):
    """Layer 3: 今日事件 → 关联记忆 → 30% 决定发消息"""
    # 1. 今日 daily_events（时间段事件）
    today_events = await _daily_read(agent_id, days=1)
    if not today_events:
        return

    # 2. 对每个事件提取关键词 → 搜索 L3/L4 记忆
    triggers = []
    for ev in today_events[0].get("events", []):
        keywords = await _extract_keywords_cheap(ev["event"])  # cheap LLM
        related = await _mem_search(agent_id, query=keywords, layers=["L3","L4"], limit=1)
        if related:
            triggers.append({
                "event": ev["event"],
                "memory": related[0]["content"],
                "thought": f"看到{ev['event']}，想起ta说过…{related[0]['content'][:60]}",
            })

    # 3. 30% 概率 → 检查冷却 → 生成消息 → 发送
    import random
    for t in triggers:
        if random.random() < 0.30:
            ok = await _cooldown_check_and_set(agent_id, "proactive_casual", 3600)
            if ok:
                msg = await _call_llm_cheap(
                    f"角色今天经历：{t['event']}\n"
                    f"想起用户曾说：{t['memory']}\n"
                    f"写一条简短的主动消息（1-2句，中文，自然口语）："
                )
                await _telegram_send(agent_id, msg)
                break  # 一天最多发一条触景消息
```

---

### M3 分类消息冷却

**目标**：每类消息有独立冷却，不共用全局 `cooldown_minutes`

#### SQLite 表（memory_db.py 新增）

```sql
CREATE TABLE IF NOT EXISTS message_cooldown (
    agent_id    TEXT NOT NULL,
    category    TEXT NOT NULL,   -- casual / weather / game_check / late_night / proactive_casual / ...
    last_sent   TEXT NOT NULL,   -- ISO8601
    PRIMARY KEY (agent_id, category)
);
```

#### 工具函数

```python
async def _cooldown_check_and_set(agent_id: str, category: str, seconds: int) -> bool:
    """Returns True（可发送）并更新 last_sent；False（在冷却中）"""
    ...
```

#### 默认冷却时间

```python
COOLDOWN_DEFAULTS = {
    "casual":           3600,   # 1小时
    "weather":          86400,  # 24小时
    "game_check":       7200,   # 2小时
    "late_night":       28800,  # 8小时（不要一晚上催多次）
    "proactive_casual": 14400,  # 4小时
    "reminder":         0,      # 提醒类无冷却
}
```

---

### M4 用户环境感知配置（7.2）

**目标**：天气/路况/新闻注入带隐私控制

#### user_config 表（已有，扩展 JSON key）

```json
{
  "user_context": {
    "location": {
      "city": "Shanghai",
      "district": "",
      "timezone": "Asia/Shanghai",
      "show_location": false,
      "share_with_character": true
    },
    "commute": {
      "enabled": true,
      "routes": ["家→公司"],
      "check_frequency": "on_demand"
    },
    "data_sources": {
      "weather":  { "enabled": true },
      "traffic":  { "enabled": true },
      "news":     { "enabled": false }
    }
  }
}
```

#### memory_surface 注入扩展

```python
# 现有：🌤 天气：上海 | 晴 | 22°C
# 新增：
if show_location:
    parts.append(f"📍 位置：{city} {district}")
if traffic_enabled and commute_routes:
    traffic = await amap_route(...)   # on_demand：每次检查
    parts.append(f"🚗 路况：{traffic[:80]}")
```

---

### M5 daily_life_generate 增强

**目标**：生成时加入当日实时上下文

#### 新增 context 输入

```python
# 生成前额外 pull：
# 1. 当日天气（amap_weather）
# 2. 昨日 carry_over（已有，确认被正确传入）
# 3. 日常骨架（user occupation / habits）→ prompt 前缀
# 4. 用户状态（今日聊天最后的情绪，从 L4 捞）

sys_prompt 前缀追加:
"Character occupation: {occupation}. Usual habits: {habits}.
Today's weather: {weather}.
Yesterday's carry-over: {carry_over}."
```

#### 日常骨架配置（存 user_config）

```json
{
  "daily_skeleton": {
    "template": "freelancer",
    "wake_up": { "range": ["08:00","11:00"], "bias": "late" },
    "sleep":   { "range": ["23:00","02:00"] },
    "habits":  ["喝咖啡", "午睡", "加班"],
    "work_style": "remote"
  }
}
```

---

## 实施顺序

```
Week 1 (核心链路):
  [x] M3 分类冷却表 + _cooldown_check_and_set (memory_db.py ~30行)
  [x] M1 activity_events 表 + POST /api/activity 端点 (main.py ~60行)
  [x] M1 memory_surface 注入活动 (main.py ~20行)
  [x] M5 daily_life_generate 加天气 + carry_over 改进

Week 2 (主动消息):
  [x] M2 _check_proactive_triggers (main.py ~60行)
  [x] 接入 nightly loop after daily_life_generate
  [x] M1 推送规则引擎 (main.py ~50行)

Week 3 (配置化):
  [x] M4 user_context config 读取 + 路况注入 (show_location + commute routes)
  [ ] Admin UI：activity_rules 编辑、daily_skeleton 配置
  [ ] NPC 自动生成（auto mode，无需手动配置名字）
```

---

## 数据库变更汇总

**memory_db.py**：
```python
# 1. activity_events 表（M1）
# 2. message_cooldown 表（M3）
# 两张表都在 _ensure_p1_tables() 里 CREATE IF NOT EXISTS
```

**user_config（PostgreSQL，已有表）**：
```
新 key：user_context（JSON）
新 key：daily_skeleton（JSON）
新 key：screen_time_rules（JSON array）
```

---

## 与主记忆库的关系（保持文档约定）

| 写入方向 | 说明 |
|---------|------|
| daily_events → L1 | 单向，仅 nightly 扫描提取持久事实 |
| activity_events | 不写 palimpsest，仅注入上下文 |
| 重大事件（红色）| 手动触发写 L2/L3 |
| 周/月总结（可选）| 写 L2 |

---

## 已知边界 / 暂不做

- 节假日 API（低优先）
- Health MCP 睡眠数据（需用户搭建）
- Kimi 联网新闻（需 API key + 审核延迟）
- 场景自动切换（出差检测）→ 手动触发即可
- 红色事件用户确认弹窗 → 先跳过，用 Telegram 人工确认代替
