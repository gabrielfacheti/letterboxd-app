import pytest

from letterboxd.web import mapa


def test_escala_logaritmica_mantem_os_menores_visiveis():
    """Com um país dominante, a escala linear apagaria o resto do mapa."""
    m = mapa.montar({"US": 321, "BR": 3}, {})
    por_iso = {p["iso"]: p for p in m["paises"]}
    assert por_iso["US"]["intensidade"] == 1
    assert por_iso["BR"]["intensidade"] == pytest.approx(0.24, abs=0.01)
    assert por_iso["FR"]["filmes"] == 0 and por_iso["FR"]["intensidade"] == 0


def test_o_que_nao_cabe_no_mapa_e_dito():
    """País extinto não tem contorno; sumir em silêncio esconderia filmes."""
    m = mapa.montar({"US": 10, "SU": 4}, {"SU": "Soviet Union"})
    assert m["fora"] == [("Soviet Union", 4)]


def test_historico_sem_paises():
    m = mapa.montar({}, {})
    assert m["maximo"] == 0 and m["fora"] == []


@pytest.mark.parametrize("paises,rotulo", [
    (1, "Fica em casa"), (10, "Fica em casa"),
    (11, "Turista de fim de semana"), (25, "Turista de fim de semana"),
    (26, "Viajante"), (50, "Viajante"),
    (51, "Desbravador"), (120, "Desbravador"),
])
def test_rotulo_de_explorador_nas_fronteiras(paises, rotulo):
    assert mapa.explorador(paises)[0] == rotulo


def test_frase_acompanha_o_rotulo():
    assert mapa.explorador(10) == ("Fica em casa", "Quase tudo vem de poucos lugares")
    assert mapa.explorador(51) == ("Desbravador", "Vai aonde o cinema estiver")


def test_painel_lista_o_top_10_e_conta_o_resto():
    contagem = {"US": 60, "FR": 20, "SU": 10, **{f"X{i}": 1 for i in range(10)}}
    p = mapa.painel(contagem, {"SU": "Soviet Union"}, entradas=100)
    assert p["paises"] == 13
    assert p["restantes"] == 3
    assert len(p["top"]) == 10
    assert p["mais_visto"] == "Estados Unidos", "nome em português, do mapa"
    assert [(t["iso"], t["nome"], t["filmes"], t["pct"]) for t in p["top"][:3]] == [
        ("US", "Estados Unidos", 60, 60), ("FR", "França", 20, 20),
        # Sem contorno no mapa: vale o nome em inglês do TMDB.
        ("SU", "Soviet Union", 10, 10)]
    assert p["rotulo"] == "Turista de fim de semana"


def test_painel_sem_filmes():
    p = mapa.painel({}, {}, entradas=0)
    assert (p["paises"], p["mais_visto"], p["top"], p["restantes"]) == (0, None, [], 0)
