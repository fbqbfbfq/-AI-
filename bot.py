# -*- coding: utf-8 -*-
"""
微信 AI 分身机器人 —— WeChatFerry(PC Hook)版【备用/可选】
==========================================================

⚠️ 主程序请用 bot_ilink.py(腾讯官方 iLink API,扫码即用,无需特定微信版本)。
本 bot.py 是 WeChatFerry PC Hook 版,需要精确匹配的 PC 微信版本 + 注入 DLL,
维护成本高、有封号风险,仅作备用保留。依赖: pip install wcferry。

通过 WeChatFerry 接管 PC 微信(登录小号),让"小号本身"变成 AI 分身:
大号找小号聊天,AI 以小号身份回复。

- 角色/模型/开关全部热切换:网页保存后无需重启即可生效。
- 记忆:短期原文(L1,JSON 落盘) + 长期要点(L2,向量检索) + 用户画像。
- 分段回复:按 segment_separator 切分逐段发送,段间按长度模拟打字;记忆存整段。

第一次运行:
    1. 双击 setup.bat 安装依赖(自动检测 Python≥3.10,国内镜像)
    2. 登录 PC 微信(小号)
    3. 运行: python bot.py
    4. 用大号给小号发消息,AI 会自动回复
"""
import os
import sys
import time
from queue import Empty

import check
import config
import logger
import service

try:
    from wcferry import Wcf
except ImportError:
    Wcf = None


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def get_text_of(msg) -> str:
    if msg.is_text():
        return (msg.content or "").strip()
    return ""


def handle_command(text, sender) -> str:
    cmd = text.strip().split()[0].lower()
    if cmd in ("/help", "/帮助"):
        return "可用指令:\n/help 查看帮助\n/clear 清空对话记忆(短期+长期)\n\n其他消息将由 AI 自动回复"
    if cmd in ("/clear", "/重置"):
        try:
            role_name = config.get_active_role()["name"]
            service.clear_memory(sender, role_name, clear_l1=True, clear_l2=True)
            return "已清空对话记忆(画像保留)。"
        except Exception as e:
            logger.log_exception("清空记忆失败", sender=sender)
            return "清空失败,请查看日志。"
    return "未知指令,发送 /help 查看可用指令。"


def ai_reply(sender, text) -> str:
    """调用 service 生成回复(每轮热切换在 service 内处理)。"""
    try:
        role = config.get_active_role()
        role_name = role["name"]
    except ValueError as e:
        return str(e)  # "还没有任何角色..."
    try:
        return service.chat_once(role_name, sender, text)
    except Exception as e:
        logger.log_exception("对话处理失败", sender=sender, role=role_name)
        return "API 调用失败，请稍后重试。"


def _typing_delay(text) -> float:
    """按字数模拟真人打字:每多一个字增加 typing_seconds_per_char 秒,
    上限 typing_max_seconds(默认 10 秒)。0 或未配置时不做停顿。"""
    si = config.load_system().get("system_information", {})
    per_char = float(si.get("typing_seconds_per_char", 0.1))
    max_delay = float(si.get("typing_max_seconds", 10.0))
    if not text or per_char <= 0:
        return 0.0
    return min(max_delay, len(text) * per_char)


def send_reply(wcf, to_wxid, reply) -> None:
    """分段回复(模拟真人逐条发);某段发送失败则整条单发兜底。"""
    si = config.load_system().get("system_information", {})
    sep = si.get("segment_separator", "|||") or "|||"
    segmented = bool(si.get("segmented_reply", True))

    segments = [s.strip() for s in reply.split(sep) if s.strip()][:4]
    if not segmented or len(segments) <= 1:
        segments = [reply.strip()]

    for i, seg in enumerate(segments):
        status = wcf.send_text(seg, to_wxid)
        if status != 0:
            log(f"第 {i + 1} 段发送失败(status={status}),整条单发兜底")
            wcf.send_text(reply, to_wxid)
            break
        log(f"已回复({to_wxid}): {seg[:40]}")
        if i < len(segments) - 1:
            time.sleep(_typing_delay(seg))  # 段间按长度模拟打字停顿


def handle_message(wcf, msg) -> None:
    if msg.from_self():
        return                       # 忽略自己发的,避免回环
    if msg.from_group():
        log(f"忽略群聊消息: {(msg.content or '')[:30]}")
        return
    text = get_text_of(msg)
    if not text:
        log(f"忽略非文字消息(type={msg.type})")
        return

    sender = msg.sender
    log(f"收到消息({sender}): {text[:50]}")
    if text.startswith("/"):
        reply = handle_command(text, sender)
    else:
        reply = ai_reply(sender, text)
    send_reply(wcf, sender, reply)


def run_bot(wcf) -> None:
    wcf.enable_receiving_msg()
    log("开始监听消息,用大号给小号发条消息试试吧!")

    while True:
        try:
            msg = wcf.get_msg()      # 阻塞取消息(超时 1s 抛 Empty)
        except Empty:
            continue
        try:
            handle_message(wcf, msg)
        except Exception as e:
            logger.log_exception("处理消息时出错")
            log(f"处理消息时出错: {e}")


# ==================== 主程序 ====================

def startup_check() -> None:
    """启动自检:损坏自愈 + 配置完整性检查。"""
    config.repair_corrupted_files()   # 坏文件删除+重建+日志+提示
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
    log("微信 AI 分身机器人(WeChatFerry 版)")
    log("=" * 50)

    logger.setup_logger()
    startup_check()

    if Wcf is None:
        log("未安装 wcferry,请先运行 setup.bat 或执行: pip install wcferry")
        sys.exit(1)

    log("连接微信中...请先登录 PC 微信(小号)")
    wcf = Wcf()                     # block=True:会一直等到微信登录成功
    log(f"微信已连接,当前账号: {wcf.get_self_wxid()}")

    run_bot(wcf)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("已退出")
