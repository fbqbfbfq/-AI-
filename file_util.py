# -*- coding: utf-8 -*-
"""
文件工具:JSON 原子读写 + 线程锁 + 损坏自愈 + 时间格式化。

- atomic_save_json: 先写临时文件再 os.replace 原子替换,避免写一半被读到。
- save_json / load_json: 加文件级线程锁(单进程内线程安全)。
- safe_load_json: 损坏自愈读取——文件存在但解析失败时删除并重建,返回默认值。
- format_time: 时间格式化,%A/%a 替换为中文星期,避免依赖系统 locale。
"""
import datetime
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import logger

# ---------- 线程锁(单进程内安全) ----------
_FILE_LOCKS = {}
_LOCK_DICT_LOCK = threading.Lock()


def _get_file_lock(file_path):
    with _LOCK_DICT_LOCK:
        if file_path not in _FILE_LOCKS:
            _FILE_LOCKS[file_path] = threading.Lock()
        return _FILE_LOCKS[file_path]


# ---------- 时间格式化 ----------
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def format_time(dt=None, fmt="%Y-%m-%d %A %H:%M:%S"):
    """格式化时间;把 %A/%a 替换成中文星期,不依赖系统 locale。"""
    if dt is None:
        dt = datetime.datetime.now()
    weekday = WEEKDAY_CN[dt.weekday()]
    fmt = fmt.replace("%A", weekday).replace("%a", weekday)
    return dt.strftime(fmt)


def parse_time(s):
    """从 '2026-08-17 周一 19:25:35' 或 '2026-08-17 19:25:35' 解析 datetime;失败返回 None。"""
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2}).*?(\d{2}:\d{2}:\d{2})", s)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(f"{m.group(1)} {m.group(2)}",
                                          "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


# ---------- 原子写 ----------
def atomic_save_json(directory, file_name, data, indent=4):
    """原子写 JSON:临时文件 + os.replace。

    Windows 下 os.replace 偶发因杀软/瞬时句柄占用报 PermissionError,
    这里做 3 次小退避重试,避免聊天线程写记忆文件时偶发失败。
    """
    directory.mkdir(parents=True, exist_ok=True)
    file_path = directory / f"{file_name}.json"
    with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(directory),
            prefix=".tmp_", suffix=".json", delete=False) as tmp:
        if indent is not None:
            json.dump(data, tmp, ensure_ascii=False, indent=indent)
        else:
            json.dump(data, tmp, ensure_ascii=False, separators=(",", ":"))
        tmp_path = Path(tmp.name)
    last_err = None
    for attempt in range(3):
        try:
            os.replace(str(tmp_path), str(file_path))
            return
        except OSError as exc:
            last_err = exc
            time.sleep(0.1 * (attempt + 1))
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError:
        pass
    raise last_err


def check_json_file(directory, file_name="", default_data=None):
    """确保目录/文件存在;文件不存在则写入默认数据,返回文件路径。"""
    if default_data is None:
        default_data = []
    directory.mkdir(parents=True, exist_ok=True)
    if file_name != "":
        file_path = directory / f"{file_name}.json"
        if not file_path.exists():
            atomic_save_json(directory, file_name, default_data)
        return file_path
    return directory


def save_json(directory, file_name, data, indent=4):
    """线程安全保存(加锁 + 原子写)。"""
    file_path = str(directory / f"{file_name}.json")
    lock = _get_file_lock(file_path)
    with lock:
        directory.mkdir(parents=True, exist_ok=True)
        atomic_save_json(directory, file_name, data, indent)


def load_json(directory, file_name, default_data=None):
    """线程安全读取;文件不存在则创建并返回默认值。"""
    if default_data is None:
        default_data = []
    file_path = str(directory / f"{file_name}.json")
    lock = _get_file_lock(file_path)
    with lock:
        file_path_obj = check_json_file(directory, file_name, default_data)
        with open(file_path_obj, "r", encoding="utf-8") as f:
            return json.load(f)


def safe_load_json(directory, file_name, default_data=None, on_corrupt=None):
    """损坏自愈读取。

    - 文件不存在:创建并返回 default_data。
    - 文件存在但解析失败(JSONDecodeError 等):删除坏文件、按默认重建,
      调用 on_corrupt(path, exc) 回调(用于写日志),返回 default_data。
    """
    if default_data is None:
        default_data = []
    directory.mkdir(parents=True, exist_ok=True)
    file_path = directory / f"{file_name}.json"
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            try:
                file_path.unlink()
            except Exception as unlink_exc:
                logger.log_exception("损坏文件删除失败", path=str(file_path),
                                     error=str(unlink_exc))
            atomic_save_json(directory, file_name, default_data)
            if on_corrupt is not None:
                try:
                    on_corrupt(str(file_path), exc)
                except Exception as cb_exc:
                    logger.log_exception("损坏文件回调执行失败", path=str(file_path),
                                         error=str(cb_exc))
            return default_data
    atomic_save_json(directory, file_name, default_data)
    return default_data


def find_index(data, key, value):
    """在 list[dict] 中按 key==value 查找索引,找不到返回 None。"""
    if not isinstance(data, list):
        return None
    for i, item in enumerate(data):
        if isinstance(item, dict) and item.get(key) == value:
            return i
    return None
