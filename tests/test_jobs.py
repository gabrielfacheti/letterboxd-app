import asyncio

import pytest

from letterboxd.jobs import EstadoJob, JobRunner, JobStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(tmp_path):
    with JobStore(tmp_path / "jobs.sqlite") as s:
        yield s


def test_job_nasce_pendente(store):
    job = store.criar("export", "gfac")
    assert job.estado is EstadoJob.PENDENTE
    assert job.percentual == 0
    assert not job.terminou


def test_percentual(store):
    job_id = store.criar("export", "gfac").id
    store.progredir(job_id, "buscando", 50, 200)
    assert store.obter(job_id).percentual == 25


def test_percentual_sem_total_nao_estoura(store):
    """Total zero acontece quando não há nada a fazer; não pode dividir por zero."""
    job_id = store.criar("export", "gfac").id
    store.progredir(job_id, "nada", 0, 0)
    assert store.obter(job_id).percentual == 0
    store.concluir(job_id, "gfac")
    assert store.obter(job_id).percentual == 100


async def test_runner_conclui_e_guarda_resultado(store):
    async def trabalho(relatar):
        relatar("etapa", 1, 2)
        relatar("etapa", 2, 2)
        return "slug-produzido"

    runner = JobRunner(store)
    job = runner.agendar("export", "gfac", trabalho)
    await runner.aguardar_tudo()

    final = store.obter(job.id)
    assert final.estado is EstadoJob.CONCLUIDO
    assert final.resultado == "slug-produzido"
    assert final.percentual == 100


async def test_runner_registra_a_falha_em_vez_de_engolir(store):
    async def trabalho(relatar):
        raise ValueError("zip inválido")

    runner = JobRunner(store)
    job = runner.agendar("export", "gfac", trabalho)
    await runner.aguardar_tudo()

    final = store.obter(job.id)
    assert final.estado is EstadoJob.FALHOU
    assert "zip inválido" in final.erro
    assert final.terminou


async def test_progresso_e_estrangulado(store):
    """Relatar cada filme geraria centenas de escritas por job."""
    escritas = []
    original = store.progredir
    # O primeiro argumento é o job_id; só os campos de progresso interessam.
    store.progredir = lambda job_id, *a: (escritas.append(a), original(job_id, *a))[1]

    async def trabalho(relatar):
        for i in range(1, 201):
            relatar("buscando", i, 200)
        return "ok"

    runner = JobRunner(store)
    runner.agendar("export", "gfac", trabalho)
    await runner.aguardar_tudo()
    # 200 chamadas, mas só ~100 percentuais distintos.
    assert len(escritas) <= 105, f"escreveu {len(escritas)} vezes"
    assert escritas[-1] == ("buscando", 200, 200), "o fim sempre é registrado"


def test_marcar_interrompidos_fecha_jobs_orfaos(store):
    """Job 'rodando' sem tarefa viva é resto de processo morto."""
    rodando = store.criar("export", "a").id
    store.iniciar(rodando)
    concluido = store.criar("export", "b").id
    store.concluir(concluido, "b")

    assert store.marcar_interrompidos() == 1
    assert store.obter(rodando).estado is EstadoJob.INTERROMPIDO
    assert store.obter(concluido).estado is EstadoJob.CONCLUIDO


async def test_tarefa_longa_nao_e_coletada(store):
    """`create_task` guarda só referência fraca; o runner precisa segurar a tarefa."""
    import gc

    async def trabalho(relatar):
        await asyncio.sleep(0.05)
        return "sobreviveu"

    runner = JobRunner(store)
    job = runner.agendar("export", "gfac", trabalho)
    gc.collect()
    await runner.aguardar_tudo()
    assert store.obter(job.id).resultado == "sobreviveu"


def test_ativo_encontra_job_em_andamento(store):
    """Recarregar a página não pode disparar um segundo cálculo idêntico."""
    job_id = store.criar("recomendacoes", "gfac").id
    encontrado = store.ativo("recomendacoes", "gfac")
    assert encontrado is not None and encontrado.id == job_id

    store.iniciar(job_id)
    assert store.ativo("recomendacoes", "gfac").id == job_id


def test_ativo_ignora_jobs_terminados(store):
    job_id = store.criar("recomendacoes", "gfac").id
    store.concluir(job_id, "/p/gfac/recomendacoes")
    assert store.ativo("recomendacoes", "gfac") is None


def test_ativo_distingue_tipo_e_alvo(store):
    store.criar("recomendacoes", "gfac")
    assert store.ativo("recomendacoes", "outro") is None
    assert store.ativo("export", "gfac") is None


async def test_encerrar_marca_interrompido_antes_de_fechar(store):
    """Regressão: fechar o store antes das tarefas dava erro no desligamento."""
    comecou = asyncio.Event()

    async def trabalho(relatar):
        comecou.set()
        await asyncio.sleep(30)  # nunca termina sozinho
        return "/nunca"

    runner = JobRunner(store)
    job = runner.agendar("export", "gfac", trabalho)
    await comecou.wait()

    await runner.encerrar()
    # O estado tem que estar gravado, o que só é possível com o banco aberto.
    assert store.obter(job.id).estado is EstadoJob.INTERROMPIDO


async def test_encerrar_sem_tarefas_nao_reclama(store):
    await JobRunner(store).encerrar()
