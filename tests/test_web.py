import io
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from letterboxd.jobs import JobRunner, JobStore
from letterboxd.store import ProfileStore
from letterboxd.tmdb.cache import SQLiteCache
from letterboxd.tmdb.client import TMDBClient
from letterboxd.web import app as modulo_app
from letterboxd.web.app import Recursos, app


_IDS = {"Stalker": 77, "Solaris": 78, "Mirror": 79}
# Só a direção (id 3) tem o local de nascimento buscado; o ator (id 4) não.
_NASCIMENTOS = {3: "Moscow, Russian SFSR, USSR [now Russia]"}


def _tmdb_falso(request: httpx.Request) -> httpx.Response:
    """Responde a tudo do TMDB; o feed do Letterboxd não existe (perfil sem RSS)."""
    if request.url.host != "api.themoviedb.org":
        return httpx.Response(404)
    if request.url.path == "/3/search/movie":
        titulo = request.url.params["query"]
        return httpx.Response(200, json={"results": [{
            "id": _IDS[titulo], "title": titulo, "original_title": titulo,
            "release_date": "1979-01-01", "popularity": 9.0}]})
    id_ = int(request.url.path.rsplit("/", 1)[-1])
    if request.url.path.startswith("/3/person/"):
        return httpx.Response(200, json={"id": id_, "place_of_birth": _NASCIMENTOS[id_]})
    return httpx.Response(200, json={
        "id": id_, "title": next(t for t, i in _IDS.items() if i == id_),
        "release_date": "1979-01-01",
        "runtime": 162, "original_language": "ru", "vote_average": 8.1,
        "vote_count": 3000, "genres": [{"name": "Drama"}],
        "production_countries": [{"iso_3166_1": "SU", "name": "Soviet Union"}],
        "origin_country": ["SU"],
        "credits": {
            "crew": [{"job": "Director", "name": "Andrei Tarkovsky", "id": 3, "gender": 2}],
            "cast": [{"name": "Anatoly Solonitsyn", "id": 4, "gender": 2}],
        },
        "keywords": {"keywords": [{"name": "zone"}]},
    })


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    monkeypatch.setattr(modulo_app, "limitador",
                        modulo_app.LimitadorDeTaxa(1000, 60))
    cache = SQLiteCache(tmp_path / "cache.sqlite")
    perfis = ProfileStore(tmp_path / "perfis.sqlite")
    jobs = JobStore(tmp_path / "jobs.sqlite")
    http = httpx.AsyncClient(transport=httpx.MockTransport(_tmdb_falso))
    app.state.recursos = Recursos(
        cache=cache, perfis=perfis, jobs=jobs, runner=JobRunner(jobs),
        http=http, tmdb=TMDBClient("chave", http, cache),
    )
    with TestClient(app) as c:
        yield c
    cache.close(); perfis.close(); jobs.close()
    app.state.recursos = None


@pytest.fixture
def export_bytes():
    """Três filmes do mesmo diretor, o primeiro visto em três sessões.

    O ranking de direção exige 3 filmes, e as sessões repetidas contam uma só
    vez. Datas distintas, que é como o Letterboxd representa rewatch — e o
    caminho que já quebrou uma vez, quando o `enrich` só resolvia a primeira.
    """
    linhas = "\n".join(
        [f"2024-03-0{i},Stalker,1979,https://boxd.it/{i},5,,,2024-03-0{i}" for i in (1, 2, 3)]
        + ["2024-03-04,Solaris,1979,https://boxd.it/4,4,,,2024-03-04",
           "2024-03-05,Mirror,1979,https://boxd.it/5,4,,,2024-03-05"]
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("diary.csv",
                   "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n"
                   + linhas + "\n")
    return buffer.getvalue()


def test_perfil_sem_nenhuma_data_renderiza_com_graficos_vazios(cliente):
    """Quem importou do IMDb e nunca usou o diário tem só `watched.csv`: nenhuma
    data. A coluna de datas ficaria nula e todo `.dt` estouraria."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("watched.csv", "Date,Name,Year,Letterboxd URI\n"
                   + "\n".join(f"2020-01-0{i},{t},1979,https://boxd.it/{i}"
                               for i, t in enumerate(["Stalker", "Solaris", "Mirror"], start=1))
                   + "\n")
    perfil = _processar_perfil(cliente, buffer.getvalue())
    assert perfil.startswith("/p/")
    pagina = cliente.get(perfil)
    assert pagina.status_code == 200
    assert "<strong>0</strong>" in pagina.text, "o aviso de cobertura diz 0 entradas com data"
    for aba in ("overview", "deepdive", "people"):
        assert f'id="aba-{aba}"' in pagina.text


def test_pagina_inicial(cliente):
    r = cliente.get("/")
    assert r.status_code == 200
    # O aviso sobre o profile.csv é uma promessa ao usuário; não pode sumir.
    assert "profile.csv" in r.text


def test_usuario_invalido_e_recusado(cliente):
    """O nome entra numa URL do Letterboxd: barrar path traversal é obrigatório."""
    assert cliente.post("/processar", data={"usuario": "../admin"}).status_code == 400


def test_limite_de_taxa(cliente, monkeypatch):
    """Um app público não pode virar proxy de flood contra o Letterboxd."""
    monkeypatch.setattr(modulo_app, "limitador", modulo_app.LimitadorDeTaxa(2, 60))
    codigos = [cliente.post("/processar", data={"usuario": "alguem"},
                            follow_redirects=False).status_code for _ in range(3)]
    assert codigos[:2] == [303, 303]
    assert codigos[2] == 429


def test_limite_de_taxa_usa_o_ip_real_atras_do_tunel(cliente, monkeypatch):
    """Atrás do Cloudflare Tunnel toda conexão vem do cloudflared.

    Sem ler o IP que o Cloudflare repassa, todos os visitantes dividiriam a
    mesma cota.
    """
    monkeypatch.setattr(modulo_app, "limitador", modulo_app.LimitadorDeTaxa(1, 60))

    def pedir(ip):
        return cliente.post("/processar", data={"usuario": "alguem"},
                            headers={"CF-Connecting-IP": ip},
                            follow_redirects=False).status_code

    assert [pedir("203.0.113.1"), pedir("203.0.113.2")] == [303, 303]
    assert pedir("203.0.113.1") == 429


def test_fluxo_completo_do_export(cliente, export_bytes):
    r = cliente.post("/processar", data={"usuario": "gabriel"},
                     files={"arquivo": ("export.zip", export_bytes, "application/zip")},
                     follow_redirects=False)
    assert r.status_code == 303
    job_id = r.headers["location"].rsplit("/", 1)[-1]

    for _ in range(200):
        estado = cliente.get(f"/jobs/{job_id}/estado").json()
        if estado["terminou"]:
            break
    assert estado["estado"] == "concluido", estado
    destino = estado["destino"]
    assert destino.startswith("/p/")

    pagina = cliente.get(destino)
    assert pagina.status_code == 200
    assert "Tarkovsky" in pagina.text
    assert "Soviet Union" in pagina.text
    # 5 entradas, mas 3 filmes: rewatch não infla o ranking de pessoas.
    assert '<span class="valor">3</span>' in pagina.text
    assert "Assistidos recentemente" in pagina.text
    assert "Mais bem avaliados" in pagina.text
    overview = pagina.text[:pagina.text.index('id="aba-deepdive"')]
    assert 'href="https://www.themoviedb.org/movie/77" target="_blank"' in overview, (
        "os cartões da visão geral levam ao TMDB")


def test_aba_de_pessoas_tem_sub_abas_rankings_e_so_a_direcao_tem_pizza(cliente, export_bytes):
    perfil = _processar_perfil(cliente, export_bytes)
    html = cliente.get(perfil).text
    pessoas = html[html.index('id="aba-people"'):html.index('id="aba-recommendations"')]
    diretores = pessoas[pessoas.index('id="sub-direcao"'):pessoas.index('id="sub-elenco"')]
    elenco = pessoas[pessoas.index('id="sub-elenco"'):]

    for aba in (diretores, elenco):
        assert aba.count('class="pessoa-linha"') == 2, "um nome em cada um dos dois rankings"
    assert "Anatoly Solonitsyn" in elenco
    assert "heroi" not in pessoas, "sem destaque para o #1"

    assert "Gênero da direção" in diretores and "Masculino" in diretores
    assert "1 (100%)" in diretores
    assert "Rússia" in diretores, "[now Russia] vale mais que USSR"
    assert "0 de 1 sem local" in diretores
    assert "Gênero" not in elenco and "Países de nascimento" not in elenco
    # TMDB vence: o título do filme leva ao TMDB, em outra aba.
    assert 'href="https://www.themoviedb.org/movie/77" target="_blank"' in pessoas


def test_mapa_tem_botao_de_expandir_e_painel_cinema_pelo_mundo(cliente, export_bytes):
    perfil = _processar_perfil(cliente, export_bytes)
    html = cliente.get(perfil).text
    assert 'data-expandir="mapa"' in html and 'id="dialogo-mapa"' in html
    dialogo = html[html.index('<dialog id='):html.index('</dialog>')]
    assert "Cinema pelo mundo" in dialogo and "De onde vêm os seus filmes" in dialogo
    # Um país de origem (SU, sem contorno no mapa): nome do TMDB, 5 entradas de 5.
    assert "Soviet Union" in dialogo and "5 (100%)" in dialogo
    assert "Fica em casa" in dialogo
    assert dialogo.count("<path") == 0, "os contornos não se repetem: o script clona"
    # Mesmo número do cartão "Países diferentes" da visão geral.
    assert '<div class="numero destaque">1</div>' in html
    assert '<span class="valor acento">1</span>' in dialogo


def test_botao_de_mostrar_mais_fica_depois_da_decima_linha():
    """O botão é a última coisa do cartão, aberto ou fechado: o que se esconde
    são as linhas 6 a 10, que vêm antes dele no HTML."""
    pessoas = [{"nome": f"P{i}", "filmes": 10 - i, "nota_media": 4.0, "avaliados": 3,
                "minutos": 120, "lista": []} for i in range(8)]
    macros = modulo_app.TEMPLATES.env.get_template("_partes.html").module
    html = str(macros.ranking_pessoas(pessoas, {}, "direcao-mais"))
    assert html.count('class="pessoa-linha"') == 8
    assert 'id="direcao-mais" hidden' in html and 'aria-controls="direcao-mais"' in html
    assert html.index('P4') < html.index('id="direcao-mais"') < html.index('P5')
    assert html.index('P7') < html.index('class="ver-mais"'), "botão depois da última linha"
    assert "Mostrar mais 3" in html
    assert 'class="ver-mais"' not in str(macros.ranking_pessoas(pessoas[:5], {}, "x"))


def _sem_rede(cliente):
    """Troca o transporte por um que falha se o TMDB for chamado."""
    def proibido(request):
        raise AssertionError(f"a rota não pode ir ao TMDB: {request.url}")

    cliente.app.state.recursos.tmdb._http = httpx.AsyncClient(
        transport=httpx.MockTransport(proibido))


def _itens(cliente, perfil, dimensao, valor):
    return cliente.get(f"{perfil}/itens/{dimensao}.json", params={"valor": valor})


def test_filmes_de_um_pais_vem_do_cache_ordenados_pela_nota(cliente, export_bytes):
    perfil = _processar_perfil(cliente, export_bytes)
    _sem_rede(cliente)
    r = _itens(cliente, perfil, "pais", "su")   # minúsculas valem
    assert r.status_code == 200
    j = r.json()
    assert (j["nome"], j["tipo"], j["total"]) == ("Soviet Union", "filmes", 3), (
        "sem contorno no mapa: nome em inglês do TMDB")
    # Stalker foi visto 3 vezes (nota média 5, amostra 3); empate em 4 vai por título.
    assert [(f["titulo"], f["nota"], f["avaliacoes"]) for f in j["itens"]] == [
        ("Stalker", 5.0, 3), ("Mirror", 4.0, 1), ("Solaris", 4.0, 1)]
    assert {"tmdb_id", "ano", "poster"} <= set(j["itens"][0])


def test_pais_sem_filmes_devolve_lista_vazia_com_nome_do_mapa(cliente, export_bytes):
    perfil = _processar_perfil(cliente, export_bytes)
    j = _itens(cliente, perfil, "pais", "BR").json()
    assert (j["nome"], j["total"], j["itens"]) == ("Brasil", 0, [])


def test_dimensoes_temporais_devolvem_sessoes_e_a_conta_bate_com_a_coluna(cliente, export_bytes):
    """Stalker foi visto em 1, 2 e 3 de março de 2024 (sex, sáb, dom): três itens."""
    perfil = _processar_perfil(cliente, export_bytes)
    _sem_rede(cliente)
    ano = _itens(cliente, perfil, "ano", "2024").json()
    assert (ano["tipo"], ano["total"], ano["nome"]) == ("sessoes", 5, "2024")
    # A altura da coluna anual é a mesma contagem.
    pagina = cliente.get(perfil).text
    assert 'data-dica="2024|5 filmes"' in pagina and 'data-valor="2024"' in pagina
    # Mais nova primeiro, cada sessão com a nota daquela entrada e a data.
    assert [(i["titulo"], i["data"]) for i in ano["itens"]][:2] == [
        ("Mirror", "2024-03-05"), ("Solaris", "2024-03-04")]
    assert [i["data"] for i in ano["itens"] if i["titulo"] == "Stalker"] == [
        "2024-03-03", "2024-03-02", "2024-03-01"], "o rewatch aparece em cada sessão"
    assert all(i["nota"] in (4.0, 5.0) and i["avaliacoes"] == 1 for i in ano["itens"])
    assert _itens(cliente, perfil, "mes", "2024-03").json()["total"] == 5
    assert _itens(cliente, perfil, "mes", "2024-04").json()["total"] == 0
    domingo = _itens(cliente, perfil, "dia", "7").json()
    assert (domingo["nome"], domingo["total"], domingo["itens"][0]["titulo"]) == ("Domingo", 1, "Stalker")


def test_dimensoes_nao_temporais_devolvem_filmes_distintos_com_a_media(cliente, export_bytes):
    perfil = _processar_perfil(cliente, export_bytes)
    _sem_rede(cliente)
    for dimensao, valor in [("decada", "1970"), ("genero", "Drama"), ("tema", "zone"),
                            ("idioma", "ru"), ("duracao", "epicos"), ("pais", "SU")]:
        j = _itens(cliente, perfil, dimensao, valor).json()
        assert j["tipo"] == "filmes" and j["total"] == 3, dimensao
        # O rewatch de Stalker é um filme só, com a média e a amostra de 3.
        assert (j["itens"][0]["titulo"], j["itens"][0]["nota"], j["itens"][0]["avaliacoes"]) == (
            "Stalker", 5.0, 3), dimensao
    assert _itens(cliente, perfil, "idioma", "ru").json()["nome"] == "Russo"
    assert _itens(cliente, perfil, "duracao", "curtos").json()["total"] == 0
    # Nota 4: Solaris e Mirror. Nota 5: Stalker, mesmo com 3 sessões.
    assert [i["titulo"] for i in _itens(cliente, perfil, "nota", "4").json()["itens"]] == [
        "Mirror", "Solaris"]
    assert _itens(cliente, perfil, "nota", "5").json()["total"] == 1


def test_ranking_inteiro_das_listas(cliente, export_bytes):
    perfil = _processar_perfil(cliente, export_bytes)
    _sem_rede(cliente)
    r = cliente.get(f"{perfil}/ranking/genero.json").json()
    assert r["linhas"][0] == {"valor": "Drama", "nome": "Drama", "filmes": 5, "pct": 100.0}
    idiomas = cliente.get(f"{perfil}/ranking/idioma.json").json()["linhas"]
    assert (idiomas[0]["valor"], idiomas[0]["nome"]) == ("ru", "Russo")
    temas = cliente.get(f"{perfil}/ranking/tema.json").json()["linhas"]
    assert temas == [{"valor": "zone", "nome": "zone", "filmes": 5, "pct": None}]
    duracao = cliente.get(f"{perfil}/ranking/duracao.json").json()["linhas"]
    assert [d["valor"] for d in duracao] == ["curtos", "padrao", "longos", "epicos"]
    assert duracao[3]["filmes"] == 3 and duracao[3]["detalhe"] == "> 150 min"
    for fora in ["nota", "ano", "pais", "qualquer"]:
        assert cliente.get(f"{perfil}/ranking/{fora}.json").status_code == 404


def test_rotas_de_dimensao_validam_antes_de_carregar_e_respeitam_o_segredo(cliente, export_bytes):
    perfil = _processar_perfil(cliente, export_bytes)
    _sem_rede(cliente)
    assert cliente.get("/p/naoexiste/itens/pais.json", params={"valor": "SU"}).status_code == 404
    assert cliente.get("/p/gabriel/itens/pais.json", params={"valor": "SU"}).status_code == 404, (
        "usuário não abre perfil")
    assert cliente.get("/p/naoexiste/ranking/genero.json").status_code == 404
    invalidos = [
        ("pais", "USA"), ("pais", "S"), ("pais", "1U"), ("pais", "S "), ("pais", ".."), ("pais", ""),
        ("nota", "0"), ("nota", "5.5"), ("nota", "4.2"), ("nota", "abc"),
        ("ano", "1899"), ("ano", "2101"), ("ano", "24"), ("ano", "2024-01"),
        ("mes", "2024-13"), ("mes", "2024-00"), ("mes", "2024"), ("mes", "1800-05"),
        ("dia", "0"), ("dia", "8"), ("dia", "seg"),
        ("decada", "1975"), ("decada", "1860"), ("decada", "2110"),
        ("genero", ""), ("genero", "x" * 61), ("tema", "x" * 81), ("tema", "  "),
        ("idioma", "PT"), ("idioma", "p"), ("idioma", "portugues"), ("idioma", "1a"),
        ("duracao", "gigante"), ("duracao", "0"),
        ("desconhecida", "1"), ("filmes", "1"),
    ]
    for dimensao, valor in invalidos:
        assert _itens(cliente, perfil, dimensao, valor).status_code == 404, (dimensao, valor)
    assert cliente.get(f"{perfil}/itens/pais.json").status_code == 404, "sem valor"
    assert _itens(cliente, perfil, "tema", "x" * 80).status_code == 200


def test_pagina_de_perfil_antigo_nao_rebusca_nem_quebra(cliente, export_bytes):
    """Entrada sem `pessoas`/locais (processada antes do campo) abre sem rede."""
    perfil = _processar_perfil(cliente, export_bytes)
    res = cliente.app.state.recursos
    for id_ in (77, 78, 79):
        antigo = res.cache.get_film(id_)
        antigo.pessoas = None
        res.cache.put_film(antigo)
    res.cache._conn.execute("DELETE FROM people")

    def proibido(request):
        raise AssertionError(f"a página não pode ir ao TMDB: {request.url}")

    res.tmdb._http = httpx.AsyncClient(transport=httpx.MockTransport(proibido))
    pagina = cliente.get(perfil)
    assert pagina.status_code == 200
    assert "Processe o perfil de novo para ver os países" in pagina.text
    assert "Processe o perfil de novo para ver o gênero" in pagina.text


def test_falha_do_job_vira_mensagem_e_nao_traceback(cliente):
    """Sem export e sem RSS não há o que processar; o usuário precisa saber."""
    r = cliente.post("/processar", data={"usuario": "fantasma"}, follow_redirects=False)
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    for _ in range(200):
        estado = cliente.get(f"/jobs/{job_id}/estado").json()
        if estado["terminou"]:
            break
    assert estado["estado"] == "falhou"
    assert estado["erro"] and "Traceback" not in estado["erro"]


def _processar_perfil(cliente, export_bytes, usuario="gabriel"):
    """Roda o job de perfil até o fim e devolve o caminho da página."""
    r = cliente.post("/processar", data={"usuario": usuario},
                     files={"arquivo": ("e.zip", export_bytes, "application/zip")},
                     follow_redirects=False)
    job_id = r.headers["location"].rsplit("/", 1)[-1]
    for _ in range(200):
        estado = cliente.get(f"/jobs/{job_id}/estado").json()
        if estado["terminou"]:
            break
    return estado["destino"]


def test_json_avisa_quando_falta_o_corpus(cliente, export_bytes):
    """Sem linha de base não dá para dizer o que é distintivo — melhor avisar."""
    perfil = _processar_perfil(cliente, export_bytes)
    j = cliente.get(f"{perfil}/recomendacoes.json").json()
    assert j["situacao"] == "sem_referencia"


def test_recomendacoes_viram_job_em_vez_de_travar_o_request(cliente, export_bytes):
    """Calcular no request seria meio minuto pendurado para cada perfil novo."""
    perfil = _processar_perfil(cliente, export_bytes)
    cliente.app.state.recursos.cache.put_background([77])

    j = cliente.get(f"{perfil}/recomendacoes.json").json()
    assert j["situacao"] == "calculando"
    assert j["job"]


def test_nao_agenda_duas_vezes_o_mesmo_calculo(cliente, export_bytes):
    """Abrir a aba de novo tem que cair no job que já está a caminho."""
    perfil = _processar_perfil(cliente, export_bytes)
    cliente.app.state.recursos.cache.put_background([77])

    primeiro = cliente.get(f"{perfil}/recomendacoes.json").json()
    segundo = cliente.get(f"{perfil}/recomendacoes.json").json()
    assert primeiro["job"] == segundo["job"]


def test_recomendacoes_prontas_sao_servidas_direto(cliente, export_bytes):
    perfil = _processar_perfil(cliente, export_bytes)
    cliente.app.state.recursos.perfis.salvar_recomendacoes(perfil.removeprefix("/p/"), {
        "perfil_gosto": {"Direção": [["dir:Tarkovsky", 3.1]]},
        "itens": [{"tmdb_id": 1398, "titulo": "Solaris", "ano": 1972,
                   "score": 0.9, "motivo": "direção de Tarkovsky"}],
    })
    j = cliente.get(f"{perfil}/recomendacoes.json").json()
    assert j["situacao"] == "pronto"
    assert j["dados"]["itens"][0]["titulo"] == "Solaris"


def test_job_de_recomendacao_roda_e_guarda_o_resultado(cliente, export_bytes):
    perfil = _processar_perfil(cliente, export_bytes)
    cliente.app.state.recursos.cache.put_background([77])

    job_id = cliente.get(f"{perfil}/recomendacoes.json").json()["job"]
    for _ in range(300):
        estado = cliente.get(f"/jobs/{job_id}/estado").json()
        if estado["terminou"]:
            break
    assert estado["estado"] == "concluido", estado
    assert estado["destino"] == f"{perfil}/recomendacoes"
    assert cliente.get(f"{perfil}/recomendacoes.json").json()["situacao"] == "pronto"


def test_rota_antiga_leva_para_a_aba(cliente, export_bytes):
    """A tela de progresso manda o usuário para cá; links antigos também."""
    perfil = _processar_perfil(cliente, export_bytes)
    r = cliente.get(f"{perfil}/recomendacoes", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"{perfil}#recommendations"


def test_cabecalhos_de_cartao_usam_icones_do_sprite_definido_uma_vez(cliente, export_bytes):
    import re
    perfil = _processar_perfil(cliente, export_bytes)
    html = cliente.get(perfil).text
    simbolos = re.findall(r'<symbol id="(i-[a-z]+)"', html)
    assert simbolos and len(simbolos) == len(set(simbolos)), "cada ícone definido uma vez"
    cabecalhos = re.findall(
        r'<div class="cabeca-secao">\s*<span class="icone-caixa"><svg[^>]*><use href="#(i-[a-z]+)"/>'
        r'</svg></span>\s*<div class="cabeca-texto">\s*<h2>([^<]+)</h2>', html)
    titulos = [t for _, t in cabecalhos]
    for esperado in ["Distribuição de notas", "Atividade anual", "Assistidos recentemente",
                     "Gêneros favoritos", "Mapa dos países", "Popular ou garimpo?",
                     "Direção mais assistida", "Gênero da direção"]:
        assert esperado in titulos, f"cartão sem cabeçalho: {esperado}"
    assert all(i in simbolos for i, _ in cabecalhos), "todo ícone usado existe no sprite"
    usados = set(re.findall(r'<use href="#(i-[a-z]+)"', html))
    assert usados <= set(simbolos) and set(simbolos) <= usados, "sem símbolo sobrando nem faltando"


def test_biblioteca_de_exportar_imagem_e_servida_com_licenca_e_hash(cliente):
    import hashlib
    from pathlib import Path
    r = cliente.get("/static/vendor/html-to-image.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert "htmlToImage" in r.text
    vendor = Path(modulo_app.__file__).parent / "static" / "vendor"
    assert "MIT License" in (vendor / "LICENSE-html-to-image").read_text()
    # O hash anotado junto do arquivo confere com o arquivo servido.
    assert hashlib.sha256(r.content).hexdigest() in (vendor / "LEIA-ME.txt").read_text()
    assert cliente.get("/static/vendor/").status_code == 404, "sem listagem de diretório"


def test_todo_cartao_tem_o_botao_de_compartilhar_e_a_biblioteca_nao_vem_na_pagina(
        cliente, export_bytes):
    perfil = _processar_perfil(cliente, export_bytes)
    html = cliente.get(perfil).text
    cartoes = html.count('<section class="painel secao')
    assert cartoes >= 10
    assert html.count('class="btn btn-icone compartilhar"') == cartoes
    assert html.count('id="menu-compartilhar"') == 1, "um menu só para a página"
    assert html.count("/static/vendor/html-to-image.js") == 1, "só dentro do script, ao exportar"
    assert "<script src" not in html, "a biblioteca só carrega ao exportar, injetada pelo script"


def test_exportar_imagem_nunca_usa_o_endereco_do_perfil(cliente, export_bytes):
    """Invariante 6: o link secreto não pode ir parar na imagem nem no arquivo."""
    import re
    perfil = _processar_perfil(cliente, export_bytes)
    html = cliente.get(perfil).text
    slug = perfil.removeprefix("/p/")
    codigo = html[html.index("// ---- exportar cartão: início ----"):
                  html.index("// ---- exportar cartão: fim ----")]
    assert not re.search(r"\bslug\b|location|href\s*=\s*['\"]?/p/", codigo), (
        "o código de exportar não toca no slug nem na URL da página")
    assert slug not in codigo
    assert "const usuario = \"gabriel\"" in codigo
    assert "`@${usuario} · letterboxdstats.party`" in codigo
    assert "'letterboxd-stats-' + titulo" in codigo, "arquivo nomeado pelo título do cartão"


def test_imagem_exportada_tem_largura_fixa_e_texto_que_quebra_linha(cliente, export_bytes):
    """Do celular, o PNG sai com o arranjo do desktop e nome longo não passa por
    cima da barra."""
    perfil = _processar_perfil(cliente, export_bytes)
    html = cliente.get(perfil).text
    codigo = html[html.index("// ---- exportar cartão: início ----"):
                  html.index("// ---- exportar cartão: fim ----")]
    # A largura vem das medidas do layout, não do cartão vivo (que no celular é estreito).
    assert "pagina.style.width = (larguraNoDesktop(cartao) + 48)" in codigo
    assert "getBoundingClientRect().width" not in codigo
    assert "main.envelope" in codigo and "columnGap" in codigo
    css = html[html.index("/* Texto cortado vira texto que quebra de linha"):]
    assert ".exporta .corta { white-space: normal" in css and "overflow-wrap: anywhere" in css
    assert ".exporta td.nome { max-width: none; }" in css, "o truque de 1px só vale sem quebra"
    assert ".exporta .pessoa-linha summary {" in css and ".exporta .filmes-pessoa {" in css
    assert ".exporta, .exporta * { animation: none !important" in html


def test_graficos_de_colunas_rolam_dentro_do_cartao_e_usam_a_dica_unica(cliente, export_bytes):
    perfil = _processar_perfil(cliente, export_bytes)
    html = cliente.get(perfil).text
    assert html.count('<div class="colunas-rolagem"') >= 5
    # Largura mínima por coluna, vinda do maior rótulo; a dica é a do popover da página.
    assert '<div class="colunas"\n       style="--c:' in html
    assert 'data-dica="' in html and 'class="dica"' not in html
    assert ".colunas-rolagem { overflow-x: auto; overscroll-behavior-x: contain;" in html
    assert "flex: 1 0 calc(var(--c) * var(--ch) + 2px)" in html
    assert ".exporta .colunas-rolagem { overflow: visible; }" in html, "a imagem não rola"


def test_ajustes_de_celular(cliente, export_bytes):
    perfil = _processar_perfil(cliente, export_bytes)
    html = cliente.get(perfil).text
    # Grade de filmes: trilho `minmax(0, 1fr)`, senão o título alarga além do cartão.
    assert "@media (max-width: 480px) { .filmes-pessoa { grid-template-columns: minmax(0, 1fr); } }" in html
    assert ".filmes-pessoa .filme { margin: 0; min-width: 0;" in html
    # A dica do mapa em tela cheia vale com a lista ao lado (desktop) ou embaixo (celular).
    assert "linha da lista de países, para ver os filmes dele" in html
    assert "lista ao lado" not in html
    # A aba ativa vem para o meio da fileira rolável e a borda esmaece enquanto sobrar aba.
    assert "barra.scrollLeft = ativa.offsetLeft" in html and "'mais-direita'" in html


def test_botao_de_expandir_so_nos_cartoes_com_grafico_e_o_dialogo_nao_compartilha(
        cliente, export_bytes):
    import re
    from datetime import date
    perfil = _processar_perfil(cliente, export_bytes)
    html = cliente.get(perfil).text
    cartoes = {}
    for secao in re.findall(r'<section class="painel secao.*?</section>', html, re.S):
        titulo = re.search(r"<h2>([^<]+)</h2>", secao)[1]
        cartoes[titulo] = re.findall(r'data-expandir="([a-z]+)"', secao)
    esperado = {"Distribuição de notas": "nota", "Atividade anual": "ano",
                f"Atividade mensal · {date.today().year}": "mes", "Atividade por dia da semana": "dia",
                "Filmes por década": "decada", "Gêneros favoritos": "genero",
                "Idiomas originais": "idioma", "Temas recorrentes": "tema",
                "Duração dos filmes": "duracao", "Mapa dos países": "mapa"}
    assert {t: v for t, v in cartoes.items() if v} == {t: [d] for t, d in esperado.items()}
    for sem in ["Assistidos recentemente", "Mais bem avaliados", "Popular ou garimpo?",
                "Onde você discorda do público"]:
        assert cartoes[sem] == [], sem
    assert 'aria-haspopup="dialog"' in html and 'aria-label="Expandir Mapa dos países"' in html
    # Compartilhar só no cartão: nem o diálogo nem o código de exportar o conhecem.
    dialogo = html[html.index('<dialog id="dialogo-grafico"'):]
    dialogo = dialogo[:dialogo.index("</dialog>")]
    assert "compartilhar" not in dialogo and "btn-icone" not in dialogo
    exporta = html[html.index("// ---- exportar cartão: início ----"):
                   html.index("// ---- exportar cartão: fim ----")]
    assert "dialogo" not in exporta and "dg-" not in exporta


def test_cartao_de_revisitas_no_deepdive(cliente, export_bytes):
    """Stalker foi visto 3 vezes: é o único revisto, e o destaque."""
    perfil = _processar_perfil(cliente, export_bytes)
    html = cliente.get(perfil).text
    deepdive = html[html.index('id="aba-deepdive"'):html.index('id="aba-people"')]
    assert deepdive.index(">Duração dos filmes</h2>") < deepdive.index(">Revisitas</h2>") < deepdive.index(
        ">Temas recorrentes</h2>")
    cartao = deepdive[deepdive.index(">Revisitas</h2>"):deepdive.index(">Temas recorrentes</h2>")]
    assert "Os filmes aos quais você volta" in deepdive
    assert "Filmes revistos" in cartao and "Tempo revendo" in cartao
    assert "3 sessões" in cartao and "×3" in cartao and "01/03/2024" in cartao
    assert "3/3 avaliadas" in cartao
    assert "Diretores revistos" in cartao and "sessões a mais" not in cartao
    assert "Diretor mais revisto" in cartao and "Andrei Tarkovsky" in cartao
    assert "1 filme revisto" in cartao
    assert 'data-expandir' not in cartao and "compartilhar" in cartao
    assert 'class="ver-mais"' not in cartao, "um filme só: não há o que mostrar a mais"


def test_revisitas_mostra_seis_e_esconde_o_resto_atras_do_botao():
    lista = [{"tmdb_id": i, "titulo": f"F{i}", "ano": 2000, "poster": None, "sessoes": 2}
             for i in range(8)]
    r = {"filmes": 8, "revisitas": 8, "minutos": 600, "generos": [{"nome": "Drama", "n": 8, "pct": 100.0}],
         "destaque": {"tmdb_id": 0, "titulo": "F0", "ano": 2000, "poster": None, "sessoes": 2,
                      "nota_media": None, "avaliadas": 0, "linha_do_tempo": [{"data": None, "nota": None}] * 2},
         "lista": lista}
    macros = modulo_app.TEMPLATES.env.get_template("_partes.html").module
    html = str(macros.revisitas(r, {}))
    visiveis, escondidos = html.split('id="rev-mais" hidden>')
    assert visiveis.count('class="rev-item"') == 6 and escondidos.count('class="rev-item"') == 2
    assert 'aria-controls="rev-mais"' in html and "Mostrar todos (8)" in html
    assert "sem data" in html and "sem nota" in html
    assert 'class="ver-mais"' not in str(macros.revisitas({**r, "lista": lista[:6]}))
    assert "Nenhum filme revisto ainda." in str(macros.revisitas({"filmes": 0}))


def test_deepdive_na_ordem_com_os_cartoes_novos(cliente, export_bytes):
    perfil = _processar_perfil(cliente, export_bytes)
    html = cliente.get(perfil).text
    deepdive = html[html.index('id="aba-deepdive"'):html.index('id="aba-people"')]
    ordem = ["Filmes por década", "Gêneros favoritos", "Idiomas originais", "Mapa dos países",
             "Popular ou garimpo?", "Duração dos filmes", "Temas recorrentes",
             "Onde você discorda do público"]
    posicoes = [deepdive.index(f">{t}</h2>") for t in ordem]
    assert posicoes == sorted(posicoes), "ordem do Deepdive"
    assert "Gêneros explorados" in deepdive and "Idiomas explorados" in deepdive
    assert "Menos assistido" in deepdive
    assert "Russo" in deepdive and ">ru<" not in deepdive, "idioma pelo nome, não pelo código"
    # Três filmes com 3000 votos: log10(3000/100) / log10(30000/100) = 60%.
    assert "60%" in deepdive and "Equilibrado" in deepdive
    assert "Seus achados" in deepdive and "Pelo número de votos no TMDB" in deepdive
    assert "Duração média" in deepdive and "Mais curto" in deepdive and "2h 42min" in deepdive


def test_pagina_do_perfil_traz_as_quatro_abas(cliente, export_bytes):
    perfil = _processar_perfil(cliente, export_bytes)
    html = cliente.get(perfil).text
    for aba in ["overview", "deepdive", "people", "recommendations"]:
        assert f'id="aba-{aba}"' in html, f"painel {aba} ausente"
    # Os gráficos de atividade são temporais: o aviso de cobertura mora onde
    # eles começam, na Overview, e uma vez só.
    overview = html[html.index('id="aba-overview"'):html.index('id="aba-deepdive"')]
    assert overview.count("entradas com data registrada") == 1
    assert overview.index("entradas com data registrada") < overview.index("Atividade anual")
    ordem = [overview.index(t) for t in ("Atividade anual", "Atividade mensal", "Atividade por dia da semana")]
    assert ordem == sorted(ordem)
    assert overview.index("Distribuição de notas") < ordem[0] < overview.index("Assistidos recentemente")
    # O Deepdive não tem mais seção de tempo, então nem aviso nem os gráficos.
    deepdive = html[html.index('id="aba-deepdive"'):html.index('id="aba-people"')]
    assert "entradas com data" not in deepdive
    assert "Perfil de gêneros" not in html and "Dia da semana" not in deepdive


def test_rotas_inexistentes(cliente):
    assert cliente.get("/jobs/naoexiste").status_code == 404
    assert cliente.get("/p/naoexiste").status_code == 404


def test_perfil_nao_e_achado_pelo_nome_de_usuario(cliente, export_bytes):
    """O nome de usuário é público: não pode abrir o perfil de ninguém."""
    perfil = _processar_perfil(cliente, export_bytes, usuario="gabriel")
    assert cliente.get(perfil).status_code == 200
    assert cliente.get("/p/gabriel").status_code == 404
    assert cliente.get("/p/gabriel/recomendacoes.json").status_code == 404
    assert "gabriel" not in cliente.get("/").text, "a página inicial não lista perfis"
