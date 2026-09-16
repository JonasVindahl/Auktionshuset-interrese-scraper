"""Lokal cache af lot-billeder.

Auktionshuset fjerner billedet når et lot er afsluttet, så adressen i
``lots.image_url`` er død i samme øjeblik auktionen lukker. Det er præcis der
billedet er mest værd: Udløbet-fanen er hvor man skal genkende hvad man
overvejede at byde på.

Derfor hentes miniaturen mens lot'et stadig er aktivt — men kun for de lots
der faktisk er blevet til et fund eller er lagt til gennemsyn. De er en
håndfuld om dagen, ikke de ~2.200 der scrapes, så både trafikken mod
auktionshuset og pladsen på disken bliver ubetydelig.

Cachen er fail-open i begge retninger: kan billedet ikke hentes, vises
pladsholderen som før, og en fejl her må aldrig vælte en kørsel.

Filnavnet er et hash af ``lot_id``. Det kommer fra et HTML-attribut på
auktionshusets side og må derfor aldrig bruges som filnavn direkte.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

# Hvor mange billeder der højst hentes pr. kørsel. Holder en backfill fra at
# sende hundredvis af forespørgsler af sted på én gang.
MAX_PER_RUN = 20

# Et miniaturebillede er småt. Alt derover er enten et fuldformatbillede eller
# noget der ikke er et billede, og begge dele skal afvises.
MAX_BYTES = 512_000

# Hvor længe et cachet billede beholdes efter lot'et er afsluttet.
DEFAULT_RETENTION_DAYS = 180

DOWNLOAD_TIMEOUT = 15

# Magiske bytes -> MIME-type. Content-Type fra serveren kan ikke stoles på,
# og vi skal kende typen for at kunne udlevere filen igen.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        log.warning("%s er ikke et heltal (%r) — bruger %d", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "ja", "yes", "on")


def caching_enabled() -> bool:
    return _env_bool("CACHE_IMAGES", True)


def retention_days() -> int:
    return _env_int("IMAGE_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)


def max_bytes() -> int:
    return _env_int("MAX_IMAGE_BYTES", MAX_BYTES)


def cache_dir(db_path: str | Path) -> Path:
    """Billederne ligger ved siden af databasen, så de flyttes og backes op med den."""
    return Path(db_path).parent / "images"


def _filename(lot_id: str) -> str:
    return hashlib.sha256(lot_id.encode("utf-8")).hexdigest()[:24]


def cache_path(db_path: str | Path, lot_id: str) -> Path:
    return cache_dir(db_path) / _filename(lot_id)


def is_cached(db_path: str | Path, lot_id: str) -> bool:
    path = cache_path(db_path, lot_id)
    return path.is_file() and path.stat().st_size > 0


def sniff_type(data: bytes) -> str | None:
    """MIME-typen ud fra filens første bytes, eller None hvis det ikke er et billede."""
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def looks_complete(data: bytes) -> bool:
    """Om filen ser ud til at være hel og ikke en afbrudt overførsel.

    En halv fil har stadig de rigtige magiske bytes i starten, så signaturen
    alene siger intet. Alle de formater vi accepterer har til gengæld en kendt
    afslutning, og den kan kun stå der hvis hele filen kom igennem. Det er en
    bedre kontrol end en mindstestørrelse, som ville afvise et lille men
    gyldigt billede.
    """
    mime = sniff_type(data)
    if mime == "image/png":
        return data.endswith(b"IEND\xaeB`\x82")
    if mime == "image/jpeg":
        return data.endswith(b"\xff\xd9")
    if mime == "image/gif":
        return data.endswith(b"\x3b")
    if mime == "image/webp":
        # Længdefeltet i RIFF-hovedet tæller alt efter de første otte bytes.
        declared = int.from_bytes(data[4:8], "little")
        return len(data) >= declared + 8
    return False


def read_cached(db_path: str | Path, lot_id: str) -> tuple[bytes, str] | None:
    """(bytes, mime-type) for et cachet billede, eller None."""
    path = cache_path(db_path, lot_id)
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    mime = sniff_type(data)
    return (data, mime) if mime else None


def download(
    url: str,
    destination: Path,
    *,
    session: requests.Session | None = None,
    limit: int | None = None,
) -> bool:
    """Hent ét billede. Returnerer False ved enhver fejl — aldrig en undtagelse.

    Svaret streames, så en fil der viser sig at være for stor afbrydes i
    stedet for at blive læst helt ind i hukommelsen.
    """
    if not url.startswith(("http://", "https://")):
        return False

    limit = limit or max_bytes()
    client = session or requests
    temporary = destination.with_suffix(".part")

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with client.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True) as response:
            if response.status_code != 200:
                log.debug("Billede gav HTTP %s: %s", response.status_code, url)
                return False

            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                log.debug("Billede er for stort (%s bytes): %s", declared, url)
                return False

            size = 0
            chunks: list[bytes] = []
            for chunk in response.iter_content(8192):
                size += len(chunk)
                if size > limit:
                    log.debug("Billede oversteg %d bytes undervejs: %s", limit, url)
                    return False
                chunks.append(chunk)

        data = b"".join(chunks)
        if sniff_type(data) is None:
            log.debug("Svaret er ikke et billede: %s", url)
            return False
        if not looks_complete(data):
            log.debug("Billedet er afkortet (%d bytes): %s", len(data), url)
            return False

        # Skriv til en nabofil og flyt den på plads, så webserveren aldrig
        # kan nå at læse en halv fil.
        temporary.write_bytes(data)
        temporary.replace(destination)
        return True

    except (requests.RequestException, OSError) as exc:
        log.debug("Kunne ikke hente billede %s: %s", url, exc)
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def pending(conn, db_path: str | Path, limit: int = MAX_PER_RUN) -> list[tuple[str, str]]:
    """(lot_id, url) for fund der mangler et cachet billede.

    Kun lots der er blevet til en notifikation eller ligger til gennemsyn —
    det er dem der vises på siderne. De øvrige ~2.200 hentes aldrig.

    Nyeste først, så en backfill på en stor database begynder med det man
    faktisk kigger på.
    """
    rows = conn.execute(
        """
        SELECT l.lot_id, l.image_url
        FROM lots l
        WHERE l.image_url <> ''
          AND l.lot_id IN (
                SELECT lot_id FROM notifications
                UNION
                SELECT lot_id FROM review_queue
              )
        ORDER BY l.last_seen DESC
        LIMIT ?
        """,
        (limit * 4,),   # der hentes flere end nødvendigt; de cachede sorteres fra
    ).fetchall()

    out: list[tuple[str, str]] = []
    for row in rows:
        lot_id = row["lot_id"]
        if not is_cached(db_path, lot_id):
            out.append((lot_id, row["image_url"]))
        if len(out) >= limit:
            break
    return out


def cache_pending(
    conn,
    db_path: str | Path,
    *,
    session: requests.Session | None = None,
    limit: int = MAX_PER_RUN,
) -> int:
    """Hent de manglende billeder. Returnerer antallet der lykkedes."""
    if not caching_enabled():
        return 0

    todo = pending(conn, db_path, limit=limit)
    if not todo:
        return 0

    saved = 0
    for lot_id, url in todo:
        if download(url, cache_path(db_path, lot_id), session=session):
            saved += 1

    if saved:
        log.info("Cachede %d billede(r) af %d forsøg", saved, len(todo))
    return saved


def prune(conn, db_path: str | Path, *, days: int | None = None) -> int:
    """Slet billeder for lots der har været afsluttet længe.

    Billedet er kun værd at have mens man husker lot'et. Efter et halvt år er
    det dødvægt, og originalen på auktionshuset er væk alligevel.

    ``ends_at`` sammenlignes i Python, ikke i SQL: feltet er ISO med
    tidszone-offset og kan ikke sammenlignes som streng med SQLites
    tidsfunktioner.
    """
    directory = cache_dir(db_path)
    if not directory.is_dir():
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=days or retention_days())

    keep: set[str] = set()
    for row in conn.execute("SELECT lot_id, ends_at FROM lots"):
        ends = row["ends_at"]
        if not ends:
            keep.add(_filename(row["lot_id"]))
            continue
        try:
            parsed = datetime.fromisoformat(str(ends))
        except (ValueError, TypeError):
            keep.add(_filename(row["lot_id"]))
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed > cutoff:
            keep.add(_filename(row["lot_id"]))

    removed = 0
    for path in directory.iterdir():
        if not path.is_file() or path.name in keep:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass

    if removed:
        log.info("Ryddede %d gammelt/gamle billede(r)", removed)
    return removed


def usage(db_path: str | Path) -> tuple[int, int]:
    """(antal filer, bytes i alt) i billedcachen."""
    directory = cache_dir(db_path)
    if not directory.is_dir():
        return 0, 0
    count = total = 0
    for path in directory.iterdir():
        if path.is_file():
            count += 1
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return count, total
