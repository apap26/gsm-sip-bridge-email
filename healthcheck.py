#!/usr/bin/env python3
"""Return success while the forwarder's polling loop is alive."""

import os
import sys
import time
from pathlib import Path


health_file = Path(os.getenv("HEALTH_FILE", "/tmp/healthy"))
poll_interval = float(os.getenv("POLL_INTERVAL_SECONDS", "30"))
max_age = max(60.0, poll_interval * 3.0)

try:
    age = time.time() - health_file.stat().st_mtime
except OSError:
    sys.exit(1)
sys.exit(0 if age <= max_age else 1)
