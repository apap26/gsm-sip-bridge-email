#!/usr/bin/env python3
"""Validate runtime configuration and dependencies without sending email."""

from __future__ import annotations

import json
import smtplib
import sqlite3
import ssl
import sys
from contextlib import closing

from forwarder import Config, Forwarder


def check_smtp(config: Config) -> None:
    smtp_class = smtplib.SMTP_SSL if config.smtp_ssl else smtplib.SMTP
    context = ssl.create_default_context()
    kwargs: dict[str, object] = {
        "host": config.smtp_host,
        "port": config.smtp_port,
        "timeout": config.smtp_timeout_seconds,
    }
    if config.smtp_ssl:
        kwargs["context"] = context
    with smtp_class(**kwargs) as client:
        if config.smtp_starttls:
            client.starttls(context=context)
        if config.smtp_username:
            client.login(config.smtp_username, config.smtp_password or "")
        status, _message = client.noop()
        if status != 250:
            raise RuntimeError(f"SMTP NOOP returned status {status}")


def main() -> int:
    forwarder: Forwarder | None = None
    try:
        config = Config.from_env()
        forwarder = Forwarder(config)
        with closing(forwarder.open_source()) as source:
            sms_count = source.execute("SELECT COUNT(*) FROM sms").fetchone()[0]
            if source.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise RuntimeError("source database is not query-only")
        check_smtp(config)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "source_database": "read-only",
                    "sms_count": sms_count,
                    "state_database": "writable",
                    "smtp": "connected and authenticated",
                }
            )
        )
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error, smtplib.SMTPException) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": f"{type(error).__name__}: {error}",
                }
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if forwarder is not None:
            forwarder.close()


if __name__ == "__main__":
    raise SystemExit(main())
