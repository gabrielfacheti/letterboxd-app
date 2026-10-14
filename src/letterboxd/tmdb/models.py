"""Metadados de filme vindos do TMDB, achatados para o que o projeto usa.

A resposta crua do TMDB é grande e aninhada. Guardar tudo desperdiça espaço no
cache e amarra o resto do código ao formato da API, então extraímos só os campos
que alimentam as estatísticas e as recomendações.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

# Quantos nomes do elenco principal guardar. O TMDB devolve o elenco inteiro,
# incluindo figurantes creditados, que não dizem nada sobre gosto.
CAST_LIMIT = 10


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@dataclass(slots=True)
class FilmDetails:
    """Metadados de um filme, prontos para virar estatística."""

    tmdb_id: int
    title: str
    original_title: str = ""
    release_date: date | None = None
    runtime: int | None = None           # minutos
    original_language: str = ""          # ISO 639-1
    genres: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)       # ISO 3166-1 alpha-2
    country_names: list[str] = field(default_factory=list)
    # País de origem, normalmente um só. `countries` traz todo coprodutor: em
    # The Dark Knight são GB e US, e o Reino Unido apareceria em dezenas de
    # filmes americanos. None marca entrada de cache anterior a este campo.
    origin_countries: list[str] | None = None
    directors: list[str] = field(default_factory=list)
    cast: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    vote_average: float | None = None
    # Sem isto não dá para distinguir "a média é 0" de "ninguém votou": o TMDB
    # devolve vote_average=0.0 nos dois casos.
    vote_count: int = 0
    popularity: float | None = None
    poster_path: str | None = None
    # Caminho da foto de cada pessoa (direção e elenco), por nome. Vem de graça
    # nos créditos, que já são pedidos com `append_to_response`. Guardado como
    # mapa à parte para não mexer nas listas de nomes, que alimentam as
    # estatísticas e os traços de gosto.
    fotos: dict[str, str] = field(default_factory=dict)
    # Id e gênero (convenção do TMDB: 0 desconhecido, 1 feminino, 2 masculino,
    # 3 não-binário) de quem está em `directors` e `cast`, por nome. O id leva
    # ao local de nascimento (`/person/{id}`). None marca entrada de cache
    # anterior a este campo; {} é um filme cujos créditos vieram sem ids.
    pessoas: dict[str, dict] | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def release_year(self) -> int | None:
        return self.release_date.year if self.release_date else None

    @classmethod
    def from_api(cls, payload: dict) -> FilmDetails:
        """Achata a resposta de `/movie/{id}?append_to_response=credits,keywords`.

        O `append_to_response` é o que permite pegar detalhes, créditos e
        keywords numa requisição só — três vezes menos chamadas por filme.
        """
        credits = payload.get("credits") or {}

        diretores = [
            member
            for member in credits.get("crew") or []
            if member.get("job") == "Director" and member.get("name")
        ]
        elenco = [
            member
            for member in (credits.get("cast") or [])[:CAST_LIMIT]
            if member.get("name")
        ]
        directors = [member["name"] for member in diretores]
        cast = [member["name"] for member in elenco]
        keywords = [
            item["name"]
            for item in ((payload.get("keywords") or {}).get("keywords") or [])
            if item.get("name")
        ]
        countries = payload.get("production_countries") or []

        fotos = {
            pessoa["name"]: pessoa["profile_path"]
            for pessoa in [*(credits.get("crew") or []), *(credits.get("cast") or [])[:CAST_LIMIT]]
            if pessoa.get("name") and pessoa.get("profile_path")
        }

        pessoas = {
            member["name"]: {"id": int(member["id"]), "genero": member.get("gender") or 0}
            for member in [*diretores, *elenco]
            if member.get("id") is not None
        }

        return cls(
            tmdb_id=int(payload["id"]),
            title=payload.get("title") or "",
            original_title=payload.get("original_title") or "",
            release_date=_parse_date(payload.get("release_date")),
            runtime=payload.get("runtime"),
            original_language=payload.get("original_language") or "",
            genres=[g["name"] for g in payload.get("genres") or [] if g.get("name")],
            countries=[c["iso_3166_1"] for c in countries if c.get("iso_3166_1")],
            country_names=[c["name"] for c in countries if c.get("name")],
            origin_countries=list(payload.get("origin_country") or []),
            directors=directors,
            cast=cast,
            keywords=keywords,
            vote_average=payload.get("vote_average"),
            vote_count=payload.get("vote_count") or 0,
            popularity=payload.get("popularity"),
            poster_path=payload.get("poster_path"),
            fotos=fotos,
            pessoas=pessoas,
        )

    def to_json(self) -> str:
        data = asdict(self)
        data["release_date"] = self.release_date.isoformat() if self.release_date else None
        data["fetched_at"] = self.fetched_at.isoformat()
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> FilmDetails:
        data = json.loads(raw)
        data["release_date"] = _parse_date(data.get("release_date"))
        data["fetched_at"] = datetime.fromisoformat(data["fetched_at"])
        return cls(**data)
