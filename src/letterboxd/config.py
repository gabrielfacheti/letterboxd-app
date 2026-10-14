"""Configuração lida do ambiente, com `.env` como conveniência local.

Em produção as variáveis vêm do ambiente do container; o `.env` existe só para
não ter que exportar tudo à mão durante o desenvolvimento.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MissingConfig(RuntimeError):
    """Variável obrigatória ausente."""


@dataclass(frozen=True, slots=True)
class Settings:
    tmdb_key: str
    data_dir: Path

    @property
    def cache_path(self) -> Path:
        """Banco do cache de metadados.

        Fica no SSD por ser pequeno e muito acessado. Os dados volumosos é que
        vão para o HD.
        """
        return self.data_dir / "tmdb-cache.sqlite"


def load_settings(env_file: Path | None = None) -> Settings:
    """Carrega as configurações, falhando cedo se a chave do TMDB faltar."""
    load_dotenv(env_file or PROJECT_ROOT / ".env")

    key = os.environ.get("TMDB_KEY", "").strip()
    if not key:
        raise MissingConfig(
            "TMDB_KEY não definida. Copie .env.example para .env e preencha "
            "com a chave de https://www.themoviedb.org/settings/api"
        )

    data_dir = Path(os.environ.get("LETTERBOXD_DATA_DIR", PROJECT_ROOT / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(tmdb_key=key, data_dir=data_dir)
