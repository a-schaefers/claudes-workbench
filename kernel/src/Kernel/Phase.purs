-- | What a task's phase means, judged the same way by both hosts, and the tally of a list of
-- | tasks by phase. The phase sets come from Tables, which is generated from workbench/store.py,
-- | so neither host keeps a copy (#1356, #1325's M1).
module Kernel.Phase (Facts, Meaning, phaseOf, openCounts) where

import Prelude

import Data.Array (elem, filter, foldl, length, mapMaybe)
import Data.Maybe (Maybe(..))
import Kernel.Tables (tables)

type Facts = { phase :: String, parked :: Boolean }

type Meaning =
  { active :: Boolean
  , running :: Boolean
  , working :: Boolean
  , needsClaude :: Boolean
  , waits :: Boolean
  , stopped :: Boolean
  , done :: Boolean
  , closed :: Boolean
  }

-- | A task's phase and its raw `parked` mark (status.json) in, what that phase means out:
-- | `active` (store.ACTIVE), `running` (active and not waiting on Claude: queued counts, as the
-- | mod's status line has it), `working` (a round under way, store.WORKING: where its clock, its
-- | context and its estimate mean something), `needsClaude` (store.NEEDS_CLAUDE), `waits` (needs
-- | Claude and not parked by a quit, as store.parked counts a mark only in store.PARKS, #698),
-- | `stopped` (stuck or failed, store.STOPPED: Claude steers it next), `done` (its work done: a
-- | review, or closed) and `closed` (store.CLOSED). A phase no table names (a newer wb's, or a
-- | typo) is none of them, the safe answer: nothing to wake for, nothing running, nothing finished.
phaseOf :: Facts -> Meaning
phaseOf { phase, parked } =
  { active
  , running: active && not needsClaude
  , working: elem phase tables.working
  , needsClaude
  , waits: needsClaude && not (parked && elem phase tables.parks)
  , stopped
  , done: closed || (elem phase tables.parks && not stopped)
  , closed
  }
  where
  active = elem phase tables.active
  needsClaude = elem phase tables.needsClaude
  stopped = elem phase tables.stopped
  closed = elem phase tables.closed

-- | A list of tasks tallied by phase: one row for each open phase that has a task, in
-- | store.OPEN_ORDER (the work under way first), with how many are in it (`n`), how many of those
-- | wait on Claude (`waiting`: not parked), and whether the phase is `active` or `stopped`; `done`
-- | counts the tasks whose work is done (a review, or closed). The pane's group header and the
-- | status line both draw from it, so the two never count differently (#1048: "2 awaiting review"
-- | once said so of a review and a stuck task). A phase no table names is in no row.
openCounts
  :: { tasks :: Array Facts }
  -> { counts ::
         Array
           { phase :: String
           , n :: Number
           , waiting :: Number
           , active :: Boolean
           , stopped :: Boolean
           }
     , done :: Number
     }
openCounts { tasks } =
  { counts: mapMaybe row tables.openOrder
  , done: count (\t -> (phaseOf t).done) tasks
  }
  where
  row p =
    let
      these = filter (\t -> t.phase == p) tasks
      m = phaseOf { phase: p, parked: false }
    in
      if length these == 0 then Nothing
      else Just
        { phase: p
        , n: count (const true) these
        , waiting: count (\t -> (phaseOf t).waits) these
        , active: m.active
        , stopped: m.stopped
        }

-- | How many of `xs` hold, as a Number: no Int crosses the wire (kernel/BOUNDARY.md, *Wire types*)
count :: forall a. (a -> Boolean) -> Array a -> Number
count f = foldl (\acc x -> if f x then acc + 1.0 else acc) 0.0
