-- | The kernel's properties, on seeded facts: `python3 scripts/kernel.py props` compiles this with
-- | kernel/src and runs it under Bun (props.test.ts beside it). Pure, as the kernel is: no effect,
-- | no clock, no chance but the seed, so a seed and a case number name a failure for good. The
-- | generator is Park and Miller's minimal standard, by Schrage's method so no product leaves an Int.
module Props (props) where

import Prelude

import Data.Array (concatMap, elem, foldl, index, intersect, length, nub, range, sort)
import Data.Maybe (fromMaybe)
import Kernel.Phase (phaseOf)
import Kernel.Tables (tables)

-- | `n` facts for each seed: every failure, each naming its seed, its case, the facts and the
-- | property; `cases` is how many facts were judged.
props :: { seeds :: Array Int, n :: Int } -> { cases :: Int, failures :: Array String }
props { seeds, n } =
  { cases: length seeds * max 0 n
  , failures: tableFailures <> concatMap (\seed -> run seed n) seeds
  }

-- | What the tables themselves must say, whatever the facts: a closed task is never active, the
-- | one phase both active and needing Claude is `asking`, and a parked mark counts only in phases
-- | that need Claude.
tableFailures :: Array String
tableFailures =
  check (intersect tables.closed tables.active == []) "tables: closed and active share a phase"
    <> check (sort (intersect tables.active tables.needsClaude) == [ "asking" ])
      "tables: active and needsClaude share something other than asking"
    <> check (intersect tables.parks tables.needsClaude == tables.parks)
      "tables: a parked mark counts in a phase that doesn't need Claude"

run :: Int -> Int -> Array String
run seed n = (foldl step { s: start seed, out: [] } (if n < 1 then [] else range 1 n)).out
  where
  step acc i =
    let
      s1 = next acc.s
      s2 = next s1
      s3 = next s2
      facts = { phase: phaseFrom s1 s2, parked: s3 `mod` 2 == 0 }
      say what = "seed " <> show seed <> " case " <> show i <> ": " <> show facts <> " gave "
        <> show (phaseOf facts) <> ": "
        <> what
    in
      { s: s3, out: acc.out <> map say (judge facts) }

-- | The phase round-trips: each flag says which of wb's sets the phase is in (store.py, through
-- | Tables), and a phase no set names is none of them.
judge :: { phase :: String, parked :: Boolean } -> Array String
judge facts@{ phase, parked } =
  check (p.active == elem phase tables.active) "active is not store.ACTIVE's"
    <> check (p.needsClaude == elem phase tables.needsClaude) "needsClaude is not store.NEEDS_CLAUDE's"
    <> check (p.closed == elem phase tables.closed) "closed is not store.CLOSED's"
    <> check (p.running == (p.active && not p.needsClaude)) "running is not active and not needing Claude"
    <> check (p.waits == (p.needsClaude && not (parked && elem phase tables.parks)))
      "waits is not needing Claude unless parked where a mark counts (store.parked)"
    <> check (not (p.closed && (p.active || p.needsClaude))) "closed and open at once"
    <> check (known || p == none) "a phase no table names is not none of them"
  where
  p = phaseOf facts
  known = elem phase phases
  none = { active: false, running: false, needsClaude: false, waits: false, closed: false }

check :: Boolean -> String -> Array String
check ok what = if ok then [] else [ what ]

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
