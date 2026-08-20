# 微信 AI 分身 —— 项目模块说明与整体思路

> 版本：v0.5（对应当前代码；本文档是「模块作用 + 整体思路」的唯一主文档）
> 本文档回答三件事：**① 这个项目是干什么的；② 整体思路怎么转起来；③ 每个模块的详细作用**。
> 更细的设计历史见：[`REDESIGN.md`](REDESIGN.md)（改造规范）、[`DESIGN.md`](DESIGN.md)、[`MEMORY_DESIGN.md`](MEMORY_DESIGN.md)、[`UPGRADE_DESIGN.md`](UPGRADE_DESIGN.md)（v2 升级设计：情绪/动态温度/工具/主动对话）。

---

## 0. 这个项目是干什么的

一句话：**扫码后，在你微信里得到一个 AI 机器人联系人，私聊它就能对话**，且带「长期记忆(ChromaDB 向量库,按话题分类) + 用户画像 + 多角色人设 + 情绪系统（动态温度）+ 工具调用(可自定义白名单) + 主动对话 + 网页管理(模型预设一键适配多厂商)」。

- 底层走**腾讯官方 iLink 接口**：不需要 Hook、不需要特定微信版本，扫码授权即可用，易于分发和维护。
- 记忆分三层：短期原文（L1，仅保留近 10 轮）、长期要点（L2 存 **ChromaDB**，HNSW 向量检索，带**话题/重要度/情绪/效价**）、用户画像（只记长期不变信息，常驻提示词），全部落盘不丢。
- **情绪系统**：推断 AI 自己的情绪，用情绪决定下一次说话的 temperature；检索 L2 时也算记忆唤起情绪（灵梦桌宠思路）。
- **工具调用**：`tools/` 白名单安全工具（联网搜索/计算器/时间查询，带介绍）+ 网页 JSON 维护的**自定义白名单工具**，AI 按需调用；可开启"AI 生成代码"（沙箱执行，默认关闭，红字警告）。
- **主动对话**：定时/提醒解析/周期性主动发消息（间隔与时间段可自定义），话题取自高重要度记忆且不重复。
- 用 **Streamlit 网页**管理「角色 / 提示词 / 模型配置(多厂商预设) / 记忆 / 工具 / 情绪 / 主动对话」，配置改完即时生效，无需重启机器人。

---

## 1. 整体思路

### 1.1 一句话版

```
微信消息 ──► 检索相关记忆(带情绪) ──► 情绪结算 → 动态温度
        ──► 组装 system prompt(人设 + 当前时间 + 画像 + 记忆 + 当前情绪)
        ──► 拼最近几轮对话 ──► 工具循环(白名单工具/网络搜索) ──► 调大模型
        ──► 按 ||| 分段逐条发送(模拟打字)
        ──► 把本轮(去除 ||| 的整段)写进记忆 ──► 每 10 轮后台压缩(去重)一次
另:调度线程定时/周期性主动发消息(高重要度话题,防重复)
```

核心原则：**一个业务编排层（service.py），两个入口（微信机器人 + 网页）共用**，保证「情绪结算、动态温度、时间注入、记忆检索、10 轮总结、工具循环、配置热切换」等逻辑只写一份、不重复。

### 1.2 每轮对话的主管线（普通聊天，详细版）

```
用户发来消息
  │
  ▼
① 检索 L2 相关记忆（带重要度/情绪）──► 算「记忆唤起情绪」
  │
  ▼
② 分析模型推断「用户这条消息唤起的 AI 情绪」
  │  情绪结算 = 自然衰减 + 记忆唤起×0.3 + 消息情绪×0.7
  │  动态温度 =（配置文件温度 + 情绪温度）/ 2     ← 情绪决定下一次说话的温度
  ▼
③ 组装 system prompt：人设 + 当前时间 + 画像 + 相关记忆 + 【当前情绪】
  │
  ▼
④ 拼最近 N 轮 L1 上下文 + 当前消息
  │  工具循环：白名单安全工具 + 分析模型判断"是否联网搜索"
  ▼
⑤ 调对话模型（动态温度 + function calling 工具循环）
  │
  ▼
⑥ 回复按 ||| 分段逐条发送（模拟打字）；记忆存去除 ||| 的整段
  │
  ▼
⑦ 记录本轮进 L1 → 每 10 轮后台压缩成 L2（相似度去重）；关键词命中"提醒"则解析并登记
```

### 1.3 后台常驻的两条线程

| 线程 | 干什么 |
|---|---|
| **压缩线程**（`MemoryManager` 内） | 未总结轮数 ≥ 10 时，后台异步把 L1 压缩成 L2 并增量更新画像；**失败不标记、重启持续补压**，不阻塞回复 |
| **调度线程**（`scheduler.py`） | 每 30 秒检查：① 周期性主动发消息（间隔/时间段可配置，只对 token 新鲜的已知用户发）；② 提醒到点发送（token 过期则跳过等下次） |

### 1.4 核心设计原则

1. **一个编排层，两个入口**：微信机器人（`bot_ilink.py`）和网页（`web_ui.py`）都调 `service.py`，情绪/温度/记忆/工具/时间注入逻辑只写一份。
2. **记忆不丢**：L1/L2/情绪态全落盘；压缩任一步失败都不标记"已总结"，下次自动重试。
3. **每条记忆带时间，模型知道"现在几点"**：当前时间注入 system，L1/L2 记忆带时间戳。
4. **L2 有选择性**：像人一样提取记忆——检索按「0.7×相似度 + 0.3×动态重要度」排序，重要度带 14 天半衰期；写库前相似度去重，高度重叠就更新旧条目并重算重要度。
5. **情绪 → 温度**：情绪指 **AI 自己的情绪**（根据上下文推断），用情绪决定下一次说话的 temperature；检索 L2 时也算记忆唤起情绪反哺当前情绪（灵梦桌宠思路）。
6. **工具折中方案**：默认只允许白名单安全工具（`safe=true`）；"AI 生成代码"默认关闭，开启需红字警告确认，且在受限沙箱执行、日志留代码快照。
7. **主动对话是机会式的**：iLink 的 `context_token` 只能从对方发来的消息里拿到、且会过期，主动发前必须检查新鲜期；话题取自高重要度记忆并自动轮换不重复。
8. **10 轮总结一次**：一轮 = 用户 1 条 + AI 1 条；`l1_summary_rounds=10`，后台异步、不阻塞回复，重启持续补压。
9. **分段 ≠ 记忆**：回复按 `|||` 分段逐条发（模拟打字），但 L1 里存的是去除 `|||` 的整段。
10. **提示词自由**：角色提示词完全由用户编写、不限长；时间/画像/记忆/情绪四个附加段各有开关。
11. **全部配置热切换**：网页保存即生效，机器人无需重启。
12. **容错**：损坏文件自愈重建、AI 失败统一兜底"API 调用失败"、异常落日志带完整堆栈。

---

## 2. 分层架构

```
┌─────────────────────────────────────────────────────┐
│                     接入层                           │
│   bot_ilink.py(微信,iLink 主入口)   web_ui.py(网页)   │
│   bot.py(WeChatFerry 备用)                            │
└───────────────────────────┬─────────────────────────┘
                            │ 都调用
┌───────────────────────────▼─────────────────────────┐
│                service.py 业务编排层                 │
│  情绪结算→动态温度 · 检索 · 工具循环 · 调AI · 记录     │
└──────┬─────────────┬─────────────┬──────────────────┘
       │             │             │
┌──────▼─────┐ ┌─────▼──────┐ ┌────▼────────────┐
│ config.py   │ │ memory.py  │ │ ai_client.py    │
│ 配置/角色    │ │ L1/L2/画像 │ │ 对话(OpenAI兼容)│
└──────┬─────┘ └─────┬──────┘ └─────────────────┘
       │             │
┌──────▼─────────────▼──────────────────────────────┐
│   file_util.py(原子JSON读写)  logger.py(日志)       │
│   check.py(配置校验)                                │
├─────────────────────────────────────────────────────┤
│   emotion.py(情绪态/情绪→温度)                       │
│   tool_kit.py(工具发现/执行/沙箱) + tools/           │
│   scheduler.py + state.py(主动对话调度/状态)          │
└─────────────────────────────────────────────────────┘
```

| 层 | 职责 | 模块 |
|---|---|---|
| 接入层 | 收发微信消息 / 提供网页界面 | `bot_ilink.py`、`web_ui.py`、`bot.py` |
| 业务编排层 | 把「情绪/温度/时间/画像/记忆/工具」拼成一次完整对话 | `service.py` |
| 功能层 | 配置与角色、记忆系统、情绪系统、工具系统、AI 调用 | `config.py`、`memory.py`、`emotion.py`、`tool_kit.py`、`ai_client.py` |
| 主动对话层 | 定时/提醒/周期性主动发消息 + 状态持久化 | `scheduler.py`、`state.py` |
| 基础设施层 | 文件读写、日志、配置自检 | `file_util.py`、`logger.py`、`check.py` |

---

## 3. 各模块作用（逐个说明）

### 3.1 `bot_ilink.py` —— 微信机器人主程序（iLink 官方 API）

定位：**只做「微信 ↔ service」的搬运工**，不自己写 AI/记忆逻辑。

1. **iLink 协议客户端**：`make_headers()` 构造请求头（每次随机生成 X-WECHAT-UIN 防重放）、`api_post()/api_get()` 发 HTTP/JSON 请求。
2. **扫码登录**：`request_qrcode()` 申请二维码 → `save_and_show_qrcode()` 保存/打开图片 → `login()` 轮询扫码状态直到 `confirmed`，拿到 `bot_token`。
3. **消息收发主循环**：`run_bot()` 用 `getupdates` 长轮询收消息 → `handle_message()` → `service.chat_once()` → `send_reply()` 按 `|||` 分段、`_send_one()` 逐段带"正在输入"发送、段间**按字数模拟打字**（每字秒数/上限在系统配置里可调，默认每字 0.1s、上限 10s）。
4. **主动对话接入**：收到消息时把 `context_token` 存进 `state`（主动发的通行证）；启动时拉起 `scheduler` 调度线程（周期性主动 + 提醒到点发送）。
5. **启动自检**：`config.repair_corrupted_files()` + `check.check_all()`，缺文本模型 key 或无角色才退出。

### 3.2 `bot.py` —— 备用程序（WeChatFerry PC Hook 版）

早期用 PC 微信 Hook（WeChatFerry）实现的版本，因为**需要精确匹配的微信版本 + 注入 DLL**、维护难、有封号风险，已降级为备用。逻辑与 `bot_ilink.py` 相同（都调 `service.chat_once()`），只是接入方式换成 wcferry。需要时 `pip install wcferry` 才能用。

### 3.3 `web_ui.py` —— Streamlit 管理网页（8 个页签）

| 页签 | 功能 |
|---|---|
| 💬 对话 | 选中角色直接对话测试；回复下方显示本轮**温度与情绪**（验证动态温度效果）。测试身份 `user_id='test'`，与微信用户隔离；带「🧹 清空测试对话」按钮 |
| 🎭 角色/提示词 | 新建/编辑/删除角色（提示词自由编写、不限长）、切换"当前启用角色" |
| ⚙️ 系统配置 | 对话/分析模型均可**一键应用模型预设**（自动填 base_url/模型名并按预设过滤不支持的参数）；所有字段带说明；含 frequency/presence penalty、HNSW 参数等 + 「🔍 检查配置完整性」按钮 |
| 🧠 记忆 | 按「管理对象 × 角色 × 话题」查看记忆：L1 全部显示（最新在上）、每条带 🗑️；L2 显示最近 30 条（可按话题过滤）、每条可删；另有 L1/L2 一键清理 |
| 👤 用户画像 | 编辑长期不变的信息：称呼/关系/性别/生日/爱好/性格/家庭环境/居住环境/长期事实（近期事件不属于画像，由 L2 记忆承担） |
| 🛠️ 工具 | 内置工具带介绍与白名单标记；**自定义白名单工具 JSON 文本栏维护**（可选 code 沙箱执行）；「允许 AI 生成并执行代码」开关（默认关，**开启时红字警告**）；工具设置 |
| 💗 情绪 | 查看当前角色情绪态（情绪/效价/唤醒度），可重置为平静 |
| 📣 主动对话 | 主动模式开关/间隔/时间段设置、「立即生成一条主动消息」预览（不写记忆）、提醒任务列表 |

原则：**UI 不直接碰文件/API**，全部通过 `config.py`、`service.py`、`check.py`、`tool_kit.py`。所有修改保存后即时生效（微信 bot 无需重启）。侧边栏「记忆管理对象」下拉框决定记忆/情绪页签管理哪个用户（默认真实微信用户；删除记忆即时同步，L2 存 ChromaDB 天然跨进程一致）。

### 3.4 `service.py` —— 业务编排层（核心）

所有"AI 对话"逻辑的唯一实现处，微信和网页共用：

- `chat_once(role_name, user_id, user_text)` / `chat_once_with_meta()`：一轮完整对话（§1.2 管线）。流程：
  1. `config.reload_if_changed()` 热切换；
  2. 取角色 + 画像，检索 L2 相关记忆（含情绪）→ 算记忆唤起情绪；**检索条数 K = ⌊ln(总记忆数)⌋(1~10) 动态计算**；
  3. 分析模型推断 AI 情绪 → 情绪结算（衰减 + 记忆×0.3 + 消息×0.7）→ **动态温度 =（配置温度 + 情绪温度）/ 2**；
  4. `config.build_system_prompt()` 组装 system（人设 + 当前时间 + 画像 + 记忆 + 当前情绪）；
  5. 拼最近 N 轮 L1 上下文 + 当前消息；工具循环（白名单/自定义工具 + 分析模型判断是否联网搜索）；
  6. 记录本轮：**assistant 存去除 `|||` 的完整文本**（触发后台压缩）；
  7. 关键词命中"提醒"时解析提醒并存入 state；
  8. 返回原始回复 + meta（温度/情绪/效价/唤醒度，供网页展示）。
- `proactive_once()`：生成一条**主动消息**——从高重要度记忆取话题（`proactive_seq` 防重复），同样走情绪 + 动态温度，写进 L1 作为 AI 独白。
- `parse_reminder()`：用分析模型解析"提醒我明天9点开会" → `{fire_at, content}` 存入 state。
- `build_ai_client()` / `build_analysis_client()`：按当前配置构造对话模型 / 分析模型客户端。
- `get_manager()` / `_build_manager()`：按 `(用户, 角色)` 缓存记忆管理器，记忆相关配置变化时自动重建；命中缓存时先 `sync_from_disk()`（文件 mtime 变了就重读，保证网页删除/清空对 bot 进程即时生效）。
- `get_state()` / `get_emotion_state()` / `get_toolkit()`：状态存储、情绪态、工具注册表的单例缓存。
- 工具函数：`current_time_str()`、`strip_separator()`、`strip_context_prefix()` / `clean_reply()`（剥掉模型模仿上下文产生的 `[时间] AI:` 开头前缀）、`history_path()`、`store_dir_of()`、`emotion_path()`、`safe_id()`、`user_dir_id()`（用户目录名：`memory`/`test`）、`list_user_ids()`。
- 供网页用：`get_l1_turns()`、`get_l2_items()`、`get_l2_topics()`（话题过滤用）、`delete_l1_turn()`（删 L1 某一轮）、`delete_l2_item()`（按 id 删一条 L2）、`clear_memory()`、`reset_emotion()`。

### 3.5 `config.py` —— 配置层（多角色 + 热切换）

管理五份配置文件：`config/系统配置文件.json`、`角色配置文件.json`、`用户画像.json`、`模型预设.json`（常用模型供应商预设）、`工具配置.json`（用户自定义白名单工具）。

系统配置共 8 段：

| 段 | 管什么 |
|---|---|
| `text_model_information` | 对话模型（name/apikey/base_url/temperature/max_tokens/top_p/**frequency_penalty/presence_penalty**/timeout/supported_params） |
| `embedding_model_information` | 记忆向量（默认阿里云 text-embedding-v4，1024 维；维度按配置自适应，可换其他 OpenAI 兼容 embedding） |
| `analysis_model_information` | **分析模型**（情绪分析/文本压缩/搜索决策，低温度 0.1，网页可自定义） |
| `memory_information` | 记忆开关、上下文轮数、总结轮数、L1 仅保留轮数（默认 10）、去重阈值、检索权重、**ChromaDB HNSW 参数** |
| `emotion_information` | 情绪开关、温度摆动幅度/上下限、衰减系数、情绪注入开关 |
| `tool_information` | 工具开关、生成代码开关（默认关）、沙箱超时、工具循环轮数、**联网搜索方式**（auto/native/tool） |
| `proactive_information` | 主动对话开关、间隔小时数、允许发送时间段、token 新鲜期 |
| `system_information` | 分段分隔符、时间格式、`inject_time/profile/memory` 开关、**模拟打字设置**（`typing_seconds_per_char` 每字秒数 / `typing_max_seconds` 上限 10s） |

- **模型预设**：`load_model_presets()` 读 `config/模型预设.json`（DeepSeek/OpenAI/DashScope/GLM/Kimi/SiliconFlow/Ollama 七家预设，含 base_url/模型列表/支持的参数/说明）；网页选预设后自动填 base_url 与模型名，并按 `supported_params` 过滤该模型不支持的采样参数（如部分模型不支持 frequency/presence penalty），避免 400 报错。
- **自定义工具**：`load_custom_tools()/save_custom_tools()` 读写 `config/工具配置.json`，网页 JSON 文本栏维护。

- **多角色 CRUD**：`load_roles / add_role / update_role / delete_role / get_role`。
- **当前启用角色**：`get_active_role()`（缺失回退第一个，列表为空抛"需要一个角色"）、`set_active_role()`。
- **内存缓存 + 双写**：`load_*` 读缓存、`save_*` 先改内存再原子落盘。
- **热切换**：`reload_if_changed()` 每轮校验文件 mtime，变了才重读——所以网页保存后机器人无需重启。
- **损坏自愈**：`repair_corrupted_files()` + `safe_load_json`，坏文件删除重建、写日志、提示。
- **密钥策略**：优先环境变量（`DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY`），找不到退回系统注册表。
- **提示词组装**：`build_system_prompt()` 按开关拼接"人设 + 当前时间 + 画像 + 相关记忆 + 当前情绪"（分段指令不进 prompt，由角色提示词自行书写）。

### 3.6 `memory.py` —— 记忆层（三层记忆）

| 类 | 作用 |
|---|---|
| `EmbeddingClient` | 调 OpenAI 兼容 embedding 接口（默认阿里云 text-embedding-v4，维度按配置自适应）；支持 `embed_batch()` 批量（省调用） |
| `L1Store` | 短期记忆：`[{time, user, assistant, summarized}]`，JSON 落盘 + 内存持有，每条带时间；**仅保留最近 N 轮（默认 10）**；支持 `remove_at()` 逐条删除、`reload()` 重读 |
| `MemoryStore` | 长期记忆：**ChromaDB 持久化**（HNSW 余弦检索，`data/vector_store/<用户目录>/<角色>/chroma/`）；文本进 documents、元数据进 metadatas（importance/emotion/emotion_value/source/access_count/last_accessed/last_proactive_used_at/proactive_seq/created_at/**topic**）；**旧版 memory.json+vectors.npy 首次打开自动迁移**；支持 `remove_by_id()`/`clear()`/`all_topics()` |
| `Compressor` | 用**分析模型**把一批对话（默认 10 轮）**按话题分类**压缩成结构化长期记忆（要点 + 话题名 + 重要度 1~5 + 情绪 + 效价），prompt 里喂画像、已有 L2 与已有话题列表供去重/纠错/归类 |
| `ProfileUpdater` | 用分析模型增量更新用户画像——**只记长期不变的信息**（称呼/关系/性别/生日/爱好/性格/家庭环境/居住环境/长期事实），近期事件不写入 |
| `MemoryManager` | 编排：`add_turn` 写 L1 → 未总结轮数 ≥10 时后台压缩成 L2 → 检索/上下文/主动话题；`sync_from_disk()` 同步 L1 文件（L2 由 ChromaDB 天然跨进程一致）；`retrieve()` 检索条数按 `retrieval_k()` 动态计算 |

关键算法：

- **动态检索条数** = ⌊ln(总记忆数)⌋，下限 1、上限 10（更新计划第1条）。
- **动态重要度** = 基值 ×（0.6×访问频率分 + 0.4×近因分），近因半衰期 14 天。
- **检索排序** = 0.7×余弦相似度 + 0.3×动态重要度（权重可配）；先从 ChromaDB 取候选（max(3K,30) 条）再混合加权重排。
- **相似度去重**：压缩出新条目 → 与已有 L2 算余弦相似度，≥ 阈值（默认 0.85）就**更新旧条目**（合并文本、重算重要度、刷新时间），不新增。
- **主动话题防重复**：`pick_proactive_topic()` 取"重要度高 且 最近 3 次主动没用过"的记忆；条目太少才允许回退复用。
- **P1 修复**：压缩/向量化/入库任一步失败都不标记"已总结"，记忆永不丢。
- **P6 修复**：重启后持续补压，直到所有未总结轮次处理完。

记忆三层：

| 层 | 内容 | 存储 | 进 prompt 方式 |
|---|---|---|---|
| L1 短期 | 最近原文对话（仅保留近 10 轮） | `data/history/<用户目录>/<角色>.json` | 拼最近 N 轮上下文（带时间） |
| L2 长期 | 压缩出的要点（带话题/重要度/情绪） | `data/vector_store/<用户目录>/<角色>/chroma/`（ChromaDB） | 向量检索动态 K 条注入「相关记忆」 |
| 画像 | 长期不变的信息（称呼/关系/性别/生日/爱好/性格/家庭环境/居住环境/长期事实） | `config/用户画像.json` | 常驻 system prompt（不参与衰减） |

> **用户目录命名**：单用户设计，落盘目录固定为可读名字——真实微信用户 `memory`、网页测试身份 `test`（与微信用户隔离，网页聊天不写入真实用户记忆）。网页侧边栏「记忆管理对象」下拉框决定记忆/情绪/主动对话页签管理哪个用户。

> **L2 与画像分工**：L2 = 事件性/近期发生的（含用户最近干的事）；画像 = 长时间不会变的内容（爱好/性格/性别/生日/家庭环境/居住环境等，防止被 L2 的近因衰减冲淡遗忘）。**近期事件严禁写入画像**。

### 3.7 `ai_client.py` —— AI 客户端

薄封装：调 OpenAI 兼容的 `/chat/completions`（默认 DeepSeek），失败重试 3 次（等待 1s/2s/4s）。

- `complete(messages, temperature=..., tools=..., tool_executor=...)`：
  - 支持**动态 temperature**（情绪系统用）；
  - 支持 **frequency_penalty / presence_penalty**（重复性与话题惩罚，默认 0.4/0.3，网页可调）；
  - **按 `supported_params` 过滤请求参数**：只发送该模型预设支持的字段（部分模型不支持 penalty/top_p，自动裁剪，避免 400）；
  - 支持 **function calling 工具循环**：收到 `tool_calls` → 执行工具 → 结果回填 → 再问模型，直到出最终文本或超轮数（默认 4 轮防死循环）。
- `complete_with_search(messages, ...)`：走 DeepSeek 官方 **Responses API + 内置 `web_search` 工具**（模型自己决定是否搜索，多步搜索后返回最终回答）；失败抛异常供 service 回退到 Bing 工具。

### 3.8 `emotion.py` —— 情绪系统（动态温度）

思路来源：B 站灵梦桌宠评论区——**持久化情绪态 → 情绪决定下一次说话温度 → 检索 L2 时也算记忆唤起情绪**。

| 函数/类 | 作用 |
|---|---|
| `EmotionState` | 一个 `(用户, 角色)` 的情绪态，存 `data/emotion/<用户>/<角色>.json`；二维模型：`valence` 效价(-1 负面~+1 正面) + `arousal` 唤醒度(0~1) + `emotion` 标签 |
| `label_of()` | 由 (效价, 唤醒度) 映射情绪标签（平静/开心/兴奋/难过/生气/焦虑/害羞） |
| `infer_message_emotion()` | 用**分析模型**推断"用户这条消息唤起的 **AI 自己的情绪**"（根据上下文，不是用户情绪） |
| `memory_evoked_emotion()` | 检索 L2 时算记忆唤起情绪 = Σ(记忆 emotion_value × 相似度权重) / Σ权重 |
| `settle()` | 情绪结算 = 自然衰减(上轮) + 记忆唤起×0.3 + 消息情绪×0.7（每轮向平静基线回落，不会一直亢奋） |
| `emotion_temperature()` | 情绪 → 温度：唤醒度主驱动 + 效价微调，**最终温度 =（配置温度 + 情绪温度）/ 2** |
| `emotion_hint()` | 生成【当前情绪】提示写进 system prompt（让语气自然体现情绪，但不许直接说出数值） |

### 3.9 `tool_kit.py` + `tools/` —— 工具调用

- **工具发现**：扫描 `tools/*.py`，约定结构 `TOOL` 字典（`name/description/safe/parameters`）+ `run()` 函数，自动注册；内置工具自带介绍。
- **白名单**：`safe=true` 的工具 AI 可直接用，tools 描述里标注"安全工具,已通过白名单审核"。
- **内置三个安全工具**：`web_search.py`（联网搜索：**Bing 为主**(cn→www)、DuckDuckGo 兜底，无需 key，国内网络可用）、`计算器.py`、`时间查询.py`。
- **DeepSeek 原生联网搜索**：DeepSeek 官方 Responses API 内置 `web_search` 工具（`ai_client.complete_with_search()`）——模型自己决定是否搜索、搜什么并直接给出带来源的回答；`web_search_mode` 配置：`auto`（默认，DeepSeek 官方 base_url 自动启用原生，其他模型回退 Bing 工具）/ `native`（强制原生）/ `tool`（始终 Bing 工具）；原生调用失败自动回退 Bing 工具。
- **用户自定义工具**：`config/工具配置.json` 维护，网页 JSON 文本栏编辑（像用户画像一样）——每条含 `name/description/safe/parameters/code`；`code` 在受限沙箱执行，执行时注入 `args` 变量（模型传入的参数字典）；与内置工具重名自动跳过。
- **ToolKit**：`openai_tools()` 生成 function calling 的 tools 列表；`execute()` 执行工具并回填结果。
- **AI 生成代码**：`run_python_code` 通道，**默认关闭**；开启后 AI 现场生成的代码在**受限沙箱**执行——静态黑名单（禁 `os.system/subprocess/socket` 等）+ 隔离 subprocess（`python -I -S`）+ 超时 + 精简环境，执行前**日志留完整代码快照**。
- **搜索决策**：`decide_search()` 用分析模型判断"这条消息是否需要联网搜索"，需要才启用 `web_search`（省调用、避免无关联网）。

### 3.10 `scheduler.py` + `state.py` —— 主动对话

- `state.py`：`data/state.json` 持久化——
  - `users`：每个用户的 `context_token`（主动发的通行证，只能从对方发来的消息拿到）+ `token_updated_at` + `last_active` + `last_proactive`；key 是微信 openid（`xxx@im.wechat` 格式，bot 应用内的用户标识，用于回消息/主动发消息寻址，必须明文保存才能工作）；
  - `reminders`：提醒任务列表（解析出的 `{fire_at, content}` + 是否已发）。
  - `token_fresh()`：判断 token 是否还在新鲜期（默认 30 分钟）。
  - **自愈**：文件被删/损坏会自动重建为 `{"users": {}, "reminders": []}`；openid 与 context_token 会在好友下一条消息到达时重新写入（提醒列表则丢失）。存量记忆不受影响（L1/L2/情绪在其他文件），网页管理下拉框有占位兜底，仍可管理 `memory/` 下的记忆。
- `scheduler.py`：后台线程每 30 秒 tick——
  - **周期性主动**：开启且当前时间在 `[start_hour, end_hour)` 内、距上次主动 ≥ `interval_hours`、token 新鲜 → 生成并发送一条主动消息；
  - **提醒**：到点且 token 新鲜才发，token 过期则跳过等下次（不标记已发）。
- 主动消息内容由 `service.proactive_once()` 生成：从**高重要度记忆**取话题，自动轮换不重复；空库则通用问候兜底。

### 3.11 `file_util.py` —— 文件工具

所有落盘的统一底层：

- `atomic_save_json()`：先写临时文件再 `os.replace` 原子替换，防写一半。
- `save_json()/load_json()`：文件级线程锁。
- `safe_load_json()`：**损坏自愈**——文件损坏则删除重建、回调写日志、返回默认值。
- `format_time()`：`%A` 手动映射成中文"周一~周日"（不依赖系统 locale）。
- `parse_time()`：从带星期的中文时间串解析回 datetime（重要度衰减/调度用）。
- `find_index()`：按 name 查列表索引。

### 3.12 `logger.py` —— 日志

- 只记**报错/异常**（WARN 及以上），正常流程不写。
- 追加写到 `logs/bot.log`，不轮转、不删旧日志（无代码用户报障只需提供日志尾部）。
- `log_exception()`（完整堆栈+上下文）、`log_error()`（非异常类错误，如文件损坏、AI 生成代码快照）。

### 3.13 `check.py` —— 配置完整性校验

返回逐项报告 `[{level: ok|warn|error, item, message}]`，检查 **9 项**：文本模型、embedding、**分析模型**、角色配置、用户画像、路径可写、依赖、**工具**、分段分隔符；另有 `check_connectivity()` 联网测 base_url。供网页「检查配置」按钮和 bot 启动自检使用（只有"文本模型/角色配置"错误才阻止启动）。

### 3.14 `smoke_test.py` —— 纯逻辑自测

无 API 调用的自测脚本（`python smoke_test.py`），覆盖：情绪数学、ChromaDB 去重/检索加权/主动话题防重复、压缩解析（含话题）、工具发现/沙箱/自定义工具、旧数据自动迁移、逐条删除/清空、回复前缀剥离、动态检索条数公式。共 59 项断言。

### 3.15 脚本与目录

| 文件/目录 | 作用 |
|---|---|
| `setup.bat` | 一键装环境：检测 Python≥3.10 → 依次尝试清华/阿里云/官方源装依赖（含 chromadb） |
| `start.bat` | 一键启动：同时开 `bot_ilink.py` + `web_ui.py` 两个窗口 |
| `smoke_test.py` | 纯逻辑自测（无 API 调用）：`python smoke_test.py` |
| `config/` | 系统配置（8 段 + 密钥）、角色配置、用户画像、模型预设、工具配置（含密钥，勿提交） |
| `tools/` | 白名单工具目录（AI 按需调用）；`.sandbox_tmp/` 为沙箱临时目录（自动清理） |
| `data/history/<用户目录>/<角色>.json` | L1 短期记忆（用户目录：`memory` 微信用户 / `test` 网页测试身份；仅保留近 10 轮） |
| `data/vector_store/<用户目录>/<角色>/chroma/` | L2 长期记忆（ChromaDB：documents+metadatas+HNSW 向量） |
| `data/emotion/<用户目录>/<角色>.json` | 情绪态 |
| `data/state.json` | 主动对话状态（context_token + 提醒任务）。**敏感**：含微信 openid、context_token（主动发消息凭证）与提醒内容，已加入 `.gitignore`，勿外传/勿提交 |
| `logs/bot.log` | 运行日志（只记报错） |
| `docs/` | iLink 协议参考资料 |
| `requirements.txt` | 依赖：requests、numpy、streamlit、chromadb |

> **隔离粒度**：记忆与情绪都按 `(用户, 角色)` 两级隔离——切换角色不串记忆；单用户设计下所有微信好友共用 `memory` 目录，`test` 为网页对话页签的测试身份、与微信用户隔离。网页侧边栏「记忆管理对象」可切换管理对象（默认真实微信用户）。

---

## 4. 一次完整对话的数据流（详细版）

```
用户私聊"帮我写个脚本"
        │
        ▼
bot_ilink.run_bot()  getupdates 收到消息
        │  └─ state.update_token(用户, context_token)   # 主动发的通行证
        ▼
bot_ilink.handle_message()  →  service.chat_once_with_meta(role, user, text)
        │
        ├─ 1. config.reload_if_changed()          # 热切换：配置变了就重读
        ├─ 2. 取角色 + 用户画像
        ├─ 3. mgr.retrieve(text, k)               # L2 检索(带重要度/情绪)
        │      └─ 记忆唤起情绪 = Σ(emotion_value×相似度)/Σ相似度
        ├─ 4. 分析模型推断 AI 情绪 → 情绪结算 → 动态温度=(配置温度+情绪温度)/2
        ├─ 5. build_system_prompt()               # 人设+时间+画像+记忆+当前情绪
        ├─ 6. context_messages(n)                 # 最近 N 轮 L1(带时间)
        ├─ 7. 工具:白名单 tools + 分析模型判断是否联网搜索
        ├─ 8. ai.complete(messages, temperature, tools)   # 调大模型(可多轮工具循环)
        ├─ 9. mgr.add_turn(用户, 去|||的回复)      # 落盘 L1,可能触发后台压缩
        ├─ 10. 关键词命中"提醒" → 解析提醒存入 state
        └─ 返回 (回复, meta: 温度/情绪/效价/唤醒度)
        │
        ▼
bot_ilink.send_reply()  按 ||| 切分 → 逐段发送(段间按长度模拟打字)
        │
        ▼
(后台压缩线程) 每 10 轮:分析模型压缩 → 相似度去重 → 批量embedding → 写入 L2 → 更新画像
(调度线程)     周期性主动(高重要度话题,防重复) / 提醒到点发送
```

---

## 5. 模块快速对照表

| 模块 | 一句话职责 |
|---|---|
| `bot_ilink.py` | 微信接入主程序：扫码登录、收发消息、分段发送、主动对话调度（搬运工） |
| `bot.py` | WeChatFerry 备用接入（需特定微信版本，不推荐） |
| `service.py` | 业务编排层：每轮对话的唯一实现处（情绪/温度/记忆/工具/记录） |
| `config.py` | 配置层：8 段系统配置 + 多角色 CRUD + 热切换 + 自愈 |
| `memory.py` | 记忆层：L1 落盘 / L2 向量检索(重要度+情绪) / 压缩去重 / 画像更新 |
| `emotion.py` | 情绪系统：情绪态 + 情绪推断 + 情绪→温度 |
| `tool_kit.py` | 工具调用：发现/注册/执行 + 代码沙箱 + 搜索决策 |
| `scheduler.py` | 主动对话调度：周期性主动 + 提醒 |
| `state.py` | 状态持久化：context_token + 提醒任务 |
| `ai_client.py` | AI 客户端：OpenAI 兼容 + 重试 + 动态温度 + 惩罚参数 + 按预设过滤参数 + 工具循环 |
| `file_util.py` | 原子 JSON 读写 + 线程锁 + 自愈 + 时间工具 |
| `logger.py` | 只记报错，追加写 `logs/bot.log` |
| `check.py` | 配置完整性校验（9 项 + 连通性） |
| `web_ui.py` | Streamlit 管理网页（8 页签） |
| `smoke_test.py` | 纯逻辑自测（59 项断言） |
| `tools/` | 白名单安全工具目录（web_search/计算器/时间查询 + 用户自定义工具） |

---

## 6. 快速上手

```bash
# 1. 装环境(自动检测 Python≥3.10,多镜像回退)
setup.bat

# 2. 一键启动(机器人 + 网页)
start.bat

# 或分别手动
python bot_ilink.py          # 微信机器人：扫码登录
streamlit run web_ui.py      # 管理网页：http://localhost:8501
```

- 机器人窗口扫码 → 微信里私聊机器人联系人 → AI 回复。
- 网页里建角色、填 key、点「检查配置完整性」、对话测试。
- 改完配置保存即生效（微信 bot 无需重启）；想验证逻辑可先跑 `python smoke_test.py`。
