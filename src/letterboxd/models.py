"""Estruturas de dados compartilhadas pelas duas fontes (RSS e export CSV).

O objetivo destes tipos é esconder do resto do projeto a diferença entre as
fontes: o RSS entrega poucos filmes mas já com o ID do TMDB, o export entrega o
histórico inteiro mas só com título e ano. Depois de normalizado, o resto do
código não precisa saber de onde cada entrada veio.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class Source(StrEnum):
    """De onde a entrada veio. Relevante porque a confiança nos campos difere."""

    RSS = "rss"
    EXPORT = "export"


def normalize_title(title: str) -> str:
    """Reduz um título à forma canônica, para comparação entre fontes.

    O mesmo filme aparece com pontuação e acentuação diferentes no RSS e no
    CSV ("WALL·E" vs "WALL-E"), então tudo é rebaixado para letras e números.
    """
    # NFKD separa o acento da letra; o filtro seguinte descarta os acentos.
    decomposed = unicodedata.normalize("NFKD", title.casefold())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", stripped).strip()


@dataclass(slots=True)
class FilmRef:
    """Referência mínima a um filme, sem nada do usuário junto."""

    title: str
    year: int | None = None
    tmdb_id: int | None = None
    letterboxd_uri: str | None = None

    @property
    def match_key(self) -> tuple[str, int | None]:
        """Chave usada para casar o mesmo filme vindo de fontes diferentes."""
        return (normalize_title(self.title), self.year)


@dataclass(slots=True)
class DiaryEntry:
    """Um registro de "assisti este filme nesta data".

    Uma pessoa pode ter várias entradas do mesmo filme (rewatches), por isso a
    data faz parte da identidade da entrada, e não só o filme.
    """

    film: FilmRef
    rating: float | None = None       # 0.5 a 5.0 em passos de 0.5; None = sem nota
    watched_date: date | None = None  # quando assistiu (informado pelo usuário)
    logged_date: date | None = None   # quando registrou no Letterboxd
    rewatch: bool = False
    liked: bool | None = None         # o export não traz likes; fica None
    review: str | None = None
    tags: list[str] = field(default_factory=list)
    source: Source = Source.RSS

    @property
    def dedup_key(self) -> tuple[str, int | None, date | None]:
        """Identidade da entrada para mesclar RSS e export sem duplicar.

        Não usa `tmdb_id` porque o export não tem esse campo: o denominador
        comum entre as fontes é título normalizado + ano + data assistida.
        """
        norm_title, year = self.film.match_key
        return (norm_title, year, self.watched_date)


@dataclass(slots=True)
class Profile:
    """Tudo que conseguimos reunir sobre um usuário, de todas as fontes."""

    username: str
    entries: list[DiaryEntry] = field(default_factory=list)
    watchlist: list[FilmRef] = field(default_factory=list)
    sources: set[Source] = field(default_factory=set)

    def merge(self, other: Profile) -> Profile:
        """Combina dois perfis do mesmo usuário, sem repetir entradas.

        A ordem importa: as entradas de `self` ganham dos conflitos. Na prática
        mesclamos export (histórico completo) com RSS (recente), e o RSS tende
        a ser mais rico — traz likes e o ID do TMDB —, então ele entra como
        `self` e o export como `other`.
        """
        merged = Profile(username=self.username, sources=self.sources | other.sources)
        seen: dict[tuple[str, int | None, date | None], DiaryEntry] = {}

        for entry in [*self.entries, *other.entries]:
            existing = seen.get(entry.dedup_key)
            if existing is None:
                seen[entry.dedup_key] = entry
                continue
            # Mesmo filme, mesma data, fontes diferentes: aproveita os campos
            # que só uma delas tem em vez de descartar a duplicata inteira.
            if existing.film.tmdb_id is None and entry.film.tmdb_id is not None:
                existing.film.tmdb_id = entry.film.tmdb_id
            if existing.rating is None:
                existing.rating = entry.rating
            if existing.liked is None:
                existing.liked = entry.liked
            if not existing.review and entry.review:
                existing.review = entry.review
            if not existing.tags and entry.tags:
                existing.tags = entry.tags

        merged.entries = sorted(
            seen.values(),
            key=lambda e: (e.watched_date is not None, e.watched_date or date.min),
            reverse=True,
        )

        watchlist_seen: dict[tuple[str, int | None], FilmRef] = {}
        for film in [*self.watchlist, *other.watchlist]:
            watchlist_seen.setdefault(film.match_key, film)
        merged.watchlist = list(watchlist_seen.values())

        return merged
