"""The docs say what the code does. Dated 2026-11-16, the user: "I don't want to have to do this
again", after a whole-codebase audit found CLAUDE.md and ARCHITECTURE.md months behind the code.
What a test can see is checked here: every command and flag is in ARCHITECTURE.md's command
reference, every module in CLAUDE.md's layout, every environment variable in CLAUDE.md. What it
can't (a default, a behaviour) is CLAUDE.md's rule: the commit that changes it updates the docs."""
import argparse
import ast
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workbench import cli, context, runner  # noqa: E402

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


# A key, then ": value" or nothing (a block follows): a key is plain or double-quoted, never ":"
_KEY = re.compile(r'("(?:[^"\\]|\\.)*"|[^\s"\[{#-][^:]*?|-[^\s:][^:]*?):(?: (.*))?')


def read_yaml(text: str):
    """docs/chat-api.yaml as data, without PyYAML (standard library only): the subset its header
    names, block mappings and lists, one-line scalars, flow lists, {} and folded ">-" blocks. A
    line outside it fails here rather than reading as something else (#1138)."""
    lines = [(len(raw) - len(raw.lstrip(" ")), raw.strip()) for raw in text.splitlines()
             if raw.strip() and not raw.strip().startswith("#")]

    def scalar(v: str):
        if v.startswith('"'):
            return json.loads(v)
        if v.startswith("["):
            items = re.findall(r'"(?:[^"\\]|\\.)*"|[^,\s][^,]*', v[1:-1])
            return [scalar(x.strip()) for x in items]
        if v.startswith(("{", "'", "|", "&", "*", "!")) and v != "{}":
            raise ValueError(f"outside the subset: {v}")
        return ({} if v == "{}" else {"true": True, "false": False, "null": None}.get(v, v)
                if not re.fullmatch(r"-?\d+", v) else int(v))

    def block(i: int, ind: int):
        if lines[i][1].startswith("- "):
            out = []
            while i < len(lines) and lines[i][0] == ind and lines[i][1].startswith("- "):
                rest = lines[i][1][2:]
                if _KEY.fullmatch(rest):  # "- key: value": a mapping whose keys sit 2 further in
                    lines[i] = (ind + 2, rest)
                    v, i = block(i, ind + 2)
                else:
                    v, i = scalar(rest), i + 1
                out.append(v)
            return out, i
        out = {}
        while i < len(lines) and lines[i][0] == ind:
            m = _KEY.fullmatch(lines[i][1])
            if not m:
                raise ValueError(f"outside the subset: {lines[i][1]}")
            key, v = json.loads(m[1]) if m[1].startswith('"') else m[1], m[2]
            if key in out:
                raise ValueError(f"{key} twice")
            i += 1
            if v == ">-":
                parts = []
                while i < len(lines) and lines[i][0] > ind:
                    parts.append(lines[i][1])
                    i += 1
                out[key] = " ".join(parts)
            elif v is None:
                out[key], i = block(i, lines[i][0])
            else:
                out[key] = scalar(v)
        if i < len(lines) and lines[i][0] > ind:
            raise ValueError(f"indented out of place: {lines[i][1]}")
        return out, i

    got, end = block(0, 0)
    if end != len(lines):
        raise ValueError(f"left over: {lines[end][1]}")
    return got


def v1(route: str) -> str:
    """chat.ROUTES's /api/<x> as the OpenAPI file names it: /api/v1/<x>, an id-taking one {id}."""
    return "/api/v1/" + route[len("/api/"):] + ("{id}" if route.endswith("/") else "")


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
        # the 2026-11-19 audit: ~/.claude hard-coded, wrong for CLAUDE_CONFIG_DIR users. Since
        # #1190 the quickstart has no such line (the mod runs wb setup): only where one appears
        for text in ((ROOT / "README.md").read_text(), ARCH):
            for line in text.splitlines():
                if "marketplaces/workbench/bin/wb setup" in line:
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

    def test_the_pane_keeps_no_copy_of_a_setting_wb_owns(self):
        # the review of #857's off and on (2026-11-22) caught the pane's probe sentence copying
        # context.PROBE_FILE_LINES; since #1283 (the owner, 2026-11-24) the settings are the web
        # app's and the pane is Tasks only, so it keeps no copy of a setting's words to fall behind
        pane = (ROOT / "mod/hooks/register.tsx").read_text()
        for copy in ("const PROBE_STOPS", "const CODING_STOPS", "const SETTING_ROWS", "const TABS"):
            self.assertNotIn(copy, pane)

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
        # the dry run the dialog reads: the kernel's gate `start` builds its arguments (#1325)
        self.assertIn('{ gate: "start", args: [ "--start" ]', (ROOT / "kernel/src/Kernel/Gate.purs").read_text())
        self.assertIn("g.gate !== 'start'", (ROOT / "mod/hooks/register.tsx").read_text())

    def test_the_skill_changes_a_setting_only_on_the_users_clear_ask_and_yes(self):
        # #1287 (the owner, 2026-11-24: "it should just require a clear ask and yes from the
        # user"; "We trust Claude"): the rule wb settings set runs by, in the terminal or the web chat
        skill = " ".join((ROOT / "skill/workbench/SKILL.md").read_text().split())
        for said in ("explain it from `wb settings`",
                     "Change one only when they have clearly asked for that change and said yes: `wb settings set "
                     "<key> <value>`", "One yes covers the change asked for and nothing more.",
                     "Never change a setting unasked, and after a no don't ask again.",
                     "installing nothing without their yes", "A role moved to API still goes through the mod's dialog",
                     "The same holds from the web chat: answer there and run the same command."):
            self.assertIn(said, skill)
        # the dry run the dialog reads: the kernel's gate `settings` builds its arguments (#1325)
        self.assertIn('{ gate: "settings", args: [ "--settings" ]', (ROOT / "kernel/src/Kernel/Gate.purs").read_text())
        self.assertIn("g.gate !== 'settings'", (ROOT / "mod/hooks/register.tsx").read_text())

    def test_the_skills_question_bullet_sends_a_files_text_never_a_path_outside_the_lane(self):
        # #1133 (2026-11-23): an answer naming ~/.claude/plans/x.md (hidden from the lane whole)
        # or another repo got the agent a denied read and another question, another wake
        skill = (ROOT / "skill/workbench/SKILL.md").read_text()
        bullet = skill[skill.index("- **question** —"):skill.index("- **stuck / failed** —")]
        bullet = " ".join(bullet.split())
        self.assertIn("never a path you name elsewhere (a plan under ~/.claude, another repo)", bullet)
        self.assertIn("`wb answer <name> --file <path>`, which sends the file's contents", bullet)
        self.assertIn("Path(a.file).read_text(", (ROOT / "workbench/cli.py").read_text())   # cmd_answer's

    def test_the_skills_deep_workflows_say_what_ultracode_with_deepcode_does_and_fix_through_fix(self):
        # the review of #1278 (2026-11-25): the issue says that with both words in one prompt
        # Claude follows each in its own scope, and its plan's §6 that Claude's Workflow then only
        # reviews and that findings are fixed through the `fix` flow; the skill said neither
        skill = (ROOT / "skill/workbench/SKILL.md").read_text()
        deep = " ".join(section(skill, "### Deep workflows").split())
        self.assertIn("With `ultracode` and `deepcode` in one prompt, follow each in its own scope", deep)
        self.assertIn("your own Workflow (ultracode's) only reviews what the run returns", deep)
        self.assertIn("fixed through the `fix` flow", deep)
        self.assertTrue((ROOT / "agent/flows/fix.json").is_file())

    def test_the_macos_ollama_install_puts_the_window_in_the_model_not_a_launchctl_setenv(self):
        # #1195's review (2026-11-24): `launchctl setenv OLLAMA_CONTEXT_LENGTH` is lost at logout or
        # reboot; Ollama then served its 4K default and short_window failed every GPU task
        ref = " ".join((ROOT / "skill/workbench/reference/models.md").read_text().split())
        mac = ref[ref.index("- Ollama on macOS"):ref.index("- Ollama on Linux")]
        self.assertIn("Never `launchctl setenv OLLAMA_CONTEXT_LENGTH`", mac)
        self.assertNotIn("&& launchctl setenv", mac)
        self.assertIn('"parameters":{"num_ctx":<N>}', mac)
        self.assertIn("wb model add ollama/<family>:<tag>-<K>k", mac)
        self.assertIn("a `launchctl setenv` is lost at logout or reboot", " ".join(ARCH.split()))

    def test_the_backlog_promises_start_order_for_the_gpu_only_not_for_api_slots(self):
        # #1130 (2026-11-23): driving.md promised start order and sent-back-first, which only
        # GpuLease's `since` keeps; NoLease's waiters take a free slot in no set order
        from workbench import models
        driving = " ".join((ROOT / "skill/workbench/reference/driving.md").read_text().split())
        self.assertIn(f"an API provider's slot ({models.API_PARALLEL} tasks at once) goes to whichever "
                      "waiting task looks first, in no set order", driving)
        self.assertNotIn("since", models.NoLease.__init__.__code__.co_names)   # still no queue order
        self.assertIn("since", models.GpuLease.__init__.__code__.co_names)
        self.assertIn("Waiters take a slot in no set order", " ".join(ARCH.split()))

    def test_every_web_chat_api_path_is_in_the_api_reference(self):
        # #1042: docs/chat-api.md is the page another agent is handed; a path the server answers
        # that it doesn't name is one nobody can find
        doc = (ROOT / "docs/chat-api.md").read_text()
        paths = set(re.findall(r'"(/api/[a-z][a-z.-]*/?)', (ROOT / "workbench/chat.py").read_text()))
        self.assertGreater(len(paths), 10)
        missing = sorted(p for p in paths if p not in doc)
        self.assertEqual(missing, [], "add these to docs/chat-api.md")

    def test_the_openapi_file_describes_exactly_the_routes_chat_py_serves(self):
        # #1138: docs/chat-api.yaml is what another tool reads the API from; a route chat.py
        # serves that it lacks (or one it describes that chat.py doesn't) is a broken client.
        # chat.ROUTES is what is served (a path off it is 404), and each route in it is a branch
        # of its do_<method>, read from the source
        from workbench import chat
        spec = read_yaml((ROOT / "docs/chat-api.yaml").read_text())
        self.assertTrue(str(spec["openapi"]).startswith("3.1"), spec["openapi"])
        described = {(m.upper(), p) for p, item in spec["paths"].items() for m in item
                     if m in ("get", "put", "post", "delete", "patch", "head", "options")}
        served = {(m, v1(r)) for m, routes in chat.ROUTES.items() for r in routes}
        self.assertEqual(sorted(described - served), [], "not served by chat.py: drop from chat-api.yaml")
        self.assertEqual(sorted(served - described), [], "add these to docs/chat-api.yaml")
        handler = next(n for n in ast.parse((ROOT / "workbench/chat.py").read_text()).body
                       if isinstance(n, ast.ClassDef) and n.name == "Handler")
        for fn in (n for n in handler.body if isinstance(n, ast.FunctionDef) and n.name.startswith("do_")):
            branches = {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant)
                        and isinstance(n.value, str) and re.fullmatch(r"/api/[a-z][a-z.-]*/?", n.value)}
            self.assertEqual(sorted(branches), sorted(chat.ROUTES[fn.name[3:]]), fn.name)

    def test_the_openapi_file_is_whole_with_one_error_shape_bearer_auth_and_the_idempotency_key(self):
        # #1138: every $ref resolves, every refusal is the one Error schema, its codes are
        # chat.py's, what an agent may not call is marked so, and a send takes the key
        from workbench import chat
        spec = read_yaml((ROOT / "docs/chat-api.yaml").read_text())

        def refs(node):
            if isinstance(node, dict):
                yield from ([node["$ref"]] if "$ref" in node else [])
                for v in node.values():
                    yield from refs(v)
            elif isinstance(node, list):
                for v in node:
                    yield from refs(v)

        def follow(ref):
            node = spec
            for part in ref.removeprefix("#/").split("/"):
                node = node[part]
            return node
        for ref in set(refs(spec)):
            self.assertTrue(ref.startswith("#/components/"), ref)
            follow(ref)                                                  # a KeyError names a dangling one
        self.assertEqual(spec["security"], [{"bearer": []}])
        self.assertEqual(spec["components"]["securitySchemes"]["bearer"]["scheme"], "bearer")
        error = spec["components"]["schemas"]["Error"]
        self.assertEqual(error["required"], ["error", "code"])
        self.assertEqual(sorted(error["properties"]["code"]["enum"]),
                         sorted(set(chat.ERROR_CODES.values()) | set(chat.ERRORS_SAID)))
        user_only = set()
        for path, item in spec["paths"].items():
            names = {p.get("name") for p in [*item.get("parameters", []), *(
                o.get("parameters", []) for o in item.values() if isinstance(o, dict))] for p in (
                    [follow(p["$ref"])] if isinstance(p, dict) and "$ref" in p else p if isinstance(p, list) else [p])}
            for name in re.findall(r"{(\w+)}", path):
                self.assertIn(name, names, path)                         # a path's id is declared
            for method, op in ((m, o) for m, o in item.items() if m != "parameters"):
                if op.get("x-user-only"):
                    user_only.add((method.upper(), path))
                for status, answer in op["responses"].items():
                    if int(status) >= 400:
                        answer = follow(answer["$ref"])
                        self.assertEqual(answer["content"]["application/json"]["schema"],
                                         {"$ref": "#/components/schemas/Error"}, (path, status))
        self.assertEqual(sorted(user_only), sorted((m, v1(r)) for m, routes in chat.ROUTES.items()
                                                   for r in routes if not chat.agent_may(m, r)))
        send = spec["paths"]["/api/v1/send"]["post"]
        key = follow(send["parameters"][0]["$ref"])
        self.assertEqual((key["name"], key["in"]), ("Idempotency-Key", "header"))
        self.assertIn("Idempotent-Replayed", send["responses"]["200"]["headers"])
        self.assertEqual(sorted(send["responses"]), ["200", "400", "401", "403", "409", "413", "415", "422", "429"])
        with self.assertRaises(ValueError):                              # off the subset: said, not misread
            read_yaml("a:\n  b: |\n    c\n")

    def test_every_key_of_the_users_chat_state_is_in_the_api_reference(self):
        # #1127 (2026-11-23): snapshot() gained `tasks` (#1016) and week_saved `api` (#1022), and
        # docs/chat-api.md's /api/state list named neither; read from the source, no state needed
        import ast
        tree = ast.parse((ROOT / "workbench/chat.py").read_text())
        keys = {}
        for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("snapshot", "week_saved")):
            ret = next(n.value for n in ast.walk(fn) if isinstance(n, ast.Return))
            keys[fn.name] = {k.value for k in ret.keys if isinstance(k, ast.Constant)}
        self.assertGreater(len(keys["snapshot"]), 8)
        doc = (ROOT / "docs/chat-api.md").read_text()
        state = doc.split("### `GET /api/state`", 1)[1].split("\n### ", 1)[0]
        listed = set(re.findall(r"^- `([a-z_]+)`:", state, re.M))
        self.assertEqual(sorted(keys["snapshot"] - listed), [], "add these to docs/chat-api.md's /api/state")
        saving = " ".join(next(b for b in state.split("\n- ") if b.startswith("`savings`")).split())
        self.assertEqual(sorted(k for k in keys["week_saved"] if f"`{k}`" not in saving), [],
                         "name these in /api/state's `savings`")

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


    def test_the_handbooks_name_every_file_the_edit_tools_refuse_and_propose_only_what_wb_pairs(self):
        # #1521: they named CLAUDE.md, AGENTS.md and `.claude/` only, so an agent on a task touching
        # opencode.json or `.opencode/` met a denied edit with no word of it, and a `.proposed`
        # inside `.claude/` is denied too (`.claude/*`) while runner.PROPOSABLE pairs only those two
        names = {re.sub(r"^\*/?|\*$", "", p) for p in (*context.CLAUDE_FILES, *context.OPENCODE_FILES)}
        files = {n for n in names if not n.endswith("/")}
        for doc in ("agent/HANDBOOK.md", "agent/WRITING.md"):
            text = re.sub(r"\s+", " ", (ROOT / doc).read_text())
            self.assertEqual(sorted(n for n in names if n not in text), [], f"{doc}: names a refused file")
            said = text[text.index("can't be edited:"):text.index("`<name>.proposed`")]
            self.assertEqual({n for n in files if re.search(rf"(?<![\w.]){re.escape(n)}(?!\w)", said)},
                             set(runner.PROPOSABLE), f"{doc}: proposes only what wb pairs")
            self.assertIn("outside `.claude/`", said, doc)

class DocsFor20(unittest.TestCase):
    """#1027: since 2.0 an agent is a GPU model or an API model, and what people and Claude read
    says so; an API-only user read "A local model" as "not for me"."""

    def test_the_readme_pitch_names_the_api_and_the_gpu(self):
        # #1357 (2.2): the pitch leads with OpenCode, either host drives, and the pinned clause
        # (the owner, 2026-11-23: agents, not subagents) stays word for word
        readme = (ROOT / "README.md").read_text()
        self.assertIn("**Workbench is a harness: OpenCode agents via API or GPU write the code.**", readme)
        self.assertIn("Drive them from OpenCode with any model, or from Claude Code with `/workbench`.", readme)
        self.assertIn("**The crew** are OpenCode agents, on your GPU or an API model.", readme)
        self.assertIn("- A model for the agents: one you connected in OpenCode (`/connect`), or a local one.", readme)
        self.assertNotIn("local LLM", readme)
        self.assertNotIn("subagent", readme)

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
        # "stays local" lost only its last word: wb takeover's refusal still says where the work
        # stays (cli._coding_gate); the mod's ask rule went to a dialog at the edit (#1322)
        self.assertIn("Unanswered, the work stays with the agent", (ROOT / "workbench/cli.py").read_text())

    def test_no_wb_coding_sentence_calls_the_agent_local(self):
        # #1027 changed cli.CODING and the pane's copy in one word; the copy went with the pane's
        # Settings (#1283), and the sentences stay free of "local"
        for mode, (_, sentence) in cli.CODING.items():
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


class DocsForTheWebApp(unittest.TestCase):
    """#1288: 2.0 moved the pane's Stats and Settings to the web app (#1279), and the owner locked in
    (2026-11-24) that the terminal stays primary, the web chat a second lane, the web app's Settings
    the settings' home, with Claude helping on a clear ask."""

    def test_the_readme_quickstart_names_the_web_apps_settings_and_stats_and_its_link(self):
        readme = (ROOT / "README.md").read_text()
        quick = readme[readme.index("## Quickstart"):readme.index("## What it does")]
        self.assertIn("Settings and Stats live in the web app", quick)
        self.assertIn("`[•_•] Claude's Workbench - http://localhost:1337/#token=…`", quick)
        self.assertIn("`wb chat reach tailnet`", quick)  # #1355's phone reach: chat.json's `bind` by command

    def test_what_people_and_claude_read_names_no_pane_tab(self):
        # the pane is Tasks only since #1283: a Settings or Stats tab there no longer exists
        for path in ("README.md", "skill/workbench/SKILL.md", "skill/workbench/reference/driving.md"):
            text = (ROOT / path).read_text()
            for gone in ("Settings tab", "Stats tab"):
                self.assertNotIn(gone, text, path)

    def test_the_terminal_is_primary_where_claude_and_the_maintainers_read(self):
        skill = " ".join((ROOT / "skill/workbench/SKILL.md").read_text().split())
        self.assertIn("The terminal is primary", skill)
        self.assertIn("the web chat is a second lane", skill)
        self.assertIn("the settings live in the web app's Settings", skill.replace("The settings", "the settings"))
        driving = " ".join((ROOT / "skill/workbench/reference/driving.md").read_text().split())
        self.assertIn("The terminal stays primary; the chat is a second lane", driving)
        self.assertIn("**The terminal is primary; the web chat is a second lane**", " ".join(ARCH.split()))

    def test_the_agent_tokens_allow_list_in_the_api_reference_is_agent_may(self):
        # docs/chat-api.md is what another agent is handed: each (method, path) chat.AGENT_MAY lets
        # an agent token use is on its allow-list line, a trailing / as its <id>
        from workbench import chat
        doc = " ".join((ROOT / "docs/chat-api.md").read_text().split())
        allow = doc[doc.index("It works on an allow-list:"):]
        allow = allow[:allow.index("Every other path")]
        for method, path in sorted(chat.AGENT_MAY):
            self.assertIn(method, allow)
            self.assertIn(f"{path}<id>`" if path.endswith("/") else f"{path}`", allow, (method, path))

    def test_the_agents_skill_and_the_api_reference_say_the_driver_never_claude_alone(self):
        # #1366: another agent hands work to whichever driver listens, Claude Code or OpenCode,
        # and the skill and docs/chat-api.md said "Claude" for it: Claude is named only as Claude
        # Code (one driver) or Claude's Workbench, and the old path is only a pointer
        skill = ROOT / "skill/agents/ask-workbench"
        for path in [skill / "SKILL.md", *sorted((skill / "scripts").glob("*.py")),
                     ROOT / "docs/chat-api.md", ROOT / "docs/chat-api.yaml"]:
            text = " ".join(path.read_text().split()).replace("automated use of Claude", "")
            alone = [text[max(0, m.start() - 40):m.end() + 20]
                     for m in re.finditer(r"\bClaude\b(?! Code|'s Workbench)", text)]
            self.assertEqual(alone, [], path.relative_to(ROOT))
        self.assertIn("name: ask-workbench", (skill / "SKILL.md").read_text())
        old = (ROOT / "skill/agents/claude-workbench/SKILL.md").read_text()
        self.assertIn("Moved to the ask-workbench skill", old)


class ContactInOnePlace(unittest.TestCase):
    """#1291: `patches.UPSTREAM_CONTACT` is the one place the contact lives; every doc that also
    spells it out is in *Couplings*, so a new address reaches each. The skill once held the literal
    the Couplings entry didn't list (the #1291 review): Claude would have told users the old one."""

    def test_every_file_spelling_out_the_contact_is_in_its_couplings_entry_and_the_skill_names_none(self):
        from workbench import patches
        contact = patches.UPSTREAM_CONTACT
        couplings = " ".join(section(ARCH, "## Couplings").split())
        entry = couplings[couplings.index("**Workbench's contact**"):]
        entry = entry[:entry.index("**", len("**Workbench's contact**"))]
        holders = sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                         if p.is_file() and p.suffix in (".md", ".txt", ".tsx", ".ts", ".json")
                         and ".git" not in p.parts and "tests" not in p.parts
                         and contact in p.read_text(errors="replace"))
        self.assertFalse([h for h in holders if h.startswith("skill/")], "the skill says 'the address it printed'")
        for holder in holders:
            if holder != "ARCHITECTURE.md":
                self.assertIn(holder, entry)
        self.assertIn("its row in *Every `wb` command*", entry)
        self.assertIn("*Sending a patch without GitHub*", entry)


class TypeScriptOrPureScript(unittest.TestCase):
    """#1323, the owner (2026-11-24): "we should have either typescript or purescript, no more
    javascript". A JavaScript file in the repo is a generated one, its first line saying from what
    and how to build it again: the kernel's bundle (scripts/kernel.py) and the web app's scripts
    (scripts/web.py, each from its src/*.ts)."""

    HEADERS = {"mod/hooks/kernel.js": "// Generated by scripts/kernel.py from kernel/src: do not edit;"}
    WEB = "workbench/chat_static"

    def test_every_javascript_file_in_the_repo_is_generated_and_says_from_what(self):
        import subprocess
        try:
            out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard",
                                  "*.js", "*.mjs", "*.cjs"], capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("not a git checkout")
        files = [f for f in out.split("\n") if f and (ROOT / f).is_file()]
        self.assertIn(f"{self.WEB}/app.js", files)
        for f in files:
            head = (ROOT / f).read_text(errors="replace").split("\n", 1)[0]
            if f in self.HEADERS:
                self.assertTrue(head.startswith(self.HEADERS[f]), f"{f}: its generated header")
                continue
            path = Path(f)
            self.assertEqual(str(path.parent), self.WEB, f"{f}: hand-written JavaScript; write it in TypeScript")
            src = f"{self.WEB}/src/{path.stem}.ts"
            self.assertTrue((ROOT / src).is_file(), f"{f}: no {src} builds it")
            self.assertTrue(head.startswith(f"// Generated by scripts/web.py from {src}: do not edit;"), f"{f}: its header")


class OneCopyOfWbsShapes(unittest.TestCase):
    """#1323: what wb sends (the task files, notices, the chat's state, settings.py's rows, wb's
    JSON) is declared once, in mod/types/wire.d.ts. The web app kept its own copy of the chat's
    shapes, the mod its own of `wb route --json`'s, and the OpenCode plugin read them all as
    `Record<string, any>`: a field wb renamed met no type check but the one copy someone
    remembered. Each of the three takes them from there, and none declares one of them again."""

    WIRE = "mod/types/wire.d.ts"
    READERS = ("workbench/chat_static/src/types.d.ts", "mod/hooks/register.tsx", "mod/hooks/tasks.ts",
               "mod/hooks/modeline.ts", "opencode-plugin/pane.ts", "opencode-plugin/notices.ts", "opencode-plugin/chat.ts")

    def test_the_web_app_the_mod_and_the_opencode_plugin_take_wbs_shapes_from_one_module(self):
        wire = (ROOT / self.WIRE).read_text()
        names = set(re.findall(r"^export type (\w+)\b", wire, re.M))
        self.assertTrue({"TaskFile", "StatusFile", "NoticeRow", "ChatMsg", "Snapshot", "SettingRow", "RouteJson"} <= names)
        for reader in self.READERS:
            self.assertTrue(re.search(r"(?:from |import\()'[./a-z]*/wire\.(?:js|d\.ts)'", (ROOT / reader).read_text()),
                            f"{reader} takes no shape from {self.WIRE}")
        files = [ROOT / r for r in ("workbench/chat_static/src/types.d.ts", "mod/types/index.d.ts")]
        files += sorted((ROOT / "opencode-plugin").glob("*.ts*")) + sorted((ROOT / "mod/hooks").glob("*.ts*"))
        files += sorted((ROOT / "workbench/chat_static/src").glob("*.ts"))
        for f in files:
            for kind, name, body in re.findall(r"^(?:export )?(interface|type) (\w+)\b[^=\n{]*(=?)", f.read_text(), re.M):
                if name in names and not (kind == "type" and body):
                    self.fail(f"{f.relative_to(ROOT)} declares {name} again: take it from {self.WIRE}")
                if name in names:  # an alias: of wire's own, by import() type or by name
                    line = re.search(rf"^(?:export )?type {name}\b.*$", f.read_text(), re.M).group(0)
                    self.assertRegex(line, rf"=\s*(?:import\('[^']*types/wire\.js'\)\.)?{name}\s*$",
                                     f"{f.relative_to(ROOT)}: its {name} isn't {self.WIRE}'s")


def ts_strings(code: str) -> list[tuple[int, str]]:
    """The string literals of TypeScript source ``code`` ('…', "…", `…` whole), with their line:
    outside comments and regular expression literals."""
    out, i, n, prev = [], 0, len(code), ""
    while i < n:
        c = code[i]
        if code.startswith("//", i):
            i = code.find("\n", i)
            i = n if i < 0 else i
        elif code.startswith("/*", i):
            i = code.find("*/", i + 2)
            i = n if i < 0 else i + 2
        elif c in "'\"`":
            j, buf = i + 1, []
            while j < n and code[j] != c and (c == "`" or code[j] != "\n"):
                buf.append(code[j:j + 2] if code[j] == "\\" else code[j])
                j += 2 if code[j] == "\\" else 1
            out.append((code.count("\n", 0, i) + 1, "".join(buf)))
            i, prev = j + 1, c
        elif c == "/" and prev and prev in "(,=:[!&|?{};+-*%<>~^":  # a regular expression literal
            j, cls = i + 1, False
            while j < n and code[j] != "\n" and (cls or code[j] != "/"):
                cls = (cls and code[j] != "]") or code[j] == "["
                j += 2 if code[j] == "\\" else 1
            i, prev = j + 1, "/"
        else:
            prev = c if not c.isspace() else prev
            i += 1
    return out


class TheDriverIsNamedOnThePage(unittest.TestCase):
    # #1355: the page named Claude where the driver was meant ("waiting on Claude", "When Claude
    # answers", "Claude will offer to set it up") to an OpenCode user. A string in the web app's
    # TypeScript says the driver's name as the state gives it, never "Claude" by itself, but for
    # the name itself (a driver-name table's value, the fallback before 2.2's rows named theirs),
    # the app's own name, Claude Code (the host) and the savings score, which counts Claude's
    # tokens by name (savings.py). story.ts is generated from workbench/story.py.
    ALLOWED = re.compile(r"Claude's Workbench|Claude Code|Claude tokens|saved Claude")

    def test_no_string_in_the_web_apps_typescript_says_claude_where_the_driver_is_meant(self):
        src = ROOT / "workbench" / "chat_static" / "src"
        found = []
        for f in sorted(src.glob("*.ts")):
            if f.name == "story.ts" or f.name.endswith(".d.ts"):
                continue
            for line, text in ts_strings(f.read_text()):
                if "Claude" in text and text != "Claude" and "Claude" in self.ALLOWED.sub("", text):
                    found.append(f"{f.name}:{line}: {text[:120]}")
        self.assertEqual(found, [])

    def test_the_guard_reads_strings_not_comments_or_patterns(self):
        code = ("// Claude here\nconst a = 'waiting on Claude' /* Claude */\nconst r = /Claude's/.test(x)\n"
                "const b = `When ${who}`")
        self.assertEqual(ts_strings(code), [(2, "waiting on Claude"), (4, "When ${who}")])


if __name__ == "__main__":
    unittest.main()
