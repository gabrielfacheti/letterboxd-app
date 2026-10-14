"""Aplicação web.

Um processo único e persistente, que é o que o servidor de casa oferece: os
stores e o cliente HTTP vivem no `lifespan` e são compartilhados por todos os
requests, em vez de reabertos a cada um.

As rotas são finas de propósito — todo o trabalho está em `pipeline`, `stats` e
`recommend`, que não sabem que existe um servidor web.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from letterboxd import stats
from letterboxd.config import load_settings
from letterboxd.jobs import EstadoJob, JobRunner, JobStore
from letterboxd.sources.export import MAX_ZIP_BYTES
from letterboxd.sources.rss import InvalidUsername
from letterboxd.store import ProfileStore
from letterboxd.tmdb.cache import SQLiteCache
from letterboxd.tmdb.client import TMDBClient
from letterboxd.web import mapa, pipeline

logger = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Um app público faz o servidor de casa buscar RSS e TMDB a pedido de qualquer
# um. O limite existe para que ele não vire proxy de flood contra o Letterboxd.
JANELA_SEGUNDOS = 60
MAX_PEDIDOS_POR_JANELA = 5


@dataclass
class Recursos:
    """Tudo que vive enquanto o processo vive."""

    cache: SQLiteCache
    perfis: ProfileStore
    jobs: JobStore
    runner: JobRunner
    http: httpx.AsyncClient
    tmdb: TMDBClient


class LimitadorDeTaxa:
    """Contador de pedidos por IP numa janela deslizante.

    Em memória de propósito: o app é um processo só, e persistir isso em disco
    resolveria um problema que não existe.
    """

    def __init__(self, maximo: int, janela: float) -> None:
        self._maximo, self._janela = maximo, janela
        self._registros: dict[str, deque[float]] = defaultdict(deque)

    def permitir(self, chave: str) -> bool:
        agora = time.monotonic()
        marcas = self._registros[chave]
        while marcas and agora - marcas[0] > self._janela:
            marcas.popleft()
        if len(marcas) >= self._maximo:
            return False
        marcas.append(agora)
        return True


limitador = LimitadorDeTaxa(MAX_PEDIDOS_POR_JANELA, JANELA_SEGUNDOS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Recursos já presentes vêm de fora (testes, ou um embutidor qualquer): quem
    # os criou é responsável por fechá-los. Este é o ponto de costura que
    # permite exercitar as rotas sem tocar a rede nem o `.env`.
    if getattr(app.state, "recursos", None) is not None:
        yield
        return

    settings = load_settings()
    cache = SQLiteCache(settings.cache_path)
    perfis = ProfileStore(settings.data_dir / "perfis.sqlite")
    jobs = JobStore(settings.data_dir / "jobs.sqlite")

    # Jobs marcados como rodando são restos de um processo anterior: sem isto a
    # tela de progresso deles giraria para sempre.
    jobs.marcar_interrompidos()
    jobs.limpar_antigos()

    # O mesmo cliente atende TMDB e Letterboxd: identificar-se é cortesia
    # mínima para quem serve um feed público de graça.
    http = httpx.AsyncClient(
        timeout=30,
        headers={"User-Agent": "letterboxd-stats (projeto pessoal)"},
        follow_redirects=True,
    )
    runner = JobRunner(jobs)
    app.state.recursos = Recursos(
        cache=cache, perfis=perfis, jobs=jobs, runner=runner,
        http=http, tmdb=TMDBClient(settings.tmdb_key, http, cache),
    )

    # O corpus de fundo serve a todo mundo e leva minutos. É montado uma vez, em
    # segundo plano, para não bloquear a subida do app.
    if not cache.get_background():
        runner.agendar("fundo", "corpus", lambda relatar: pipeline.preparar_fundo(
            app.state.recursos.tmdb, cache, relatar))

    try:
        yield
    finally:
        # A ordem importa: as tarefas ainda escrevem o estado final ao serem
        # canceladas, então os stores só podem fechar depois delas.
        await runner.encerrar()
        await http.aclose()
        cache.close()
        perfis.close()
        jobs.close()


app = FastAPI(title="Estatísticas do Letterboxd", lifespan=lifespan)
# Só a biblioteca de exportar cartões como imagem (html-to-image, vendorada em
# static/vendor). Imagens continuam vindo direto do TMDB para o navegador.
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


def recursos(request: Request) -> Recursos:
    return request.app.state.recursos


@app.get("/", response_class=HTMLResponse)
async def inicio(request: Request):
    return TEMPLATES.TemplateResponse(request, "index.html", {})


@app.post("/processar")
async def processar(
    request: Request,
    usuario: str = Form(...),
    arquivo: UploadFile | None = None,
):
    """Agenda o processamento e manda o visitante para a tela de progresso."""
    res = recursos(request)
    # Atrás do túnel toda conexão vem do cloudflared; o IP real chega no
    # cabeçalho. Confiar nele só é seguro porque o compose não publica porta:
    # sem passar pelo Cloudflare, ninguém alcança o app para forjá-lo.
    ip = request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "desconhecido")
    if not limitador.permitir(ip):
        raise HTTPException(429, "Muitos pedidos. Tente de novo em um minuto.")

    try:
        from letterboxd.sources.rss import validate_username
        usuario = validate_username(usuario)
    except InvalidUsername as exc:
        raise HTTPException(400, str(exc)) from exc

    conteudo: bytes | None = None
    if arquivo is not None and arquivo.filename:
        # Ler primeiro e checar depois deixaria um ZIP de 2 GB entrar na memória.
        tamanho = int(request.headers.get("content-length") or 0)
        if tamanho > MAX_ZIP_BYTES:
            raise HTTPException(413, "Arquivo grande demais.")
        conteudo = await arquivo.read()
        if len(conteudo) > MAX_ZIP_BYTES:
            raise HTTPException(413, "Arquivo grande demais.")

    async def trabalho(relatar):
        return await pipeline.processar(
            usuario, conteudo, res.tmdb, res.perfis, relatar, res.http)

    job = res.runner.agendar("export" if conteudo else "rss", usuario, trabalho)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def progresso(request: Request, job_id: str):
    job = recursos(request).jobs.obter(job_id)
    if job is None:
        raise HTTPException(404, "Job não encontrado.")
    return TEMPLATES.TemplateResponse(request, "progresso.html", {"job": job})


@app.get("/jobs/{job_id}/estado")
async def estado(request: Request, job_id: str):
    """Consultado pela tela de progresso a cada segundo."""
    job = recursos(request).jobs.obter(job_id)
    if job is None:
        raise HTTPException(404, "Job não encontrado.")
    return JSONResponse({
        "estado": str(job.estado),
        "etapa": job.etapa,
        "percentual": job.percentual,
        "feitos": job.feitos,
        "total": job.total,
        "erro": job.erro,
        "terminou": job.terminou,
        # O próprio trabalho decide para onde mandar o usuário; a rota não
        # precisa deduzir isso a partir do tipo do job.
        "destino": job.resultado or None,
    })


async def _carregar(res: Recursos, slug: str):
    salvo = res.perfis.obter(slug)
    if salvo is None:
        raise HTTPException(404, "Perfil não encontrado.")
    # Vem tudo do cache: o enriquecimento já aconteceu no job. Entrada de
    # esquema antigo é servida como está: rebuscá-la aqui custaria dezenas de
    # segundos dentro do request. O job completa os campos novos.
    meta = await res.tmdb.enrich(
        (e.film for e in salvo.profile.entries), aceitar_incompleto=True)
    return salvo, stats.build_frame(salvo.profile, meta), meta


def _nomes_de_paises(meta) -> dict[str, str]:
    """Nome do país por código, para o mapa poder dizer o que ficou de fora. A
    origem só traz códigos; os nomes vêm dos países de produção, que quase
    sempre incluem a origem (senão o código aparece cru)."""
    nomes: dict[str, str] = {}
    for details in meta.values():
        nomes.update(zip(details.countries, details.country_names))
    return nomes


@app.get("/p/{slug}", response_class=HTMLResponse)
async def perfil(request: Request, slug: str):
    res = recursos(request)
    salvo, frame, meta = await _carregar(res, slug)

    # Foto de cada pessoa, por nome. As estatísticas trabalham com nomes; o mapa
    # existe para a interface poder ilustrar sem mudar aquele contrato.
    fotos: dict[str, str] = {}
    for details in meta.values():
        fotos.update(details.fotos)
    nomes_paises = _nomes_de_paises(meta)

    # Local de nascimento só do cache, nunca do TMDB: buscar é trabalho do job.
    locais = {i: dados.get("place_of_birth")
              for i, dados in res.cache.get_people(stats.diretores_de_filmes(meta)).items()}

    resumo = stats.overview(frame)
    por_pais = stats.filmes_por_codigo_de_pais(frame)
    generos, idiomas = stats.por_genero(frame), stats.por_idioma(frame)
    return TEMPLATES.TemplateResponse(request, "perfil.html", {
        "slug": slug,
        "username": salvo.profile.username,
        "atualizado": salvo.atualizado_em,
        "resumo": resumo,
        "fotos": fotos,
        "anual": stats.atividade_anual(frame),
        "atividade": stats.atividade_mensal(frame),
        "semanal": stats.atividade_semanal(frame),
        # Visão geral
        "recentes": stats.assistidos_recentemente(frame),
        "melhores_filmes": stats.mais_bem_avaliados(frame),
        "generos": generos.head(10).to_dicts(),
        "generos_resumo": stats.explorados(generos),
        "mapa": mapa.montar(por_pais, nomes_paises),
        "cinema": mapa.painel(por_pais, nomes_paises, resumo["entradas"]),
        "decadas": stats.por_decada(frame).to_dicts(),
        "notas": stats.distribuicao_de_notas(frame).to_dicts(),
        # Aprofundamento
        "por_mes": stats.por_mes(frame).to_dicts(),
        "idiomas": idiomas.head(10).to_dicts(),
        "idiomas_resumo": stats.explorados(idiomas),
        "fama": stats.popular_ou_garimpo(frame),
        "duracao": stats.duracao_dos_filmes(frame),
        "revisitas": stats.revisitas(frame),
        "keywords": stats.por_keyword(frame).head(15).to_dicts(),
        "discordancias": stats.discordancias(frame, 6).to_dicts(),
        # Pessoas
        "pessoas": {papel: stats.aba_de_pessoas(frame, meta, locais, papel)
                    for papel in ("direcao", "elenco")},
    })


@app.get("/p/{slug}/ranking/{dimensao}.json")
async def ranking_da_dimensao(request: Request, slug: str, dimensao: str):
    """Ranking inteiro de uma dimensão, para a lista do gráfico expandido.

    Só o cache: nada aqui chega ao TMDB.
    """
    if dimensao not in stats.DIMENSOES_DE_LISTA:
        raise HTTPException(404, "Dimensão desconhecida.")
    _, frame, _ = await _carregar(recursos(request), slug)
    return JSONResponse({"dimensao": dimensao, "linhas": stats.ranking_completo(frame, dimensao)})


@app.get("/p/{slug}/itens/{dimensao}.json")
async def itens_da_dimensao(request: Request, slug: str, dimensao: str, valor: str = ""):
    """Sessões ou filmes de um valor de uma dimensão (uma coluna, uma linha, um país).

    O valor vai em query e não no caminho: palavras-chave podem ter "/". É
    validado antes de qualquer carga; só o cache é lido, nunca o TMDB.
    """
    filtro = stats.analisar_dimensao(dimensao, valor)
    if filtro is None:
        raise HTTPException(404, "Dimensão ou valor inválido.")
    expr, nome = filtro
    _, frame, meta = await _carregar(recursos(request), slug)
    resultado = stats.itens_da_dimensao(frame, dimensao, expr)
    if dimensao == "pais":
        nome = mapa.nome_do_pais(valor.strip().upper(), _nomes_de_paises(meta))
    # `date` não vira JSON sozinho.
    return JSONResponse(jsonable_encoder({"dimensao": dimensao, "nome": nome, **resultado}))


@app.get("/p/{slug}/recomendacoes.json")
async def recomendacoes_json(request: Request, slug: str):
    """Estado das recomendações, para a aba montar sozinha.

    Agendar acontece aqui e não no carregamento da página: quem só quer ver
    estatísticas não deve disparar meio minuto de trabalho sem pedir.
    """
    res = recursos(request)
    if res.perfis.obter(slug) is None:
        raise HTTPException(404, "Perfil não encontrado.")

    pronto = res.perfis.obter_recomendacoes(slug)
    if pronto is not None:
        return JSONResponse({"situacao": "pronto", "dados": pronto})

    if not res.cache.get_background():
        return JSONResponse({"situacao": "sem_referencia"})

    job = res.jobs.ativo("recomendacoes", slug) or _agendar_recomendacoes(res, slug)
    return JSONResponse({"situacao": "calculando", "job": job.id})


def _agendar_recomendacoes(res: Recursos, slug: str):
    async def trabalho(relatar):
        return await pipeline.gerar_recomendacoes(
            slug, res.tmdb, res.cache, res.perfis, relatar)

    return res.runner.agendar("recomendacoes", slug, trabalho)


@app.get("/p/{slug}/recomendacoes")
async def recomendacoes(request: Request, slug: str):
    """Rota antiga: as recomendações agora são uma aba da página do perfil.

    Mantida porque a tela de progresso manda o usuário para cá ao terminar o
    cálculo, e porque links antigos devem continuar funcionando.
    """
    if recursos(request).perfis.obter(slug) is None:
        raise HTTPException(404, "Perfil não encontrado.")
    return RedirectResponse(f"/p/{slug}#recommendations", status_code=303)
