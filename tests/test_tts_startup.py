"""Headless speech contracts must not import optional audio/UI dependencies."""

import subprocess
import sys


def test_headless_tts_import_and_cli_remain_audio_free():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import sofia.tts; import sofia.cli; assert not any(n.startswith(('PySide6', 'numpy', 'sofia.tts.native')) for n in sys.modules)",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
