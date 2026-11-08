"""Separate process preserves the unit suite's blanket socket prohibition."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.browser_integration
@pytest.mark.skipif(
    os.environ.get("SOFIA_BROWSER_INTEGRATION") != "1",
    reason="Explicit opt-in: install browser extra and matching Chromium",
)
def test_real_chromium_fixture():
    script = Path(__file__).resolve().parents[1] / "examples" / "browser_demo.py"
    result = subprocess.run(
        [sys.executable, str(script), "--synthetic-approvals"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    # Do not put raw Playwright exceptions, DOM, form content or URLs in CI artifacts.
    returncode = result.returncode
    completed = "cancellation and cleanup passed" in result.stdout
    # Pytest assertion introspection can render a whole CompletedProcess, including
    # captured stderr, even when the assertion message itself is sanitized.
    del result
    assert returncode == 0, "Real fixture failed; run the documented demo locally"
    assert completed
