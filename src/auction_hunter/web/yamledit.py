"""Linje-for-linje redigering af interests.yml.

Filens kommentarer bærer beslutningerne bag profilen — hvorfor ``disk`` ikke er
et nøgleord, hvorfor ``server`` blev flyttet til strong. Et PyYAML-gennemløb
(``safe_load`` efterfulgt af ``dump``) smider dem alle væk. Det var grunden til
at ``tools/tune.py`` blev fjernet fra projektet.

Derfor rører dette modul kun de linjer der faktisk skal ændres. Resten af filen
— kommentarer, rækkefølge, indrykning, tomme linjer — står som den stod.

Indrykningen i filen er fast (se AGENTS.md):

    categories:        <- 0
      it_tech:         <- 2   kategori
        max_price: 7000<- 4   felt
        strong:        <- 4   niveau
          - proxmark   <- 6   nøgleord

    exclude:           <- 0
      - høretelefoner  <- 2   nøgleord
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# Niveauer et nøgleord kan ligge på i en kategori.
LEVELS = ("strong", "weak", "brands", "exact")


class EditError(RuntimeError):
    """Redigeringen kunne ikke gennemføres sikkert."""


@dataclass(frozen=True)
class Block:
    """Et navngivet afsnit i filen, angivet med linjenumre (0-indekseret)."""

    start: int          # linjen med selve nøglen, fx '  it_tech:'
    end: int            # første linje efter afsnittet
    indent: int


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def find_block(lines: list[str], key: str, *, indent: int, start: int = 0,
               end: int | None = None) -> Block | None:
    """Find afsnittet for ``key`` på en given indrykning.

    Afsnittet slutter ved den første linje med indrykning mindre end eller lig
    nøglens egen — tomme linjer og kommentarer tæller ikke med, så en kommentar
    lige før næste nøgle bliver liggende hos den den hører til.
    """
    end = len(lines) if end is None else end
    pattern = re.compile(rf"^{' ' * indent}{re.escape(key)}:\s*(.*)$")

    for i in range(start, end):
        if _is_blank_or_comment(lines[i]):
            continue
        if not pattern.match(lines[i]):
            continue

        # Find slutningen af afsnittet.
        block_end = end
        for j in range(i + 1, end):
            if _is_blank_or_comment(lines[j]):
                continue
            if _indent_of(lines[j]) <= indent:
                block_end = j
                break
        else:
            block_end = end

        # Træk efterhængende tomme linjer og kommentarer ud af afsnittet, så en
        # indsættelse ikke havner efter en kommentar der hører til næste nøgle.
        while block_end - 1 > i and _is_blank_or_comment(lines[block_end - 1]):
            block_end -= 1

        return Block(start=i, end=block_end, indent=indent)
    return None


def list_items(lines: list[str], block: Block) -> list[tuple[int, str]]:
    """(linjenummer, værdi) for hvert listepunkt i et afsnit."""
    item_indent = block.indent + 2
    pattern = re.compile(rf"^{' ' * item_indent}-\s+(.*?)\s*$")
    out: list[tuple[int, str]] = []
    for i in range(block.start + 1, block.end):
        match = pattern.match(lines[i])
        if match:
            value = match.group(1)
            # Fjern eventuelle citationstegn omkring værdien.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out.append((i, value))
    return out


def _quote_if_needed(value: str) -> str:
    """Citer en værdi hvis YAML ellers ville læse den som noget andet."""
    if not value:
        return '""'
    needs_quotes = (
        value[0] in "#&*!|>%@`{}[],\"'"
        or value.strip() != value
        or value.lower() in ("true", "false", "null", "yes", "no", "on", "off", "~")
        or re.fullmatch(r"-?\d+(\.\d+)?", value) is not None
        or ": " in value
    )
    return '"' + value.replace('"', '\\"') + '"' if needs_quotes else value


class InterestsFile:
    """Læser og skriver interests.yml uden at røre kommentarer."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lines = self.path.read_text(encoding="utf-8").splitlines()

    # -- læsning -----------------------------------------------------------

    def data(self) -> dict:
        """Filens indhold som almindelig YAML — kun til visning."""
        return yaml.safe_load("\n".join(self.lines)) or {}

    def categories(self) -> list[str]:
        block = find_block(self.lines, "categories", indent=0)
        if block is None:
            return []
        pattern = re.compile(r"^  ([A-Za-z0-9_\-]+):\s*$")
        return [
            m.group(1)
            for i in range(block.start + 1, block.end)
            if (m := pattern.match(self.lines[i]))
        ]

    def keywords(self, category: str, level: str) -> list[str]:
        block = self._level_block(category, level)
        if block is None:
            return []
        return [value for _, value in list_items(self.lines, block)]

    def excludes(self) -> list[str]:
        block = find_block(self.lines, "exclude", indent=0)
        if block is None:
            return []
        return [value for _, value in list_items(self.lines, block)]

    # -- skrivning ---------------------------------------------------------

    def add_keyword(self, category: str, level: str, keyword: str) -> bool:
        """Tilføj et nøgleord. Returnerer False hvis det allerede findes."""
        keyword = self._clean(keyword)
        self._check_level(level)
        block = self._level_block(category, level)
        if block is None:
            raise EditError(f"Fandt ikke '{level}' under kategorien '{category}'")

        items = list_items(self.lines, block)
        if any(value.lower() == keyword.lower() for _, value in items):
            return False

        line = f"{' ' * (block.indent + 2)}- {_quote_if_needed(keyword)}"
        insert_at = (items[-1][0] + 1) if items else (block.start + 1)
        self.lines.insert(insert_at, line)
        return True

    def remove_keyword(self, category: str, level: str, keyword: str) -> bool:
        self._check_level(level)
        block = self._level_block(category, level)
        if block is None:
            return False
        for line_no, value in list_items(self.lines, block):
            if value.lower() == keyword.strip().lower():
                del self.lines[line_no]
                return True
        return False

    def add_exclude(self, keyword: str) -> bool:
        keyword = self._clean(keyword)
        block = find_block(self.lines, "exclude", indent=0)
        if block is None:
            raise EditError("Fandt ikke 'exclude' i filen")
        items = list_items(self.lines, block)
        if any(value.lower() == keyword.lower() for _, value in items):
            return False
        line = f"{' ' * (block.indent + 2)}- {_quote_if_needed(keyword)}"
        insert_at = (items[-1][0] + 1) if items else (block.start + 1)
        self.lines.insert(insert_at, line)
        return True

    def remove_exclude(self, keyword: str) -> bool:
        block = find_block(self.lines, "exclude", indent=0)
        if block is None:
            return False
        for line_no, value in list_items(self.lines, block):
            if value.lower() == keyword.strip().lower():
                del self.lines[line_no]
                return True
        return False

    def set_scalar(self, path: tuple[str, ...], value: str | int) -> bool:
        """Sæt et enkelt felt, fx ('categories','it_tech','max_price').

        Kun selve værdien på linjen skiftes ud; en kommentar efter værdien
        bevares.
        """
        indent = 0
        start, end = 0, len(self.lines)
        for key in path[:-1]:
            block = find_block(self.lines, key, indent=indent, start=start, end=end)
            if block is None:
                raise EditError(f"Fandt ikke '{key}' i stien {'.'.join(path)}")
            start, end, indent = block.start + 1, block.end, indent + 2

        leaf = path[-1]
        pattern = re.compile(rf"^(\s*{re.escape(leaf)}:\s*)(.*?)(\s*#.*)?$")
        for i in range(start, end):
            if _is_blank_or_comment(self.lines[i]):
                continue
            match = pattern.match(self.lines[i])
            if match and _indent_of(self.lines[i]) == indent:
                prefix, _old, comment = match.group(1), match.group(2), match.group(3) or ""
                rendered = str(value) if isinstance(value, int) else _quote_if_needed(str(value))
                self.lines[i] = f"{prefix}{rendered}{comment}"
                return True
        raise EditError(f"Fandt ikke feltet '{leaf}' under {'.'.join(path[:-1])}")

    # -- gem ---------------------------------------------------------------

    def validate(self) -> dict:
        """Kontrollér at filen stadig er gyldig YAML med den ventede struktur."""
        text = "\n".join(self.lines)
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise EditError(f"Ændringen gav ugyldig YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise EditError("Filen skal indeholde et YAML-mapping i toppen")
        if not data.get("categories"):
            raise EditError("Filen har ingen kategorier efter ændringen")
        return data

    def save(self) -> None:
        """Skriv filen atomisk, men kun hvis den stadig er gyldig.

        Agenten genlæser filen ved hver kørsel. Bliver den skrevet i to tempi,
        kan en kørsel nå at se en halv fil — derfor skrives der til en nabofil
        der omdøbes på plads.
        """
        self.validate()
        text = "\n".join(self.lines) + "\n"
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(text, encoding="utf-8")
        temp.replace(self.path)

    # -- internt -----------------------------------------------------------

    def _level_block(self, category: str, level: str) -> Block | None:
        categories = find_block(self.lines, "categories", indent=0)
        if categories is None:
            return None
        cat = find_block(
            self.lines, category, indent=2,
            start=categories.start + 1, end=categories.end,
        )
        if cat is None:
            return None
        return find_block(
            self.lines, level, indent=4, start=cat.start + 1, end=cat.end
        )

    @staticmethod
    def _check_level(level: str) -> None:
        if level not in LEVELS:
            raise EditError(
                f"Ukendt niveau '{level}'. Gyldige er: {', '.join(LEVELS)}"
            )

    @staticmethod
    def _clean(keyword: str) -> str:
        keyword = keyword.strip().lower()
        if not keyword:
            raise EditError("Nøgleordet er tomt")
        if len(keyword) > 80:
            raise EditError("Nøgleordet er for langt (maks. 80 tegn)")
        if "\n" in keyword or "\r" in keyword:
            raise EditError("Nøgleordet må ikke indeholde linjeskift")
        return keyword
