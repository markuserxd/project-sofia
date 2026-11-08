"""Optional imports preserve the headless package/CLI contract."""

import subprocess
import sys


def test_speech_and_ui_imports_do_not_initialize_optional_backends():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import sofia, sofia.ui, sofia.speech; "
            "assert 'PySide6.QtMultimedia' not in sys.modules; "
            "assert 'faster_whisper' not in sys.modules; "
            "assert 'onnxruntime' not in sys.modules; "
            "assert 'numpy' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
