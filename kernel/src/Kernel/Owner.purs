-- | Whose task it is: which tasks a driver session owns, judged the same way by both hosts and by
-- | wb's own `cli._scope` (its Python twin, held to the same goldens). The host gathers the facts
-- | (its session ids, its directory, whether the task's recorded session is gone); the kernel
-- | only compares them (#1325's M2, #1309's driver identity).
module Kernel.Owner (Facts, owns) where

import Prelude

import Data.Array (all, elem, filter, length, null, zipWith)
import Data.String.Common (split, toLower)
import Data.String.Pattern (Pattern(..))

type Facts =
  { session :: String
  , ids :: Array String
  , driver :: String
  , host :: String
  , gone :: Boolean
  , cwd :: String
  , repo :: String
  , subdir :: String
  , here :: String
  , home :: String
  , fold :: Boolean
  }

-- | A task's keys (task.json: the driver `session` that started it, `driver` as store.driver_of
-- | names it, `opencode` or `claude` or `""` for none, where `wb start` ran, `cwd`, its `repo` and
-- | `subdir`) and the session's (`ids`: every id its process has had, as /clear starts another;
-- | `host`: which driver the session is; `here`: its directory; `home`; `fold`: its file system
-- | ignores case) in; whether the session owns it (`mine`) and whether it would were the task's
-- | recorded session gone (`claims`: what `wb adopt` may take over) out.
-- |
-- | A task with a session is that session's, wherever it ran. Once that session is gone (`gone`),
-- | a session of the same driver near it adopts it: one whose directory holds the task's `cwd` or
-- | repo, or lies inside the task's spot (its package, repo/subdir; else its `cwd` inside the repo;
-- | else the repo: a monorepo's sibling package was woken for another's review, #695), or is
-- | exactly where `wb start` ran (#1403). Another driver's task is never adopted by directory
-- | (#1309): Claude Code and OpenCode never take each other's. A session in the home directory or
-- | `/` claims none by directory (a task started from ~ belonged to every session under it, the
-- | 0.90 review). A task without a session, or a session with no ids, goes by directory alone; one
-- | with no key at all, or a session with no directory, is everyone's. Paths compare by whole
-- | components (`/x/ab` is not inside `/x/a`), in lower case where `fold` says so.
owns :: Facts -> { mine :: Boolean, claims :: Boolean }
owns f = { mine: judge f.gone, claims: judge true }
  where
  judge gone
    | f.session /= "" && not (null f.ids) =
        elem f.session f.ids || (gone && f.driver == f.host && (near || ranHere))
    | f.here == "" || (f.cwd == "" && f.repo == "") = true
    | otherwise = near
  parts p = filter (\c -> c /= "" && c /= ".") (split (Pattern "/") (if f.fold then toLower p else p))
  within a b = length pa <= length pb && all identity (zipWith (==) pa pb)
    where
    pa = parts a
    pb = parts b
  at = parts f.here
  broad = null at || (f.home /= "" && at == parts f.home)
  spot
    | f.subdir /= "" = f.repo <> "/" <> f.subdir
    | f.cwd /= "" && within f.repo f.cwd = f.cwd
    | otherwise = f.repo
  near = not broad &&
    ((f.cwd /= "" && within f.here f.cwd) || (f.repo /= "" && (within f.here f.repo || within spot f.here)))
  ranHere = f.here /= "" && f.cwd /= "" && parts f.cwd == parts f.here
