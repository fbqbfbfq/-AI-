# -*- coding: utf-8 -*-
"""
工具调用(UPGRADE_DESIGN.md §5):折中方案。

- 扫描 tools/*.py 自动注册(TOOL 含 safe 白名单标记 + run 函数)。
- 安全白名单:safe=true 的工具 AI 可直接用;描述里会标注"安全工具"。
- AI 自生成代码通道 run_python_code:默认关闭;开启后在受限沙箱执行
  (静态黑名单 + 隔离 subprocess + 超时 + 精简环境),执行前日志留代码快照。
- decide_search:用分析模型判断是否需要联网搜索。
"""
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import config
import logger

TOOLS_DIR = Path(__file__).resolve().parent / "tools"
SANDBOX_DIR = TOOLS_DIR / ".sandbox_tmp"   # 沙箱临时目录(项目内,便于清理)

# 沙箱静态黑名单(尽力而为的防护;配合默认关闭 + 红字警告,见 UPGRADE_DESIGN.md §5.4)
FORBIDDEN_PATTERNS = (
    "os.system", "subprocess", "socket", "shutil", "os.remove", "os.unlink",
    "os.rmdir", "os.removedirs", "os.rename", "os.replace", "os.chdir",
    "eval(", "exec(", "__import__", "compile(", "ctypes", "winreg",
    "multiprocessing", "threading",
)

SEARCH_DECISION_SYSTEM = (
    "你是搜索决策器。判断用户的问题是否需要联网搜索才能准确回答。"
    "只返回 JSON:{\"need_search\": true/false}"
)

RUN_CODE_TOOL_NAME = "run_python_code"

RUN_CODE_TOOL = {
    "name": RUN_CODE_TOOL_NAME,
    "description": ("在受限沙箱中执行一段 Python 代码并返回输出。"
                    "仅当你需要现场计算/处理数据时使用;代码要短小、只用标准库。"),
    "safe": False,
    "parameters": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "要执行的 Python 代码"},
        },
        "required": ["code"],
    },
}


# ---------- 工具发现 ----------

def discover_tools():
    """扫描 tools/*.py,返回 [(name, spec, run_fn)]。"""
    found = []
    if not TOOLS_DIR.exists():
        return found
    for f in sorted(TOOLS_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        spec, run_fn = load_tool_file(f)
        if spec and run_fn:
            found.append((spec.get("name") or f.stem, spec, run_fn))
    return found


def load_tool_file(path):
    """动态加载一个工具文件,返回 (TOOL dict, run 函数);失败返回 (None, None)。"""
    try:
        spec = importlib.util.spec_from_file_location(f"tool_{path.stem}", str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        tool = getattr(mod, "TOOL", None)
        run = getattr(mod, "run", None)
        if not isinstance(tool, dict) or not callable(run):
            return None, None
        return tool, run
    except Exception as e:
        logger.log_exception("工具加载失败", path=str(path), error=str(e))
        return None, None


# ---------- 代码沙箱(L1) ----------

def _static_check(code):
    for pat in FORBIDDEN_PATTERNS:
        if pat in code:
            return False, pat
    return True, ""


def _clean_stale_sandbox(max_age_hours=24):
    """清理超过 max_age_hours 的沙箱残留目录(尽力而为)。"""
    try:
        if not SANDBOX_DIR.exists():
            return
        now = time.time()
        for d in SANDBOX_DIR.iterdir():
            try:
                if now - d.stat().st_mtime > max_age_hours * 3600:
                    shutil.rmtree(d, ignore_errors=True)
            except Exception as e:
                logger.log_exception("沙箱残留目录清理失败", path=str(d), error=str(e))
    except Exception as e:
        logger.log_exception("沙箱目录扫描失败", error=str(e))


def run_code_sandbox(code, timeout=10):
    """在受限沙箱执行 AI 生成的代码,返回输出文本。执行前日志留完整代码快照。"""
    ok, bad = _static_check(code)
    if not ok:
        return f"[沙箱拦截] 代码含被禁止的内容: {bad}"
    logger.log_error("执行 AI 生成代码(快照留档)", code=str(code))
    _clean_stale_sandbox()
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    # 注意:不能用 tempfile.mkdtemp(其 0o700 模式在部分 Windows 环境会被转成拒写 ACL)
    tmpdir = str(SANDBOX_DIR / f"sbx_{int(time.time() * 1000)}_{random.randint(0, 9999)}")
    try:
        os.makedirs(tmpdir, exist_ok=True)
    except Exception as e:
        return f"[沙箱创建目录失败] {e}"
    try:
        script = Path(tmpdir) / "script.py"
        try:
            script.write_text(code, encoding="utf-8")
        except Exception as e:
            return f"[沙箱写入失败] {e}"
        env = {
            "PYTHONPATH": tmpdir,
            "PATH": "",
            "TMP": tmpdir,
            "TEMP": tmpdir,
            "SYSTEMROOT": r"C:\Windows",
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-S", str(script)],   # 隔离模式 + 不加载 site
                capture_output=True, text=True, timeout=timeout,
                cwd=tmpdir, env=env,
            )
        except subprocess.TimeoutExpired:
            return f"[沙箱执行超时(>{timeout}s)]"
        except Exception as e:
            return f"[沙箱启动失败] {e}"
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            return f"[沙箱执行出错 rc={proc.returncode}] {err or out}"[:2000]
        return (out[:3000] or "[沙箱执行成功,无输出]")
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception as e:
            logger.log_exception("沙箱临时目录清理失败", path=str(tmpdir),
                                 error=str(e))


# ---------- 工具注册表 ----------

class ToolKit:
    """工具注册表 + 执行器。allow_generated_code 开启时才注册 run_python_code。"""

    def __init__(self, allow_generated_code=False, sandbox_timeout=10):
        self.allow_generated = bool(allow_generated_code)
        self.sandbox_timeout = int(sandbox_timeout)
        self.tools = {}      # name -> {"spec": dict, "run": callable|None}
        self.reload()

    def reload(self):
        self.tools = {}
        for name, spec, run_fn in discover_tools():
            spec = dict(spec)
            spec.setdefault("safe", False)
            self.tools[name] = {"spec": spec, "run": run_fn}
        # 用户自定义白名单工具(网页 JSON 文本栏维护)
        for t in config.load_custom_tools():
            try:
                spec = self._norm_custom_tool(t)
                if spec is None:
                    continue
                name = spec["name"]
                if not isinstance(name, str):
                    logger.log_error("自定义工具 name 非法,已跳过", name=name)
                    continue
                if name in self.tools:
                    logger.log_error("自定义工具与已有工具重名,已跳过",
                                     name=name)
                    continue
                self.tools[name] = {"spec": spec, "run": None}
            except Exception as e:
                logger.log_exception("自定义工具加载失败", error=str(e))
        if self.allow_generated:
            self.tools[RUN_CODE_TOOL_NAME] = {"spec": dict(RUN_CODE_TOOL), "run": None}

    @staticmethod
    def _norm_custom_tool(t):
        """校验/规整一条用户自定义工具;不合格返回 None。"""
        name = str(t.get("name") or "").strip()
        desc = str(t.get("description") or "").strip()
        if not name or not desc:
            return None
        params = t.get("parameters")
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}}
        return {
            "name": name,
            "description": desc,
            "safe": bool(t.get("safe", False)),
            "parameters": params,
            "code": str(t.get("code") or "").strip(),
        }

    def openai_tools(self, exclude=()):
        """生成 OpenAI function-calling tools 列表;exclude 里的工具名跳过。"""
        out = []
        for name, t in self.tools.items():
            if name in exclude:
                continue
            spec = t["spec"]
            desc = spec.get("description", "")
            if spec.get("safe"):
                desc = desc + "(安全工具,已通过白名单审核,可放心调用)"
            out.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": spec.get("parameters") or
                                  {"type": "object", "properties": {}},
                },
            })
        return out

    def safe_tool_names(self):
        return [n for n, t in self.tools.items() if t["spec"].get("safe")]

    def execute(self, name, args):
        """执行工具;返回结果字符串。name 不存在时报错。"""
        t = self.tools.get(name)
        if t is None:
            return f"[未注册工具] {name}"
        fn = t.get("run")
        if fn is None:
            if name == RUN_CODE_TOOL_NAME:
                code = str(args.get("code", ""))
                if not code.strip():
                    return "[沙箱执行失败] 代码为空"
                return run_code_sandbox(code, self.sandbox_timeout)
            # 用户自定义工具:code 在受限沙箱执行,args 变量为模型传入的参数字典
            code = str(t.get("spec", {}).get("code") or "")
            if not code:
                return f"[工具不可执行] {name}(定义里没有 code)"
            prelude = ("import json as _json\n"
                       "args = _json.loads(r'''"
                       + json.dumps(args or {}, ensure_ascii=False).replace("'''", "\\'\\'\\'")
                       + "''')\n")
            return run_code_sandbox(prelude + code, self.sandbox_timeout)
        try:
            return fn(**args) if args else fn()
        except TypeError:
            try:
                return fn()
            except Exception as e:
                return f"[工具执行出错] {e}"
        except Exception as e:
            return f"[工具执行出错] {e}"


# ---------- 网络搜索决策(分析模型) ----------

def decide_search(complete_fn, user_text):
    """用分析模型判断这条消息是否需要联网搜索;失败默认 False(不搜索)。"""
    prompt = (
        f"用户消息: {user_text}\n\n"
        "如果问题涉及实时信息(天气/新闻/股价/最新事件),或需要联网资料才能准确回答,"
        "返回 {\"need_search\": true};纯闲聊、计算、逻辑、常识、编程等不需要联网的,"
        "返回 {\"need_search\": false}。只返回 JSON,不要解释。"
    )
    try:
        raw = complete_fn([
            {"role": "system", "content": SEARCH_DECISION_SYSTEM},
            {"role": "user", "content": prompt},
        ])
        s = raw.strip()
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end > start:
            data = json.loads(s[start:end + 1])
            return bool(data.get("need_search", False))
    except Exception as e:
        logger.log_exception("搜索决策失败", error=str(e))
    return False


