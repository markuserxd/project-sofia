"""Fixed application-owned styles; no conversation text is interpreted as CSS."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication


def stylesheet(theme: str) -> str:
    dark = theme == "Dark" or (
        theme == "System"
        and QGuiApplication.styleHints().colorScheme() is Qt.ColorScheme.Dark
    )
    bg, panel, text, muted, border, bubble = (
        ("#101724", "#192335", "#edf2fa", "#b5c4d9", "#3e506b", "#27364e")
        if dark
        else ("#f2f5fa", "#ffffff", "#192335", "#485b73", "#b8c5d6", "#e2eaf6")
    )
    return f"""
        QWidget {{ background: {bg}; color: {text}; }}
        QMainWindow {{ background: {bg}; }}
        QFrame#panel, QScrollArea, QPlainTextEdit, QListWidget {{
            background: {panel}; border: 1px solid {border}; border-radius: 12px;
        }}
        QPlainTextEdit {{ padding: 10px; selection-background-color: #526fa3; }}
        QLabel {{ background: transparent; }}
        QLabel#heading {{ font-size: 22px; font-weight: 600; }}
        QLabel#muted {{ color: {muted}; }}
        QFrame#assistant {{ background: {panel}; border-radius: 12px; }}
        QFrame#user {{ background: {bubble}; border-radius: 12px; }}
        QPushButton {{ background: {bubble}; border: 1px solid {border};
            padding: 10px 14px; border-radius: 8px; }}
        QPushButton:hover {{ border-color: #8870d7; }}
        QPushButton:focus {{ border: 2px solid #8870d7; }}
        QPushButton:disabled {{ color: {muted}; background: {panel}; }}
        QPushButton#primary {{ background: #6650ac; color: #ffffff; }}
        QPushButton#emergency {{ background: #9e3444; color: #ffffff; font-weight: 600; }}
        QComboBox, QSpinBox {{ padding: 8px; border: 1px solid {border}; }}
        QListWidget::item {{ padding: 8px; }}
        QListWidget::item:selected {{ background: {bubble}; }}
    """
