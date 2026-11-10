"""Bounded immutable GUI snapshots and non-sensitive session preferences."""

from dataclasses import dataclass, field

from sofia.core import AgentResult, TaskStatus
from sofia.persona import ConversationLanguage, PersonaConfig
from sofia.security import BudgetSnapshot
from sofia.tts.models import TTSConfig
from sofia.ui.chat import ChatSettings

MAX_MESSAGE = 4096
MAX_MESSAGES = 80
MAX_TASKS = 20


@dataclass(frozen=True, slots=True)
class Preferences:
    theme: str = "Dark"
    font_size: int = 11
    timestamps: bool = True
    tts: TTSConfig = field(default_factory=TTSConfig)
    conversation_language: ConversationLanguage = "th"
    chat: ChatSettings = field(default_factory=ChatSettings)

    def __post_init__(self) -> None:
        if (
            type(self.theme) is not str
            or self.theme not in ("Dark", "Light", "System")
            or type(self.font_size) is not int
            or not 10 <= self.font_size <= 18
            or type(self.timestamps) is not bool
            or type(self.tts) is not TTSConfig
            or type(self.chat) is not ChatSettings
        ):
            raise ValueError("invalid_preferences")
        PersonaConfig(language=self.conversation_language)


@dataclass(frozen=True, slots=True)
class Progress:
    task_id: str
    status: TaskStatus
    budget: BudgetSnapshot


@dataclass(frozen=True, slots=True)
class ChatStatus:
    settings: ChatSettings
    budget: BudgetSnapshot


@dataclass(frozen=True, slots=True, repr=False)
class Outcome:
    task_id: str
    result: AgentResult | None = None
    reply: str = ""
    error: str = ""
    cancelled: bool = False
    chat: ChatStatus | None = None
