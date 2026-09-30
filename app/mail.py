import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate

from app.config import Settings


class SendFailed(Exception):
    pass


class SendUncertain(Exception):
    pass


class QQMailer:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send(self, lead, task, first=None):
        message = EmailMessage()
        message["From"] = self.settings.smtp_username
        message["To"] = lead["email"]
        message["Subject"] = task["subject"]
        message["Message-ID"] = task["message_id"]
        message["Date"] = formatdate(localtime=False)
        if first:
            message["In-Reply-To"] = first["message_id"]
            message["References"] = first["message_id"]
        message.set_content(task["body"])
        for attachment in task.get("attachments", []):
            maintype, subtype = attachment["content_type"].split("/", 1)
            message.add_attachment(
                attachment["content"], maintype=maintype, subtype=subtype, filename=attachment["filename"]
            )
        smtp = None
        submitting = False
        try:
            if self.settings.smtp_ssl:
                smtp = smtplib.SMTP_SSL(
                    self.settings.smtp_host,
                    self.settings.smtp_port,
                    timeout=20,
                    context=ssl.create_default_context(),
                )
            else:
                smtp = smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=20)
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.settings.smtp_username, self.settings.smtp_password)
            submitting = True
            refused = smtp.send_message(message)
            if refused:
                raise SendFailed("SMTP 明确拒收收件地址，请检查该地址是否有效。")
        except SendFailed:
            raise
        except smtplib.SMTPAuthenticationError:
            raise SendFailed("QQ SMTP 授权失败。请检查发件邮箱、授权码及 SMTP 服务是否开启。") from None
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            raise SendFailed("SMTP 明确拒绝邮件。请检查收发件地址、邮件内容和邮箱限制。") from None
        except (OSError, smtplib.SMTPException, ValueError):
            if submitting:
                raise SendUncertain(
                    "SMTP 提交结果不确定，请检查收件箱。系统已暂停该会话，不会自动重发。"
                ) from None
            raise SendFailed("连接 QQ SMTP 失败，请检查网络、服务器、端口及 SSL 设置。") from None
        finally:
            if smtp:
                # Closing must never turn an accepted message into a retryable failure.
                try:
                    smtp.close()
                except OSError:
                    pass
