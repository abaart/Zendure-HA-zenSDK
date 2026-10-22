# UTC price planning verification

The 72-hour price planner could repeatedly append the same forecast slot when
its horizon reached the October daylight saving transition. Local datetime keys
with the same ZoneInfo compared the two occurrences of 02:00 as equal.

`DynamischHandelen._haal_prijsslots()` now uses UTC for source and forecast keys,
comparisons and durations, then converts output to Europe/Amsterdam. The loop
returns an empty result and logs an error when time does not advance or the
72-hour planning exceeds 290 slots.

Validation:
- `python3 -m pytest -q tests/test_dynamisch_handelen.py`: 47 passed.
- Regression cases cover spring and autumn transitions, zero-duration forecast
  slots and the slot-count limit. The horizon assertion uses elapsed UTC hours.
- The same scoped method correction was deployed to the existing production
  file on 2026-10-22; other production-only changes were retained.
- All three strategy entities updated at 16:23:45 and 16:25:50 Europe/Amsterdam.
- All 96 next-day strategy prices matched the Nord Pool source.
- AppDaemon used approximately 175–255 MiB during the observation, versus
  2.3–2.6 GB before OOM termination. No new failures were seen over four minutes.
  Long-term memory stability has not been established.
- Production recovery copy: /config/utc-fix-backup-20261022/dynamisch_handelen.py.
