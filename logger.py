# -*- coding: utf-8 -*-
"""
全局日志:只记报错/异常,追加写、不轮转、不删除历史日志。

无代码用户报障时,只需提供 logs/bot.log 尾部即可。
"""
import logging
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE = LOG_DIR / "bot.log"

_logger = None


def setup_logger():
    """初始化 logger(幂等)。只记 WARNING 及以上(报错),正常流程不写。"""
    global _logger
    if _logger is not None:
        return _logger
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("wechat-ai-bot")
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    logger.handlers.clear()
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")  # 追加写,不轮转
    fh.setLevel(logging.WARNING)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)
    _logger = logger
    return logger


def _fmt_context(context):
    if not context:
        return ""
    return " 上下文: " + " ".join(f"{k}={v!r}" for k, v in context.items())


def log_exception(msg, **context):
    """在 except 块内调用:记录当前异常完整堆栈 + 上下文,不中断主流程。"""
    setup_logger().exception(msg + _fmt_context(context))


def log_error(msg, **context):
    """记录非异常类错误(如文件损坏、自检失败)。"""
    setup_logger().error(msg + _fmt_context(context))
