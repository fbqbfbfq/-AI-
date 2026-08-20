# -*- coding: utf-8 -*-
"""
微信 AI 分身机器人 —— iLink(腾讯官方 API)版【主程序】
====================================================

通过腾讯官方 iLink 接口,扫码后在你的微信里得到一个 AI 机器人联系人:
- 用手机微信扫二维码并确认授权
- 之后私聊这个机器人,AI 就会回复

优势:官方接口,无需 Hook、无需特定微信版本,扫码即用,便于分发与维护。

第一次运行:
    1. 双击 setup.bat 安装依赖(自动检测 Python≥3.10,国内镜像)
    2. 运行: python bot_ilink.py
    3. 用手机微信扫弹出的二维码并确认登录
    4. 在微信里私聊这个机器人,AI 会自动回复

记忆:短期原文(L1,JSON 落盘) + 长期要点(L2,向量检索) + 用户画像;
每 10 轮总结一次(一轮 = 用户 1 条 + AI 1 条);每条记忆带时间;对话注入当前时间。
配置热切换:网页(web_ui.py)保存后无需重启即生效。
"""
import base64
import os
import random
import sys
import time
import webbrowser

import requests

import check
import config
import logger
import scheduler
import service
import state

# ==================== 常量配置 ====================

# iLink 官方服务器;可用环境变量 ILINK_BASE_URL 覆盖(本地调试/代理时用)
BASE_URL = os.environ.get("ILINK_BASE_URL", "https://ilinkai.weixin.qq.com")
BASE_INFO = {                                 # 请求体里的宿主信息(仿照官方 SDK)
    "channel_version": "2.4.3",
    "bot_agent": "wechat-ai-bot/1.0 (python)",
}

QR_POLL_INTERVAL = 1.0      # 扫码状态轮询间隔(秒)
QR_WAIT_TIMEOUT = 600       # 二维码最长等待时间(秒)
GETUPDATES_TIMEOUT = 60     # 收消息长轮询超时(秒;服务器最长 hold 35s)

HERE = os.path.dirname(os.path.abspath(__file__))
QRCODE_FILE = os.path.join(HERE, "qrcode.png")


def log(msg: str) -> None:
    """带时间戳的日志输出。"""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ==================== iLink 协议客户端 ====================

def make_headers(token: str = "") -> dict:
    """构造 iLink 请求头。

    X-WECHAT-UIN 每次请求都要重新生成(随机 uint32 → 十进制字符串 → base64),
    用于防重放。
    """
    uin = str(random.randint(0, 0xFFFFFFFF))
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": base64.b64encode(uin.encode()).decode(),
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": "132099",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def api_post(path, body, token="", base_url=BASE_URL, timeout=30):
    url = f"{base_url}/{path}"
    resp = requests.post(url, json=body, headers=make_headers(token), timeout=timeout)
    try:
        return resp.json()
    except ValueError:
        log(f"[{path}] 返回了非 JSON 内容: {resp.text[:200]}")
        return {}


def api_get(path, token="", base_url=BASE_URL, timeout=30):
    url = f"{base_url}/{path}"
    resp = requests.get(url, headers=make_headers(token), timeout=timeout)
    try:
        return resp.json()
    except ValueError:
        log(f"[{path}] 返回了非 JSON 内容: {resp.text[:200]}")
        return {}


# ==================== 登录(扫码) ====================

def open_file(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)      # Windows:直接打开默认看图软件
    else:
        webbrowser.open(path)


def save_and_show_qrcode(qrcode_img_content) -> None:
    """把二维码内容保存为图片并打开;失败时打印链接让用户在手机微信打开。"""
    content = str(qrcode_img_content or "")
    if not content:
        log("没有拿到二维码图片内容,请检查网络后重试")
        return

    # 情况 1: data:image/...;base64,xxx —— 直接解码保存
    if content.startswith("data:image/"):
        try:
            b64 = content.split(",", 1)[1]
            with open(QRCODE_FILE, "wb") as f:
                f.write(base64.b64decode(b64))
            log(f"二维码已保存: {QRCODE_FILE}")
            open_file(QRCODE_FILE)
            return
        except Exception as e:
            log(f"解码二维码失败: {e}")

    # 情况 2: http(s) 链接 —— 尝试下载成图片
    if content.startswith("http"):
        try:
            resp = requests.get(content, timeout=15)
            resp.raise_for_status()
            data = resp.content
            if data[:4] == b"\x89PNG" or data[:2] in (b"\xff\xd8", b"BM"):
                with open(QRCODE_FILE, "wb") as f:
                    f.write(data)
                log(f"二维码已保存: {QRCODE_FILE}")
                open_file(QRCODE_FILE)
                return
            log("二维码链接不是图片,请用手机微信打开下面的链接完成连接:")
        except Exception as e:
            log(f"下载二维码失败: {e}")
            log("请用手机微信打开下面的链接完成连接:")
        log(content)  # 打印 liteapp 链接:可复制发给"文件传输助手",再在手机上打开
        return

    # 情况 3: 其他内容(base64 文本等)
    try:
        with open(QRCODE_FILE, "wb") as f:
            f.write(base64.b64decode(content))
        log(f"二维码已保存: {QRCODE_FILE}")
        open_file(QRCODE_FILE)
    except Exception as e:
        log(f"无法识别的二维码内容: {e}")


def request_qrcode(base_url=BASE_URL):
    """向服务器申请一个登录二维码,返回 (qrcode, 图片内容)。"""
    # 官方 2.x 用 POST;拿不到结果就退回 GET
    data = api_post("ilink/bot/get_bot_qrcode?bot_type=3",
                    {"local_token_list": []}, base_url=base_url)
    if not data or "qrcode" not in data:
        log("POST 申请二维码失败,尝试 GET...")
        data = api_get("ilink/bot/get_bot_qrcode?bot_type=3", base_url=base_url)
    if not data or "qrcode" not in data:
        raise RuntimeError(f"申请二维码失败: {data}")
    return data["qrcode"], data.get("qrcode_img_content", "")


def login():
    """完成扫码登录,返回 (bot_token, base_url);失败返回 (None, base_url)。"""
    base_url = BASE_URL
    deadline = time.time() + QR_WAIT_TIMEOUT

    while time.time() < deadline:
        try:
            qrcode, img = request_qrcode(base_url)
        except Exception as e:
            log(f"申请二维码失败: {e},3 秒后重试")
            time.sleep(3)
            continue

        save_and_show_qrcode(img)
        log("请用手机微信扫一扫二维码,并在手机上确认登录...")

        # 轮询扫码状态,直到确认或超时
        while time.time() < deadline:
            try:
                st = api_get(f"ilink/bot/get_qrcode_status?qrcode={qrcode}", base_url=base_url)
            except Exception as e:
                log(f"查询扫码状态失败: {e},稍后重试")
                time.sleep(2)
                continue
            status = st.get("status", "")
            if status == "wait":
                log("等待扫码...")
            elif status == "scaned":
                log("已扫码!请在手机上点击确认")
            elif status == "confirmed":
                token = st.get("bot_token", "")
                if st.get("baseurl"):
                    base_url = st["baseurl"].rstrip("/")
                log(f"登录成功! 服务器: {base_url}")
                return token, base_url
            elif status == "scaned_but_redirect":
                host = st.get("redirect_host", "")
                if host:
                    base_url = f"https://{host}"
                log("连接节点切换中,继续等待...")
            elif status == "need_verifycode":
                log("微信要求配对码:请在手机上查看并输入数字配对码完成确认")
            elif status == "verify_code_blocked":
                log("配对码错误次数过多,重新申请二维码...")
                break
            elif status == "expired":
                log("二维码已过期,重新申请...")
                break
            else:
                log(f"未知扫码状态: {status}")
            time.sleep(QR_POLL_INTERVAL)

    log("等待扫码超时,请重新运行本程序")
    return None, base_url


# ==================== 消息处理 ====================

class UserSession:
    """一个用户(微信号)的会话状态:仅缓存 typing_ticket(记忆由 service 统一管理)。"""

    def __init__(self):
        self.typing_ticket = None  # 由 getconfig 获取,可缓存约 24 小时


def get_text_of(msg) -> str:
    """从收到的消息里提取第一段文字;不是文字消息则返回空串。"""
    if msg.get("message_type") != 1:
        return ""
    for item in msg.get("item_list") or []:
        if item.get("type") == 1:
            return (item.get("text_item") or {}).get("text", "")
    return ""


def handle_command(text, from_id) -> str:
    cmd = text.strip().split()[0].lower()
    if cmd in ("/help", "/帮助"):
        return "可用指令:\n/help 查看帮助\n/clear 清空对话记忆(短期+长期)\n\n其他消息将由 AI 自动回复"
    if cmd in ("/clear", "/重置"):
        try:
            role_name = config.get_active_role()["name"]
            service.clear_memory(from_id, role_name, clear_l1=True, clear_l2=True)
            return "已清空对话记忆(画像保留)。"
        except Exception as e:
            logger.log_exception("清空记忆失败", user=from_id)
            return "清空失败,请查看日志。"
    return "未知指令,发送 /help 查看可用指令。"


def ai_reply(from_id, text) -> str:
    """调用 service 生成回复(时间注入/记忆检索/热切换都在 service 内处理)。"""
    try:
        role_name = config.get_active_role()["name"]
    except ValueError as e:
        return str(e)  # "还没有任何角色..."
    try:
        return service.chat_once(role_name, from_id, text)
    except Exception as e:
        logger.log_exception("对话处理失败", user=from_id, role=role_name)
        return "API 调用失败，请稍后重试。"


def _set_typing(from_id, ticket, status, token, base_url) -> None:
    if ticket:
        api_post(
            "ilink/bot/sendtyping",
            {"ilink_user_id": from_id, "typing_ticket": ticket,
             "status": status, "base_info": BASE_INFO},
            token, base_url,
        )


def _typing_delay(text) -> float:
    """按字数模拟真人打字:每多一个字增加 typing_seconds_per_char 秒,
    上限 typing_max_seconds(默认 10 秒)。0 或未配置时不做停顿。
    配置在网页「系统配置 → 系统设置」里改,热切换即时生效。"""
    si = config.load_system().get("system_information", {})
    per_char = float(si.get("typing_seconds_per_char", 0.1))
    max_delay = float(si.get("typing_max_seconds", 10.0))
    if not text or per_char <= 0:
        return 0.0
    return min(max_delay, len(text) * per_char)


def _send_one(from_id, context_token, text, session, token, base_url) -> bool:
    """发送一段,带"正在输入"和模拟打字停顿;成功返回 True。"""
    _set_typing(from_id, session.typing_ticket, 1, token, base_url)
    time.sleep(_typing_delay(text))
    try:
        client_id = f"openclaw-weixin-{random.randint(0, 0xFFFFFFFF):08x}"
        api_post(
            "ilink/bot/sendmessage",
            {
                "msg": {
                    "from_user_id": "",               # 必填,官方 SDK 发的是空串
                    "to_user_id": from_id,
                    "client_id": client_id,
                    "message_type": 2,                # BOT 发出的消息
                    "message_state": 2,               # 完整消息
                    "context_token": context_token,   # 必须用当前消息的 token
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                },
                "base_info": BASE_INFO,
            },
            token, base_url,
        )
        log(f"已回复({from_id}): {text[:40]}")
        return True
    except Exception as e:
        log(f"发送失败: {e}")
        return False
    finally:
        _set_typing(from_id, session.typing_ticket, 2, token, base_url)


def send_reply(from_id, context_token, reply, session, token, base_url) -> None:
    """回复:优先分段(模拟真人打字),首段失败则自动降级为单条完整回复。"""
    # getconfig: 每个用户首次获取 typing_ticket(可缓存)
    if session.typing_ticket is None:
        cfg = api_post(
            "ilink/bot/getconfig",
            {"ilink_user_id": from_id, "context_token": context_token,
             "base_info": BASE_INFO},
            token, base_url,
        )
        session.typing_ticket = cfg.get("typing_ticket", "")

    # 分隔符/开关从配置读取(热切换)
    si = config.load_system().get("system_information", {})
    sep = si.get("segment_separator", "|||") or "|||"
    segmented = bool(si.get("segmented_reply", True))

    segments = [s.strip() for s in reply.split(sep) if s.strip()][:4]
    if not segmented or len(segments) <= 1:
        segments = [reply.strip()]

    for idx, seg in enumerate(segments):
        ok = _send_one(from_id, context_token, seg, session, token, base_url)
        if not ok and idx == 0 and len(segments) > 1:
            log("分段发送失败,降级为单条完整回复")
            _send_one(from_id, context_token, reply, session, token, base_url)
            break
        if idx < len(segments) - 1:
            time.sleep(0.3)  # 段间小停顿


def handle_message(msg, sessions, token, base_url, state_store=None) -> None:
    """处理一条收到的消息。"""
    text = get_text_of(msg)
    if not text:
        log(f"忽略非文字消息: {msg.get('message_type')}")
        return
    from_id = msg.get("from_user_id", "")
    if not from_id:
        log("消息缺少 from_user_id,忽略")
        return
    if msg.get("group_id"):
        log(f"忽略群聊消息(当前版本只支持私聊): {text[:30]}")
        return

    session = sessions.setdefault(from_id, UserSession())
    context_token = msg.get("context_token", "")
    # 记录 context_token(主动发消息的通行证)与活跃时间
    if state_store is not None:
        state_store.update_token(from_id, context_token)
    log(f"收到消息({from_id}): {text[:50]}")

    if text.startswith("/"):
        reply = handle_command(text, from_id)
    else:
        reply = ai_reply(from_id, text)

    send_reply(from_id, context_token, reply, session, token, base_url)


def run_bot(token, base_url) -> None:
    """主循环:长轮询收消息,收到就处理。另启动主动对话调度线程。"""
    get_updates_buf = ""
    sessions = {}
    state_store = state.StateStore()
    log("开始监听消息,用另一个微信给机器人发条消息试试吧!")

    def on_proactive(user_id):
        """周期性主动:生成一条主动消息并发送(发前已由调度器检查 token 新鲜)。"""
        try:
            role_name = config.get_active_role()["name"]
            reply = service.proactive_once(role_name, user_id)
        except Exception as e:
            logger.log_exception("主动消息生成失败", user=user_id)
            return
        ctx = state_store.get_user(user_id).get("context_token", "")
        if not ctx:
            return
        session = sessions.setdefault(user_id, UserSession())
        send_reply(user_id, ctx, reply, session, token, base_url)

    def on_reminder(r):
        """提醒到点:直接发送提醒内容(调度器已检查 token 新鲜)。"""
        user_id = r.get("user_id", "")
        ctx = state_store.get_user(user_id).get("context_token", "")
        if not ctx:
            return
        session = sessions.setdefault(user_id, UserSession())
        send_reply(user_id, ctx, f"⏰ 提醒:{r.get('content', '')}",
                   session, token, base_url)

    sched = scheduler.Scheduler(
        state_store=state_store,
        proactive_config_fn=lambda: config.load_system().get(
            "proactive_information", {}),
        on_proactive=on_proactive,
        on_reminder=on_reminder,
    )
    sched.start()
    log("主动对话调度已启动(定时/提醒/周期性主动)")

    while True:
        try:
            result = api_post(
                "ilink/bot/getupdates",
                {"get_updates_buf": get_updates_buf, "base_info": BASE_INFO},
                token, base_url, timeout=GETUPDATES_TIMEOUT,
            )
            # 游标:必须每次更新,否则会重复收到旧消息
            get_updates_buf = result.get("get_updates_buf") or get_updates_buf

            for msg in result.get("msgs") or []:
                try:
                    handle_message(msg, sessions, token, base_url, state_store)
                except Exception as e:
                    logger.log_exception("处理消息时出错")
                    log(f"处理消息时出错: {e}")
        except KeyboardInterrupt:
            log("收到 Ctrl+C,退出")
            break
        except Exception as e:
            log(f"getupdates 出错: {e},3 秒后重试")
            time.sleep(3)


# ==================== 主程序 ====================

def startup_check() -> None:
    """启动自检:损坏自愈 + 配置完整性检查。"""
    config.repair_corrupted_files()
    results = check.check_all()
    for r in results:
        if r["level"] == "error":
            log(f"[自检] ✗ {r['item']}: {r['message']}")
        elif r["level"] == "warn":
            log(f"[自检] ⚠ {r['item']}: {r['message']}")
    blocking = check.blocking_error(results)
    if blocking is not None:
        logger.log_error("启动自检未通过", item=blocking["item"], message=blocking["message"])
        log(f"启动失败: {blocking['message']}")
        sys.exit(1)


def main() -> None:
    log("=" * 50)
    log("微信 AI 分身机器人(iLink 官方 API 版)")
    log("=" * 50)

    logger.setup_logger()
    startup_check()

    log("正在申请登录二维码...")
    token, base_url = login()
    if not token:
        sys.exit(1)

    run_bot(token, base_url)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("已退出")
