# -*- coding: utf-8 -*-
"""
Streamlit 管理前端(UPGRADE_DESIGN.md v2):
多角色管理 + 系统配置(含分析模型/情绪设置) + 记忆查看/逐条删除(可切管理对象)
+ 用户画像(只记长期不变内容) + 对话测试 + 工具管理(白名单/生成代码开关+红字警告)
+ 情绪查看 + 主动对话设置。

运行:
    streamlit run web_ui.py

UI 不直接操作文件/API,全部通过 config.py / service.py / check.py / tool_kit.py。
"""
import copy
import json

import streamlit as st

import check
import config
import service
import tool_kit

st.set_page_config(page_title="微信 AI 分身 · 管理", page_icon="🤖", layout="wide")

# ---------- session_state ----------
if "current_role" not in st.session_state:
    st.session_state.current_role = None
if "messages" not in st.session_state:
    st.session_state.messages = []


def load_history(role_name):
    turns = service.get_l1_turns("test", role_name)
    msgs = []
    for t in turns:
        if t.get("user"):
            msgs.append({"role": "user", "content": t.get("user", ""),
                         "time": t.get("time", "")})
        if t.get("assistant"):
            msgs.append({"role": "assistant",
                         "content": service.strip_separator(t.get("assistant", "")),
                         "time": t.get("time", "")})
    return msgs


def _list_editor(label, key, items):
    txt = st.text_area(label, "\n".join(items or []), height=80, key=key)
    return [l.strip() for l in txt.split("\n") if l.strip()]


def _show_check_results():
    results = check.check_all() + [check.check_connectivity()]
    for r in results:
        if r["level"] == "ok":
            st.success(f"✓ {r['item']}: {r['message']}")
        elif r["level"] == "warn":
            st.warning(f"⚠ {r['item']}: {r['message']}")
        else:
            st.error(f"✗ {r['item']}: {r['message']}")


# ---------- 侧边栏:角色选择 ----------
with st.sidebar:
    st.title("🤖 角色")
    roles = config.load_roles()
    if not roles:
        st.info("暂无角色,请到「🎭 角色/提示词」页签新建")
    else:
        names = [r["name"] for r in roles]
        default_idx = 0
        if st.session_state.current_role in names:
            default_idx = names.index(st.session_state.current_role)
        sel = st.selectbox("选择对话角色", names, index=default_idx)
        if sel != st.session_state.current_role:
            st.session_state.current_role = sel
            st.session_state.messages = load_history(sel)
            st.rerun()
    st.divider()
    st.subheader("🧠 记忆管理对象")
    users = service.list_user_ids()
    if "manage_user" not in st.session_state or st.session_state.manage_user not in users:
        st.session_state.manage_user = users[0]
    manage_user = st.selectbox(
        "查看/清理哪个用户的记忆与情绪",
        users,
        format_func=service.user_display_label,
        key="manage_user",
    )
    st.divider()
    st.caption("所有修改保存后即时生效,微信 bot 无需重启。")

# ---------- 主区页签 ----------
tab_chat, tab_role, tab_sys, tab_mem, tab_profile, tab_tool, tab_emo, tab_pro = st.tabs(
    ["💬 对话", "🎭 角色/提示词", "⚙️ 系统配置", "🧠 记忆",
     "👤 用户画像", "🛠️ 工具", "💗 情绪", "📣 主动对话"])

# ============ 页签 1:对话测试 ============
with tab_chat:
    st.subheader("对话测试")
    role_name = st.session_state.current_role
    if not role_name:
        st.info("请先在侧边栏选择角色,或到「🎭 角色/提示词」页签新建角色。")
    else:
        st.caption(f"当前角色: {role_name} · 测试身份 user_id='test'(与微信用户隔离,不写入该用户记忆)")
        for m in st.session_state.messages:
            with st.chat_message(m["role"]):
                st.markdown(m["content"])
                if m.get("time"):
                    st.caption(m["time"])
        c1, _ = st.columns([0.4, 0.6])
        if c1.button("🧹 清空测试对话", key="clear_chat_test"):
            service.clear_memory("test", role_name, clear_l1=True, clear_l2=False)
            st.session_state.messages = []
            st.success("已清空测试对话(屏幕记录 + test 身份的 L1 记忆)")
            st.rerun()
        user_input = st.chat_input("输入消息...")
        if user_input:
            with st.chat_message("user"):
                st.markdown(user_input)
            with st.chat_message("assistant"):
                with st.spinner("思考中..."):
                    try:
                        reply, meta = service.chat_once_with_meta(
                            role_name, "test", user_input)
                    except Exception as e:
                        reply, meta = f"❌ 错误: {e}", {}
                st.markdown(service.strip_separator(reply))
                if meta:
                    st.caption(
                        f"🌡️ 温度 {meta.get('temperature')} | "
                        f"💗 情绪 {meta.get('emotion')} "
                        f"(效价 {float(meta.get('valence', 0)):+.2f}, "
                        f"唤醒度 {float(meta.get('arousal', 0)):.2f})")
            st.session_state.messages = load_history(role_name)
            st.rerun()

# ============ 页签 2:角色/提示词 ============
with tab_role:
    st.subheader("角色 / 提示词")

    roles = config.load_roles()
    if roles:
        active = config.load_system().get("active_role", "")
        names = [r["name"] for r in roles]
        active_idx = names.index(active) if active in names else 0
        new_active = st.selectbox("当前启用角色(供微信 bot 使用)", names, index=active_idx)
        if new_active != active:
            config.set_active_role(new_active)
            st.success(f"已启用角色 '{new_active}',即时生效")
            st.rerun()

    for r in roles:
        with st.expander(f"{r['name']}", expanded=False):
            new_prompt = st.text_area("提示词(自由编写,长度不限)", r.get("prompt", ""),
                                      height=220, key=f"prompt_{r['name']}")
            c1, c2 = st.columns(2)
            if c1.button("💾 保存提示词", key=f"save_{r['name']}"):
                config.update_role(r["name"], new_prompt)
                st.success("已保存")
                st.rerun()
            if c2.button("🗑️ 删除角色", key=f"del_{r['name']}"):
                if st.session_state.get(f"confirm_del_{r['name']}"):
                    config.delete_role(r["name"])
                    service.clear_memory("test", r["name"], clear_l1=True, clear_l2=True)
                    if st.session_state.current_role == r["name"]:
                        st.session_state.current_role = None
                        st.session_state.messages = []
                    st.session_state.pop(f"confirm_del_{r['name']}", None)
                    st.success(f"已删除角色 '{r['name']}'(若为启用角色已回退到第一个)")
                    st.rerun()
                else:
                    st.session_state[f"confirm_del_{r['name']}"] = True
                    st.warning(f"再次点击「删除」确认删除 '{r['name']}'(连带清空其记忆)")
                    st.rerun()

    st.divider()
    with st.form("new_role_form"):
        st.subheader("➕ 新建角色")
        new_name = st.text_input("角色名")
        new_prompt = st.text_area("提示词(自由编写,长度不限)", height=220)
        if st.form_submit_button("创建角色", type="primary"):
            if not (new_name or "").strip():
                st.error("角色名不能为空")
            elif not (new_prompt or "").strip():
                st.error("提示词不能为空")
            else:
                try:
                    config.add_role(new_name.strip(), new_prompt)
                    st.success(f"已创建角色 '{new_name.strip()}'")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

# ============ 页签 3:系统配置 ============
with tab_sys:
    st.subheader("系统配置")

    if st.button("🔍 检查配置完整性", type="primary"):
        _show_check_results()

    st.divider()
    cfg = copy.deepcopy(config.load_system())

    tm = cfg["text_model_information"]
    st.subheader("文本模型(对话)")
    presets = config.load_model_presets()
    preset_names = ["(不选择预设)"] + [p["name"] for p in presets]
    c1, c2 = st.columns([2, 1])
    sel_preset = c1.selectbox("📦 应用模型预设(自动填接口地址/模型名/支持的参数)",
                              preset_names, key="tm_preset")
    if c2.button("应用预设", key="apply_tm_preset") and sel_preset != "(不选择预设)":
        p = next(x for x in presets if x["name"] == sel_preset)
        cfg["text_model_information"]["base_url"] = p["base_url"]
        cfg["text_model_information"]["name"] = p["models"][0]
        cfg["text_model_information"]["supported_params"] = list(
            p.get("supported_params", config.ALL_SAMPLING_PARAMS))
        config.save_system(cfg)
        st.success(f"已应用预设 {p['name']}({p['models'][0]}),不支持的参数已自动过滤")
        st.rerun()
    if sel_preset != "(不选择预设)":
        p = next(x for x in presets if x["name"] == sel_preset)
        st.caption(f"说明: {p.get('desc', '')} 可选模型: {', '.join(p.get('models', []))}")
    tm["name"] = st.text_input("模型名", tm.get("name", ""), key="tm_name",
                               help="对话用的模型名称,可从上方预设里选。")
    tm["apikey"] = st.text_input("API Key", tm.get("apikey", ""), type="password",
                                 key="tm_key", help="密钥;也支持环境变量 DEEPSEEK_API_KEY。")
    tm["base_url"] = st.text_input("接口地址(OpenAI 兼容)", tm.get("base_url", ""),
                                   key="tm_url",
                                   help="OpenAI 兼容接口地址,通常以 /v1 结尾;不同厂商地址见模型预设。")
    c1, c2, c3, c4 = st.columns(4)
    tm["temperature"] = c1.number_input("temperature", 0.0, 2.0,
                                        float(tm.get("temperature", 0.7)), 0.1,
                                        help="采样温度:越高越发散/有创造性,越低越稳定。")
    tm["max_tokens"] = c2.number_input("max_tokens", 1, 8192, int(tm.get("max_tokens", 1024)),
                                       help="单次回复最大 token 数。")
    tm["top_p"] = c3.number_input("top_p", 0.0, 1.0, float(tm.get("top_p", 1.0)), 0.05,
                                  help="核采样:只从累计概率 top_p 的词里选,1.0=不限制;部分模型不支持。")
    tm["timeout"] = c4.number_input("timeout(秒)", 5, 300, int(tm.get("timeout", 60)),
                                    help="请求超时秒数。")
    c1, c2 = st.columns(2)
    tm["frequency_penalty"] = c1.number_input(
        "frequency_penalty(重复惩罚)", 0.0, 2.0,
        float(tm.get("frequency_penalty", 0.4)), 0.1,
        help="惩罚已出现过的词,防止重复用词(更新计划:建议 0.3~0.5);部分模型不支持,应用预设时自动过滤。")
    tm["presence_penalty"] = c2.number_input(
        "presence_penalty(话题惩罚)", 0.0, 2.0,
        float(tm.get("presence_penalty", 0.3)), 0.1,
        help="惩罚已提及过的话题,鼓励聊新内容(建议 0.2~0.4);部分模型不支持。")

    am = cfg["analysis_model_information"]
    st.subheader("分析模型(情绪分析 / 文本压缩 / 搜索决策)")
    c1, c2 = st.columns([2, 1])
    am_preset = c1.selectbox("应用模型预设", preset_names, key="am_preset")
    if c2.button("应用预设", key="apply_am_preset") and am_preset != "(不选择预设)":
        p = next(x for x in presets if x["name"] == am_preset)
        cfg["analysis_model_information"]["base_url"] = p["base_url"]
        cfg["analysis_model_information"]["name"] = p["models"][0]
        cfg["analysis_model_information"]["supported_params"] = list(
            p.get("supported_params", config.ALL_SAMPLING_PARAMS))
        config.save_system(cfg)
        st.success(f"已应用预设 {p['name']} 到分析模型")
        st.rerun()
    am["name"] = st.text_input("分析模型名", am.get("name", ""), key="am_name",
                               help="情绪分析/记忆压缩/搜索决策用,建议便宜快速的模型。")
    am["apikey"] = st.text_input("分析模型 API Key", am.get("apikey", ""),
                                 type="password", key="am_key",
                                 help="密钥;也支持环境变量 DEEPSEEK_API_KEY。")
    am["base_url"] = st.text_input("分析模型地址", am.get("base_url", ""), key="am_url",
                                   help="OpenAI 兼容接口地址。")
    am["temperature"] = st.number_input("分析温度(建议 0.1)", 0.0, 1.0,
                                        float(am.get("temperature", 0.1)), 0.05,
                                        help="分析类任务要求稳定,建议保持低温。")
    am["max_tokens"] = st.number_input("分析输出上限 max_tokens(建议 4096)", 1024, 8192,
                                       int(am.get("max_tokens", 4096)), 1024,
                                       help="推理模型的思维链也占用该预算;太小会导致"
                                            "记忆压缩/画像提取返回空内容。")

    em = cfg["embedding_model_information"]
    st.subheader("Embedding(记忆向量)")
    em["name"] = st.text_input("embedding 模型", em.get("name", ""), key="em_name")
    em["apikey"] = st.text_input("embedding API Key", em.get("apikey", ""), type="password", key="em_key")
    em["base_url"] = st.text_input("embedding 地址", em.get("base_url", ""), key="em_url")
    em["embedding_dimensions"] = st.number_input("维度", 128, 3072, int(em.get("embedding_dimensions", 1024)))

    mi = cfg["memory_information"]
    st.subheader("记忆设置")
    mi["enabled"] = st.checkbox("启用长期记忆(L2 + ChromaDB 向量检索)", bool(mi.get("enabled", True)))
    c1, c2, c3 = st.columns(3)
    mi["l1_context_turns"] = c1.number_input("上下文轮数", 1, 50,
                                             int(mi.get("l1_context_turns", 10)),
                                             help="拼进 prompt 的最近对话轮数。")
    mi["l1_summary_rounds"] = c2.number_input("总结轮数", 5, 50,
                                              int(mi.get("l1_summary_rounds", 10)),
                                              help="每积累多少轮压缩一次到 L2。")
    mi["l1_maxlen"] = c3.number_input("L1 仅保留轮数", 5, 50, int(mi.get("l1_maxlen", 10)),
                                      help="短期记忆只保留最近 N 轮(更新计划:10 轮),超出自动截断。")
    st.caption("检索条数自动计算:K = ⌊ln(总记忆数)⌋,范围 1~10,无需手动设置。")
    c1, c2 = st.columns(2)
    mi["dedup_threshold"] = c1.number_input("记忆去重相似度阈值", 0.5, 1.0,
                                            float(mi.get("dedup_threshold", 0.85)), 0.05,
                                            help="新记忆与旧记忆相似度超过该值则更新旧条目而非新增。")
    mi["retrieval_sim_weight"] = c2.number_input("检索权重(相似度占比)", 0.0, 1.0,
                                                 float(mi.get("retrieval_sim_weight", 0.7)), 0.05,
                                                 help="综合分 = 该权重×相似度 + (1-该权重)×动态重要度。")
    c1, c2 = st.columns(2)
    mi["retrieval_min_similarity"] = c1.number_input("检索相似度门槛", 0.0, 1.0,
                                                     float(mi.get("retrieval_min_similarity", 0.45)), 0.05,
                                                     help="记忆与当前消息的相似度低于该值不召回(0.45 = 45% 以上才检索)。")
    mi["profile_message_batch"] = c2.number_input("画像提取批大小", 5, 100,
                                                  int(mi.get("profile_message_batch", 20)),
                                                  help="攒满多少条用户消息就提取一次画像,并与现有画像融合(消息存 data/user_messages.json)。")
    st.caption("ChromaDB HNSW 参数(按 500~5000 条数据量调优;修改后新索引生效)")
    c1, c2, c3 = st.columns(3)
    mi["hnsw_m"] = c1.number_input("hnsw M(连接数)", 4, 128, int(mi.get("hnsw_m", 32)),
                                   help="图中每个节点的连接数,越大越准越占内存。")
    mi["hnsw_ef_construction"] = c2.number_input("建索引 ef", 50, 1000,
                                                 int(mi.get("hnsw_ef_construction", 200)),
                                                 help="构建索引时的候选广度,越大建库越慢但检索越准。")
    mi["hnsw_ef_search"] = c3.number_input("检索 ef", 10, 500, int(mi.get("hnsw_ef_search", 60)),
                                           help="检索时的候选广度,越大越准越慢。")
    mi["vector_store_path"] = st.text_input("向量库路径", mi.get("vector_store_path", "data/vector_store"))
    mi["history_path"] = st.text_input("历史路径", mi.get("history_path", "data/history"))

    ei = cfg["emotion_information"]
    st.subheader("情绪设置(动态温度)")
    ei["enabled"] = st.checkbox("启用情绪系统", bool(ei.get("enabled", True)))
    ei["inject_emotion"] = st.checkbox("把当前情绪写进提示词", bool(ei.get("inject_emotion", True)))
    c1, c2, c3, c4 = st.columns(4)
    ei["temperature_span"] = c1.number_input("情绪摆动幅度 span", 0.0, 1.0, float(ei.get("temperature_span", 0.3)), 0.05)
    ei["temperature_min"] = c2.number_input("温度下限", 0.0, 1.0, float(ei.get("temperature_min", 0.1)), 0.05)
    ei["temperature_max"] = c3.number_input("温度上限", 0.5, 2.0, float(ei.get("temperature_max", 1.5)), 0.05)
    ei["decay"] = c4.number_input("每轮情绪衰减系数", 0.5, 1.0, float(ei.get("decay", 0.85)), 0.05)

    si = cfg["system_information"]
    st.subheader("系统设置")
    si["segmented_reply"] = st.checkbox("分段回复", bool(si.get("segmented_reply", True)))
    si["segment_separator"] = st.text_input("分段分隔符", si.get("segment_separator", "|||"))
    si["time_format"] = st.text_input("时间格式", si.get("time_format", "%Y-%m-%d %A %H:%M:%S"))
    si["inject_time"] = st.checkbox("注入当前时间", bool(si.get("inject_time", True)))
    si["inject_profile"] = st.checkbox("注入用户画像", bool(si.get("inject_profile", True)))
    si["inject_memory"] = st.checkbox("注入相关记忆", bool(si.get("inject_memory", True)))
    c1, c2 = st.columns(2)
    si["typing_seconds_per_char"] = c1.number_input(
        "模拟打字:每字增加秒数", 0.0, 1.0,
        float(si.get("typing_seconds_per_char", 0.1)), 0.05,
        help="模拟真人打字:发送前停顿 = 字数 × 每秒数(默认 0.1;0 = 关闭模拟打字)。")
    si["typing_max_seconds"] = c2.number_input(
        "模拟打字:停顿上限(秒)", 1.0, 10.0,
        float(si.get("typing_max_seconds", 10.0)), 1.0,
        help="模拟打字停顿的最大秒数(按更新计划:不超过 10 秒)。")

    if st.button("💾 保存系统配置", type="primary"):
        config.save_system(cfg)
        st.success("已保存并即时生效(微信 bot 无需重启)")

# ============ 页签 4:记忆 ============
with tab_mem:
    st.subheader("记忆查看 / 逐条删除 / 一键清理")
    role_name = st.session_state.current_role
    if not role_name:
        st.info("请先在侧边栏选择角色")
    else:
        st.caption(f"管理对象: {service.user_display_label(manage_user)} ｜ 角色: {role_name}"
                   " ｜ 该页签显示的是这个角色与该用户的对话记忆")
        safe_role = service.safe_id(role_name)

        l1 = service.get_l1_turns(manage_user, role_name)
        st.markdown(f"#### 短期记忆(L1) · 共 {len(l1)} 轮(全部显示,最新在上)")
        if not l1:
            st.caption("暂无短期记忆")
        for i, t in enumerate(reversed(l1)):
            real = len(l1) - 1 - i
            c1, c2 = st.columns([0.95, 0.05])
            with c1:
                if t.get("user"):
                    st.markdown(f"**[{t.get('time', '')}]** 用户: {t.get('user', '')}")
                if t.get("assistant"):
                    st.markdown(f"[{t.get('time', '')}] AI: {t.get('assistant', '')}")
            with c2:
                if st.button("🗑️", key=f"del_l1_{manage_user}_{safe_role}_{real}",
                             help="删除这一轮对话"):
                    service.delete_l1_turn(manage_user, role_name, real)
                    st.success("已删除这一轮对话")
                    st.rerun()

        st.divider()
        l2 = service.get_l2_items(manage_user, role_name)
        topics = service.get_l2_topics(manage_user, role_name)
        topic_filter = st.selectbox("按话题过滤", ["全部"] + topics,
                                    key=f"topic_filter_{safe_role}")
        l2_show = sorted(l2, key=lambda m: (m.get("time") or ""), reverse=True)[:30]
        if topic_filter != "全部":
            l2_show = [m for m in l2_show
                       if (m.get("topic") or "未分类") == topic_filter]
        st.markdown(f"#### 长期记忆(L2) · 共 {len(l2)} 条"
                    f"(显示 {len(l2_show)} 条,话题 '{topic_filter}')")
        if not l2_show:
            st.caption("暂无长期记忆")
        for m in l2_show:
            c1, c2 = st.columns([0.95, 0.05])
            with c1:
                st.markdown(
                    f"- **[{m.get('time', '')}]** 🏷{m.get('topic', '未分类')} "
                    f"{m.get('text', '')} "
                    f"*(重要度 {float(m.get('importance', 0)):.2f} · "
                    f"{m.get('emotion', '平静')} {float(m.get('emotion_value', 0)):+.2f} · "
                    f"命中 {m.get('access_count', 0)} 次)*")
            with c2:
                if st.button("🗑️", key=f"del_l2_{m.get('id', '')}",
                             help="删除这条长期记忆"):
                    service.delete_l2_item(manage_user, role_name, str(m.get("id", "")))
                    st.success("已删除这条长期记忆")
                    st.rerun()

        st.divider()
        st.markdown("#### 一键清理")
        c1, c2 = st.columns(2)
        if c1.button("清空短期记忆(L1)"):
            service.clear_memory(manage_user, role_name, clear_l1=True, clear_l2=False)
            if str(manage_user).lower() == "test":
                st.session_state.messages = []
            st.success("已清空短期记忆")
            st.rerun()
        if c2.button("清空长期记忆(L2)"):
            service.clear_memory(manage_user, role_name, clear_l1=False, clear_l2=True)
            st.success("已清空长期记忆")
            st.rerun()

# ============ 页签 5:用户画像 ============
with tab_profile:
    st.subheader("用户画像(只记长期不变的内容)")
    st.caption("画像只放长时间不会变的信息:爱好、性格、性别、生日、家庭环境、居住环境等。"
               "近期事件 / 临时计划 / 当前在做的事请交给记忆系统自动抽取,不要写在这里。")
    mi = config.load_system().get("memory_information", {})
    pending = service.get_pending_profile_messages()
    batch = int(mi.get("profile_message_batch", 20))
    st.caption(f"自动提取:用户消息已攒 {pending}/{batch} 条(存于 "
               f"{mi.get('profile_user_messages_path', 'data/user_messages.json')}),"
               f"攒满 {batch} 条后由分析模型提取并与现有画像融合。")
    profile = copy.deepcopy(config.load_profile())
    c1, c2 = st.columns(2)
    profile["称呼"] = c1.text_input("称呼", profile.get("称呼", ""), key="p_call")
    profile["关系"] = c2.text_input("关系", profile.get("关系", ""), key="p_rel")
    c1, c2 = st.columns(2)
    profile["性别"] = c1.text_input("性别", profile.get("性别", ""), key="p_gender")
    profile["生日"] = c2.text_input("生日", profile.get("生日", ""), key="p_birth")
    profile["爱好"] = _list_editor("爱好(一行一条)", "p_hobbies", profile.get("爱好", []))
    profile["性格"] = _list_editor("性格(一行一条)", "p_traits", profile.get("性格", []))
    profile["家庭环境"] = st.text_area("家庭环境", profile.get("家庭环境", ""), key="p_family")
    profile["居住环境"] = st.text_area("居住环境", profile.get("居住环境", ""), key="p_home")
    profile["长期事实"] = _list_editor("其他长期事实(职业/学历等,一行一条)",
                                       "p_facts", profile.get("长期事实", []))
    if st.button("💾 保存画像", type="primary"):
        config.save_profile(profile)
        st.success("已保存用户画像(即时生效,微信 bot 无需重启)")

# ============ 页签 6:工具 ============
with tab_tool:
    st.subheader("工具调用")
    st.caption("AI 可以在对话中按需调用工具(OpenAI function calling)。")

    st.markdown("#### 已注册工具(自动扫描 tools/ 目录)")
    found = tool_kit.discover_tools()
    if not found:
        st.info("tools/ 目录下没有发现工具。工具文件格式:TOOL 字典(含 name/description/safe/parameters)+ run() 函数。")
    for name, spec, _run in found:
        safe_mark = "✅ 白名单安全" if spec.get("safe") else "⚠️ 未标记安全"
        st.markdown(f"- **{name}** {safe_mark}\n\n  {spec.get('description', '')}")

    st.divider()
    st.markdown("#### 自定义白名单工具")
    st.caption(
        "格式为 JSON 数组,每个元素字段:\n"
        "`name` 工具名(英文唯一) / `description` 给模型的介绍(必填) / "
        "`safe` true=白名单安全 / `parameters` OpenAI function calling 参数定义 / "
        "`code` 可选的 Python 代码(在受限沙箱执行,变量 `args` 是模型传入的参数字典)。"
        "保存后即时生效,与内置工具重名的会被跳过。")
    ctools = config.load_custom_tools()
    cjson = st.text_area("工具定义 JSON", json.dumps(ctools, ensure_ascii=False, indent=2),
                         height=260, key="custom_tools_json")
    if st.button("💾 保存自定义工具", type="primary"):
        try:
            parsed = json.loads(cjson)
            if not isinstance(parsed, list):
                raise ValueError("顶层必须是 JSON 数组")
            config.save_custom_tools(parsed)
            service.get_toolkit(config.load_system()).reload()
            st.success("已保存自定义工具并即时生效(微信 bot 无需重启)")
            st.rerun()
        except Exception as e:
            st.error(f"JSON 解析失败,未保存: {e}")

    st.divider()
    st.markdown("#### AI 生成代码(危险)")
    tool_cfg = copy.deepcopy(config.load_system())["tool_information"]
    cur_allow = bool(tool_cfg.get("allow_generated_code", False))
    new_allow = st.checkbox("允许 AI 生成并执行代码", value=cur_allow, key="allow_gen")

    if new_allow and not cur_allow:
        st.markdown(
            ":red[⚠️ **警告**:开启后 AI 可现场生成并执行 Python 代码,"
            "可能产生不可预期的严重后果(如误删文件、执行恶意操作)。"
            "请确保你信任该 AI 并知晓风险。代码将在受限沙箱中执行"
            "(超时/禁网/禁危险模块),但沙箱并非绝对安全。]")
        if st.button("我已知晓风险,确认开启", type="primary", key="confirm_gen"):
            cfg2 = copy.deepcopy(config.load_system())
            cfg2["tool_information"]["allow_generated_code"] = True
            config.save_system(cfg2)
            st.success("已开启 AI 生成代码能力(受限沙箱执行)")
            st.rerun()
    elif not new_allow and cur_allow:
        cfg2 = copy.deepcopy(config.load_system())
        cfg2["tool_information"]["allow_generated_code"] = False
        config.save_system(cfg2)
        st.success("已关闭 AI 生成代码能力")
        st.rerun()

    st.markdown("#### 工具设置")
    tool_cfg["enabled"] = st.checkbox("启用工具调用", bool(tool_cfg.get("enabled", True)))
    mode_opts = ["auto", "native", "tool"]
    mode_labels = {
        "auto": "auto 自动(DeepSeek 用官方原生联网搜索,其他模型用内置 Bing 工具)",
        "native": "native 原生(强制走 DeepSeek Responses 联网搜索,仅 DeepSeek 可用)",
        "tool": "tool 工具(所有模型都用内置 Bing 搜索工具)",
    }
    tool_cfg["web_search_mode"] = st.selectbox(
        "联网搜索方式",
        options=mode_opts,
        index=mode_opts.index(str(tool_cfg.get("web_search_mode", "auto"))),
        format_func=lambda v: mode_labels[v],
        help="DeepSeek 官方 Responses API 内置 web_search 工具(模型自己决定是否搜索);"
             "其他厂商模型自动回退到 Bing 网页搜索工具。")
    c1, c2 = st.columns(2)
    tool_cfg["sandbox_timeout"] = c1.number_input("沙箱超时(秒)", 1, 60, int(tool_cfg.get("sandbox_timeout", 10)))
    tool_cfg["max_tool_rounds"] = c2.number_input("工具循环最大轮数", 1, 10, int(tool_cfg.get("max_tool_rounds", 4)))
    if st.button("💾 保存工具设置", type="primary"):
        cfg2 = copy.deepcopy(config.load_system())
        cfg2["tool_information"] = tool_cfg
        config.save_system(cfg2)
        st.success("已保存工具设置")

# ============ 页签 7:情绪 ============
with tab_emo:
    st.subheader("AI 情绪状态")
    st.caption("情绪是 AI 自己的情绪,由上下文推断;决定下一次说话的 temperature。")
    role_name = st.session_state.current_role
    if not role_name:
        st.info("请先在侧边栏选择角色")
    else:
        st.caption(f"角色: {role_name} · 管理对象: {service.user_display_label(manage_user)}")
        es = service.get_emotion_state(manage_user, role_name).get()
        c1, c2, c3 = st.columns(3)
        c1.metric("当前情绪", es.get("emotion", "平静"))
        c2.metric("效价 valence", f"{float(es.get('valence', 0)):+.2f}")
        c3.metric("唤醒度 arousal", f"{float(es.get('arousal', 0.3)):.2f}")
        st.caption(f"更新时间: {es.get('updated_at', '')}")
        st.markdown("""
        情绪 → 温度(线性公式,最终温度 = (配置温度 + 情绪温度) / 2):
        - 唤醒度主要驱动温度:兴奋/生气 → 偏高;平静/低落 → 偏低
        - 每轮自然衰减,情绪不会一直亢奋;检索 L2 时记忆情绪也会反哺当前情绪
        """)
        if st.button("重置情绪为平静"):
            service.reset_emotion(manage_user, role_name)
            st.success("已重置")
            st.rerun()

# ============ 页签 8:主动对话 ============
with tab_pro:
    st.subheader("主动对话")
    st.caption("微信 bot 进程内的调度线程负责实际发送;这里可设置参数并预览生成效果。")

    pi = copy.deepcopy(config.load_system()).get("proactive_information", {})
    pi["enabled"] = st.checkbox("开启主动对话(周期性主动发消息)", bool(pi.get("enabled", False)))
    c1, c2 = st.columns(2)
    pi["interval_hours"] = c1.number_input("主动间隔(小时,0=关闭周期主动)", 0.0, 24.0, float(pi.get("interval_hours", 3)), 0.5)
    pi["token_fresh_minutes"] = c2.number_input("context_token 新鲜期(分钟)", 5, 120, int(pi.get("token_fresh_minutes", 30)))
    c1, c2 = st.columns(2)
    pi["start_hour"] = c1.number_input("允许发送的开始时间(时)", 0, 23, int(pi.get("start_hour", 8)))
    pi["end_hour"] = c2.number_input("允许发送的结束时间(时,不含)", 1, 24, int(pi.get("end_hour", 23)))
    if st.button("💾 保存主动对话设置", type="primary"):
        cfg2 = copy.deepcopy(config.load_system())
        cfg2["proactive_information"] = pi
        config.save_system(cfg2)
        st.success("已保存并即时生效(微信 bot 无需重启)")

    st.divider()
    st.markdown("#### 立即主动发一条(预览)")
    st.caption("从高重要度记忆里取话题;预览不消耗话题轮换、不写入记忆,到点由微信 bot 进程真正发送。")
    role_name = st.session_state.current_role
    if not role_name:
        st.info("请先在侧边栏选择角色")
    elif st.button("📣 立即生成一条主动消息", type="primary"):
        with st.spinner("生成中..."):
            try:
                reply = service.proactive_once(role_name, manage_user, commit=False)
            except Exception as e:
                reply = f"❌ 错误: {e}"
        with st.chat_message("assistant"):
            st.markdown(service.strip_separator(reply))

    st.divider()
    st.markdown(f"#### 提醒任务({service.user_display_label(manage_user)})")
    rems = service.get_state().reminders_of(manage_user)
    if not rems:
        st.caption("暂无提醒。在对话里说「提醒我明天9点开会」即可自动解析并添加。")
    for r in rems:
        st.markdown(f"- {'✅ 已发' if r.get('fired') else '⏳ 待发'} **{r.get('fire_at','')}** {r.get('content','')}")
