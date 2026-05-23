---
name: zendure-dynamic-strategy-analysis
description: >
  Analyze and safely change the Zendure Home Assistant dynamic trading strategy.

  TRIGGER THIS SKILL WHEN:
  - Changing AppDaemon dynamic trading behavior
  - Updating the DP optimizer in appdaemon/apps/strategie_dp.py
  - Adding or changing Home Assistant helpers for the dynamic strategy
  - Modifying the strategy dashboard or sensor attributes
  - Investigating thermal, SoC, derating, C-value, or price-spread behavior
  - Adding regression tests for strategy decisions

  SYMPTOMS:
  - A helper exists in YAML but is not read by AppDaemon
  - The DP returns fields that the dashboard does not show
  - A strategy attribute is renamed without backwards compatibility
  - Thermal penalties affect the wrong SoC range
  - Tests check only actions and miss metadata fields
metadata:
  version: 1
  repository: Zendure-HA-zenSDK
---

# Zendure Dynamic Strategy Analysis

Use this workflow for changes around `sensor.dynamisch_handelsstrategie`, the DP optimizer, thermal modeling, SoC limits, helper entities, and Lovelace dashboard cards.

## Fast Orientation

Start by checking the current worktree, because this repository often has active local edits:

```bash
git status --short
```

Then search with `rg` before reading whole files:

```bash
rg -n "thermisch|temp_limiet|overtemp|warmte|soc_drempel|derating|spread" appdaemon/apps tests "Dutch (NL) Integration"
```

For thermal or SoC changes, inspect these files in this order:

1. `appdaemon/apps/strategie_dp.py`
2. `appdaemon/apps/dynamisch_handelen.py`
3. `Dutch (NL) Integration/packages/zendure_local_nl.yaml`
4. `Dutch (NL) Integration/dashboard_strategie.yaml`
5. `tests/test_strategie_dp.py`

## System Map

The dynamic strategy has a five-step data flow:

1. HA helpers are declared in `Dutch (NL) Integration/packages/zendure_local_nl.yaml`.
2. AppDaemon reads helpers in `appdaemon/apps/dynamisch_handelen.py`.
3. The pure DP algorithm runs in `appdaemon/apps/strategie_dp.py`.
4. AppDaemon publishes `sensor.dynamisch_handelsstrategie` with schema attributes.
5. The dashboard in `Dutch (NL) Integration/dashboard_strategie.yaml` reads helper entities and sensor attributes.

When adding a setting, update all five layers unless there is a specific reason not to.

## Thermal Analysis Pattern

For battery-temperature behavior, trace both the decision logic and the visible metadata.

In `strategie_dp.py`, inspect:

- `los_dp_op(...)` function parameters and docstring
- thermal normalization near `temp_limiet_c`, `temp_penalty_factor`, and `temp_soc_drempel_pct`
- `voorspel_temp_na_c(...)`
- `overtemp_penalty_eur(...)`
- temperature grid sizing: `temp_min`, `temp_max`, `n_temp`
- backwards-pass calls that subtract `kosten_temp`
- forward extraction fields such as `overtemp_penalty_eur`, `temp_limiet_c`, and `temp_limiet_actief`
- `herbereken_modelvelden(...)`, because plateau spreading can rewrite model fields after the first extraction

Check both paths whenever a field changes:

- DP optimization path: what action is chosen?
- published metadata path: what does the dashboard/test see?

## Helper Change Checklist

When adding a helper like a new temperature limit:

1. Add the `input_number` or other helper in `Dutch (NL) Integration/packages/zendure_local_nl.yaml`.
2. Add a `listen_state(...)` trigger in `DynamischHandelen.initialize()`.
3. Read the helper in `_haal_thermische_config(...)` or the relevant config reader.
4. Pass the value into `los_dp_op(...)`.
5. Publish it as a sensor attribute if the dashboard or automations should observe it.
6. Add it to the dashboard settings card.
7. Add it to the dashboard status/attribute card if useful.
8. Update markdown explanation text.
9. Add or update tests.

Prefer backwards-compatible aliases for existing sensor attributes when dashboards or automations may already depend on them.

## Temperature Limit Pattern

If there are different limits by SoC range, centralize limit selection in the DP instead of duplicating checks at call sites.

Good local shape:

```python
def actieve_temp_limiet_c(soc_kwh: float) -> float:
    soc_pct = soc_kwh / max_kwh * 100.0 if max_kwh > 0 else 0.0
    return hoge_soc_limiet if soc_pct >= temp_soc_drempel_pct else lage_soc_limiet
```

Then use the selected limit in:

- `overtemp_penalty_eur(...)`
- result metadata fields
- `temp_min` and `temp_max` grid bounds
- any recomputation after plateau spreading

Expose enough metadata to debug the decision later. At minimum, publish:

- active limit for the slot
- whether high-SoC limit is active
- over-temperature penalty
- before/after pack temperature

## Test Strategy

Tests should cover behavior and metadata.

Useful test patterns:

- high SoC with a hot pack should avoid charging above the high-SoC thermal limit
- low SoC with a separate low-SoC limit should still produce an over-temperature penalty
- `temp_penalty_factor=0.0` should leave decisions unaffected by over-temperature cost
- backwards-compatible aliases should match new attribute names
- metadata fields should remain present after plateau spreading

Run focused tests first:

```bash
pytest tests/test_strategie_dp.py -q
```

If AppDaemon integration code changed, also run any broader test command already used by the repo.

## Common Pitfalls

- Do not update only `strategie_dp.py`; helpers, AppDaemon config, dashboard, and tests usually need matching changes.
- Do not rename published attributes without compatibility unless explicitly requested.
- Do not forget `herbereken_modelvelden(...)`; it can overwrite fields calculated earlier.
- Do not assume the SoC percentage in DP is the visible Zendure percentage. DP SoC is normalized to the configured hardware window, then AppDaemon maps it back to real battery percentage before publishing.
- Do not treat thermal model fields as active when `batterij_temp_start_c` is `None`.
- Do not use ad hoc string edits for YAML when a small structured patch is enough; keep indentation exact.
