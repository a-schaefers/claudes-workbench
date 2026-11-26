-- | Workbench mode's machine: what a conversation still lacks of the rules (what it was told
-- | against what it should know), which sessions resume in the mode, and when a task boundary
-- | compacts the driver's context. Both hosts follow it: the Claude Code mod's prompt.context,
-- | sayRules, keepMode and poll, and the OpenCode plugin's instructions and compaction (#1325, by
-- | the owner's ruling of 2026-11-25, for #1312 and #1315). The words are each host's, and so is
-- | reading the facts (notes.md, the roles, the coding mode, the context's usage).
module Kernel.Mode (Said, tellPlan, modesPlan, compactPlan) where

import Prelude

import Data.Array (any, elem, filter, foldl, snoc)

-- | What a conversation has been told, or should know: the user's notes (`""` none), that wakes
-- | are off, the Claude-coding mode (`decide`, the default, is said by nothing), the roles'
-- | summary (`""` none) and Dynamic workflows (`on` or `off`).
type Said = { notes :: String, wakeOff :: Boolean, coding :: String, routes :: String, workflows :: String }

-- | What to tell a conversation, and what it has been told after (`said`).
-- |
-- | At its first message (`first`; again after a compaction or /clear) the context blocks, in
-- | this order: `mode` while delegating, `coding` when the mode isn't `decide`, `notes`,
-- | `workflows` when on, `away` while the user is away, `wakeOff`, `routes` (the host adds it
-- | once any role has an API model); what /workbench's own first message said already (`was`,
-- | when `known`) isn't said again (#540); `said` is then `want`.
-- |
-- | Mid-conversation the rules it lacks now, in this order: `notes`, `wakeOff`, `coding`,
-- | `routes` (`routesChanged` once a summary was said), `workflows`. Notes and the wake-off note
-- | come once; the coding rule, the roles and Dynamic workflows again whenever they change, and
-- | only while delegating or with a task open (`any`): out of the mode with nothing open,
-- | nothing changes. Said again with `was` its own `said`, it says nothing.
tellPlan
  :: { first :: Boolean, want :: Said, was :: Said, known :: Boolean, any :: Boolean, delegating :: Boolean, away :: Boolean }
  -> { say :: Array String, said :: Said }
tellPlan f = if f.first then atFirst else midway
  where
  want = f.want
  was = f.was
  had p = f.known && p
  atFirst =
    { say: filter (_ /= "")
        [ when' f.delegating "mode"
        , when' (want.coding /= "decide" && not (had (was.coding == want.coding))) "coding"
        , when' (want.notes /= "" && not (had (was.notes /= ""))) "notes"
        , when' (want.workflows == "on" && not (had (was.workflows == "on"))) "workflows"
        , when' f.away "away"
        , when' (want.wakeOff && not (had was.wakeOff)) "wakeOff"
        , when' (want.routes /= "" && not (had (was.routes == want.routes))) "routes"
        ]
    , said: want
    }
  on = f.any
  midway =
    { say: filter (_ /= "")
        [ when' (want.notes /= "" && was.notes == "") "notes"
        , when' (want.wakeOff && not was.wakeOff) "wakeOff"
        , when' (on && want.coding /= was.coding) "coding"
        , when' (on && want.routes /= was.routes) (if was.routes == "" then "routes" else "routesChanged")
        , when' (on && want.workflows /= was.workflows) "workflows"
        ]
    , said:
        { notes: if was.notes /= "" then was.notes else want.notes
        , wakeOff: was.wakeOff || want.wakeOff
        , coding: if on then want.coding else was.coding
        , routes: if on then want.routes else was.routes
        , workflows: if on then want.workflows else was.workflows
        }
    }

when' :: Boolean -> String -> String
when' b s = if b then s else ""

-- | The sessions in workbench mode, by id (`modes`: when each turned it on, ms), so a session
-- | resumed in a new process comes back in it (#689): `kept`, whether one of this process's `ids`
-- | is among them; `modes`, as they are to be kept: none 30 days old, this process's own dropped,
-- | then, turning it on (`on`), each of its ids at `now`. A quit clears every id it has had.
modesPlan
  :: { modes :: Array { id :: String, at :: Number }, ids :: Array String, now :: Number, on :: Boolean }
  -> { kept :: Boolean, modes :: Array { id :: String, at :: Number } }
modesPlan f =
  { kept: any (\m -> elem m.id f.ids) f.modes
  , modes: (if f.on then \ms -> foldl (\acc id -> snoc acc { id, at: f.now }) ms f.ids else identity)
      (filter (\m -> f.now - m.at < modesKeepMs && not (elem m.id f.ids)) f.modes)
  }

modesKeepMs :: Number
modesKeepMs = 30.0 * 24.0 * 3600.0 * 1000.0

-- | A task boundary compacts the driver's context early, at a clean point instead of mid-review:
-- | one of this session's open tasks closed since the last look (`was` against `open`), in
-- | workbench mode only (a task closing compacted every session's, delegating or not: the
-- | 2026-11-16 audit). `boundary` is the mark kept between looks: set by a close, cleared out of
-- | the mode and once checked. `check`: read the context's usage now, Claude idle and none of its
-- | tasks waiting on it. `compact`, with the usage read (`tokens`, 0 or less unread; `window`; the
-- | engine's own auto-compact `threshold` when `autoCompact`, 0 none): `tokens` at or past
-- | `percent` (`WB_COMPACT_AT`; unset, `percentSet` false: half; 0: never) of the way to the
-- | engine's own compaction.
compactPlan
  :: { on :: Boolean
     , was :: Array String
     , open :: Array String
     , boundary :: Boolean
     , busy :: Boolean
     , waiting :: Boolean
     , percentSet :: Boolean
     , percent :: Number
     , tokens :: Number
     , window :: Number
     , autoCompact :: Boolean
     , threshold :: Number
     }
  -> { boundary :: Boolean, check :: Boolean, compact :: Boolean }
compactPlan f = { boundary: marked && not check, check, compact }
  where
  marked = f.on && (f.boundary || any (\n -> not (elem n f.open)) f.was)
  check = marked && not f.busy && not f.waiting
  at = if f.percentSet then f.percent / 100.0 else 0.5
  limit = if f.autoCompact && f.threshold /= 0.0 then f.threshold else f.window
  compact = check && at /= 0.0 && f.tokens > 0.0 && f.tokens >= at * limit
