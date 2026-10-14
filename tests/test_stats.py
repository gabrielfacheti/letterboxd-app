from datetime import date

import polars as pl
import pytest

from letterboxd import stats
from letterboxd.models import DiaryEntry, FilmRef, Profile
from letterboxd.tmdb.models import FilmDetails


@pytest.fixture
def frame() -> pl.DataFrame:
    """Perfil pequeno, com e sem data, para exercitar a regra central do módulo."""
    meta = {
        1: FilmDetails(tmdb_id=1, title="A", genres=["Drama"], country_names=["Japan"],
                       directors=["Kurosawa"], runtime=120, vote_average=8.0, vote_count=5000,
                       poster_path="/a.jpg"),
        2: FilmDetails(tmdb_id=2, title="B", genres=["Drama", "Horror"], country_names=["Brazil"],
                       directors=["Kurosawa"], runtime=90, vote_average=7.0, vote_count=1000),
        3: FilmDetails(tmdb_id=3, title="C", genres=["Horror"], country_names=["Brazil"],
                       directors=["Outro"], runtime=60, vote_average=0.0, vote_count=0),
    }
    entries = [
        DiaryEntry(film=FilmRef("A", 2000, tmdb_id=1), rating=5.0, watched_date=date(2024, 1, 6)),
        DiaryEntry(film=FilmRef("B", 2001, tmdb_id=2), rating=4.0, watched_date=date(2024, 1, 7)),
        # Sem data: entra nas não-temporais, sai das temporais.
        DiaryEntry(film=FilmRef("C", 2002, tmdb_id=3), rating=5.0, watched_date=None),
    ]
    return stats.build_frame(Profile("gabriel", entries), meta)


def test_entradas_sem_data_entram_nas_nao_temporais(frame):
    """Decisão de projeto: o histórico completo vale para gênero, país, etc."""
    horror = stats.por_genero(frame).filter(pl.col("valor") == "Horror")
    assert horror["filmes"].item() == 2, "o filme sem data tem que contar aqui"
    assert stats.overview(frame)["entradas"] == 3


def test_entradas_sem_data_saem_das_temporais(frame):
    """A contrapartida: nada temporal pode incluir quem não tem data."""
    assert stats.dated(frame).height == 2
    assert stats.por_ano(frame)["filmes"].sum() == 2
    assert stats.por_dia_da_semana(frame)["filmes"].sum() == 2
    assert stats.por_mes(frame)["filmes"].sum() == 2


def test_overview_expoe_a_cobertura_temporal(frame):
    """A interface precisa poder avisar sobre quantos dados o gráfico fala."""
    resumo = stats.overview(frame)
    assert resumo["com_data"] == 2
    assert resumo["cobertura_temporal"] == pytest.approx(66.7, abs=0.1)


def test_horas_somam_todas_as_entradas(frame):
    assert stats.overview(frame)["horas"] == pytest.approx((120 + 90 + 60) / 60, abs=0.1)


def test_discordancias_ignora_filmes_sem_votos(frame):
    """vote_average=0.0 com zero votos é ausência de dado, não nota baixa."""
    titulos = set(stats.discordancias(frame)["titulo"])
    assert "C" not in titulos, "filme sem votos no TMDB não pode virar discordância"


def test_melhores_exige_minimo_de_avaliacoes():
    """Uma nota só não é média: sem o filtro, quem viu um filme e adorou lidera."""
    meta = {i: FilmDetails(tmdb_id=i, title=str(i), directors=["Prolífico"] if i < 4 else ["Sortudo"])
            for i in range(1, 6)}
    entries = [
        # Prolífico: 3 filmes, 3 notas medianas.
        *(DiaryEntry(film=FilmRef(str(i), 2000, tmdb_id=i), rating=3.0) for i in range(1, 4)),
        # Sortudo: 2 filmes, só 1 avaliado, com nota máxima.
        DiaryEntry(film=FilmRef("4", 2000, tmdb_id=4), rating=5.0),
        DiaryEntry(film=FilmRef("5", 2000, tmdb_id=5), rating=None),
    ]
    frame = stats.build_frame(Profile("g", entries), meta)

    ranking = stats.melhores(frame, "direcao", min_avaliados=3)
    assert list(ranking["valor"]) == ["Prolífico"], "Sortudo tem 1 nota, não pode entrar"


def test_discordancias_traz_o_que_o_cartao_precisa(frame):
    """Pôster e id para o cartão do TMDB; a nota do TMDB segue separada da sua."""
    linha = stats.discordancias(frame).row(0, named=True)
    assert (linha["titulo"], linha["tmdb_id"], linha["poster"]) == ("A", 1, "/a.jpg")
    assert (linha["nota"], linha["nota_tmdb"], linha["diferenca"]) == (5.0, 8.0, 2.0)


def test_atividade_semanal_tem_os_sete_dias_de_segunda_a_domingo(frame):
    """6 e 7 de janeiro de 2024 foram sábado e domingo; o sem data fica de fora."""
    semana = stats.atividade_semanal(frame)
    assert [d["nome"] for d in semana] == ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
    assert [d["filmes"] for d in semana] == [0, 0, 0, 0, 0, 1, 1]


def test_atividade_anual_inclui_os_anos_vazios_do_meio():
    meta = {i: FilmDetails(tmdb_id=i, title=str(i)) for i in (1, 2, 3)}
    entries = [DiaryEntry(film=FilmRef(str(i), 2000, tmdb_id=i), watched_date=d)
               for i, d in [(1, date(2021, 5, 1)), (2, date(2021, 6, 1)), (3, date(2024, 1, 1)),
                            (3, None)]]
    frame = stats.build_frame(Profile("g", entries), meta)
    assert stats.atividade_anual(frame) == [
        {"nome": "2021", "filmes": 2, "chave": "2021"}, {"nome": "2022", "filmes": 0, "chave": "2022"},
        {"nome": "2023", "filmes": 0, "chave": "2023"}, {"nome": "2024", "filmes": 1, "chave": "2024"}]


def test_ranking_reporta_quantos_foram_avaliados(frame):
    """A contagem de avaliações precisa acompanhar a média, para a UI ser honesta."""
    linha = stats.por_direcao(frame, min_amostra=1).filter(pl.col("valor") == "Kurosawa")
    assert linha["filmes"].item() == 2
    assert linha["avaliados"].item() == 2


def test_filme_sem_metadados_nao_some_da_tabela():
    """Some-lo falsearia os totais; melhor a interface mostrar o buraco."""
    entries = [DiaryEntry(film=FilmRef("Desconhecido", 1999, tmdb_id=None), rating=4.0)]
    frame = stats.build_frame(Profile("g", entries), {})
    assert frame.height == 1
    assert stats.overview(frame)["sem_metadados"] == 1


def _perfil(entradas: list[tuple], **meta_extra) -> pl.DataFrame:
    """Monta um frame a partir de (título, tmdb_id, nota, data) por entrada."""
    meta = {
        tmdb_id: FilmDetails(tmdb_id=tmdb_id, title=titulo, directors=["Noé"],
                             genres=["Drama", "Terror", "Música"], poster_path=f"/{tmdb_id}.jpg",
                             runtime=meta_extra.get("runtime", 100))
        for titulo, tmdb_id, _, _ in entradas
    }
    entries = [
        DiaryEntry(film=FilmRef(titulo, 2018, tmdb_id=tmdb_id), rating=nota, watched_date=data)
        for titulo, tmdb_id, nota, data in entradas
    ]
    return stats.build_frame(Profile("g", entries), meta)


def test_pessoas_contam_cada_filme_uma_vez():
    """Bug real: um filme visto 8 vezes fazia o diretor ter '8 filmes'."""
    frame = _perfil([("Climax", 1, nota, None) for nota in (4.0, 4.5, 4.5, 5.0)])

    for ranking in (stats.por_direcao(frame, min_amostra=1),
                    stats.melhores(frame, "direcao", min_avaliados=1)):
        linha = ranking.row(0, named=True)
        assert linha["filmes"] == 1
        assert linha["nota_media"] == 4.5, "média das notas do filme, não de cada entrada"
        assert linha["avaliados"] == 1


def test_pessoas_ignoram_nota_ausente_ao_colapsar_o_filme():
    frame = _perfil([("Climax", 1, None, None), ("Climax", 1, 4.0, None), ("Enter", 2, None, None)])
    linha = stats.por_direcao(frame, min_amostra=1).row(0, named=True)
    assert linha["filmes"] == 2
    assert linha["nota_media"] == 4.0
    assert linha["avaliados"] == 1, "o filme sem nenhuma nota não conta como avaliado"


def test_generos_continuam_contando_por_entrada():
    """A regra de filme único é só das pessoas: gêneros medem tempo gasto."""
    frame = _perfil([("Climax", 1, 4.0, None), ("Climax", 1, 5.0, None)])
    assert stats.por_genero(frame).filter(pl.col("valor") == "Drama")["filmes"].item() == 2


def test_assistidos_recentemente_exclui_entradas_sem_data():
    frame = _perfil([
        ("Velho", 1, 3.0, date(2020, 1, 1)),
        ("Sem data", 2, 5.0, None),
        ("Novo", 3, 4.0, date(2024, 5, 1)),
        ("Médio", 4, None, date(2022, 3, 1)),
    ])
    recentes = stats.assistidos_recentemente(frame, n=3)
    assert [f["titulo"] for f in recentes] == ["Novo", "Médio", "Velho"]
    assert recentes[1]["nota"] is None
    assert recentes[0]["generos"] == ["Drama", "Terror"], "no máximo dois gêneros"


def test_assistidos_recentemente_repete_filme_revisto():
    frame = _perfil([("Climax", 1, 4.0, date(2024, 1, 1)), ("Climax", 1, 5.0, date(2024, 2, 1))])
    recentes = stats.assistidos_recentemente(frame)
    assert [f["nota"] for f in recentes] == [5.0, 4.0], "cada entrada com a própria nota"


def test_mais_bem_avaliados_une_rewatches_pela_media_e_desempata():
    frame = _perfil([
        ("Climax", 1, 4.0, date(2024, 1, 1)),
        ("Climax", 1, 5.0, date(2024, 3, 1)),     # média 4,5, visto por último em março
        ("Empate velho", 2, 4.5, date(2023, 1, 1)),
        ("Empate novo", 3, 4.5, date(2024, 6, 1)),
        ("Empate sem data", 4, 4.5, None),
        ("Sem nota", 5, None, date(2024, 7, 1)),
    ])
    top = stats.mais_bem_avaliados(frame)
    assert [f["titulo"] for f in top] == [
        "Empate novo", "Climax", "Empate velho", "Empate sem data"]
    climax = top[1]
    assert climax["nota"] == 4.5 and climax["avaliacoes"] == 2
    assert climax["data"] == date(2024, 3, 1), "data da última vez que foi visto"
    assert top[3]["data"] is None


def test_mais_bem_avaliados_nao_funde_filmes_sem_tmdb_id():
    entries = [
        DiaryEntry(film=FilmRef("X", 2000, tmdb_id=None), rating=5.0),
        DiaryEntry(film=FilmRef("Y", 2001, tmdb_id=None), rating=4.0),
    ]
    frame = stats.build_frame(Profile("g", entries), {})
    assert [f["titulo"] for f in stats.mais_bem_avaliados(frame)] == ["X", "Y"]


def test_ritmo_usa_so_entradas_com_data():
    frame = _perfil([
        ("A", 1, 4.0, date(2024, 1, 1)),    # segunda
        ("B", 2, 4.0, date(2024, 1, 8)),    # segunda
        ("C", 3, 4.0, date(2024, 1, 14)),   # domingo; janela de 14 dias = 2 semanas
        ("D", 4, 4.0, None),
    ])
    resumo = stats.overview(frame)
    assert resumo["por_semana"] == 1.5
    assert resumo["dia_mais_ativo"] == "segunda"
    assert resumo["dia_mais_ativo_entradas"] == 2


def test_ritmo_com_janela_curta_nao_dispara():
    """Três filmes num dia só não são '21 por semana'."""
    frame = _perfil([(str(i), i, 4.0, date(2024, 1, 1)) for i in range(1, 4)])
    assert stats.overview(frame)["por_semana"] == 3.0


def test_ritmo_sem_nenhuma_data():
    resumo = stats.overview(_perfil([("A", 1, 4.0, None)]))
    assert resumo["por_semana"] is None
    assert resumo["dia_mais_ativo"] is None


def test_filmes_diferentes_e_duracao_media_contam_filmes_distintos():
    frame = _perfil([("A", 1, 4.0, None), ("A", 1, 4.0, None), ("B", 2, 4.0, None)], runtime=90)
    resumo = stats.overview(frame)
    assert resumo["pct_filmes_diferentes"] == 67     # 2 de 3 entradas
    assert resumo["duracao_media"] == 90
    assert resumo["duracao_amostra"] == 2, "o rewatch não pesa duas vezes na média"


def test_duracao_media_ignora_duracao_desconhecida():
    frame = _perfil([("A", 1, 4.0, None)], runtime=0)
    resumo = stats.overview(frame)
    assert resumo["duracao_media"] is None and resumo["duracao_amostra"] == 0


def test_perfil_sem_nenhuma_data_nao_estoura_nas_estatisticas_temporais():
    """Toda entrada sem data: a coluna precisa ser Date mesmo toda nula."""
    meta = {1: FilmDetails(tmdb_id=1, title="A"), 2: FilmDetails(tmdb_id=2, title="B")}
    entries = [DiaryEntry(film=FilmRef("A", 2000, tmdb_id=1), rating=4.0),
               DiaryEntry(film=FilmRef("B", 2001, tmdb_id=2))]
    frame = stats.build_frame(Profile("g", entries), meta)

    assert frame.schema["data_assistida"] == pl.Date
    assert frame.schema["data_registro"] == pl.Date
    assert stats.assistidos_recentemente(frame) == []
    assert stats.atividade_anual(frame) == []
    assert [d["filmes"] for d in stats.atividade_semanal(frame)] == [0] * 7
    assert [m["filmes"] for m in stats.atividade_mensal(frame)] == [0] * 12
    assert stats.por_ano(frame).height == 0 and stats.por_mes(frame).height == 0
    resumo = stats.overview(frame)   # passa por `_ritmo`
    assert (resumo["com_data"], resumo["cobertura_temporal"], resumo["por_semana"],
            resumo["dia_mais_ativo"], resumo["no_ano"]) == (0, 0.0, None, None, 0)
