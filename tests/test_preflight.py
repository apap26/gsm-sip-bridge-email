import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from forwarder import Config
from preflight import check_smtp


class FakeSmtp:
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


class PreflightTest(unittest.TestCase):
    @patch("preflight.smtplib.SMTP", FakeSmtp)
    def test_smtp_tls_login_and_noop(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = Config(
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
            check_smtp(config)


if __name__ == "__main__":
    unittest.main()
