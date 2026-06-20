import hashlib
import hmac
import base64
import json
import smtplib
import time
import urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from typing import Any, Dict, List, Optional

import requests

from . import logger
from .config_loader import config
from .storage import storage


class BaseNotifier:
    channel: str = ""

    def __init__(self):
        self.cfg = config.get(f"notify.notify_channels.{self.channel}", {})
        self.enabled = self.cfg.get("enabled", False)
        self.retry_count = self.cfg.get("retry_count", 3)
        self.retry_interval = self.cfg.get("retry_interval_seconds", 5)

    def send(self, content: Any, recipients: Optional[List[str]] = None) -> bool:
        raise NotImplementedError

    def send_with_retry(
        self, content: Any, recipients: Optional[List[str]] = None
    ) -> bool:
        if not self.enabled:
            logger.debug(f"[{self.channel}] 通知渠道未启用，跳过")
            return False

        placeholder_patterns = ["${", "example.com"]
        cfg_str = str(self.cfg)
        if any(p in cfg_str for p in placeholder_patterns):
            logger.debug(f"[{self.channel}] 通知渠道未真实配置，跳过")
            return False

        for attempt in range(1, self.retry_count + 1):
            try:
                if self.send(content, recipients):
                    logger.debug(f"[{self.channel}] 通知发送成功")
                    return True
            except Exception as e:
                logger.debug(
                    f"[{self.channel}] 第{attempt}次发送失败: {e}"
                )
            if attempt < self.retry_count:
                time.sleep(self.retry_interval)

        logger.warning(f"[{self.channel}] 通知发送失败，已重试{self.retry_count}次")
        return False


class WeWorkNotifier(BaseNotifier):
    channel = "wework"

    def send(self, content: Dict[str, Any], recipients: Optional[List[str]] = None) -> bool:
        url = self.cfg.get("webhook_url", "")
        if not url:
            return False

        payload = {
            "msgtype": "markdown",
            "markdown": {"content": content.get("markdown", "")},
        }
        mentioned = self.cfg.get("mentioned_mobile_list", [])
        if mentioned:
            payload["markdown"]["mentioned_mobile_list"] = mentioned

        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()
        return result.get("errcode", -1) == 0


class DingTalkNotifier(BaseNotifier):
    channel = "dingtalk"

    def _sign(self) -> tuple:
        timestamp = str(round(time.time() * 1000))
        secret = self.cfg.get("secret", "")
        if not secret:
            return timestamp, ""
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return timestamp, sign

    def send(self, content: Dict[str, Any], recipients: Optional[List[str]] = None) -> bool:
        base_url = self.cfg.get("webhook_url", "")
        if not base_url:
            return False

        timestamp, sign = self._sign()
        url = f"{base_url}&timestamp={timestamp}&sign={sign}" if sign else base_url

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": content.get("title", "通知"),
                "text": content.get("markdown", ""),
            },
        }
        at_mobiles = self.cfg.get("at_mobiles", [])
        if at_mobiles:
            payload["at"] = {"atMobiles": at_mobiles, "isAtAll": False}

        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()
        return result.get("errcode", -1) == 0


class EmailNotifier(BaseNotifier):
    channel = "email"

    def send(self, content: Dict[str, Any], recipients: Optional[List[str]] = None) -> bool:
        if not recipients:
            return False

        host = self.cfg.get("smtp_host", "")
        port = self.cfg.get("smtp_port", 465)
        use_ssl = self.cfg.get("smtp_use_ssl", True)
        username = self.cfg.get("smtp_username", "")
        password = self.cfg.get("smtp_password", "")
        sender = self.cfg.get("sender", username)

        if not all([host, username, password]):
            logger.warning("[email] SMTP配置不完整")
            return False

        msg = MIMEMultipart()
        msg["From"] = Header(sender)
        msg["To"] = Header(", ".join(recipients))
        msg["Subject"] = Header(content.get("subject", "通知"), "utf-8")

        body = content.get("body", "")
        msg.attach(MIMEText(body, "html", "utf-8"))

        try:
            if use_ssl:
                smtp = smtplib.SMTP_SSL(host, port, timeout=15)
            else:
                smtp = smtplib.SMTP(host, port, timeout=15)
                smtp.starttls()
            smtp.login(username, password)
            smtp.sendmail(sender, recipients, msg.as_string())
            smtp.quit()
            return True
        except Exception as e:
            logger.error(f"[email] 发送失败: {e}")
            return False


class NotifierService:
    def __init__(self):
        self.wework = WeWorkNotifier()
        self.dingtalk = DingTalkNotifier()
        self.email = EmailNotifier()

    def _resolve_recipients(self, role_names: List[str]) -> List[str]:
        default_map = config.get("approval.default_approvers", {})
        emails = []
        for role in role_names:
            emails.extend(default_map.get(role, []))
        return [e for e in emails if e]

    def render_template(self, template_name: str, context: Dict[str, Any]) -> Dict[str, Any]:
        templates = config.get("notify.templates", {})
        tpl = templates.get(template_name, {})
        if not tpl:
            logger.warning(f"通知模板不存在: {template_name}")
            return {}

        result = {}
        for key in ["wework_markdown", "dingtalk_markdown", "email_subject", "email_body"]:
            if key in tpl:
                try:
                    result[key] = tpl[key].format(**context)
                except KeyError as e:
                    logger.warning(f"模板渲染失败，缺少变量: {e}")
                    result[key] = tpl[key]
        result["title"] = tpl.get("title", template_name)
        return result

    def _log(
        self,
        channel: str,
        template_name: str,
        status: str,
        error: str = "",
        release_id: str = "",
        rollback_id: str = "",
        drill_id: str = "",
        recipients: Optional[List[str]] = None,
    ):
        storage.log_notification(
            {
                "release_id": release_id or None,
                "rollback_id": rollback_id or None,
                "drill_id": drill_id or None,
                "channel": channel,
                "template_name": template_name,
                "recipients": recipients or [],
                "status": status,
                "error_message": error,
            }
        )

    def send_all(
        self,
        template_name: str,
        context: Dict[str, Any],
        roles: Optional[List[str]] = None,
        extra_emails: Optional[List[str]] = None,
        release_id: str = "",
        rollback_id: str = "",
        drill_id: str = "",
    ) -> Dict[str, bool]:
        rendered = self.render_template(template_name, context)
        recipients = self._resolve_recipients(roles or [])
        if extra_emails:
            recipients.extend(extra_emails)
        recipients = list(set(recipients))

        results: Dict[str, bool] = {}

        if self.wework.enabled and "wework_markdown" in rendered:
            ok = self.wework.send_with_retry(
                {"markdown": rendered["wework_markdown"]}
            )
            results["wework"] = ok
            self._log(
                "wework", template_name, "success" if ok else "failed",
                release_id=release_id, rollback_id=rollback_id, drill_id=drill_id,
            )

        if self.dingtalk.enabled and "dingtalk_markdown" in rendered:
            ok = self.dingtalk.send_with_retry(
                {"title": rendered.get("title", ""), "markdown": rendered["dingtalk_markdown"]}
            )
            results["dingtalk"] = ok
            self._log(
                "dingtalk", template_name, "success" if ok else "failed",
                release_id=release_id, rollback_id=rollback_id, drill_id=drill_id,
            )

        if self.email.enabled and recipients and "email_body" in rendered:
            ok = self.email.send_with_retry(
                {"subject": rendered.get("email_subject", ""), "body": rendered["email_body"]},
                recipients=recipients,
            )
            results["email"] = ok
            self._log(
                "email", template_name, "success" if ok else "failed",
                release_id=release_id, rollback_id=rollback_id, drill_id=drill_id,
                recipients=recipients,
            )

        logger.info(f"通知发送完成 [{template_name}]: {results}")
        return results


notifier = NotifierService()
