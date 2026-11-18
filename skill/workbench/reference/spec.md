# Checking a spec before `wb start`

Read from the /workbench skill before you send a spec you wrote (not the user's request pasted as
given). Spec gaps cause review rounds. You check the numbers you compute, but they hide in the
sentences you didn't execute. Go through these, each from a real miss:

1. **Every rule has a test.** For each "must", "rejects", "at most" or "only", name the test
   that fails if it's broken. A rule without one is a wish (seen: "one trailing newline"
   was stated, no test rejected a blank row, and the parser overflowed on one).
2. **Every edge input is named:** empty, one, the maximum, one past it, and for text, separators
   that lead, double or trail.
3. **Rules checked against each other and against the algorithm.** A size limit and a method
   can conflict: a heap that skips stale entries holds one entry per push, not per item. Give
   each fixed size its bound and why it holds.
4. **Every number run, not reasoned:** compute each expected value with a script, through the
   limits the spec sets (a clamp, a cap, integer division, float rounding). Seen: 24,900 of
   32,768 printed as 75%.
5. **Every fact about the repo checked in the repo:** each name, field, file and build step you
   mention (`grep` it). Memory is not a source. Seen: a field called `in.x` that was `move_x`,
   and "add it to the Makefile" for a Makefile that finds files by itself.
6. **Only the task.** Nothing about later steps, in the spec or in CLAUDE.md. An agent takes
   any "next" as its job.
7. **The reading fits.** Name only the files the agent must read, headers before sources, and
   put a short fact it needs (a signature, a constant) in the spec instead of a file to read.
   The repo's CLAUDE.md is already in its instructions: asking it to read that is reading twice.
   Spec plus named files (`wc -c`, ~4 characters a token) well under half the window, since the
   agent reads more than you name. Seen: a step whose reading filled 60% of a 32K window
   before the first edit, twenty sessions in a row.
8. **The old tests still hold.** A change of behaviour breaks tests that assumed the old one:
   run the whole suite against your simulated change (or read each test the change touches)
   and say in the spec which tests change and how. Seen: a smarter pursuit reached its target
   sooner, so a test measuring distance from the starting point failed, and a new collision rule
   erased an input a replay test relied on: two of the agent's questions, both the spec's.
9. **Every insertion point is a line to quote.** In a file the agent reads only in part, say
   where new code goes by the exact line it follows, unique in the file, not "after function
   X": it can't see where X ends. Seen: new tests put after a test's first `wait` call, its
   last eleven lines deleted, and the summary claiming otherwise.
