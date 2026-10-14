"""Cliente do TMDB: resolução de título→ID e busca de metadados.

Duas operações, com custos bem diferentes:

- **detalhes** (`/movie/{id}`) — uma requisição por filme, precisa e barata,
  usada quando já temos o ID (tudo que vem do RSS);
- **resolução** (`/search/movie`) — necessária para o export, que só tem título
  e ano. É a parte imprecisa do projeto; a heurística mora em `matching`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable

import httpx

from letterboxd.models import FilmRef, normalize_title
from letterboxd.tmdb.cache import MetadataCache
from letterboxd.tmdb.matching import pick_match, year_of
from letterboxd.tmdb.models import FilmDetails

logger = logging.getLogger(__name__)

# Recebe (etapa, feitos, total). Existe porque enriquecer um export leva dezenas
# de segundos: sem relato de progresso, a interface só pode mostrar um spinner
# sem fim.
Progresso = Callable[[str, int, int], None]

BASE_URL = "https://api.themoviedb.org/3"

# O TMDB tolera ~50 req/s. Ficamos bem abaixo: o gargalo aqui não é o tempo
# total e sim não parecer abuso vindo de um IP residencial.
DEFAULT_CONCURRENCY = 8
MAX_RETRIES = 3


# Quantos candidatos checar por título alternativo antes de desistir. Passar
# disso significa que a busca foi ruim, não que o filme tem título alternativo.
ALT_TITLE_CANDIDATES = 3


class TMDBError(RuntimeError):
    """Falha de comunicação com o TMDB depois de esgotadas as tentativas."""


class TMDBClient:
    """Acesso ao TMDB com cache na frente e concorrência limitada.

    O cache entra por injeção para que o resto do código não saiba se por trás
    existe SQLite, Postgres ou nada.
    """

    def __init__(
        self,
        api_key: str,
        http: httpx.AsyncClient,
        cache: MetadataCache,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self._key = api_key
        self._http = http
        self._cache = cache
        self._semaphore = asyncio.Semaphore(concurrency)

    async def _get(self, path: str, **params: object) -> dict | None:
        """GET com retry. Devolve None em 404, levanta `TMDBError` no resto.

        O 404 é esperado: filme removido do TMDB, ID inválido vindo de um feed
        antigo. Não é erro a ponto de interromper o enriquecimento inteiro.
        """
        params = {"api_key": self._key, **params}
        delay = 1.0

        for attempt in range(1, MAX_RETRIES + 1):
            async with self._semaphore:
                try:
                    response = await self._http.get(f"{BASE_URL}{path}", params=params)
                except httpx.RequestError as exc:
                    if attempt == MAX_RETRIES:
                        raise TMDBError(f"falha de rede em {path}: {exc}") from exc
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue

            if response.status_code == 404:
                return None
            if response.status_code == 429:
                # O TMDB informa quanto esperar; respeitar isso é o que evita
                # escalar de throttle para bloqueio.
                wait = float(response.headers.get("Retry-After", delay))
                logger.warning("TMDB pediu espera de %.1fs em %s", wait, path)
                await asyncio.sleep(wait)
                continue
            if response.status_code >= 500:
                if attempt == MAX_RETRIES:
                    raise TMDBError(f"TMDB respondeu {response.status_code} em {path}")
                await asyncio.sleep(delay)
                delay *= 2
                continue

            response.raise_for_status()
            return response.json()

        raise TMDBError(f"tentativas esgotadas em {path}")

    async def get_films(
        self,
        ids: Iterable[int],
        *,
        progresso: Progresso | None = None,
        etapa: str = "metadados",
        aceitar_incompleto: bool = False,
    ) -> dict[int, FilmDetails]:
        """Busca vários filmes em paralelo, respeitando o limite de concorrência."""
        alvos = list(dict.fromkeys(ids))
        obtidos = await self._gather_com_progresso(
            [self.get_film(i, aceitar_incompleto=aceitar_incompleto) for i in alvos],
            progresso, etapa,
        )
        return {f.tmdb_id: f for f in obtidos if f is not None}

    @staticmethod
    async def _gather_com_progresso(
        corrotinas: list,
        progresso: Progresso | None,
        etapa: str,
    ) -> list:
        """`gather` que avisa a cada conclusão, preservando a ordem de entrada.

        `as_completed` daria o progresso mas perderia a ordem; envolver cada
        corrotina num contador mantém as duas coisas.
        """
        total = len(corrotinas)
        if progresso:
            progresso(etapa, 0, total)
        if not total:
            return []

        # Agendar como tarefas antes de esperar é o que permite cancelar todas
        # no desligamento. Esperando corrotinas cruas, as que nunca chegaram a
        # começar ficariam órfãs — "coroutine was never awaited" em cada
        # reinício com trabalho em voo.
        tarefas = [asyncio.ensure_future(c) for c in corrotinas]
        feitos = 0

        async def contando(tarefa: asyncio.Future):
            nonlocal feitos
            resultado = await tarefa
            feitos += 1
            if progresso:
                progresso(etapa, feitos, total)
            return resultado

        try:
            return await asyncio.gather(*(contando(t) for t in tarefas))
        except BaseException:
            for tarefa in tarefas:
                tarefa.cancel()
            await asyncio.gather(*tarefas, return_exceptions=True)
            raise

    async def get_film(
        self, tmdb_id: int, *, aceitar_incompleto: bool = False
    ) -> FilmDetails | None:
        """Metadados completos de um filme, do cache quando possível.

        `aceitar_incompleto` é para o caminho da página, onde rebuscar centenas
        de filmes por causa de um campo novo estouraria o tempo do request. O
        job de processamento não usa: é ele quem completa as entradas.
        """
        cached = self._cache.get_film(tmdb_id, aceitar_incompleto=aceitar_incompleto)
        if cached is not None:
            return cached

        # `append_to_response` traz detalhes, créditos e keywords numa
        # requisição só, em vez de três.
        payload = await self._get(
            f"/movie/{tmdb_id}", append_to_response="credits,keywords"
        )
        if payload is None:
            return None

        film = FilmDetails.from_api(payload)
        self._cache.put_film(film)
        return film

    async def _match_by_alt_titles(
        self, results: list[dict], title: str, year: int | None
    ) -> dict | None:
        """Último recurso: compara com os títulos alternativos dos candidatos.

        Um filme costuma ter um título por país, e o Letterboxd não usa
        necessariamente o mesmo que o TMDB adotou como canônico. O caso que
        motivou isto: "The Secret World of Arrietty" (título americano) é
        `Arrietty` no TMDB, com `original_title` em japonês — a busca acha o
        filme, mas nenhum dos dois títulos bate.

        `/alternative_titles` resolve isso com precisão, sem chute. Custa uma
        requisição extra, mas só para os poucos filmes que chegam até aqui.
        """
        target = normalize_title(title)
        candidatos = [
            item for item in results
            if year is None
            or (year_of(item) is not None and abs(year_of(item) - year) <= 1)
        ]

        for item in candidatos[:ALT_TITLE_CANDIDATES]:
            payload = await self._get(f"/movie/{item['id']}/alternative_titles")
            alternativos = {
                normalize_title(entry.get("title") or "")
                for entry in (payload or {}).get("titles") or []
            }
            if target in alternativos:
                logger.info("%r casou por título alternativo -> %s", title, item["id"])
                return item
        return None

    async def resolve(self, title: str, year: int | None) -> int | None:
        """Descobre o ID do TMDB a partir de título e ano (o caso do export).

        Resultados negativos também são guardados: um filme que o TMDB não tem
        não deve ser pesquisado de novo a cada execução.
        """
        cached, tmdb_id = self._cache.get_resolution(title, year)
        if cached:
            return tmdb_id

        params: dict[str, object] = {"query": title}
        if year is not None:
            params["year"] = year

        payload = await self._get("/search/movie", **params)
        results = (payload or {}).get("results") or []
        match = pick_match(results, title, year)

        # Filtrar por ano pode esconder o filme certo quando as bases discordam.
        # Só então vale repetir a busca sem a restrição.
        if match is None and year is not None:
            payload = await self._get("/search/movie", query=title)
            extra = (payload or {}).get("results") or []
            # Os candidatos das duas buscas alimentam o degrau seguinte.
            vistos = {item["id"] for item in results}
            results += [item for item in extra if item["id"] not in vistos]
            match = pick_match(results, title, year)

        if match is None:
            match = await self._match_by_alt_titles(results, title, year)

        resolved = int(match["id"]) if match else None
        if resolved is None:
            logger.info("sem correspondência no TMDB: %r (%s)", title, year)
        self._cache.put_resolution(title, year, resolved)
        return resolved

    async def related(self, tmdb_id: int) -> list[dict]:
        """Filmes que o TMDB associa a um filme dado.

        Combina `/recommendations` (comportamento agregado dos usuários) e
        `/similar` (semelhança de gênero e keyword). Aqui isso serve apenas para
        **gerar candidatos** — a ordenação é feita depois, contra o perfil de
        gosto do usuário. O TMDB responde "quem é parecido"; quem responde
        "o que *você* vai gostar" é o nosso ranqueamento.
        """
        cached = self._cache.get_related(tmdb_id)
        if cached is not None:
            return cached

        respostas = await asyncio.gather(
            self._get(f"/movie/{tmdb_id}/recommendations"),
            self._get(f"/movie/{tmdb_id}/similar"),
            return_exceptions=True,
        )
        candidatos: list[dict] = []
        completo = True
        for resposta in respostas:
            if isinstance(resposta, BaseException):
                logger.warning("falha ao buscar relacionados de %s: %s", tmdb_id, resposta)
                completo = False
                continue
            candidatos += (resposta or {}).get("results") or []
        # Lista parcial não vai para o cache: uma falha passageira ficaria
        # congelada por seis meses.
        if completo:
            self._cache.put_related(tmdb_id, candidatos)
        return candidatos

    async def related_many(
        self,
        ids: Iterable[int],
        *,
        progresso: Progresso | None = None,
        etapa: str = "explorando favoritos",
    ) -> list[list[dict]]:
        """Relacionados de vários filmes, relatando progresso."""
        alvos = list(dict.fromkeys(ids))
        return await self._gather_com_progresso(
            [self.related(i) for i in alvos], progresso, etapa
        )

    async def enrich(
        self,
        films: Iterable[FilmRef],
        *,
        progresso: Progresso | None = None,
        aceitar_incompleto: bool = False,
    ) -> dict[int, FilmDetails]:
        """Resolve e busca metadados de vários filmes, em paralelo.

        Preenche `tmdb_id` nos `FilmRef` que vieram sem ele (os do export),
        então o chamador também recebe as referências corrigidas de brinde.
        """
        todos = list(films)
        unique: dict[tuple[str, int | None], FilmRef] = {}
        for film in todos:
            unique.setdefault(film.match_key, film)

        async def resolve_one(film: FilmRef) -> None:
            if film.tmdb_id is None:
                film.tmdb_id = await self.resolve(film.title, film.year)

        # Duas etapas com custos distintos: resolver títulos (só o export precisa)
        # e buscar metadados. A interface mostra as duas separadamente.
        pendentes = [f for f in unique.values() if f.tmdb_id is None]
        await self._gather_com_progresso(
            [resolve_one(f) for f in pendentes], progresso, "identificando filmes"
        )

        # O mesmo filme pode aparecer em várias entradas (rewatch), cada uma com
        # seu próprio FilmRef. Só o representante foi resolvido acima; sem
        # propagar o id, as demais entradas ficariam sem metadados.
        for film in todos:
            if film.tmdb_id is None:
                film.tmdb_id = unique[film.match_key].tmdb_id

        ids = {f.tmdb_id for f in unique.values() if f.tmdb_id is not None}
        return await self.get_films(
            ids, progresso=progresso, etapa="buscando metadados",
            aceitar_incompleto=aceitar_incompleto,
        )

    async def get_pessoas(
        self,
        ids: Iterable[int],
        *,
        progresso: Progresso | None = None,
        etapa: str = "buscando pessoas",
    ) -> None:
        """Guarda no cache o local de nascimento de quem ainda não está lá.

        Não devolve nada: o local só é lido depois, direto do cache, pela página.
        Quem já está em cache e dentro da validade não é pedido de novo.
        """
        alvos = list(dict.fromkeys(ids))
        guardados = self._cache.get_people(alvos)
        await self._gather_com_progresso(
            [self._buscar_pessoa(i) for i in alvos if i not in guardados],
            progresso, etapa,
        )

    async def _buscar_pessoa(self, person_id: int) -> None:
        payload = await self._get(f"/person/{person_id}")
        # O 404 também vai para o cache (sem local): a pessoa saiu do TMDB e
        # perguntar de novo a cada processamento não mudaria a resposta.
        local = (payload or {}).get("place_of_birth") or None
        self._cache.put_person(person_id, {"place_of_birth": local})
