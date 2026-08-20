# -*- coding: utf-8 -*-
"""
状态持久化(UPGRADE_DESIGN.md §6.2):
- bot 身份 + 最近 context_token(主动发消息用) + 活跃/主动时间,重启不丢。
- 提醒任务列表(提醒解析 → 到点主动发)。
"""
import datetime
import threading
import time
from pathlib import Path

import file_util
import logger

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "data"
STATE_NAME = "state"

DEFAULT_STATE = {
    "users": {},       # user_id -> {"context_token","token_updated_at","last_active","last_proactive"}
    "reminders": [],   # [{"id","user_id","role","fire_at","content","fired"}]
}


def _now_compact():
    """紧凑时间串(YYYY-MM-DD HH:MM:SS),字符串可比较顺序。"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class StateStore:
    """data/state.json 的线程安全读写(write-through)。"""

    def __init__(self):
        self.state = dict(DEFAULT_STATE)
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        data = file_util.safe_load_json(STATE_DIR, STATE_NAME, DEFAULT_STATE)
        if not isinstance(data, dict):
            data = dict(DEFAULT_STATE)
        self.state = {**DEFAULT_STATE, **data}

    def _save(self):
        file_util.save_json(STATE_DIR, STATE_NAME, self.state)

    # ---- 用户 token / 活跃 ----

    def update_token(self, user_id, context_token):
        """收到对方消息时更新 context_token 与活跃时间(主动发的通行证)。"""
        with self._lock:
            u = self.state["users"].setdefault(user_id, {})
            u["context_token"] = context_token
            u["token_updated_at"] = _now_compact()
            u["last_active"] = _now_compact()
            self._save()

    def mark_proactive(self, user_id):
        with self._lock:
            u = self.state["users"].setdefault(user_id, {})
            u["last_proactive"] = _now_compact()
            self._save()

    def get_user(self, user_id):
        with self._lock:
            return dict(self.state["users"].get(user_id, {}))

    def token_fresh(self, user_id, fresh_minutes=30):
        """context_token 是否还新鲜(在新鲜期内才能主动发消息)。"""
        u = self.get_user(user_id)
        tok = u.get("context_token", "")
        if not tok:
            return False
        t = file_util.parse_time(u.get("token_updated_at", ""))
        if t is None:
            return False
        return (datetime.datetime.now() - t).total_seconds() < fresh_minutes * 60.0

    # ---- 提醒 ----

    def add_reminder(self, user_id, role, fire_at, content):
        with self._lock:
            rid = f"r-{int(time.time() * 1000)}"
            self.state["reminders"].append({
                "id": rid, "user_id": user_id, "role": role,
                "fire_at": fire_at, "content": content, "fired": False,
            })
            self._save()
            return rid

    def due_reminders(self):
        """到点未发的提醒(按时间字符串比较)。"""
        now = _now_compact()
        with self._lock:
            return [dict(r) for r in self.state["reminders"]
                    if not r.get("fired") and (r.get("fire_at") or "") <= now]

    def mark_fired(self, rid):
        with self._lock:
            for r in self.state["reminders"]:
                if r.get("id") == rid:
                    r["fired"] = True
            self._save()

    def reminders_of(self, user_id):
        with self._lock:
            return [dict(r) for r in self.state["reminders"]
                    if r.get("user_id") == user_id]

    def clear_fired(self):
        with self._lock:
            self.state["reminders"] = [r for r in self.state["reminders"]
                                       if not r.get("fired")]
            self._save()
