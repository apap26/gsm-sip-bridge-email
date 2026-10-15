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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote


LOG = logging.getLogger("gsm_sip_bridge_email")
REQUIRED_SMS_COLUMNS = {"id", "module_id", "sender", "body", "received_at"}


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
    multipart_wait_seconds: float = 180

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
            multipart_wait_seconds=float(os.getenv("MULTIPART_WAIT_SECONDS", "180")),
        )
        if config.smtp_port < 1 or config.smtp_port > 65535:
            raise ValueError("SMTP_PORT must be between 1 and 65535")
        if config.poll_interval_seconds <= 0 or config.batch_size <= 0:
            raise ValueError("POLL_INTERVAL_SECONDS and BATCH_SIZE must be positive")
        if not 0 < config.multipart_wait_seconds < float("inf"):
            raise ValueError("MULTIPART_WAIT_SECONDS must be finite and positive")
        return config


@dataclass(frozen=True)
class Sms:
    id: int
    module_id: str
    sender: str
    body: str
    received_at: str
    concat_ref: int | None = None
    concat_total: int | None = None
    concat_sequence: int | None = None
    transport: str = ""


def received_time(sms: Sms) -> datetime:
    value = datetime.fromisoformat(sms.received_at.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("multipart received_at must include a timezone")
    return value


def multipart_key(sms: Sms) -> tuple | None:
    if (
        type(sms.concat_ref) is int and 0 <= sms.concat_ref <= 65535
        and type(sms.concat_total) is int and 2 <= sms.concat_total <= 255
        and type(sms.concat_sequence) is int
        and 1 <= sms.concat_sequence <= sms.concat_total
    ):
        try:
            received_time(sms)
        except ValueError:
            return None
        return (sms.module_id, sms.sender, sms.transport, sms.concat_ref, sms.concat_total)
    return None


def assemble_messages(messages: list[Sms], now: datetime, wait: float):
    """Yield (email, source IDs); pending parts stay in the read-only source.

    Never infer concatenation from sender, text length, or '[1/2]' text.
    Reference reuse is separated by a bounded arrival window. Conflicting
    duplicate sequence numbers are not combined, even after the deadline.
    """
    groups: dict[tuple, list[list[Sms]]] = {}
    for sms in messages:
        key = multipart_key(sms)
        if key is None:
            yield sms, [sms.id]
            continue
        windows = groups.setdefault(key, [])
        group = next((g for g in windows if abs(
            (received_time(sms) - received_time(g[0])).total_seconds()
        ) <= wait), None)
        if group is None:
            windows.append([sms])
        else:
            group.append(sms)
    for windows in groups.values():
        for parts in windows:
            ordered: dict[int, Sms] = {}
            conflict = False
            for part in parts:
                previous = ordered.get(part.concat_sequence)
                if previous and previous.body != part.body:
                    conflict = True
                ordered[part.concat_sequence] = part
            first = min(parts, key=lambda s: s.id)
            expired = (now - min(received_time(s) for s in parts)).total_seconds() >= wait
            complete = len(ordered) == first.concat_total
            if conflict:
                if expired:
                    for part in parts:
                        yield replace(part, body=(
                            f"[Unassembled SMS: conflicting parts; "
                            f"part {part.concat_sequence}/{part.concat_total}]\n{part.body}"
                        )), [part.id]
            elif complete or expired:
                body = "".join(ordered[n].body for n in sorted(ordered))
                if not complete:
                    missing = sorted(set(range(1, first.concat_total + 1)) - ordered.keys())
                    body = f"[Incomplete SMS; missing parts: {missing}]\n" + "\n".join(
                        f"[{n}/{first.concat_total}] {ordered[n].body}" for n in sorted(ordered)
                    )
                yield replace(first, body=body), [p.id for p in parts]


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
            f"Sender: {sms.sender}\n"
            f"Recipient module: {sms.module_id}\n"
            f"Received at: {sms.received_at}\n\n"
            f"{sms.body}\n",
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
        self.mark_many_sent([sms_id], message_id)

    def mark_many_sent(self, sms_ids: list[int], message_id: str) -> None:
        with self.state:
            self.state.executemany(
                "INSERT OR IGNORE INTO sent_sms (sms_id, sent_at, message_id) "
                "VALUES (?, ?, ?)",
                [(sms_id, datetime.now(UTC).isoformat(), message_id) for sms_id in sms_ids],
            )

    def poll_once(self) -> tuple[int, int]:
        sent = failed = 0
        last_id = 0
        pending = []
        with closing(self.open_source()) as source:
            columns = {row[1] for row in source.execute("PRAGMA table_info(sms)")}
            metadata = ("concat_ref", "concat_total", "concat_sequence")
            extra = ", " + ", ".join(metadata) if set(metadata) <= columns else ", NULL, NULL, NULL"
            extra += ", COALESCE(transport, '')" if "transport" in columns else ", ''"
            while not self.stopping:
                rows = source.execute(
                    "SELECT id, module_id, sender, body, received_at" + extra + " FROM sms "
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
                    pending.append(sms)
                if len(rows) < self.config.batch_size:
                    break
            for sms, sms_ids in assemble_messages(
                pending, datetime.now(UTC), self.config.multipart_wait_seconds
            ):
                if self.stopping:
                    break
                try:
                    message_id = self.send(sms)
                    self.mark_many_sent(sms_ids, message_id)
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
