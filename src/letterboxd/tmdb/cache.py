"""Cache local dos metadados do TMDB.

Existe por três motivos, em ordem de importância:

1. **Respeito ao TMDB.** Um export de 3.000 filmes vira 3.000 requisições na
   primeira carga. Sem cache, isso se repetiria a cada visita à página.
2. **Velocidade.** Depois da primeira carga as estatísticas saem de disco local.
3. **Resiliência.** O app continua útil se o TMDB estiver fora do ar.

⚠️ Os termos de uso do TMDB limitam o cache a **6 meses**. `CACHE_TTL` existe
para cumprir isso e não deve ser aumentado.

O acesso passa pelo protocolo `MetadataCache` de propósito: se um dia o app
rodar em processo efêmero (Vercel, Fly), troca-se a implementação SQLite por
uma sobre Postgres sem tocar no resto do código.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from letterboxd.models import normalize_title
from letterboxd.tmdb.models import FilmDetails

# Teto imposto pelos termos de uso do TMDB. Não aumentar.
CACHE_TTL = timedelta(days=180)

SCHEMA = """
CREATE TABLE IF NOT EXISTS films (
    tmdb_id    INTEGER PRIMARY KEY,
    payload    TEXT    NOT NULL,
    fetched_at TEXT    NOT NULL
);

-- Ids do corpus de fundo: a amostra de filmes que serve de linha de base para
-- medir o que é distintivo no gosto de alguém. Montá-la custa centenas de
-- requisições, então precisa sobreviver a reinícios.
CREATE TABLE IF NOT EXISTS background (
    tmdb_id    INTEGER PRIMARY KEY,
    fetched_at TEXT    NOT NULL
);

-- Guarda o resultado da busca por título+ano, inclusive quando nada foi
-- encontrado (tmdb_id NULL): sem isso, todo filme sem correspondência seria
-- pesquisado de novo a cada execução.
CREATE TABLE IF NOT EXISTS resolutions (
    title_key  TEXT    NOT NULL,
    year       INTEGER,
    tmdb_id    INTEGER,
    fetched_at TEXT    NOT NULL,
    PRIMARY KEY (title_key, year)
);

-- Respostas cruas de /recommendations + /similar. Cada geração de candidatos
-- consulta até 40 filmes, quase sempre os mesmos favoritos de novo.
CREATE TABLE IF NOT EXISTS related (
    tmdb_id    INTEGER PRIMARY KEY,
    payload    TEXT    NOT NULL,
    fetched_at TEXT    NOT NULL
);

-- Dados de pessoa (hoje só o local de nascimento), por id do TMDB. Fica à parte
-- dos filmes porque vem de outro endpoint e só se busca para quem aparece nos
-- rankings. Quem não tem local também é guardado: pedir de novo não adiantaria.
CREATE TABLE IF NOT EXISTS people (
    person_id  INTEGER PRIMARY KEY,
    payload    TEXT    NOT NULL,
    fetched_at TEXT    NOT NULL
);
"""


class MetadataCache(Protocol):
    """Contrato mínimo que o enriquecimento espera de um cache."""

    def get_film(
        self, tmdb_id: int, *, aceitar_incompleto: bool = False
    ) -> FilmDetails | None: ...
    def put_film(self, film: FilmDetails) -> None: ...
    def get_resolution(self, title: str, year: int | None) -> tuple[bool, int | None]: ...
    def put_resolution(self, title: str, year: int | None, tmdb_id: int | None) -> None: ...
    def get_background(self) -> list[int]: ...
    def put_background(self, ids: Iterable[int]) -> None: ...
    def get_related(self, tmdb_id: int) -> list[dict] | None: ...
    def put_related(self, tmdb_id: int, filmes: list[dict]) -> None: ...
    def get_people(self, ids: Iterable[int]) -> dict[int, dict]: ...
    def put_person(self, person_id: int, dados: dict) -> None: ...


class SQLiteCache:
    """Implementação em SQLite, adequada a um processo único e persistente.

    O banco fica no SSD: é pequeno e muito acessado, o padrão oposto ao do HD.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False` + lock permite que o FastAPI chame isto de
        # dentro de um threadpool sem abrir uma conexão por requisição.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            # WAL deixa leituras acontecerem durante escritas, que é o padrão
            # de uso aqui: muita leitura, escrita só no enriquecimento.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> SQLiteCache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def _is_fresh(fetched_at: str) -> bool:
        try:
            when = datetime.fromisoformat(fetched_at)
        except ValueError:
            return False
        return datetime.now(timezone.utc) - when < CACHE_TTL

    def get_film(
        self, tmdb_id: int, *, aceitar_incompleto: bool = False
    ) -> FilmDetails | None:
        """Filme em cache, ou None se ausente, vencido ou de esquema antigo.

        `aceitar_incompleto` serve a quem não pode esperar uma rebusca (a página
        do perfil): entrada fresca mas gravada antes de um campo existir é
        servida assim mesmo, com o campo ausente. Entrada vencida nunca é
        servida, com ou sem a flag — o teto do TMDB não admite exceção.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, fetched_at FROM films WHERE tmdb_id = ?", (tmdb_id,)
            ).fetchone()
        if row is None or not self._is_fresh(row[1]):
            return None
        film = FilmDetails.from_json(row[0])
        if aceitar_incompleto:
            return film
        # Entrada gravada antes de `origin_countries` ou `pessoas` existirem:
        # tratar como ausente faz o filme ser buscado de novo uma vez, já com
        # os campos.
        return None if film.origin_countries is None or film.pessoas is None else film

    def put_film(self, film: FilmDetails) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO films (tmdb_id, payload, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(tmdb_id) DO UPDATE SET payload=excluded.payload, "
                "fetched_at=excluded.fetched_at",
                (film.tmdb_id, film.to_json(), film.fetched_at.isoformat()),
            )
            self._conn.commit()

    def get_resolution(self, title: str, year: int | None) -> tuple[bool, int | None]:
        """Devolve `(encontrado_no_cache, tmdb_id)`.

        A tupla distingue "nunca pesquisei" de "pesquisei e não existe" — sem
        isso, um filme obscuro seria buscado no TMDB indefinidamente.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT tmdb_id, fetched_at FROM resolutions WHERE title_key = ? "
                "AND year IS ?", (normalize_title(title), year),
            ).fetchone()
        if row is None or not self._is_fresh(row[1]):
            return (False, None)
        return (True, row[0])

    def put_resolution(self, title: str, year: int | None, tmdb_id: int | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO resolutions (title_key, year, tmdb_id, fetched_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(title_key, year) DO UPDATE SET "
                "tmdb_id=excluded.tmdb_id, fetched_at=excluded.fetched_at",
                (normalize_title(title), year, tmdb_id,
                 datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()

    def get_background(self) -> list[int]:
        """Ids do corpus de fundo ainda dentro da validade."""
        limite = (datetime.now(timezone.utc) - CACHE_TTL).isoformat()
        with self._lock:
            linhas = self._conn.execute(
                "SELECT tmdb_id FROM background WHERE fetched_at > ?", (limite,)
            ).fetchall()
        return [linha[0] for linha in linhas]

    def put_background(self, ids: Iterable[int]) -> None:
        agora = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.executemany(
                "INSERT INTO background (tmdb_id, fetched_at) VALUES (?, ?) "
                "ON CONFLICT(tmdb_id) DO UPDATE SET fetched_at=excluded.fetched_at",
                [(int(i), agora) for i in ids],
            )
            self._conn.commit()

    def get_related(self, tmdb_id: int) -> list[dict] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, fetched_at FROM related WHERE tmdb_id = ?", (tmdb_id,)
            ).fetchone()
        if row is None or not self._is_fresh(row[1]):
            return None
        return json.loads(row[0])

    def put_related(self, tmdb_id: int, filmes: list[dict]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO related (tmdb_id, payload, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(tmdb_id) DO UPDATE SET payload=excluded.payload, "
                "fetched_at=excluded.fetched_at",
                (tmdb_id, json.dumps(filmes), datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()

    def get_people(self, ids: Iterable[int]) -> dict[int, dict]:
        """Dados das pessoas em cache e dentro da validade, por id.

        Quem falta no resultado nunca foi buscado (ou venceu); quem está com o
        local nulo foi buscado e o TMDB não sabe — a diferença importa.
        """
        alvos = list(dict.fromkeys(int(i) for i in ids))
        if not alvos:
            return {}
        with self._lock:
            linhas = self._conn.execute(
                "SELECT person_id, payload, fetched_at FROM people WHERE person_id IN "
                f"({','.join('?' * len(alvos))})", alvos,
            ).fetchall()
        return {i: json.loads(payload) for i, payload, quando in linhas
                if self._is_fresh(quando)}

    def put_person(self, person_id: int, dados: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO people (person_id, payload, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(person_id) DO UPDATE SET payload=excluded.payload, "
                "fetched_at=excluded.fetched_at",
                (person_id, json.dumps(dados, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()

    def stats(self) -> dict[str, int]:
        """Contagens para diagnóstico: quantos filmes e buscas estão guardados."""
        with self._lock:
            films = self._conn.execute("SELECT COUNT(*) FROM films").fetchone()[0]
            resolved = self._conn.execute(
                "SELECT COUNT(*) FROM resolutions WHERE tmdb_id IS NOT NULL"
            ).fetchone()[0]
            missing = self._conn.execute(
                "SELECT COUNT(*) FROM resolutions WHERE tmdb_id IS NULL"
            ).fetchone()[0]
            fundo = self._conn.execute("SELECT COUNT(*) FROM background").fetchone()[0]
        return {"filmes": films, "resolvidos": resolved,
                "sem_correspondencia": missing, "fundo": fundo}
