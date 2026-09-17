"""Tests for udtraekket af lot-sider.

Markupen i testene er skrevet her, ikke hentet fra auktionshuset. Udtraekket er
bevidst strukturuafhaengigt, saa testene handler om at signalerne findes i
broedteksten og ikke i navigationen, ikke om bestemte CSS-klasser.
"""

from __future__ import annotations

from auction_hunter.details import find_signals, parse

SIDE = """<!doctype html><html lang="da"><head>
  <style>.x{color:red}</style><script>var x = "defekt";</script>
</head><body>
  <nav>Forside Auktioner Om os Kontakt defekt</nav>
  <div class="indhold">
    <h1>Thorens TD 160 pladespiller</h1>
    <p>Pladespilleren er i fin kosmetisk stand, men tonearmen er defekt og
    der mangler dele til den.</p>
    <p>Afhentning efter aftale i Koge.</p>
  </div>
  <footer>Handelsbetingelser og vilkaar</footer>
</body></html>"""


def test_parser_finder_beskrivelsen_og_ikke_navigationen():
    detaljer = parse(SIDE)
    assert "tonearmen er defekt" in detaljer.text
    assert "Handelsbetingelser" not in detaljer.text
    assert "Om os" not in detaljer.text


def test_parser_ser_bort_fra_script_og_style():
    html = ('<html><body><div><script>var s = "defekt";</script>'
            "<p>Varen er ubrugt og ligger i original emballage.</p></div></body></html>")
    detaljer = parse(html)
    assert "defekt" not in detaljer.flags
    assert "ny" in detaljer.flags


def test_flere_signaler_rapporteres_i_raekkefoelge():
    """Det alvorligste staar foerst, uanset hvor i teksten det stod."""
    from auction_hunter.details import SIGNALS

    detaljer = parse(SIDE)
    raekkefoelge = [flag for flag, _ in SIGNALS]
    positioner = [raekkefoelge.index(flag) for flag in detaljer.flags]
    assert positioner == sorted(positioner)
    assert "defekt" in detaljer.flags
    assert "afhentning" in detaljer.flags


def test_komplet_og_ny_kan_staa_sammen():
    detaljer = parse("<div><p>Komplet saet, ubrugt og i original emballage.</p></div>")
    assert set(detaljer.flags) >= {"komplet", "ny"}


def test_tom_side_giver_tomt_svar():
    assert parse("").empty


def test_side_uden_signaler_har_tekst_men_ingen_flag():
    detaljer = parse("<div><p>En ganske almindelig beskrivelse uden noegleord.</p></div>")
    assert detaljer.text
    assert detaljer.flags == ()


def test_find_signals_er_tilfaeldiguafhaengig():
    assert find_signals("Varen er DEFEKT") == ("defekt",)


def test_lang_tekst_afkortes():
    detaljer = parse("<div><p>" + "ord " * 500 + "</p></div>", limit=100)
    assert len(detaljer.text) <= 100


def test_auktionsbetingelser_i_dropdown_giver_ikke_flag():
    """Vilkaarene ligger i en dropdown og handler ikke om varens stand.

    I rigtige data staar der ord som "reparation", "afhentning" og
    "momsfritagelse" i dem. Laeste vi hele siden, fik hvert eneste lot de
    samme falske flag: defekt, afhentning, momsfri.
    """
    html = """<!doctype html><html><body>
      <div class="indhold">
        <div class=" space-y-4 prosa"><p>Forbehold for evt. manglende stroemkabler</p></div>
      </div>
      <div class="dropdown closed">
        <div class="dropdown-clicker"><h3>Auktionsbetingelser</h3></div>
        <div class="dropdown-body prosa">
          <p>1. Varer koebt som beset. Beskadigelse ved afhentning.
          Reparation og moms afregnes saerskilt. Momsfritagelse kraever
          dokumentation.</p>
        </div>
      </div>
    </body></html>"""
    detaljer = parse(html)
    assert detaljer.flags == ()
    assert "Forbehold" in detaljer.text
    assert "Varer koebt" not in detaljer.text


def test_beskrivelse_i_prosa_flagger_stadig():
    html = '<div class="prosa"><p>Tonearmen er defekt og der mangler dele.</p></div>'
    assert parse(html).flags == ("defekt",)


def test_tom_beskrivelse_falder_ikke_tilbage_til_hele_siden():
    html = """<html><body><div class="prosa"></div>
      <div class="dropdown"><div class="dropdown-body prosa">
      Varen er defekt og til reparation.</div></div></body></html>"""
    detaljer = parse(html)
    assert detaljer.text == ""
    assert detaljer.flags == ()
