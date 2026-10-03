"""Keep tests away from the real machine agents space and active-release pointer (~/.ihav)."""

import os
import tempfile

os.environ.setdefault("IHAV_HOME", tempfile.mkdtemp(prefix="ihav home tests "))
