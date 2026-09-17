"""Tests for AI-klassificeringen.

Netværket erstattes af en injiceret transport-funktion. Det er ikke en mock af
koden under test: ``OpenAICompatibleClient`` kaldes rigtigt, og det er
parsingen, cachen og køen der verificeres. Kun selve HTTP-kaldet er byttet ud.
"""

import json

import pytest

from auction_hunter.classifier import (
    Classifier,
    ClassifierSettings,
    LLMError,
    OpenAICompatibleClient,
    Verdict,
    build_user_prompt,
    input_hash,
    parse_verdict,
)
from auction_hunter.config import load_config
from auction_hunter.matcher import match_lot
from auction_hunter.scraper import Lot
from auction_hunter.storage import Store

PROFILE = "Jeg kan lide servere, HiFi og ure. Jeg vil ikke have kabler."

def make_lot(title, *, bid=None, lot_id="L1"):
    return Lot(
        lot_id=lot_id,
        title=title,
        url=f"https://auktionshuset.dk/auktioner/test/lots/1/{lot_id}",
        lot_number="1",
        auction_id="A1",
        auction_title="Testauktion",
        current_bid=bid,
        total_price=None,
        ends_at=None,
        image_url="",
        has_bids=bool(bid),
    )

def reply(verdict_json: str, status: int = 200):
    """Byg en transport der svarer med et givet modelindhold."""

    def transport(*, url, headers, payload, timeout):
        return status, {"choices": [{"message": {"content": verdict_json}}]}

    return transport

@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()

@pytest.fixture
def settings():
    return ClassifierSettings(
        enabled=True, model="test-model", api_key="x", profile=PROFILE, max_per_run=10
    )

def make_classifier(store, settings, transport):
    client = OpenAICompatibleClient(
        base_url="https://example.invalid/v1",
        api_key="x",
        model="test-model",
        transport=transport,
    )
    return Classifier(client, settings, store)

class TestParseVerdict:
    def test_plain_json(self):
        assert parse_verdict('{"verdict": "ja", "reason": "passer"}') == (
            Verdict.YES, "passer",
        )

    def test_markdown_fenced_json(self):
        raw = '```json\n{"verdict": "nej", "reason": "rodlot"}\n```'
        assert parse_verdict(raw) == (Verdict.NO, "rodlot")

    def test_json_with_surrounding_text(self):
        raw = 'Her er svaret: {"verdict": "maaske", "reason": "usikker"} færdig.'
        assert parse_verdict(raw) == (Verdict.MAYBE, "usikker")

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("ja", Verdict.YES),
            ("Nej.", Verdict.NO),
            ("MÅSKE", Verdict.MAYBE),
            ("yes", Verdict.YES),
            ("no", Verdict.NO),
            ("maybe", Verdict.MAYBE),
        ],
    )
    def test_bare_word_is_accepted(self, raw, expected):
        result = parse_verdict(raw)
        assert result is not None and result[0] is expected

    @pytest.mark.parametrize("raw", ["", "   ", "ved det ikke", "{ikke json"])
    def test_unreadable_returns_none(self, raw):
        assert parse_verdict(raw) is None

    def test_unknown_verdict_returns_none(self):
        assert parse_verdict('{"verdict": "måske-agtigt"}') is None

    def test_long_reason_is_truncated(self):
        long_reason = "x" * 500
        result = parse_verdict(json.dumps({"verdict": "ja", "reason": long_reason}))
        assert result is not None and len(result[1]) <= 200

class TestInputHash:
    def test_stable_across_whitespace_and_case(self):
        a = input_hash(title="Ekstern  Harddisk 1 TB", category_key="it_tech", version="1", model="m")
        b = input_hash(title="ekstern harddisk 1 tb", category_key="it_tech", version="1", model="m")
        assert a == b

    def test_changes_with_title(self):
        a = input_hash(title="Harddisk 1 TB", category_key="it_tech", version="1", model="m")
        b = input_hash(title="Harddisk 2 TB", category_key="it_tech", version="1", model="m")
        assert a != b

    def test_changes_with_version_and_model(self):
        kwargs = dict(title="Harddisk", category_key="it_tech")
        base = input_hash(**kwargs, version="1", model="m")
        assert base != input_hash(**kwargs, version="2", model="m")
        assert base != input_hash(**kwargs, version="1", model="other")

class TestPrompt:
    def test_includes_title_profile_and_matched_keywords(self):
        prompt = build_user_prompt(
            title="Synology DiskStation DS920+",
            category_label="IT / tech / hacking",
            keywords=("synology", "nas"),
            profile=PROFILE,
        )
        assert "Synology DiskStation DS920+" in prompt
        assert PROFILE in prompt
        assert "synology" in prompt and "nas" in prompt
        assert "IT / tech / hacking" in prompt

    def test_handles_no_keywords(self):
        prompt = build_user_prompt(
            title="Et eller andet", category_label="IT", keywords=(), profile=PROFILE
        )
        assert "(ingen)" in prompt

class TestClient:
    def test_returns_content_on_success(self):
        client = OpenAICompatibleClient(
            base_url="https://x/v1", api_key="k", model="m",
            transport=reply('{"verdict":"ja"}'),
        )
        assert client.complete(system="s", user="u") == '{"verdict":"ja"}'

    def test_strips_trailing_slash_in_url(self):
        seen = {}

        def transport(*, url, headers, payload, timeout):
            seen["url"] = url
            return 200, {"choices": [{"message": {"content": "ja"}}]}

        client = OpenAICompatibleClient(
            base_url="https://x/v1/", api_key="k", model="m", transport=transport
        )
        client.complete(system="s", user="u")
        assert seen["url"] == "https://x/v1/chat/completions"

    def test_client_error_is_not_retried(self):
        calls = {"n": 0}

        def transport(*, url, headers, payload, timeout):
            calls["n"] += 1
            return 401, {"error": "bad key"}

        client = OpenAICompatibleClient(
            base_url="https://x/v1", api_key="k", model="m", transport=transport
        )
        with pytest.raises(LLMError):
            client.complete(system="s", user="u")
        assert calls["n"] == 1

    def test_rate_limit_is_retried(self):
        calls = {"n": 0}

        def transport(*, url, headers, payload, timeout):
            calls["n"] += 1
            if calls["n"] < 3:
                return 429, {"error": "slow down"}
            return 200, {"choices": [{"message": {"content": "ja"}}]}

        client = OpenAICompatibleClient(
            base_url="https://x/v1", api_key="k", model="m",
            transport=transport, max_retries=3,
        )
        assert client.complete(system="s", user="u") == "ja"
        assert calls["n"] == 3

    def test_unexpected_body_raises(self):
        client = OpenAICompatibleClient(
            base_url="https://x/v1", api_key="k", model="m",
            transport=lambda **kw: (200, {"unexpected": True}),
        )
        with pytest.raises(LLMError):
            client.complete(system="s", user="u")

class TestClassifyMatching:
    """Cachen og fordelingen i ja/nej/måske/digest."""

    def _match(self, title, bid=None):
        config = load_config("config/interests.yml")
        matches = match_lot(make_lot(title, bid=bid), config)
        assert matches, f"{title!r} matchede ikke — testen er forkert opsat"
        return matches[0]

    def test_yes_is_accepted(self, store, settings):
        classifier = make_classifier(store, settings, reply('{"verdict":"ja","reason":"ok"}'))
        result = classifier.classify_matches([self._match("Server")])
        assert len(result.accepted) == 1
        assert not result.rejected and not result.review

    def test_no_is_rejected(self, store, settings):
        classifier = make_classifier(store, settings, reply('{"verdict":"nej","reason":"rodlot"}'))
        result = classifier.classify_matches([self._match("Server")])
        assert len(result.rejected) == 1
        assert not result.accepted

    def test_maybe_goes_to_review_and_queue(self, store, settings):
        classifier = make_classifier(store, settings, reply('{"verdict":"maaske","reason":"usikker"}'))
        result = classifier.classify_matches([self._match("Server")])
        assert len(result.review) == 1
        assert not result.accepted
        assert store.pending_review_count() == 1

    def test_second_call_uses_cache(self, store, settings):
        calls = {"n": 0}

        def transport(*, url, headers, payload, timeout):
            calls["n"] += 1
            return 200, {"choices": [{"message": {"content": '{"verdict":"ja"}'}}]}

        match = self._match("Server")
        make_classifier(store, settings, transport).classify_matches([match])
        make_classifier(store, settings, transport).classify_matches([match])
        assert calls["n"] == 1, "andet kald skulle have været et cache-hit"

    def test_cache_gives_no_extra_review_row(self, store, settings):
        transport = reply('{"verdict":"maaske","reason":"usikker"}')
        match = self._match("Server")
        make_classifier(store, settings, transport).classify_matches([match])
        make_classifier(store, settings, transport).classify_matches([match])
        assert store.pending_review_count() == 1

    def test_cache_key_changes_invalidate(self, store, settings):
        calls = {"n": 0}

        def transport(*, url, headers, payload, timeout):
            calls["n"] += 1
            return 200, {"choices": [{"message": {"content": '{"verdict":"ja"}'}}]}

        match = self._match("Server")
        make_classifier(store, settings, transport).classify_matches([match])

        bumped = ClassifierSettings(
            enabled=True, model="test-model", api_key="x", profile=PROFILE,
            max_per_run=10, version="999",
        )
        make_classifier(store, bumped, transport).classify_matches([match])
        assert calls["n"] == 2, "en ny version skal tvinge et nyt opslag"

    def test_model_error_fails_open(self, store, settings):
        """Et svar vi ikke kan få må ikke koste et rigtigt fund."""
        def transport(*, url, headers, payload, timeout):
            raise LLMError("modellen er nede")

        client = OpenAICompatibleClient(
            base_url="https://x/v1", api_key="k", model="m", transport=transport
        )
        classifier = Classifier(client, settings, store)
        result = classifier.classify_matches([self._match("Server")])
        assert len(result.accepted) == 1, "fundet skal bevares når modellen fejler"
        assert result.errors == 1

    def test_unreadable_answer_fails_open(self, store, settings):
        classifier = make_classifier(store, settings, reply("jeg ved det ikke rigtigt"))
        result = classifier.classify_matches([self._match("Server")])
        assert len(result.accepted) == 1
        assert result.errors == 1

    def test_max_per_run_defers_remainder(self, store, settings):
        settings = ClassifierSettings(
            enabled=True, model="test-model", api_key="x", profile=PROFILE,
            max_per_run=1,
        )
        config = load_config("config/interests.yml")
        lots = [make_lot(t, lot_id=f"L{i}") for i, t in enumerate(["Server", "Switch TP-LINK"])]
        matches = [m for lot in lots for m in match_lot(lot, config)]
        classifier = make_classifier(store, settings, reply('{"verdict":"ja"}'))
        result = classifier.classify_matches(matches)
        assert len(result.deferred) == len(matches) - 1

    def test_deferred_lots_are_not_cached(self, store, settings):
        settings = ClassifierSettings(
            enabled=True, model="test-model", api_key="x", profile=PROFILE,
            max_per_run=1,
        )
        config = load_config("config/interests.yml")
        lots = [make_lot(t, lot_id=f"L{i}") for i, t in enumerate(["Server", "Switch TP-LINK"])]
        matches = [m for lot in lots for m in match_lot(lot, config)]
        classifier = make_classifier(store, settings, reply('{"verdict":"ja"}'))
        classifier.classify_matches(matches)
        assert len(store.classification_counts()) <= 1

    def test_cache_hits_koster_ikke_budget(self, store, settings):
        """Loftet skal taelle kald til modellen, ikke fund.

        Foer talte et cache-opslag med, saa 25 cachede fund kunne bruge hele
        budgettet uden at der blev ringet én gang, og det 26. blev udskudt
        uden grund.
        """
        config = load_config("config/interests.yml")
        lots = [make_lot(t, lot_id=f"L{i}") for i, t in enumerate(
            ["Server Dell PowerEdge", "Switch TP-LINK", "Pladespiller Thorens"]
        )]
        matches = [m for lot in lots for m in match_lot(lot, config)]
        assert len(matches) >= 3

        calls = {"n": 0}

        def counting(*, url, headers, payload, timeout):
            calls["n"] += 1
            return 200, {"choices": [{"message": {"content": '{"verdict":"ja"}'}}]}

        # Foerste runde uden loft: alt bliver klassificeret og cachet.
        rummelig = ClassifierSettings(
            enabled=True, model="test-model", api_key="x", profile=PROFILE,
            max_per_run=99,
        )
        make_classifier(store, rummelig, counting).classify_matches(matches)
        foerste = calls["n"]
        assert foerste >= 3

        # Anden runde med et loft paa 1: alt ligger i cachen, saa intet maa
        # udskydes og der maa ikke ringes.
        stramt = ClassifierSettings(
            enabled=True, model="test-model", api_key="x", profile=PROFILE,
            max_per_run=1,
        )
        result = make_classifier(store, stramt, counting).classify_matches(matches)
        assert calls["n"] == foerste, "cache-opslag maatte ikke give et kald"
        assert result.deferred == [], "cachede fund maatte ikke udskydes"

    def test_aendret_profil_ugyldiggoer_cachen(self, store, settings):
        """Profilen er prompten, saa en rettelse skal give nye opslag.

        Uden profilen i noeglen genbrugte cachen gamle domme i op til 180 dage,
        og den eneste vej til at rydde dem var at haeve en konstant i koden.
        """
        config = load_config("config/interests.yml")
        match = match_lot(make_lot("Server Dell PowerEdge"), config)[0]

        classifier = make_classifier(store, settings, reply('{"verdict":"nej"}'))
        assert classifier.classify_one(match).verdict is Verdict.NO
        assert classifier.classify_one(match).source == "cache"

        # Samme lot, samme model, ny profiltekst: cachen maa ikke svare.
        ny = ClassifierSettings(
            enabled=True, model="test-model", api_key="x",
            profile=PROFILE + " Jeg vil alligevel gerne have servere.",
            max_per_run=10,
        )
        anden = make_classifier(store, ny, reply('{"verdict":"ja"}'))
        resultat = anden.classify_one(match)
        assert resultat.source == "model", "profilen skulle have ugyldiggjort cachen"
        assert resultat.verdict is Verdict.YES


class TestSettingsGuard:
    def test_usable_requires_key_and_profile(self):
        assert not ClassifierSettings(enabled=True, api_key=None, profile=PROFILE).usable
        assert not ClassifierSettings(enabled=True, api_key="k", profile="").usable
        assert not ClassifierSettings(enabled=False, api_key="k", profile=PROFILE).usable
        assert ClassifierSettings(enabled=True, api_key="k", profile=PROFILE).usable


class TestProfilensEgenSmag:
    """Profile.classifier_profile blev laest fra YAML og aldrig brugt."""

    def _match(self, title, profil):
        config = load_config("config/interests.yml")
        return match_lot(make_lot(title), config, profile=profil)[0]

    def test_profilens_egen_prompt_bruges(self, store, settings):
        from auction_hunter.config import Profile

        set_ned = Profile(key="hifi", label="HiFi", classifier_profile="Kun roerforstaerkere.")
        match = self._match("Forstærker ROTEL", set_ned)

        classifier = Classifier(
            OpenAICompatibleClient(base_url="http://x", api_key="k", model="m",
                                   transport=reply('{"verdict":"ja"}')),
            settings, store,
        )
        assert classifier.profile_for(match) == "Kun roerforstaerkere."

    def test_uden_egen_prompt_bruges_den_globale(self, store, settings):
        from auction_hunter.config import Profile

        uden = Profile(key="alt", label="Alt")
        match = self._match("Forstærker ROTEL", uden)
        classifier = Classifier(
            OpenAICompatibleClient(base_url="http://x", api_key="k", model="m",
                                   transport=reply('{"verdict":"ja"}')),
            settings, store,
        )
        assert classifier.profile_for(match) == PROFILE

    def test_to_profiler_deler_ikke_cache(self, store, settings):
        """Samme lot, to profiler med hver sin smag, skal give to opslag."""
        from auction_hunter.config import Profile

        en = self._match("Forstærker ROTEL", Profile(key="a", label="A",
                                                     classifier_profile="Smag A"))
        to = self._match("Forstærker ROTEL", Profile(key="b", label="B",
                                                     classifier_profile="Smag B"))
        classifier = Classifier(
            OpenAICompatibleClient(base_url="http://x", api_key="k", model="m",
                                   transport=reply('{"verdict":"ja"}')),
            settings, store,
        )
        assert classifier._key(en) != classifier._key(to)
