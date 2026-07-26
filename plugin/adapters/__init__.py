"""Adapter registry. To support a new CLI, add its module and one line here."""

from adapters.codex_app_server import CodexAppServerAdapter
from adapters.codex import CodexAdapter
from adapters.claude import ClaudeAdapter
from adapters.grok import GrokAdapter

# "codex" is Level-2 (native app-server); "codex-exec" is the Level-1 fallback.
_ADAPTERS = [CodexAppServerAdapter(), CodexAdapter(), ClaudeAdapter(), GrokAdapter()]
REGISTRY = {a.name: a for a in _ADAPTERS}


def get(name):
    return REGISTRY.get(name)


def names():
    return list(REGISTRY)
