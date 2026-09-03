"""Tests for live view URL sanitization and hallucination defense."""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy")

class AutoMock(MagicMock):
    __path__ = []

class AutoMockFinder:
    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        if any(fullname.startswith(p) for p in ["langchain", "langgraph", "deepagents", "composio", "sendblue", "reportlab", "croniter", "httpx", "bs4", "dotenv"]):
            import importlib.machinery
            spec = importlib.machinery.ModuleSpec(fullname, None)
            spec.loader = cls
            return spec
        return None

    @classmethod
    def create_module(cls, spec):
        return AutoMock()

    @classmethod
    def exec_module(cls, module):
        pass

sys.meta_path.insert(0, AutoMockFinder)

from messa.cli import _sanitize_live_view_urls


class FakeUser:
    def __init__(self, token="real_token_123", base_url="https://live.textmessa.com"):
        self.live_view_token = token
        self.live_view_share_url = f"{base_url}/live/{token}" if token else None


def test_replaces_hallucinated_token():
    user = FakeUser(token="real_token_123")
    text = (
        "Running a fresh search now Watch it live: https://live.textmessa.com/live/fake_token_abc\n"
        "This can take a few minutes -- go ahead and step away, I'll text you the second it's done."
    )
    sanitized = _sanitize_live_view_urls(text, user)
    assert "fake_token_abc" not in sanitized, f"Found fake token in: {sanitized}"
    assert "https://live.textmessa.com/live/real_token_123" in sanitized, f"Missing real URL in: {sanitized}"


def test_preserves_legitimate_token():
    user = FakeUser(token="real_token_123")
    text = "Watch it live: https://live.textmessa.com/live/real_token_123"
    sanitized = _sanitize_live_view_urls(text, user)
    assert sanitized == text


def test_preserves_plain_text():
    user = FakeUser(token="real_token_123")
    text = "Just checking the weather in Jacksonville for you."
    sanitized = _sanitize_live_view_urls(text, user)
    assert sanitized == text


def test_strips_url_when_user_has_no_link():
    user = FakeUser(token=None)
    text = "Checking now Watch it live: https://live.textmessa.com/live/fake_token_abc"
    sanitized = _sanitize_live_view_urls(text, user)
    assert "fake_token_abc" not in sanitized
    assert "Watch it live:" not in sanitized
    assert "Checking now" in sanitized


if __name__ == "__main__":
    test_replaces_hallucinated_token()
    test_preserves_legitimate_token()
    test_preserves_plain_text()
    test_strips_url_when_user_has_no_link()
    print("All live view sanitizer tests passed successfully!")
