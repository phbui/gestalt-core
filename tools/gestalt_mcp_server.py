"""Importable alias for ``gestalt-mcp-server.py``.

The server lives in a hyphenated file because the MCP registration in ``~/.claude.json``
executes it as a script (``python3 .../gestalt-mcp-server.py``), and renaming it would
break that registration. But hyphens are not legal in Python module names, so

    from gestalt_mcp_server import gestalt_search

could never resolve — and five call sites use exactly that line:

    .claude/hooks/prompt-intelligence.py        (the gestalt_search leg of per-prompt injection)
    .claude/hooks/gestalt-session-start.sh      (CWD hint, and embedding-model warmup)
    .claude/hooks/post-compact-reinject.sh      (post-compact task hint)
    tools/auto-promote.py                       (promotion-target fallback)

Every one of them wraps the import in ``try/except``, so the failure was silent: those
features degraded to no-ops rather than erroring. This module closes that gap by loading
the hyphenated file under a legal name and re-exporting its public helpers.

Keep the hyphenated file as the executable entry point; import through this shim.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SERVER_PATH = Path(__file__).resolve().parent / "gestalt-mcp-server.py"

if not _SERVER_PATH.exists():  # pragma: no cover - defensive
    raise ImportError(f"gestalt MCP server not found at {_SERVER_PATH}")

_spec = importlib.util.spec_from_file_location("_gestalt_mcp_server_impl", _SERVER_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover - defensive
    raise ImportError(f"could not load spec for {_SERVER_PATH}")

_impl = importlib.util.module_from_spec(_spec)
# Register before exec so the module is importable from inside itself if it ever needs to be.
sys.modules.setdefault("_gestalt_mcp_server_impl", _impl)
_spec.loader.exec_module(_impl)

# Public surface. The read/manifest/graph helpers are registered inside build_mcp()
# and stay there.
gestalt_search = _impl.gestalt_search
# Lexical-only search for latency-bound callers (hooks). Importing this name does NOT
# pull in torch or sentence-transformers — that is the entire reason it exists.
gestalt_search_fts = _impl.gestalt_search_fts
# Skill routing over indexed SKILL.md bodies. Also lexical-only and torch-free.
gestalt_route = _impl.gestalt_route
build_mcp = _impl.build_mcp
get_db = _impl.get_db
get_fts_db = _impl.get_fts_db
get_model = _impl.get_model

__all__ = [
    "build_mcp",
    "gestalt_route",
    "gestalt_search",
    "gestalt_search_fts",
    "get_db",
    "get_fts_db",
    "get_model",
]
