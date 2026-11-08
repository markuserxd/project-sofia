"""Opt-in speech output. Importing this package opens no device or Qt engine."""

from sofia.tts.engine import TTSEngine
from sofia.tts.models import Capabilities, TTSConfig, TTSError, TTSRequest, Voice

__all__ = ["Capabilities", "TTSConfig", "TTSEngine", "TTSError", "TTSRequest", "Voice"]
