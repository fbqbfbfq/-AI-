# -*- coding: utf-8 -*-
"""
业务编排层(UPGRADE_DESIGN.md v2):

每轮对话管线(灵梦桌宠思路):
  用户消息 → 检索 L2(带情绪) → 记忆唤起情绪 → 分析模型推断 AI 情绪
  → 情绪结算(衰减+记忆×0.3+消息×0.7) → 动态温度=(配置温度+情绪温度)/2
  → 组装 system(人设+时间+画像+记忆+情绪) → 工具循环(白名单+网络搜索决策)
  → 调 AI → 记录 L1 → 后台压缩。

另含:分析模型客户端、提醒解析、主动消息生成(高重要度话题+防重复)。
"""
import datetime
import json
import re
import threading
import urllib.parse
from pathlib import Path

import ai_client
import config
import emotion
import file_util
import logger
import memory
import state as state_mod
import tool_kit

# 会话记忆管理器缓存:(user_id, role_name) -> (fingerprint, MemoryManager)
_managers = {}
_managers_lock = threading.Lock()

# 情绪态缓存:(user_id, role_name) -> EmotionState
_emotion_states = {}
_emotion_lock = threading.Lock()

# 工具注册表缓存
_toolkit = None
_toolkit_fp = None
_toolkit_lock = threading.Lock()

# 状态存储(单例)
_state_store = None
_state_lock = threading.Lock()


def base_dir():
    return Path(__file__).resolve().parent


def safe_id(name):
    """文件名/目录名清洗:保留字母数字与汉字,其余转下划线(用于角色名等)。"""
    return "".join(c if c.isalnum() else "_" for c in (name or ""))


# ---------- 用户目录命名 ----------
# 单用户设计:落盘目录固定用可读名字——'test' 是网页对话测试身份,
# 真实微信用户的记忆统一放 'memory'(没有多用户需求,不按 openid 区分)。


def user_dir_id(user_id):
    """用户落盘目录名:'test' → 网页测试身份;其他 → 'memory'(单用户设计)。"""
    uid = str(user_id or "")
    if uid.lower() == "test":
        return "test"
    return "memory"


def list_user_ids():
    """管理页可管理的用户:真实微信用户在前,'test' 测试身份放最后。

    state.json 被删除重建后 users 为空:若 memory 目录里仍有记忆数据,
    用占位 id 'memory' 顶上(单用户设计,所有真实用户共用同一目录),
    保证网页仍能查看/清理存量记忆;好友下次发消息会重新登记 openid。
    """
    ids = [u for u in get_state().state.get("users", {}).keys()
           if u and str(u).lower() != "test"]
    if not ids:
        data_dir = base_dir() / "data"
        has_mem_data = ((data_dir / "history" / "memory").exists()
                        or (data_dir / "emotion" / "memory").exists()
                        or (data_dir / "vector_store" / "memory").exists())
        if has_mem_data:
            ids.append("memory")
    ids.append("test")
    return ids


def user_display_label(user_id):
    """下拉框显示名。"""
    if str(user_id).lower() == "test":
        return "test · 网页测试身份(与微信用户隔离)"
    return "memory · 微信好友"


def _is_deepseek_base(base_url):
    """判断 base_url 是否 DeepSeek 官方(原生联网搜索仅官方 Responses API 提供)。"""
    try:
        host = urllib.parse.urlparse(str(base_url or "")).netloc.lower()
    except Exception:
        return False
    return "deepseek.com" in host


def current_time_str():
    fmt = config.load_system().get("system_information", {}).get(
        "time_format", "%Y-%m-%d %A %H:%M:%S")
    return file_util.format_time(fmt=fmt)


def strip_separator(text):
    """去除分段分隔符,还原完整文本(记忆存储用)。"""
    sep = config.load_system().get("system_information", {}).get("segment_separator", "|||")
    if not sep or not text:
        return (text or "").strip()
    return "".join(text.split(sep)).strip()


# 模型会模仿上下文里的 "[2026-08-18 周二 19:00:35] AI:" 格式,
# 在自己的回复开头带上这类前缀。这里的正则负责剥掉它:
#   - 方括号/中文括号内包含日期或时间(如 2026-08-18 / 2026年8月18日 / 19:00:35)
#   - 后面可选跟角色标签(AI/助手/用户/assistant/user/bot)
#   - 冒号兼容半角 ':' 与全角 '：'
_CTX_PREFIX_DATE_RE = re.compile(
    r"^\s*[\[\【][^\]】]*\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}[^\]】]*[\]】]\s*"
    r"(?:AI|助手|用户|assistant|user|bot)?\s*[:：]\s*", re.IGNORECASE)
_CTX_PREFIX_TIME_RE = re.compile(
    r"^\s*[\[\【][^\]】]*\d{1,2}:\d{2}(?::\d{2})?[^\]】]*[\]】]\s*"
    r"(?:AI|助手|用户|assistant|user|bot)?\s*[:：]\s*", re.IGNORECASE)


def strip_context_prefix(text):
    """剥掉回复开头的时间戳+角色前缀(最多剥 4 层,防模型叠加多段)。"""
    if not text:
        return text
    for _ in range(4):
        new = text
        for pat in (_CTX_PREFIX_DATE_RE, _CTX_PREFIX_TIME_RE):
            new = pat.sub("", new, count=1)
        if new == text:
            return text
        text = new
    return text


def clean_reply(reply):
    """回复清理:按 ||| 分段后逐段剥前缀(模型可能只在第一段加,也可能每段都加)。"""
    sep = config.load_system().get("system_information", {}).get("segment_separator", "|||")
    if not sep or not reply:
        return strip_context_prefix(reply or "")
    return sep.join(strip_context_prefix(s) for s in reply.split(sep))


def history_path(user_id, role_name):
    mem_cfg = config.load_system().get("memory_information", {})
    d = base_dir() / mem_cfg.get("history_path", "data/history") / user_dir_id(user_id)
    return d / f"{safe_id(role_name)}.json"


def store_dir_of(user_id, role_name):
    mem_cfg = config.load_system().get("memory_information", {})
    return base_dir() / mem_cfg.get("vector_store_path", "data/vector_store") \
        / user_dir_id(user_id) / safe_id(role_name)


def emotion_path(user_id, role_name):
    return base_dir() / "data" / "emotion" / user_dir_id(user_id) / f"{safe_id(role_name)}.json"


# ---------- AI 客户端 ----------

def build_ai_client(sys_cfg=None):
    """对话模型客户端(不预置 system,由 chat_once 每轮组装)。"""
    sys_cfg = sys_cfg or config.load_system()
    tm = sys_cfg["text_model_information"]
    return ai_client.AIClient(
        api_key=tm.get("apikey", ""),
        base_url=tm.get("base_url", "https://api.deepseek.com"),
        model=tm.get("name", "deepseek-v4-flash"),
        system_prompt="",
        temperature=tm.get("temperature", 0.7),
        max_tokens=tm.get("max_tokens", 1024),
        top_p=tm.get("top_p", 1.0),
        timeout=tm.get("timeout", 60),
        frequency_penalty=tm.get("frequency_penalty"),
        presence_penalty=tm.get("presence_penalty"),
        supported_params=tm.get("supported_params"),
    )


def build_analysis_client(sys_cfg=None):
    """分析模型客户端(情绪推断/压缩/搜索决策用,低温度,可热切换)。"""
    sys_cfg = sys_cfg or config.load_system()
    am = sys_cfg["analysis_model_information"]
    return ai_client.AIClient(
        api_key=am.get("apikey", ""),
        base_url=am.get("base_url", "https://api.deepseek.com"),
        model=am.get("name", "deepseek-v4-flash"),
        system_prompt="",
        temperature=float(am.get("temperature", 0.1)),
        # 推理模型的思维链(reasoning_content)与正文共用输出预算,
        # 1024 会因思维链过长导致 content 为空;默认 4096
        max_tokens=int(am.get("max_tokens", 4096)),
        top_p=1.0,
        timeout=60,
        supported_params=am.get("supported_params"),
    )


def _complete(messages):
    """后台压缩/画像用的 LLM 调用:走分析模型,支持模型热切换。"""
    return build_analysis_client().complete(messages)


# ---------- 状态存储 ----------

def get_state():
    global _state_store
    with _state_lock:
        if _state_store is None:
            _state_store = state_mod.StateStore()
        return _state_store


# ---------- 工具注册表 ----------

def _toolkit_fingerprint(sys_cfg):
    ti = sys_cfg.get("tool_information", {})
    # 自定义工具内容变化也要重建注册表
    ct = config.load_custom_tools()
    return (bool(ti.get("enabled", True)),
            bool(ti.get("allow_generated_code", False)),
            int(ti.get("sandbox_timeout", 10)),
            json.dumps(ct, ensure_ascii=False, sort_keys=True))


def get_toolkit(sys_cfg=None):
    """按当前配置构造/缓存工具注册表(配置变化自动重建)。"""
    global _toolkit, _toolkit_fp
    sys_cfg = sys_cfg or config.load_system()
    fp = _toolkit_fingerprint(sys_cfg)
    with _toolkit_lock:
        if _toolkit is None or fp != _toolkit_fp:
            ti = sys_cfg.get("tool_information", {})
            _toolkit = tool_kit.ToolKit(
                allow_generated_code=bool(ti.get("allow_generated_code", False)),
                sandbox_timeout=int(ti.get("sandbox_timeout", 10)),
            )
            _toolkit_fp = fp
        return _toolkit


# ---------- 情绪态 ----------

def get_emotion_state(user_id, role_name):
    key = (user_id, role_name)
    with _emotion_lock:
        if key not in _emotion_states:
            _emotion_states[key] = emotion.EmotionState(
                emotion_path(user_id, role_name))
        return _emotion_states[key]


def reset_emotion(user_id, role_name):
    """把情绪重置为平静(网页手动复位)。"""
    get_emotion_state(user_id, role_name).set(
        "平静", 0.0, 0.3, current_time_str())


# ---------- 记忆管理器 ----------

def _mem_fingerprint(sys_cfg, role_name):
    mi = sys_cfg.get("memory_information", {})
    ei = sys_cfg.get("embedding_model_information", {})
    return (
        role_name,
        bool(mi.get("enabled", True)),
        int(mi.get("l1_summary_rounds", 10)),
        int(mi.get("l1_maxlen", 40)),
        ei.get("apikey", ""),
        ei.get("name", ""),
        ei.get("base_url", ""),
        int(ei.get("embedding_dimensions", 1024)),
        float(mi.get("dedup_threshold", 0.85)),
        float(mi.get("retrieval_sim_weight", 0.7)),
        float(mi.get("retrieval_min_similarity", 0.45)),
        int(mi.get("profile_message_batch", 20)),
        str(mi.get("profile_user_messages_path", "data/user_messages.json")),
    )


def _build_manager(user_id, role_name, sys_cfg):
    mem_cfg = sys_cfg.get("memory_information", {})
    emb_cfg = sys_cfg.get("embedding_model_information", {})
    enabled = bool(mem_cfg.get("enabled", True)) and bool(emb_cfg.get("apikey"))

    store = memory.MemoryStore(store_dir_of(user_id, role_name))
    l1_store = memory.L1Store(history_path(user_id, role_name),
                              maxlen=int(mem_cfg.get("l1_maxlen", 40)))
    embedder = None
    if enabled:
        embedder = memory.EmbeddingClient(
            emb_cfg["apikey"], emb_cfg["base_url"], emb_cfg["name"],
            int(emb_cfg.get("embedding_dimensions", 1024)))
    compressor = memory.Compressor(_complete) if enabled else None
    updater = None
    if enabled:
        updater = memory.ProfileUpdater(
            complete_fn=_complete,
            load_fn=config.load_profile,
            save_fn=config.save_profile,
        )
    # 画像提取只收集真实微信用户的消息;网页 'test' 测试身份不写缓冲(避免污染真实画像)
    msg_buffer = None
    if str(user_id).lower() != "test":
        msg_buffer = memory.get_user_message_buffer(
            base_dir() / mem_cfg.get("profile_user_messages_path",
                                     "data/user_messages.json"),
            batch=int(mem_cfg.get("profile_message_batch", 20)))
    return memory.MemoryManager(
        store, embedder, compressor, l1_store,
        enabled=enabled,
        summary_rounds=int(mem_cfg.get("l1_summary_rounds", 10)),
        context_turns=int(mem_cfg.get("l1_context_turns", 10)),
        top_k=int(mem_cfg.get("l2_top_k", 5)),
        profile_updater=updater,
        profile_loader=config.load_profile,
        msg_buffer=msg_buffer,
        dedup_threshold=float(mem_cfg.get("dedup_threshold", 0.85)),
        sim_weight=float(mem_cfg.get("retrieval_sim_weight", 0.7)),
        min_similarity=float(mem_cfg.get("retrieval_min_similarity", 0.45)),
    )


def get_manager(user_id, role_name, sys_cfg):
    fp = _mem_fingerprint(sys_cfg, role_name)
    with _managers_lock:
        entry = _managers.get((user_id, role_name))
        if entry is not None and entry[0] == fp:
            entry[1].sync_from_disk()   # 网页删除/清空后,按 mtime 同步内存
            return entry[1]
        mgr = _build_manager(user_id, role_name, sys_cfg)
        _managers[(user_id, role_name)] = (fp, mgr)
        return mgr


# ---------- 提醒 ----------

_REMINDER_HINTS = ("提醒", "叫我", "记得", "别忘了", "几点", "分钟后", "小时后",
                   "明天", "后天", "今晚", "今天下午", "今天上午", "明天早上")


def _looks_like_reminder(text):
    return any(h in text for h in _REMINDER_HINTS)


def parse_reminder(user_text):
    """用分析模型解析提醒意图 → {"fire_at","content"} 或 None。"""
    now = datetime.datetime.now()
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    prompt = (
        f"当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')} {weekdays[now.weekday()]}\n"
        f"用户消息: {user_text}\n\n"
        "如果用户表达了「在某个时间提醒我 / 叫我做某事」的意思,请解析出绝对提醒时间"
        "(格式 YYYY-MM-DD HH:MM:SS)和提醒内容,返回 JSON:"
        '{"fire_at": "YYYY-MM-DD HH:MM:00", "content": "提醒内容"}。'
        "相对时间(如'明天9点'、'1小时后')要换算成绝对时间。"
        "如果没有提醒意图,返回 {\"fire_at\": \"\", \"content\": \"\"}。只返回 JSON。"
    )
    raw = build_analysis_client().complete([
        {"role": "system", "content": "你是提醒解析器。只返回 JSON。"},
        {"role": "user", "content": prompt},
    ])
    try:
        s = raw.strip()
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end > start:
            data = json.loads(s[start:end + 1])
            fire_at = (data.get("fire_at") or "").strip()
            content = (data.get("content") or "").strip()
            if fire_at and content:
                return {"fire_at": fire_at[:19], "content": content}
    except Exception as e:
        logger.log_exception("提醒解析失败", error=str(e))
    return None


# ---------- 对话入口 ----------

def chat_once(role_name, user_id, user_text):
    """一轮对话(兼容旧接口,只返回文本)。"""
    reply, _meta = chat_once_with_meta(role_name, user_id, user_text)
    return reply


def chat_once_with_meta(role_name, user_id, user_text):
    """一轮对话:情绪结算 → 动态温度 → 检索 → 工具循环 → 调 AI → 记录。

    返回 (reply, meta);meta 含 temperature/emotion/valence/arousal/memories,
    供 web_ui 展示与验证动态温度效果。
    """
    config.reload_if_changed()
    sys_cfg = config.load_system()
    role = config.get_role(role_name)
    if role is None:
        raise ValueError(f"未找到角色: {role_name}")
    profile = config.load_profile()
    mem_cfg = sys_cfg.get("memory_information", {})
    emo_cfg = sys_cfg.get("emotion_information", {})
    tool_cfg = sys_cfg.get("tool_information", {})
    mgr = get_manager(user_id, role_name, sys_cfg)
    now_str = current_time_str()

    # ① 检索 L2(带情绪)→ 记忆唤起情绪
    # 检索条数按 ⌊ln(总记忆数)⌋(1~10) 动态计算(更新计划第1条)
    memories = mgr.retrieve(user_text)
    mem_evoked = emotion.memory_evoked_emotion(memories)

    tm = sys_cfg["text_model_information"]
    t_base = float(tm.get("temperature", 0.7))
    analysis = build_analysis_client(sys_cfg)
    emo_enabled = bool(emo_cfg.get("enabled", True))

    # ② 分析模型推断「用户这条消息唤起的 AI 情绪」+ 情绪结算
    if emo_enabled:
        emo_state = get_emotion_state(user_id, role_name)
        msg_emo = emotion.infer_message_emotion(
            analysis.complete, user_text, emo_state.get(), memories)
        valence, arousal = emotion.settle(
            emo_state.get(), mem_evoked, msg_emo,
            decay=float(emo_cfg.get("decay", 0.85)),
            mem_w=float(emo_cfg.get("memory_emotion_weight", 0.3)),
            user_w=float(emo_cfg.get("user_emotion_weight", 0.7)))
        label = emotion.label_of(valence, arousal)
        emo_state.set(label, valence, arousal, now_str)
    else:
        valence, arousal = 0.0, emotion.BASELINE_AROUSAL
        label = "平静"

    # ③ 动态温度 = (配置温度 + 情绪温度) / 2(情绪关闭时用配置温度)
    if emo_enabled:
        dyn_temp = emotion.emotion_temperature(
            valence, arousal, t_base,
            t_span=float(emo_cfg.get("temperature_span", 0.3)),
            t_min=float(emo_cfg.get("temperature_min", 0.1)),
            t_max=float(emo_cfg.get("temperature_max", 1.5)))
    else:
        dyn_temp = t_base

    # ④ 组装 system(人设 + 时间 + 画像 + 记忆 + 情绪提示)
    emotion_text = None
    if emo_enabled and emo_cfg.get("inject_emotion", True):
        emotion_text = emotion.emotion_hint(label, valence, arousal)
    system = config.build_system_prompt(role, profile, now_str=now_str,
                                        memories=memories, emotion_text=emotion_text)
    messages = [{"role": "system", "content": system}]
    messages += mgr.context_messages(n=int(mem_cfg.get("l1_context_turns", 10)))
    messages.append({"role": "user", "content": user_text})

    # ⑤ 工具:白名单工具 + 网络搜索(原生/工具两种方式)
    tools = None
    executor = None
    native_search = False
    if tool_cfg.get("enabled", True):
        mode = str(tool_cfg.get("web_search_mode", "auto"))
        if mode in ("auto", "native") and _is_deepseek_base(tm.get("base_url", "")):
            # DeepSeek 官方 Responses API 内置 web_search:模型自己决定是否搜索
            native_search = True
        if not native_search:
            toolkit = get_toolkit(sys_cfg)
            exclude = set()
            if "web_search" in toolkit.tools and not tool_kit.decide_search(
                    analysis.complete, user_text):
                exclude.add("web_search")
            tools = toolkit.openai_tools(exclude=exclude)
            if tools:
                executor = toolkit.execute

    # ⑥ 调对话模型(动态温度 + 工具循环 / 原生联网搜索)
    try:
        ai = build_ai_client(sys_cfg)
        if native_search:
            try:
                reply = ai.complete_with_search(messages, temperature=dyn_temp)
            except Exception as e:
                # 原生搜索失败(如模型不支持)→ 回退到 Bing 搜索工具路径
                logger.log_exception("原生联网搜索调用失败,回退到 Bing 搜索工具",
                                     user_id=user_id, role=role_name, error=str(e))
                toolkit = get_toolkit(sys_cfg)
                tools = toolkit.openai_tools(exclude=set())
                executor = toolkit.execute
                reply = ai.complete(messages, temperature=dyn_temp, tools=tools,
                                    tool_executor=executor,
                                    max_tool_rounds=int(tool_cfg.get("max_tool_rounds", 4)))
        else:
            reply = ai.complete(messages, temperature=dyn_temp, tools=tools,
                                tool_executor=executor,
                                max_tool_rounds=int(tool_cfg.get("max_tool_rounds", 4)))
        reply = clean_reply(reply)   # 剥掉模型模仿的 "[时间] AI:" 前缀
    except Exception as e:
        logger.log_exception("AI 调用失败", user_id=user_id, role=role_name)
        return "API 调用失败，请稍后重试。", {}

    final_reply = strip_separator(reply)
    mgr.add_turn(user_text, final_reply, now_str)

    # ⑦ 提醒解析(仅关键词命中才调分析模型,省钱)
    if _looks_like_reminder(user_text):
        try:
            rem = parse_reminder(user_text)
            if rem:
                get_state().add_reminder(user_id, role_name,
                                         rem["fire_at"], rem["content"])
        except Exception as e:
            logger.log_exception("提醒解析失败", user_id=user_id, error=str(e))

    meta = {
        "temperature": round(dyn_temp, 4),
        "emotion": label,
        "valence": round(valence, 4),
        "arousal": round(arousal, 4),
        "memories": memories,
        "time": now_str,
    }
    return reply, meta


# ---------- 主动对话 ----------

def proactive_once(role_name, user_id, commit=True):
    """生成一条主动消息:从高重要度记忆取话题(防重复),走情绪+动态温度。

    commit=False 时只生成预览:不消耗话题轮换序号、不把消息写进 L1。
    """
    config.reload_if_changed()
    sys_cfg = config.load_system()
    role = config.get_role(role_name)
    if role is None:
        raise ValueError(f"未找到角色: {role_name}")
    profile = config.load_profile()
    mem_cfg = sys_cfg.get("memory_information", {})
    emo_cfg = sys_cfg.get("emotion_information", {})
    mgr = get_manager(user_id, role_name, sys_cfg)
    now_str = current_time_str()

    topic = mgr.pick_proactive_topic(mark=commit)
    emo_enabled = bool(emo_cfg.get("enabled", True))
    if emo_enabled:
        emo_state = get_emotion_state(user_id, role_name)
        valence = float(emo_state.get().get("valence", 0.0))
        arousal = float(emo_state.get().get("arousal", 0.3))
    else:
        valence, arousal = 0.0, emotion.BASELINE_AROUSAL
    label = emotion.label_of(valence, arousal)

    tm = sys_cfg["text_model_information"]
    t_base = float(tm.get("temperature", 0.7))
    if emo_enabled:
        dyn_temp = emotion.emotion_temperature(
            valence, arousal, t_base,
            t_span=float(emo_cfg.get("temperature_span", 0.3)),
            t_min=float(emo_cfg.get("temperature_min", 0.1)),
            t_max=float(emo_cfg.get("temperature_max", 1.5)))
    else:
        dyn_temp = t_base

    if topic:
        topic_line = (
            "你想起了一条高重要度的记忆:"
            f"「{topic['text']}」({topic.get('time', '')})。"
            "围绕这条记忆,自然地给用户起一个话头(比如询问进展、聊聊相关的事)。")
    else:
        topic_line = "根据当前时间和你们的画像,自然地向用户打个招呼或关心近况。"

    system = config.build_system_prompt(
        role, profile, now_str=now_str,
        emotion_text=(emotion.emotion_hint(label, valence, arousal)
                      if emo_enabled and emo_cfg.get("inject_emotion", True)
                      else None))
    messages = [
        {"role": "system", "content": system + "\n\n【主动发消息】" + topic_line},
    ]
    messages += mgr.context_messages(
        n=min(4, int(mem_cfg.get("l1_context_turns", 10))))
    messages.append({"role": "user",
                     "content": "（现在请你主动向用户发一条消息,直接输出消息内容。）"})

    try:
        reply = clean_reply(
            build_ai_client(sys_cfg).complete(messages, temperature=dyn_temp))
    except Exception as e:
        logger.log_exception("主动消息生成失败", user_id=user_id, role=role_name)
        return "API 调用失败，请稍后重试。"

    clean = strip_separator(reply)
    if commit:
        mgr.add_turn("", clean, now_str)   # AI 独白写进 L1(用户消息为空)
    return reply


# ---------- 记忆查看/管理(供 web_ui 使用) ----------

def get_l1_turns(user_id, role_name):
    """L1 全部轮次(走缓存管理器,与逐条删除操作同一份内存)。"""
    return get_manager(user_id, role_name, config.load_system()).l1.all()


def get_l2_items(user_id, role_name):
    """L2 全部条目(走缓存管理器,与逐条删除操作同一份内存)。"""
    return get_manager(user_id, role_name, config.load_system()).store.items()


def get_l2_topics(user_id, role_name):
    """L2 记忆的话题列表(去重),供网页过滤下拉框。"""
    return get_manager(user_id, role_name, config.load_system()).store.all_topics()


def get_pending_profile_messages():
    """用户消息缓冲当前积攒条数(画像提取用,供网页展示)。"""
    mi = config.load_system().get("memory_information", {})
    buf = memory.get_user_message_buffer(
        base_dir() / mi.get("profile_user_messages_path",
                            "data/user_messages.json"),
        batch=int(mi.get("profile_message_batch", 20)))
    return buf.count()


def delete_l1_turn(user_id, role_name, index):
    """删除 L1 第 index 轮(0 起):直接改缓存管理器对象,内存与磁盘同步。"""
    mgr = get_manager(user_id, role_name, config.load_system())
    mgr.l1.remove_at(int(index))
    return True


def delete_l2_item(user_id, role_name, item_id):
    """按 id 删除一条 L2:元数据与对应向量行同步删除,内存与磁盘同步。"""
    mgr = get_manager(user_id, role_name, config.load_system())
    mgr.store.remove_by_id(str(item_id))
    return True


def clear_memory(user_id, role_name, clear_l1=True, clear_l2=False):
    """清空 L1/L2(可分别指定),并失效进程内缓存的管理器。"""
    mi = config.load_system().get("memory_information", {})
    if clear_l1:
        memory.L1Store(history_path(user_id, role_name), maxlen=int(mi.get("l1_maxlen", 40))).clear()
    if clear_l2:
        memory.MemoryStore(store_dir_of(user_id, role_name)).clear()
    with _managers_lock:
        _managers.pop((user_id, role_name), None)
