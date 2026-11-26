-- | Whether the user is away from the terminal, so a session's news and dialogs go to the web
-- | chat too: the Claude Code mod and the OpenCode plugin judge it alike (#1325's M2, for #1319).
module Kernel.Away (awayNow) where

import Prelude

import Data.Array (any)
import Kernel.Phase (phaseOf)

-- | The user counts as away this long after they last used the web chat: a web message, or a
-- | page in use (chat/away.json's `at`, written by `wb chat serve`'s /api/active, #959).
awayMs :: Number
awayMs = 6.0 * 3600.0 * 1000.0

-- | When the user last used the web chat (`webAt`, ms; 0 or less: never, or since back at the
-- | terminal), the time now and the session's tasks (`mine`: the session owns it, Kernel.Owner) in;
-- | whether they are away out. Away for `awayMs` after the web chat's last use, or for as long as
-- | one of the session's own tasks is open: running, or waiting on Claude and not parked by a
-- | quit. A night's batch outlasts six hours, and its permission prompts then waited in the
-- | terminal until morning (#110); backlog tasks alone held it, so one long task asked for from the
-- | phone was reported in the terminal only (#1402).
awayNow
  :: { webAt :: Number
     , now :: Number
     , tasks :: Array { phase :: String, parked :: Boolean, mine :: Boolean }
     }
  -> Boolean
awayNow { webAt, now, tasks } =
  webAt > 0.0 && (now - webAt < awayMs || any open tasks)
  where
  open t = t.mine && (p.active || p.waits)
    where
    p = phaseOf { phase: t.phase, parked: t.parked }
