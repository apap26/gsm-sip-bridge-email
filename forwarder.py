#!/usr/bin/env python3
"""Forward inbound gsm-sip-bridge SMS records to email."""

from __future__ import annotations

import json
import logging
import os
import signal
import smtplib
import sqlite3
import ssl
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote


LOG = logging.getLogger("gsm_sip_bridge_email")
REQUIRED_SMS_COLUMNS = {"id", "sender", "body", "received_at"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }
        for name in ("sms_id", "sender", "error"):
            if hasattr(record, name):
                entry[name] = getattr(record, name)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    LOG.handlers[:] = [handler]
    LOG.setLevel(level.upper())
    LOG.propagate = False


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class Config:
    sms_db_path: str
    state_db_path: str
    health_file: str
    smtp_host: str
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_from: str
    smtp_to: tuple[str, ...]
    smtp_starttls: bool
    smtp_ssl: bool
    smtp_timeout_seconds: float
    poll_interval_seconds: float
    batch_size: int
    db_busy_timeout_seconds: float
    log_level: str

    @classmethod
    def from_env(cls) -> Config:
        required = ("SMTP_HOST", "SMTP_FROM", "SMTP_TO")
        missing = [name for name in required if not os.getenv(name, "").strip()]
        if missing:
            raise ValueError(f"missing required settings: {', '.join(missing)}")

        username = os.getenv("SMTP_USERNAME", "").strip() or None
        password = os.getenv("SMTP_PASSWORD") or None
        if bool(username) != bool(password):
            raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must be set together")

        starttls = env_bool("SMTP_STARTTLS", True)
        use_ssl = env_bool("SMTP_SSL", False)
        if starttls and use_ssl:
            raise ValueError("SMTP_STARTTLS and SMTP_SSL cannot both be enabled")

        recipients = tuple(
            address.strip()
            for address in os.environ["SMTP_TO"].split(",")
            if address.strip()
        )
        if not recipients:
            raise ValueError("SMTP_TO must contain at least one address")

        config = cls(
            sms_db_path=os.getenv("SMS_DB_PATH", "/bridge-data/store.db"),
            state_db_path=os.getenv("STATE_DB_PATH", "/data/state.db"),
            health_file=os.getenv("HEALTH_FILE", "/tmp/healthy"),
            smtp_host=os.environ["SMTP_HOST"],
            smtp_port=int(os.getenv("SMTP_PORT", "587")),
            smtp_username=username,
            smtp_password=password,
            smtp_from=os.environ["SMTP_FROM"],
            smtp_to=recipients,
            smtp_starttls=starttls,
            smtp_ssl=use_ssl,
            smtp_timeout_seconds=float(os.getenv("SMTP_TIMEOUT_SECONDS", "30")),
            poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "30")),
            batch_size=int(os.getenv("BATCH_SIZE", "100")),
            db_busy_timeout_seconds=float(os.getenv("DB_BUSY_TIMEOUT_SECONDS", "5")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )
        if config.smtp_port < 1 or config.smtp_port > 65535:
            raise ValueError("SMTP_PORT must be between 1 and 65535")
        if config.poll_interval_seconds <= 0 or config.batch_size <= 0:
            raise ValueError("POLL_INTERVAL_SECONDS and BATCH_SIZE must be positive")
        return config


@dataclass(frozen=True)
class Sms:
    id: int
    sender: str
    body: str
    received_at: str


class Forwarder:
    def __init__(self, config: Config):
        self.config = config
        self.stopping = False
        Path(config.state_db_path).parent.mkdir(parents=True, exist_ok=True)
        self.state = sqlite3.connect(config.state_db_path)
        self.state.execute("PRAGMA journal_mode=WAL")
        self.state.execute("PRAGMA synchronous=FULL")
        self.state.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_sms (
                sms_id INTEGER PRIMARY KEY,
                sent_at TEXT NOT NULL,
                message_id TEXT NOT NULL
            )
            """
        )
        self.state.commit()

    def close(self) -> None:
        self.state.close()

    def stop(self, _signum: int, _frame: object) -> None:
        self.stopping = True

    def open_source(self) -> sqlite3.Connection:
        path = quote(str(Path(self.config.sms_db_path).resolve()), safe="/")
        connection = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=self.config.db_busy_timeout_seconds,
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute(
            f"PRAGMA busy_timeout={int(self.config.db_busy_timeout_seconds * 1000)}"
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sms)").fetchall()
        }
        if not REQUIRED_SMS_COLUMNS.issubset(columns):
            connection.close()
            missing = ", ".join(sorted(REQUIRED_SMS_COLUMNS - columns))
            raise RuntimeError(f"sms table is missing required columns: {missing}")
        return connection

    def already_sent(self, sms_id: int) -> bool:
        row = self.state.execute(
            "SELECT 1 FROM sent_sms WHERE sms_id = ?", (sms_id,)
        ).fetchone()
        return row is not None

    def send(self, sms: Sms) -> str:
        message_id = f"<gsm-sip-bridge-email-sms-{sms.id}@localhost>"
        message = EmailMessage()
        message["Subject"] = f"SMS from {sms.sender}"
        message["From"] = self.config.smtp_from
        message["To"] = ", ".join(self.config.smtp_to)
        message["Message-ID"] = message_id
        message.set_content(
            f"Sender: {sms.sender}\nReceived at: {sms.received_at}\n\n{sms.body}\n",
            charset="utf-8",
        )

        smtp_class = smtplib.SMTP_SSL if self.config.smtp_ssl else smtplib.SMTP
        context = ssl.create_default_context()
        kwargs: dict[str, object] = {
            "host": self.config.smtp_host,
            "port": self.config.smtp_port,
            "timeout": self.config.smtp_timeout_seconds,
        }
        if self.config.smtp_ssl:
            kwargs["context"] = context
        with smtp_class(**kwargs) as client:
            if self.config.smtp_starttls:
                client.starttls(context=context)
            if self.config.smtp_username:
                client.login(self.config.smtp_username, self.config.smtp_password or "")
            refused = client.send_message(message)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        return message_id

    def mark_sent(self, sms_id: int, message_id: str) -> None:
        with self.state:
            self.state.execute(
                "INSERT OR IGNORE INTO sent_sms (sms_id, sent_at, message_id) "
                "VALUES (?, ?, ?)",
                (sms_id, datetime.now(UTC).isoformat(), message_id),
            )

    def poll_once(self) -> tuple[int, int]:
        sent = failed = 0
        last_id = 0
        with closing(self.open_source()) as source:
            while not self.stopping:
                rows = source.execute(
                    "SELECT id, sender, body, received_at FROM sms "
                    "WHERE id > ? ORDER BY id ASC LIMIT ?",
                    (last_id, self.config.batch_size),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    sms = Sms(*row)
                    last_id = sms.id
                    if self.already_sent(sms.id):
                        continue
                    try:
                        message_id = self.send(sms)
                        self.mark_sent(sms.id, message_id)
                        sent += 1
                        LOG.info(
                            "SMS delivered",
                            extra={"sms_id": sms.id, "sender": sms.sender},
                        )
                    except Exception as error:
                        failed += 1
                        LOG.error(
                            "SMTP delivery failed; SMS will be retried",
                            extra={
                                "sms_id": sms.id,
                                "sender": sms.sender,
                                "error": f"{type(error).__name__}: {error}",
                            },
                        )
                if len(rows) < self.config.batch_size:
                    break
        Path(self.config.health_file).touch()
        return sent, failed

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        LOG.info("forwarder started")
        while not self.stopping:
            try:
                sent, failed = self.poll_once()
                if sent or failed:
                    LOG.info(f"poll complete: sent={sent}, failed={failed}")
            except (sqlite3.Error, OSError, RuntimeError) as error:
                LOG.error(
                    "poll failed; retrying later",
                    extra={"error": f"{type(error).__name__}: {error}"},
                )
            if not self.stopping:
                time.sleep(self.config.poll_interval_seconds)
        LOG.info("forwarder stopped")


def main() -> int:
    try:
        config = Config.from_env()
        configure_logging(config.log_level)
        forwarder = Forwarder(config)
    except (OSError, sqlite3.Error, ValueError) as error:
        configure_logging("INFO")
        LOG.error("startup failed", extra={"error": f"{type(error).__name__}: {error}"})
        return 1
    try:
        forwarder.run()
    finally:
        forwarder.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
