"""Prometheus-format for dashboardet.

Kun aggregerede tal: raekkeantal, alderen paa seneste koersel, fordelingen af
AI-afgoerelser og stoerrelsen paa database og billedcache. Ingen lot-titler,
ingen hemmeligheder og ingen konfigurationsvaerdier.

Endpunktet er aabent som standard, fordi Prometheus skal kunne skrape det uden
en browser-session. Saet METRICS_TOKEN for at kraeve et bearer-token.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from .. import images as images_mod
from . import queries
from .formatting import parse_dt

STALE_AFTER_SECONDS = 45 * 60


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Writer:
    """Samler Prometheus-linjer og skriver HELP/TYPE én gang pr. metrik."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._declared: set[str] = set()

    def add(
        self,
        name: str,
        value: float | int,
        help_text: str,
        *,
        kind: str = "gauge",
        labels: dict[str, str] | None = None,
    ) -> None:
        if name not in self._declared:
            self._lines.append(f"# HELP {name} {help_text}")
            self._lines.append(f"# TYPE {name} {kind}")
            self._declared.add(name)
        suffix = ""
        if labels:
            inner = ",".join(f'{k}="{_escape(str(v))}"' for k, v in labels.items())
            suffix = "{" + inner + "}"
        self._lines.append(f"{name}{suffix} {value}")

    def text(self) -> str:
        return "\n".join(self._lines) + "\n"


def collect(
    conn,
    *,
    version: str,
    db_path: str,
    profiles: int,
    categories: int,
) -> str:
    """Byg hele metrik-udtraekket som Prometheus-tekst."""
    writer = _Writer()
    writer.add("hunter_up", 1, "1 naar dashboardet kan laese databasen")
    writer.add("hunter_build_info", 1, "Versionsinformation", labels={"version": version})
    writer.add("hunter_profiles", profiles, "Antal aktive interesseprofiler")
    writer.add("hunter_categories", categories, "Antal kategorier i profilen")

    for table, rows in sorted(queries.table_counts(conn).items()):
        writer.add(
            "hunter_table_rows", rows, "Raekker pr. tabel", labels={"table": table}
        )

    if queries.has_table(conn, "runs"):
        row = conn.execute(
            "SELECT finished_at FROM runs WHERE finished_at IS NOT NULL "
            "ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        finished = parse_dt(row[0]) if row and row[0] else None
        if finished is not None:
            age = (datetime.now(UTC) - finished).total_seconds()
            writer.add(
                "hunter_run_age_seconds", round(age, 1),
                "Sekunder siden seneste afsluttede koersel",
            )
            writer.add(
                "hunter_stale", 1 if age > STALE_AFTER_SECONDS else 0,
                "1 hvis agenten ikke har koert i 45 minutter",
            )

    if queries.has_table(conn, "classifications"):
        for row in conn.execute(
            "SELECT verdict, COUNT(*) FROM classifications GROUP BY verdict"
        ):
            writer.add(
                "hunter_classifications", row[1],
                "Klassificeringer pr. afgoerelse", kind="counter",
                labels={"verdict": str(row[0])},
            )

    if queries.has_table(conn, "review_queue"):
        pending = conn.execute(
            "SELECT COUNT(*) FROM review_queue WHERE digested_at IS NULL"
        ).fetchone()[0]
        writer.add("hunter_pending_reviews", pending, "Granskninger der ikke er sendt")

    if queries.has_table(conn, "feedback"):
        for row in conn.execute(
            "SELECT action, COUNT(*) FROM feedback GROUP BY action"
        ):
            writer.add(
                "hunter_feedback", row[1], "Markeringer pr. handling", kind="counter",
                labels={"action": str(row[0])},
            )

    try:
        db_bytes = os.path.getsize(db_path)
    except OSError:
        db_bytes = 0
    writer.add("hunter_db_bytes", db_bytes, "Stoerrelse paa SQLite-filen i bytes")

    image_count, image_bytes = images_mod.usage(db_path)
    writer.add("hunter_images", image_count, "Cachede miniaturebilleder")
    writer.add("hunter_image_bytes", image_bytes, "Stoerrelse paa billedcachen i bytes")

    return writer.text()
