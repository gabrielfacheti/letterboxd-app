"""Fila de trabalhos em segundo plano.

Enriquecer um export leva dezenas de segundos e montar o corpus de fundo leva
minutos — nada disso cabe num request HTTP. O usuário envia o arquivo, recebe um
identificador e acompanha o progresso numa tela que consulta este módulo.

**Por que não Celery, RQ ou Redis:** o app roda como processo único e
persistente no servidor de casa. Uma fila externa acrescentaria um contêiner, um
broker e uma classe inteira de falhas para resolver um problema que `asyncio` já
resolve. O estado vai para SQLite para sobreviver a reinícios; o que não
sobrevive é a execução, e é por isso que existe `INTERROMPIDO`.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

# Relatar cada filme processado geraria centenas de escritas por job. Só grava
# quando o percentual inteiro muda, ou quando a etapa muda.
PASSO_MINIMO_PERCENTUAL = 1


class EstadoJob(StrEnum):
    PENDENTE = "pendente"
    RODANDO = "rodando"
    CONCLUIDO = "concluido"
    FALHOU = "falhou"
    # O processo morreu no meio. Distinto de FALHOU: não houve erro, houve
    # reinício — e a interface deve oferecer "tentar de novo", não um traceback.
    INTERROMPIDO = "interrompido"


ESTADOS_FINAIS = {EstadoJob.CONCLUIDO, EstadoJob.FALHOU, EstadoJob.INTERROMPIDO}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    tipo          TEXT NOT NULL,
    alvo          TEXT NOT NULL,
    estado        TEXT NOT NULL,
    etapa         TEXT NOT NULL DEFAULT '',
    feitos        INTEGER NOT NULL DEFAULT 0,
    total         INTEGER NOT NULL DEFAULT 0,
    erro          TEXT,
    resultado     TEXT,
    criado_em     TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_por_estado ON jobs (estado, atualizado_em);
"""


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    tipo: str
    alvo: str
    estado: EstadoJob
    etapa: str
    feitos: int
    total: int
    erro: str | None
    resultado: str | None
    criado_em: datetime
    atualizado_em: datetime

    @property
    def percentual(self) -> int:
        """Progresso em 0–100. Sem total conhecido, devolve 0."""
        if not self.total:
            return 100 if self.estado is EstadoJob.CONCLUIDO else 0
        return min(100, round(100 * self.feitos / self.total))

    @property
    def terminou(self) -> bool:
        return self.estado in ESTADOS_FINAIS


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    """Estado dos jobs em SQLite, para sobreviver a reinícios do processo."""

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

    def __enter__(self) -> JobStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def criar(self, tipo: str, alvo: str) -> Job:
        job_id = uuid.uuid4().hex
        agora = _agora()
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, tipo, alvo, estado, criado_em, atualizado_em) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, tipo, alvo, EstadoJob.PENDENTE, agora, agora),
            )
            self._conn.commit()
        return self.obter(job_id)  # type: ignore[return-value]

    def obter(self, job_id: str) -> Job | None:
        with self._lock:
            linha = self._conn.execute(
                "SELECT id, tipo, alvo, estado, etapa, feitos, total, erro, "
                "resultado, criado_em, atualizado_em FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if linha is None:
            return None
        return Job(
            id=linha[0], tipo=linha[1], alvo=linha[2], estado=EstadoJob(linha[3]),
            etapa=linha[4], feitos=linha[5], total=linha[6], erro=linha[7],
            resultado=linha[8], criado_em=datetime.fromisoformat(linha[9]),
            atualizado_em=datetime.fromisoformat(linha[10]),
        )

    def iniciar(self, job_id: str) -> None:
        self._definir(job_id, estado=EstadoJob.RODANDO)

    def progredir(self, job_id: str, etapa: str, feitos: int, total: int) -> None:
        self._definir(job_id, estado=EstadoJob.RODANDO, etapa=etapa,
                      feitos=feitos, total=total)

    def concluir(self, job_id: str, resultado: str) -> None:
        self._definir(job_id, estado=EstadoJob.CONCLUIDO, resultado=resultado)

    def interromper(self, job_id: str) -> None:
        """Marca um job cancelado por desligamento. Não é falha: é reinício."""
        self._definir(job_id, estado=EstadoJob.INTERROMPIDO)

    def falhar(self, job_id: str, erro: str) -> None:
        # A mensagem vai para a interface: cortar evita despejar um traceback
        # inteiro na tela de quem só queria ver suas estatísticas.
        self._definir(job_id, estado=EstadoJob.FALHOU, erro=erro[:500])

    def _definir(self, job_id: str, **campos: object) -> None:
        campos["atualizado_em"] = _agora()
        atribuicoes = ", ".join(f"{nome} = ?" for nome in campos)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {atribuicoes} WHERE id = ?",
                (*[str(v) if isinstance(v, StrEnum) else v for v in campos.values()], job_id),
            )
            self._conn.commit()

    def ativo(self, tipo: str, alvo: str) -> Job | None:
        """Job do mesmo tipo e alvo ainda em andamento, se houver.

        Existe para que recarregar a página não dispare um segundo cálculo
        idêntico — o primeiro já está a caminho.
        """
        with self._lock:
            linha = self._conn.execute(
                "SELECT id FROM jobs WHERE tipo = ? AND alvo = ? AND estado IN (?, ?) "
                "ORDER BY criado_em DESC LIMIT 1",
                (tipo, alvo, EstadoJob.PENDENTE, EstadoJob.RODANDO),
            ).fetchone()
        return self.obter(linha[0]) if linha else None

    def marcar_interrompidos(self) -> int:
        """Fecha jobs que ficaram `RODANDO` de uma execução anterior.

        Chamado na inicialização: um job marcado como rodando sem tarefa viva é
        resto de processo morto, e deixá-lo assim faria a tela de progresso girar
        para sempre.
        """
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE jobs SET estado = ?, atualizado_em = ? WHERE estado IN (?, ?)",
                (EstadoJob.INTERROMPIDO, _agora(), EstadoJob.RODANDO, EstadoJob.PENDENTE),
            )
            self._conn.commit()
        if cursor.rowcount:
            logger.warning("%d job(s) interrompido(s) por reinício", cursor.rowcount)
        return cursor.rowcount

    def limpar_antigos(self, dias: int = 7) -> int:
        """Descarta jobs finalizados antigos. São registros de processo, não dados."""
        limite = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM jobs WHERE atualizado_em < ? AND estado IN (?, ?, ?)",
                (limite, EstadoJob.CONCLUIDO, EstadoJob.FALHOU, EstadoJob.INTERROMPIDO),
            )
            self._conn.commit()
        return cursor.rowcount


# A função de trabalho recebe um relator de progresso e devolve o identificador
# do que produziu (o slug do perfil).
Trabalho = Callable[[Callable[[str, int, int], None]], Awaitable[str]]


class JobRunner:
    """Dispara trabalhos como tarefas asyncio e reflete o andamento no store."""

    def __init__(self, store: JobStore) -> None:
        self._store = store
        # `create_task` só guarda uma referência fraca: sem este conjunto, uma
        # tarefa longa pode ser coletada pelo garbage collector no meio.
        self._tarefas: set[asyncio.Task] = set()

    def agendar(self, tipo: str, alvo: str, trabalho: Trabalho) -> Job:
        job = self._store.criar(tipo, alvo)
        tarefa = asyncio.create_task(self._executar(job.id, trabalho))
        self._tarefas.add(tarefa)
        tarefa.add_done_callback(self._tarefas.discard)
        return job

    async def _executar(self, job_id: str, trabalho: Trabalho) -> None:
        self._store.iniciar(job_id)
        ultimo_percentual = -1
        ultima_etapa = ""

        def relatar(etapa: str, feitos: int, total: int) -> None:
            nonlocal ultimo_percentual, ultima_etapa
            percentual = round(100 * feitos / total) if total else 0
            mudou_bastante = abs(percentual - ultimo_percentual) >= PASSO_MINIMO_PERCENTUAL
            if etapa == ultima_etapa and not mudou_bastante and feitos != total:
                return
            ultimo_percentual, ultima_etapa = percentual, etapa
            self._store.progredir(job_id, etapa, feitos, total)

        try:
            resultado = await trabalho(relatar)
        except asyncio.CancelledError:
            # Cancelamento aqui significa desligamento do processo, não erro do
            # usuário — e a interface deve oferecer "tentar de novo".
            self._store.interromper(job_id)
            raise
        except Exception as exc:  # noqa: BLE001 — a falha tem que virar estado, não sumir
            logger.exception("job %s falhou", job_id)
            self._store.falhar(job_id, f"{type(exc).__name__}: {exc}")
        else:
            self._store.concluir(job_id, resultado)

    async def aguardar_tudo(self) -> None:
        """Espera as tarefas em voo terminarem por conta própria."""
        if self._tarefas:
            await asyncio.gather(*list(self._tarefas), return_exceptions=True)

    async def encerrar(self) -> None:
        """Cancela o que está em voo e espera cada tarefa reagir.

        Precisa rodar **antes** de fechar os stores: a tarefa cancelada ainda
        grava o estado `INTERROMPIDO`, e um banco já fechado transformaria isso
        numa exceção não tratada no desligamento.
        """
        pendentes = list(self._tarefas)
        for tarefa in pendentes:
            tarefa.cancel()
        if pendentes:
            await asyncio.gather(*pendentes, return_exceptions=True)
