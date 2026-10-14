"""Cartões do Deepdive: gêneros e idiomas explorados, fama dos filmes e duração."""

from datetime import date

import polars as pl
import pytest

from letterboxd import stats
from letterboxd.idiomas import nome_do_idioma
from letterboxd.models import DiaryEntry, FilmRef, Profile
from letterboxd.tmdb.models import FilmDetails


def _frame(filmes: list[dict], entradas: list[tuple] | None = None) -> pl.DataFrame:
    """`filmes`: kwargs de FilmDetails (tmdb_id e title obrigatórios).
    `entradas`: (tmdb_id, nota) por entrada; por padrão, uma sem nota por filme."""
    meta = {f["tmdb_id"]: FilmDetails(**{"release_date": date(2000, 1, 1),
                                         "original_language": "en", **f})
            for f in filmes}
    entradas = entradas or [(f["tmdb_id"], None) for f in filmes]
    return stats.build_frame(Profile("g", [
        DiaryEntry(film=FilmRef(meta[i].title, 2000, tmdb_id=i), rating=nota) for i, nota in entradas
    ]), meta)


# --- nomes de idioma ---

@pytest.mark.parametrize("codigo,nome", [
    ("en", "Inglês"), ("ja", "Japonês"), ("ka", "Georgiano"),
    ("cn", "Cantonês"),          # o TMDB usa "cn", que não é ISO 639-1
    ("xx", "Sem idioma"),
    ("zz", "zz"),                # desconhecido volta como veio
])
def test_nome_do_idioma(codigo, nome):
    assert nome_do_idioma(codigo) == nome


def test_idiomas_aparecem_pelo_nome_e_agrupam_codigos_iguais():
    frame = _frame([
        {"tmdb_id": 1, "title": "A", "original_language": "ja"},
        {"tmdb_id": 2, "title": "B", "original_language": "ja"},
        {"tmdb_id": 3, "title": "C", "original_language": "cn"},
    ])
    assert dict(stats.por_idioma(frame).select("valor", "filmes").iter_rows()) == {
        "Japonês": 2, "Cantonês": 1}


# --- explorados ---

def test_explorados_conta_distintos_e_desempata_o_menos_assistido_pelo_nome():
    frame = _frame([
        {"tmdb_id": 1, "title": "A", "genres": ["Drama", "Terror"]},
        {"tmdb_id": 2, "title": "B", "genres": ["Drama", "Animação"]},
        {"tmdb_id": 3, "title": "C", "genres": ["Drama"]},
    ])
    r = stats.explorados(stats.por_genero(frame))
    # Terror e Animação têm 1 entrada cada: vence o nome, Animação.
    assert r == {"total": 3, "menos": "Animação", "entradas": 1}
    assert stats.explorados(pl.DataFrame(schema={"valor": pl.String, "filmes": pl.UInt32})) == {
        "total": 0, "menos": None, "entradas": 0}


def test_explorados_de_idiomas_usa_o_nome_em_portugues():
    frame = _frame([
        {"tmdb_id": 1, "title": "A", "original_language": "en"},
        {"tmdb_id": 2, "title": "B", "original_language": "en"},
        {"tmdb_id": 3, "title": "C", "original_language": "ka"},
    ])
    assert stats.explorados(stats.por_idioma(frame)) == {
        "total": 2, "menos": "Georgiano", "entradas": 1}


# --- popular ou garimpo ---

def test_pontuacao_de_fama_pela_formula_de_votos():
    pontuacao = lambda *votos: stats.popular_ou_garimpo(_frame(  # noqa: E731
        [{"tmdb_id": i, "title": str(i), "vote_count": v} for i, v in enumerate(votos, 1)]
    ))["pontuacao"]
    assert pontuacao(0) == 0, "ninguém votou: obscuro de verdade"
    assert pontuacao(100) == 0, "até o piso de 100 votos, fama zero"
    assert pontuacao(1000) == 40       # log10(1000/100) / log10(30000/100)
    assert pontuacao(30000) == 100
    assert pontuacao(500000) == 100, "acima do teto de 30 mil, a fama segura"
    assert pontuacao(100, 30000) == 50   # média dos dois


def test_fama_conta_filmes_distintos_e_so_os_com_metadados():
    frame = _frame(
        [{"tmdb_id": 1, "title": "A", "vote_count": 30000},
         {"tmdb_id": 2, "title": "B", "vote_count": 0}],
        entradas=[(1, None), (1, None), (1, None), (2, None)])   # A foi revisto
    assert stats.popular_ou_garimpo(frame)["pontuacao"] == 50, "rewatch não pesa três vezes"
    sem_metadados = stats.build_frame(
        Profile("g", [DiaryEntry(film=FilmRef("X", 2000, tmdb_id=None))]), {})
    assert stats.popular_ou_garimpo(sem_metadados) is None


@pytest.mark.parametrize("pontuacao,rotulo", [
    (0, "Garimpeiro"), (44, "Garimpeiro"), (45, "Equilibrado"), (64, "Equilibrado"),
    (65, "Segue o público"), (79, "Segue o público"), (80, "Arrasa-quarteirão"),
    (100, "Arrasa-quarteirão"),
])
def test_perfis_de_fama_nas_fronteiras(pontuacao, rotulo):
    assert stats.perfil_de_fama(pontuacao)[0] == rotulo


def test_achados_sao_os_bem_avaliados_menos_votados():
    frame = _frame(
        [{"tmdb_id": 1, "title": "Famoso", "vote_count": 9000},
         {"tmdb_id": 2, "title": "Obscuro B", "vote_count": 10},
         {"tmdb_id": 3, "title": "Obscuro A", "vote_count": 10},
         {"tmdb_id": 4, "title": "Mal avaliado", "vote_count": 1},
         {"tmdb_id": 5, "title": "Sem nota", "vote_count": 2},
         {"tmdb_id": 6, "title": "Rewatch", "vote_count": 5}],
        entradas=[(1, 5.0), (2, 4.0), (3, 4.5), (4, 3.5), (5, None),
                  (6, 5.0), (6, 3.0)])   # média 4,0 com duas sessões
    achados = stats.popular_ou_garimpo(frame)["achados"]
    # Rewatch (5 votos) vem antes dos de 10; entre os de 10, a nota maior (A, 4,5).
    assert [(a["titulo"], a["nota"], a["avaliacoes"]) for a in achados] == [
        ("Rewatch", 4.0, 2), ("Obscuro A", 4.5, 1)]
    assert achados[0]["tmdb_id"] == 6


def test_sem_achados_quando_nada_chega_a_quatro():
    frame = _frame([{"tmdb_id": 1, "title": "A", "vote_count": 1}], entradas=[(1, 3.5)])
    assert stats.popular_ou_garimpo(frame)["achados"] == []


# --- duração ---

def test_faixas_de_duracao_nas_fronteiras():
    filmes = [{"tmdb_id": i, "title": f"F{i}", "runtime": m}
              for i, m in enumerate([89, 90, 120, 121, 150, 151], 1)]
    d = stats.duracao_dos_filmes(_frame(filmes))
    assert [(f["valor"], f["detalhe"], f["filmes"], f["pct"]) for f in d["faixas"]] == [
        ("Curtos", "< 90 min", 1, 16.7), ("Padrão", "90–120 min", 2, 33.3),
        ("Longos", "121–150 min", 2, 33.3), ("Épicos", "> 150 min", 1, 16.7)]


def test_duracao_media_e_a_mesma_da_visao_geral_e_extremos_desempatam_por_titulo():
    filmes = [{"tmdb_id": 1, "title": "Zeta", "runtime": 7},
              {"tmdb_id": 2, "title": "Beta", "runtime": 200},
              {"tmdb_id": 3, "title": "Alfa", "runtime": 200},
              {"tmdb_id": 4, "title": "Sem duração", "runtime": 0}]   # 0 = desconhecida
    frame = _frame(filmes, entradas=[(1, None), (2, None), (2, None), (3, None), (4, None)])
    d = stats.duracao_dos_filmes(frame)
    resumo = stats.overview(frame)
    assert (d["duracao_media"], d["duracao_amostra"]) == (
        resumo["duracao_media"], resumo["duracao_amostra"]) == (136, 3)
    assert sum(f["filmes"] for f in d["faixas"]) == 3, "o de duração 0 fica de fora"
    assert d["curto"]["titulo"] == "Zeta" and d["curto"]["duracao"] == 7
    assert d["longo"]["titulo"] == "Alfa", "empate em 200: o título"
    assert stats.duracao_dos_filmes(_frame([{"tmdb_id": 1, "title": "A"}])) is None


def test_ranking_completo_vai_alem_do_topo_que_o_cartao_mostra():
    """O cartão mostra 10 gêneros e 15 temas; o diálogo, a lista inteira."""
    frame = _frame([{"tmdb_id": i, "title": str(i),
                     "genres": [f"G{n:02d}" for n in range(20)],
                     "keywords": [f"k{n:02d}" for n in range(20)]} for i in (1, 2, 3)])
    assert len(stats.ranking_completo(frame, "genero")) == 20
    temas = stats.ranking_completo(frame, "tema")
    assert len(temas) == 20 and temas[0]["pct"] is None, "tema sem porcentagem, como o cartão"
    assert stats.ranking_completo(frame, "duracao") == [], "sem duração conhecida, sem faixas"


def test_o_valor_do_idioma_na_lista_e_o_codigo_e_o_nome_e_portugues():
    frame = _frame([{"tmdb_id": 1, "title": "A", "original_language": "cn"}])
    assert stats.ranking_completo(frame, "idioma") == [
        {"valor": "cn", "nome": "Cantonês", "filmes": 1, "pct": 100.0}]
    filtro, nome = stats.analisar_dimensao("idioma", "cn")
    assert nome == "Cantonês"
    assert stats.itens_da_dimensao(frame, "idioma", filtro)["total"] == 1


# --- revisitas ---

def _com_datas(filmes: list[dict], sessoes: list[tuple]) -> pl.DataFrame:
    """`sessoes`: (tmdb_id, nota, data[, rewatch]) por entrada, na ordem dada."""
    meta = {f["tmdb_id"]: FilmDetails(**{"release_date": date(2000, 1, 1), **f}) for f in filmes}
    return stats.build_frame(Profile("g", [
        DiaryEntry(film=FilmRef(meta[i].title, 2000, tmdb_id=i), rating=nota, watched_date=data,
                   rewatch=bool(resto and resto[0]))
        for i, nota, data, *resto in sessoes]), meta)


def test_revisto_e_o_filme_com_duas_ou_mais_sessoes():
    frame = _com_datas(
        [{"tmdb_id": 1, "title": "A", "runtime": 100}, {"tmdb_id": 2, "title": "B", "runtime": 50},
         {"tmdb_id": 3, "title": "C", "runtime": 80}],
        [(1, 4.0, date(2024, 1, 1)), (1, 4.5, date(2024, 2, 1)), (1, 5.0, date(2024, 3, 1)),
         (2, 3.0, date(2024, 1, 5)), (2, None, date(2024, 1, 6)),
         (3, 4.0, date(2024, 1, 7))])   # C: uma sessão só, fora
    r = stats.revisitas(frame)
    assert r["filmes"] == 2, "A tem 3 sessões e conta uma vez"
    assert r["minutos"] == 100 * 2 + 50 * 1, "sessões de revisita: 2 de A e 1 de B"
    assert [f["titulo"] for f in r["lista"]] == ["A", "B"]
    assert [f["sessoes"] for f in r["lista"]] == [3, 2]
    assert "C" not in [f["titulo"] for f in r["lista"]]


def test_destaque_desempata_pela_sessao_mais_recente_e_depois_pelo_titulo():
    filmes = [{"tmdb_id": i, "title": t} for i, t in [(1, "Zeta"), (2, "Alfa"), (3, "Beta")]]
    frame = _com_datas(filmes, [
        (1, 4.0, date(2024, 1, 1)), (1, 4.0, date(2024, 5, 1)),     # Zeta: última em maio
        (2, 4.0, date(2024, 1, 1)), (2, 4.0, date(2024, 2, 1)),     # Alfa: fevereiro
        (3, 4.0, date(2024, 1, 1)), (3, 4.0, date(2024, 5, 1))])    # Beta: maio, como Zeta
    r = stats.revisitas(frame)
    assert [f["titulo"] for f in r["lista"]] == ["Beta", "Zeta", "Alfa"]
    assert r["destaque"]["titulo"] == "Beta"


def test_linha_do_tempo_vai_do_mais_antigo_ao_mais_novo_e_deixa_as_sem_data_no_fim():
    frame = _com_datas([{"tmdb_id": 1, "title": "A"}], [
        (1, 5.0, date(2024, 3, 1)), (1, None, None), (1, 4.0, date(2024, 1, 1)),
        (1, 4.5, date(2024, 2, 1))])
    d = stats.revisitas(frame)["destaque"]
    assert d["sessoes"] == 4
    assert [(s["data"], s["nota"]) for s in d["linha_do_tempo"]] == [
        (date(2024, 1, 1), 4.0), (date(2024, 2, 1), 4.5), (date(2024, 3, 1), 5.0), (None, None)]
    # Média com a amostra: 3 das 4 sessões têm nota.
    assert (d["nota_media"], d["avaliadas"]) == (4.5, 3)


def test_generos_dos_revistos_contam_cada_filme_uma_vez_e_agrupam_o_resto_em_outros():
    frame = _com_datas(
        [{"tmdb_id": 1, "title": "A", "genres": ["Drama", "Romance", "G3", "G4", "G5", "G6", "G7"]},
         {"tmdb_id": 2, "title": "B", "genres": ["Drama"]},
         {"tmdb_id": 3, "title": "Solo", "genres": ["Terror"]}],
        [(1, None, date(2024, 1, 1)), (1, None, date(2024, 1, 2)), (1, None, date(2024, 1, 3)),
         (2, None, date(2024, 1, 1)), (2, None, date(2024, 1, 2)),
         (3, None, date(2024, 1, 1))])
    fatias = stats.revisitas(frame)["generos"]
    # Drama 2 (A e B, uma vez cada, mesmo com A em 3 sessões); os demais 1.
    assert [(f["nome"], f["n"]) for f in fatias] == [
        ("Drama", 2), ("G3", 1), ("G4", 1), ("G5", 1), ("G6", 1), ("Outros", 2)]
    assert sum(f["pct"] for f in fatias) == pytest.approx(100, abs=0.2)
    assert "Terror" not in [f["nome"] for f in fatias], "Solo não foi revisto"


def test_sem_revisitas():
    frame = _com_datas([{"tmdb_id": 1, "title": "A"}], [(1, 4.0, date(2024, 1, 1))])
    assert stats.revisitas(frame) == {"filmes": 0}
    sem_id = stats.build_frame(Profile("g", [
        DiaryEntry(film=FilmRef("X", 2000, tmdb_id=None), watched_date=date(2024, 1, d))
        for d in (1, 2)]), {})
    assert stats.revisitas(sem_id) == {"filmes": 0}, "sem tmdb_id não dá para saber que é o mesmo filme"


def test_filme_marcado_como_rewatch_conta_mesmo_com_uma_sessao_so():
    frame = _com_datas(
        [{"tmdb_id": 1, "title": "Marcado", "runtime": 70},
         {"tmdb_id": 2, "title": "Comum", "runtime": 90}],
        [(1, 4.0, date(2024, 1, 1), True), (2, 4.0, date(2024, 1, 2))])
    r = stats.revisitas(frame)
    assert r["filmes"] == 1
    assert [(f["titulo"], f["sessoes"]) for f in r["lista"]] == [("Marcado", 1)], "×1"
    assert r["minutos"] == 70, "a única sessão do filme marcado é de revisita"


def test_sessoes_de_revisita_sao_as_marcadas_ou_as_que_nao_sao_a_mais_antiga():
    def minutos(*sessoes):
        return stats.revisitas(_com_datas([{"tmdb_id": 1, "title": "A", "runtime": 10}],
                                          [(1, None, *s) for s in sessoes]))["minutos"]
    jan, fev = date(2024, 1, 1), date(2024, 2, 1)
    assert minutos((jan,), (fev,)) == 10                       # a mais antiga não conta
    assert minutos((fev,), (jan,), (None,)) == 20              # sem data vem depois das datadas
    assert minutos((None,), (None,), (None,)) == 20            # tudo sem data: só uma escapa
    assert minutos((jan, True), (fev,)) == 20                  # a mais antiga, marcada, conta
    assert minutos((jan,), (fev, True)) == 10                  # marcada e não-primeira: uma vez só


def test_destaque_desempata_pela_nota_media_antes_da_sessao_mais_recente():
    filmes = [{"tmdb_id": i, "title": t} for i, t in [(1, "Antigo bom"), (2, "Novo ruim"),
                                                      (3, "Sem nota"), (4, "Novo bom")]]
    frame = _com_datas(filmes, [
        (1, 5.0, date(2024, 1, 1)), (1, 4.0, date(2024, 1, 2)),     # média 4,5, janeiro
        (2, 3.0, date(2024, 6, 1)), (2, 3.0, date(2024, 6, 2)),     # média 3, junho
        (3, None, date(2024, 7, 1)), (3, None, date(2024, 7, 2)),   # sem nota, julho
        (4, 4.5, date(2024, 5, 1)), (4, 4.5, date(2024, 5, 2))])    # média 4,5, maio
    r = stats.revisitas(frame)
    # Empate em sessões: nota média (4,5 dos dois primeiros), então a mais recente
    # (maio antes de janeiro); sem nota perde de quem tem, ainda que mais recente.
    assert [f["titulo"] for f in r["lista"]] == ["Novo bom", "Antigo bom", "Novo ruim", "Sem nota"]
    assert r["destaque"]["titulo"] == "Novo bom"


def test_diretores_revistos_e_o_diretor_destaque():
    filmes = [
        {"tmdb_id": 1, "title": "A", "directors": ["Ana", "Beto"]},   # dois diretores: ambos contam
        {"tmdb_id": 2, "title": "B", "directors": ["Ana"]},
        {"tmdb_id": 3, "title": "C", "directors": ["Beto"]},
        {"tmdb_id": 4, "title": "Solo", "directors": ["Cris"]},       # visto uma vez: fora
    ]
    sessoes = [(1, 4.0, date(2024, 1, 1)), (1, 4.0, date(2024, 1, 2)),
               (2, 4.0, date(2024, 2, 1)), (2, 4.0, date(2024, 2, 2)),
               (3, 4.0, date(2024, 3, 1)), (3, 4.0, date(2024, 3, 2)), (3, 4.0, date(2024, 3, 3)),
               (4, 4.0, date(2024, 4, 1))]
    r = stats.revisitas(_com_datas(filmes, sessoes))
    assert r["diretores"] == 2, "Ana e Beto; Cris só tem filme visto uma vez"
    # Ana e Beto têm 2 filmes revistos cada; Beto tem mais sessões de revisita (1 + 2 = 3, Ana 1 + 1 = 2).
    assert r["diretor"]["nome"] == "Beto" and r["diretor"]["filmes"] == 2
    assert sorted(t["titulo"] for t in r["diretor"]["titulos"]) == ["A", "C"]
    # Empate total em filmes e sessões: o nome decide.
    igual = _com_datas([{"tmdb_id": 1, "title": "A", "directors": ["Zeca"]},
                        {"tmdb_id": 2, "title": "B", "directors": ["Ana"]}],
                       [(1, 4.0, date(2024, 1, 1)), (1, 4.0, date(2024, 1, 2)),
                        (2, 4.0, date(2024, 2, 1)), (2, 4.0, date(2024, 2, 2))])
    assert stats.revisitas(igual)["diretor"]["nome"] == "Ana"
    sem_diretor = _com_datas([{"tmdb_id": 1, "title": "A"}],
                             [(1, 4.0, date(2024, 1, 1)), (1, 4.0, date(2024, 1, 2))])
    assert stats.revisitas(sem_diretor)["diretor"] is None
