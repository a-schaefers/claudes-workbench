-- | The coding gate's rules (#92, #94, #1322), judged the same way by both hosts: whether Claude
-- | (or OpenCode's driver) may edit a file in a task's worktree now, under the user's choice (`wb
-- | coding`, coding.json's `mode`) and the task's allowances (tasks/<t>/coding.json's `allowed`).
-- | The host finds the task from the path, reads both files, asks in its dialog and writes the
-- | allowance; the kernel only decides (kernel/BOUNDARY.md, *Rulings*).
module Kernel.Coding (codingGate, editTools) where

import Prelude

import Data.Array (findIndex, index)
import Data.Int (toNumber)
import Data.Maybe (Maybe(..))

-- | The file tools whose path field names the file an edit writes, each host's own: Claude Code's
-- | Edit, MultiEdit, Write and NotebookEdit, and OpenCode's edit and write (2.4.1's tools, `path`;
-- | its patch tool names its files inside the patch text, which the host would have to parse).
editTools :: Array { host :: String, tool :: String, field :: String }
editTools =
  [ { host: "claude-code", tool: "Edit", field: "file_path" }
  , { host: "claude-code", tool: "MultiEdit", field: "file_path" }
  , { host: "claude-code", tool: "Write", field: "file_path" }
  , { host: "claude-code", tool: "NotebookEdit", field: "notebook_path" }
  , { host: "opencode", tool: "edit", field: "path" }
  , { host: "opencode", tool: "write", field: "path" }
  ]

-- | An edit of a task's code, under the user's `mode` and the task's allowances (each: `used`, it
-- | was handed back; `when`, a one-task exception's condition was given; `started`, the exception's
-- | first edit was made). `verdict`, closed: `pass` (no gate: "Let Claude decide", a mode wb
-- | doesn't know, as `cli.coding_mode` reads it, or an allowance not yet handed back), `start`
-- | (pass, and the host stamps `started` on allowance `open`: a one-task exception is under way
-- | from its first edit, so the next hand-back spends it), `ask` ("Ask me first": the host's
-- | dialog, its yes recorded with `wb coding allow`), `refuse` ("Only when I say"). `open`: the
-- | first allowance not handed back, -1 for none.
codingGate
  :: { mode :: String
     , allowed :: Array { used :: Boolean, when :: Boolean, started :: Boolean }
     }
  -> { verdict :: String, open :: Number }
codingGate { mode, allowed }
  | mode /= "ask" && mode /= "only" = { verdict: "pass", open: -1.0 }
  | otherwise = case findIndex (not <<< _.used) allowed of
      Just k -> case index allowed k of
        Just a | a.when && not a.started -> { verdict: "start", open: toNumber k }
        _ -> { verdict: "pass", open: toNumber k }
      Nothing -> { verdict: if mode == "ask" then "ask" else "refuse", open: -1.0 }
