-- | The kernel's exports: exactly the rows of kernel/BOUNDARY.md's `## Exports`, re-exported here
-- | so esbuild bundles only what crosses into TypeScript (mod/hooks/core.ts).
module Kernel.Main
  ( module Kernel.Away
  , module Kernel.Owner
  , module Kernel.Phase
  , module Kernel.Tables
  ) where

import Kernel.Away (awayNow)
import Kernel.Owner (owns)
import Kernel.Phase (openCounts, phaseOf)
import Kernel.Tables (tables)
