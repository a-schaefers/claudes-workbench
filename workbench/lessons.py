"""Lessons: what a task taught, gathered for Claude to turn into skills.

The agents learn nothing between tasks; Claude does, but only from what it notices. So every task
leaves its raw material here, read from the files it already has: each correction Claude sent back
(a feedback round's numbered items), what the agent itself says a future agent would trip on (a
`Lesson:` line in its summary), what stopped it (a guard), what it had to ask, and a handed skill
it loaded yet still needed review rounds. A correction that recurs across tasks is the strongest
signal there is: the next agent will get it wrong too, so it belongs in a skill.

The agent proposes; Claude decides. An agent's lesson from a task Claude discarded is dropped by
itself: a task that never found a working method has dead ends, not a recipe.

$WB_HOME/lessons.jsonl is append-only: a lesson row, later a row that closes it (written into a
skill, or dropped with why). A lesson is known by where it came from (task, kind, round, text),
so gathering a task twice adds nothing; its id is a short prefix of that's hash, made longer only
when the prefix is taken.
"""
from __future__ import annotations

import collections
import hashlib
import math
import re
from pathlib import Path

from . import ask_mcp, skills, store

FEEDBACK_HEAD = "Review feedback from the senior reviewer."
ITEM = re.compile(r"^\s*\d+[.)]\s+")
LESSON = re.compile(r"^\s*(?:[-*]\s*)?\**lesson\**\s*:\**\s*(.+)$", re.I | re.M)
SIMILAR = 0.35  # word overlap (Jaccard) at which two corrections are the same lesson
# The guards' fixed wording (events.StuckDetector, runner's round limits), which every stop lesson
# shares whatever stopped the agent: any two stops passed `_similar` on it alone, so each was
# "again" of every other, the 12 lines `wb lessons` shows were these clusters, kilobytes long,
# and boilerplate read as the lesson that recurs most (#733, the 2026-11-21 audit). Stops are
# compared on what varies: the call repeated, the error, the model's words.
STOP_WORDS = skills.words(
    "the guard stopped the agent: the same call times in a row; failing tool calls in a row "
    "(last: ); tool calls without changing a file in the task; the round ran past its minute "
    "limit; no output for minutes, waiting on a background job; no first output in minutes: the "
    "model may be reading its prompt slowly (on the CPU, or partly in RAM?); --silence-timeout "
    "gives a new task longer; no tool call or word for minutes, even after a nudge; minutes again "
    "with no tool call, even after a nudge; handed over to a fresh session times in this round "
    "without finishing: the step is too big for this model's window; split it (wb next) or take "
    "it over")
AGAIN_SHOWN = 5  # the other tasks a lesson's line names; the rest are counted (#733)


def path() -> Path:
    return store.home() / "lessons.jsonl"


ID_LEN = 6  # an id's shortest form: Claude types and reads them


def _id(task: str, kind: str, rnd, text: str) -> str:
    """The full hash; harvest keeps the shortest free prefix of it (_free_id)."""
    return hashlib.sha1(f"{task}\0{kind}\0{rnd}\0{text}".encode()).hexdigest()


def _key(x: dict) -> tuple:
    return x["task"], x["kind"], x.get("round"), x["text"]


def _free_id(digest: str, taken) -> str:
    """The shortest prefix of ``digest`` (ID_LEN, then 2 more at a time) no other lesson has.
    Six characters alone collided past a few thousand lessons, and a new lesson with a taken id
    was never written (the 2026-11-16 audit)."""
    n = ID_LEN
    while digest[:n] in taken and n < len(digest):
        n += 2
    return digest[:n]


def items(feedback: str) -> list[str]:
    """A feedback round's corrections, one per numbered item (the lines under it joined, its
    bullets too: they are the evidence for it, not corrections of their own); the whole text,
    as one, when nothing is numbered."""
    lines = feedback.splitlines()
    if not any(ITEM.match(line) for line in lines):
        whole = " ".join(line.strip() for line in lines if line.strip())
        return [whole[:600]] if len(whole) > 8 else []
    out, cur = [], None
    for line in lines:
        if ITEM.match(line):
            if cur:
                out.append(" ".join(cur))
            cur = [ITEM.sub("", line).strip()]
        elif line.strip() and cur is not None:
            cur.append(line.strip())
    if cur:
        out.append(" ".join(cur))
    return [x[:600] for x in out if len(x) > 8]


def feedback(prompt: str) -> list[str]:
    """The body of each review feedback a round's prompt holds, on its own. A round that never
    reached the agent gets wb feedback's text after its own prompt (runner.add_to_round), and a
    note sent before a round began goes after it too (_early_notes): read as one body from the
    first blank line on, the second feedback's head and its "carry on" were joined to the last
    item, a garbled lesson of its own (#1180, 2026-11-24). So each FEEDBACK_HEAD paragraph opens
    a feedback, wherever it is (a wb next step's unsent round's too), and a folded note ends it.
    A handover's carry-on (CARRY_ON) or a resume's (RESUME) holds none of its own: it copies the
    earlier rounds' feedback whole, which each recorded again under the new round, the last item
    with the agent's last words joined on (the review of #1180, 2026-11-24)."""
    from .runner import CARRY_ON, NOTE_HEAD, RESUME  # runner imports this module
    if prompt.startswith((CARRY_ON[:60], RESUME[:60])):
        return []
    out = []
    for part in re.split(r"\n\n(?=" + re.escape(FEEDBACK_HEAD) + ")", prompt):
        if part.startswith(FEEDBACK_HEAD):
            out.append(part.split("\n\n", 1)[-1].split("\n\n" + NOTE_HEAD, 1)[0])
    return out


def round_feedback(task: store.Task, rnd: int) -> list[str]:
    """The review feedback round ``rnd`` gave (feedback's bodies): its prompt's when the prompt is
    a feedback (a fresh session's handoff took prompt.md's place: store.round_prompt), else only
    what wb feedback added to the round before it was sent (runner.add_to_round, kept in its
    notes.md too): a carry-on or a resume copies the earlier feedback whole, and a spec or a wb
    next step could quote the head (the review of #1180, 2026-11-24)."""
    from .runner import FOLDED  # runner imports this module
    prompt = store.round_prompt(task, rnd)
    text = prompt.read_text() if prompt.is_file() else ""
    if not text.startswith(FEEDBACK_HEAD):
        folded = task.round_dir(rnd) / FOLDED
        text = folded.read_text() if folded.is_file() else ""
    return feedback(text)


def gather(task: store.Task, outcome: str | None = None) -> list[dict]:
    """Every lesson candidate the task's files hold (outcome: how it closed, if it has)."""
    spec, st = task.spec, task.status
    loaded = skills.loaded(task)
    base = {"task": task.name, "repo": spec.get("repo"), "skills": loaded}
    out = []

    def add(kind, rnd, text):
        out.append({**base, "id": _id(task.name, kind, rnd, text), "kind": kind, "round": rnd,
                    "text": text})

    for d in sorted(task.dir.glob("rounds/*"), key=lambda p: int(p.name) if p.name.isdigit() else 0):
        rnd = int(d.name) if d.name.isdigit() else 0
        for body in round_feedback(task, rnd):
            for item in items(body):
                add("feedback", rnd, item)
        # the agent's whole final message, which the report may cut (#1186); a round from before
        # final.md, its report's section
        summary, final = d / "summary.md", d / "final.md"
        said = ("" if outcome == "discarded" else final.read_text() if final.is_file()
                else summary.read_text().split("agent's summary:", 1)[-1] if summary.is_file() else "")
        for m in LESSON.finditer(said):
            add("agent", rnd, m.group(1).strip()[:400])
    for e in store.read_jsonl(task.dir / "events.jsonl")[0]:
        if e.get("kind") == "stopped" and e.get("text") and not e.get("by"):  # the guard's, not a person's
            add("stopped", e.get("round"), f"the guard stopped the agent: {e['text'][:300]}")
    for q in sorted(task.dir.glob("ask/*.json")):
        a = q.with_suffix(".answer")
        if a.is_file() and a.read_text().strip() != ask_mcp.NO_ANSWER:  # dropped, never answered
            ask = store.read_json(q, {})
            add("question", ask.get("round"), f"the agent asked: {ask.get('question', '')[:250]} "
                                              f"— answered: {a.read_text().strip()[:250]}")
    for g in st.get("spec_gaps") or []:  # Claude's own: a rule for writing specs, not for agents
        add("spec", g.get("round"), f"the spec missed: {g['text'][:400]}")
    steps = (st.get("step_sizes") or []) + [{"reviews": st.get("reviews", 0)}]
    agent = sum(s.get("reviews") or 0 for s in steps) - len({g.get("round") for g in st.get("spec_gaps") or []})
    if outcome and agent > 0:  # every step's rounds, less the ones the spec cost
        for s in loaded:
            add("skill", None, f"loaded {s}, yet needed {agent} review round(s): "
                               f"does {s} say the right thing?")
    return out


def _all() -> tuple[dict[str, dict], dict[str, dict]]:
    """(every lesson by id, the closing row of each closed one)."""
    lessons, closed = {}, {}
    for r in store.read_jsonl(path())[0]:
        if "close" in r:
            closed[r["close"]] = r
        elif r.get("id"):
            lessons.setdefault(r["id"], r)
    return lessons, closed


def harvest(task: store.Task, outcome: str | None = None) -> list[dict]:
    """Record the task's new lessons; on a discard, drop the agent's own (unresolved: no recipe).
    A trial run (a check of workbench itself) teaches the agents nothing."""
    if task.spec.get("trial"):
        return []
    lessons, closed = _all()
    known, taken, new = {_key(x) for x in lessons.values()}, set(lessons), []
    for x in gather(task, outcome):
        if _key(x) in known:
            continue
        known.add(_key(x))
        x = {**x, "id": _free_id(x["id"], taken)}
        taken.add(x["id"])
        new.append(x)
        store.append_jsonl(path(), {"ts": store.now(), **x})
    if outcome == "discarded":
        for x in lessons.values():
            if x["task"] == task.name and x["kind"] == "agent" and x["id"] not in closed:
                close([x["id"]], "dropped", "the task was discarded: its attempts are not a recipe")
    return new


def close(ids: list[str], how: str, why: str = "") -> list[str]:
    """Close lessons ("skill:<name>" or "dropped"); returns the ids that were open."""
    lessons, closed = _all()
    done = [i for i in ids if i in lessons and i not in closed]
    for i in done:
        store.append_jsonl(path(), {"close": i, "ts": store.now(), "how": how, "why": why})
    return done


def repos(ids: list[str]) -> set:
    """The repos the given lessons came from."""
    lessons = _all()[0]
    return {lessons[i].get("repo") for i in ids if i in lessons}


def _similar(a: set, b: set) -> bool:
    shared = len(a & b)  # Jaccard without building the union: |a | b| = |a| + |b| - shared
    return shared >= 3 and shared >= SIMILAR * (len(a) + len(b) - shared)


def _prefix(n: int) -> int:
    """How many of a lesson's n words, in one fixed order (rarest first), any lesson similar to it
    must share one of: two similar lessons share at least 3 words and SIMILAR of their union,
    which is at least SIMILAR * n, so both hold one of each one's first n - that + 1."""
    return max(0, n - max(3, math.ceil(SIMILAR * n)) + 1)


def _pairs(bags: dict[str, set], only: set | None) -> dict[str, set]:
    """Each lesson's similar ones, by prefix filtering (exact for `_similar`): only lessons that
    share a word of their prefixes are compared. For every lesson, each pair is compared once, as
    the index fills; for ``only``, those against the whole index."""
    df = collections.Counter(w for bag in bags.values() for w in bag)
    prefix = {j: sorted(bag, key=lambda w: (df[w], w))[:_prefix(len(bag))] for j, bag in bags.items()}
    out: dict[str, set] = {j: set() for j in bags}
    index: dict[str, list[str]] = {}
    if only is not None:
        for j, words in prefix.items():
            for w in words:
                index.setdefault(w, []).append(j)
    for i, words in prefix.items():
        if only is not None and i not in only:
            continue
        for j in {k for w in words for k in index.get(w, ())}:
            if j != i and _similar(bags[i], bags[j]):
                out[i].add(j)
                out[j].add(i)
        if only is None:
            for w in words:
                index.setdefault(w, []).append(i)
    return out


def survey(only: set | None = None) -> list[dict]:
    """The open lessons, each with `again` (other tasks with the same lesson; a closed one there
    means it came back after it was handled), and `skill` (the library skill it seems to belong
    to); the ones that recur first. ``only``: just those ids (nudge's new ones).

    Lessons are compared only where `_pairs`' prefixes meet, and the skills' descriptions are read
    into words once: every open lesson against all of lessons.jsonl, never pruned, made each
    accept, feedback and discard wait seconds past a thousand tasks (5 s at 3,000 lessons, #556)."""
    lessons, closed = _all()
    have = skills.available()
    bags = {i: skills.words(x["text"]) - (STOP_WORDS if x["kind"] == "stopped" else set())
            for i, x in lessons.items() if x["kind"] != "skill"}  # a stop on what varies (#733)
    near = _pairs(bags, only)
    described = [(n, skills.words(f"{n} {v['description']}")) for n, v in have.items()]
    out = []
    for i, x in lessons.items():
        if i in closed or only is not None and i not in only:
            continue
        again = {(lessons[j]["task"], closed.get(j, {}).get("how", "")) for j in near.get(i, ())
                 if lessons[j]["task"] != x["task"]}
        skill = next((s for s in x.get("skills") or [] if s in have), None)
        if not skill and i in bags:
            best = max(((len(bags[i] & bag), n) for n, bag in described), default=(0, None))
            skill = best[1] if best[0] >= 3 else None
        out.append({**x, "again": sorted(again), "skill": skill})
    # the most other tasks where it is still open first: closed ones there say it was handled (#733)
    return sorted(out, key=lambda x: (-len({t for t, h in x["again"] if not h}), -len(x["again"]),
                                      x["skill"] is None, -x.get("ts", 0)))


def line(x: dict) -> str:
    where = f"{x['task']}" + (f" round {x['round']}" if x.get("round") else "")
    again = ""
    if x["again"]:
        # capped: every one listed made a line kilobytes long (#733)
        handled = [f"{t} ({h})" if h else t for t, h in x["again"]]
        tasks = len({t for t, _ in x["again"]})
        again = (f" · also in {', '.join(handled)}" if len(handled) <= AGAIN_SHOWN else
                 f" · also in {tasks} other tasks ({', '.join(handled[:AGAIN_SHOWN])}, …)")
    hint = f" → {x['skill']}?" if x["skill"] else ""
    return f"{x['id']} {x['kind']} ({where}){again}: {x['text'][:200]}{hint}"


def nudge(task: store.Task, outcome: str | None = None) -> str:
    """The one line `wb feedback|next|resume|accept|discard` ends with when the task taught something."""
    new = harvest(task, outcome)
    if not new:
        return ""
    ids = {x["id"] for x in new}
    open_ = survey(only=ids)  # the new ones only, not a survey of every open lesson (#556)
    again = sum(bool(x["again"]) for x in open_)
    return (f"lessons: {len(open_)} from this task" + (f", {again} seen in other tasks too" if again else "")
            + " (wb lessons: write or patch a skill now, while it's fresh)") if open_ else ""
