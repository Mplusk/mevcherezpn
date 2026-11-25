"""Notify on monitor state changes without storing secrets or message content."""
import json
import os
from pathlib import Path
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import parseaddr

KEYS = {"de", "pl", "nl", "subscription"}
VALUES = {"ok", "failed", "unknown", "configuration_error"}
LABELS = {"de": "Германия", "pl": "Польша", "nl": "Нидерланды", "subscription": "Подписка"}
WORDS = {"ok": "работает", "failed": "ошибка проверки", "unknown": "проверка не выполнена", "configuration_error": "ошибка настройки проверки"}


def read_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and set(data) == KEYS and all(value in VALUES for value in data.values()):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def send_email(sender, password, recipient, state, test, *, source="github"):
    if any("\n" in value or "\r" in value for value in (sender, recipient)):
        raise ValueError("invalid address")
    if any(not parseaddr(value)[1] or parseaddr(value)[1] != value for value in (sender, recipient)):
        raise ValueError("invalid address")
    msg = EmailMessage()
    msg["From"], msg["To"] = sender, recipient
    msg["Subject"] = "Проверка VPN: тестовое сообщение" if test else "Проверка VPN: изменение состояния"
    lines = ["Тестовое уведомление мониторинга VPN." if test else "Состояние проверок VPN изменилось.", ""]
    lines += [f"{LABELS[key]}: {WORDS[state[key]]}" for key in ("de", "pl", "nl", "subscription")]
    sources = {"github": "GitHub Actions", "control": "основной VPS", "nl": "VPS Нидерланды"}
    if source not in sources:
        raise ValueError("invalid monitoring source")
    lines += ["", f"Точка проверки: {sources[source]}. Результаты относятся только к этой сети."]
    msg.set_content("\n".join(lines))
    with smtplib.SMTP_SSL("smtp.spaceweb.ru", 465, context=ssl.create_default_context(), timeout=20) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


def main():
    result_path = os.environ.get("MONITOR_RESULT_FILE", "")
    state_path = os.environ.get("MONITOR_STATE_FILE", "")
    current = read_state(result_path) or dict.fromkeys(KEYS, "unknown")
    previous = read_state(state_path)
    test = os.environ.get("MONITOR_TEST_EMAIL", "false").lower() == "true"
    source = os.environ.get("MONITOR_SOURCE", "github")
    sender = os.environ.pop("MONITOR_SMTP_USERNAME", "")
    password = os.environ.pop("MONITOR_SMTP_PASSWORD", "")
    recipient = os.environ.pop("MONITOR_ALERT_TO", "")
    if not sender or not password or not recipient or not state_path:
        print("ALERT_CONFIGURATION_ERROR", flush=True)
        return 1
    should_send = test or (previous is None and any(value != "ok" for value in current.values())) or (previous is not None and previous != current)
    if should_send:
        try:
            send_email(sender, password, recipient, current, test, source=source)
        except (OSError, ValueError, smtplib.SMTPException):
            print("ALERT_SEND_FAILED", flush=True)
            return 1
        print("ALERT_SENT", flush=True)
    else:
        print("ALERT_UNCHANGED", flush=True)
    if test:
        # A test is not a delivery of an actual transition; keep it pending.
        return 0
    try:
        path = Path(state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        print("ALERT_STATE_WRITE_FAILED", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
