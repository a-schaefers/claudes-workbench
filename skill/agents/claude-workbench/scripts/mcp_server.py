#!/usr/bin/env python3
"""Moved to ../../ask-workbench/scripts/mcp_server.py (#1366, workbench 2.2): the agents' skill is
ask-workbench now, as either driver (Claude Code or OpenCode) answers. This path stays so an MCP
config written before 2.2 still starts the server: it runs the new one, whose request tool is
ask_workbench (ask_claude still reaches it)."""
import runpy
import sys
from pathlib import Path

NEW = Path(__file__).resolve().parents[2] / "ask-workbench" / "scripts" / "mcp_server.py"

if __name__ == "__main__":
    if not NEW.is_file():  # this directory copied alone: the new one isn't beside it
        sys.exit("mcp_server.py moved: install skill/agents/ask-workbench from the workbench "
                 "repository (its scripts/mcp_server.py)")
    sys.argv[0] = str(NEW)
    runpy.run_path(str(NEW), run_name="__main__")
