"""Dados de pessoa do TMDB: gênero, id, local de nascimento e o que se faz com eles."""

import io
import zipfile
from datetime import date

import httpx
import pytest

from letterboxd import stats
from letterboxd.models import DiaryEntry, FilmRef, Profile
from letterboxd.nascimento import pais_de_nascimento
from letterboxd.store import ProfileStore
from letterboxd.tmdb.cache import SQLiteCache
from letterboxd.tmdb.client import TMDBClient
from letterboxd.tmdb.models import FilmDetails
from letterboxd.web import pipeline

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _cliente(tmp_path, handler):
    cache = SQLiteCache(tmp_path / "c.sqlite")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TMDBClient("chave-falsa", http, cache), cache, http


# --- créditos ---

def test_pessoas_saem_dos_creditos_com_id_e_genero():
    film = FilmDetails.from_api({
        "id": 1, "title": "X",
        "credits": {
            "crew": [{"job": "Director", "name": "Diretora", "id": 10, "gender": 1},
                     {"job": "Editor", "name": "Montador", "id": 11, "gender": 2}],
            "cast": [{"name": "Ator", "id": 20, "gender": 2},
                     # TMDB devolve 0 (ou nulo) quando não sabe.
                     {"name": "Sem Gênero", "id": 21, "gender": None},
                     {"name": "Sem Id"}],
        },
    })
    assert film.pessoas == {"Diretora": {"id": 10, "genero": 1},
                            "Ator": {"id": 20, "genero": 2},
                            "Sem Gênero": {"id": 21, "genero": 0}}, (
        "só direção e elenco guardado; quem não tem id não dá para consultar")


def test_pessoas_ausente_sobrevive_ao_json_de_cache_antigo():
    """Entrada gravada antes do campo: o JSON não tem a chave, o valor é None."""
    antigo = FilmDetails(tmdb_id=1, title="X", origin_countries=["US"])
    cru = antigo.to_json().replace('"pessoas":null,', "")
    assert "pessoas" not in cru
    assert FilmDetails.from_json(cru).pessoas is None


# --- país de nascimento ---

@pytest.mark.parametrize("local,esperado", [
    ("London, England, UK", "Reino Unido"),
    ("Edinburgh, Scotland", "Reino Unido"),
    ("Brooklyn, New York City, New York, USA", "Estados Unidos"),
    ("Chicago, Illinois, U.S.", "Estados Unidos"),
    ("Hollywood, Los Angeles, California, United States", "Estados Unidos"),
    ("Moscow, Russian SFSR, USSR [now Russia]", "Rússia"),
    ("Kiev, Ukrainian SSR, USSR [now Ukraine]", "Ucrânia"),
    ("Breslau, Germany [now Wrocław, Poland]", "Polônia"),
    ("Tbilisi, Georgian SSR, USSR", "União Soviética"),
    ("Ujiyamada [now Ise], Mie, Japan", "Japão"),   # o "now" renomeia a cidade
    ("Hamburg, Deutschland", "Alemanha"),
    ("Tokyo, Japan", "Japão"),
    ("Seoul, South Korea", "Coreia do Sul"),
    ("Munich, West Germany", "Alemanha"),
    ("São Paulo, Brazil", "Brasil"),
    ("Lyon, France ", "França"),
    ("Paramaribo, Suriname", "Suriname"),   # fora do dicionário: volta como veio
    ("", None),
    (None, None),
])
def test_pais_de_nascimento(local, esperado):
    assert pais_de_nascimento(local) == esperado


# --- cliente ---

async def test_job_rebusca_entrada_sem_pessoas_e_a_pagina_nao(tmp_path):
    chamadas = []

    def handler(request):
        chamadas.append(request.url.path)
        return httpx.Response(200, json={
            "id": 5, "title": "Filme", "origin_country": ["US"],
            "credits": {"crew": [{"job": "Director", "name": "D", "id": 1, "gender": 2}]}})

    client, cache, http = _cliente(tmp_path, handler)
    cache.put_film(FilmDetails(tmdb_id=5, title="Antigo", origin_countries=["US"]))
    filme = [FilmRef("Filme", 2000, tmdb_id=5)]

    meta = await client.enrich(filme, aceitar_incompleto=True)
    assert chamadas == [], "a página serve a entrada antiga sem ir ao TMDB"
    assert meta[5].title == "Antigo" and meta[5].pessoas is None

    meta = await client.enrich(filme)
    assert len(chamadas) == 1, "o job trata a entrada sem pessoas como ausente"
    assert meta[5].pessoas == {"D": {"id": 1, "genero": 2}}
    await http.aclose(); cache.close()


async def test_get_pessoas_pula_as_que_ja_estao_em_cache(tmp_path):
    chamadas = []

    def handler(request):
        chamadas.append(request.url.path)
        if request.url.path.endswith("/3"):
            return httpx.Response(404)
        return httpx.Response(200, json={"place_of_birth": "Paris, France"})

    client, cache, http = _cliente(tmp_path, handler)
    cache.put_person(1, {"place_of_birth": "Roma, Italy"})
    etapas = []
    await client.get_pessoas([1, 2, 3, 2], progresso=lambda *a: etapas.append(a))

    assert sorted(chamadas) == ["/3/person/2", "/3/person/3"]
    assert cache.get_people([1, 2, 3]) == {
        1: {"place_of_birth": "Roma, Italy"},
        2: {"place_of_birth": "Paris, France"},
        3: {"place_of_birth": None},  # 404 também é resposta
    }
    assert etapas[-1] == ("buscando pessoas", 2, 2)

    await client.get_pessoas([1, 2, 3])
    assert len(chamadas) == 2, "nada a buscar na segunda vez"
    await http.aclose(); cache.close()


# --- job ---

def _zip_com_tres_filmes() -> bytes:
    linhas = "\n".join(
        f"2024-03-0{i},{titulo},1979,https://boxd.it/{i},4,,,2024-03-0{i}"
        for i, titulo in enumerate(["Stalker", "Solaris", "Mirror"], start=1))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("diary.csv", "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n"
                   + linhas + "\n")
    return buffer.getvalue()


async def test_job_busca_so_a_direcao(tmp_path):
    """O elenco não tem local de nascimento na interface: consultar milhares de
    atores um a um seria abuso do TMDB para dado que ninguém veria."""
    ids = {"Stalker": 1, "Solaris": 2, "Mirror": 3}
    pessoas_chamadas = []

    def handler(request):
        if request.url.host != "api.themoviedb.org":
            return httpx.Response(404)
        if request.url.path == "/3/search/movie":
            titulo = request.url.params["query"]
            return httpx.Response(200, json={"results": [{
                "id": ids[titulo], "title": titulo, "original_title": titulo,
                "release_date": "1979-01-01", "popularity": 1.0}]})
        if request.url.path.startswith("/3/person/"):
            pessoas_chamadas.append(int(request.url.path.rsplit("/", 1)[-1]))
            return httpx.Response(200, json={"place_of_birth": "Paris, France"})
        id_ = int(request.url.path.rsplit("/", 1)[-1])
        # Um ator em todos os filmes, outro só em cada um: nenhum é buscado.
        return httpx.Response(200, json={
            "id": id_, "title": f"F{id_}", "release_date": "1979-01-01", "origin_country": ["US"],
            "credits": {"crew": [{"job": "Director", "name": "Diretor", "id": 10, "gender": 2}],
                        "cast": [{"name": "Fixo", "id": 20, "gender": 1},
                                 {"name": f"Avulso {id_}", "id": 30 + id_, "gender": 2}]}})

    client, cache, http = _cliente(tmp_path, handler)
    store = ProfileStore(tmp_path / "perfis.sqlite")
    await pipeline.processar("gabriel", _zip_com_tres_filmes(), client, store,
                             lambda *a: None, http)
    assert pessoas_chamadas == [10]

    await pipeline.processar("gabriel", _zip_com_tres_filmes(), client, store,
                             lambda *a: None, http)
    assert pessoas_chamadas == [10], "já em cache: não se busca de novo"
    await http.aclose(); cache.close(); store.close()


# --- estatísticas ---

def _frame(entradas, filmes):
    """`entradas`: (tmdb_id, nota, data); `filmes`: FilmDetails por id."""
    return stats.build_frame(
        Profile("g", [DiaryEntry(film=FilmRef(filmes[i].title, filmes[i].release_year, tmdb_id=i),
                                 rating=nota, watched_date=data)
                      for i, nota, data in entradas]),
        filmes)


def _filme(id_, titulo, ano, diretores, elenco=(), runtime=100, poster=None):
    pessoas = {nome: {"id": i, "genero": g} for nome, i, g in [*diretores, *elenco]}
    return FilmDetails(tmdb_id=id_, title=titulo, release_date=date(ano, 1, 1), runtime=runtime,
                       directors=[n for n, _, _ in diretores], cast=[n for n, _, _ in elenco],
                       poster_path=poster, pessoas=pessoas, origin_countries=["US"])


NOLAN = ("Nolan", 1, 2)


@pytest.fixture
def nolan():
    """Nolan com 3 filmes, um revisto duas vezes (e com notas diferentes)."""
    filmes = {i: _filme(i, t, ano, [NOLAN], runtime=r, poster=f"/{i}.jpg")
              for i, t, ano, r in [(1, "Tenet", 2020, 150), (2, "Memento", 2000, 113),
                                   (3, "Inception", 2010, 148)]}
    entradas = [(1, 4.0, date(2024, 1, 1)), (2, 5.0, date(2024, 1, 2)),
                (2, 4.0, date(2024, 2, 1)),            # rewatch de Memento
                (3, None, date(2024, 1, 3))]
    return _frame(entradas, filmes), filmes


def test_filmes_da_pessoa_somam_rewatch_na_nota_e_ordenam_por_ano(nolan):
    frame, _ = nolan
    [pessoa] = stats.ranking_de_pessoas(frame, "direcao")
    assert [f["titulo"] for f in pessoa["lista"]] == ["Memento", "Inception", "Tenet"]
    memento, inception, tenet = pessoa["lista"]
    assert (memento["nota"], memento["avaliacoes"]) == (4.5, 2), "média das duas sessões"
    assert (inception["nota"], inception["avaliacoes"]) == (None, 0)
    assert tenet["tmdb_id"] == 1 and tenet["poster"] == "/1.jpg"
    assert (pessoa["filmes"], pessoa["avaliados"]) == (3, 2)


def test_horas_da_pessoa_contam_as_revisoes(nolan):
    """Filmes distintos são 3, mas o tempo gasto é de 4 sessões."""
    frame, _ = nolan
    [pessoa] = stats.ranking_de_pessoas(frame, "direcao")
    assert pessoa["filmes"] == 3
    assert pessoa["minutos"] == 150 + 113 + 113 + 148


def test_ranking_de_pessoas_desempata_pelo_nome():
    filmes = {i: _filme(i, f"F{i}", 2000 + i, [("Zeca", 1, 2), ("Ana", 2, 1)]) for i in (1, 2, 3)}
    frame = _frame([(i, 4.0, None) for i in (1, 2, 3)], filmes)
    assert [p["nome"] for p in stats.ranking_de_pessoas(frame, "direcao")] == ["Ana", "Zeca"]


def test_genero_conta_cada_diretor_uma_vez_e_mantem_o_desconhecido():
    filmes = {
        1: _filme(1, "A", 2000, [("Ana", 1, 1)], [("Beto", 2, 2)]),
        2: _filme(2, "B", 2001, [("Ana", 1, 1), ("Dani", 4, 3), ("Eli", 5, 0)]),
    }
    diretores = stats.diretores_de_filmes(filmes)
    assert set(diretores) == {1, 4, 5}, "o elenco não entra"
    genero = stats.diversidade_de_genero(diretores)
    por_nome = {f["nome"]: f["n"] for f in genero["fatias"]}
    assert por_nome == {"Feminino": 1, "Masculino": 0, "Não-binário": 1, "Desconhecido": 1}
    assert genero["total"] == 3, "Ana aparece em dois filmes e conta uma vez"
    assert sum(f["pct"] for f in genero["fatias"]) == pytest.approx(100, abs=0.05)
    assert stats.diversidade_de_genero({}) is None


def test_filme_de_cache_antigo_nao_conta_para_o_genero():
    antigo = FilmDetails(tmdb_id=1, title="A", directors=["Ana"], pessoas=None)
    assert stats.diretores_de_filmes({1: antigo}) == {}


def test_aba_de_elenco_nao_tem_genero_nem_paises(nolan):
    frame, filmes = nolan
    assert {"genero", "paises"} <= set(stats.aba_de_pessoas(frame, filmes, {}, "direcao"))
    assert not {"genero", "paises"} & set(stats.aba_de_pessoas(frame, filmes, {}, "elenco"))


def test_ranking_por_nota_exige_avaliacoes_e_desempata(nolan):
    """Nolan tem 2 notas em 3 filmes: abaixo do mínimo, fora do ranking por nota."""
    frame, _ = nolan
    assert stats.ranking_de_pessoas(frame, "direcao", por="nota") == []

    filmes = {i: _filme(i, f"F{i}", 2000 + i, [("Zeca", 1, 2), ("Ana", 2, 1), ("Beto", 3, 2)])
              for i in (1, 2, 3, 4)}
    entradas = [(1, 5.0, None), (2, 5.0, None), (3, 5.0, None), (4, 4.0, None)]
    frame = _frame(entradas, filmes)
    ranking = stats.ranking_de_pessoas(frame, "direcao", por="nota", n=2)
    # Mesma nota e mesmas avaliações: o nome decide.
    assert [(p["nome"], p["nota_media"], p["avaliados"]) for p in ranking] == [
        ("Ana", 4.75, 4), ("Beto", 4.75, 4)]
    assert len(ranking[0]["lista"]) == 4, "cada linha leva os filmes da pessoa"


def test_paises_de_nascimento_separa_sem_local_de_nunca_buscado():
    pessoas = {1: ("A", 0), 2: ("B", 0), 3: ("C", 0), 4: ("D", 0)}
    paises = stats.paises_de_nascimento(
        pessoas, {1: "Paris, France", 2: "Lyon, France", 3: None})
    assert paises["linhas"] == [{"valor": "França", "pessoas": 2, "pct": 100.0}]
    assert (paises["conhecidos"], paises["sem_local"], paises["total"]) == (2, 2, 4)
    assert stats.paises_de_nascimento(pessoas, {}) is None, "ninguém buscado = perfil antigo"


def test_filmes_do_pais_filtra_a_origem_e_ordena():
    """Origem, não coprodução; rewatch é um filme; sem nota por último."""
    def filme(id_, titulo, ano, origem, paises=("US",)):
        return FilmDetails(tmdb_id=id_, title=titulo, release_date=date(ano, 1, 1),
                           origin_countries=origem, countries=list(paises), pessoas={})
    filmes = {1: filme(1, "Beta", 2001, ["FR"]), 2: filme(2, "Alfa", 2001, ["FR"]),
              3: filme(3, "Velho", 1950, ["FR", "IT"]), 4: filme(4, "Sem Nota", 1990, ["FR"]),
              5: filme(5, "Coprodução", 2000, ["US"], paises=("US", "FR")),
              6: filme(6, "Topo", 2010, ["FR"])}
    entradas = [(1, 4.0, None), (2, 4.0, None), (3, 4.0, None), (4, None, None),
                (5, 5.0, None), (6, 5.0, None), (6, 4.5, None)]
    frame = _frame(entradas, filmes)

    def itens(iso):
        filtro, _ = stats.analisar_dimensao("pais", iso)
        return stats.itens_da_dimensao(frame, "pais", filtro)

    resultado = itens("fr")   # minúsculas valem
    assert resultado["tipo"] == "filmes" and resultado["total"] == 5
    assert [(f["titulo"], f["nota"], f["avaliacoes"]) for f in resultado["itens"]] == [
        ("Topo", 4.75, 2),           # média das duas sessões
        ("Velho", 4.0, 1),           # empate em 4: o ano mais antigo primeiro
        ("Alfa", 4.0, 1), ("Beta", 4.0, 1),   # mesmo ano: por título
        ("Sem Nota", None, 0)]       # sem nota, por último
    assert "Coprodução" not in [f["titulo"] for f in resultado["itens"]]
    assert itens("JP")["itens"] == []
