"""Send the memo and read replies (Lesson 4).

Sending uses SMTP; reading replies uses IMAP. Both use the dedicated mailbox in
.env. No web server is needed: the engine polls its own inbox on each run.

Every connection here carries an explicit timeout. Without one, Python blocks
forever, and a mail port that is dropped rather than refused (a company
firewall, some home routers, and many cloud providers, which block outbound
mail on new accounts by default) turns the whole run into a blank window with
no error and no log line. Under cron that is silent and permanent: the alert
path is SMTP too, so it hangs alongside the run. Thirty seconds and a logged
failure is always the better answer.
"""

from __future__ import annotations

import re

TIMEOUT_SECONDS = 30


def _md_to_html(markdown_text: str) -> str:
    try:
        import markdown  # lazy
        return markdown.markdown(markdown_text, extensions=["tables", "sane_lists"])
    except Exception:
        from html import escape
        return f"<pre style='font-family:Georgia,serif;white-space:pre-wrap'>{escape(markdown_text)}</pre>"


def send_memo(env: dict, subject: str, markdown_body: str, logger=None) -> None:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = env["ENGINE_EMAIL_ADDRESS"]
    msg["To"] = env["RECIPIENT_EMAIL"]
    msg.attach(MIMEText(markdown_body, "plain", "utf-8"))
    msg.attach(MIMEText(_md_to_html(markdown_body), "html", "utf-8"))

    with smtplib.SMTP_SSL(env["SMTP_HOST"], int(env["SMTP_PORT"]),
                          timeout=TIMEOUT_SECONDS) as server:
        server.login(env["ENGINE_EMAIL_ADDRESS"], env["ENGINE_EMAIL_APP_PASSWORD"])
        server.send_message(msg)
    if logger:
        logger.info(f"Sent memo to {env['RECIPIENT_EMAIL']}: {subject}")


def send_alert(env: dict, subject: str, body: str, logger=None) -> None:
    """Plain-text alert, used when a run fails."""
    import smtplib
    from email.mime.text import MIMEText

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = env["ENGINE_EMAIL_ADDRESS"]
    msg["To"] = env["RECIPIENT_EMAIL"]
    try:
        with smtplib.SMTP_SSL(env["SMTP_HOST"], int(env["SMTP_PORT"]),
                              timeout=TIMEOUT_SECONDS) as server:
            server.login(env["ENGINE_EMAIL_ADDRESS"], env["ENGINE_EMAIL_APP_PASSWORD"])
            server.send_message(msg)
    except Exception as exc:
        if logger:
            logger.error(f"Could not send alert email: {exc}")


def _extract_reply_text(msg) -> str:
    """Pull out just what the person wrote, ignoring the quoted memo below."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition", ""))
            if part.get_content_type() == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(part.get_content_charset() or "utf-8", "replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(msg.get_content_charset() or "utf-8", "replace")

    kept = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith(">"):
            continue
        if re.match(r"^On .+wrote:$", s):
            break
        if s.startswith("-----Original Message-----"):
            break
        if s.startswith("From:") and kept:
            break
        kept.append(line)
    return "\n".join(kept).strip()


def read_replies(env: dict, logger=None) -> list:
    """Return the text of each unread reply, and mark those messages read."""
    import imaplib
    import email

    texts = []
    try:
        box = imaplib.IMAP4_SSL(env["IMAP_HOST"], int(env["IMAP_PORT"]),
                                timeout=TIMEOUT_SECONDS)
        box.login(env["ENGINE_EMAIL_ADDRESS"], env["ENGINE_EMAIL_APP_PASSWORD"])
        box.select("INBOX")
        status, data = box.search(None, "UNSEEN")
        if status == "OK":
            for num_id in data[0].split():
                status, msg_data = box.fetch(num_id, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                text = _extract_reply_text(msg)
                if text:
                    texts.append(text)
                box.store(num_id, "+FLAGS", "\\Seen")
        box.logout()
    except Exception as exc:
        if logger:
            logger.warning(f"Could not read replies ({exc}); continuing without feedback.")
    return texts
