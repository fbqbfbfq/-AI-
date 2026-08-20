# -*- coding: utf-8 -*-
"""
配置完整性校验:检查"生成 AI 内容所需的内容"是否完整。

返回逐项报告 [{"level":"ok|warn|error","item","message","detail"}],
供 web_ui「检查配置」按钮与 bot 启动自检使用。
"""
import importlib
import os
from pathlib import Path

import config

BASE = Path(__file__).resolve().parent


def _check_text_model(sys_cfg):
    tm = sys_cfg.get("text_model_information", {})
    missing = []
    for k, label in (("name", "模型名"), ("apikey", "apikey"), ("base_url", "base_url")):
        if not tm.get(k):
            missing.append(label)
    if missing:
        return {"level": "error", "item": "文本模型",
                "message": f"缺少: {', '.join(missing)}"}
    return {"level": "ok", "item": "文本模型", "message": tm.get("name", "")}


def _check_embedding(sys_cfg):
    mi = sys_cfg.get("memory_information", {})
    ei = sys_cfg.get("embedding_model_information", {})
    if not mi.get("enabled", True):
        return {"level": "warn", "item": "Embedding",
                "message": "长期记忆已关闭,无需配置 embedding"}
    missing = []
    for k, label in (("name", "模型名"), ("apikey", "apikey"), ("base_url", "base_url")):
        if not ei.get(k):
            missing.append(label)
    if missing:
        return {"level": "error", "item": "Embedding",
                "message": f"缺少: {', '.join(missing)}"}
    return {"level": "ok", "item": "Embedding", "message": ei.get("name", "")}


def _check_analysis_model(sys_cfg):
    """分析模型(情绪/压缩/搜索决策)配置检查;无消费者时仅告警。"""
    am = sys_cfg.get("analysis_model_information", {})
    mi = sys_cfg.get("memory_information", {})
    ei = sys_cfg.get("emotion_information", {})
    ti = sys_cfg.get("tool_information", {})
    used = bool(mi.get("enabled", True)) or bool(ei.get("enabled", True)) \
        or bool(ti.get("enabled", True))
    missing = []
    for k, label in (("name", "模型名"), ("apikey", "apikey"), ("base_url", "base_url")):
        if not am.get(k):
            missing.append(label)
    if missing:
        level = "error" if used else "warn"
        return {"level": level, "item": "分析模型",
                "message": f"缺少: {', '.join(missing)}"
                           + ("" if used else "(情绪/记忆/工具均关闭,暂不影响)")}
    return {"level": "ok", "item": "分析模型", "message": am.get("name", "")}


def _check_tools(sys_cfg):
    """工具配置检查:目录可读 + 白名单安全工具数量。"""
    if not sys_cfg.get("tool_information", {}).get("enabled", True):
        return {"level": "warn", "item": "工具", "message": "工具调用已关闭"}
    try:
        import tool_kit
        found = tool_kit.discover_tools()
        safe = [n for n, s, _ in found if s.get("safe")]
        if not found:
            return {"level": "warn", "item": "工具",
                    "message": "tools/ 目录下没有发现工具"}
        return {"level": "ok", "item": "工具",
                "message": f"发现 {len(found)} 个工具,其中白名单安全 {len(safe)} 个"}
    except Exception as e:
        return {"level": "error", "item": "工具", "message": f"工具加载失败: {e}"}


def _check_roles(sys_cfg):
    roles = config.load_roles()
    if not roles:
        return {"level": "error", "item": "角色配置",
                "message": "还没有任何角色,你需要一个角色(请在网页新建)"}
    active = sys_cfg.get("active_role", "")
    if not active:
        return {"level": "ok", "item": "角色配置",
                "message": f"启用角色: {roles[0]['name']}(未显式设置,默认第一个,共 {len(roles)} 个)"}
    role = config.get_role(active)
    if role is None:
        return {"level": "warn", "item": "角色配置",
                "message": f"active_role='{active}' 不存在,将回退到第一个角色 '{roles[0]['name']}'"}
    if not (role.get("prompt") or "").strip():
        return {"level": "error", "item": "角色配置",
                "message": f"角色 '{active}' 的提示词为空,请编写提示词"}
    return {"level": "ok", "item": "角色配置",
            "message": f"当前角色: {active}(共 {len(roles)} 个角色)"}


def _check_profile():
    prof = config.load_profile()
    if not isinstance(prof, dict):
        return {"level": "warn", "item": "用户画像", "message": "画像格式异常"}
    return {"level": "ok", "item": "用户画像", "message": "画像可正常解析"}


def _check_paths(sys_cfg):
    mi = sys_cfg.get("memory_information", {})
    bad = []
    for key in ("vector_store_path", "history_path"):
        p = BASE / mi.get(key, "")
        try:
            p.mkdir(parents=True, exist_ok=True)
            if not os.access(str(p), os.W_OK):
                bad.append(key)
        except Exception:
            bad.append(key)
    try:
        logs_dir = BASE / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        if not os.access(str(logs_dir), os.W_OK):
            bad.append("logs")
    except Exception:
        bad.append("logs")
    if bad:
        return {"level": "error", "item": "路径",
                "message": f"以下路径不可写: {', '.join(bad)}"}
    return {"level": "ok", "item": "路径", "message": "数据目录可写"}


def _check_deps():
    bad = []
    for mod in ("requests", "numpy", "streamlit", "chromadb"):
        try:
            importlib.import_module(mod)
        except Exception:
            bad.append(mod)
    if bad:
        return {"level": "error", "item": "依赖",
                "message": f"缺少库: {', '.join(bad)}(请运行 setup.bat 安装)"}
    return {"level": "ok", "item": "依赖",
            "message": "requests / numpy / streamlit / chromadb 已安装"}


def _check_separator(sys_cfg):
    si = sys_cfg.get("system_information", {})
    if si.get("segmented_reply", True):
        sep = si.get("segment_separator", "")
        if not (sep or "").strip():
            return {"level": "warn", "item": "分段分隔符",
                    "message": "已开启分段回复,但 segment_separator 为空/纯空格"}
    return {"level": "ok", "item": "分段分隔符", "message": "ok"}


def check_all():
    """本地检查(不联网):文本模型/embedding/分析模型/角色/画像/路径/依赖/工具/分隔符。"""
    sys_cfg = config.load_system()
    return [
        _check_text_model(sys_cfg),
        _check_embedding(sys_cfg),
        _check_analysis_model(sys_cfg),
        _check_roles(sys_cfg),
        _check_profile(),
        _check_paths(sys_cfg),
        _check_deps(),
        _check_tools(sys_cfg),
        _check_separator(sys_cfg),
    ]


def check_connectivity(timeout=5):
    """可选联网检查:对 base_url 发 GET,超时 5 秒。"""
    tm = config.load_system().get("text_model_information", {})
    url = (tm.get("base_url") or "").rstrip("/")
    if not url:
        return {"level": "warn", "item": "连通性", "message": "未配置 base_url"}
    try:
        import requests
        r = requests.get(url + "/", timeout=timeout)
        return {"level": "ok", "item": "连通性",
                "message": f"base_url 可达(HTTP {r.status_code})"}
    except Exception as e:
        return {"level": "error", "item": "连通性",
                "message": f"无法连接 base_url: {e}"}


def blocking_error(results):
    """返回会阻止启动的错误(缺文本模型 key / 无角色);无则 None。"""
    for r in results:
        if r.get("level") == "error" and r.get("item") in ("文本模型", "角色配置"):
            return r
    return None


def has_error(results):
    return any(r.get("level") == "error" for r in results)
