# -*- coding: utf-8 -*-
"""时间查询工具(白名单安全)。"""
import datetime

TOOL = {
    "name": "get_time",
    "description": "查询当前日期和时间(含星期)。",
    "safe": True,
    "parameters": {"type": "object", "properties": {}},
}

_WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def run():
    now = datetime.datetime.now()
    return f"{now.strftime('%Y-%m-%d')} {_WEEK[now.weekday()]} {now.strftime('%H:%M:%S')}"
