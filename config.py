# -*- coding: utf-8 -*-
"""
配置层:系统配置 / 角色配置(多角色) / 用户画像。

- 内存缓存 + 硬盘双写(write-through):load_* 读缓存,save_* 先改内存再落盘。
- 热切换:reload_if_changed() 每轮校验文件 mtime,变化才重读。
- 损坏自愈:文件损坏时删除并重建默认值,写日志并提示。
- 多角色:角色列表自由增删改;提示词纯自由文本,不强制任何默认人设。
- active_role:当前启用角色;删除后回退第一个;列表为空抛"需要一个角色"。

密钥策略:优先读环境变量,找不到退回系统注册表(MACHINE 级)。
"""
import json
import os
import threading
from pathlib import Path

import file_util
import logger

HERE = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get("BOT_CONFIG_DIR", HERE / "config"))

SYSTEM_NAME = "系统配置文件"
ROLE_NAME = "角色配置文件"
PROFILE_NAME = "用户画像"
TOOL_NAME = "工具配置"

SYSTEM_FILE = CONFIG_DIR / f"{SYSTEM_NAME}.json"
ROLE_FILE = CONFIG_DIR / f"{ROLE_NAME}.json"
PROFILE_FILE = CONFIG_DIR / f"{PROFILE_NAME}.json"
TOOL_FILE = CONFIG_DIR / f"{TOOL_NAME}.json"

# ---------- 默认配置 ----------

# 模型支持的采样参数全集;按预设可裁剪(部分模型不支持 penalty/top_p 等)。
ALL_SAMPLING_PARAMS = ["temperature", "max_tokens", "top_p",
                       "frequency_penalty", "presence_penalty"]

DEFAULT_SYSTEM = {
    "active_role": "",
    "text_model_information": {
        "name": "deepseek-v4-flash",
        "apikey": "",
        "base_url": "https://api.deepseek.com",
        "temperature": 0.7,
        "max_tokens": 1024,
        "top_p": 1.0,
        "frequency_penalty": 0.4,      # 重复性审查:越高越不容易重复用词(0~2,建议 0.3~0.5)
        "presence_penalty": 0.3,       # 话题审查:越高越倾向于聊新话题(0~2,建议 0.2~0.4)
        "timeout": 60,
        "supported_params": list(ALL_SAMPLING_PARAMS),
    },
    "embedding_model_information": {
        "name": "text-embedding-v4",
        "apikey": "",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "embedding_dimensions": 1024,
    },
    "analysis_model_information": {
        "name": "deepseek-v4-flash",
        "apikey": "",
        "base_url": "https://api.deepseek.com",
        "temperature": 0.1,
        "max_tokens": 4096,        # 推理模型思维链也占用预算,1024 会被吃光导致 content 为空
        "output_format": "json",
        "supported_params": ["temperature", "max_tokens", "top_p"],
    },
    "memory_information": {
        "enabled": True,
        "l1_context_turns": 10,
        "l1_summary_rounds": 10,
        "l1_maxlen": 10,               # L1 仅保留近 10 轮
        "l2_top_k": 5,                 # 保留兼容;实际检索条数按 ⌊ln(总记忆数)⌋(1~10) 动态计算
        "vector_store_path": "data/vector_store",
        "history_path": "data/history",
        "dedup_threshold": 0.85,
        "retrieval_sim_weight": 0.7,
        "retrieval_min_similarity": 0.45,   # 检索门槛:相似度低于 0.45(45%)的记忆不召回
        "profile_message_batch": 20,        # 攒满多少条用户消息提取一次画像
        "profile_user_messages_path": "data/user_messages.json",  # 用户消息缓冲(内存+JSON 落盘)
        "importance_half_life_days": 14,
        "hnsw_m": 32,                  # ChromaDB HNSW 参数(500~5000 条数据量适用)
        "hnsw_ef_construction": 200,
        "hnsw_ef_search": 60,
    },
    "emotion_information": {
        "enabled": True,
        "temperature_span": 0.3,
        "temperature_min": 0.1,
        "temperature_max": 1.5,
        "decay": 0.85,
        "inject_emotion": True,
        "memory_emotion_weight": 0.3,
        "user_emotion_weight": 0.7,
    },
    "tool_information": {
        "enabled": True,
        "allow_generated_code": False,
        "sandbox_timeout": 10,
        "max_tool_rounds": 4,
        # 联网搜索方式:auto=DeepSeek 用官方原生搜索、其他模型用 Bing 工具;
        # native=强制原生(仅 DeepSeek 可用);tool=始终用内置 Bing 搜索工具
        "web_search_mode": "auto",
    },
    "proactive_information": {
        "enabled": False,
        "interval_hours": 3,
        "start_hour": 8,
        "end_hour": 23,
        "token_fresh_minutes": 30,
    },
    "system_information": {
        "version": "0.6.0",
        "language": "zh-CN",
        "log_level": "ERROR",
        "segmented_reply": True,
        "segment_separator": "|||",
        "time_format": "%Y-%m-%d %A %H:%M:%S",
        "inject_time": True,
        "inject_profile": True,
        "inject_memory": True,
        "typing_seconds_per_char": 0.1,   # 模拟真人打字:每多一个字增加的秒数(0=关闭模拟)
        "typing_max_seconds": 10.0,       # 模拟打字停顿的上限(不超过 10 秒)
    },
}

# 不再硬编码任何默认人设:角色列表为空即提示"需要一个角色"。
DEFAULT_ROLE = []

DEFAULT_PROFILE = {
    "称呼": "",
    "关系": "朋友",
    "性别": "",
    "生日": "",
    "爱好": [],
    "性格": [],
    "家庭环境": "",
    "居住环境": "",
    "长期事实": [],
    "updated_at": "",
}

# 画像只记「长期不变」的内容;近期事件/临时计划属于 L2 对话记忆,不属于画像。
PROFILE_KEYS = ["称呼", "关系", "性别", "生日", "爱好", "性格",
                "家庭环境", "居住环境", "长期事实"]

# ---------- 模型预设(多模型适配) ----------
# 业界通用做法:统一走 OpenAI 兼容协议,每个供应商一份预设(base_url/模型列表/支持的参数),
# 按预设自动过滤该模型不支持的采样参数,避免 400 报错。
PRESET_NAME = "模型预设"

DEFAULT_MODEL_PRESETS = [
    {"id": "deepseek", "name": "DeepSeek(官方)", "base_url": "https://api.deepseek.com",
     "models": ["deepseek-chat", "deepseek-reasoner"],
     "supported_params": ["temperature", "max_tokens", "top_p",
                          "frequency_penalty", "presence_penalty"],
     "desc": "国内直连,OpenAI 兼容。deepseek-chat 日常对话;deepseek-reasoner 深度推理(不支持部分采样参数)。"},
    {"id": "openai", "name": "OpenAI(官方)", "base_url": "https://api.openai.com/v1",
     "models": ["gpt-4o-mini", "gpt-4o", "o3-mini"],
     "supported_params": ["temperature", "max_tokens", "top_p",
                          "frequency_penalty", "presence_penalty"],
     "desc": "需海外网络与 OpenAI key,OpenAI 兼容协议的标准实现。"},
    {"id": "dashscope", "name": "阿里云百炼 DashScope", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "models": ["qwen-plus", "qwen-max", "qwen-turbo"],
     "supported_params": ["temperature", "max_tokens", "top_p",
                          "frequency_penalty", "presence_penalty"],
     "desc": "通义千问,OpenAI 兼容,国内直连,key 与 embedding 同源。"},
    {"id": "glm", "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "models": ["glm-4-plus", "glm-4-flash"],
     "supported_params": ["temperature", "max_tokens", "top_p"],
     "desc": "国内直连;不支持 frequency/presence penalty,已自动过滤这两个参数。"},
    {"id": "kimi", "name": "Moonshot Kimi", "base_url": "https://api.moonshot.cn/v1",
     "models": ["moonshot-v1-8k", "moonshot-v1-32k", "kimi-latest"],
     "supported_params": ["temperature", "max_tokens", "top_p"],
     "desc": "国内直连;不支持 frequency/presence penalty,已自动过滤。"},
    {"id": "siliconflow", "name": "硅基流动 SiliconFlow", "base_url": "https://api.siliconflow.cn/v1",
     "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen3-32B"],
     "supported_params": ["temperature", "max_tokens", "top_p",
                          "frequency_penalty", "presence_penalty"],
     "desc": "聚合多家开源模型,国内直连,模型名带前缀(如 deepseek-ai/DeepSeek-V3)。"},
    {"id": "ollama", "name": "Ollama(本地)", "base_url": "http://localhost:11434/v1",
     "models": ["qwen2.5:7b", "llama3.1:8b"],
     "supported_params": ["temperature", "max_tokens", "top_p"],
     "desc": "本地部署,免费离线;先 ollama pull 模型,key 随便填占位即可。"},
]

# ---------- 内存缓存(write-through) ----------

_cache = {}
_cache_lock = threading.Lock()


def _get_cached(path):
    with _cache_lock:
        return _cache.get(str(path))


def _set_cached(path, data):
    with _cache_lock:
        mtime = path.stat().st_mtime if path.exists() else -1
        _cache[str(path)] = {"mtime": mtime, "data": data}


def _cache_hit(path):
    entry = _get_cached(path)
    if entry is None:
        return None, False
    mtime = path.stat().st_mtime if path.exists() else -1
    if entry["mtime"] != mtime:
        return None, False
    return entry["data"], True


def _merge_defaults(current, defaults):
    """深合并:补齐缺失字段,兼容旧配置文件。"""
    if isinstance(current, dict) and isinstance(defaults, dict):
        merged = dict(current)
        for k, v in defaults.items():
            if k not in merged:
                merged[k] = v
            else:
                merged[k] = _merge_defaults(merged[k], v)
        return merged
    return current


# ---------- 底层读写 ----------

def get_key(env_name):
    """读密钥:先环境变量,再系统注册表(MACHINE 级)。"""
    v = os.environ.get(env_name)
    if v:
        return v
    if os.name == "nt":
        try:
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            )
            v, _ = winreg.QueryValueEx(k, env_name)
            return v
        except FileNotFoundError:
            return ""   # 注册表里没有该值,属正常情况(密钥配置在文件里)
        except Exception as e:
            logger.log_exception("读取注册表密钥失败", env=env_name, error=str(e))
    return ""


def _on_corrupt(path, exc):
    msg = f"检测到文件损坏,已删除并重新生成: {path}"
    logger.log_error(msg, error=str(exc))
    print(f"[config] {msg}", flush=True)


def _fill_keys(cfg):
    """密钥为空时从环境/注册表补齐(仅内存,不写回,避免 mtime 抖动)。"""
    for section, key_env in (
        ("text_model_information", "DEEPSEEK_API_KEY"),
        ("embedding_model_information", "DASHSCOPE_API_KEY"),
        ("analysis_model_information", "DEEPSEEK_API_KEY"),
    ):
        sec = cfg.setdefault(section, {})
        if not sec.get("apikey"):
            sec["apikey"] = get_key(key_env)
    return cfg


# ---------- 系统配置 ----------

def load_system():
    data, hit = _cache_hit(SYSTEM_FILE)
    if hit:
        return data
    data = file_util.safe_load_json(CONFIG_DIR, SYSTEM_NAME, DEFAULT_SYSTEM, _on_corrupt)
    if not isinstance(data, dict):
        data = {}
    data = _merge_defaults(data, DEFAULT_SYSTEM)
    data = _fill_keys(data)
    _set_cached(SYSTEM_FILE, data)
    return data


def save_system(cfg):
    file_util.save_json(CONFIG_DIR, SYSTEM_NAME, cfg)
    _set_cached(SYSTEM_FILE, cfg)


# ---------- 角色配置(多角色) ----------

def load_roles():
    data, hit = _cache_hit(ROLE_FILE)
    if hit:
        return data
    data = file_util.safe_load_json(CONFIG_DIR, ROLE_NAME, DEFAULT_ROLE, _on_corrupt)
    if not isinstance(data, list):
        data = []
    _set_cached(ROLE_FILE, data)
    return data


def save_roles(roles):
    file_util.save_json(CONFIG_DIR, ROLE_NAME, roles)
    _set_cached(ROLE_FILE, roles)


def get_role(name):
    idx = file_util.find_index(load_roles(), "name", name)
    return load_roles()[idx] if idx is not None else None


def add_role(name, prompt=""):
    roles = load_roles()
    if file_util.find_index(roles, "name", name) is not None:
        raise ValueError(f"角色 '{name}' 已存在")
    roles.append({"name": name, "prompt": prompt})
    save_roles(roles)
    return roles


def update_role(name, prompt):
    roles = load_roles()
    idx = file_util.find_index(roles, "name", name)
    if idx is None:
        raise ValueError(f"未找到角色: {name}")
    roles[idx]["prompt"] = prompt
    save_roles(roles)
    return roles


def delete_role(name):
    """删除角色;若删的是 active_role,自动回退到第一个。"""
    roles = load_roles()
    idx = file_util.find_index(roles, "name", name)
    if idx is None:
        raise ValueError(f"未找到角色: {name}")
    roles.pop(idx)
    save_roles(roles)
    sys_cfg = load_system()
    if sys_cfg.get("active_role") == name:
        sys_cfg["active_role"] = roles[0]["name"] if roles else ""
        save_system(sys_cfg)
    return roles


def load_role():
    """兼容旧接口:返回第一个角色(供 bot_ilink.py 等旧代码)。"""
    roles = load_roles()
    return roles[0] if roles else {}


def get_active_role():
    """返回当前启用角色;active_role 缺失回退第一个;列表为空抛异常。"""
    roles = load_roles()
    if not roles:
        raise ValueError("还没有任何角色,请先在网页里新建一个角色(你需要一个角色)")
    active = load_system().get("active_role", "")
    idx = file_util.find_index(roles, "name", active)
    return roles[idx] if idx is not None else roles[0]


def set_active_role(name):
    sys_cfg = load_system()
    sys_cfg["active_role"] = name
    save_system(sys_cfg)


# ---------- 用户画像 ----------

def _migrate_old_profile(data):
    """旧画像字段迁移:facts→长期事实 / prefs→爱好 / traits→性格;
    events(近期事项)不再属于画像,直接丢弃(事件类内容由 L2 记忆承担)。"""
    if "长期事实" not in data and "facts" in data:
        data["长期事实"] = data.pop("facts")
    if "爱好" not in data and "prefs" in data:
        data["爱好"] = data.pop("prefs")
    if "性格" not in data and "traits" in data:
        data["性格"] = data.pop("traits")
    for k in ("facts", "prefs", "traits", "events"):
        data.pop(k, None)
    return data


def load_profile():
    data, hit = _cache_hit(PROFILE_FILE)
    if hit:
        return data
    data = file_util.safe_load_json(CONFIG_DIR, PROFILE_NAME, DEFAULT_PROFILE, _on_corrupt)
    if not isinstance(data, dict):
        data = {}
    data = _migrate_old_profile(data)
    data = _merge_defaults(data, DEFAULT_PROFILE)
    _set_cached(PROFILE_FILE, data)
    return data


def save_profile(prof):
    file_util.save_json(CONFIG_DIR, PROFILE_NAME, prof)
    _set_cached(PROFILE_FILE, prof)


# ---------- 模型预设 ----------

def load_model_presets():
    """读取模型预设列表;文件不存在/损坏时用内置默认值重建。"""
    data = file_util.safe_load_json(CONFIG_DIR, PRESET_NAME, DEFAULT_MODEL_PRESETS, _on_corrupt)
    if not isinstance(data, list) or not data:
        data = DEFAULT_MODEL_PRESETS
    return data


def save_model_presets(presets):
    file_util.save_json(CONFIG_DIR, PRESET_NAME, presets)


# ---------- 自定义工具(用户白名单) ----------

DEFAULT_CUSTOM_TOOLS = {"custom_tools": []}


def load_custom_tools():
    """读取用户自定义工具列表;损坏时重建默认(空列表)。"""
    data, hit = _cache_hit(TOOL_FILE)
    if hit:
        return data
    data = file_util.safe_load_json(CONFIG_DIR, TOOL_NAME, DEFAULT_CUSTOM_TOOLS, _on_corrupt)
    if not isinstance(data, dict):
        data = dict(DEFAULT_CUSTOM_TOOLS)
    tools = data.get("custom_tools") or []
    tools = [t for t in tools if isinstance(t, dict)]
    _set_cached(TOOL_FILE, tools)
    return tools


def save_custom_tools(tools):
    file_util.save_json(CONFIG_DIR, TOOL_NAME, {"custom_tools": list(tools)})
    _set_cached(TOOL_FILE, list(tools))


# ---------- 热切换 / 自愈 ----------

def reload_if_changed():
    """热切换:文件 mtime 变化则清对应缓存(下次 load 重读)。bot 每轮调用。"""
    with _cache_lock:
        for key, entry in list(_cache.items()):
            path = Path(key)
            mtime = path.stat().st_mtime if path.exists() else -1
            if entry["mtime"] != mtime:
                _cache.pop(key, None)


def reload_all():
    """强制清缓存并重读(前端"刷新"按钮)。"""
    with _cache_lock:
        _cache.clear()
    load_system()
    load_roles()
    load_profile()


def repair_corrupted_files():
    """启动自检:检测并修复所有配置文件(损坏则删除+重建+日志+提示)。"""
    load_system()
    load_roles()
    load_profile()


# ---------- 提示词组装 ----------

def _profile_lines(profile):
    """把画像转成提示词行,只输出有内容的部分(均为长期不变的信息)。"""
    profile = profile or {}
    lines = []
    for key in ("称呼", "关系", "性别", "生日"):
        if profile.get(key):
            lines.append(f"{key}: {profile[key]}")
    for key in ("爱好", "性格", "长期事实"):
        items = profile.get(key) or []
        if items:
            lines.append(f"{key}: " + "; ".join(items))
    for key in ("家庭环境", "居住环境"):
        if profile.get(key):
            lines.append(f"{key}: {profile[key]}")
    return lines


def build_system_prompt(role, profile, now_str=None, memories=None, emotion_text=None):
    """组装最终 system prompt:角色提示词 + 时间/画像/记忆/情绪(各段可开关)。"""
    parts = [(role or {}).get("prompt", "").strip()]
    si = load_system().get("system_information", {})
    ei = load_system().get("emotion_information", {})

    if si.get("inject_time", True) and now_str:
        parts.append(f"【系统信息】当前时间：{now_str}。"
                     f"如果用户问现在几点/今天几号/星期几，请据此回答。")

    if si.get("inject_profile", True):
        lines = _profile_lines(profile)
        if lines:
            parts.append("## 用户画像\n" + "\n".join(lines))

    if si.get("inject_memory", True) and memories:
        mem_lines = []
        for m in memories:
            text = (m.get("text") or "").strip()
            t = (m.get("time") or "").strip()
            if text:
                mem_lines.append(f"- ({t}) {text}" if t else f"- {text}")
        if mem_lines:
            parts.append("## 相关记忆（长期）\n" + "\n".join(mem_lines))

    if ei.get("inject_emotion", True) and emotion_text:
        parts.append(emotion_text)

    return "\n\n".join(p for p in parts if p)
