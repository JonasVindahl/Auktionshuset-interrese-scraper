"""Tests for selve kørslen: scrape -> match -> AI -> notificér -> husk.

Scraperen og Discord erstattes af små test-doubles, men ``run_once`` og hele
kæden omkring AI-trinnet, cachen, digestet og hukommelsen kører som i drift.
Sprogmodellen nås gennem en injiceret transport-funktion, så intet netværk
rammes.
"""

import json

import pytest

from auction_hunter.classifier import Classifier, ClassifierSettings, OpenAICompatibleClient
from auction_hunter.config import ClassifierConfig, Config, Source
from auction_hunter.notifier import DiscordNotifier, NotifyResult
from auction_hunter.runner import build_classifier, run_once
from auction_hunter.scraper import Auction, Lot
from auction_hunter.storage import Store

# Titler der alle matcher it_tech på nøgleord alene.
TITLES = [
    "Switch TP-LINK TL-SG1016D",
    "Intern harddiske ca. 12 stk 1 TB SEAGATE EXOS",
    "Raspberry Pi 4 model B 8GB",
]

def fake_auction(lot_count=3):
    return Auction(
        auction_id="A1", title="Testauktion", url="https://x/a/1",
        ends_text="i morgen", auction_type="Netauktion",
        address_lines=("Vej 1",), lot_count=lot_count,
    )

def fake_lot(index, title):
    return Lot(
        lot_id=f"L{index}", title=title, url=f"https://x/lot/{index}",
        lot_number=str(index), auction_id="A1", auction_title="Testauktion",
        current_bid=100, total_price=188, ends_at=None, image_url="", has_bids=True,
    )

class FakeScraper:
    """Scraper der svarer med faste lots og ikke rører nettet."""

    def __init__(self, titles=None, *, fail_on=None):
        self.titles = titles if titles is not None else TITLES
        self.fail_on = fail_on or set()
        self.pauses = 0

    def fetch_auctions(self):
        return [fake_auction(len(self.titles))]

    def fetch_lots(self, auction):
        if auction.auction_id in self.fail_on:
            raise RuntimeError("kunne ikke hente")
        return [fake_lot(i, t) for i, t in enumerate(self.titles, start=1)]

    def _polite_pause(self):
        self.pauses += 1

class RecordingNotifier(DiscordNotifier):
    """Discord-klient der samler payloads i stedet for at sende dem."""

    def __init__(self, *, fail=False):
        super().__init__("https://discord.invalid/webhook")
        self.payloads = []
        self.fail = fail

    def _post(self, payload):
        self.payloads.append(payload)
        if self.fail:
            from auction_hunter.notifier import DiscordError

            raise DiscordError("kunne ikke sende")

    @property
    def embed_count(self):
        return sum(len(p.get("embeds", [])) for p in self.payloads)

    @property
    def text_messages(self):
        """Kun rene tekstbeskeder. Embed-beskeder har ogsaa 'content' (overskrift)."""
        return [
            p["content"] for p in self.payloads
            if "content" in p and not p.get("embeds")
        ]

    @property
    def embed_messages(self):
        return [p for p in self.payloads if p.get("embeds")]

def client_with(verdict_for, *, calls=None, default='{"verdict":"ja","reason":"ok"}'):
    """Transport der svarer ud fra titlen i prompten."""

    def transport(*, url, headers, payload, timeout):
        prompt = payload["messages"][-1]["content"]
        if calls is not None:
            calls.append(prompt)
        content = default
        for title, verdict in verdict_for.items():
            if title in prompt:
                content = verdict
                break
        return 200, {"choices": [{"message": {"content": content}}]}

    return OpenAICompatibleClient(
        base_url="https://example.invalid/v1", api_key="x", model="m",
        transport=transport,
    )

@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "run.db"))
    yield s
    s.close()

@pytest.fixture
def config():
    return Config(
        source=Source(),
        categories=(),
        max_price=1000,
        soft_over_budget_factor=2.5,
    )

def config_with_classifier(profile="Jeg vil have servere."):
    """Rigtig config fra filen, men med AI slået til og en profil."""
    from auction_hunter.config import load_config

    base = load_config("config/interests.yml")
    return Config(
        source=base.source,
        categories=base.categories,
        max_price=base.max_price,
        soft_over_budget_factor=base.soft_over_budget_factor,
        regions=base.regions,
        exclude=base.exclude,
        opening_bid=base.opening_bid,
        classifier=ClassifierConfig(
            enabled=True, model="m", profile=profile, max_per_run=10
        ),
    )

def inject_classifier(monkeypatch, config, store, client, *, max_per_run=10):
    """Erstat build_classifier så en test-klient bruges i stedet for netværk."""
    settings = ClassifierSettings(
        enabled=True, model="m", api_key="x",
        profile=config.classifier.profile, max_per_run=max_per_run,
    )
    classifier = Classifier(client, settings, store)
    monkeypatch.setattr(
        "auction_hunter.runner.build_classifier", lambda cfg, st: classifier
    )
    return classifier

class TestBasicRun:
    def test_notifies_new_matches(self, config, store):
        notifier = RecordingNotifier()
        config = _with_categories(config)
        stats = run_once(config, store, notifier, scraper_factory=lambda c: FakeScraper())
        assert stats.lots == 3
        assert stats.new_matches > 0
        assert stats.notified > 0
        assert notifier.embed_count == stats.notified

    def test_second_run_does_not_renotify(self, config, store):
        config = _with_categories(config)
        first = RecordingNotifier()
        run_once(config, store, first, scraper_factory=lambda c: FakeScraper())
        assert first.embed_count > 0

        second = RecordingNotifier()
        stats = run_once(config, store, second, scraper_factory=lambda c: FakeScraper())
        assert stats.new_matches == 0
        assert second.embed_count == 0

def _with_categories(config):
    """Brug de rigtige kategorier, så testene matcher som i drift."""
    from auction_hunter.config import load_config

    base = load_config("config/interests.yml")
    return Config(
        source=base.source,
        categories=base.categories,
        max_price=base.max_price,
        soft_over_budget_factor=base.soft_over_budget_factor,
        regions=base.regions,
        exclude=base.exclude,
        opening_bid=base.opening_bid,
        classifier=config.classifier,
    )

class TestClassifierIntegration:
    def test_rejected_match_is_not_sent(self, config, store, monkeypatch):
        config = _with_categories(config_with_classifier())
        client = client_with(
            {"Intern harddiske": '{"verdict":"nej","reason":"rodlot"}'},
            default='{"verdict":"ja","reason":"ok"}',
        )
        inject_classifier(monkeypatch, config, store, client)
        notifier = RecordingNotifier()

        stats = run_once(config, store, notifier, scraper_factory=lambda c: FakeScraper())

        assert stats.rejected_by_ai == 1
        titles = json.dumps(notifier.payloads)
        assert "SEAGATE EXOS" not in titles, "det afviste fund blev alligevel sendt"

    def test_maybe_goes_to_digest_not_embed(self, config, store, monkeypatch):
        config = _with_categories(config_with_classifier())
        client = client_with(
            {"Switch TP-LINK": '{"verdict":"maaske","reason":"usikker"}'},
        )
        inject_classifier(monkeypatch, config, store, client)
        notifier = RecordingNotifier()

        stats = run_once(config, store, notifier, scraper_factory=lambda c: FakeScraper())

        assert stats.review_queued == 1
        assert stats.digested == 1
        assert len(notifier.text_messages) == 1, "digestet skal være en tekstbesked"
        assert "usikker" in notifier.text_messages[0]
        assert store.pending_review_count() == 0, "digestet skal markeres som sendt"

    def test_digest_content_not_in_embeds(self, config, store, monkeypatch):
        config = _with_categories(config_with_classifier())
        client = client_with({"Switch TP-LINK": '{"verdict":"maaske","reason":"usikker"}'})
        inject_classifier(monkeypatch, config, store, client)
        notifier = RecordingNotifier()
        run_once(config, store, notifier, scraper_factory=lambda c: FakeScraper())
        embeds = json.dumps([p.get("embeds", []) for p in notifier.embed_messages])
        assert "TL-SG1016D" not in embeds, "grænsetilfældet hører kun til i digestet"

    def test_no_notifier_leaves_reviews_queued(self, config, store, monkeypatch):
        """Uden notifier kan digestet ikke sendes, så køen skal bestå."""
        config = _with_categories(config_with_classifier())
        client = client_with({"Switch TP-LINK": '{"verdict":"maaske","reason":"usikker"}'})
        inject_classifier(monkeypatch, config, store, client)

        run_once(config, store, None, scraper_factory=lambda c: FakeScraper())
        assert store.pending_review_count() == 1

    def test_ai_not_called_when_no_new_matches(self, config, store, monkeypatch):
        config = _with_categories(config_with_classifier())
        calls = []
        client = client_with({}, calls=calls)
        inject_classifier(monkeypatch, config, store, client)

        run_once(config, store, RecordingNotifier(), scraper_factory=lambda c: FakeScraper())
        first = len(calls)
        assert first > 0

        run_once(config, store, RecordingNotifier(), scraper_factory=lambda c: FakeScraper())
        assert len(calls) == first, "AI-trinnet skal kun køre på nye fund"

    def test_ai_cached_across_runs(self, config, store, monkeypatch):
        """Samme lots igen må ikke give nye modelkald, selv hvis de er nye."""
        config = _with_categories(config_with_classifier())
        calls = []
        client = client_with({}, calls=calls)
        inject_classifier(monkeypatch, config, store, client)

        # Ryd notifikations-hukommelsen mellem kørslerne, så lots igen er 'nye'.
        run_once(config, store, RecordingNotifier(), scraper_factory=lambda c: FakeScraper())
        first = len(calls)
        store.conn.execute("DELETE FROM notifications")
        store.conn.commit()

        run_once(config, store, RecordingNotifier(), scraper_factory=lambda c: FakeScraper())
        assert len(calls) == first, "cachen burde have svaret anden gang"

    def test_disabled_ai_sends_everything(self, config, store):
        """Er AI slået fra, skal alle nye fund sendes som før."""
        base = _with_categories(config)
        notifier = RecordingNotifier()
        stats = run_once(base, store, notifier, scraper_factory=lambda c: FakeScraper())
        assert stats.notified == stats.new_matches

class TestScrapeErrors:
    def test_failing_auction_is_recorded_not_fatal(self, config, store):
        """Én dårlig auktion må ikke vælte hele kørslen."""
        from auction_hunter.scraper import ScrapeError

        config = _with_categories(config)
        scraper = FakeScraper()

        def exploding_lots(auction):
            raise ScrapeError("auktionen kunne ikke hentes")

        scraper.fetch_lots = exploding_lots
        stats = run_once(config, store, None, scraper_factory=lambda c: scraper)
        assert stats.errors
        assert stats.lots == 0

class TestBuildClassifier:
    def test_none_when_disabled(self, config, store):
        assert build_classifier(config, store) is None

    def test_none_when_no_key(self, store, monkeypatch):
        monkeypatch.delenv("CLASSIFIER_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        config = config_with_classifier()
        assert build_classifier(config, store) is None

    def test_built_when_key_present(self, store, monkeypatch):
        monkeypatch.setenv("CLASSIFIER_API_KEY", "hemmelig-noegle")
        config = config_with_classifier()
        classifier = build_classifier(config, store)
        assert classifier is not None
        assert classifier.settings.model == "m"

    def test_secret_file_is_used(self, store, monkeypatch, tmp_path):
        monkeypatch.delenv("CLASSIFIER_API_KEY", raising=False)
        key_file = tmp_path / "key"
        key_file.write_text("noegle-fra-fil", encoding="utf-8")
        monkeypatch.setenv("CLASSIFIER_API_KEY_FILE", str(key_file))
        classifier = build_classifier(config_with_classifier(), store)
        assert classifier is not None
        assert classifier.client.api_key == "noegle-fra-fil"


class TestFailOpen:
    """Et teknisk problem i AI-trinnet må ikke koste et rigtigt fund.

    Dette er den dyreste fejl agenten kan lave: at undertrykke en vare brugeren
    faktisk ville have. Derfor testes alle fejlveje her.
    """

    def _classify(self, store, transport):
        from auction_hunter.classifier import Classifier, ClassifierSettings
        from auction_hunter.matcher import match_lot
        from auction_hunter.scraper import Lot
        from auction_hunter.config import load_config

        config = load_config("config/interests.yml")
        lot = Lot("L1", "Switch TP-LINK TL-SG1016D", "u", "1", "A", "T",
                  100, 188, None, "", True)
        match = match_lot(lot, config)[0]
        settings = ClassifierSettings(enabled=True, model="m", api_key="k", profile="x")
        client = OpenAICompatibleClient(
            base_url="http://x/v1", api_key="k", model="m",
            transport=transport, max_retries=1,
        )
        return Classifier(client, settings, store).classify_matches([match])

    @pytest.mark.parametrize(
        "name, transport",
        [
            ("http_500", lambda **k: (500, {"error": "boom"})),
            ("http_401", lambda **k: (401, {"error": "unauthorized"})),
            ("http_429", lambda **k: (429, {})),
            ("uventet_format", lambda **k: (200, {"raw": "<html>proxy</html>"})),
            ("ulaseligt_svar", lambda **k: (
                200, {"choices": [{"message": {"content": "Det ved jeg ikke"}}]})),
            ("ugyldig_json", lambda **k: (
                200, {"choices": [{"message": {"content": "{ ikke json"}}]})),
        ],
    )
    def test_technical_errors_keep_the_lot(self, store, name, transport):
        result = self._classify(store, transport)
        assert len(result.accepted) == 1, f"{name} tabte fundet"
        assert result.rejected == []
        assert result.errors == 1

    def test_non_requests_exception_does_not_abort(self, store):
        """En klient der fejler på en anden måde end requests må ikke vælte alt."""
        def boom(**kwargs):
            raise RuntimeError("proxy-klient eksploderede")

        result = self._classify(store, boom)
        assert len(result.accepted) == 1

    def test_runner_still_notifies_when_ai_explodes(self, store, monkeypatch, config):
        """Kaster AI-trinnet, skal fundene alligevel ud."""
        config = _with_categories(config_with_classifier())

        class ExplodingClassifier:
            def classify_matches(self, matches):
                raise RuntimeError("AI nede")

        monkeypatch.setattr(
            "auction_hunter.runner.build_classifier",
            lambda cfg, st: ExplodingClassifier(),
        )
        notifier = RecordingNotifier()
        stats = run_once(
            config, store, notifier, scraper_factory=lambda c: FakeScraper()
        )
        assert stats.notified > 0, "fund gik tabt da AI fejlede"
        assert notifier.embed_count == stats.notified

    def test_lot_is_retried_next_run_after_error(self, store):
        """Fejler klassificeringen, caches intet — næste kørsel prøver igen."""
        self._classify(store, lambda **k: (500, {}))
        assert store.counts()["classifications"] == 0


class TestBlindness:
    """Agenten skal sige til hvis den holder op med at se lots.

    Uden dette melder kørslen succes med 0 fund, og brugeren opdager først
    måneder senere at der aldrig kom noget — den farligste langsigtede fejl.
    """

    def _blind_scraper(self):
        class SiteChanged(FakeScraper):
            def fetch_lots(self, auction):
                return []  # CSS-selektorerne matcher ikke længere

        return SiteChanged

    def test_no_alert_when_nothing_is_known(self, store, config):
        """Første kørsel må ikke advare om blindhed — der er ingen baseline."""
        config = _with_categories(config)
        stats = run_once(
            config, store, RecordingNotifier(),
            scraper_factory=lambda c: self._blind_scraper()(),
        )
        assert stats.blind_alert_sent is False

    def test_alert_when_lots_collapse(self, store, config):
        config = _with_categories(config)
        # Først en normal kørsel, så der findes en baseline.
        run_once(config, store, RecordingNotifier(),
                 scraper_factory=lambda c: FakeScraper())

        notifier = RecordingNotifier()
        stats = run_once(config, store, notifier,
                         scraper_factory=lambda c: self._blind_scraper()())

        assert stats.blind_alert_sent is True
        assert stats.lots == 0
        assert any("blind" in m.lower() for m in notifier.text_messages)

    def test_alert_is_not_repeated_within_cooldown(self, store, config):
        config = _with_categories(config)
        run_once(config, store, RecordingNotifier(),
                 scraper_factory=lambda c: FakeScraper())
        run_once(config, store, RecordingNotifier(),
                 scraper_factory=lambda c: self._blind_scraper()())

        second = RecordingNotifier()
        stats = run_once(config, store, second,
                         scraper_factory=lambda c: self._blind_scraper()())
        assert stats.blind_alert_sent is False, "spammer brugeren hver 15. minut"
        assert second.text_messages == []

    def test_alert_when_no_auctions_at_all(self, store, config):
        config = _with_categories(config)

        class NoAuctions(FakeScraper):
            def fetch_auctions(self):
                return []

        notifier = RecordingNotifier()
        stats = run_once(config, store, notifier,
                         scraper_factory=lambda c: NoAuctions())
        assert stats.blind_alert_sent is True
        assert any("auktioner" in m.lower() for m in notifier.text_messages)

    def test_normal_run_does_not_alert(self, store, config):
        config = _with_categories(config)
        run_once(config, store, RecordingNotifier(),
                 scraper_factory=lambda c: FakeScraper())
        notifier = RecordingNotifier()
        stats = run_once(config, store, notifier,
                         scraper_factory=lambda c: FakeScraper())
        assert stats.blind_alert_sent is False
        assert notifier.text_messages == []

    def test_scrape_error_does_not_also_alert(self, store, config):
        """Er der allerede en fejl, skal den ikke dækkes af en blindhedsadvarsel."""
        config = _with_categories(config)
        run_once(config, store, RecordingNotifier(),
                 scraper_factory=lambda c: FakeScraper())

        class Failing(FakeScraper):
            def fetch_lots(self, auction):
                from auction_hunter.scraper import ScrapeError

                raise ScrapeError("nede")

        notifier = RecordingNotifier()
        stats = run_once(config, store, notifier,
                         scraper_factory=lambda c: Failing())
        assert stats.errors
        assert stats.blind_alert_sent is False

    def test_prune_runs_at_most_once_a_day(self, store, config):
        from auction_hunter.runner import maybe_prune

        config = _with_categories(config)
        run_once(config, store, RecordingNotifier(),
                 scraper_factory=lambda c: FakeScraper())
        assert store.get_meta("pruned_at") is not None
        assert maybe_prune(store) == {}

    def test_prune_failure_does_not_lose_finds(self, store, config, monkeypatch):
        """En fejl i oprydningen må ikke koste fundene i kørslen."""
        config = _with_categories(config)
        monkeypatch.setattr(
            "auction_hunter.runner.maybe_prune",
            lambda st: (_ for _ in ()).throw(RuntimeError("db låst")),
        )
        notifier = RecordingNotifier()
        stats = run_once(config, store, notifier,
                         scraper_factory=lambda c: FakeScraper())
        assert stats.notified > 0
