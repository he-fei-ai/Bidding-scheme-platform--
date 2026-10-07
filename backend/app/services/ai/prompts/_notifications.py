"""告警通知通道实现（2026-10-06）。

提供企业微信/邮件等通知通道的具体实现，基于 _cache.py 的通知钩子。
"""

from __future__ import annotations

import logging
import os

from ._cache import register_alert_notification_hook


logger = logging.getLogger(__name__)


def send_wechat_work_alert(alerts: list[dict], webhook_url: str | None = None) -> bool:
    """发送企业微信群机器人告警通知。

    Args:
        alerts: 告警列表
        webhook_url: 企业微信webhook地址，默认从环境变量读取

    Returns:
        是否发送成功
    """
    webhook = webhook_url or os.getenv("WECHAT_WEBHOOK_URL")
    if not webhook:
        logger.debug("未配置企业微信webhook，跳过通知")
        return False

    if not alerts:
        return True

    # 构造消息
    lines = [f"【提示词监控告警】共 {len(alerts)} 条："]
    for i, alert in enumerate(alerts[:10], 1):  # 最多显示10条
        lines.append(f"{i}. [{alert.get('type', 'unknown')}] {alert.get('message', '')}")
    if len(alerts) > 10:
        lines.append(f"... 还有 {len(alerts) - 10} 条")

    content = "\n".join(lines)

    try:
        import requests  # 延迟导入，避免未安装时报错
        resp = requests.post(
            webhook,
            json={
                "msgtype": "text",
                "text": {"content": content},
            },
            timeout=5,
        )
        if resp.status_code == 200:
            logger.info("企业微信告警通知已发送")
            return True
        else:
            logger.warning("企业微信通知失败: %s %s", resp.status_code, resp.text)
            return False
    except ImportError:
        logger.warning("requests 未安装，无法发送企业微信通知")
        return False
    except Exception as e:
        logger.warning("企业微信通知异常: %s", e)
        return False


def setup_wechat_notification(webhook_url: str | None = None):
    """注册企业微信通知钩子。

    Args:
        webhook_url: 企业微信webhook地址，默认从环境变量读取
    """
    def _hook(alerts: list[dict]):
        send_wechat_work_alert(alerts, webhook_url)

    register_alert_notification_hook(_hook)
    logger.info("企业微信告警通知已注册")


def send_email_alert(
    alerts: list[dict],
    *,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    smtp_user: str | None = None,
    smtp_password: str | None = None,
    to_emails: str | None = None,
) -> bool:
    """发送邮件告警通知。

    Args:
        alerts: 告警列表
        smtp_host: SMTP服务器地址，默认从环境变量读取
        smtp_port: SMTP端口，默认从环境变量读取
        smtp_user: SMTP用户名，默认从环境变量读取
        smtp_password: SMTP密码，默认从环境变量读取
        to_emails: 收件人邮箱（逗号分隔），默认从环境变量读取

    Returns:
        是否发送成功
    """
    host = smtp_host or os.getenv("SMTP_HOST")
    port = smtp_port or int(os.getenv("SMTP_PORT", "465"))
    user = smtp_user or os.getenv("SMTP_USER")
    password = smtp_password or os.getenv("SMTP_PASSWORD")
    recipients = to_emails or os.getenv("ALERT_EMAILS")

    if not all([host, user, password, recipients]):
        logger.debug("未配置邮件通知参数，跳过")
        return False

    if not alerts:
        return True

    # 构造邮件内容
    subject = f"【提示词监控告警】共 {len(alerts)} 条"
    lines = [f"<h3>提示词监控告警（共 {len(alerts)} 条）</h3>", "<ul>"]
    for alert in alerts[:20]:
        lines.append(f"<li>[{alert.get('type', 'unknown')}] {alert.get('message', '')}</li>")
    if len(alerts) > 20:
        lines.append(f"<li>... 还有 {len(alerts) - 20} 条</li>")
    lines.append("</ul>")
    body = "\n".join(lines)

    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.header import Header

        msg = MIMEText(body, "html", "utf-8")
        msg["From"] = user
        msg["To"] = recipients
        msg["Subject"] = Header(subject, "utf-8")

        with smtplib.SMTP_SSL(host, port) as server:
            server.login(user, password)
            server.sendmail(user, recipients.split(","), msg.as_string())

        logger.info("邮件告警通知已发送")
        return True
    except Exception as e:
        logger.warning("邮件通知异常: %s", e)
        return False


def setup_email_notification(
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    smtp_user: str | None = None,
    smtp_password: str | None = None,
    to_emails: str | None = None,
):
    """注册邮件通知钩子。

    所有参数默认从环境变量读取：
    - SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, ALERT_EMAILS
    """
    def _hook(alerts: list[dict]):
        send_email_alert(
            alerts,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            to_emails=to_emails,
        )

    register_alert_notification_hook(_hook)
    logger.info("邮件告警通知已注册")


def send_email_report(report: dict, attachment_path: str | None = None) -> bool:
    """发送月度报表邮件。

    Args:
        report: 报表数据字典
        attachment_path: 报表文件路径（附件）

    Returns:
        是否发送成功
    """
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    recipients = os.getenv("ALERT_EMAILS")

    if not all([host, user, password, recipients]):
        logger.debug("未配置邮件参数，跳过报表发送")
        return False

    # 构造邮件内容
    summary = report.get("summary", {})
    cost = report.get("cost", {})
    subject = f"【提示词月度报表】总调用 {summary.get('total_calls', 0)} 次，成本 ¥{cost.get('total_cost', 0):.2f}"

    body = f"""
    <h3>提示词月度监控报表</h3>
    <p>生成时间：{report.get('generated_at', '')}</p>
    <h4>汇总</h4>
    <ul>
        <li>总调用次数：{summary.get('total_calls', 0)}</li>
        <li>总Token消耗：{summary.get('total_tokens', 0):,}</li>
        <li>预估成本：¥{cost.get('total_cost', 0):.2f}</li>
        <li>告警数量：{summary.get('alerts_count', 0)}</li>
    </ul>
    <p>详细报表请查看附件。</p>
    """

    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        from email.header import Header

        msg = MIMEMultipart()
        msg["From"] = user
        msg["To"] = recipients
        msg["Subject"] = Header(subject, "utf-8")

        # 正文
        msg.attach(MIMEText(body, "html", "utf-8"))

        # 附件（如果提供）
        if attachment_path and os.path.exists(attachment_path):
            from email.mime.base import MIMEBase
            from email import encoders
            with open(attachment_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            filename = os.path.basename(attachment_path)
            part.add_header("Content-Disposition", f"attachment; filename={filename}")
            msg.attach(part)

        with smtplib.SMTP_SSL(host, port) as server:
            server.login(user, password)
            server.sendmail(user, recipients.split(","), msg.as_string())

        logger.info("月度报表邮件已发送")
        return True
    except Exception as e:
        logger.warning("报表邮件发送异常: %s", e)
        return False


def send_dingtalk_alert(alerts: list[dict], webhook_url: str | None = None) -> bool:
    """发送钉钉群机器人告警通知。

    Args:
        alerts: 告警列表
        webhook_url: 钉钉机器人webhook地址

    Returns:
        是否发送成功
    """
    webhook = webhook_url or os.getenv("DINGTALK_WEBHOOK_URL")
    if not webhook:
        logger.debug("未配置钉钉webhook，跳过钉钉通知")
        return False

    if not alerts:
        return True

    # 构造Markdown消息
    level_emoji = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
    text_lines = ["### 【提示词告警通知】"]

    for alert in alerts:
        level = alert.get("level", "info")
        emoji = level_emoji.get(level, "🔵")
        text_lines.append(f"{emoji} **{level.upper()}** - {alert.get('message', '')}")

    text = "\n".join(text_lines)

    try:
        import requests
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": "提示词监控告警",
                "text": text,
            }
        }
        resp = requests.post(webhook, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("钉钉告警通知已发送，共%d条告警", len(alerts))
            return True
        else:
            logger.warning("钉钉通知发送失败: %s %s", resp.status_code, resp.text)
            return False
    except Exception as e:
        logger.warning("钉钉通知发送异常: %s", e)
        return False


def setup_dingtalk_notification(webhook_url: str | None = None):
    """注册钉钉通知钩子。

    webhook_url 默认从环境变量 DINGTALK_WEBHOOK_URL 读取。
    """
    def _hook(alerts: list[dict]):
        send_dingtalk_alert(alerts, webhook_url=webhook_url)

    register_alert_notification_hook(_hook)
    logger.info("钉钉告警通知已注册")


__all__ = [
    "send_wechat_work_alert",
    "setup_wechat_notification",
    "send_email_alert",
    "setup_email_notification",
    "send_email_report",
    "send_dingtalk_alert",
    "setup_dingtalk_notification",
]
