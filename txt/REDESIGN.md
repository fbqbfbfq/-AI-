# 微信 AI 分身 —— 记忆持久化 + 多角色 Streamlit 前端改造设计

> 版本：v0.4（设计稿，未写代码）
> 关联文档：[`DESIGN.md`](DESIGN.md)、[`MEMORY_DESIGN.md`](MEMORY_DESIGN.md)
> 参考实现：`C:\Users\14929\PycharmProjects\pythonProject\AI1`（完成度较高，主参考）、`AI2`（较新但残缺，仅借鉴思路）

---

## 0. 文档说明

本文档只做**设计**，不改代码。目标是回答清楚几件事：

1. 现有 `wechat-ai-bot` 存在哪些问题；
2. 每个问题怎么改、数据长什么样、模块怎么拆；
3. 异常处理与日志、配置完整性检查、一键安装、内存缓存双写；
4. 分几步落地、每步怎么验证。

读完本文档、确认无误后，才进入编码阶段。

---

## 1. 现状与问题清单

| # | 现状（代码位置） | 问题 | 改造方向 |
|---|---|---|---|
| 1 | L1 近期对话用 `collections.deque(maxlen=40)` 存内存（`memory.py::MemoryManager.l1`） | 进程一关，最近 10 轮上下文全部丢失，角色"失忆" | L1 改为 JSON 落盘 |
| 2 | L1 每条 `{"user","assistant"}` 无时间（`memory.py::add_turn`） | 模型不知道对话发生在什么时候 | 每条记忆加 `time` 字段 |
| 3 | 拼 prompt 时从不告诉模型"现在几点"（`bot.py::ai_reply`） | 问"现在几点/今天星期几"答不了 | 组装 system prompt 时注入当前时间 |
| 4 | L2 元数据**已有** `time`，但 `MemoryStore.search()` 只返回 `{"text","score"}`（`memory.py:109`） | 检索出来的长期记忆丢了时间，模型不知道"这是哪天的事" | 检索结果带 `time`，注入 prompt 时带上时间 |
| 5 | 总结频率语义混乱：`l1_window` 既当压缩批次又当窗口（`config.py`/`bot.py`），参考项目 AI1 是 5 轮 | 需求是"每 10 轮总结一次"，且要可配置 | 拆成独立配置项，默认 10 轮 |
| 6 | `DEFAULT_ROLE` 硬编码"花火"人设并自动写回（`config.py:58`） | 提示词被强制规定，用户无法自由编写 | 提示词完全由用户编写，默认只给占位模板 |
| 7 | 现有 `admin_ui.py` 单角色、单页签，无对话测试 | 无法方便管理"多个名字 + 多个提示词 + 配置文件" | 重写为多角色 Streamlit 前端（参考 AI1） |
| 8 | 角色配置只取 `[0]`（`bot.py::build_ai`、`bot_ilink.py::build_ai`） | 不支持多角色切换 | 增加"当前启用角色"机制 |

---

## 2. 需求拆解（逐条对应）

| 需求（用户原话要点） | 落地点 | 关键设计 |
|---|---|---|
| 近期 10 条记忆由内存改 JSON 存硬盘，关闭后不丢 | §5.1、§6.3 | `data/history/` 下按用户/角色存 `*.json` |
| 每条记忆加时间 | §5.1、§5.2 | L1、L2 每条都带 `time` |
| 发给模型时也加时间（让模型知道现在是几点） | §7.1 | system prompt 追加 `【系统信息】当前时间` |
| L2 每条记忆也加时间 | §5.2、§7.3 | 检索结果带 `time` 并注入 |
| 加一个 Streamlit 前端，管理不同名字/提示词/配置文件 | §10 | 参考 AI1 `web.py` + `main.py` 分层 |
| 记忆总结改为 10 轮总结一次（一轮=用户1条+AI1条） | §8 | `l1_summary_rounds=10`，可配置 |
| 配置文件不能强制规定提示词，用户可自行编写 | §9 | 角色提示词自由文本，默认占位模板 |
| 关键处异常处理，异常记日志（带时间、内容全） | §6.8 | `logger.py` → `logs/bot.log`，完整 traceback + 上下文 |
| 一个 chack 文件检查配置完整性 + 前端检查按钮 | §6.9、§10 | `check.py` 返回逐项报告，网页一键自检 |
| bat 自动检测 Python 版本 + 国内镜像装依赖 | §6.11 | `setup.bat`，要求 Python 3.10+ |
| 配置/记忆读入内存，新内容同时写内存+硬盘 | §6.10 | write-through 缓存，减少读盘 |

---

## 3. 总体架构（改造后）

沿用 AI1 的**"业务逻辑层 + 界面层"分离**思路，让微信机器人（`bot.py`）和 Streamlit 前端（`web_ui.py`）**共用同一套业务编排**，避免逻辑重复。

```
                    ┌─────────────────────────────────────┐
                    │            界面层                    │
                    │  bot.py(微信)      web_ui.py(网页)    │
                    └───────────────┬─────────────────────┘
                                    │ 都调用
                    ┌───────────────▼─────────────────────┐
                    │        service.py(业务编排层)        │
                    │  组装 system prompt(时间/画像/记忆)   │
                    │  检索 L2 → 拼 L1 上下文 → 调 AI → 记录 │
                    └──────┬───────────┬───────────┬───────┘
                           │           │           │
              ┌────────────▼──┐  ┌─────▼─────┐  ┌──▼──────────┐
              │  config.py     │  │ memory.py │  │ ai_client.py│
              │ 多角色CRUD/    │  │ L1落盘/L2 │  │ OpenAI兼容  │
              │ 配置读写       │  │ 检索/压缩  │  │ 对话客户端  │
              └───────┬───────┘  └─────┬─────┘  └─────────────┘
                      │                │
              ┌───────▼────────────────▼───────┐
              │  file_util.py(原子JSON读写+锁)   │
              └────────────────────────────────┘
```

- **service.py（新增）**：唯一"说话"入口。微信和网页都调它，保证时间注入、记忆检索、10 轮总结逻辑只有一份。
- **web_ui.py（新增，替代 admin_ui.py）**：只管 UI，不碰文件/API 细节，全部通过 `service.py` 和 `config.py`。
- **file_util.py（新增）**：从 AI1 `file.py` 抄来的原子写 JSON 工具，作为所有落盘的统一底层。
- **logger.py（新增）**：全局日志，所有异常/关键事件记录到 `logs/bot.log`（带时间 + 完整堆栈），横切所有层。
- **check.py（新增，对应 AI2 `chack.py`）**：配置完整性校验，供网页「检查」按钮与 bot 启动自检调用。
- **setup.bat（新增）**：检测 Python ≥3.10 并用国内镜像一键装依赖。

---

## 4. 目录结构（改造后）

```
wechat-ai-bot/
├── bot.py                # 微信机器人入口（改用 service + active_role）
├── bot_ilink.py          # iLink 版（归档，尽量不动）
├── ai_client.py          # AI 客户端（基本不变）
├── service.py            # 【新增】业务编排层：时间注入+记忆+回复
├── memory.py             # 【改造】L1 落盘 + L2 检索带时间 + 10 轮总结
├── config.py             # 【改造】多角色 CRUD + active_role
├── file_util.py          # 【新增】原子 JSON 读写（参考 AI1 file.py）
├── check.py              # 【新增】配置完整性校验（对应 AI2 chack.py）
├── logger.py             # 【新增】日志（异常/事件 → logs/bot.log）
├── web_ui.py             # 【新增】Streamlit 前端（替代 admin_ui.py）
├── admin_ui.py           # 【废弃】保留或删除，不再维护
├── setup.bat             # 【新增】检测 Python≥3.10 + 国内镜像装依赖
├── requirements.txt      # 补 streamlit 已存在；无需新增依赖
├── config/
│   ├── 系统配置文件.json   # 【扩展】新增记忆/时间/active_role 字段
│   ├── 角色配置文件.json   # 【扩展】多角色列表，提示词自由编写
│   └── 用户画像.json       # 基本不变（全局，关于"用户"本身）
├── data/
│   ├── history/           # 【新增】L1 短期记忆（JSON）
│   │   └── <user_id>/
│   │       └── <role_name>.json
│   └── vector_store/      # 【扩展】L2 长期记忆（沿用现有格式）
│       └── <user_id>/<role_name>/
│           ├── memory.json
│           └── vectors.npy
├── logs/
│   └── bot.log            # 【新增】运行日志（异常/事件，带时间+堆栈，滚动切割）
├── DESIGN.md / MEMORY_DESIGN.md / REDESIGN.md
└── docs/                  # iLink 协议资料（不动）
```

**记忆作用域说明**：L1/L2 按 `(user_id, role_name)` 两级隔离。
- 微信机器人：`user_id` = 对方微信号（wxid，沿用现有 `from_id` 清洗规则），`role_name` = 当前启用角色。
- Streamlit 网页：`user_id` = 固定 `"web"`（本地测试用户），`role_name` = 所选角色；**仅限本地单用户使用，不做多用户隔离**（代码中 `user_id` 仍作参数传入，未来可扩展）。
- 好处：切换角色不串记忆；同一角色对不同微信好友也各自独立。

---

## 5. 数据格式设计

### 5.1 L1 短期记忆（新，JSON 落盘）

存储路径：`data/history/<user_id>/<role_name>.json`

```json
[
  {
    "time": "2026-08-17 星期一 14:03:00",
    "user": "帮我写个Python脚本",
    "assistant": "唉——又是这种麻烦事……拿来我看看。",
    "summarized": true
  },
  {
    "time": "2026-08-17 星期一 14:04:10",
    "user": "你人还怪好的！",
    "assistant": "……喂喂喂，我只是顺手而已。",
    "summarized": false
  }
]
```

字段说明：

| 字段 | 类型 | 含义 |
|---|---|---|
| `time` | str | 该轮**发生时间**（用户消息时刻），格式见 §7.4 |
| `user` | str | 用户原始消息 |
| `assistant` | str | AI 完整回复（**已去除 `|||` 分隔符**的整段文本） |
| `summarized` | bool | 是否已被压缩进 L2（用于重启后恢复总结进度） |

- **滚动窗口**：默认最多保留 `l1_maxlen=40` 轮（可配置），超出的最旧记录删除；用于拼进 prompt 的是**最近 `l1_context_turns=10` 轮**。
- **持久化时机**：每轮 `add_turn` 后立即写盘（原子写，见 §6.1），保证崩溃/关闭不丢。
- **重启恢复**：加载 JSON，按 `summarized` 计算"还有多少轮未总结"，未总结轮数 ≥ 10 则补触发一次后台总结。

### 5.2 L2 长期记忆（沿用现有落盘，补充时间输出）

存储格式不变（`memory.json` + `vectors.npy`），每条 meta 已有 `id/time/text`。**改动点在检索结果**：

```python
# 现在(search 返回)
[{"text": "...", "score": 0.92}]

# 改造后(search 返回)
[{"text": "...", "time": "2026-08-17 星期一 01:42:10", "score": 0.92}]
```

注入 prompt 时带上时间（§7.3）。

### 5.3 用户画像（基本不变）

`config/用户画像.json` 结构不变（`称呼/关系/facts/prefs/traits/events/updated_at`）。它描述的是"用户"本身，与角色无关，故保持全局单文件；若未来要支持多个微信好友各自画像，再升级为按 `user_id` 分文件（本期不做，仅备注）。

### 5.4 系统配置文件（扩展字段）

`config/系统配置文件.json`：

```jsonc
{
  "active_role": "助手",                      // 【新增】当前启用角色名
  "text_model_information": { /* 不变 */ },
  "embedding_model_information": { /* 不变 */ },
  "memory_information": {
    "enabled": true,
    "l1_context_turns": 10,                   // 【新增】拼进 prompt 的最近轮数
    "l1_summary_rounds": 10,                  // 【新增】每多少轮总结一次
    "l1_maxlen": 40,                          // 【新增】L1 滚动窗口最大轮数
    "l2_top_k": 5,                            // 不变
    "vector_store_path": "data/vector_store", // 不变
    "history_path": "data/history"            // 【新增】L1 落盘目录
  },
  "system_information": {
    "version": "0.4.0",
    "language": "zh-CN",
    "log_level": "ERROR",                      // 只记报错（见 §6.8），可调
    "segmented_reply": true,                  // 是否按分隔符分段发送（不注入提示词）
    "segment_separator": "|||",               // 分段分隔符（与角色提示词约定一致）
    "time_format": "%Y-%m-%d %A %H:%M:%S",    // 【新增】时间显示格式(带星期)
    "inject_time": true,                      // 【新增】是否注入当前时间
    "inject_profile": true,                   // 【新增】是否注入用户画像
    "inject_memory": true                     // 【新增】是否注入相关记忆
  }
}
```

> 说明：把 `inject_*` 开关放出来，是为了呼应"不能强制规定"——除了角色提示词自由编写外，画像/记忆/时间这些**系统附加段**也都能独立关闭，不强迫用户接受任何一段。分段（`|||`）不再作为系统附加段，完全由角色提示词自行约定（见 §6.12）。

### 5.5 角色配置文件（多角色，提示词自由编写）

`config/角色配置文件.json`：

```json
[
  { "name": "助手", "prompt": "你是一个乐于助人的 AI 助手，用简洁友好的语气回答。" },
  { "name": "花火", "prompt": "（用户自己粘贴/编写的任意提示词）" }
]
```

- **每个角色只有 `name` + `prompt` 两个必填字段**，提示词是用户自由编写的纯文本，无任何强制的模板/后缀。
- **默认值**：文件缺失时只生成**一个占位角色**（name=`助手`、prompt=`"你是一个乐于助人的 AI 助手，用简洁友好的语气回答。"`），**不再硬编码"花火"人设**，也不再自动写回。
- 若用户需要"花火"这个示例，由用户自己在网页里粘贴，或保留在文档附录里供复制（见 §15），但**系统不强制**。

---

## 6. 各模块改造设计

### 6.1 file_util.py（新增，参考 AI1 `file.py`）

从 AI1 `file.py` 原样借鉴以下能力，作为统一落盘底层：

- `atomic_save_json(directory, file_name, data, indent=4)`：先写临时文件，再 `os.replace` 原子替换，避免写一半被读到。
- `save_json(...)`：加文件级线程锁 + 原子写。
- `load_json(directory, file_name, default)`：加锁读取，文件不存在则创建并返回默认值。
- `safe_load_json(directory, file_name, default)`：**损坏自愈读取**——文件存在但 `JSONDecodeError`/结构不对时，删除坏文件、按默认值重新生成，写日志告警并返回默认值（供启动自检使用，见 §6.2）。
- `find_index(data, key, value)`：按 name 查角色索引。

> 这是 AI1 里最成熟、可直接复用的部分，改造后 `config.py`、`memory.py` 的落盘都走它，彻底替换现在 `config.py`/`memory.py` 里各自手写的裸 `json.dump/open`。
>
> **锁的边界**：只用 `threading.Lock`（单进程内线程安全）。本项目 Streamlit 仅作管理前端、微信 bot 独立运行，两进程不同时写同一文件，暂不引入跨进程锁（`filelock`）；若日后加入前端对话/工具调用导致并发写，再升级（见 §6.10）。

### 6.2 config.py（多角色 CRUD + active_role）

在现有基础上扩展：

```python
load_system() / save_system(cfg)
load_roles()  -> list[dict]            # 改为返回整个列表
save_roles(roles)
get_role(name) -> dict | None
add_role(name, prompt)                  # 重名校验
update_role(name, prompt)
delete_role(name)                       # 允许删除 active_role，删除后回退到第一个
get_active_role() -> dict               # 缺失→回退第一个，列表为空→抛"需要一个角色"
set_active_role(name)
load_profile() / save_profile(prof)
repair_corrupted_files()                # 启动自检：坏文件删除+重建+日志+提示
reload_if_changed()                     # 热切换：文件 mtime 变化才重读，供 bot 每轮调用
build_system_prompt(role, profile, now_str=None, memories=None, ...)  # 组装最终 system
```

关键变化：
1. `DEFAULT_ROLE` 从"硬编码花火"改为"占位助手"；`load_role()` 不再 `[0]`，改多角色。
2. `build_system_prompt` 升级为**可插拔组装器**：按 `inject_time/inject_profile/inject_memory` 开关逐段拼接（见 §7）；分段指令不再由系统注入，改由**角色提示词自行书写**。
3. 增加**内存缓存 + 双写**：`load_*` 读缓存、`save_*` 先改内存再落盘（见 §6.10）。
4. **全部配置支持热切换**：`active_role`、模型参数、`inject_time`、`l1_summary_rounds` 等所有开关，改动后无需重启即生效（`reload_if_changed()` 每轮校验文件 mtime，变化才重读）。
5. **删除角色回退**：允许删除当前 `active_role`，删除后自动回退到列表第一个角色；若角色列表为空，启动/使用时明确提示"你需要一个角色"。
6. **损坏自愈**：启动时 `repair_corrupted_files()` 检测所有配置/记忆文件，坏文件删除并按默认值重建，写日志并提示"文件损坏已重新生成"。

### 6.3 memory.py（L1 落盘 + 时间 + 10 轮总结）

新增 `L1Store` 类（持久化短期记忆）：

```python
class L1Store:
    def __init__(self, path, maxlen=40):
        # 加载或初始化 JSON 列表
    def add(self, user_text, assistant_text, now_str):   # 追加 + 立即落盘
    def recent(self, n=10) -> list[dict]                 # 取最近 n 轮
    def unsummarized(self) -> list[dict]                 # 未总结的轮
    def mark_summarized(self, count):                    # 标记前 count 条已总结
    def clear(self)
```

`MemoryManager` 改造点：

| 项 | 现在 | 改造后 |
|---|---|---|
| L1 容器 | `collections.deque`（内存） | `L1Store`（JSON 落盘） |
| 每条时间 | 无 | `add_turn` 时写入 `now_str` |
| 总结批次 | `batch = l1_window`（语义混乱） | `batch = l1_summary_rounds`（默认 10） |
| 触发条件 | `len(l1)-compressed_upto >= batch` | 未总结轮数 `>= batch`（等价逻辑，但持久化） |
| 重启恢复 | 无（全丢） | 加载 L1，未总结 ≥ batch 则补触发总结 |
| 检索结果 | `{"text","score"}` | `{"text","time","score"}` |

`Compressor` 改造点：`_dialogue_text()` 每条前加时间前缀，让压缩器知道"这是什么时候的事"（§7.3）。

`ProfileUpdater`：基本不变；触发频率沿用现有 `profile_batch=2`（每攒 2 条新 L2 更新一次画像）。

> 落盘统一走 write-through：`L1Store`/`MemoryStore` 始终**内存持有数据 + 同步落盘**，读用内存、写双写（见 §6.10）。

### 6.4 service.py（新增，业务编排层）

参考 AI1 `main.py` 的 `send_message`，抽成一个无 UI 依赖的编排函数：

```python
def current_time_str(fmt) -> str:
    return time.strftime(fmt)

def chat_once(role_name, user_id, user_text) -> str:
    # 1. 取角色 + 用户画像
    # 2. 注入当前时间
    # 3. 检索 L2（含时间）→ 拼"相关记忆"段
    # 4. 取 L1 最近 N 轮（含时间）→ 拼上下文
    # 5. 组装 system + messages，调 ai_client
    # 6. 记录本轮：assistant 存"去除 ||| 的完整回复"（add_turn，触发后台 10 轮总结）
    # 7. 返回回复（原样，是否含 ||| 由调用方决定）
```

- `user_id` 由调用方传入：微信传 wxid，网页传 `"web"`（本地单用户）。
- 时间注入、记忆检索、10 轮总结**只在这里实现一次**，`bot.py` 和 `web_ui.py` 都调用它。
- **热切换**：每轮开头调 `config.reload_if_changed()`，用最新的 `active_role`/模型参数/开关构造或更新 `AIClient`，确保网页保存后即时生效。
- **分段与记忆分离**：分段发送由调用方负责（bot 逐段发、web_ui 直接展示整段），记忆存储统一用去除 `|||` 的完整文本（见 §6.12）。

### 6.5 ai_client.py（基本不变）

`AIClient.chat/complete` 无需改动。时间、记忆、画像都在 `service.py` 层拼好再传进来。

### 6.6 bot.py（接入 service + active_role）

- 删除 `ai_reply()` 里手写的记忆检索/prompt 拼装，改为调用 `service.chat_once(active_role, sender, text)`。
- `build_ai()` 改用 `config.get_active_role()` 取当前角色。
- `make_manager()` 的 `store_dir` 加 `role_name` 一级；L1 落盘目录按 `(user_id, role_name)`。
- 启动时：初始化日志 → `config.repair_corrupted_files()`（坏文件删除+重建+日志+提示）→ `check.py` 自检（缺 apikey 才退出，其余仅告警；无角色则提示"你需要一个角色"）。
- **热切换**：每条消息处理前调用 `config.reload_if_changed()`，让网页改的 `active_role`/模型/开关即时生效，无需重启。
- AI 调用失败：统一兜底回复"API 调用失败，请稍后重试"。
- 分段发送（`send_reply`）：按 `segment_separator` 切分 → 逐段发送，段间停顿按该段长度单独计算（`max(1.5, min(4.0, 1.0 + len/8))` 秒），首段失败降级整条发送。

### 6.7 web_ui.py（新增，参考 AI1 web.py + main.py）

页面结构见 §10。核心原则：
- **UI 不直接操作文件/API**，全部通过 `config.py`（配置 CRUD）和 `service.py`（对话）。
- 会话状态用 `st.session_state` 管理（角色列表、当前角色、消息列表），模式照搬 AI1 `web.py`。

### 6.8 logger.py（新增，全局日志）

项目要公开给他人（含无代码基础用户）使用，**关键路径必须做异常处理，并把异常写进带时间的文本日志**，方便排查。

- 技术：Python 标准库 `logging` + `FileHandler`（**追加写**），落盘到 `logs/bot.log`。
- **只记报错、永不删旧日志**：仅 ERROR/WARN 级问题（异常、文件损坏等）写日志，正常流程不写；**不切割、不轮转、不删除历史报错**，报错一直追加累积。
- 格式：`[2026-08-17 星期一 14:03:00] [ERROR] [module.func] 消息` + 下一行完整 `Traceback` + 关键上下文（user_id / role / 消息前 N 字）。
- 辅助函数：
  - `setup_logger()`：初始化（bot / web_ui 启动时调用）。
  - `log_exception(msg, e, **context)`：记录完整异常堆栈 + 上下文，**不中断主流程**。
- **必须接日志的关键位置**：AI 调用、embedding、压缩、画像更新、配置文件读写、L1/L2 落盘、check 自检、bot 收/发消息。
- 兜底策略：AI 调用失败统一回复"API 调用失败，请稍后重试"，但错误必须落日志；无代码用户报障时只需提供 `logs/bot.log` 尾部即可。

### 6.9 check.py（新增，对应 AI2 `chack.py`）

借鉴 AI2 `chack.py` 的 SCHEMA + 类型校验思路，但把"抛异常"改成"返回逐项检查报告"，便于前端展示、也更适合无代码用户自诊。

```python
def check_all() -> list[dict]:
    # 返回 [{"level": "ok|warn|error", "item": "...", "message": "...", "detail": "..."}]
```

检查项（即"生成 AI 内容所需的完整性"）：

| # | 检查项 | 通过标准 |
|---|---|---|
| 1 | 文本模型 | `name`/`apikey`/`base_url` 非空、类型正确 |
| 2 | embedding | `name`/`apikey`/`base_url` 非空（仅当记忆 `enabled` 时必需，否则 warn） |
| 3 | 角色配置 | 列表为空 → error「你需要一个角色」；`active_role` 缺失 → 回退第一个并 warn；当前角色 `prompt` 非空 |
| 4 | 用户画像 | 可正常解析为 dict，关键字段存在 |
| 5 | 路径 | `vector_store_path`/`history_path`/`logs` 可创建、可写 |
| 6 | 依赖 | `requests`/`numpy`/`streamlit` 可正常 import |
| 7 |（可选）连通性 | 对 `base_url` 发 `GET`（或轻量请求），超时 5 秒 |
| 8 | 分段分隔符 | `segmented_reply=true` 但 `segment_separator` 为空/纯空格 → warn |

调用方：`web_ui.py` 的「检查配置」按钮（§10）、`bot.py` 启动时自检（发现问题打印/写日志但不退出；缺 apikey 或角色列表为空时才退出，无角色提示"你需要一个角色"）。

### 6.10 内存缓存 + 硬盘双写（write-through）

目标：**减少直接读硬盘次数**，配置与记忆都"读内存、写双写"。

- **config 层**：`load_system/load_roles/load_profile` 首次读盘后缓存到模块级字典，之后直接返回缓存；`save_*` 先更新内存、再原子写盘（write-through）。提供 `reload_all()`（前端「刷新」按钮调用）和 `reload_if_changed()`（每轮校验文件 mtime，变化才重读，支撑 §6.2 的"全部配置热切换"）。
- **memory 层**：`L1Store` / `MemoryStore` 本来就是"内存持有 + 落盘"，明确为 write-through 语义——`add()` 同时更新内存和写盘，`search()/recent()` 只读内存。
- **一致性边界**：单进程内线程安全（复用 `file_util` 的 `threading.Lock`）。Streamlit 仅作管理前端、微信 bot 独立运行，两进程不同时写，**暂不引入跨进程锁**；若日后加入前端对话/工具调用导致并发写，再升级 `filelock`。

### 6.11 setup.bat（新增，一键环境）

面向无代码基础用户，双击即可完成环境准备：

1. `chcp 65001`：切 UTF-8，保证中文提示不乱码。
2. 按 `py -3` → `python` → `python3` 顺序探测命令，取第一个可用的。
3. 解析版本号：**硬性要求 Python ≥ 3.10**（建议最新稳定版）；主版本 < 3 或（==3 且次版本 <10）→ 中文提示"本项目需要 Python 3.10 及以上版本，请到 python.org 安装最新版"，给出下载地址并退出。
4. `pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`（清华镜像，可加 `--upgrade pip`）。
5. 装完提示成功；失败给出中文排查提示（网络/代理/权限），并建议重试或换阿里镜像。
6. 结尾 `pause` 停留，避免窗口一闪而过。

### 6.12 分段回复与记忆存储（`|||` 机制）

- **分段指令由角色提示词书写**：提示词里要求 AI 用 `|||` 把回复分成若干段（分几段以提示词为准），系统不再注入"回复格式"。
- **发送**：bot 拿到回复后按 `segment_separator`（默认 `|||`）切分、去空段，**逐段发送**；段与段之间停顿**按该段内容长度单独计算**（`max(1.5, min(4.0, 1.0 + len(段)/8))` 秒），模拟真人打字；首段发送失败自动降级为整条发送。
- **记忆存储**：无论是否分段，L1 里存的 `assistant` 都是**去除 `|||` 后的完整一段文本**，保证记忆干净、后续压缩/检索不含分隔符。
- 分段与否由 `segmented_reply` 控制；`segment_separator` 可配置（须与角色提示词约定一致，`check.py` 会校验非空，见 §6.9）。
- web_ui 对话测试页直接展示去除 `|||` 的整段文本，不做模拟打字。

---

## 7. 时间注入设计（重点）

### 7.1 当前时间（让模型知道"现在是几点"）

参考 AI1 `main.py` 的写法，在 system prompt 追加：

```
【系统信息】当前时间：2026-08-17 星期一 14:03:00。如果用户问现在几点/今天几号/星期几，请据此回答。
```

- 由 `service.chat_once` 每轮动态生成（`current_time_str`），**不是启动时写死**。
- 受 `inject_time` 开关控制。

### 7.2 L1 每条记忆时间

- 落盘：`add_turn` 写入 `time`（§5.1）。
- 拼进 prompt：最近 N 轮上下文按如下格式（时间放行首）：

```
[2026-08-17 星期一 14:01:00] 用户: 帮我写个Python脚本
[2026-08-17 星期一 14:01:12] AI: 唉——又是这种麻烦事……拿来我看看。
```

- 这样模型既知道"现在几点"，也知道"前面几句话是什么时候说的"。

### 7.3 L2 每条记忆时间

- 检索：`MemoryStore.search()` 返回 `time`（§5.2）。
- 注入 prompt：

```
## 相关记忆（长期）
- (2026-08-17 星期一 01:42) 用户是 AI 花火的开发者，计划添加多模态和工具调用功能。
```

- 压缩阶段：`_dialogue_text()` 给每轮加时间前缀，让压缩器生成的要点能保留时间线索（压缩出的要点本身由 `MemoryStore.add` 统一盖当前时间戳）。

### 7.4 时间格式与开关

- 默认格式：`%Y-%m-%d %A %H:%M:%S`（24 小时制 + 星期，可配置 `time_format`；该格式**统一用于**当前时间注入与 L1/L2 时间戳）。
- 是否注入当前时间：`inject_time`（默认 true）。
- L1/L2 的 `time` 字段**始终落盘**（与注入开关无关），注入开关只决定是否把这些时间拼进 prompt。

> 注意：`%A` 输出的是**当前系统 locale** 的星期名（英文系统可能输出 `Monday`）。为保证中文"星期一"，实现时不直接依赖 `%A`，而是用 `datetime.weekday()` 手动映射到 `周一`~`周日`；若 `time_format` 里含 `%A`，也按此映射替换。

### 7.5 最终 system prompt 形态（示意）

```
{角色提示词 —— 用户自由编写的纯文本，可自行加入"用 ||| 分段"等输出格式要求}

【系统信息】当前时间：2026-08-17 星期一 14:03:00。如果用户问现在几点/星期几，请据此回答。

## 用户画像
称呼：阿伟
关系：朋友
事实：用户是 AI 花火的开发者

## 相关记忆（长期）
- (2026-08-17 星期一 01:42) 用户计划修改 AI 的记忆系统和提示词。
```

> 系统只注入"当前时间 / 用户画像 / 相关记忆"三段（各由 `inject_*` 开关控制）；**分段指令不再由系统注入**，由角色提示词自行书写。`segmented_reply`/`segment_separator` 仅控制"是否按分隔符分段发送"，不进入 prompt。

---

## 8. 记忆总结：10 轮一次（重点）

**轮的定义**：用户发 1 条消息 + AI 回 1 条消息 = **1 轮**（即 L1 里的一条记录）。

| 项 | 值 |
|---|---|
| 总结频率 | **每 10 轮**（`l1_summary_rounds = 10`，可配置） |
| 触发 | 未总结轮数 ≥ 10，且后台空闲时 |
| 压缩方式 | 沿用现有"快照 + 提交后清理"异步模型，**不阻塞回复** |
| 重启恢复 | 加载 L1 JSON，按 `summarized` 标记补总结未处理的轮；未总结轮一次性交给后台压缩（异步、不阻塞），不额外处理分批/上限 |
| 与上下文窗口关系 | `l1_context_turns`（拼 prompt 用，默认 10）与 `l1_summary_rounds`（总结用，默认 10）**分离**，互不干扰 |

> 澄清一处现状：当前 `config.py` 里 `l1_window` 默认是 10，看似"已经是 10 轮"，但它同时被当作"压缩批次"和"窗口"使用，语义混乱且**不持久化**；本次按上面的表把它彻底拆开、固定语义、落盘。

---

## 9. 提示词自由编写（重点）

1. **角色配置文件里的 `prompt` 是纯自由文本**，系统不追加、不强制任何固定人设。
2. **默认只给一个占位角色**（`助手` + 一句通用提示词），不再自动写入"花火"人设。
3. **网页端用 `text_area` 自由编辑**，支持新增/改名/删除（§10）。
4. **系统附加段可关闭**：画像、相关记忆、当前时间均提供开关，用户嫌哪段多余就关掉，不被强制。
5. 现有配置文件里已有的"花火"内容**不会被删除**，只是不再作为强制默认；用户可在网页里继续编辑或删除。
6. **提示词长度不限**：不设前后端长度上限，填多长是用户的自由（token 消耗由用户自行把握）。

---

## 10. Streamlit 前端页面设计（web_ui.py）

参考 AI1 `web.py`（侧边栏管理角色 + 主区对话）的交互模式，扩展出配置与记忆管理。

### 布局

```
┌─────────────── 侧边栏(sidebar) ───────────────┐   ┌──────────────── 主区 ────────────────┐
│ 🤖 角色列表                                    │   │   💬 与「当前角色」对话（测试）       │
│   [助手]  ✏️ 🗑️                                │   │   历史消息气泡 + 底部输入框            │
│   [花火]  ✏️ 🗑️                                │   │                                      │
│   ────────────                                │   │   （选中角色后，可直接发消息测试      │
│   ➕ 新建角色（名称 + 提示词 text_area）        │   │    提示词效果、验证时间/记忆注入）     │
│   ────────────                                │   │                                      │
│   ⚙️ 工具                                      │   │   页签（主区上方可切换）：             │
│   [清空当前角色对话记忆]                        │   │   [对话] [角色/提示词] [系统配置]      │
│   [刷新角色列表]                                │   │        [记忆/画像管理]                │
└───────────────────────────────────────────────┘   └──────────────────────────────────────┘
```

### 页签 1：对话（测试）

- 侧边栏选择角色 → 主区加载该角色历史（来自 L1 落盘），用 `st.chat_message` 展示。
- 底部 `st.chat_input` 发送，走 `service.chat_once(role_name, "web", text)`。
- 每条消息旁可显示其时间戳（可视化验证 §7）。

### 页签 2：角色 / 提示词

- 角色列表 + 新建/编辑/删除（AI1 `web.py` 的表单模式，含重名校验）。
- 编辑区：角色名（`text_input`）+ 提示词（`text_area`，高度 ≥ 300px，纯自由文本，**长度不限**）。
- 删除角色时：连带删除该角色的 L1 历史与 L2 向量库（二次确认）；若删的是当前 `active_role`，自动回退到列表第一个角色；删空则提示"你需要一个角色"。
- **当前启用角色**：一个下拉/单选，写回 `系统配置文件.json` 的 `active_role`，保存后**即时生效**（热切换，无需重启 bot）。

### 页签 3：系统配置

- **🔍 检查配置完整性**：页签顶部放一个「检查配置」按钮，点击调用 `check.py::check_all()`，逐项显示 ✓/⚠️/✗ + 中文提示（缺 apikey、缺提示词、路径不可写等一眼可见）。
- **保存即热切换**：本页任何一项保存后，都弹提示"已保存并即时生效（微信 bot 无需重启）"。
- **模型配置**：文本模型 name/apikey/base_url/temperature/max_tokens/top_p/timeout；embedding name/apikey/base_url/维度（密钥脱敏显示，可改后保存）。
- **记忆配置**：`enabled`、`l1_context_turns`、`l1_summary_rounds`、`l1_maxlen`、`l2_top_k`、两个路径。
- **系统设置**：`segmented_reply`、`segment_separator`、`time_format`、`inject_time/inject_profile/inject_memory`。

### 页签 4：记忆 / 画像管理

- **用户画像**：称呼/关系 + 四个列表编辑（一行一条），保存。
- **短期记忆(L1)**：查看当前角色最近记录（含时间），支持清空。
- **长期记忆(L2)**：查看条目（含时间），支持单条删除 / 全部清空。
- 危险操作（清空）均二次确认。

---

## 11. 兼容性与迁移

| 对象 | 处理 |
|---|---|
| 旧 `config/角色配置文件.json`（含花火） | 直接当多角色列表读，不改动；用户可在网页里继续用或删 |
| 旧 `data/vector_store/<user_id>/memory.json` | **不做迁移**（项目尚未发布、无历史角色记忆）；旧 `<user_id>/` 结构若存在则忽略 |
| 旧 `data/vector_store/<user_id>/vectors.npy` | 不动 |
| L1（原内存 deque） | 无历史文件，改造后自然从空开始，逐步累积落盘 |
| `admin_ui.py` | 废弃，功能由 `web_ui.py` 承接 |
| `requirements.txt` | 已含 streamlit/numpy/requests，无需新增 |

---

## 12. 实施步骤（分阶段）

| 阶段 | 内容 | 依赖 |
|---|---|---|
| **R1** | 新增 `file_util.py`（原子 JSON 读写，抄 AI1 `file.py`） | 无 |
| **R2** | 新增 `logger.py`（日志：异常/事件 → `logs/bot.log`，带时间+堆栈） | 无 |
| **R3** | 改造 `config.py`：多角色 CRUD + active_role + 占位默认角色 + 新配置字段 + 内存缓存双写 | R1、R2 |
| **R4** | 改造 `memory.py`：`L1Store` 落盘 + L2 检索带时间 + 10 轮总结 + 重启恢复 + 内存缓存双写 | R1、R2 |
| **R5** | 新增 `service.py`：时间注入 + 记忆编排 + 对话入口（异常全走日志） | R3、R4 |
| **R6** | 新增 `check.py`：配置完整性校验（对应 AI2 `chack.py`） | R3 |
| **R7** | 改造 `bot.py`：接 service + active_role + 启动自检 | R5、R6 |
| **R8** | 新增 `web_ui.py`：四页签前端 + 「检查配置」按钮 | R5、R6 |
| **R9** | 新增 `setup.bat`：Python 版本检测 + 国内镜像装依赖 | 无 |
| **R10** | 端到端联调 + 补文档（README/DESIGN） | R7、R8 |

建议顺序：R1 → R2 →（R3 与 R4 可并行）→ R5 →（R6/R7/R8/R9 可并行）→ R10。

---

## 13. 验证方式

| 需求 | 验证 |
|---|---|
| L1 落盘不丢 | 聊几轮 → 关闭进程 → 重启，`data/history/<user>/<role>.json` 存在且能加载，最近上下文还在 |
| 每条记忆有时间 | 打开 L1 JSON 与 L2 `memory.json`，每条都有 `time` |
| 发给模型的时间 | 网页/微信问"现在几点/今天几号"，模型按注入时间回答；临时改系统时间验证 |
| L2 时间注入 | 问之前聊过的事，回复能体现"那是（某天）的事"；日志里 `相关记忆` 段带时间 |
| 10 轮总结一次 | 连聊 10 轮（每轮用户+AI），观察第 10 轮后触发一次压缩、L2 新增条目；第 11 轮不丢消息、不卡顿 |
| 多角色 | 网页新建"角色A/角色B"不同提示词，切换后回复风格不同，记忆互不串 |
| 提示词自由编写 | 网页把 prompt 改成任意内容，保存后立即生效，无被系统改回/追加的现象 |
| 配置管理 | 网页改模型/记忆/系统配置，保存后微信 bot 重启读到的就是新值 |
| 异常与日志 | 人为制造一次错误（如填错 apikey），`logs/bot.log` 出现带时间 + 完整堆栈的记录 |
| 配置检查 | 网页点「检查配置」按钮，缺 apikey/提示词/角色时逐项标红并给中文提示 |
| 一键安装 | 双击 `setup.bat`：Python<3.10 被拦截提示；≥3.10 自动走国内镜像装依赖 |
| 内存缓存 | 改配置后不重读盘立即可读到新值；重启后从盘加载一致；读操作不再频繁触盘 |
| 配置热切换 | 网页改 active_role/模型/开关后，微信 bot 不重启即用新值回复 |
| 损坏自愈 | 手动把某个 JSON 改成非法内容 → 重启 → 坏文件被删除重建、日志有记录、界面有提示 |
| 兜底回复 | 断开网络/填错 key 发消息 → 收到"API 调用失败，请稍后重试"，且日志有完整报错 |
| 分段发送 | 角色提示词要求用 `|||` 分段时，回复按段逐条发送、段间按每段长度停顿；首段失败降级整条 |
| 记忆存整段 | 分段回复落进 L1 的 `assistant` 是去除 `|||` 的完整文本，不含分隔符 |

---

## 14. 决策记录（已确认）

| 事项 | 决定 |
|---|---|
| 网页端对话测试页签 | ✅ 保留（四页签：对话 / 角色提示词 / 系统配置 / 记忆画像） |
| 当前启用角色 | ✅ 用 `系统配置文件.json` 的 `active_role` 字段承载 |
| 时间格式 | ✅ `%Y-%m-%d %A %H:%M:%S`（24 小时制 + 星期，如 `2026-08-17 星期一 14:03:00`） |
| 配置热切换 | ✅ 全部配置（含 active_role/模型参数/开关）改后即时生效，无需重启 |
| 损坏文件 | ✅ 启动自检：坏文件删除 + 按默认重建 + 写日志 + 提示"文件损坏已重新生成" |
| 删除当前角色 | ✅ 允许删除，回退到第一个角色；删空则提示"你需要一个角色" |
| 旧 L2 数据迁移 | ✅ 不做迁移（尚未发布、无历史角色记忆） |
| 跨进程锁 | ✅ 暂不引入 filelock，Streamlit 仅管理、bot 独立运行 |
| 多用户 | ✅ 仅本地单用户，user_id 作参数预留扩展 |
| AI 兜底回复 | ✅ 统一"API 调用失败，请稍后重试" |
| 重启未总结轮数 | ✅ 一次性后台压缩，不额外分批 |
| Python 版本 | ✅ 硬性要求 ≥ 3.10，建议最新 |
| 日志策略 | ✅ 只记报错，追加写、永不删旧日志 |
| 提示词长度 | ✅ 不限长，用户自由 |

> 补充确认（已定稿）：记忆总结按 **10 轮**（一轮 = 用户 1 条 + AI 1 条）；分段由角色提示词驱动、以 `|||` 为分隔线逐段发送、每段间隔按长度单独计算，记忆存储为去除 `|||` 的完整文本。

---

## 15. 附录：参考 AI1/AI2 的关键模式

| 参考文件 | 借鉴内容 |
|---|---|
| AI1 `file.py` | `atomic_save_json` / `save_json` / `load_json` / `find_index`（原子写 + 线程锁）→ 本项目 `file_util.py` |
| AI1 `main.py` | 业务逻辑层封装（角色 CRUD、历史、`send_message` 里注入 `当前时间`、话题检索注入记忆）→ 本项目 `service.py` |
| AI1 `web.py` | Streamlit 侧边栏角色管理（列表 + 新建/编辑/删除 + 重名校验）+ 主区 `st.chat_message` 对话 → 本项目 `web_ui.py` |
| AI1 `history_file.py` | 按角色名存 `角色名.json` 的历史文件、`max_length` 滚动 → 本项目 L1 落盘 |
| AI1 `prepare_the_files.py` | `系统配制文件/角色配制文件` 的 init/update/load/save/delete → 本项目 `config.py` 多角色 CRUD |
| AI2 `prepare_file.py` | 用类封装 `SystemPrepareFile`/`CharacterPrepareFile`（更结构化，但内容残缺）→ 仅借鉴"类封装 + 校验"思路 |
| AI2 `chack.py` | 配置结构校验（SCHEMA + 类型检查）→ 本项目 `check.py`（配置完整性校验 + 前端检查按钮） |
| AI2 `history_file.py` | `{"user","assistant","number_of_conversations"}` 的轮次计数思路 → 对应本设计的 `summarized` 进度标记 |

> AI1 完成度最高，作为主要参照；AI2 较新但残缺（`history_file.py`、`prepare_file.py` 均未写完），只取其"用类封装 + 配置校验"的改进方向，不照搬代码。
