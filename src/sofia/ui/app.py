"""Lazy optional-dependency loading and explicit offline application startup."""

import sys


def main() -> int:
    try:
        from PySide6.QtWidgets import QApplication

        from sofia.ui.main_window import MainWindow
    except ModuleNotFoundError as error:
        if error.name and error.name.split(".")[0] in ("PySide6", "shiboken6"):
            print('SOFIA GUI requires: python -m pip install ".[ui]"', file=sys.stderr)
            return 2
        raise

    from sofia.config import ConfigurationError, load_settings
    from sofia.logging_setup import configure_logging

    try:
        settings = load_settings()
    except ConfigurationError:
        print(
            "SOFIA GUI requires valid settings and SOFIA_PROVIDER=mock.",
            file=sys.stderr,
        )
        return 2
    configure_logging(settings)
    app = QApplication(sys.argv)
    app.setApplicationName("SOFIA")
    app.setOrganizationName("Project SOFIA")
    window = MainWindow()
    window.show()
    return app.exec()
