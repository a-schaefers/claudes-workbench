-- | The kernel's properties, on seeded facts: `python3 scripts/kernel.py props` compiles this with
-- | kernel/src and runs it under Bun (props.test.ts beside it). Pure, as the kernel is: no effect,
-- | no clock, no chance but the seed, so a seed and a case number name a failure for good. The
-- | generator is Park and Miller's minimal standard, by Schrage's method so no product leaves an Int.
module Props (props) where

import Prelude

import Data.Array (all, any, concatMap, elem, filter, foldl, index, intersect, length, mapWithIndex, nub, range, sort)
import Data.Int (toNumber)
import Data.Maybe (fromMaybe)
import Kernel.Away (awayNow)
import Kernel.Owner (owns)
import Kernel.Phase (openCounts, phaseOf)
import Kernel.Tables (tables)

-- | `n` facts for each seed: every failure, each naming its seed, its case, the facts and the
-- | property; `cases` is how many facts were judged (each case judges a phase, a list of tasks,
-- | an ownership and an away).
props :: { seeds :: Array Int, n :: Int } -> { cases :: Int, failures :: Array String }
props { seeds, n } =
  { cases: length seeds * max 0 n
  , failures: tableFailures <> concatMap (\seed -> run seed n) seeds
  }

-- | What the tables themselves must say, whatever the facts: a closed task is never active, the
-- | one phase both active and needing Claude is `asking`, a parked mark counts only in phases
-- | that need Claude, openOrder names every open phase once, a working phase is active and a
-- | stopped one can be parked.
tableFailures :: Array String
tableFailures =
  check (intersect tables.closed tables.active == []) "tables: closed and active share a phase"
    <> check (sort (intersect tables.active tables.needsClaude) == [ "asking" ])
      "tables: active and needsClaude share something other than asking"
    <> check (intersect tables.parks tables.needsClaude == tables.parks)
      "tables: a parked mark counts in a phase that doesn't need Claude"
    <> check (sort tables.openOrder == sort (nub (tables.active <> tables.needsClaude)) && nub tables.openOrder == tables.openOrder)
      "tables: openOrder is not every open phase once"
    <> check (intersect tables.working tables.active == tables.working) "tables: a working phase isn't active"
    <> check (intersect tables.stopped tables.parks == tables.stopped) "tables: a stopped phase can't be parked"

run :: Int -> Int -> Array String
run seed n = (foldl step { s: start seed, out: [] } (if n < 1 then [] else range 1 n)).out
  where
  step acc i =
    let
      s1 = next acc.s
      s2 = next s1
      s3 = next s2
      facts = { phase: phaseFrom s1 s2, parked: s3 `mod` 2 == 0 }
      tl = tasksFrom (next s3)
      ow = ownerFrom tl.s
      aw = awayFrom ow.s tl.tasks
      say :: forall a b. Show a => Show b => a -> b -> String -> String
      say x got what = "seed " <> show seed <> " case " <> show i <> ": " <> show x <> " gave " <> show got
        <> ": "
        <> what
    in
      { s: aw.s
      , out: acc.out <> map (say facts (phaseOf facts)) (judge facts)
          <> map (say tl.tasks (openCounts { tasks: tl.tasks })) (judgeCounts tl.tasks)
          <> map (say ow.facts (owns ow.facts)) (judgeOwner ow.facts)
          <> map (say aw.facts (awayNow aw.facts)) (judgeAway aw.facts)
      }

-- | The phase round-trips: each flag says which of wb's sets the phase is in (store.py, through
-- | Tables), and a phase no set names is none of them.
judge :: { phase :: String, parked :: Boolean } -> Array String
judge facts@{ phase, parked } =
  check (p.active == elem phase tables.active) "active is not store.ACTIVE's"
    <> check (p.needsClaude == elem phase tables.needsClaude) "needsClaude is not store.NEEDS_CLAUDE's"
    <> check (p.closed == elem phase tables.closed) "closed is not store.CLOSED's"
    <> check (p.working == elem phase tables.working) "working is not store.WORKING's"
    <> check (p.stopped == elem phase tables.stopped) "stopped is not store.STOPPED's"
    <> check (p.running == (p.active && not p.needsClaude)) "running is not active and not needing Claude"
    <> check (p.waits == (p.needsClaude && not (parked && elem phase tables.parks)))
      "waits is not needing Claude unless parked where a mark counts (store.parked)"
    <> check (p.done == (p.closed || (elem phase tables.parks && not p.stopped))) "done is not a review or closed"
    <> check (not (p.done && p.active)) "done and still active"
    <> check (not (p.closed && (p.active || p.needsClaude))) "closed and open at once"
    <> check (known || p == none) "a phase no table names is not none of them"
  where
  p = phaseOf facts
  known = elem phase phases
  none = { active: false, running: false, working: false, needsClaude: false, waits: false, stopped: false, done: false, closed: false }

-- | The tally holds what each task's phaseOf says: a row for each open phase with a task, in
-- | openOrder, its counts those of its tasks, and `done` the tasks whose work is done.
judgeCounts :: Array { phase :: String, parked :: Boolean } -> Array String
judgeCounts tasks =
  check (map _.phase c.counts == filter (\p -> any (\t -> t.phase == p) tasks) tables.openOrder)
    "openCounts' rows are not the open phases with a task, in openOrder"
    <> check (all (\r -> r.n == num (filter (\t -> t.phase == r.phase) tasks)) c.counts) "a row's n is not its tasks"
    <> check (all (\r -> r.waiting == num (filter (\t -> t.phase == r.phase && (phaseOf t).waits) tasks)) c.counts)
      "a row's waiting is not its tasks that wait"
    <> check (all (\r -> r.active == (bare r.phase).active && r.stopped == (bare r.phase).stopped) c.counts)
      "a row's flags are not its phase's"
    <> check (c.done == num (filter (\t -> (phaseOf t).done) tasks)) "done is not the tasks done"
    <> check (foldl (\a r -> a + r.n) 0.0 c.counts == num (filter (\t -> elem t.phase tables.openOrder) tasks))
      "the rows don't count every open task once"
  where
  c = openCounts { tasks }
  bare phase = phaseOf { phase, parked: false }

type OwnerFacts =
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

-- | Whose task it is: the session's own id's always; a live session's never; another driver's
-- | never by directory (#1309); in home or /, nothing by directory but where wb start ran (#1403);
-- | whole components only (/x/ab is not inside /x/a); what is mine is mine to claim.
judgeOwner :: OwnerFacts -> Array String
judgeOwner f =
  check (not (byIds && own) || o.mine) "a task of the session's own id is not mine"
    <> check (not (byIds && not own && f.driver /= f.host) || not o.mine && not o.claims)
      "another driver's task is mine by directory"
    <> check (not (byIds && not own && not f.gone) || not o.mine) "a live session's task is mine"
    <> check (not (broad && not byIds && (f.cwd /= "" || f.repo /= "")) || not o.mine)
      "home or / claims a task without a session by directory"
    <> check (not (broad && byIds && not own && f.cwd /= f.here) || not o.claims)
      "home or / claims another session's task that didn't start there"
    <> check (not (f.here == "/x/a" && not byIds && f.repo == "/x/ab" && (f.cwd == "" || f.cwd == "/x/ab")) || not o.mine)
      "/x/ab is inside /x/a"
    <> check (not o.mine || o.claims) "mine but not mine to claim"
  where
  o = owns f
  byIds = f.session /= "" && length f.ids > 0
  own = elem f.session f.ids
  broad = f.here == "/" || f.here == f.home

type AwayFacts = { webAt :: Number, now :: Number, tasks :: Array { phase :: String, parked :: Boolean, mine :: Boolean } }

-- | Away: never without the web chat; within six hours of it; after them, while a task of the
-- | session's is open (active, or waiting on Claude unparked).
judgeAway :: AwayFacts -> Array String
judgeAway f =
  check (f.webAt > 0.0 || not a) "away with no web chat"
    <> check (not (f.webAt > 0.0 && f.now - f.webAt < sixHours) || a) "not away within six hours of the web chat"
    <> check (not (f.webAt > 0.0 && f.now - f.webAt >= sixHours) || a == any open f.tasks)
      "past six hours, away is not a task of the session's open"
  where
  a = awayNow f
  sixHours = 6.0 * 3600.0 * 1000.0
  open t = t.mine && (p.active || p.waits)
    where
    p = phaseOf { phase: t.phase, parked: t.parked }

check :: Boolean -> String -> Array String
check ok what = if ok then [] else [ what ]

num :: forall a. Array a -> Number
num = foldl (\a _ -> a + 1.0) 0.0

phases :: Array String
phases = nub (tables.active <> tables.needsClaude <> tables.parks <> tables.closed)

-- | Near misses and strangers: a case change, a space, non-ASCII, an astral character, a phase
-- | with something after it.
fragments :: Array String
fragments = [ "", "x", "R", "-", " ", "\x0000e9", "\x01f527", "review", "Running", "queued", "Accepted" ]

-- | A phase wb has half the time, one with a fragment after it a quarter, two fragments the rest.
phaseFrom :: Int -> Int -> String
phaseFrom a b = case a `mod` 4 of
  0 -> pick phases b
  1 -> pick phases b
  2 -> pick phases b <> pick fragments (next b)
  _ -> pick fragments b <> pick fragments (next b)

-- | Up to seven tasks, each phase drawn as phaseFrom draws one.
tasksFrom :: Int -> { s :: Int, tasks :: Array { phase :: String, parked :: Boolean } }
tasksFrom s0 = foldl add { s: next s0, tasks: [] } (range 1 (s0 `mod` 8))
  where
  add acc _ =
    let
      a = next acc.s
      b = next a
      c = next b
    in
      { s: c, tasks: acc.tasks <> [ { phase: phaseFrom a b, parked: c `mod` 3 == 0 } ] }

-- | Paths a session and a task may name: nested ones, a near miss (/x/ab beside /x/a), home and
-- | /, a trailing slash, another case.
places :: Array String
places = [ "", "/", "/x", "/x/a", "/x/ab", "/x/a/b", "/x/a/", "/X/A", "/w/me", "/w/me/p" ]

ownerFrom :: Int -> { s :: Int, facts :: OwnerFacts }
ownerFrom s0 =
  { s: r 10
  , facts:
      { session: pick [ "", "s1", "s2", "s3" ] (r 0)
      , ids: fromMaybe [] (index [ [], [ "s1" ], [ "s1", "s2" ] ] (r 1 `mod` 3))
      , driver: pick [ "claude", "opencode", "", "Claude" ] (r 2)
      , host: pick [ "claude", "opencode" ] (r 3)
      , gone: r 4 `mod` 2 == 0
      , cwd: pick places (r 5)
      , repo: pick places (r 6)
      , subdir: pick [ "", "", "a", "b/c" ] (r 7)
      , here: pick places (r 8)
      , home: "/w/me"
      , fold: r 9 `mod` 4 == 0
      }
  }
  where
  r k = foldl (\x _ -> next x) s0 (range 0 k)

awayFrom :: Int -> Array { phase :: String, parked :: Boolean } -> { s :: Int, facts :: AwayFacts }
awayFrom s0 tasks =
  { s: c
  , facts:
      { webAt: if a `mod` 5 == 0 then 0.0 else 1.0e12
      , now: 1.0e12 + 1000.0 * toNumber (b `mod` 43200)
      , tasks: mapWithIndex (\i t -> { phase: t.phase, parked: t.parked, mine: (c / 7 + i) `mod` 2 == 0 }) tasks
      }
  }
  where
  a = next s0
  b = next a
  c = next b

pick :: Array String -> Int -> String
pick xs r = fromMaybe "" (index xs (r `mod` max 1 (length xs)))

modulus :: Int
modulus = 2147483647

start :: Int -> Int
start seed = 1 + (seed `mod` (modulus - 1))

next :: Int -> Int
next s = if t > 0 then t else t + modulus
  where
  t = 48271 * (s `mod` 44488) - 3399 * (s / 44488)
