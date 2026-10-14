import httpx
import polars as pl
import pytest

from letterboxd import recommend, stats
from letterboxd.models import DiaryEntry, FilmRef, Profile
from letterboxd.tmdb.cache import SQLiteCache
from letterboxd.tmdb.client import TMDBClient
from letterboxd.tmdb.models import FilmDetails


def film(tmdb_id, *, genres=(), keywords=(), directors=(), countries=(),
         lang="en", ano=2000, votos=1000):
    return FilmDetails(
        tmdb_id=tmdb_id, title=f"Filme {tmdb_id}", genres=list(genres),
        keywords=list(keywords), directors=list(directors),
        country_names=list(countries), original_language=lang,
        release_date=__import__("datetime").date(ano, 1, 1), vote_count=votos,
    )


# --- AUC ---

def test_auc_separacao_perfeita():
    assert recommend.auc([3.0, 4.0], [1.0, 2.0]) == 1.0


def test_auc_ordem_invertida():
    assert recommend.auc([1.0, 2.0], [3.0, 4.0]) == 0.0


def test_auc_empate_total_e_meio():
    """Scores todos iguais significam modelo sem informação nenhuma."""
    assert recommend.auc([1.0, 1.0], [1.0, 1.0]) == 0.5


def test_auc_trata_empates_parciais():
    """Empate vale meio ponto, não zero nem um.

    Com positivos [2, 3] e negativos [1, 2] há quatro pares: três favorecem o
    positivo e um empata, logo (3 + 0,5) / 4.
    """
    assert recommend.auc([2.0, 3.0], [1.0, 2.0]) == pytest.approx(0.875)


def test_auc_sem_amostras():
    assert recommend.auc([], [1.0]) == 0.5


# --- traços ---

def test_features_marca_o_tipo_no_prefixo():
    traços = recommend.features_of(film(1, genres=["Drama"], directors=["Tarkovsky"],
                                        countries=["Japan"], ano=1979))
    assert "genero:Drama" in traços
    assert "dir:Tarkovsky" in traços
    assert "pais:Japan" in traços
    assert "decada:1970" in traços
    assert "idioma:en" in traços


def test_features_limita_o_elenco():
    d = film(1)
    d.cast = [f"Ator {i}" for i in range(20)]
    config = recommend.TasteConfig(elenco_considerado=3)
    atores = [t for t in recommend.features_of(d, config) if t.startswith("ator:")]
    assert len(atores) == 3


# --- perfil de gosto ---

@pytest.fixture
def cenario():
    """Histórico enviesado para um diretor e um país; fundo neutro."""
    meta = {
        1: film(1, directors=["Favorito"], genres=["Drama"], countries=["Japan"]),
        2: film(2, directors=["Favorito"], genres=["Drama"], countries=["Japan"]),
        3: film(3, directors=["Favorito"], genres=["Drama"], countries=["Japan"]),
    }
    entries = [
        DiaryEntry(film=FilmRef(f"Filme {i}", 2000, tmdb_id=i), rating=4.5)
        for i in meta
    ]
    frame = stats.build_frame(Profile("g", entries), meta)
    # Fundo: "Drama" é onipresente, "Favorito" e "Japan" não aparecem.
    fundo = [film(100 + i, directors=[f"Outro {i}"], genres=["Drama"],
                  countries=["USA"]) for i in range(20)]
    return frame, fundo


def test_lift_positivo_para_traco_distintivo(cenario):
    frame, fundo = cenario
    taste = recommend.build_taste(frame, fundo)
    assert taste.lift["dir:Favorito"] > 1.0
    assert taste.lift["pais:Japan"] > 1.0


def test_lift_neutro_para_traco_onipresente(cenario):
    """'Drama' está em todo o histórico e em todo o fundo: não diz nada.

    É o que dispensa uma regra especial contra gêneros genéricos — o log-odds
    zera sozinho.
    """
    frame, fundo = cenario
    taste = recommend.build_taste(frame, fundo)
    assert abs(taste.lift["genero:Drama"]) < 0.2


def test_lift_negativo_para_traco_ausente_do_historico(cenario):
    frame, fundo = cenario
    taste = recommend.build_taste(frame, fundo)
    assert taste.lift["pais:USA"] < 0


def test_top_ignora_tracos_de_uma_aparicao_so(cenario):
    """Um traço visto uma vez pode ter lift alto sem dizer nada da pessoa."""
    frame, fundo = cenario
    taste = recommend.build_taste(frame, fundo, recommend.TasteConfig(min_ocorrencias_motivo=2))
    assert all(taste.ocorrencias[t] >= 2 for t, _ in taste.top())


def test_nota_fica_fora_do_score_por_padrao(cenario):
    """Medição: somar a afinidade de nota baixou o AUC de 0,835 para 0,816."""
    frame, fundo = cenario
    taste = recommend.build_taste(frame, fundo)
    assert taste.config.peso_nota == 0.0
    # A afinidade existe (serve às explicações), mas não entra no peso.
    assert taste.afinidade
    candidato = film(999, directors=["Favorito"])
    peso_puro = taste.lift["dir:Favorito"] * recommend.PESOS_POR_TIPO["dir"]
    assert taste.peso("dir:Favorito") == pytest.approx(peso_puro)


# --- pontuação ---

def test_score_usa_media_e_nao_soma(cenario):
    """Somar premiaria acumulação: um filme com 40 keywords irrelevantes venceria."""
    frame, fundo = cenario
    taste = recommend.build_taste(frame, fundo)

    enxuto = film(900, directors=["Favorito"])
    inflado = film(901, directors=["Favorito"],
                   keywords=[f"irrelevante {i}" for i in range(40)])
    assert recommend.score_film(taste, enxuto).score > recommend.score_film(taste, inflado).score


def test_score_explica_a_recomendacao(cenario):
    frame, fundo = cenario
    taste = recommend.build_taste(frame, fundo)
    rec = recommend.score_film(taste, film(900, directors=["Favorito"], countries=["Japan"]))
    assert "Favorito" in rec.explicar()
    assert rec.motivos and all(peso > 0 for _, peso in rec.motivos)


def test_rank_exclui_assistidos_e_pouco_votados(cenario):
    frame, fundo = cenario
    taste = recommend.build_taste(frame, fundo)
    candidatos = {
        1: film(1, directors=["Favorito"]),              # já assistido
        900: film(900, directors=["Favorito"], votos=5),  # votos insuficientes
        901: film(901, directors=["Favorito"], votos=500),
    }
    ids = [r.details.tmdb_id for r in recommend.rank(taste, candidatos, min_votos=50)]
    assert ids == [901]


# --- geração de candidatos ---

@pytest.mark.anyio
async def test_sementes_limitam_filmes_por_diretor(tmp_path):
    """Regressão: ordenar por nota e cortar no topo pegava só um canto do gosto.

    Com 43 filmes empatados em 5,0, o corte arbitrário trouxe muita animação
    infantil e as recomendações saíram todas para esse lado.
    """
    pedidos: list[int] = []

    def handler(request):
        partes = request.url.path.strip("/").split("/")
        if len(partes) >= 3 and partes[1] == "movie" and partes[-1] in {"recommendations", "similar"}:
            pedidos.append(int(partes[2]))
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"id": 1, "title": "x"})

    cache = SQLiteCache(tmp_path / "c.sqlite")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TMDBClient("k", http, cache)

    # 5 filmes nota 5,0 do mesmo diretor + 1 de outro.
    meta = {i: film(i, directors=["Prolífico"]) for i in range(1, 6)}
    meta[6] = film(6, directors=["Único"])
    entries = [DiaryEntry(film=FilmRef(f"Filme {i}", 2000, tmdb_id=i), rating=5.0)
               for i in meta]
    frame = stats.build_frame(Profile("g", entries), meta)

    await recommend.gather_candidates(client, frame, sementes=10, max_por_direcao=2)
    do_prolifico = [i for i in set(pedidos) if i <= 5]
    assert len(do_prolifico) <= 2, "no máximo 2 sementes por diretor"
    assert 6 in pedidos, "o outro diretor tem que entrar nas sementes"
    cache.close()


@pytest.fixture
def anyio_backend():
    return "asyncio"
