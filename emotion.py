# -*- coding: utf-8 -*-
"""
情绪系统(UPGRADE_DESIGN.md §4):以 B 站灵梦桌宠评论思路为主。

- 持久化情绪态(按 user_id + role 隔离),二维模型 valence(效价) + arousal(唤醒度)。
- 情绪指「AI 自己可能的情绪」,由上下文(用户消息 + 当前情绪 + 检索到的记忆)推断。
- 每轮结算:自然衰减 + 记忆唤起情绪×0.3 + 用户消息唤起情绪×0.7 → 动态温度。
- 最终调用温度 = (配置文件温度 + 情绪温度) / 2。
"""
import json
import threading
from pathlib import Path

import file_util
import logger

EMOTION_LABELS = ["平静", "开心", "兴奋", "难过", "生气", "焦虑", "害羞"]

DEFAULT_STATE = {"emotion": "平静", "valence": 0.0, "arousal": 0.3, "updated_at": ""}

BASELINE_AROUSAL = 0.3   # 唤醒度自然回落的基线


# ---------- 标签映射 ----------

def label_of(valence, arousal):
    """由 (效价, 唤醒度) 映射情绪标签。"""
    v, a = valence, arousal
    if a >= 0.6:
        return "兴奋" if v >= 0.2 else ("生气" if v <= -0.2 else "焦虑")
    if v >= 0.4:
        return "开心"
    if v <= -0.4:
        return "难过"
    if v <= -0.15:
        return "焦虑"
    return "平静"


def norm_label(label, valence, arousal):
    """
    纠正非法标签，也就是不在EMOTION_LABELS的标签
    """
    label = str(label or "")
    if label not in EMOTION_LABELS:
        label = label_of(valence, arousal)
    return label


# ---------- 情绪态持久化 ----------

class EmotionState:
    """一个 (user, role) 的情绪态,JSON 落盘 + 内存持有。"""

    def __init__(self, path):
        self.path = Path(path)
        self.dir = self.path.parent
        self.file_name = self.path.stem
        self.state = dict(DEFAULT_STATE)
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.state = {**DEFAULT_STATE, **data}
            except Exception as exc:
                self.state = dict(DEFAULT_STATE)
                logger.log_error("情绪态文件损坏,已重建",
                                 path=str(self.path), error=str(exc))
        self._save()

    def _save(self):
        file_util.atomic_save_json(self.dir, self.file_name, self.state)

    def get(self):
        with self._lock:
            return dict(self.state)

    def set(self, emotion, valence, arousal, now_str):
        with self._lock:
            self.state = {
                "emotion": emotion,
                "valence": round(valence, 4),
                "arousal": round(arousal, 4),
                "updated_at": now_str,
            }
            self._save()


# ---------- 情绪抽取(分析模型) ----------

EXTRACT_SYSTEM = (
    "你是情绪分析器。根据上下文推断 AI(助手)自己此刻的情绪,"
    "只返回 JSON:{\"emotion\":\"标签\",\"valence\":-1~1,\"arousal\":0~1}"
)


def infer_message_emotion(complete_fn, user_text, prev_state, memories):
    """用分析模型推断「用户这条消息唤起的 AI 情绪」。失败返回 None。

    :param complete_fn: 分析模型调用,签名 complete(messages) -> str
    :param user_text: 用户刚刚发来的消息原文
    :param prev_state: 上一轮情绪态 {"emotion","valence","arousal"}
    :param memories: 检索到的 L2 记忆列表(含 emotion/emotion_value)
    """
    mem_lines = []
    for m in (memories or [])[:5]:
        mem_lines.append(f"- ({m.get('emotion', '平静')}) {m.get('text', '')}")
    prompt = (
        f"用户刚刚发来消息: {user_text}\n\n"
        f"AI 上一轮的情绪: {prev_state.get('emotion', '平静')}"
        f"(效价 {prev_state.get('valence', 0.0):+.2f}, "
        f"唤醒度 {prev_state.get('arousal', 0.3):.2f})\n\n"
        + ("最近回想起来的记忆:\n" + "\n".join(mem_lines) + "\n\n" if mem_lines else "")
        + f"请推断 AI(助手)听到这条消息后自己的情绪。"
          f"emotion 从 {EMOTION_LABELS} 里选一个;"
          "valence 是效价(-1 负面 ~ +1 正面);arousal 是唤醒度(0 平静 ~ 1 激动)。"
          "注意:必须根据用户最新消息的内容重新判断,不要直接沿用上一轮的情绪;"
          "用户辱骂/指责/表达难过时应体现负面情绪,好消息/夸奖时应体现正面情绪。"
          "只返回 JSON,不要解释。"
    )
    raw = complete_fn([
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user", "content": prompt},
    ])
    return parse_emotion(raw)


def parse_emotion(raw):
    try:
        s = raw.strip()
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end > start:
            data = json.loads(s[start:end + 1])
            if isinstance(data, dict):
                v = float(data.get("valence", 0.0))
                a = float(data.get("arousal", BASELINE_AROUSAL))
                v = max(-1.0, min(1.0, v))
                a = max(0.0, min(1.0, a))
                emo = norm_label(data.get("emotion", ""), v, a)
                return {"emotion": emo, "valence": v, "arousal": a}
    except Exception as e:
        logger.log_exception("情绪 JSON 解析失败,返回 None", error=str(e),
                             raw=str(raw)[:200])
    return None


# ---------- 情绪结算 ----------

def memory_evoked_emotion(memories):
    """检索 L2 时计算「记忆唤起情绪」= Σ(emotion_value × 相似度权重) / Σ权重。"""
    if not memories:
        return None
    total_w = 0.0
    total_v = 0.0
    for m in memories:
        w = max(0.0, float(m.get("similarity", 0.0)))
        if w <= 0:
            continue
        total_w += w
        total_v += float(m.get("emotion_value", 0.0)) * w
    if total_w <= 0:
        return None
    return max(-1.0, min(1.0, total_v / total_w))


def settle(prev_state, mem_evoked_valence, msg_emotion,
           decay=0.85, mem_w=0.3, user_w=0.7):
    """情绪结算:自然衰减(上一轮) + 记忆唤起情绪×mem_w + 用户消息情绪×user_w。

    返回 (valence, arousal)。唤醒度只受消息情绪影响(记忆只有效价),向 0.3 基线回落。
    """
    v = float(prev_state.get("valence", 0.0)) * decay
    a = BASELINE_AROUSAL + (float(prev_state.get("arousal", BASELINE_AROUSAL))
                            - BASELINE_AROUSAL) * decay
    if mem_evoked_valence is not None:
        v = v * (1.0 - mem_w) + mem_evoked_valence * mem_w
    if msg_emotion is not None:
        v = v * (1.0 - user_w) + float(msg_emotion["valence"]) * user_w
        a = a * (1.0 - user_w) + float(msg_emotion["arousal"]) * user_w
    return max(-1.0, min(1.0, v)), max(0.0, min(1.0, a))


# ---------- 情绪 → 温度 ----------

def emotion_temperature(valence, arousal, t_base, t_span=0.3, t_min=0.1, t_max=1.5):
    """情绪温度 + 最终温度 = (配置温度 + 情绪温度) / 2。

    唤醒度主要驱动温度,效价做微调。
    """
    t_emotion = t_base + (arousal - 0.5) * 2.0 * t_span
    if valence < -0.2 and arousal >= 0.5:          # 生气:更情绪化
        t_emotion += t_span
    elif valence > 0.3 and arousal <= 0.4:         # 平静满足:更稳定
        t_emotion -= t_span
    t_emotion = max(t_min, min(t_max, t_emotion))
    return (t_base + t_emotion) / 2.0


def emotion_hint(emotion, valence, arousal):
    """写进 system prompt 的情绪提示。"""
    return (f"【当前情绪】你此刻的心情是「{emotion}」"
            f"(效价 {valence:+.2f},唤醒度 {arousal:.2f})。"
            "你的语气和用词应当自然体现这一情绪,但不要直接说出自己的情绪数值。")
