"""Persistência dos perfis processados.

Banco separado do cache do TMDB de propósito: são dados de pessoas, com ciclo de
vida e sensibilidade diferentes dos metadados públicos de filmes. Apagar o cache
é manutenção; apagar isto é perda de dados de usuário.

**O que NÃO é guardado:**

- o ZIP cru enviado — é parseado em memória e descartado;
- qualquer campo de `profile.csv`, que no export real contém **e-mail, nome
  completo, localização e bio**. Esse arquivo nunca é lido. O nome de usuário
  vem do formulário, que o fluxo híbrido precisa de todo jeito para o RSS.

**Ninguém lista perfis.** Cada processamento fica sob um link secreto
(`secrets.token_urlsafe`), que só quem processou recebe. O nome de usuário não
serve de endereço: ele é público, e qualquer um poderia abrir o perfil de
qualquer outro.

O texto das reviews é guardado: é conteúdo público no Letterboxd e abre espaço
para análises futuras. Se isso mudar de opinião, é só deixar de serializá-lo em
`_entrada_para_dict`.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from letterboxd.models import DiaryEntry, FilmRef, Profile, Source

SCHEMA = """
-- Recomendações prontas, por perfil. Gerá-las custa dezenas de chamadas ao
-- TMDB, então não podem ser recalculadas a cada visita à página.
CREATE TABLE IF NOT EXISTS recomendacoes (
    slug      TEXT PRIMARY KEY,
    gerado_em TEXT NOT NULL,
    payload   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS perfis (
    slug          TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    origens       TEXT NOT NULL,
    criado_em     TEXT NOT NULL,
    atualizado_em TEXT NOT NULL,
    payload       TEXT NOT NULL
);
"""

# Recomendações vencem porque o corpus de fundo é recarregado a cada seis meses
# e os pesos mudam com ele. Mudança no perfil invalida na hora, via `salvar`.
VALIDADE_RECOMENDACOES = timedelta(days=30)


def _data_iso(valor: date | None) -> str | None:
    return valor.isoformat() if valor else None


def _data_de(valor: str | None) -> date | None:
    return date.fromisoformat(valor) if valor else None


def _entrada_para_dict(entrada: DiaryEntry) -> dict:
    """Serializa uma entrada. Chaves curtas porque são milhares por perfil."""
    return {
        "t": entrada.film.title,
        "a": entrada.film.year,
        "i": entrada.film.tmdb_id,
        "u": entrada.film.letterboxd_uri,
        "n": entrada.rating,
        "d": _data_iso(entrada.watched_date),
        "r": _data_iso(entrada.logged_date),
        "rw": entrada.rewatch,
        "c": entrada.liked,
        "rv": entrada.review,
        "tg": entrada.tags or None,
        "s": str(entrada.source),
    }


def _entrada_de_dict(dados: dict) -> DiaryEntry:
    return DiaryEntry(
        film=FilmRef(title=dados["t"], year=dados.get("a"),
                     tmdb_id=dados.get("i"), letterboxd_uri=dados.get("u")),
        rating=dados.get("n"),
        watched_date=_data_de(dados.get("d")),
        logged_date=_data_de(dados.get("r")),
        rewatch=bool(dados.get("rw")),
        liked=dados.get("c"),
        review=dados.get("rv"),
        tags=dados.get("tg") or [],
        source=Source(dados.get("s", "export")),
    )


@dataclass(frozen=True, slots=True)
class PerfilSalvo:
    slug: str
    profile: Profile
    criado_em: datetime
    atualizado_em: datetime


class ProfileStore:
    """Guarda e recupera perfis processados, pelo slug secreto."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> ProfileStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def salvar(self, profile: Profile) -> str:
        """Grava um perfil sob um link secreto novo e devolve o slug.

        Sempre um registro novo: reaproveitar o slug de um processamento
        anterior do mesmo usuário entregaria o link dele a quem processasse
        o mesmo nome de usuário depois.
        """
        # shortcut: reprocessar deixa a versão anterior órfã no banco; se isso
        # crescer, expirar perfis por idade, como `JobStore.limpar_antigos`.
        slug = secrets.token_urlsafe(16)
        agora = datetime.now(timezone.utc).isoformat()
        payload = json.dumps({
            "entradas": [_entrada_para_dict(e) for e in profile.entries],
            "watchlist": [
                {"t": f.title, "a": f.year, "i": f.tmdb_id, "u": f.letterboxd_uri}
                for f in profile.watchlist
            ],
        }, ensure_ascii=False, separators=(",", ":"))

        with self._lock:
            self._conn.execute(
                "INSERT INTO perfis (slug, username, origens, criado_em, atualizado_em, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (slug, profile.username, ",".join(sorted(profile.sources)),
                 agora, agora, payload),
            )
            self._conn.commit()
        return slug

    def salvar_recomendacoes(self, slug: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO recomendacoes (slug, gerado_em, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(slug) DO UPDATE SET gerado_em=excluded.gerado_em, "
                "payload=excluded.payload",
                (slug, datetime.now(timezone.utc).isoformat(),
                 json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
            )
            self._conn.commit()

    def obter_recomendacoes(self, slug: str) -> dict | None:
        """Recomendações prontas, ou None se ausentes ou vencidas."""
        with self._lock:
            linha = self._conn.execute(
                "SELECT gerado_em, payload FROM recomendacoes WHERE slug = ?", (slug,)
            ).fetchone()
        if linha is None:
            return None
        gerado = datetime.fromisoformat(linha[0])
        if datetime.now(timezone.utc) - gerado > VALIDADE_RECOMENDACOES:
            return None
        return json.loads(linha[1])

    def obter(self, slug: str) -> PerfilSalvo | None:
        with self._lock:
            linha = self._conn.execute(
                "SELECT username, origens, criado_em, atualizado_em, payload "
                "FROM perfis WHERE slug = ?", (slug,),
            ).fetchone()
        if linha is None:
            return None

        username, origens, criado, atualizado, payload = linha
        dados = json.loads(payload)
        profile = Profile(
            username=username,
            entries=[_entrada_de_dict(d) for d in dados["entradas"]],
            watchlist=[
                FilmRef(title=f["t"], year=f.get("a"), tmdb_id=f.get("i"),
                        letterboxd_uri=f.get("u"))
                for f in dados.get("watchlist", [])
            ],
            sources={Source(o) for o in origens.split(",") if o},
        )
        return PerfilSalvo(slug=slug, profile=profile,
                           criado_em=datetime.fromisoformat(criado),
                           atualizado_em=datetime.fromisoformat(atualizado))

    def apagar(self, slug: str) -> bool:
        """Remove um perfil. Existe porque a pessoa tem direito de sumir."""
        with self._lock:
            cursor = self._conn.execute("DELETE FROM perfis WHERE slug = ?", (slug,))
            self._conn.execute("DELETE FROM recomendacoes WHERE slug = ?", (slug,))
            self._conn.commit()
        return cursor.rowcount > 0
