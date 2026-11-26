-- | The kernel's exports: exactly the rows of kernel/BOUNDARY.md's `## Exports`, re-exported here
-- | so esbuild bundles only what crosses into TypeScript (mod/hooks/core.ts).
module Kernel.Main
  ( module Kernel.Coding
  , module Kernel.Gate
  , module Kernel.Phase
  , module Kernel.Tables
  ) where

import Kernel.Coding (codingGate, editTools)
import Kernel.Gate (gateOf)
import Kernel.Phase (phaseOf)
import Kernel.Tables (tables)
