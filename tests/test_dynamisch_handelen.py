"""
Unit tests voor AppDaemon-hulpfuncties in dynamisch_handelen.py.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "appdaemon" / "apps"))

from dynamisch_handelen import bouw_grafiek_slots, haal_grafiek_slots_uit_history_items


def _slot(start: datetime, duur_uren: float, label: str) -> dict:
    return {
        "start": start.isoformat(),
        "end": (start + timedelta(hours=duur_uren)).isoformat(),
        "label": label,
    }


def test_bouw_grafiek_slots_bewaart_laatste_zes_uur():
    nu = datetime(2026, 5, 24, 14, 0, tzinfo=timezone.utc)
    oud = _slot(nu - timedelta(hours=8), 1, "te-oud")
    recent = _slot(nu - timedelta(hours=5), 1, "recent")
    toekomst = _slot(nu + timedelta(hours=1), 1, "toekomst")

    grafiek_slots = bouw_grafiek_slots([oud, recent], [toekomst], nu)

    assert [slot["label"] for slot in grafiek_slots] == ["recent", "toekomst"]


def test_bouw_grafiek_slots_gebruikt_nieuwe_slot_bij_dubbele_tijd():
    nu = datetime(2026, 5, 24, 14, 0, tzinfo=timezone.utc)
    start = nu + timedelta(hours=1)
    vorig = _slot(start, 1, "vorig")
    nieuw = _slot(start, 1, "nieuw")

    grafiek_slots = bouw_grafiek_slots([vorig], [nieuw], nu)

    assert [slot["label"] for slot in grafiek_slots] == ["nieuw"]


def test_haal_grafiek_slots_uit_history_items_leest_recente_slots():
    nu = datetime(2026, 5, 24, 14, 0, tzinfo=timezone.utc)
    oud = _slot(nu - timedelta(hours=8), 1, "te-oud")
    recent_vroeg = _slot(nu - timedelta(hours=2), 1, "recent-vroeg")
    recent_laat = _slot(nu - timedelta(hours=2), 1, "recent-laat")
    te_laat_gepubliceerd = _slot(nu - timedelta(hours=1), 1, "na-slot-einde")

    history_items = [
        {
            "last_changed": (nu - timedelta(hours=7)).isoformat(),
            "attributes": {"slots": [oud]},
        },
        {
            "last_changed": (nu - timedelta(hours=2, minutes=30)).isoformat(),
            "attributes": {"slots": [recent_vroeg]},
        },
        {
            "last_changed": (nu - timedelta(hours=1, minutes=15)).isoformat(),
            "attributes": {"slots": [recent_laat]},
        },
        {
            "last_changed": (nu + timedelta(minutes=5)).isoformat(),
            "attributes": {"slots": [te_laat_gepubliceerd]},
        },
        {
            "last_changed": (nu - timedelta(hours=1)).isoformat(),
            "attributes": {"slots_grafiek": [recent_laat]},
        },
    ]

    grafiek_slots = haal_grafiek_slots_uit_history_items(history_items, nu)

    assert [slot["label"] for slot in grafiek_slots] == ["recent-laat"]
