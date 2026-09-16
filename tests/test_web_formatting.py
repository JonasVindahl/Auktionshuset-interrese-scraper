"""Tests for tids- og prisformatering.

Tidsberegningerne lever i Python og ikke i SQL, fordi ``ends_at`` gemmes som
ISO med tidszone-offset mens SQLites ``datetime('now')`` bruger mellemrum som
separator. ``T`` sorterer efter mellemrum, så en strengsammenligning ville
melde at alting ligger i fremtiden.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from auction_hunter.web.formatting import (
    date_label, kr, parse_dt, rel_past, time_left, timestamp,
)

UTC = timezone.utc


def iso_in(**delta) -> str:
    return (datetime.now(UTC) + timedelta(**delta)).isoformat(timespec="seconds")


# -- parse_dt --------------------------------------------------------------

def test_parse_dt_antager_utc_uden_tidszone():
    dt = parse_dt("2026-09-16T12:00:00")
    assert dt is not None and dt.tzinfo is UTC


def test_parse_dt_bevarer_offset():
    dt = parse_dt("2026-09-16T14:30:00+02:00")
    assert dt is not None and dt.utcoffset() == timedelta(hours=2)


@pytest.mark.parametrize("value", [None, "", "noget vrøvl", "2026-13-45"])
def test_parse_dt_paa_ugyldigt_input(value):
    assert parse_dt(value) is None


def test_timestamp_paa_ugyldigt_input():
    assert timestamp("vrøvl") == 0


# -- time_left -------------------------------------------------------------

def test_time_left_haster_under_seks_timer():
    text, css, secs = time_left(iso_in(hours=3))
    assert text.startswith("Slutter om")
    assert css == "urgent"
    assert secs > 0


def test_time_left_snart_under_et_doegn():
    _text, css, _secs = time_left(iso_in(hours=12))
    assert css == "soon"


def test_time_left_roligt_over_et_doegn():
    text, css, _secs = time_left(iso_in(days=3))
    assert css == ""
    assert "dage" in text


def test_time_left_viser_minutter_saa_en_time_ikke_forsvinder():
    """2 t 59 min må ikke vises som '2 t' — det ser ud som en time mindre."""
    text, _css, _secs = time_left(iso_in(hours=2, minutes=59))
    assert "2 t" in text and "min" in text


def test_time_left_under_en_time_viser_minutter():
    # Trunkeres nedad: 24 min 59 s vises som "24 min". Det er den sikre
    # retning for en nedtælling, så testen accepterer begge.
    text, css, _secs = time_left(iso_in(minutes=25))
    assert text in ("Slutter om 25 min", "Slutter om 24 min")
    assert css == "urgent"


def test_time_left_paa_afsluttet_lot():
    text, css, secs = time_left(iso_in(hours=-5))
    assert text == "Sluttede 5 t siden"
    assert css == "ended"
    assert secs == 0, "afsluttede lots skal sortere efter de aktive"


def test_time_left_entalsform():
    text, _css, _secs = time_left(iso_in(days=1, hours=2))
    assert text.startswith("Slutter om 1 dag")
    assert "dage" not in text


def test_time_left_uden_sluttidspunkt():
    text, css, secs = time_left(None)
    assert text == "Sluttid ukendt"
    assert css == "unknown"
    assert secs == 0, "et lot uden sluttid skal sortere som de afsluttede"


# -- rel_past --------------------------------------------------------------

def test_rel_past_lige_nu():
    rel, _full = rel_past(iso_in(seconds=-30))
    assert rel == "lige nu"


def test_rel_past_minutter():
    rel, _full = rel_past(iso_in(minutes=-20))
    assert rel == "for 20 min. siden"


def test_rel_past_dage():
    rel, _full = rel_past(iso_in(days=-3))
    assert rel == "for 3 dage siden"


def test_rel_past_paa_ugyldigt_input():
    assert rel_past("vrøvl") == ("", "")


# -- kr --------------------------------------------------------------------

def test_rel_past_ental():
    """'1 dage siden' er forkert dansk."""
    rel, _full = rel_past(iso_in(days=-1))
    assert rel == "for 1 dag siden"


@pytest.mark.parametrize("value,expected", [
    (1234, "1.234 kr"),
    (1234567, "1.234.567 kr"),
    (50, "50 kr"),
    (0, "–"),
    (None, "–"),
])
def test_kr_formaterer_dansk(value, expected):
    assert kr(value) == expected


# -- date_label ------------------------------------------------------------

def test_date_label_i_dag():
    assert date_label(iso_in(minutes=-5)) == "I dag"


def test_date_label_i_gaar():
    assert date_label(iso_in(days=-1, hours=-2)) == "I går"


def test_date_label_denne_uge():
    assert "dage siden" in date_label(iso_in(days=-3))


def test_date_label_paa_ugyldigt_input():
    assert date_label("vrøvl") == "Ukendt"


def test_date_label_skriver_danske_maaneder():
    """strftime("%B") følger systemets locale, som er engelsk i containeren."""
    label = date_label(iso_in(days=-40))
    dansk = ("januar", "februar", "marts", "april", "maj", "juni", "juli",
             "august", "september", "oktober", "november", "december")
    engelsk = ("January", "February", "March", "April", "May", "June", "July",
               "August", "October", "December")
    assert any(m in label for m in dansk), label
    assert not any(m in label for m in engelsk), label


def test_time_left_afsluttet_entalsform():
    text, css, _secs = time_left(iso_in(days=-1, hours=-1))
    assert text == "Sluttede 1 dag siden"
    assert css == "ended"
