-- | Which notice is due and what a host does with it: the same plan whether the Claude Code mod
-- | wakes Claude or the OpenCode plugin wakes its agent (#1325's M3 and M4, for #1313). The host
-- | reads the files (notify.jsonl, a task's `acked` and `noticed`, its status, its open questions)
-- | and carries the plan out: the toast, the wake, wb's compare-and-set (`wb ack`) that stays the
-- | lock, the words. Each notice is delivered at most once while it is still true.
module Kernel.Notice (Row, noticesDue, noticePlan, rewakePlan) where

import Prelude

import Data.Array (filter, findIndex, foldl, modifyAt, snoc)
import Data.Maybe (Maybe(..), fromMaybe)
import Kernel.Phase (phaseOf)

-- | A notify.jsonl row as the host parsed it: its task, kind (`review`, `stuck`, `failed`,
-- | `question`, `checkin`, …), time (s) and, for a batch's notice, the tasks it names ([] for none).
type Row = { task :: String, kind :: String, ts :: Number, tasks :: Array String }

-- | Never woken for a notice older than this before the later of the load's start and now.
oldestWakeMs :: Number
oldestWakeMs = 60.0 * 60.0 * 1000.0

-- | The notices due a look, from notify.jsonl's rows (oldest first), the load's start and the
-- | time (ms): per task, in the order the tasks first come due, its every question and its newest
-- | notice (`picks`: the rows' indices; two questions asked at once are both open, and the newest
-- | alone hid the first, its agent blocked, the 2026-11-16 audit), none older than an hour; and
-- | `noticed`, each task's newest notice however old, a batch's counting for every task it names
-- | (rewake's mark: news Claude had, not a lost one).
noticesDue
  :: { rows :: Array Row, startedAt :: Number, now :: Number }
  -> { due :: Array { task :: String, picks :: Array Number }
     , noticed :: Array { task :: String, ts :: Number }
     }
noticesDue { rows, startedAt, now } =
  { due: map (\d -> { task: d.task, picks: map _.at d.picks }) (foldl pickRow { due: [], at: 0.0 } rows).due
  , noticed: foldl mark [] rows
  }
  where
  since = max startedAt now - oldestWakeMs
  pickRow acc r =
    { at: acc.at + 1.0
    , due:
        if r.ts * 1000.0 < since then acc.due
        else case findIndex (\d -> d.task == r.task) acc.due of
          Nothing -> snoc acc.due { task: r.task, picks: [ this ] }
          Just i -> fromMaybe acc.due (modifyAt i (\d -> d { picks = snoc (filter _.question d.picks) this }) acc.due)
    }
    where
    this = { at: acc.at, question: r.kind == "question" }
  mark acc r = foldl (raise r.ts) acc ([ r.task ] <> r.tasks)
  raise ts acc name = case findIndex (\n -> n.task == name) acc of
    Nothing -> snoc acc { task: name, ts }
    Just i -> fromMaybe acc (modifyAt i (\n -> n { ts = max n.ts ts }) acc)

-- | One due notice of a task, judged: is it still true (`current`: a check-in while the task
-- | runs; a question with an id while the task asks and it is still open, `questionOpen`; any
-- | other kind while the task is in the phase it names, a question without an id while it asks);
-- | is it toasted (still true and not toasted before: once); and what the host does (`act`):
-- | - `skip`: acknowledged already (`ts` at or below `acked`);
-- | - `none`: the toast only: another session's task (`own` false) or wakes off (`wake` false),
-- |   so the notice stays for its own session or `wb wait`;
-- | - `steer`: into the running turn (`busy`), a question: its agent is blocked, and after the
-- |   ask timeout it guesses (a 20-minute review turn, #542); refused, the host holds it;
-- | - `hold`: anything else still true while busy: delivered once the turn ends (a prompt now
-- |   would queue and arrive after the task moved on);
-- | - `wake`: a turn for it now, once `wb ack` grants it;
-- | - `ack`: no longer true: acknowledged in silence (a review since discarded, accepted or sent
-- |   back);
-- | - `missed`: no longer true, but a question held through a busy turn (`heldAsk`) while its task
-- |   is open: acknowledged, and said if it timed out unanswered (#542; the host reads its answer).
noticePlan
  :: { kind :: String
     , hasId :: Boolean
     , phase :: String
     , questionOpen :: Boolean
     , ts :: Number
     , acked :: Number
     , own :: Boolean
     , wake :: Boolean
     , busy :: Boolean
     , toasted :: Boolean
     , heldAsk :: Boolean
     }
  -> { current :: Boolean, toast :: Boolean, act :: String }
noticePlan f = { current, toast: not skip && current && not f.toasted, act }
  where
  p = phaseOf { phase: f.phase, parked: false }
  asks = p.active && p.needsClaude
  current
    | f.kind == "checkin" = p.running
    | f.kind == "question" = asks && (not f.hasId || f.questionOpen)
    | otherwise = f.kind /= "" && f.phase == f.kind
  skip = f.ts <= f.acked
  act
    | skip = "skip"
    | not f.own || not f.wake = "none"
    | f.busy && current = if f.kind == "question" then "steer" else "hold"
    | current = "wake"
    | f.heldAsk && f.hasId && (p.active || p.needsClaude) = "missed"
    | otherwise = "ack"

-- | The safety net under the notices: a task of this session's waiting on Claude whose news never
-- | reached it (a lost or swallowed notice; on 2026-11-16 a failure sat unseen for an hour) wakes
-- | Claude anyway, once, after a minute of Claude being idle. `recent`: it waits on this session
-- | and no longer than `rewakeMaxMs` (older, it waited for a session long gone: wb status shows
-- | it), which also keeps the weekly jobs off. `candidate`: due a look now (idle, wakes on, a
-- | minute since it changed and since a dropped wake, `dropped`: ms, 0 for none). `due`: a
-- | candidate whose news Claude never had: `acked` (s) is below its newest notice (`seen` this
-- | load, -1 for none, and the task's own `noticed` mark, 0 for none: after a reload notify.jsonl's
-- | tail may not hold it, #844), or, with no notice at all, below the time it entered its phase.
-- | `kind`: the notice its phase stands for (a question while it asks).
rewakePlan
  :: { phase :: String
     , parked :: Boolean
     , mine :: Boolean
     , updatedAt :: Number
     , now :: Number
     , busy :: Boolean
     , wake :: Boolean
     , dropped :: Number
     , acked :: Number
     , seen :: Number
     , marked :: Number
     }
  -> { recent :: Boolean, candidate :: Boolean, due :: Boolean, kind :: String }
rewakePlan f = { recent, candidate, due: candidate && not had, kind }
  where
  p = phaseOf { phase: f.phase, parked: f.parked }
  age = f.now - f.updatedAt
  recent = p.waits && f.mine && age <= rewakeMaxMs
  candidate = not f.busy && f.wake && recent && age >= rewakeMs && f.now - f.dropped >= rewakeMs
  had =
    if f.seen >= 0.0 || f.marked /= 0.0 then f.acked >= max (max f.seen 0.0) f.marked
    else f.acked * 1000.0 >= f.updatedAt
  kind = if p.active && p.needsClaude then "question" else f.phase

rewakeMs :: Number
rewakeMs = 60.0 * 1000.0

rewakeMaxMs :: Number
rewakeMaxMs = 12.0 * 3600.0 * 1000.0
