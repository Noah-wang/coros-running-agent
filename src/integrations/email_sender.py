"""用 SMTP 把报告寄出去。

**为什么加邮件这条通道**

聊天平台各有各的门槛：Discord 中文用户基本不用；微信个人主体只能注册
订阅号，而订阅号在接口层面就不允许发模板消息，要主动推送必须
企业主体 + 认证服务号——那是行政流程，不是写代码能解决的。

邮件没有平台守门人，而且日报本来就是长文，比 IM 更适合。

**没配 SMTP 时什么都不做，但要说出来。** 静默跳过的话，
你会以为报告发了、用户会以为没人理他，两边都不知道哪儿断了。
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage

from src.runtime.trace import log_event

TIMEOUT_SECONDS = 30


def configured() -> bool:
    return bool(os.getenv("SMTP_HOST", "").strip() and os.getenv("SMTP_FROM", "").strip())


def _port() -> int:
    raw = os.getenv("SMTP_PORT", "").strip()
    if raw.isdigit():
        return int(raw)
    # 465 走隐式 TLS，587 走 STARTTLS。默认用 587，因为它更普遍。
    return 465 if os.getenv("SMTP_SSL", "").strip().lower() in {"1", "true", "yes", "on"} else 587


def _use_implicit_ssl() -> bool:
    explicit = os.getenv("SMTP_SSL", "").strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    if explicit in {"0", "false", "no", "off"}:
        return False
    return _port() == 465


def send_report(to_address: str, subject: str, body: str) -> None:
    """寄一封纯文本报告。失败就抛，让调用方决定要不要吞。"""
    if not configured():
        raise RuntimeError("SMTP 没有配置（需要 SMTP_HOST 和 SMTP_FROM）。")

    message = EmailMessage()
    message["From"] = os.getenv("SMTP_FROM", "")
    message["To"] = to_address
    message["Subject"] = subject
    # 报告是 Markdown。这里按纯文本发——邮件客户端渲染 Markdown 的行为
    # 差异太大，转 HTML 反而会把表格和代码块搞乱。
    message.set_content(body)

    host = os.getenv("SMTP_HOST", "").strip()
    port = _port()
    user = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")

    context = ssl.create_default_context()
    if _use_implicit_ssl():
        with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_SECONDS, context=context) as server:
            if user:
                server.login(user, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=TIMEOUT_SECONDS) as server:
            server.starttls(context=context)
            if user:
                server.login(user, password)
            server.send_message(message)

    log_event("email_report_sent", to=_masked(to_address), chars=len(body))


def _masked(address: str) -> str:
    """日志里不留完整邮箱。"""
    name, _, domain = address.partition("@")
    head = name[:2] if len(name) > 2 else name[:1]
    return f"{head}***@{domain}"
