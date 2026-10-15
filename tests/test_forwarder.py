import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from forwarder import Config, Forwarder, Sms, assemble_messages


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
                "CREATE TABLE sms (id INTEGER PRIMARY KEY, module_id TEXT NOT NULL, "
                "sender TEXT NOT NULL, "
                "body TEXT NOT NULL, received_at TEXT NOT NULL, "
                "forwarding_status TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO sms VALUES (1, 'pcsc0', '+123', 'Привет мир', "
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
            self.assertIn("Recipient module: pcsc0", message.get_content())
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

    @patch("forwarder.smtplib.SMTP", FakeSmtp)
    def test_multipart_across_polls_restart_batches_and_smtp_retry(self):
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.source_path) as db:
            db.execute("DELETE FROM sms")
            for column in ("concat_ref", "concat_total", "concat_sequence"):
                db.execute(f"ALTER TABLE sms ADD COLUMN {column} INTEGER")
            db.execute("INSERT INTO sms VALUES (1,'pcsc0','+123','мир',?,'pending',7,2,2)", (now,))
        config = replace(self.config, batch_size=1)
        service = Forwarder(config)
        self.assertEqual(service.poll_once(), (0, 0))
        service.close()
        with sqlite3.connect(self.source_path) as db:
            db.execute("INSERT INTO sms VALUES (2,'pcsc0','+123','Привет ',?,'pending',7,2,1)", (now,))
        service = Forwarder(config)
        try:
            FakeSmtp.fail = True
            self.assertEqual(service.poll_once(), (0, 1))
            self.assertFalse(service.already_sent(1))
            self.assertFalse(service.already_sent(2))
            FakeSmtp.fail = False
            self.assertEqual(service.poll_once(), (1, 0))
            self.assertIn("Привет мир", FakeSmtp.messages[0].get_content())
            self.assertTrue(service.already_sent(1))
            self.assertTrue(service.already_sent(2))
            self.assertEqual(service.poll_once(), (0, 0))
        finally:
            service.close()


class AssemblyTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(UTC)
        self.first = Sms(1, "modem", "+123", "Hello ", self.now.isoformat(), 7, 2, 1)
        self.second = replace(self.first, id=2, body="world", concat_sequence=2)

    def assemble(self, parts, age=0):
        return list(assemble_messages(parts, self.now + timedelta(seconds=age), 180))

    def test_order_and_duplicate_segments(self):
        result = self.assemble([self.second, self.first, replace(self.first, id=3)])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0].body, "Hello world")
        self.assertEqual(set(result[0][1]), {1, 2, 3})

    def test_separates_sender_modem_reference_transport_and_total(self):
        for change in ({"sender": "+456"}, {"module_id": "other"},
                       {"concat_ref": 8}, {"transport": "other"}, {"concat_total": 3}):
            with self.subTest(change=change):
                self.assertEqual(self.assemble([self.first, replace(self.second, **change)]), [])

    def test_no_guessing_from_text_markers_or_arrival_time(self):
        parts = [replace(self.first, concat_ref=None, body="[1/2] Hello "),
                 replace(self.second, concat_ref=None, body="[2/2] world")]
        self.assertEqual(len(self.assemble(parts)), 2)

    def test_incomplete_timeout_is_labelled_not_silently_truncated(self):
        self.assertEqual(self.assemble([self.first]), [])
        result = self.assemble([self.first], age=181)
        self.assertIn("Incomplete SMS; missing parts: [2]", result[0][0].body)
        self.assertEqual(result[0][1], [1])

    def test_conflicting_parts_never_combine(self):
        parts = [self.first, replace(self.first, id=3, body="Other message"), self.second]
        self.assertEqual(self.assemble(parts), [])
        result = self.assemble(parts, age=181)
        self.assertEqual(len(result), 3)
        self.assertTrue(all("conflicting parts" in sms.body for sms, _ in result))

    def test_reference_reuse_outside_window_does_not_combine(self):
        second = replace(self.second, received_at=(self.now + timedelta(seconds=181)).isoformat())
        self.assertEqual(self.assemble([self.first, second]), [])

    def test_malformed_metadata_does_not_block_delivery(self):
        for change in ({"concat_sequence": 0}, {"concat_total": 256},
                       {"concat_ref": -1}, {"received_at": "bad"}):
            self.assertEqual(len(self.assemble([replace(self.first, **change)])), 1)


if __name__ == "__main__":
    unittest.main()
