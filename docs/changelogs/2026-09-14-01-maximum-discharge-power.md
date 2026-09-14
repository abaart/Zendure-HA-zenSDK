---
title: "Maximum discharge power in quarter-hour plans"
summary: "The battery planner now accounts for the additional energy available at the configured maximum discharge power."
---

## Battery planning

The planner can now choose 2400 W when the additional discharge is worthwhile.
Previously, rounding could make 2250 W and 2400 W appear to deliver the same
energy during a quarter-hour. The maximum-power calculation now counts the
additional energy and preserves the remaining battery energy between slots.

The planner keeps the existing energy resolution. Lower discharge powers remain
available when price differences, battery energy, or penalties favour a lower power.
