import httpx
import pytest

from letterboxd.models import FilmRef
from letterboxd.tmdb.cache import SQLiteCache
from letterboxd.tmdb.client import TMDBClient
from letterboxd.tmdb.matching import pick_match

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _movie(id_, title, year, popularity=1.0, original_title=None):
    return {"id": id_, "title": title, "original_title": original_title or title,
            "release_date": f"{year}-01-01", "popularity": popularity}


def make_client(tmp_path, handler, **kwargs):
    cache = SQLiteCache(tmp_path / "c.sqlite")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TMDBClient("chave-falsa", http, cache, **kwargs), cache


# --- heurística de correspondência (função pura) ---

@pytest.mark.parametrize("ano,esperado", [(1972, 1), (2002, 2)])
def test_ano_desambigua_homonimos(ano, esperado):
    """'Solaris' existe em 1972 e 2002; o ano tem que decidir."""
    results = [_movie(2, "Solaris", 2002, popularity=50), _movie(1, "Solaris", 1972, popularity=10)]
    assert pick_match(results, "Solaris", ano)["id"] == esperado


def test_casa_pelo_titulo_original():
    """O export traz 'Cidade de Deus'; no TMDB o title é 'City of God'."""
    results = [_movie(598, "City of God", 2002, original_title="Cidade de Deus")]
    assert pick_match(results, "Cidade de Deus", 2002)["id"] == 598


def test_aceita_ano_com_um_de_diferenca():
    """Letterboxd usa ano de produção; TMDB, data de lançamento."""
    results = [_movie(7, "Festival", 2019)]
    assert pick_match(results, "Festival", 2020)["id"] == 7


def test_recusa_quando_nenhum_titulo_bate():
    """Melhor não devolver nada do que atribuir o filme errado."""
    results = [_movie(9, "Outro Filme Qualquer", 1999)]
    assert pick_match(results, "Stalker", 1979) is None


# --- comportamento HTTP ---

async def test_404_vira_none(tmp_path):
    client, cache = make_client(tmp_path, lambda r: httpx.Response(404, json={}))
    assert await client.get_film(999999) is None
    cache.close()


async def test_respeita_retry_after_no_429(tmp_path):
    """Obedecer ao 429 é o que evita escalar de throttle para bloqueio."""
    chamadas = []

    def handler(request):
        chamadas.append(request.url.path)
        if len(chamadas) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"id": 5, "title": "Depois do Retry"})

    client, cache = make_client(tmp_path, handler)
    film = await client.get_film(5)
    assert film is not None and film.title == "Depois do Retry"
    assert len(chamadas) == 2
    cache.close()


async def test_segunda_busca_sem_ano_quando_a_primeira_falha(tmp_path):
    """Filtrar por ano pode esconder o filme certo quando as bases discordam."""
    def handler(request):
        if "year" in request.url.params:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"results": [_movie(42, "Obscuro", 1995)]})

    client, cache = make_client(tmp_path, handler)
    assert await client.resolve("Obscuro", 1994) == 42
    cache.close()


async def test_enrich_usa_o_cache_e_nao_repete_requisicoes(tmp_path):
    chamadas = []

    def handler(request):
        chamadas.append(str(request.url))
        return httpx.Response(200, json={"id": 11, "title": "Um Filme",
                                         "genres": [{"name": "Drama"}]})

    client, cache = make_client(tmp_path, handler)
    films = [FilmRef("Um Filme", 2000, tmdb_id=11), FilmRef("Um Filme", 2000, tmdb_id=11)]
    meta = await client.enrich(films)
    assert list(meta) == [11]
    assert len(chamadas) == 1, "o mesmo filme não pode ser buscado duas vezes"

    await client.enrich([FilmRef("Um Filme", 2000, tmdb_id=11)])
    assert len(chamadas) == 1, "a segunda passada tem que sair do cache"
    cache.close()


async def test_enrich_resolve_filmes_sem_id(tmp_path):
    """Filmes vindos do export chegam sem tmdb_id e precisam ser resolvidos."""
    def handler(request):
        if request.url.path == "/3/search/movie":
            return httpx.Response(200, json={"results": [_movie(77, "Stalker", 1979)]})
        return httpx.Response(200, json={"id": 77, "title": "Stalker"})

    client, cache = make_client(tmp_path, handler)
    film = FilmRef("Stalker", 1979)
    meta = await client.enrich([film])
    assert film.tmdb_id == 77, "o FilmRef tem que sair com o id preenchido"
    assert list(meta) == [77]
    cache.close()


async def test_casa_por_titulo_alternativo(tmp_path):
    """Regressão: 'The Secret World of Arrietty' é 'Arrietty' no TMDB.

    A busca acha o filme, mas nem `title` nem `original_title` (japonês) batem
    com o título que o Letterboxd usa. O título americano só existe em
    /alternative_titles.
    """
    chamadas = []

    def handler(request):
        chamadas.append(request.url.path)
        if request.url.path == "/3/search/movie":
            return httpx.Response(200, json={"results": [
                {"id": 51739, "title": "Arrietty",
                 "original_title": "借りぐらしのアリエッティ",
                 "release_date": "2010-07-16", "popularity": 10.4}]})
        if request.url.path == "/3/movie/51739/alternative_titles":
            return httpx.Response(200, json={"titles": [
                {"title": "The Borrower Arrietty"},
                {"title": "The Secret World of Arrietty"}]})
        return httpx.Response(404, json={})

    client, cache = make_client(tmp_path, handler)
    assert await client.resolve("The Secret World of Arrietty", 2010) == 51739
    assert "/3/movie/51739/alternative_titles" in chamadas
    cache.close()


async def test_nao_chuta_quando_nem_alternativos_batem(tmp_path):
    """O degrau extra não pode virar licença para adivinhar."""
    def handler(request):
        if request.url.path == "/3/search/movie":
            return httpx.Response(200, json={"results": [
                {"id": 1, "title": "Filme Totalmente Diferente",
                 "original_title": "Filme Totalmente Diferente",
                 "release_date": "2010-01-01", "popularity": 99}]})
        return httpx.Response(200, json={"titles": [{"title": "Outro Nome Ainda"}]})

    client, cache = make_client(tmp_path, handler)
    assert await client.resolve("The Secret World of Arrietty", 2010) is None
    cache.close()


async def test_limita_quantos_candidatos_checa(tmp_path):
    """Checar alternativos de dezenas de candidatos seria abuso de requisição."""
    from letterboxd.tmdb.client import ALT_TITLE_CANDIDATES

    alt_calls = []

    def handler(request):
        if request.url.path == "/3/search/movie":
            return httpx.Response(200, json={"results": [
                {"id": i, "title": f"Nao Bate {i}", "original_title": f"Nao Bate {i}",
                 "release_date": "2010-01-01", "popularity": 1} for i in range(10)]})
        alt_calls.append(request.url.path)
        return httpx.Response(200, json={"titles": []})

    client, cache = make_client(tmp_path, handler)
    assert await client.resolve("Procurado", 2010) is None
    assert len(alt_calls) == ALT_TITLE_CANDIDATES
    cache.close()


async def test_enrich_propaga_id_para_entradas_repetidas(tmp_path):
    """Regressão: rewatches geram vários FilmRef do mesmo filme.

    `enrich` deduplica antes de resolver, então só o representante recebia o
    tmdb_id — as demais entradas ficavam sem metadados e sumiam das estatísticas.
    """
    def handler(request):
        if request.url.path == "/3/search/movie":
            return httpx.Response(200, json={"results": [_movie(77, "Stalker", 1979)]})
        return httpx.Response(200, json={"id": 77, "title": "Stalker"})

    client, cache = make_client(tmp_path, handler)
    # Duas sessões do mesmo filme, cada uma com seu próprio FilmRef.
    primeiro, segundo = FilmRef("Stalker", 1979), FilmRef("Stalker", 1979)
    await client.enrich([primeiro, segundo])
    assert primeiro.tmdb_id == 77
    assert segundo.tmdb_id == 77, "a segunda sessão também precisa do id"
    cache.close()


async def test_cancelamento_nao_deixa_corrotinas_orfas(tmp_path, recwarn):
    """Regressão: no desligamento sobravam 'coroutine was never awaited'.

    Esperar corrotinas cruas deixava órfãs as que nunca começaram. Agora cada
    uma vira tarefa e o cancelamento alcança todas.
    """
    import asyncio
    import gc

    async def handler_lento(request):
        await asyncio.sleep(30)
        return httpx.Response(200, json={"id": 1, "title": "x"})

    cache = SQLiteCache(tmp_path / "c.sqlite")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler_lento))
    client = TMDBClient("k", http, cache, concurrency=2)

    tarefa = asyncio.create_task(client.get_films(range(50)))
    await asyncio.sleep(0.05)
    tarefa.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarefa

    gc.collect()
    orfas = [w for w in recwarn if "never awaited" in str(w.message)]
    assert not orfas, f"{len(orfas)} corrotina(s) órfã(s)"
    cache.close()


async def test_relacionados_saem_do_cache_na_segunda_vez(tmp_path):
    """Cada geração de candidatos repete quase os mesmos ~40 favoritos."""
    chamadas = []

    def handler(request):
        chamadas.append(request.url.path)
        return httpx.Response(200, json={"results": [_movie(3, "Vizinho", 2001)]})

    client, cache = make_client(tmp_path, handler)
    primeira = await client.related(10)
    assert [f["id"] for f in primeira] == [3, 3]
    assert await client.related(10) == primeira
    assert len(chamadas) == 2, "a segunda chamada tem que sair do cache"
    cache.close()


async def test_relacionados_parciais_nao_vao_para_o_cache(tmp_path, monkeypatch):
    """Uma falha passageira não pode ficar congelada pelos seis meses do TTL."""
    monkeypatch.setattr("letterboxd.tmdb.client.MAX_RETRIES", 1)

    def handler(request):
        if request.url.path.endswith("/similar"):
            return httpx.Response(500)
        return httpx.Response(200, json={"results": [_movie(3, "Vizinho", 2001)]})

    client, cache = make_client(tmp_path, handler)
    assert [f["id"] for f in await client.related(10)] == [3]
    assert cache.get_related(10) is None
    cache.close()
