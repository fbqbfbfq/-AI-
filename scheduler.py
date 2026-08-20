# -*- coding: utf-8 -*-
"""
主动对话调度(UPGRADE_DESIGN.md §6):定时/周期性主动 + 提醒。

- 周期主动:每隔 interval_hours 且在 [start_hour, end_hour) 时间段内,对
  「token 还新鲜」的用户主动发一条(内容由 service.proactive_once 生成)。
- 提醒:到点且 token 新鲜时回调发送;token 过期则跳过、等下次(不标记已发)。
"""
import datetime
import threading

import file_util
import logger


class Scheduler:
    """后台调度线程,默认每 30 秒检查一次。"""

    def __init__(self, state_store=None, proactive_config_fn=None,
                 on_proactive=None, on_reminder=None, tick_seconds=30):
        self.state = state_store
        self.proactive_config = proactive_config_fn   # () -> dict
        self.on_proactive = on_proactive   # (user_id) -> None
        self.on_reminder = on_reminder     # (reminder dict) -> None
        self.tick_seconds = tick_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.tick_seconds):
            try:
                self.tick()
            except Exception as e:
                logger.log_exception("主动对话调度出错", error=str(e))

    def tick(self):
        cfg = self.proactive_config() if self.proactive_config else {}
        # 1. 周期性主动
        if cfg.get("enabled") and self.state is not None and self.on_proactive:
            for user_id in list(self.state.state.get("users", {}).keys()):
                if self._due_proactive(user_id, cfg):
                    try:
                        self.on_proactive(user_id)
                        self.state.mark_proactive(user_id)
                    except Exception as e:
                        logger.log_exception("主动消息发送失败", user=user_id,
                                             error=str(e))
        # 2. 提醒
        if self.state is not None and self.on_reminder:
            for r in self.state.due_reminders():
                user_id = r.get("user_id", "")
                if not self.state.token_fresh(
                        user_id, int(cfg.get("token_fresh_minutes", 30))):
                    continue   # token 过期:不发送、不标记,等下次刷新
                try:
                    self.on_reminder(r)
                    self.state.mark_fired(r["id"])
                except Exception as e:
                    logger.log_exception("提醒发送失败", reminder=r.get("id"),
                                         error=str(e))

    def _due_proactive(self, user_id, cfg):
        now = datetime.datetime.now()
        start = int(cfg.get("start_hour", 8))
        end = int(cfg.get("end_hour", 23))
        if not (start <= now.hour < end):
            return False
        interval_h = float(cfg.get("interval_hours", 3))
        if interval_h <= 0:
            return False
        last = file_util.parse_time(
            self.state.get_user(user_id).get("last_proactive", ""))
        if last and (now - last).total_seconds() < interval_h * 3600.0:
            return False
        return self.state.token_fresh(
            user_id, int(cfg.get("token_fresh_minutes", 30)))
