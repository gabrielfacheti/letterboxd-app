"""O trabalho pesado que roda em segundo plano.

Separado das rotas de propósito: nada aqui conhece FastAPI, o que deixa o
pipeline testável sem subir servidor e reaproveitável por um script de linha de
comando.

O fluxo é o híbrido decidido no começo do projeto: o export dá o histórico
completo, o RSS dá as entradas recentes com o ID do TMDB já pronto, e o `merge`
junta os dois sem duplicar sessões.
"""

from __future__ import annotations

import logging

import httpx

from letterboxd import recommend, stats
from letterboxd.models import Profile
from letterboxd.sources.export import InvalidExport, parse_export
from letterboxd.sources.rss import ProfileNotFound, fetch_profile, validate_username
from letterboxd.store import ProfileStore
from letterboxd.tmdb.cache import MetadataCache
from letterboxd.tmdb.client import Progresso, TMDBClient

logger = logging.getLogger(__name__)

async def _buscar_rss(username: str, http: httpx.AsyncClient) -> Profile | None:
    """Busca o diário recente. Devolve None em vez de falhar.

    Num upload de export o RSS é um bônus — acrescenta os filmes vistos depois
    da exportação e os IDs do TMDB. Perfil privado ou fora do ar não pode
    derrubar o processamento inteiro.

    O cliente vem de fora: abrir um por job desperdiçaria o pool de conexões e
    deixaria esta função impossível de testar sem rede.
    """
    try:
        return await fetch_profile(username, http)
    except (ProfileNotFound, httpx.HTTPError) as exc:
        logger.info("RSS indisponível para %s: %s", username, exc)
        return None


async def processar(
    username: str,
    zip_bytes: bytes | None,
    client: TMDBClient,
    store: ProfileStore,
    relatar: Progresso,
    http: httpx.AsyncClient,
) -> str:
    """Monta, enriquece e salva o perfil. Devolve o caminho da página pronta.

    `relatar` é repassado ao enriquecimento, que é a parte demorada — as etapas
    anteriores são rápidas e servem só para a tela não ficar muda no início.
    """
    username = validate_username(username)

    relatar("lendo dados", 0, 1)
    do_export = parse_export(zip_bytes, username) if zip_bytes else None

    relatar("buscando diário recente", 0, 1)
    do_rss = await _buscar_rss(username, http)

    if do_rss and do_export:
        # O RSS entra como `self` porque é mais rico: traz o ID do TMDB e os
        # likes, que o export não tem.
        profile = do_rss.merge(do_export)
    elif do_export:
        profile = do_export
    elif do_rss:
        profile = do_rss
    else:
        raise ProfileNotFound(
            f"não foi possível ler nada de '{username}': o perfil pode ser "
            "privado, ou o nome está errado. Envie o export para contornar."
        )

    if not profile.entries:
        raise InvalidExport("nenhum filme encontrado neste perfil")

    meta = await client.enrich((e.film for e in profile.entries), progresso=relatar)

    # O local de nascimento enfeita a aba de pessoas, mas não é o que o usuário
    # veio buscar: falhar aqui não pode jogar fora o processamento inteiro. O
    # que já foi buscado fica em cache, e a próxima execução continua de onde
    # parou.
    try:
        await client.get_pessoas(stats.diretores_de_filmes(meta), progresso=relatar)
    except Exception:  # noqa: BLE001
        logger.warning("falha ao buscar pessoas de %s", username, exc_info=True)

    # A watchlist é enriquecida à parte: serve às recomendações, mas não pode
    # atrasar a tela de estatísticas se falhar.
    if profile.watchlist:
        try:
            await client.enrich(profile.watchlist)
        except Exception:  # noqa: BLE001
            logger.warning("falha ao enriquecer a watchlist de %s", username)

    relatar("salvando", 1, 1)
    return f"/p/{store.salvar(profile)}"


async def preparar_fundo(
    client: TMDBClient, cache: MetadataCache, relatar: Progresso
) -> str:
    """Monta o corpus de fundo usado pelas recomendações.

    Roda uma vez (ou a cada seis meses, quando o cache vence) e serve a todos os
    usuários: a linha de base é do cinema em geral, não de ninguém em particular.
    """
    relatar("montando corpus de referência", 0, 1)
    fundo = await recommend.fetch_background(client)
    cache.put_background(fundo)
    relatar("montando corpus de referência", 1, 1)
    # Não há página para este trabalho: ele serve a todos os perfis.
    return ""


class FundoIndisponivel(RuntimeError):
    """O corpus de referência ainda não foi montado."""


async def gerar_recomendacoes(
    slug: str,
    client: TMDBClient,
    cache: MetadataCache,
    store: ProfileStore,
    relatar: Progresso,
) -> str:
    """Calcula e guarda as recomendações de um perfil.

    É um job, e não trabalho de request, porque envolve até 40 consultas de
    filmes relacionados mais centenas de buscas de metadados — dezenas de
    segundos na primeira vez para cada perfil novo.
    """
    salvo = store.obter(slug)
    if salvo is None:
        raise LookupError(f"perfil desconhecido: {slug}")

    ids_fundo = cache.get_background()
    if not ids_fundo:
        raise FundoIndisponivel(
            "o corpus de referência ainda está sendo montado; tente em alguns minutos"
        )

    relatar("carregando seu histórico", 0, 1)
    meta = await client.enrich(e.film for e in salvo.profile.entries)
    frame = stats.build_frame(salvo.profile, meta)
    vistos = recommend.vistos_de(frame)

    fundo = await client.get_films(
        ids_fundo, progresso=relatar, etapa="carregando referência"
    )
    # Filmes que a pessoa já viu não podem compor a linha de base: inflariam a
    # frequência do que é dela e achatariam o contraste.
    taste = recommend.build_taste(
        frame, [d for i, d in fundo.items() if i not in vistos]
    )

    candidatos = await recommend.gather_candidates(client, frame, progresso=relatar)
    escolhidas = recommend.rank(taste, candidatos, limite=24)

    store.salvar_recomendacoes(slug, {
        "perfil_gosto": {
            rotulo: [[traco, round(peso, 3)] for traco, peso in taste.top(tipo, 6)]
            for tipo, rotulo in [("dir", "Direção"), ("kw", "Temas"),
                                 ("pais", "Países"), ("genero", "Gêneros")]
        },
        "itens": [
            {
                "tmdb_id": rec.details.tmdb_id,
                "titulo": rec.details.title,
                "ano": rec.details.release_year,
                "direcao": ", ".join(rec.details.directors[:2]),
                "poster": rec.details.poster_path,
                # A média é do TMDB. Chamá-la de "nota do Letterboxd" seria
                # falso: não temos acesso a essas notas sem raspar o site.
                "nota_tmdb": rec.details.vote_average,
                "score": round(rec.score, 4),
                "motivo": rec.explicar(4),
            }
            for rec in escolhidas
        ],
    })
    return f"/p/{slug}/recomendacoes"
