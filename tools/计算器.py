# -*- coding: utf-8 -*-
"""安全计算器工具(白名单安全)。仅四则运算与少量安全函数。"""

TOOL = {
    "name": "calculate",
    "description": "计算数学表达式,支持 + - * / // % ** 与 abs/round/min/max/sum/pow。",
    "safe": True,
    "parameters": {
        "type": "object",
        "properties": {
            "expr": {"type": "string", "description": "数学表达式,如 '3*4+2'"},
        },
        "required": ["expr"],
    },
}

_SAFE = {"abs": abs, "round": round, "min": min, "max": max, "sum": sum, "pow": pow}


def run(expr):
    try:
        return str(eval(expr, {"__builtins__": {}}, _SAFE))
    except Exception as e:
        return f"[计算出错] {e}"
