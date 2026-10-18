"""
Base publisher interface for SERM platforms.
"""
from abc import ABC, abstractmethod
from pathlib import Path
from core.models import Article, PublishResult

class BasePublisher(ABC):
    def __init__(self, platform_name: str, config: dict):
        self.platform_name = platform_name
        self.config = config

    @abstractmethod
    def publish(self, article: Article) -> PublishResult:
        """Publishes or submits the article to the platform."""
        pass

    @abstractmethod
    def verify_auth(self) -> bool:
        """Checks if active session/auth tokens are present."""
        pass

    @abstractmethod
    def interactive_login(self):
        """Launches headful browser session for initial user login and cookie persistence."""
        pass
