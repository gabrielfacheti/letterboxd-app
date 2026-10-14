"""Leitura do ZIP de exportação que o Letterboxd oferece ao próprio usuário.

É a única fonte com o histórico completo: o RSS para nas ~50 entradas mais
recentes. Em compensação, não traz o ID do TMDB — só título e ano —, então
todo filme daqui precisa passar pela resolução contra o TMDB depois.

Os Termos de Uso do Letterboxd permitem explicitamente que cada pessoa exporte
o próprio conteúdo, que é exatamente o que acontece aqui: o arquivo chega por
upload, vindo de quem tem direito a ele.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date
from pathlib import Path

from letterboxd.models import DiaryEntry, FilmRef, Profile, Source

# Tetos contra zip bomb: um export real fica na casa de poucos MB mesmo para
# quem registrou milhares de filmes. Um ZIP de 20 MB que descomprime para 2 GB
# é ataque, não usuário.
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200

# Só estes arquivos são lidos. Qualquer outro membro do ZIP é ignorado, o que
# torna irrelevante um nome de arquivo malicioso do tipo `../../etc/passwd`.
DIARY = "diary.csv"
RATINGS = "ratings.csv"
WATCHED = "watched.csv"
WATCHLIST = "watchlist.csv"
REVIEWS = "reviews.csv"
LIKED_FILMS = "likes/films.csv"


class InvalidExport(ValueError):
    """O arquivo enviado não é um export do Letterboxd utilizável."""


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _parse_rating(value: str | None) -> float | None:
    if not value or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_year(value: str | None) -> int | None:
    if not value or not value.strip().isdigit():
        return None
    return int(value)


def _parse_tags(value: str | None) -> list[str]:
    if not value:
        return []
    return [tag.strip() for tag in value.split(",") if tag.strip()]


def _film_from_row(row: dict[str, str]) -> FilmRef:
    """Monta a referência de filme a partir das colunas comuns a todos os CSVs.

    `Name`, `Year` e `Letterboxd URI` se repetem em praticamente todos os
    arquivos do export.
    """
    return FilmRef(
        title=(row.get("Name") or "").strip(),
        year=_parse_year(row.get("Year")),
        letterboxd_uri=(row.get("Letterboxd URI") or "").strip() or None,
    )


def _read_csv(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    """Lê um CSV de dentro do ZIP, devolvendo lista vazia se ele não existir.

    Nem todo export tem todos os arquivos: quem nunca curtiu um filme não
    recebe `likes/films.csv`, por exemplo.
    """
    try:
        raw = archive.read(name)
    except KeyError:
        return []
    # O export é sempre UTF-8; `utf-8-sig` descarta o BOM quando presente.
    text = io.StringIO(raw.decode("utf-8-sig"))
    return [row for row in csv.DictReader(text)]


def _validate_archive(archive: zipfile.ZipFile) -> None:
    """Recusa ZIPs com potencial de estourar memória ao serem lidos."""
    total = sum(info.file_size for info in archive.infolist())
    if total > MAX_UNCOMPRESSED_BYTES:
        raise InvalidExport("conteúdo descomprimido acima do limite permitido")

    for info in archive.infolist():
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            raise InvalidExport(f"taxa de compressão suspeita em {info.filename!r}")

    names = set(archive.namelist())
    if not names & {DIARY, WATCHED, RATINGS, WATCHLIST}:
        raise InvalidExport(
            "o ZIP não parece um export do Letterboxd "
            "(nenhum de diary.csv, watched.csv, ratings.csv ou watchlist.csv)"
        )


def parse_export(data: bytes | Path, username: str) -> Profile:
    """Constrói um `Profile` a partir do ZIP de export.

    Aceita bytes (upload web, nunca tocando o disco) ou um caminho (uso local).
    """
    if isinstance(data, bytes):
        if len(data) > MAX_ZIP_BYTES:
            raise InvalidExport("arquivo acima do tamanho máximo aceito")
        source: io.BytesIO | Path = io.BytesIO(data)
    else:
        source = data
        if source.stat().st_size > MAX_ZIP_BYTES:
            raise InvalidExport("arquivo acima do tamanho máximo aceito")

    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise InvalidExport("arquivo não é um ZIP válido") from exc

    with archive:
        _validate_archive(archive)

        # O diário é a fonte mais rica: tem data assistida, nota, rewatch e tags.
        entries: dict[tuple, DiaryEntry] = {}
        for row in _read_csv(archive, DIARY):
            entry = DiaryEntry(
                film=_film_from_row(row),
                rating=_parse_rating(row.get("Rating")),
                watched_date=_parse_date(row.get("Watched Date")),
                logged_date=_parse_date(row.get("Date")),
                rewatch=(row.get("Rewatch") or "").strip().casefold() == "yes",
                tags=_parse_tags(row.get("Tags")),
                source=Source.EXPORT,
            )
            if entry.film.title:
                entries[entry.dedup_key] = entry

        # `watched.csv` inclui filmes marcados como vistos sem entrada no
        # diário — típico de quem importou o histórico do IMDb. Sem data, mas
        # ainda contam para "o que essa pessoa assistiu".
        diarized = {entry.film.match_key for entry in entries.values()}
        for row in _read_csv(archive, WATCHED):
            film = _film_from_row(row)
            if not film.title or film.match_key in diarized:
                continue
            entry = DiaryEntry(film=film, logged_date=_parse_date(row.get("Date")),
                               source=Source.EXPORT)
            entries.setdefault(entry.dedup_key, entry)

        # As notas vivem em `ratings.csv` por filme (a nota atual), enquanto o
        # diário guarda a nota dada naquela sessão. Preenche só o que falta.
        ratings = {
            _film_from_row(row).match_key: _parse_rating(row.get("Rating"))
            for row in _read_csv(archive, RATINGS)
        }
        reviews = {
            _film_from_row(row).match_key: (row.get("Review") or "").strip() or None
            for row in _read_csv(archive, REVIEWS)
        }
        # Quem nunca curtiu um filme não recebe este arquivo no export. Sem ele
        # não dá para afirmar "não curtiu", só "não sabemos" — daí o None.
        has_likes = LIKED_FILMS in set(archive.namelist())
        liked = {_film_from_row(row).match_key for row in _read_csv(archive, LIKED_FILMS)}

        for entry in entries.values():
            key = entry.film.match_key
            if entry.rating is None:
                entry.rating = ratings.get(key)
            if entry.review is None:
                entry.review = reviews.get(key)
            entry.liked = (key in liked) if has_likes else None

        watchlist = [
            film
            for row in _read_csv(archive, WATCHLIST)
            if (film := _film_from_row(row)).title
        ]

    profile = Profile(username=username, watchlist=watchlist,
                      sources={Source.EXPORT})
    profile.entries = sorted(
        entries.values(),
        key=lambda e: (e.watched_date is not None, e.watched_date or date.min),
        reverse=True,
    )
    return profile
