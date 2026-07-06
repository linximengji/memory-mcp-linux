"""Shared path/URL configuration for memory-mcp.
Reads MM_WORKSPACE_ROOT from environment; falls back to Windows default.
Set MM_WORKSPACE_ROOT=/home/ubuntu/projects on tx-server.
"""
import os
from pathlib import Path

WORKSPACE_ROOT = Path(os.environ.get("MM_WORKSPACE_ROOT", Path.home() / "projects"))
MEMORY_ROOT = WORKSPACE_ROOT / ".claude" / "memory"
DATA_ROOT = WORKSPACE_ROOT / ".claude" / "code-graph"
PROXY_URL = os.environ.get("MM_PROXY_URL", "http://localhost:4000/v1/messages")
LLM_MODEL = os.environ.get("MM_LLM_MODEL", "deepseek-v4-flash")
