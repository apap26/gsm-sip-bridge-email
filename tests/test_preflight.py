import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from forwarder import Config
from preflight import check_smtp


class FakeSmtp:
    mail_status = 250

    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def starttls(self, **_kwargs):
        return None

    def login(self, *_args):
        return None

    def noop(self):
        return 250, b"OK"

    def mail(self, _sender):
        return self.mail_status, b"sender response"

    def rset(self):
        return 250, b"OK"


class PreflightTest(unittest.TestCase):
    def setUp(self):
        FakeSmtp.mail_status = 250

    def config(self, root: Path) -> Config:
        return Config(
            sms_db_path=str(root / "store.db"),
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

    @patch("preflight.smtplib.SMTP", FakeSmtp)
    def test_smtp_tls_login_and_noop(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            check_smtp(self.config(root))

    @patch("preflight.smtplib.SMTP", FakeSmtp)
    def test_rejects_disallowed_sender(self):
        FakeSmtp.mail_status = 501
        with TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(RuntimeError, "rejected SMTP_FROM"):
                check_smtp(self.config(Path(temporary_directory)))


if __name__ == "__main__":
    unittest.main()
