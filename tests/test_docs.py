"""The docs say what the code does. Dated 2026-11-16, the user: "I don't want to have to do this
again", after a whole-codebase audit found CLAUDE.md and ARCHITECTURE.md months behind the code.
What a test can see is checked here: every command and flag is in ARCHITECTURE.md's command
reference, every module in CLAUDE.md's layout, every environment variable in CLAUDE.md. What it
can't (a default, a behaviour) is CLAUDE.md's rule: the commit that changes it updates the docs."""
import argparse
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workbench import cli  # noqa: E402

CLAUDE = (ROOT / "CLAUDE.md").read_text()
ARCH = (ROOT / "ARCHITECTURE.md").read_text()


class _Built(Exception):
    pass


def wb_parser() -> argparse.ArgumentParser:
    """The parser cli.main builds, caught before it parses anything."""
    def catch(self, *a, **k):
        raise _Built(self)
    real = argparse.ArgumentParser.parse_args
    argparse.ArgumentParser.parse_args = catch
    try:
        cli.main([])
    except _Built as e:
        return e.args[0]
    finally:
        argparse.ArgumentParser.parse_args = real
    raise AssertionError("cli.main never built its parser")


def commands(parser, path=()):
    """(the command's words, each option's spellings) for every command and subcommand."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                opts = [a.option_strings for a in sub._actions
                        if a.option_strings and "--help" not in a.option_strings]
                yield (*path, name), opts
                yield from commands(sub, (*path, name))


def section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:end if end > 0 else None]


class DocsMatchTheCode(unittest.TestCase):
    """CLAUDE.md and ARCHITECTURE.md name everything a reader looks up."""

    def test_every_command_and_flag_is_in_the_command_reference(self):
        ref = section(ARCH, "## Every `wb` command")
        missing = []
        for words, opts in commands(wb_parser()):
            rows = [line for line in ref.splitlines() if f"wb {words[0]}" in line]
            if not rows or not all(any(w in r for r in rows) for w in words[1:]):
                missing.append("wb " + " ".join(words))
                continue
            for spellings in opts:  # -m or --message: either will do
                if not any(re.search(rf"`[^`]*{re.escape(o)}\b", r) for r in rows for o in spellings):
                    missing.append(f"wb {' '.join(words)} {spellings[-1]}")
        self.assertEqual(missing, [], "add these to ARCHITECTURE.md's *Every `wb` command*")

    def test_the_quickstart_finds_the_marketplace_where_claude_code_keeps_it(self):
        # the 2026-11-19 audit: ~/.claude hard-coded, wrong for CLAUDE_CONFIG_DIR users
        for text in ((ROOT / "README.md").read_text(), ARCH):
            setup = [line for line in text.splitlines() if "marketplaces/workbench/bin/wb setup" in line]
            self.assertTrue(setup)
            for line in setup:
                self.assertIn('"${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/', line)

    def test_every_module_is_in_the_layout(self):
        layout = section(CLAUDE, "## Layout")
        missing = [p.name for p in sorted((ROOT / "workbench").glob("*.py"))
                   if p.name != "__init__.py" and f"`{p.name}`" not in layout]
        self.assertEqual(missing, [], "add these to CLAUDE.md's *Layout*")

    def test_every_environment_variable_is_in_claude_md(self):
        code = [*(ROOT / "workbench").glob("*.py"), ROOT / "mod/hooks/register.tsx"]
        names = {n for f in code for n in re.findall(r"\bWB_[A-Z_]+\b", f.read_text())}
        missing = sorted(n for n in names if f"`{n}`" not in CLAUDE)
        self.assertEqual(missing, [], "add these to CLAUDE.md's *State and config*")

    def test_the_readme_names_the_floors_the_code_holds(self):
        # #273 (the audit of 1.2): "OpenCode 2.x" while runner.OPENCODE_TESTED starts at 2.3, and
        # "16K at least" with OpenCode's own prompt about 10K of it
        from workbench import models, runner
        readme = (ROOT / "README.md").read_text()
        low = ".".join(map(str, runner.OPENCODE_TESTED[0]))
        self.assertIn(f"[OpenCode](https://opencode.ai) {low} or newer", readme)
        self.assertIn(f"{models.MIN_WINDOW // 1024}K at the least", readme)
        self.assertIn("32K context window recommended", readme)

    def test_the_panes_probe_row_names_the_line_count_the_code_holds(self):
        # the review of #857's off and on (2026-11-22): the pane's "On (large files only)" sentence
        # copies context.PROBE_FILE_LINES, which it can't import, so a change to one is caught here
        from workbench import context
        said = f"one function of a file over {context.PROBE_FILE_LINES:,} lines with probe"
        pane = (ROOT / "mod/hooks/register.tsx").read_text()
        stops = pane[pane.index("const PROBE_STOPS"):]
        self.assertIn(said, stops[:stops.index("]\n")], "the pane's PROBE_STOPS on sentence")
        self.assertIn(said, " ".join(ARCH.split()), "ARCHITECTURE.md's pane row")

    def test_wb_start_role_and_the_agents_switch_are_in_the_command_reference(self):
        # #1016: the role Claude picks per task, and the user's Agents switch
        ref = section(ARCH, "## Every `wb` command")
        start = next(line for line in ref.splitlines() if line.startswith("| `wb start "))
        self.assertIn("`--role significant\\|trivial\\|writing`", start)
        self.assertIn("`wb route all gpu\\|api\\|both`", ref)
        self.assertIn("--role", (ROOT / "skill/workbench/SKILL.md").read_text())

    def test_the_skills_first_run_sets_api_roles_in_one_route_and_never_picks_an_api_model(self):
        # #1024: the API-only first run is one question and one wb route with its cap; an API
        # model is the user's choice, never Claude's
        skill = (ROOT / "skill/workbench/SKILL.md").read_text()
        first = skill[skill.index("- **First run**"):skill.index("- **Claude coding**")]
        self.assertIn("wb route", first)
        self.assertIn("--day-usd", first)
        self.assertIn("no role can run", first)
        self.assertNotIn("no model can run yet", first)
        # how Claude sees that no role can run: wb start's exit 3 with the router's refusal (the
        # Couplings pair); `wb route` shows the roles, never whether one can run (#1024's review)
        self.assertIn("`no GPU model…`", first)
        self.assertIn('"no GPU model', (ROOT / "workbench/router.py").read_text())
        self.assertNotIn("`wb route` and `wb setup` say so", first)
        choosing = section(skill, "## 1. Pick the role; the user's roles pick the model")
        self.assertIn("never pass `--model <api id>`", " ".join(choosing.split()))

    def test_the_skill_lets_a_user_named_api_model_through_wb_starts_dialog(self):
        # #1132 (2026-11-23): "never pass `--model <api id>`" read as a ban even on the user's own
        # `/workbench … --model <id>` (the skill's argument-hint), the start cli.start_gate and the
        # mod's dialog were built for
        skill = (ROOT / "skill/workbench/SKILL.md").read_text()
        self.assertIn("[--model <id>]", skill[:skill.index("\n---", 4)])
        choosing = " ".join(section(skill, "## 1. Pick the role; the user's roles pick the model").split())
        self.assertIn("never pass `--model <api id>` unless the user named that id for this task (the mod's "
                      'dialog then asks them, and a "not changed" means no)', choosing)
        self.assertTrue(callable(cli.start_gate))
        self.assertIn("calls.push(['--start'", (ROOT / "mod/hooks/register.tsx").read_text())

    def test_the_skills_question_bullet_sends_a_files_text_never_a_path_outside_the_lane(self):
        # #1133 (2026-11-23): an answer naming ~/.claude/plans/x.md (hidden from the lane whole)
        # or another repo got the agent a denied read and another question, another wake
        skill = (ROOT / "skill/workbench/SKILL.md").read_text()
        bullet = skill[skill.index("- **question** —"):skill.index("- **stuck / failed** —")]
        bullet = " ".join(bullet.split())
        self.assertIn("never a path you name elsewhere (a plan under ~/.claude, another repo)", bullet)
        self.assertIn("`wb answer <name> --file <path>`, which sends the file's contents", bullet)
        self.assertIn("Path(a.file).read_text(", (ROOT / "workbench/cli.py").read_text())   # cmd_answer's

    def test_every_web_chat_api_path_is_in_the_api_reference(self):
        # #1042: docs/chat-api.md is the page another agent is handed; a path the server answers
        # that it doesn't name is one nobody can find
        doc = (ROOT / "docs/chat-api.md").read_text()
        paths = set(re.findall(r'"(/api/[a-z]+/?)', (ROOT / "workbench/chat.py").read_text()))
        self.assertGreater(len(paths), 10)
        missing = sorted(p for p in paths if p not in doc)
        self.assertEqual(missing, [], "add these to docs/chat-api.md")

    def test_claude_md_stays_under_claude_codes_size_warning(self):
        # Claude Code warns past 40,000 characters, and every session that changes the code
        # loads this file (local agents too, when workbench delegates work on itself)
        self.assertLess(len(CLAUDE), 25_000, "move detail to ARCHITECTURE.md")

    def test_every_flag_the_skill_tells_claude_to_pass_is_one_wb_takes(self):
        # the review of #1025 (2026-11-23): SKILL.md said `--role writing`, which only #1016's
        # parser takes, so every prose task started by the skill would have died at `wb start`
        # (exit 2). A `wb …` span or block line anywhere in the skill, and in SKILL.md a span
        # that is a bare flag (its flags are wb's); another program's (`mkdocs build --strict`)
        # is its own
        known = {o for _, opts in commands(wb_parser()) for spellings in opts for o in spellings}
        known.add("--help")
        skill = ROOT / "skill/workbench"
        unknown = []
        for path in [skill / "SKILL.md", *sorted((skill / "reference").glob("*.md"))]:
            for span in re.findall(r"```.*?```|`[^`\n]+`", path.read_text(), re.S):
                for line in span.strip("`").splitlines():
                    first = (line.split() or [""])[0].lstrip("[")
                    if first == "wb" or (first.startswith("--") and path.name == "SKILL.md"):
                        unknown += [f"{path.name}: {f}" for f in
                                    re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", line) if f not in known]
        self.assertEqual(unknown, [], "the skill names flags wb doesn't parse")

    def test_the_writing_handbook_stays_short_and_names_no_model(self):
        # every writing prompt carries it, and its size is prefill time (#1025); models come from
        # OpenCode's config, never the code or what the agent reads (CLAUDE.md, Conventions)
        text = (ROOT / "agent/WRITING.md").read_bytes()
        self.assertLess(len(text), 3_000, "agent/WRITING.md: keep it short")
        families = r"\b(qwen|llama|gemma|mistral|devstral|deepseek|gpt|phi|mimo|glm|kimi|grok|gemini|" \
                   r"haiku|sonnet|opus|codestral|granite|nemotron|minimax)\w*"
        self.assertIsNone(re.search(families, text.decode(), re.I))


class DocsFor20(unittest.TestCase):
    """#1027: since 2.0 an agent is a GPU model or an API model, and what people and Claude read
    says so; an API-only user read "A local model" as "not for me"."""

    def test_the_readme_pitch_names_the_api_and_the_gpu(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("**Claude orchestrates. OpenCode agents via API or GPU write the code.**", readme)
        self.assertIn("**Claude is the lead, your OpenCode agents are the crew:**", readme)
        self.assertIn("- A model: one you connected in OpenCode (`/connect`), or a local one.", readme)
        self.assertNotIn("local LLM", readme)

    def test_the_plugin_description_says_opencode_agents_not_local_ones(self):
        import json
        said = json.loads((ROOT / "mod/.claude-plugin/plugin.json").read_text())["description"]
        self.assertIn("tasks Claude delegates to OpenCode agents (wb)", said)
        self.assertNotIn("local", said)

    def test_the_mods_claude_facing_strings_never_call_the_agent_local(self):
        # the weekly model check's SCOUT_NOTE is the first-run issue's (#1024) and the image and
        # audio texts stay local; comments aren't said to Claude
        pane = (ROOT / "mod/hooks/register.tsx").read_text()
        scout = pane.index("const SCOUT_NOTE")
        pane = pane[:scout] + pane[pane.index("\nconst ", scout + 1):]
        said = "\n".join(line for line in pane.splitlines() if not line.lstrip().startswith("//"))
        for words in ("local agent", "work stays local"):
            self.assertNotIn(words, said)
        # "stays local" lost only its last word: the ask rule still says where the work stays, as
        # wb takeover's refusal does (cli._coding_gate)
        self.assertIn("Unanswered, the work stays with the agent.", said)

    def test_wb_coding_and_the_pane_say_the_same_sentence_for_each_choice(self):
        # *Couplings*: cli.CODING → the mod's CODING_STOPS (#1027 changed both in one word)
        stops = (ROOT / "mod/hooks/register.tsx").read_text()
        stops = stops[stops.index("const CODING_STOPS"):]
        stops = stops[:stops.index("]\n")]
        for mode, (_, sentence) in cli.CODING.items():
            self.assertIn(f"'{sentence}'", stops, mode)
            self.assertNotIn("local", sentence, mode)

    def test_architecture_md_defines_the_agent_as_a_gpu_or_an_api_model(self):
        opening = " ".join(ARCH[:ARCH.index("Contents:")].split())
        self.assertIn('"the agent" is the model running in OpenCode on one task', opening)
        self.assertIn("or an API model", opening)
        pieces = section(ARCH, "## The pieces")
        self.assertIn("an API task: no lease, its key in the keyhole", pieces)

    def test_claude_md_says_an_unset_role_is_the_gpu_and_the_key_stays_outside_the_lane(self):
        runs = " ".join(section(CLAUDE, "## How a task runs").split())
        self.assertIn("unset: GPU", runs)
        self.assertIn("the key stays there, outside the lane", runs)
        self.assertNotIn("local models in OpenCode", CLAUDE)

    def test_files_and_formats_says_a_task_from_before_roles_reads_as_gpu(self):
        # #1027's edge case: a task.json from before 2.0 has no `via`, and every reader takes it
        # as GPU (bench, runner, cli, chat: `!= "api"` or `or "gpu"`)
        keys = " ".join(section(ARCH, "## Files and formats").split())
        keys = keys[keys.index("**task.json**"):]
        self.assertIn("a task from before it has neither, and reads as GPU", keys[:keys.index("`size`")])


if __name__ == "__main__":
    unittest.main()
