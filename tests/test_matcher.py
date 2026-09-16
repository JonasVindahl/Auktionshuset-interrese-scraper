"""Tests for matchning og hukommelse."""

import pytest

from auction_hunter.config import load_config
from auction_hunter.matcher import match_all, match_lot, sort_matches
from auction_hunter.scraper import Lot
from auction_hunter.storage import Store


@pytest.fixture(scope="module")
def config():
    return load_config("config/interests.yml")


def make_lot(title, *, bid=None, lot_id="L1", auction_title="Testauktion", total=None):
    return Lot(
        lot_id=lot_id,
        title=title,
        url="https://auktionshuset.dk/auktioner/test/lots/1/x",
        lot_number="1",
        auction_id="A1",
        auction_title=auction_title,
        current_bid=bid,
        total_price=total,
        ends_at=None,
        image_url="",
        has_bids=bool(bid),
    )


def test_strict_keyword_matches_alone(config):
    matches = match_lot(make_lot("Proxmark3 RDV4 med antenne"), config)
    assert matches and matches[0].category.key == "it_tech"


def test_single_weak_keyword_is_not_enough(config):
    """Et enkelt bredt ord må ikke udløse et match alene.

    'router' står i weak, fordi ordet også bruges om andet end netværksudstyr.
    Det kræver derfor en anden træffer ved siden af.
    """
    assert match_lot(make_lot("Router"), config) == []
    assert match_lot(make_lot("Rack"), config) == []


def test_server_matches_alone(config):
    """Server er bevidst flyttet til strong.

    Brugeren vil gerne have servere med, og i en auktionssammenhæng er 'server'
    næsten altid en maskine. Afvejningen er at 'Kaffeservering' ikke fanges,
    fordi bøjningen ikke står på listen over endelser.
    """
    matches = match_lot(make_lot("Server"), config)
    assert matches and matches[0].category.key == "it_tech"
    assert match_lot(make_lot("Kaffeservering"), config) == []


def test_single_strong_keyword_is_enough(config):
    """Specifikke varer matcher alene — 'Switch' er netværksudstyr."""
    matches = match_lot(make_lot("Switch"), config)
    assert matches and matches[0].category.key == "it_tech"


def test_two_weak_keywords_match(config):
    matches = match_lot(make_lot("Router med netværksudstyr"), config)
    assert matches


class TestServersAndDisks:
    """Server- og drevudstyr skal fanges, men ikke butiksdiske.

    'disk' er bevidst ikke et nøgleord: det rammer 'ekspeditionsdisk',
    'købmandsdisk' og 'Industriopvaskemaskine WEXIÖDISK'. Kategorien bruger
    derfor 'harddisk' og de øvrige sammensætninger.
    """

    @pytest.mark.parametrize(
        "title",
        [
            "Intern harddiske ca. 12 stk 1 TB, 8 TB, 18 TB SEAGATE EXOS",
            "Intern harddiske ca. 30 stk. 2 TB, 6 TB, 12 TB TOSHIBA",
            "Ekstern harddisk 1 TB GLYPH Model: Secure Drive",
            "2 stk. Ekstern hardisk 1 TB GLYPH Model: Blackbox Plus",
            "Synology DiskStation DS920+",
            "Dell PowerEdge R720 server",
            "HP ProLiant DL380 Gen9",
            "NAS med 4 drev",
            "Samsung SSD 870 EVO 1TB",
        ],
    )
    def test_server_and_disk_gear_matches(self, title, config):
        assert "it_tech" in {m.category.key for m in match_lot(make_lot(title), config)}

    @pytest.mark.parametrize(
        "title",
        [
            "1.Stk. Ekspeditions disk i træ",
            "Industriopvaskemaskine WEXIÖDISK WD-4",
            "Stor købmandsdisk",
            "Tøm disken",
            "Vitrineskab på disken",
        ],
    )
    def test_disk_word_does_not_match_shop_furniture(self, title, config):
        assert "it_tech" not in {m.category.key for m in match_lot(make_lot(title), config)}

    def test_server_bundle_with_higher_price_still_matches(self, config):
        """Drevbunker er dyre, så loftet for it_tech er hævet til 5.000 kr."""
        lot = make_lot("Intern harddiske ca. 12 stk 1 TB SEAGATE EXOS", bid=11000, total=11000)
        matches = match_lot(lot, config)
        assert matches and matches[0].category.key == "it_tech"
        assert matches[0].over_budget is True

    def test_brand_alone_does_not_match(self, config):
        """Mærket står ikke i brands og må derfor ikke bære et match alene."""
        assert match_lot(make_lot("Glyph Blackbox Pro"), config) == []


def test_excluded_keywords_win_over_matches(config):
    """Høretelefoner er udelukket, selv når mærket er et HiFi-nøgleord."""
    assert match_lot(make_lot("Høretelefoner SENNHEISER HD 280 PRO"), config) == []
    assert match_lot(make_lot("Tastatur og mus LOGITECH"), config) == []
    assert match_lot(make_lot("Gaming headset STEELSERIES"), config) == []


class TestBrandTier:
    """Mærkenavne må ikke matche alene — de kræver en produkttype ved siden af.

    Regressionstest for tre støjfund fundet i rigtige data: 'Div. batterier
    SENNHEISER', 'Flightcase SENNHEISER' og 'Div. Computer reserverdele ...
    SYNOLOGY'. Fælles for dem er at kun mærket er interessant, ikke varen.
    """

    @pytest.mark.parametrize(
        "title, category",
        [
            ("Div. batterier SENNHEISER", "audio_hifi"),
            ("Flightcase SENNHEISER", "audio_hifi"),
            ("Håndholdt-transmitter SENNHEISER Model: 508792", "audio_hifi"),
            ("Diverse Sonos emballage", "audio_hifi"),
            ("Synology emballage og manualer", "it_tech"),
        ],
    )
    def test_brand_alone_does_not_match(self, title, config, category):
        assert category not in {m.category.key for m in match_lot(make_lot(title), config)}

    @pytest.mark.parametrize(
        "title, category",
        [
            ("Forstærker SENNHEISER Model: HPGE", "audio_hifi"),
            ("Sennheiser forstærker", "audio_hifi"),
            ("Synology NAS DS920+", "it_tech"),
            ("Marantz forstærker", "audio_hifi"),
            ("Seiko ur automatisk", "watches"),
        ],
    )
    def test_brand_plus_product_type_matches(self, title, config, category):
        assert category in {m.category.key for m in match_lot(make_lot(title), config)}

    def test_watches_needs_a_watch_word_not_just_a_brand(self, config):
        """En kasse eller rem fra et urmærke er ikke et ur."""
        assert match_lot(make_lot("Oyster uræske ROLEX"), config) == []
        assert match_lot(make_lot("Omega emballage og papirer"), config) == []


def test_sennheiser_still_matches_non_excluded_gear(config):
    """Udelukkelsen må ikke fjerne hele mærket."""
    matches = match_lot(make_lot("Forstærker SENNHEISER Model: HPGE"), config)
    assert matches


def test_no_bid_is_priced_as_entry_not_free(config):
    matches = match_lot(make_lot("DALI højttalere"), config)
    assert matches
    assert matches[0].is_estimate
    assert matches[0].cost == 125


def test_blodt_loft_dropper_bud_over_graense(config):
    """Over kategoriens loft x soft factor skal helt udelades."""
    coffee = config.category("coffee")
    assert coffee is not None
    over = coffee.max_price * config.soft_over_budget_factor

    lots = [make_lot("Espressomaskine RANCILIO Silvia", bid=int(over) + 5000)]
    assert match_all(lots, config) == []


def test_blodt_loft_markerer_men_beholder(config):
    coffee = config.category("coffee")
    assert coffee is not None
    bid = int(coffee.max_price * 1.5)  # over loft, under soft-graense

    matches = match_all([make_lot("Espressomaskine RANCILIO Silvia", bid=bid)], config)
    assert matches
    assert matches[0].over_budget


def test_sort_puts_cheapest_first(config):
    lots = [
        make_lot("Espressomaskine RANCILIO Silvia", bid=500, lot_id="dyr"),
        make_lot("Espressomaskine GAGGIA Classic", bid=100, lot_id="billig"),
        make_lot("Espressomaskine ASCASO Steel", bid=250, lot_id="mellem"),
    ]
    ordered = [m.lot.lot_id for m in sort_matches(match_all(lots, config))]
    assert ordered == ["billig", "mellem", "dyr"]


def test_lot_without_bids_sorts_by_entry_cost_not_zero(config):
    """Et lot uden bud koster hvad første bud koster — ikke 0 kr.

    Uden bud koster 125 kr (mindste bud + salær), hvilket er billigere end et
    lot med et bud på 100 kr, der ender på 188 kr. Rækkefølgen skal afspejle
    den reelle pris.
    """
    lots = [
        make_lot("DALI højttalere", lot_id="udenbud"),
        make_lot("Espressomaskine GAGGIA Classic", bid=100, lot_id="medbud"),
    ]
    ordered = sort_matches(match_all(lots, config))
    assert [m.lot.lot_id for m in ordered] == ["udenbud", "medbud"]
    assert [m.cost for m in ordered] == [125, 188]


def test_lot_matches_multiple_categories(config):
    matches = match_lot(make_lot("Rack med højttalere og netværksswitch"), config)
    assert {m.category.key for m in matches} == {"it_tech", "audio_hifi"}


class TestStore:
    def test_records_lot_and_detects_price_change(self, tmp_path):
        with Store(tmp_path / "t.db") as store:
            store.record_lot(make_lot("Test", bid=100), 188)
            stats = store.lot_stats("L1")
            assert stats is not None
            assert stats.first_bid == 100

            store.record_lot(make_lot("Test", bid=200), 375)
            stats = store.lot_stats("L1")
            assert stats.first_bid == 100, "first_bid skal bevares"
            assert stats.last_bid == 200

    def test_notifications_prevent_duplicates(self, tmp_path, config):
        lot = make_lot("Proxmark3 RDX", lot_id="P1")
        matches = match_lot(lot, config)
        assert matches

        with Store(tmp_path / "t.db") as store:
            assert store.filter_new(matches) == matches, "første gang er ny"
            store.mark_notified(lot.lot_id, matches[0].category.key, matches[0].cost)
            assert store.filter_new(matches) == [], "anden gang er kendt"

    def test_counts_and_export(self, tmp_path):
        with Store(tmp_path / "t.db") as store:
            run_id = store.start_run()
            store.record_lot(make_lot("Test"), 125)
            store.finish_run(run_id, lots=1)

            counts = store.counts()
            assert counts["lots"] == 1
            assert counts["runs"] == 1

            out = store.export_json(tmp_path / "export.json")
            assert out.exists()

    def test_price_risers_reports_increases(self, tmp_path):
        with Store(tmp_path / "t.db") as store:
            store.record_lot(make_lot("Stiger", bid=100, lot_id="A"), 188)
            store.record_lot(make_lot("Stiger", bid=300, lot_id="A"), 375)
            store.record_lot(make_lot("Falder", bid=300, lot_id="B"), 375)
            store.record_lot(make_lot("Falder", bid=100, lot_id="B"), 188)

            risers = store.price_risers()
        assert len(risers) == 1
        assert risers[0]["last_bid"] > risers[0]["first_bid"]
