import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from forwarder import Config, Forwarder


class FakeSmtp:
    messages = []
    fail = False

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def starttls(self, **_kwargs):
        return None

    def login(self, *_args):
        return None

    def send_message(self, message):
        if self.fail:
            raise OSError("SMTP unavailable")
        self.messages.append(BytesParser(policy=policy.default).parsebytes(message.as_bytes()))
        return {}


class ForwarderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source_path = root / "store.db"
        with sqlite3.connect(self.source_path) as db:
            db.execute(
                "CREATE TABLE sms (id INTEGER PRIMARY KEY, sender TEXT NOT NULL, "
                "body TEXT NOT NULL, received_at TEXT NOT NULL, "
                "forwarding_status TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO sms VALUES (1, '+123', 'Привет мир', "
                "'2026-09-10T10:00:00Z', 'pending')"
            )
        self.config = Config(
            sms_db_path=str(self.source_path),
            state_db_path=str(root / "state.db"),
            health_file=str(root / "healthy"),
            smtp_host="smtp.example.test",
            smtp_port=587,
            smtp_username="user",
            smtp_password="secret",
            smtp_from="sms@example.test",
            smtp_to=("recipient@example.test",),
            smtp_starttls=True,
            smtp_ssl=False,
            smtp_timeout_seconds=1,
            poll_interval_seconds=1,
            batch_size=100,
            db_busy_timeout_seconds=1,
            log_level="INFO",
        )
        FakeSmtp.messages = []
        FakeSmtp.fail = False

    def tearDown(self):
        self.temp.cleanup()

    @patch("forwarder.smtplib.SMTP", FakeSmtp)
    def test_delivers_unicode_and_deduplicates(self):
        service = Forwarder(self.config)
        try:
            self.assertEqual(service.poll_once(), (1, 0))
            self.assertEqual(service.poll_once(), (0, 0))
            self.assertEqual(len(FakeSmtp.messages), 1)
            message = FakeSmtp.messages[0]
            self.assertEqual(message["Subject"], "SMS from +123")
            self.assertIn("Привет мир", message.get_content())
            self.assertTrue(Path(self.config.health_file).exists())
        finally:
            service.close()

    @patch("forwarder.smtplib.SMTP", FakeSmtp)
    def test_failed_delivery_is_retried(self):
        service = Forwarder(self.config)
        try:
            FakeSmtp.fail = True
            self.assertEqual(service.poll_once(), (0, 1))
            FakeSmtp.fail = False
            self.assertEqual(service.poll_once(), (1, 0))
        finally:
            service.close()

    def test_source_connection_is_read_only(self):
        service = Forwarder(self.config)
        try:
            with closing(service.open_source()) as source:
                with self.assertRaises(sqlite3.OperationalError):
                    source.execute("UPDATE sms SET body = 'changed' WHERE id = 1")
        finally:
            service.close()

    def test_invalid_security_combination(self):
        with patch.dict(
            os.environ,
            {
                "SMTP_HOST": "smtp.example.test",
                "SMTP_FROM": "sms@example.test",
                "SMTP_TO": "recipient@example.test",
                "SMTP_STARTTLS": "true",
                "SMTP_SSL": "true",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "cannot both"):
                Config.from_env()


if __name__ == "__main__":
    unittest.main()
