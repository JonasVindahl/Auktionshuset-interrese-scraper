"""Tests for hukommelsen over lang tid.

Disse tests handler ikke om én kørsel, men om hvad der sker efter hundreder af
kørsler over måneder: at databasen ikke vokser i det uendelige, at oprydningen
bevarer den viden dedup'en hviler på, og at agenten kan opdage at den er blevet
blind uden at sige det.
"""

from datetime import UTC, datetime, timedelta

import pytest

from auction_hunter.storage import DEFAULT_RETENTION_DAYS, Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "storage.db"))
    yield s
    s.close()


# -- prisadvarsler ---------------------------------------------------------

def test_prisadvarsel_tilstand_runder_tur(store):
    assert store.feedback_actions(["x"]) == {}
    assert store.price_alert_state(["x"]) == {}

    store.set_price_baseline("x", 100)
    assert store.price_alert_state(["x"]) == {"x": (100, None)}

    store.mark_price_alerted("x", 120, "2026-09-16T12:00:00+00:00")
    assert store.price_alert_state(["x"]) == {"x": (120, "2026-09-16T12:00:00+00:00")}

    # En ny baseline opdaterer prisen men bevarer advarselstidspunktet.
    store.set_price_baseline("x", 130)
    assert store.price_alert_state(["x"]) == {"x": (130, "2026-09-16T12:00:00+00:00")}


def test_feedback_handlinger_slaas_op_pr_lot(store):
    for lot_id, action in (("a", "watch"), ("b", "skip")):
        store.conn.execute(
            "INSERT INTO feedback (lot_id, category_key, action, title, created_at)"
            " VALUES (?,?,?,'x','2026-09-16T00:00:00+00:00')",
            (lot_id, "it_tech", action),
        )
    store.conn.commit()
    assert store.feedback_actions(["a", "b", "c"]) == {"a": "watch", "b": "skip"}


def test_sidste_chance_huskes(store):
    assert store.last_chance_sent(["x"]) == set()
    store.mark_last_chance(["x", "y"], "2026-09-16T12:00:00+00:00")
    assert store.last_chance_sent(["x", "y", "z"]) == {"x", "y"}


def iso(days_ago: float = 0) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(
        timespec="seconds"
    )


def add_lot(store, lot_id, *, title="Switch", seen=0, bid=100, total=125):
    store.conn.execute(
        """INSERT INTO lots (lot_id, auction_id, title, first_seen, last_seen,
           first_bid, last_bid, last_total) VALUES (?,?,?,?,?,?,?,?)""",
        (lot_id, "A1", title, iso(seen), iso(seen), bid, bid, total),
    )
    store.conn.commit()


class TestPriceHistoryGrowth:
    """Pris-historikken skal vokse med ændringer, ikke med tiden."""

    def test_unchanged_price_writes_one_row(self, store):
        from tests.test_runner import fake_lot

        lot = fake_lot(1, "Switch TP-LINK")
        for _ in range(50):
            store.record_lot(lot, 188)

        rows = store.conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        assert rows == 1, f"50 identiske kørsler gav {rows} rækker"

    def test_price_change_is_recorded(self, store):
        from tests.test_runner import fake_lot

        store.record_lot(fake_lot(1, "Switch"), 100)
        changed = fake_lot(1, "Switch")
        object.__setattr__(changed, "current_bid", 250)
        store.record_lot(changed, 300)

        rows = store.conn.execute(
            "SELECT bid FROM price_history ORDER BY observed_at"
        ).fetchall()
        assert [r["bid"] for r in rows] == [100, 250]

    def test_last_bid_still_updates_without_history_row(self, store):
        """Uden en ny historik-række skal det aktuelle bud stadig opdateres."""
        from tests.test_runner import fake_lot

        lot = fake_lot(1, "Switch")
        store.record_lot(lot, 100)
        same = fake_lot(1, "Switch")
        object.__setattr__(same, "current_bid", 175)
        store.record_lot(same, 100)

        row = store.conn.execute("SELECT last_bid FROM lots WHERE lot_id='L1'").fetchone()
        assert row["last_bid"] == 175


class TestPrune:
    """Oprydning må rydde historik, men ikke hukommelse."""

    @pytest.fixture
    def populated(self):
        store = Store(":memory:")
        add_lot(store, "OLD", seen=400)
        add_lot(store, "NEW", seen=0)
        store.conn.execute("INSERT INTO price_history VALUES ('OLD',?,1,1)", (iso(400),))
        store.conn.execute("INSERT INTO price_history VALUES ('NEW',?,2,2)", (iso(0),))
        store.conn.execute(
            "INSERT INTO notifications (lot_id, category_key, sent_at, cost)"
            " VALUES ('OLD','it_tech',?,125)",
            (iso(400),),
        )
        store.conn.execute(
            "INSERT INTO classifications VALUES ('h1','OLD','it_tech','1','m','ja','',?)",
            (iso(400),),
        )
        store.conn.execute(
            "INSERT INTO classifications VALUES ('h2','NEW','it_tech','1','m','ja','',?)",
            (iso(0),),
        )
        store.conn.execute(
            "INSERT INTO review_queue (input_hash,lot_id,category_key,title,url,created_at,digested_at)"
            " VALUES ('r1','OLD','it_tech','t','u',?,?)",
            (iso(400), iso(400)),
        )
        store.conn.execute("INSERT INTO runs (started_at,finished_at,lots) VALUES (?,?,5)", (iso(400), iso(400)))
        store.conn.execute("INSERT INTO runs (started_at,finished_at,lots) VALUES (?,?,5)", (iso(0), iso(0)))
        store.conn.commit()
        yield store
        store.close()

    def test_removes_old_history(self, populated):
        removed = populated.prune()
        assert removed["price_history"] == 1
        assert removed["classifications"] == 1
        assert removed["runs"] == 1

    def test_keeps_new_history(self, populated):
        populated.prune()
        assert populated.counts()["price_history"] == 1
        assert populated.counts()["classifications"] == 1
        assert populated.counts()["runs"] == 1

    def test_keeps_lots_and_notifications(self, populated):
        """Uden disse gensender agenten gamle fund efter oprydningen."""
        populated.prune()
        counts = populated.counts()
        assert counts["lots"] == 2, "lot-rækker er dedup'ens grundlag"
        assert counts["notifications"] == 1, "ellers sendes et gammelt fund igen"

    def test_keeps_undigested_review(self, populated):
        """Et 'måske' der ikke er sendt endnu må ikke forsvinde."""
        populated.conn.execute(
            "INSERT INTO review_queue (input_hash,lot_id,category_key,title,url,created_at)"
            " VALUES ('r2','OLD','it_tech','t','u',?)",
            (iso(400),),
        )
        populated.conn.commit()
        populated.prune()

        pending = populated.conn.execute(
            "SELECT COUNT(*) FROM review_queue WHERE input_hash='r2'"
        ).fetchone()[0]
        assert pending == 1, "usendt gennemgang bør ikke ryddes"

    def test_nothing_removed_when_all_is_new(self, store):
        add_lot(store, "NEW")
        store.conn.execute("INSERT INTO price_history VALUES ('NEW',?,1,1)", (iso(0),))
        store.conn.commit()
        removed = store.prune()
        assert removed.get("price_history", 0) == 0

    def test_retention_defaults_to_half_a_year(self):
        assert DEFAULT_RETENTION_DAYS == 180


class TestMaybePrune:
    """Oprydning må ikke køre ved hver kørsel — det er spild af arbejde."""

    def test_first_run_prunes(self, store):
        from auction_hunter.runner import maybe_prune

        add_lot(store, "OLD", seen=400)
        store.conn.execute("INSERT INTO price_history VALUES ('OLD',?,1,1)", (iso(400),))
        store.conn.commit()
        removed = maybe_prune(store)
        assert removed.get("price_history") == 1

    def test_second_run_is_skipped(self, store):
        from auction_hunter.runner import maybe_prune

        add_lot(store, "OLD", seen=400)
        store.conn.execute("INSERT INTO price_history VALUES ('OLD',?,1,1)", (iso(400),))
        store.conn.commit()
        maybe_prune(store)
        # Ny gammel række ville blive ryddet, hvis oprydningen kørte igen.
        store.conn.execute("INSERT INTO price_history VALUES ('OLD',?,1,1)", (iso(401),))
        store.conn.commit()
        assert maybe_prune(store) == {}, "oprydning kørte to gange i træk"


class TestMeta:
    def test_roundtrip(self, store):
        assert store.get_meta("nøgle") is None
        store.set_meta("nøgle", "værdi")
        assert store.get_meta("nøgle") == "værdi"

    def test_overwrite(self, store):
        store.set_meta("k", "a")
        store.set_meta("k", "b")
        assert store.get_meta("k") == "b"

    def test_default(self, store):
        assert store.get_meta("findes_ikke", "fallback") == "fallback"


class TestBlindnessBaseline:
    """Baseline for blindhedstjek skal overleve både fejl og oprydning."""

    def test_none_without_successful_runs(self, store):
        assert store.last_successful_lot_count() is None

    def test_zero_lots_is_not_a_baseline(self, store):
        """En kørsel med 0 lots må ikke blive normen man måler imod."""
        store.finish_run(store.start_run(), lots=0)
        assert store.last_successful_lot_count() is None

    def test_failed_run_is_not_a_baseline(self, store):
        store.finish_run(store.start_run(), lots=500)
        store.finish_run(store.start_run(), lots=500, error="scrape fejlede")
        assert store.last_successful_lot_count() == 500

    def test_baseline_survives_prune(self, store):
        store.finish_run(store.start_run(), lots=2208)
        store.conn.execute(
            "UPDATE runs SET started_at=?", (iso(400),)
        )
        store.conn.commit()
        store.prune()
        # Rækken er væk, så der er ingen baseline — men det må ikke kaste.
        store.last_successful_lot_count()


def test_kortlivet_store_fjerner_ikke_agentens_livstegn(tmp_path):
    """'auction_hunter stats' aabner ogsaa en Store.

    Slettede den blindt livstegnet ved close(), ville et kortlivet opslag faa
    restore til at tro at databasen var ledig, mens agenten skrev videre i den.
    Det er den farlige retning at fejle i.
    """
    db = tmp_path / "hunter.db"
    agent = Store(db)
    try:
        beat = agent.heartbeat_path()
        assert not beat.exists(), "at aabne filen er ikke at koere"
        agent.start_run()
        assert beat.exists()

        # Et kortlivet opslag oven i den koerende agent.
        opslag = Store(db)
        opslag.counts()
        opslag.close()

        assert beat.exists(), "agentens livstegn maatte ikke forsvinde"
    finally:
        agent.close()
    assert not agent.heartbeat_path().exists(), "agenten skal selv rydde op"
