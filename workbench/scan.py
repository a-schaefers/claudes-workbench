"""The security scan for a skill on its way into the library.

A skill is text an agent follows with a shell and the user's files, so one written elsewhere (a
public library, a repo, an agent's proposal) is read like untrusted code before it goes in: what
would leak secrets, inject instructions, destroy files, open a shell to the outside, or hide what
it says. A critical finding refuses the skill; a high one is a warning Claude must read. On an
update only the findings the change brought in count, so an accepted line doesn't nag forever.

The patterns, the invisible-character list, the padded-code check and the content hash are
adapted from Hermes Agent's tools/skills_guard.py (NousResearch/hermes-agent at 081e8d9):

    MIT License. Copyright (c) 2025 Nous Research. Permission is hereby granted, free of charge,
    to any person obtaining a copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including without limitation the
    rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the
    Software, and to permit persons to whom the Software is furnished to do so, subject to the
    following conditions: The above copyright notice and this permission notice shall be included
    in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS",
    WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
    OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
    ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE
    OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

Kept: the critical and high rules that matter to a coding agent. Left out: Hermes's trust tiers
(everything from outside is untrusted here), its own config paths, and the medium rules that fire
on ordinary coding advice. Added: Unicode format characters and tag characters (invisible
"ASCII smuggling"), which its fixed list misses.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

SECRET = r"\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)S?\b"
LOOPBACK = r"(?![^\n]*https?://(?:localhost|127\.0\.0\.1|\[::1\]))"
AGENT_FILES = r"(?:AGENTS\.md|CLAUDE\.md|\.claude/settings[\w.]*)"
SHELLS = r"(?:bash|sh|zsh|ksh|dash)"
# an interpreter that runs what it reads (`| sh`, `| python3 -`), not one running a module or
# a file of its own on the piped data (`| python3 -m json.tool`)
RUNS_STDIN = r"(?=\s*(?:$|[;&|)`]|-\s|-$|-s\b|-i\b))"
S = r"[ \t]+"            # the blanks between words of one line
W = r"(?:\w+[ \t]+){0,4}"  # and at most four words between the ones a rule names

# (regex, rule, severity, what it means); compiled case-insensitive, (?-i:) where case matters
PATTERNS = [
    # exfiltration
    (rf"curl\s+{LOOPBACK}[^\n]*{SECRET}", "env_exfil_curl", "critical",
     "curl sends a secret environment variable"),
    (rf"wget\s+{LOOPBACK}[^\n]*{SECRET}", "env_exfil_wget", "critical",
     "wget sends a secret environment variable"),
    # reading one: not writing it (`cat <<EOF > .env`), not the template (`.env.example`)
    (r"cat\s+(?!<<)[^\n>|]*(\.env(?![\w.-])|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)", "read_secrets_file",
     "critical", "reads a known secrets file"),
    (r"(?:\$HOME|~)/\.(?:ssh|aws|gnupg|kube|docker)\b", "credential_dir", "high",
     "points at the user's credential directories"),
    (r"printenv|(?<![A-Za-z0-9_])env\s*\|(?!\|)(?!\s*[{(])", "dump_all_env", "high",
     "dumps every environment variable"),
    (r"!?\[[^\[]*\]\(https?://[^\)]*\$\{?", "md_link_exfil", "high",
     "a markdown link or image whose URL carries a variable"),
    # a sender after it: not a script of the skill's own (`&& python3 tools/summarize.py`, #455)
    (r">\s*/tmp/[^\s]*\s*&&\s*(curl|wget|nc|ncat|netcat|python[23]?\s+-c)\b", "tmp_staging", "critical",
     "stages data in /tmp, then sends it"),
    (r"(send|post|upload|transmit)\s+.*\s+(to|at)\s+https?://", "send_to_url", "high",
     "tells the agent to send data to a URL"),
    (r"webhook\.site|requestbin\.com|pipedream\.net|hookbin\.com|pastebin\.com", "exfil_service",
     "high", "names a data-collection service"),
    # prompt injection: within a line, words apart at most a few ({W}): `\s+` and any number
    # of words once read "## Ignore generated files", a blank line, "Follow all instructions",
    # and "system prompt and the project config can override", as injections (#455)
    (rf"ignore{S}{W}(previous|all|above|prior){S}instructions", "ignore_instructions",
     "critical", "prompt injection: ignore previous instructions"),
    (rf"disregard{S}{W}(your|all|any){S}{W}(instructions|rules|guidelines)",
     "disregard_rules", "critical", "tells the agent to disregard its rules"),
    (rf"system{S}{W}prompt{S}(?:\w+{S}){{0,2}}override|override{S}{W}system{S}prompt",
     "sys_prompt_override", "critical", "tries to override the system prompt"),
    (rf"act{S}as{S}(if|though){S}{W}you{S}{W}(have{S}no|don't{S}have){S}"
     rf"{W}(restrictions|limits|rules)", "bypass_restrictions", "critical",
     "tells the agent to act without restrictions"),
    (rf"(respond|answer|reply){S}without{S}{W}(restrictions|limitations|filters|safety)",
     "remove_filters", "critical", "tells the agent to drop its safety rules"),
    (rf"\bDAN{S}mode\b|Do{S}Anything{S}Now", "jailbreak", "critical", "a known jailbreak"),
    (rf"you{S}are{S}{W}now{S}", "role_hijack", "high", "tries to change the agent's role"),
    (rf"pretend{S}{W}(you{S}are|to{S}be){S}", "role_pretend", "high",
     "tries to give the agent another identity"),
    (rf"do{S}not{S}{W}tell{S}{W}the{S}(?:user|reviewer)(?!{S}to{S}[\"'])"
     r"(?!.*\b(?:unless|except|until|confirm|verify|check)\b)", "deception_hide", "high",
     "tells the agent to hide something from the user or reviewer"),
    (rf"(when|if){S}no[ \t]*one{S}is{S}(watching|looking)", "conditional_deception", "high",
     "behave differently when unobserved"),
    (rf"output{S}{W}(system|initial){S}prompt", "leak_system_prompt", "high",
     "tries to extract the system prompt"),
    (r"<!--[^>]*\b(?:ignore|forget|disregard|override|secret|hidden|system\s*prompt|you\s+are\s+now)"
     r"\b[^>]*-->", "html_comment_injection", "high", "instructions hidden in an HTML comment"),
    (r"<\s*div\s+style\s*=\s*[\"'][\s\S]*?display\s*:\s*none", "hidden_div", "high",
     "an invisible HTML block"),
    # destructive
    (r"(?<![A-Za-z0-9])rm\s+(-[^\s]*)?r[^\n]*\s[\"']?(?:\$HOME|\$\{HOME\}|~)/?\*?[\"']?(?:\s|$)",
     "destructive_home_rm", "critical", "recursive delete of the home directory"),
    (r"(?<![A-Za-z0-9])rm\s+-[a-z]*r[a-z]*\s+(?:--no-preserve-root\s+)?/(?:\s|\*|$)",
     "destructive_root_rm", "critical", "recursive delete from the root"),
    (r">\s*/etc/", "system_overwrite", "critical", "overwrites system configuration"),
    (r"\bmkfs\b", "format_filesystem", "critical", "formats a filesystem"),
    (r"\bdd\s+.*if=.*of=/dev/", "disk_overwrite", "critical", "writes a raw disk"),
    (r"/etc/sudoers|visudo", "sudoers_mod", "critical", "changes sudoers"),
    # remote code and shells
    (rf"(?:curl|wget)\s+[^|\s][^\n]*\|\s*(?:sudo\s+(?:-\S+\s+)*)?{SHELLS}\b", "pipe_to_shell",
     "critical", "downloads a script and runs it"),
    (rf"/bin/{SHELLS}\b\s+-i\s+.*>/dev/tcp/", "bash_reverse_shell", "critical",
     "an interactive reverse shell"),
    # its own flags, not a later command's (`until nc -z db 5432; do …; done; ./run -c x`, #455)
    (r"\b(?:nc|ncat|netcat)\b[^\n;&|]*\s-[a-z]*[ec]\b|\bsocat\b[^\n]*\b(?:exec|system|pty)[\w-]*:",
     "reverse_shell", "critical", "a reverse shell (a listener that runs a program)"),
    (r"\b(?:nc|ncat|netcat)\s+-[a-z]*l|\bsocat\b[^\n]*\b(?:tcp|udp)[\w-]*-listen:",
     "listener", "high", "a network listener"),
    (r"python[23]?\s+-c\s+[\"']import\s+socket", "python_socket_oneliner", "critical",
     "a one-line Python socket (likely a reverse shell)"),
    (r"\bngrok\b|\blocaltunnel\b|\bserveo\b", "tunnel_service", "high",
     "opens a tunnel from outside to this machine"),
    # obfuscation
    (rf"\bbase64\s+(?:-[^\s|]+\s+)*?(?:-[a-z]*d[a-z]*|--decode)\b[^\n]*\|\s*(?:{SHELLS}|python3?|perl|ruby|node)\b{RUNS_STDIN}",
     "base64_decode_pipe", "critical", "decodes base64 and runs it"),
    (rf"echo\s+[^\n]*\|\s*(?:{SHELLS}|python3?|perl|ruby|node)\b{RUNS_STDIN}", "echo_pipe_exec",
     "critical", "echoes text into an interpreter"),
    (r"chr\s*\(\s*\d+\s*\)\s*\+\s*chr\s*\(\s*\d+", "chr_building", "high",
     "builds a string from chr() calls"),
    # credentials in the skill itself
    (r"(?-i:-----BEGIN\s+(RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE\s+KEY-----)", "embedded_private_key",
     "critical", "a private key"),
    (r"(?-i:ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{80,}|glpat-[A-Za-z0-9_\-]{20,})",
     "git_token", "critical", "a GitHub or GitLab token"),
    (r"(?-i:sk-ant-[A-Za-z0-9_-]{90,}|sk-[A-Za-z0-9]{20,})", "api_key", "critical", "an API key"),
    (r"(?-i:AKIA[0-9A-Z]{16})", "aws_access_key", "critical", "an AWS access key"),
    # Claude's files: agents never edit them (context.py denies it); a skill that says to is wrong
    (rf"(?:>>|[\w\"'`)\]]\s*>)\s*[~\w./-]*{AGENT_FILES}(?!\.?\w)|\bsed\b[^\n]*\s-[A-Za-z]*i[^\n]*"
     rf"{AGENT_FILES}|\btee\s+(?:-a\s+)?[~\w./\"'-]*{AGENT_FILES}", "agent_config_write", "critical",
     "a shell write to CLAUDE.md, AGENTS.md or Claude's settings"),
    (rf"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:add|append|write|edit|modify|update|change|insert|replace|"
     rf"overwrite|remove|delete)\b[^\n,]{{0,80}}?{AGENT_FILES}", "agent_config_edit", "high",
     "tells the agent to edit Claude's files (CLAUDE.md, AGENTS.md)"),
]
COMPILED = [(re.compile(p, re.IGNORECASE | re.MULTILINE), *rest) for p, *rest in PATTERNS]

INVISIBLE = {
    "​": "zero-width space", "‌": "zero-width non-joiner", "‍": "zero-width joiner",
    "⁠": "word joiner", "⁢": "invisible times", "⁣": "invisible separator",
    "⁤": "invisible plus", "﻿": "zero-width no-break space"}
BIDI = {"‪", "‫", "‬", "‭", "‮", "⁦", "⁧", "⁨", "⁩"}
# code pushed off-screen: 200+ blanks, then 40+ characters on the same line
PADDED = re.compile(r"(?<![ \t])[ \t]{200,}+(?=\S[^\n]{39})")
PROSE = {".md", ".txt", ".html", ".xml", ".tex", ".css"}
BINARY = {".exe", ".dll", ".so", ".dylib", ".bin", ".dat", ".com", ".msi", ".dmg", ".app", ".deb",
          ".rpm", ".jar", ".class", ".pyc"}
ARCHIVE = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar"}
MEDIA = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".ttf", ".otf", ".woff", ".woff2",
         ".mp3", ".wav", ".ogg", ".mp4"}  # assets a skill may ship; not text to scan
ORDER = {"critical": 0, "high": 1}


def finding(rule, severity, file, line, text, why) -> dict:
    return {"rule": rule, "severity": severity, "file": file, "line": line,
            "text": text.strip()[:120], "why": why}


def files(path: Path) -> list[tuple[str, Path]]:
    """(relative name, path) for a skill directory's files, or a lone SKILL.md."""
    if path.is_file():
        return [(path.name, path)]
    return sorted((p.relative_to(path).as_posix(), p) for p in path.rglob("*")
                  if (p.is_file() or p.is_symlink()) and ".git" not in p.relative_to(path).parts)


def scan(path: Path) -> list[dict]:
    """Every finding in a skill (a directory or a SKILL.md), most severe first. What is scanned is
    what `skills.add` copies: links are followed (copytree and copyfile dereference them), so a
    link's target is scanned under the link's own name and suffix. Before the 2026-11-16 audit an
    in-skill link was skipped: a SKILL.md given as a link, or guide.md -> pic.png holding text,
    reached the library unscanned."""
    out = []
    root = path.resolve() if path.is_dir() else None  # a lone file has no skill around it
    for rel, p in files(path):
        if p.is_symlink() and root:
            target = p.resolve()
            if not target.is_relative_to(root) or ".git" in target.relative_to(root).parts:
                out.append(finding("symlink_escape", "critical", rel, 0, f"-> {target}",
                                   "a symlink out of the skill"))
                continue
            # a link to nothing, or to the directory it is in or one above, is refused: the copy
            # follows links, failed on the first and recursed into self/self/... on the second,
            # leaving a half-copied skill in the library (#604, Claude's audit of 1.90)
            if not target.exists():
                out.append(finding("symlink_dangling", "critical", rel, 0, f"-> {target}",
                                   "a symlink to nothing: the copy can't follow it"))
                continue
            if target.is_dir() and p.parent.resolve().is_relative_to(target):
                out.append(finding("symlink_loop", "critical", rel, 0, f"-> {target}",
                                   "a symlink to a directory holding it: the copy never ends"))
                continue
            if target.is_dir():
                continue  # its files are scanned under their own names
        if p.suffix.lower() in BINARY:
            out.append(finding("binary_file", "critical", rel, 0, p.suffix,
                               "a binary or executable has no place in a skill"))
            continue
        if p.suffix.lower() in ARCHIVE:
            out.append(finding("archive", "high", rel, 0, p.suffix,
                               "an archive hides its contents from review: unpack it"))
            continue
        if p.suffix.lower() in MEDIA:  # real images, fonts and audio hold NUL bytes early
            with open(p, "rb") as f:
                if b"\x00" in f.read(8192):
                    continue
        raw = p.read_bytes()
        if p.suffix.lower() in MEDIA and not _utf8(raw):
            continue  # binary all the same; text named .png is scanned as prose
        if b"\x00" in raw:
            out.append(finding("nul_bytes", "high", rel, 0, "", "NUL bytes in a text file"))
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            out.append(finding("not_utf8", "high", rel, 0, "", "not valid UTF-8"))
            continue
        out += scan_text(text, rel, prose=p.suffix.lower() in PROSE | MEDIA)
    return sorted(out, key=lambda f: (ORDER[f["severity"]], f["file"], f["line"]))


def _utf8(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


# What once refused legitimate skills as critical (#455): a `>` closing an HTML tag read as a
# redirect (`Read <code>CLAUDE.md</code>`), a warning against reading a secrets file ("Never
# `cat .env`"), a container build writing its own /etc (`RUN echo … > /etc/apt/…`), and one agent
# file set against another (`AGENTS.md > CLAUDE.md`, a precedence or a copy: Claude reads it).
REDIRECTS = {"system_overwrite", "agent_config_write", "tmp_staging"}  # matched with tags blanked
TAG = re.compile(r"</?[A-Za-z][\w-]*(?:\s[^<>\n]*)?>")
NEGATED = re.compile(r"\b(?:never|don'?t|do\s+not|must\s+not|shouldn'?t|avoid)\b[^.;:!?\n]*$",
                     re.IGNORECASE)
CONTAINER = re.compile(r"^\s*RUN\s")  # a Dockerfile's step: it writes the image, not this machine


def _softened(rule: str, rel: str, text: str, m: re.Match) -> str | None:
    """None to drop the finding, "high" to make it a warning, "" to keep it as it is."""
    head = text[text.rfind("\n", 0, m.start()) + 1:m.start() + 1]  # its line, up to the match
    if rule == "read_secrets_file" and NEGATED.search(head[:-1]):
        return None
    if rule == "system_overwrite" and (CONTAINER.match(head) or Path(rel).name.lower() in
                                       ("dockerfile", "containerfile")
                                       or rel.lower().endswith(".dockerfile")):
        return "high"
    if rule == "agent_config_write" and re.search(rf"{AGENT_FILES}$", head, re.IGNORECASE):
        return "high"
    return ""


def scan_text(text: str, rel: str = "SKILL.md", prose: bool = True) -> list[dict]:
    out = []
    lines = text.splitlines()
    tagless = TAG.sub(lambda m: " " * len(m.group()), text)  # offsets and lines kept
    for rx, rule, severity, why in COMPILED:
        body = tagless if rule in REDIRECTS else text
        for m in rx.finditer(body):
            if (soft := _softened(rule, rel, body, m)) is None:
                continue
            n = body.count("\n", 0, m.start()) + 1
            out.append(finding(rule, soft or severity, rel, n,
                               lines[n - 1] if n <= len(lines) else "", why))
    for n, line in enumerate(lines, 1):
        if n == 1:
            line = line.removeprefix("\ufeff")  # a byte-order mark opening a file is ordinary
        hidden = sorted({c for c in line if c in INVISIBLE or c in BIDI
                         or 0xE0000 <= ord(c) <= 0xE007F
                         or (unicodedata.category(c) == "Cf" and c != "­")})
        if hidden:
            names = ", ".join(INVISIBLE.get(c) or f"U+{ord(c):04X}" for c in hidden)
            worst = any(c in BIDI or 0xE0000 <= ord(c) <= 0xE007F for c in hidden)
            out.append(finding("invisible_text", "critical" if worst else "high", rel, n,
                               names, "invisible characters can hide instructions"))
        if not prose and (m := PADDED.search(line)) and re.search(r"[A-Za-z_$][\w$]{2,}", line[m.end():]):
            out.append(finding("padded_code", "critical", rel, n, line[m.end():],
                               "code hidden after a long run of blanks (off-screen in editors)"))
    return out


def new_findings(after: list[dict], before: list[dict]) -> list[dict]:
    """The findings an edit brought in: the same file, rule and line text counts as the same one."""
    seen = {(f["file"], f["rule"], f["text"]) for f in before}
    return [f for f in after if (f["file"], f["rule"], f["text"]) not in seen]


def verdict(found: list[dict]) -> str:
    return "dangerous" if any(f["severity"] == "critical" for f in found) else \
        "caution" if found else "clean"


def lines(found: list[dict]) -> list[str]:
    return [f"{f['severity']}: {f['file']}" + (f":{f['line']}" if f["line"] else "")
            + f" {f['why']} [{f['rule']}]" + (f": {f['text']}" if f["text"] else "") for f in found]


# What `wb skill add --reviewed RULE` can't pass: what Claude can't read for itself (hidden
# characters, code pushed off-screen, a binary) or what would copy from outside the skill
UNREVIEWABLE = {"symlink_escape", "binary_file", "invisible_text", "padded_code", "nul_bytes"}


def content_hash(path: Path) -> str:
    """sha256 over each file's relative path and bytes, in path order: the same skill, the same hash."""
    h = hashlib.sha256()
    for rel, p in files(path):
        if p.is_file() and not p.is_symlink():
            h.update(rel.encode() + b"\x00" + p.read_bytes())
    return h.hexdigest()
