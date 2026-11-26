#!/usr/bin/env python3
"""Moved to ../../ask-workbench/scripts/ask_workbench.py (#1366, workbench 2.2): the agents' skill
is ask-workbench now, as either driver (Claude Code or OpenCode) answers. This path stays so an
install made before 2.2 still runs: it runs the new script, with the same arguments and exit
status."""
import runpy
import sys
from pathlib import Path

NEW = Path(__file__).resolve().parents[2] / "ask-workbench" / "scripts" / "ask_workbench.py"

if __name__ == "__main__":
    if not NEW.is_file():  # this directory copied alone: the new one isn't beside it
        sys.exit("ask_claude.py moved: install skill/agents/ask-workbench from the workbench "
                 "repository (its scripts/ask_workbench.py)")
    sys.argv[0] = str(NEW)
    runpy.run_path(str(NEW), run_name="__main__")
