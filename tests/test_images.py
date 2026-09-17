"""Tests for den lokale billedcache.

Auktionshuset fjerner billedet når et lot lukker, så adressen i
``lots.image_url`` er død netop når Udløbet-fanen skal bruge den. Cachen
henter miniaturen mens lot'et er aktivt.

To ting testene vogter særligt:

* ``lot_id`` kommer fra et HTML-attribut på auktionshusets side og må aldrig
  bruges som filnavn direkte.
* Alt ved billedhentning er fail-open. En død billedserver må hverken vælte
  en kørsel eller en side.
"""

from __future__ import annotations

import http.server
import struct
import threading
import zlib

import pytest

from auction_hunter import images
from auction_hunter.storage import Store


def make_png(width: int = 4, height: int = 4) -> bytes:
    """En rigtig, gyldig PNG — magiske bytes alene ville ikke teste nok."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


PNG = make_png()


@pytest.fixture(autouse=True)
def _lokal_billedserver_er_tilladt(monkeypatch):
    """Testene koerer mod en server paa 127.0.0.1, som drift ellers blokerer."""
    monkeypatch.setenv("IMAGE_ALLOW_PRIVATE_HOSTS", "1")


@pytest.fixture()
def image_server():
    """Lokal server der kan svare som en billedserver — og som en ødelagt en."""
    payload = PNG

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body: bytes, status: int = 200, ctype: str = "image/png"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/ok.png":
                self._send(payload)
            elif self.path == "/huge.png":
                self._send(b"\x89PNG\r\n\x1a\n" + b"x" * 2_000_000)
            elif self.path == "/no-length":
                # Ingen Content-Length: grænsen skal ramme undervejs i stedet
                # for at stole på et tal der ikke er der.
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                blob = b"\x89PNG\r\n\x1a\n" + b"x" * 65536
                try:
                    for _ in range(40):
                        self.wfile.write(b"%X\r\n" % len(blob) + blob + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass   # klienten lagde på, hvilket er meningen
            elif self.path == "/truncated":
                # Content-Length lyver nedad; requests afkorter, og resultatet
                # er en halv fil der stadig har PNG-signaturen.
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", "10")
                self.end_headers()
                self.wfile.write(b"\x89PNG\r\n\x1a\nxx")
            elif self.path == "/notimage":
                self._send(b"<!doctype html><html>nej</html>", ctype="text/html")
            elif self.path == "/error":
                self._send(b"", status=500)
            else:
                self._send(b"", status=404)

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


# -- SSRF-vaern ------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1/x.png", False),
    ("http://[::1]/x.png", False),
    ("http://192.168.1.10/x.png", False),
    ("http://10.0.0.5/x.png", False),
    ("http://169.254.169.254/latest/meta-data/", False),
    ("http://8.8.8.8/x.png", True),
    ("", False),
    ("ikke-en-url", False),
])
def test_kun_offentlige_vaerter_er_tilladte(url, expected):
    assert images.is_public_host(url) is expected


def test_blokerer_privat_adresse_naar_vaernet_er_paa(tmp_path, monkeypatch):
    monkeypatch.delenv("IMAGE_ALLOW_PRIVATE_HOSTS", raising=False)
    dest = tmp_path / "x"
    assert images.download("http://127.0.0.1:1/x.png", dest) is False
    assert not dest.exists()


# -- typegenkendelse -------------------------------------------------------

@pytest.mark.parametrize("data,expected", [
    (PNG, "image/png"),
    (b"\xff\xd8\xff\xe0 resten af en jpeg", "image/jpeg"),
    (b"GIF89a og resten", "image/gif"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
])
def test_genkender_billedtyper(data, expected):
    assert images.sniff_type(data) == expected


@pytest.mark.parametrize("data", [
    b"<!doctype html>", b"", b"ikke et billede overhovedet", b"\x00\x01\x02\x03",
])
def test_afviser_alt_der_ikke_er_et_billede(data):
    assert images.sniff_type(data) is None


# -- filnavne --------------------------------------------------------------

def test_lot_id_kan_ikke_slippe_ud_af_cachemappen(tmp_path):
    """lot_id kommer fra et HTML-attribut og er derfor ikke til at stole på."""
    db = str(tmp_path / "t.db")
    for evil in ["../../../etc/passwd", "..\\..\\windows", "a/b/c", "~/.ssh/id_rsa",
                 "lot\x00null", "." * 300]:
        path = images.cache_path(db, evil)
        assert path.parent == images.cache_dir(db)
        assert "/" not in path.name and "\\" not in path.name
        assert ".." not in path.name


def test_forskellige_lots_faar_forskellige_filer(tmp_path):
    db = str(tmp_path / "t.db")
    assert images.cache_path(db, "a") != images.cache_path(db, "b")


def test_samme_lot_giver_samme_fil(tmp_path):
    db = str(tmp_path / "t.db")
    assert images.cache_path(db, "lot-42") == images.cache_path(db, "lot-42")


def test_cachen_ligger_ved_siden_af_databasen(tmp_path):
    db = str(tmp_path / "data" / "hunter.db")
    assert images.cache_dir(db) == tmp_path / "data" / "images"


# -- download --------------------------------------------------------------

def test_henter_gyldigt_billede(tmp_path, image_server):
    dest = tmp_path / "billede"
    assert images.download(f"{image_server}/ok.png", dest) is True
    assert dest.read_bytes() == PNG


def test_afviser_for_stort_billede(tmp_path, image_server):
    dest = tmp_path / "stort"
    assert images.download(f"{image_server}/huge.png", dest) is False
    assert not dest.exists()


def test_afviser_stort_svar_uden_content_length(tmp_path, image_server):
    """Uden et oplyst tal skal grænsen ramme undervejs i overførslen."""
    dest = tmp_path / "uden-laengde"
    assert images.download(f"{image_server}/no-length", dest) is False
    assert not dest.exists()


def test_afviser_afkortet_billede(tmp_path, image_server):
    """En halv fil har stadig PNG-signaturen, men mangler sin afslutning."""
    dest = tmp_path / "afkortet"
    assert images.download(f"{image_server}/truncated", dest) is False
    assert not dest.exists()


@pytest.mark.parametrize("data,complete", [
    (PNG, True),
    (PNG[:-4], False),                                   # IEND klippet af
    (b"\x89PNG\r\n\x1a\nxx", False),                    # kun signaturen
    (b"\xff\xd8\xff\xe0" + b"x" * 50 + b"\xff\xd9", True),
    (b"\xff\xd8\xff\xe0" + b"x" * 50, False),            # jpeg uden afslutning
    (b"GIF89a" + b"x" * 20 + b"\x3b", True),
    (b"GIF89a" + b"x" * 20, False),
])
def test_genkender_hel_kontra_afkortet_fil(data, complete):
    assert images.looks_complete(data) is complete


def test_afviser_svar_der_ikke_er_et_billede(tmp_path, image_server):
    """En fejlside svarer ofte 200 med HTML."""
    dest = tmp_path / "html"
    assert images.download(f"{image_server}/notimage", dest) is False
    assert not dest.exists()


@pytest.mark.parametrize("path", ["/error", "/findes-ikke"])
def test_afviser_fejlsvar(tmp_path, image_server, path):
    dest = tmp_path / "fejl"
    assert images.download(f"{image_server}{path}", dest) is False
    assert not dest.exists()


def test_doed_server_giver_false_ikke_undtagelse(tmp_path):
    """Fail-open: en utilgængelig server må ikke vælte kørslen."""
    dest = tmp_path / "doed"
    assert images.download("http://127.0.0.1:1/x.png", dest) is False


@pytest.mark.parametrize("url", ["", "ikke-en-url", "file:///etc/passwd", "ftp://x/y.png"])
def test_afviser_andre_protokoller(tmp_path, url):
    """file:// ville kunne læse fra serverens eget filsystem."""
    assert images.download(url, tmp_path / "x") is False


def test_efterlader_ingen_midlertidig_fil(tmp_path, image_server):
    dest = tmp_path / "ryddet"
    images.download(f"{image_server}/huge.png", dest)
    assert not dest.with_suffix(".part").exists()
    assert list(tmp_path.glob("*.part")) == []


def test_erstatter_eksisterende_fil(tmp_path, image_server):
    dest = tmp_path / "erstattes"
    dest.write_bytes(b"gammelt indhold")
    assert images.download(f"{image_server}/ok.png", dest) is True
    assert dest.read_bytes() == PNG


# -- cache-opslag ----------------------------------------------------------

def test_is_cached_paa_manglende_fil(tmp_path):
    assert images.is_cached(str(tmp_path / "t.db"), "l1") is False


def test_is_cached_paa_tom_fil(tmp_path):
    """En tom fil er ikke et billede."""
    db = str(tmp_path / "t.db")
    path = images.cache_path(db, "l1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    assert images.is_cached(db, "l1") is False


def test_read_cached_returnerer_bytes_og_type(tmp_path):
    db = str(tmp_path / "t.db")
    path = images.cache_path(db, "l1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG)
    result = images.read_cached(db, "l1")
    assert result is not None
    data, mime = result
    assert data == PNG and mime == "image/png"


def test_read_cached_afviser_beskadiget_fil(tmp_path):
    db = str(tmp_path / "t.db")
    path = images.cache_path(db, "l1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"det her er ikke et billede")
    assert images.read_cached(db, "l1") is None


# -- udvælgelse ------------------------------------------------------------

def test_henter_kun_for_fund_ikke_for_alle_lots(tmp_path, lot_factory):
    """~2.200 lots scrapes pr. kørsel. Kun fundene skal hentes."""
    db = tmp_path / "t.db"
    with Store(db) as store:
        store.record_lots([
            lot_factory("fund", "Et fund", image="https://x/1.jpg"),
            lot_factory("review", "Til gennemsyn", image="https://x/2.jpg"),
            lot_factory("almindeligt", "Bare et lot", image="https://x/3.jpg"),
        ])
        store.mark_notified("fund", "hifi", 100)
        store.enqueue_review("h1", lot_id="review", category_key="hifi",
                             title="Til gennemsyn", url="https://x/2")
        todo = {lot_id for lot_id, _url in images.pending(store.conn, str(db))}

    assert todo == {"fund", "review"}


def test_springer_lots_uden_billedadresse_over(tmp_path, lot_factory):
    db = tmp_path / "t.db"
    with Store(db) as store:
        store.record_lots([lot_factory("l1", "Uden billede", image="")])
        store.mark_notified("l1", "hifi", 100)
        assert images.pending(store.conn, str(db)) == []


def test_springer_allerede_cachede_over(tmp_path, lot_factory):
    db = tmp_path / "t.db"
    with Store(db) as store:
        store.record_lots([lot_factory("l1", "Med billede", image="https://x/1.jpg")])
        store.mark_notified("l1", "hifi", 100)
        assert len(images.pending(store.conn, str(db))) == 1

        path = images.cache_path(str(db), "l1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        assert images.pending(store.conn, str(db)) == []


def test_respekterer_loftet_pr_koersel(tmp_path, lot_factory):
    db = tmp_path / "t.db"
    with Store(db) as store:
        store.record_lots([
            lot_factory(f"l{i}", f"Lot {i}", image=f"https://x/{i}.jpg")
            for i in range(50)
        ])
        for i in range(50):
            store.mark_notified(f"l{i}", "hifi", 100)
        assert len(images.pending(store.conn, str(db), limit=5)) == 5


def test_cache_pending_henter_og_gemmer(tmp_path, lot_factory, image_server):
    db = tmp_path / "t.db"
    with Store(db) as store:
        store.record_lots([
            lot_factory("l1", "Et fund", image=f"{image_server}/ok.png"),
        ])
        store.mark_notified("l1", "hifi", 100)
        assert images.cache_pending(store.conn, str(db)) == 1

    assert images.is_cached(str(db), "l1")
    assert images.read_cached(str(db), "l1")[0] == PNG


def test_cache_pending_kan_slaas_fra(tmp_path, lot_factory, image_server, monkeypatch):
    monkeypatch.setenv("CACHE_IMAGES", "0")
    db = tmp_path / "t.db"
    with Store(db) as store:
        store.record_lots([lot_factory("l1", "Fund", image=f"{image_server}/ok.png")])
        store.mark_notified("l1", "hifi", 100)
        assert images.cache_pending(store.conn, str(db)) == 0
    assert not images.is_cached(str(db), "l1")


def test_cache_pending_taaler_at_alle_fejler(tmp_path, lot_factory):
    """Fail-open: ingen undtagelse, bare nul hentede."""
    db = tmp_path / "t.db"
    with Store(db) as store:
        store.record_lots([lot_factory("l1", "Fund", image="http://127.0.0.1:1/x.png")])
        store.mark_notified("l1", "hifi", 100)
        assert images.cache_pending(store.conn, str(db)) == 0


# -- oprydning -------------------------------------------------------------

def test_rydder_billeder_for_laengst_afsluttede_lots(tmp_path, lot_factory):
    db = tmp_path / "t.db"
    with Store(db) as store:
        store.record_lots([
            lot_factory("gammelt", "Sluttede for laenge siden", ends_in_hours=-24 * 200),
            lot_factory("nyligt", "Sluttede for nylig", ends_in_hours=-24),
            lot_factory("aktivt", "Koerer stadig", ends_in_hours=48),
        ])
        for lot_id in ("gammelt", "nyligt", "aktivt"):
            path = images.cache_path(str(db), lot_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PNG)

        assert images.prune(store.conn, str(db), days=180) == 1

    assert not images.is_cached(str(db), "gammelt")
    assert images.is_cached(str(db), "nyligt")
    assert images.is_cached(str(db), "aktivt")


def test_oprydning_beholder_lots_uden_sluttidspunkt(tmp_path, lot_factory):
    db = tmp_path / "t.db"
    with Store(db) as store:
        store.record_lots([lot_factory("uden", "Ingen sluttid", ends_in_hours=None)])
        path = images.cache_path(str(db), "uden")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        assert images.prune(store.conn, str(db), days=1) == 0
    assert images.is_cached(str(db), "uden")


def test_oprydning_paa_tom_cache(tmp_path):
    db = tmp_path / "t.db"
    with Store(db) as store:
        assert images.prune(store.conn, str(db)) == 0


def test_forbrug_kan_maales(tmp_path, lot_factory):
    db = str(tmp_path / "t.db")
    for lot_id in ("a", "b"):
        path = images.cache_path(db, lot_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
    count, total = images.usage(db)
    assert count == 2
    assert total == 2 * len(PNG)


def test_forbrug_paa_manglende_mappe(tmp_path):
    assert images.usage(str(tmp_path / "findes-ikke.db")) == (0, 0)
