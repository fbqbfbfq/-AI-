# 微信 AI 分身(iLink 官方 API 版)

扫码后在你微信里得到一个 AI 机器人联系人,私聊它即可对话。基于**腾讯官方 iLink 接口**,无需 Hook、无需特定微信版本,扫码即用、易分发易维护。

技术栈:**Python + 腾讯 iLink API + DeepSeek(对话/压缩/画像)+ 阿里云 embedding(记忆检索)+ Streamlit(管理网页)**。

> ℹ️ 说明:iLink 的形态是"扫码后得到一个机器人联系人、你私聊它",机器人并非你自己的微信号(这是腾讯官方接口的固有形态)。如需"让小号本身变成 AI"的 PC Hook 方案,见备用文件 `bot.py`(WeChatFerry 版,需特定微信版本、有封号风险,不推荐)。

---

## 为什么用 iLink

- **官方接口**:腾讯官方 iLink API,不碰逆向、不注入 DLL,合规风险低。
- **无需特定微信版本**:扫码授权即可,用户不用去下载某个旧版微信,分发和维护都简单。
- **扫码即用**:运行程序 → 手机微信扫码 → 得到一个 AI 机器人联系人 → 私聊即对话。

> 曾尝试过的 WeChatFerry(PC 微信 Hook)需要**精确匹配的 PC 微信版本**、注入 DLL,普通用户很难搞定,且微信升级就失效,故本项目以 iLink 为主;`bot.py` 仅作备用保留。

---

## 功能

- ✅ 聊天回复(扫码后私聊机器人 → AI 回复)
- ✅ 三层记忆:短期原文(L1,JSON 落盘不丢)、长期要点(L2 向量检索)、用户画像(常驻提示词)
- ✅ L2 记忆带**重要度 / 情绪 / 效价**,检索按「相关性 × 重要度」加权,压缩时相似度去重、更新重算重要度
- ✅ 每条记忆带时间戳,对话时把"当前时间"注入给模型
- ✅ 画像自动增量更新(越聊越懂你)
- ✅ 每 10 轮自动总结一次(一轮 = 用户 1 条 + AI 1 条);压缩失败不丢记忆,重启自动补压
- ✅ **情绪系统 + 动态温度**(灵梦桌宠思路):推断 AI 自己的情绪 → 决定下一次说话的温度,检索记忆时也算记忆唤起情绪
- ✅ **工具调用**:`tools/` 白名单安全工具(联网搜索/计算器/时间查询),AI 按需调用;可开启"AI 生成代码"能力(沙箱执行,默认关闭)
- ✅ **主动对话**:定时/提醒解析/周期性主动发消息(间隔与时间段可自定义),话题取自高重要度记忆且不重复
- ✅ 分段回复(角色提示词约定 `|||` 分隔,逐段模拟真人打字;记忆存整段)
- ✅ 多角色管理(不同名字/不同提示词,自由编写、长度不限)
- ✅ Streamlit 管理网页(角色/配置/记忆/画像/工具/情绪/主动对话 + 对话测试 + 配置自检)
- ✅ 全部配置热切换(保存即生效,无需重启)、异常日志(`logs/bot.log`)、一键安装(`setup.bat`)

---

## 目录结构

```
wechat-ai-bot/
├── bot_ilink.py        # 主程序(iLink 官方 API 版,扫码即用;含主动对话调度)
├── bot.py              # 备用(WeChatFerry 版,需特定微信版本,不推荐)
├── service.py          # 业务编排层(情绪结算 + 动态温度 + 检索 + 工具 + 记录)
├── memory.py           # 记忆模块:L1 落盘 / L2 向量检索(重要度+情绪) / 压缩去重 / 画像
├── emotion.py          # 情绪系统:情绪态持久化 + 情绪推断 + 情绪→温度
├── tool_kit.py         # 工具调用:工具发现/注册/执行 + 代码沙箱 + 搜索决策
├── scheduler.py        # 主动对话调度(定时/提醒/周期性主动)
├── state.py            # 状态持久化(bot 身份 + context_token + 提醒)
├── ai_client.py        # AI 客户端(DeepSeek,OpenAI 兼容;动态温度 + 工具循环)
├── config.py           # 配置层(多角色 CRUD + active_role + 热切换)
├── file_util.py        # 原子 JSON 读写 + 损坏自愈
├── logger.py           # 日志(异常 → logs/bot.log)
├── check.py            # 配置完整性校验
├── web_ui.py           # Streamlit 管理网页(替代 admin_ui.py)
├── smoke_test.py       # 纯逻辑自测(无 API 调用):python smoke_test.py
├── admin_ui.py         # 已废弃(功能由 web_ui.py 承接)
├── setup.bat           # 一键环境(Python≥3.10 检测 + 国内镜像装依赖)
├── start.bat           # 一键启动(同时启动 bot_ilink.py + web_ui.py)
├── config/             # 配置文件(含密钥,勿提交)
├── tools/              # 工具目录(白名单安全工具,AI 按需调用)
├── data/               # history/(L1) + vector_store/(L2) + emotion/(情绪态) + state.json
├── logs/               # 运行日志(只记报错,不删旧日志)
├── DESIGN.md / MEMORY_DESIGN.md / REDESIGN.md / UPGRADE_DESIGN.md   # 设计文档
└── docs/               # iLink 协议参考
```

---

## 快速开始

### 1. 安装依赖

双击 `setup.bat`,会自动检测 Python(需 3.10 及以上),依次尝试清华镜像 → 阿里云镜像 → 官方源安装依赖。

或手动执行:

```bash
cd wechat-ai-bot
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

### 2. 一键启动(推荐)

双击 `start.bat`,会同时启动两个窗口:
- **机器人窗口**:`python bot_ilink.py` —— 弹出二维码,用手机微信扫码并确认
- **管理网页**:`streamlit run web_ui.py` —— 浏览器访问 http://localhost:8501 管理角色/配置/记忆

或分别手动启动:

```bash
python bot_ilink.py          # 微信机器人(扫码登录)
streamlit run web_ui.py      # 管理网页
```

机器人窗口读到 `开始监听消息` 后,在微信里私聊这个机器人联系人,AI 就回复了。

密钥从系统环境变量自动读取(`DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY`),也可在管理网页里填写保存。

### 3. 管理网页说明

网页支持:新建/编辑/删除角色(自由提示词、长度不限)、切换启用角色、改模型/记忆/系统配置、查看/清空记忆、配置自检、对话测试。所有修改保存后即时生效(机器人无需重启)。

---

## 配置(config/ 目录)

```jsonc
// config/系统配置文件.json
{
  "active_role": "",                  // 当前启用角色(空=用第一个;网页可切换)
  "text_model_information": {          // 对话模型
    "name": "deepseek-v4-flash", "apikey": "<DEEPSEEK_API_KEY>",
    "base_url": "https://api.deepseek.com",
    "temperature": 0.7, "max_tokens": 1024, "top_p": 1.0, "timeout": 60
  },
  "embedding_model_information": {     // 记忆向量
    "name": "text-embedding-v4", "apikey": "<DASHSCOPE_API_KEY>",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "embedding_dimensions": 1024
  },
  "analysis_model_information": {      // 分析模型(情绪分析/压缩/搜索决策,网页可改)
    "name": "deepseek-v4-flash", "apikey": "<DEEPSEEK_API_KEY>",
    "base_url": "https://api.deepseek.com", "temperature": 0.1
  },
  "memory_information": {              // 记忆设置
    "enabled": true, "l1_context_turns": 10, "l1_summary_rounds": 10,
    "l1_maxlen": 40, "l2_top_k": 5,
    "dedup_threshold": 0.85, "retrieval_sim_weight": 0.7,
    "vector_store_path": "data/vector_store", "history_path": "data/history"
  },
  "emotion_information": {             // 情绪系统(动态温度)
    "enabled": true, "temperature_span": 0.3, "temperature_min": 0.1,
    "temperature_max": 1.5, "decay": 0.85, "inject_emotion": true
  },
  "tool_information": {                // 工具调用
    "enabled": true, "allow_generated_code": false,
    "sandbox_timeout": 10, "max_tool_rounds": 4
  },
  "proactive_information": {           // 主动对话
    "enabled": false, "interval_hours": 3, "start_hour": 8,
    "end_hour": 23, "token_fresh_minutes": 30
  },
  "system_information": {              // 系统设置
    "segmented_reply": true, "segment_separator": "|||",
    "time_format": "%Y-%m-%d %A %H:%M:%S",
    "inject_time": true, "inject_profile": true, "inject_memory": true
  }
}
// config/角色配置文件.json   —— 多角色列表 [{"name":"...","prompt":"..."}],提示词自由编写
// config/用户画像.json       —— 你的画像(称呼/关系/事实/偏好/性格/近期事项)
```

---

## 机器人指令

| 指令 | 作用 |
|---|---|
| `/help` | 查看指令列表 |
| `/clear` | 清空对话记忆(短期+长期,画像保留) |

其他消息交给 AI 回复。

---

## 常见问题

**Q: 没反应 / 收不到消息?**
- 确认机器人窗口已显示 `登录成功` 和 `开始监听消息`
- 确认是在微信里**私聊**机器人联系人(群聊暂不支持)
- 机器人不回自己发的消息

**Q: 登录会话多久有效?**
iLink 登录会话约 24 小时有效,过期后重新运行 `bot_ilink.py` 扫码即可。

**Q: 图片/语音会回吗?**
不会,当前只处理文字,其他类型记日志跳过。

**Q: 分段回复的间隔怎么来的?**
按每段字数估算(约 1.5~4 秒)模拟真人打字;段与段之间的停顿按该段长度单独计算。

**Q: 记忆会丢吗?**
不会。短期记忆(L1)JSON 落盘、长期记忆(L2)向量落盘,重启后仍在;每 10 轮自动总结一次。

---

## 参考

- iLink 协议资料:见 `docs/` 目录(`ilink-协议技术解析.md`、`cc-connect-微信接入指南.md` 等)
- 阿里云 embedding:[OpenAI 兼容接口](https://help.aliyun.com/zh/model-studio/embedding-interfaces-compatible-with-openai)
- DeepSeek API:<https://api-docs.deepseek.com>
