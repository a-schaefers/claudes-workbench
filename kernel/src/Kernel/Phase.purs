-- | What a task's phase means, judged the same way by both hosts. The phase sets come from
-- | Tables, which is generated from workbench/store.py, so neither host keeps a copy (#1356).
module Kernel.Phase (phaseOf) where

import Prelude

import Data.Array (elem)
import Kernel.Tables (tables)

-- | A task's phase and its raw `parked` mark (status.json) in, what that phase means out:
-- | `active` (store.ACTIVE), `running` (active and not waiting on Claude: the mod's RUNNING),
-- | `needsClaude` (store.NEEDS_CLAUDE), `waits` (needs Claude and not parked by a quit, as
-- | store.parked counts a mark only in store.PARKS, #698) and `closed` (store.CLOSED). A phase
-- | no table names (a newer wb's, or a typo) is none of them, the safe answer: nothing to wake
-- | for, nothing running, nothing finished.
phaseOf
  :: { phase :: String, parked :: Boolean }
  -> { active :: Boolean
     , running :: Boolean
     , needsClaude :: Boolean
     , waits :: Boolean
     , closed :: Boolean
     }
phaseOf { phase, parked } =
  { active
  , running: active && not needsClaude
  , needsClaude
  , waits: needsClaude && not (parked && elem phase tables.parks)
  , closed: elem phase tables.closed
  }
  where
  active = elem phase tables.active
  needsClaude = elem phase tables.needsClaude
