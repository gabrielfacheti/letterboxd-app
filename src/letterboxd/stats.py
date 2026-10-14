"""Estatísticas sobre um perfil, em Polars.

**Regra central deste módulo:** cerca de metade das entradas de um perfil não
tem data assistida — são filmes marcados como vistos sem registro no diário.
Por decisão de projeto, essas entradas:

- **entram** nas estatísticas não-temporais (gêneros, países, diretores, notas);
- **saem** de qualquer estatística que dependa do tempo.

O filtro está isolado em `dated()`, e toda função temporal começa por ele. O
`overview()` informa a cobertura para que a interface possa avisar o usuário —
um gráfico temporal que silenciosamente ignora metade dos dados mente por
omissão.

Contagens são **por sessão assistida**, não por filme único: rever um filme
conta duas vezes em "gêneros mais assistidos", porque é tempo gasto de novo.
`overview()` reporta os dois totais. A exceção são os rankings de pessoas
(diretores e elenco): ali cada filme conta uma vez, porque "8 filmes" de um
diretor que você só viu oito vezes seguidas seria uma mentira.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping
from datetime import date

import polars as pl

from letterboxd.idiomas import nome_do_idioma
from letterboxd.models import Profile
from letterboxd.nascimento import pais_de_nascimento
from letterboxd.tmdb.models import FilmDetails

# Quantos filmes um diretor (ou ator, país...) precisa ter para que a média de
# notas dele signifique alguma coisa. Sem isso, quem viu um único filme e deu 5
# lidera todos os rankings de "melhor avaliado".
MIN_AMOSTRA = 3

# Quantos votos um filme precisa ter no TMDB para que a média dele sirva de
# comparação. Filmes obscuros vêm com vote_average=0.0 e zero votos, o que
# fabricaria discordâncias enormes e falsas.
MIN_VOTOS_TMDB = 50


def build_frame(profile: Profile, metadata: Mapping[int, FilmDetails]) -> pl.DataFrame:
    """Junta as entradas do perfil com os metadados do TMDB numa tabela.

    Uma linha por entrada de diário. Entradas cujo filme não resolveu no TMDB
    são mantidas, com os campos de metadados nulos: some-las falsearia os
    totais, e é melhor a interface mostrar o buraco.
    """
    linhas = []
    for entry in profile.entries:
        details = metadata.get(entry.film.tmdb_id) if entry.film.tmdb_id else None
        linhas.append({
            "titulo": entry.film.title,
            "ano": entry.film.year,
            "tmdb_id": entry.film.tmdb_id,
            "nota": entry.rating,
            "data_assistida": entry.watched_date,
            "data_registro": entry.logged_date,
            "rewatch": entry.rewatch,
            "curtiu": entry.liked,
            "tem_review": bool(entry.review),
            "tags": entry.tags,
            "duracao": details.runtime if details else None,
            "idioma": details.original_language if details else None,
            "nota_tmdb": details.vote_average if details else None,
            "votos_tmdb": details.vote_count if details else 0,
            "popularidade": details.popularity if details else None,
            "generos": details.genres if details else [],
            "paises": details.country_names if details else [],
            "origem": (details.origin_countries or []) if details else [],
            "direcao": details.directors if details else [],
            "elenco": details.cast if details else [],
            "keywords": details.keywords if details else [],
            "poster": details.poster_path if details else None,
        })

    frame = pl.DataFrame(linhas, schema_overrides={
        "ano": pl.Int32, "tmdb_id": pl.Int64, "nota": pl.Float64,
        "duracao": pl.Int32, "nota_tmdb": pl.Float64, "popularidade": pl.Float64,
        "votos_tmdb": pl.Int64,
        # Datas explícitas: num perfil sem nenhuma data (só `watched.csv`) a
        # coluna seria toda nula, o Polars a inferiria como Null e todo `.dt`
        # estouraria.
        "data_assistida": pl.Date, "data_registro": pl.Date,
    })

    # A década sai do ano do filme, que existe para toda entrada — diferente da
    # data assistida. Por isso não é uma estatística temporal.
    return frame.with_columns(
        (pl.col("ano") // 10 * 10).alias("decada"),
        pl.col("data_assistida").is_not_null().alias("tem_data"),
    )


def dated(frame: pl.DataFrame) -> pl.DataFrame:
    """Só as entradas com data assistida. Porta de entrada de toda análise temporal."""
    return frame.filter(pl.col("tem_data"))


def overview(frame: pl.DataFrame) -> dict[str, object]:
    """Números gerais, incluindo a cobertura temporal.

    `cobertura_temporal` existe para a interface poder dizer em cima de quantos
    dados cada gráfico de tempo foi construído.
    """
    com_data = dated(frame)
    minutos = frame["duracao"].sum() or 0
    return {
        "entradas": frame.height,
        "filmes_unicos": frame.select(pl.col("tmdb_id").n_unique()).item(),
        "rewatches": int(frame["rewatch"].sum()),
        "avaliados": int(frame["nota"].is_not_null().sum()),
        "nota_media": round(frame["nota"].mean(), 2) if frame["nota"].count() else None,
        "curtidos": int(frame["curtiu"].fill_null(False).sum()),
        "com_review": int(frame["tem_review"].sum()),
        "horas": round(minutos / 60, 1),
        "dias": round(minutos / 1440, 1),
        "sem_metadados": int(frame["tmdb_id"].is_null().sum()),
        "com_data": com_data.height,
        "cobertura_temporal": round(100 * com_data.height / frame.height, 1) if frame.height else 0.0,
        "primeiro_registro": com_data["data_assistida"].min() if com_data.height else None,
        "ultimo_registro": com_data["data_assistida"].max() if com_data.height else None,
        # Pela origem, como o mapa: contar coprodutores inflaria o número.
        "paises_distintos": _distintos(frame, "origem"),
        "direcao_distintos": _distintos(frame, "direcao"),
        "no_ano": _no_ano_corrente(com_data),
        "ano_corrente": date.today().year,
        **_ritmo(com_data),
        "pct_filmes_diferentes": _pct_filmes_diferentes(frame),
        **_duracao_media(frame),
    }


def _distintos(frame: pl.DataFrame, coluna: str) -> int:
    """Quantos valores diferentes aparecem numa coluna de listas."""
    if not frame.height:
        return 0
    return (
        frame.select(coluna)
        .explode(coluna, empty_as_null=True)
        .drop_nulls(coluna)
        .n_unique()
    )


def _ritmo(com_data: pl.DataFrame) -> dict[str, object]:
    """Entradas por semana e o dia da semana mais ativo. Só entradas datadas.

    A janela vai da primeira à última data assistida e nunca é menor que uma
    semana: três filmes num único dia seriam "21 por semana" se a janela
    tivesse um dia só.
    """
    if not com_data.height:
        return {"por_semana": None, "dia_mais_ativo": None, "dia_mais_ativo_entradas": 0}
    dias = (com_data["data_assistida"].max() - com_data["data_assistida"].min()).days + 1
    topo = por_dia_da_semana(com_data).sort("filmes", descending=True, maintain_order=True).row(0, named=True)
    return {
        "por_semana": round(com_data.height / (max(dias, 7) / 7), 1),
        "dia_mais_ativo": topo["nome"],
        "dia_mais_ativo_entradas": topo["filmes"],
    }


def _pct_filmes_diferentes(frame: pl.DataFrame) -> int | None:
    """Que fração das entradas são filmes distintos: baixa = quem revê muito."""
    if not frame.height:
        return None
    unicos = frame.select(pl.col("tmdb_id").n_unique()).item()
    return round(100 * unicos / frame.height)


def _duracoes_conhecidas(frame: pl.DataFrame) -> pl.DataFrame:
    """Uma linha por filme distinto com duração conhecida.

    Rever um filme não pode pesar duas vezes, e o TMDB devolve 0 para duração
    desconhecida, que não é um filme de zero minuto.
    """
    return (
        frame.drop_nulls(["tmdb_id", "duracao"])
        .filter(pl.col("duracao") > 0)
        .unique("tmdb_id", keep="first", maintain_order=True)
    )


def _duracao_media(frame: pl.DataFrame) -> dict[str, int | None]:
    """Duração média por filme distinto."""
    conhecidos = _duracoes_conhecidas(frame)
    if not conhecidos.height:
        return {"duracao_media": None, "duracao_amostra": 0}
    return {
        "duracao_media": round(conhecidos["duracao"].mean()),
        "duracao_amostra": conhecidos.height,
    }


def _no_ano_corrente(com_data: pl.DataFrame) -> int:
    """Filmes assistidos no ano em curso. Só entradas datadas, naturalmente."""
    if not com_data.height:
        return 0
    return com_data.filter(
        pl.col("data_assistida").dt.year() == date.today().year
    ).height


def atividade_mensal(frame: pl.DataFrame, ano: int | None = None) -> list[dict]:
    """Filmes por mês de um ano, com os doze meses sempre presentes.

    Meses vazios precisam aparecer no gráfico — omiti-los faria um ano de pouca
    atividade parecer cheio.
    """
    ano = ano or date.today().year
    contagem = dict(
        dated(frame)
        .filter(pl.col("data_assistida").dt.year() == ano)
        .group_by(pl.col("data_assistida").dt.month().alias("mes"))
        .agg(pl.len().alias("filmes"))
        .iter_rows()
    )
    nomes = ["jan", "fev", "mar", "abr", "mai", "jun",
             "jul", "ago", "set", "out", "nov", "dez"]
    return [
        {"mes": numero, "nome": nomes[numero - 1], "filmes": contagem.get(numero, 0),
         "chave": f"{ano}-{numero:02d}"}
        for numero in range(1, 13)
    ]


def atividade_anual(frame: pl.DataFrame) -> list[dict]:
    """Entradas com data por ano, do primeiro ao último, anos vazios incluídos.

    Um ano sem nada no meio do intervalo precisa aparecer: omiti-lo juntaria
    dois anos distantes num gráfico que parece contínuo.
    """
    contagem = dict(por_ano(frame).select("ano_assistido", "filmes").iter_rows())
    if not contagem:
        return []
    return [{"nome": str(ano), "filmes": contagem.get(ano, 0), "chave": str(ano)}
            for ano in range(min(contagem), max(contagem) + 1)]


def atividade_semanal(frame: pl.DataFrame) -> list[dict]:
    """Entradas com data por dia da semana, segunda a domingo, os sete sempre
    presentes (dia sem entradas é informação, não ausência de dado)."""
    contagem = dict(por_dia_da_semana(frame).select("dia", "filmes").iter_rows())
    curtos = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
    return [{"nome": nome, "filmes": contagem.get(dia, 0), "chave": str(dia)}
            for dia, nome in enumerate(curtos, start=1)]


def _ranking(
    frame: pl.DataFrame,
    coluna: str,
    min_amostra: int = 1,
    min_avaliados: int = 0,
    por_filme: bool = False,
) -> pl.DataFrame:
    """Conta ocorrências de uma coluna de listas e cruza com a nota média.

    Serve para gêneros, países, diretores, elenco e keywords — todos são listas
    por filme, e todos interessam pelas mesmas duas perguntas: quanto você
    assistiu, e quanto você gostou.

    `min_amostra` filtra por quantidade de filmes e `min_avaliados` por
    quantidade de notas. São coisas diferentes: alguém pode ter visto 6 filmes
    de um diretor e avaliado só um, e nesse caso a "nota média" é uma nota só
    disfarçada de média. Rankings por contagem usam o primeiro; rankings por
    nota devem exigir o segundo.

    Com `por_filme`, cada filme conta uma vez, com a média das notas de suas
    entradas. Rankings de pessoas precisam disso: rever um filme oito vezes
    não faz do diretor dele oito filmes.
    """
    if por_filme:
        frame = _um_por_filme(frame, coluna)
    return (
        frame.select(coluna, "nota")
        # Filme sem gênero/país vem com lista vazia. `empty_as_null=True` a
        # transforma em nulo, que o `drop_nulls` seguinte descarta. É explícito
        # porque o padrão disso muda no Polars 2.0.
        .explode(coluna, empty_as_null=True)
        .drop_nulls(coluna)
        .group_by(coluna)
        .agg(
            pl.len().alias("filmes"),
            pl.col("nota").mean().round(2).alias("nota_media"),
            pl.col("nota").count().alias("avaliados"),
        )
        .filter(pl.col("filmes") >= min_amostra)
        .filter(pl.col("avaliados") >= min_avaliados)
        .sort("filmes", descending=True)
        .rename({coluna: "valor"})
        # Fração das entradas que têm o traço. Gêneros somam mais de 100% de
        # propósito: um filme pode ser drama e terror ao mesmo tempo.
        .with_columns(
            (100 * pl.col("filmes") / max(frame.height, 1)).round(1).alias("pct")
        )
    )


def _um_por_filme(frame: pl.DataFrame, coluna: str) -> pl.DataFrame:
    """Colapsa as entradas de um mesmo filme numa linha, com a nota média.

    Entrada sem `tmdb_id` não tem metadados (a lista é vazia), então nunca
    apareceria nos rankings de pessoas: descartá-la aqui só evita que todas
    colapsem num grupo nulo só. A média ignora notas ausentes; o filme sem
    nenhuma nota fica com nota nula e não conta como avaliado.

    Título, ano e pôster vêm junto para os cartões de filmes de uma pessoa;
    `avaliacoes` acompanha a média pelo mesmo motivo de `mais_bem_avaliados`.
    """
    return (
        frame.drop_nulls("tmdb_id")
        .group_by("tmdb_id", maintain_order=True)
        .agg(
            pl.col(coluna).first(),
            pl.col("nota").mean(),
            pl.col("titulo").first(),
            pl.col("ano").first(),
            pl.col("poster").first(),
            pl.col("nota").count().alias("avaliacoes"),
        )
    )


def por_genero(frame: pl.DataFrame) -> pl.DataFrame:
    return _ranking(frame, "generos")


def filmes_por_codigo_de_pais(frame: pl.DataFrame) -> dict[str, int]:
    """Entradas por país de origem (código ISO), para o mapa.

    Origem, não produção: com `production_countries` cada coprodutor conta, e
    o Reino Unido aparecia em 73 filmes de um histórico em que só 5 eram
    britânicos. O código, e não o nome, porque os contornos são indexados por
    ISO.
    """
    return dict(_ranking(frame, "origem").select("valor", "filmes").iter_rows())


def por_direcao(frame: pl.DataFrame, min_amostra: int = MIN_AMOSTRA) -> pl.DataFrame:
    return _ranking(frame, "direcao", min_amostra, por_filme=True)


def por_keyword(frame: pl.DataFrame, min_amostra: int = MIN_AMOSTRA) -> pl.DataFrame:
    return _ranking(frame, "keywords", min_amostra)


def melhores(
    frame: pl.DataFrame,
    coluna: str,
    min_avaliados: int = MIN_AMOSTRA,
) -> pl.DataFrame:
    """Ranking por nota média, e não por quantidade.

    Exige um mínimo de *avaliações* (não de filmes), porque é a nota que está
    sendo ordenada. Sem isso o topo vira quem você viu uma vez e adorou. Conta
    filmes distintos, como os rankings de pessoas em geral.
    """
    return (
        _ranking(frame, coluna, min_avaliados=min_avaliados, por_filme=True)
        .sort("nota_media", descending=True, nulls_last=True)
    )


# Convenção de gênero do TMDB, na ordem em que a pizza as mostra. O 0 fica como
# fatia própria: sumir com ele faria o resto parecer o total.
_GENEROS = [(1, "Feminino"), (2, "Masculino"), (3, "Não-binário"), (0, "Desconhecido")]


def diretores_de_filmes(metadata: Mapping[int, FilmDetails]) -> dict[int, tuple[str, int]]:
    """Diretores distintos, por id do TMDB: `{id: (nome, genero)}`.

    Pelo id e não pelo nome, porque homônimos são pessoas diferentes. Filme de
    cache anterior ao campo `pessoas` simplesmente não contribui. É o conjunto
    de quem tem o local de nascimento buscado, no job e na página: só a direção
    tem gênero e país na interface, e buscar o elenco seriam milhares de
    requisições por dado que ninguém veria.
    """
    diretores: dict[int, tuple[str, int]] = {}
    for details in metadata.values():
        for nome in details.directors:
            dados = (details.pessoas or {}).get(nome)
            if dados:
                diretores[dados["id"]] = (nome, dados["genero"])
    return diretores


def diversidade_de_genero(pessoas: Mapping[int, tuple[str, int]]) -> dict | None:
    """Contagem por gênero, com os arcos (`de`, `pct`) da pizza já calculados.

    Cada pessoa conta uma vez, não importa em quantos filmes apareça.
    """
    if not pessoas:
        return None
    contagem = Counter(genero if genero in (1, 2, 3) else 0 for _, genero in pessoas.values())
    total = len(pessoas)
    fatias, de = [], 0.0
    for indice, (codigo, nome) in enumerate(_GENEROS):
        pct = 100 * contagem[codigo] / total
        # `indice` fixa a cor da fatia mesmo quando uma categoria vem vazia.
        fatias.append({"indice": indice, "nome": nome, "n": contagem[codigo],
                       "pct": round(pct, 2), "de": round(de, 2)})
        de += pct
    return {"total": total, "fatias": fatias}


def paises_de_nascimento(
    pessoas: Mapping[int, tuple[str, int]],
    locais: Mapping[int, str | None],
    n: int = 8,
) -> dict | None:
    """Países de nascimento mais comuns entre as pessoas, com a amostra.

    `locais` traz o que o cache sabe: id ausente = nunca buscado, valor nulo =
    buscado e sem local. A porcentagem é sobre quem tem país conhecido; quem não
    tem vai em `sem_local`, para a interface dizer quão furado está o dado.
    None quando nenhuma delas foi buscada (perfil anterior a este dado).
    """
    if pessoas and not any(i in locais for i in pessoas):
        return None
    contagem = Counter(
        pais for i in pessoas if (pais := pais_de_nascimento(locais.get(i)))
    )
    conhecidos = sum(contagem.values())
    ordenados = sorted(contagem.items(), key=lambda par: (-par[1], par[0]))[:n]
    return {
        "linhas": [{"valor": pais, "pessoas": q, "pct": round(100 * q / conhecidos, 1)}
                   for pais, q in ordenados],
        "conhecidos": conhecidos,
        "sem_local": len(pessoas) - conhecidos,
        "total": len(pessoas),
    }


def _filmes_das_pessoas(
    frame: pl.DataFrame, coluna: str, nomes: list[str]
) -> dict[str, list[dict]]:
    """Filmes distintos de cada pessoa, do mais antigo ao mais novo.

    Rewatch vira um filme só, com a nota média das sessões avaliadas.
    """
    filmes = (
        _um_por_filme(frame, coluna)
        .explode(coluna, empty_as_null=True)
        .drop_nulls(coluna)
        .filter(pl.col(coluna).is_in(nomes))
        .with_columns(pl.col("nota").round(2))
        .sort(["ano", "titulo"], nulls_last=True)
    )
    por_pessoa: dict[str, list[dict]] = {nome: [] for nome in nomes}
    for linha in filmes.to_dicts():
        por_pessoa[linha[coluna]].append(_cartao_de_filme(linha))
    return por_pessoa


def ranking_de_pessoas(
    frame: pl.DataFrame, coluna: str, por: str = "filmes", n: int = 10
) -> list[dict]:
    """As `n` primeiras pessoas por filmes distintos (`por="filmes"`) ou por nota
    média (`por="nota"`), cada uma com seus filmes.

    O ranking por nota exige `MIN_AMOSTRA` avaliações, como `melhores`. Empate
    pelo nome, para a ordem não depender da ordem do arquivo. As horas contam
    por *entrada*, ao contrário da contagem de filmes: rever um filme é tempo
    gasto de novo, e é isso que a coluna "assistido" promete.
    """
    if por == "nota":
        ranking = melhores(frame, coluna).sort(
            ["nota_media", "avaliados", "valor"], descending=[True, True, False])
    else:
        ranking = (
            _ranking(frame, coluna, MIN_AMOSTRA, por_filme=True)
            .sort(["filmes", "valor"], descending=[True, False])
        )
    ranking = ranking.head(n)
    minutos = dict(
        frame.select(coluna, "duracao")
        .explode(coluna, empty_as_null=True)
        .drop_nulls(coluna)
        .group_by(coluna)
        .agg(pl.col("duracao").sum())
        .iter_rows()
    )
    filmes = _filmes_das_pessoas(frame, coluna, ranking["valor"].to_list())
    return [
        {
            "nome": linha["valor"],
            "filmes": linha["filmes"],
            "nota_media": linha["nota_media"],
            "avaliados": linha["avaliados"],
            "minutos": minutos.get(linha["valor"], 0),
            "lista": filmes[linha["valor"]],
        }
        for linha in ranking.to_dicts()
    ]


def aba_de_pessoas(
    frame: pl.DataFrame,
    metadata: Mapping[int, FilmDetails],
    locais: Mapping[int, str | None],
    papel: str,
) -> dict:
    """Tudo que uma sub-aba de pessoas (direção ou elenco) mostra.

    Gênero e países existem só na direção, e vêm None quando não há dado de
    pessoa (perfil processado antes de ele existir), para a interface pedir que
    o perfil seja processado de novo em vez de mostrar um gráfico vazio.
    """
    aba = {
        "papel": papel,
        "mais": ranking_de_pessoas(frame, papel),
        "melhores": ranking_de_pessoas(frame, papel, por="nota"),
    }
    if papel == "direcao":
        diretores = diretores_de_filmes(metadata)
        aba["genero"] = diversidade_de_genero(diretores)
        aba["paises"] = paises_de_nascimento(diretores, locais) if diretores else None
    return aba


def assistidos_recentemente(frame: pl.DataFrame, n: int = 5) -> list[dict]:
    """Últimas entradas do diário. Temporal: entrada sem data não tem "recente".

    Um filme revisto duas vezes seguidas aparece duas vezes, porque são duas
    entradas — e cada uma mostra a nota que recebeu naquele dia.
    """
    return [
        _cartao_de_filme(linha)
        for linha in dated(frame)
        .sort(["data_assistida", "titulo"], descending=[True, False])
        .head(n)
        .to_dicts()
    ]


def mais_bem_avaliados(frame: pl.DataFrame, n: int = 5) -> list[dict]:
    """Filmes distintos com as maiores notas; rewatches entram pela nota média.

    Empates em 5,0 são a regra, não a exceção: desempata-se pelo mais recente
    (sem data por último) e depois pelo título, para a ordem não depender da
    ordem do arquivo. `avaliacoes` acompanha a nota porque uma média de várias
    entradas não pode aparecer sem a amostra.
    """
    # Filme sem tmdb_id não tem identidade além de título e ano; agrupá-los
    # pelo id nulo fundiria filmes diferentes num só.
    chave = pl.coalesce(
        pl.col("tmdb_id").cast(pl.String),
        pl.col("titulo") + pl.lit("|") + pl.col("ano").cast(pl.String).fill_null(""),
    )
    return [
        _cartao_de_filme(linha)
        for linha in frame.drop_nulls("nota")
        .group_by(chave.alias("filme"))
        .agg(
            pl.col("titulo").first(),
            pl.col("ano").first(),
            pl.col("generos").first(),
            pl.col("poster").first(),
            pl.col("tmdb_id").first(),
            pl.col("nota").mean().round(2),
            pl.col("nota").count().alias("avaliacoes"),
            pl.col("data_assistida").max(),
        )
        .sort(["nota", "data_assistida", "titulo"],
              descending=[True, True, False], nulls_last=True)
        .head(n)
        .to_dicts()
    ]


def _cartao_de_filme(linha: dict) -> dict:
    """Só o que o cartão de filme da interface desenha; no máximo dois gêneros.

    Data e gêneros são opcionais: os filmes de uma pessoa vêm de uma linha por
    filme, sem eles.
    """
    return {
        "titulo": linha["titulo"],
        "ano": linha["ano"],
        "tmdb_id": linha["tmdb_id"],
        "data": linha.get("data_assistida"),
        "nota": linha["nota"],
        "avaliacoes": linha.get("avaliacoes", 1 if linha["nota"] is not None else 0),
        "generos": (linha.get("generos") or [])[:2],
        "poster": linha["poster"],
    }


# Dimensões dos gráficos expandidos. As temporais (ano, mês, dia da semana)
# devolvem *sessões* — uma por entrada com data, como a altura da coluna —; as
# demais, filmes distintos com a nota média das sessões.
DIMENSOES_TEMPORAIS = {"ano", "mes", "dia"}
DIMENSOES_DE_LISTA = ("genero", "idioma", "tema", "duracao")
# Chave de cada faixa de duração na URL, na ordem de `FAIXAS_DE_DURACAO`.
_CHAVES_DE_DURACAO = {"curtos": 0, "padrao": 1, "longos": 2, "epicos": 3}
_MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
_DIAS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]


def analisar_dimensao(dimensao: str, valor: str) -> tuple[pl.Expr, str | None] | None:
    """Filtro (e nome para exibir) de um valor de uma dimensão; None se inválido.

    A URL é entrada de usuário: o valor é validado e convertido aqui, antes de
    qualquer coisa ser carregada, e nunca entra numa consulta como texto cru.
    """
    texto = valor.strip()
    if dimensao == "nota":
        try:
            nota = float(texto)
        except ValueError:
            return None
        return (pl.col("nota") == nota, f"★ {nota:g}") if nota * 2 in range(1, 11) else None
    if dimensao == "ano" and re.fullmatch(r"\d{4}", texto) and 1900 <= int(texto) <= 2100:
        return pl.col("data_assistida").dt.year() == int(texto), texto
    if dimensao == "mes" and (m := re.fullmatch(r"(\d{4})-(\d{2})", texto)):
        ano, mes = int(m[1]), int(m[2])
        if 1900 <= ano <= 2100 and 1 <= mes <= 12:
            data = pl.col("data_assistida")
            return (data.dt.year() == ano) & (data.dt.month() == mes), f"{_MESES[mes - 1]}/{ano}"
    if dimensao == "dia" and re.fullmatch(r"[1-7]", texto):
        return pl.col("data_assistida").dt.weekday() == int(texto), _DIAS[int(texto) - 1]
    if dimensao == "decada" and re.fullmatch(r"\d{4}", texto):
        decada = int(texto)
        if decada % 10 == 0 and 1870 <= decada <= 2100:
            return pl.col("decada") == decada, f"{decada}s"
    if dimensao == "genero" and 0 < len(texto) <= 60:
        return pl.col("generos").list.contains(texto), texto
    if dimensao == "tema" and 0 < len(texto) <= 80:
        return pl.col("keywords").list.contains(texto), texto
    if dimensao == "idioma" and re.fullmatch(r"[a-z]{2,3}", texto):
        # "xx" também é o idioma vazio (ver `por_idioma`).
        codigos = ["xx", ""] if texto == "xx" else [texto]
        return pl.col("idioma").is_in(codigos), nome_do_idioma(texto)
    if dimensao == "duracao" and texto in _CHAVES_DE_DURACAO:
        indice = _CHAVES_DE_DURACAO[texto]
        baixo = FAIXAS_DE_DURACAO[indice - 1][0] if indice else 0
        _, nome, legenda = FAIXAS_DE_DURACAO[indice]
        duracao = pl.col("duracao")
        return (duracao > baixo) & (duracao <= FAIXAS_DE_DURACAO[indice][0]), f"{nome} ({legenda})"
    if dimensao == "pais" and re.fullmatch(r"[A-Za-z]{2}", texto):
        return pl.col("origem").list.contains(texto.upper()), None
    return None


def itens_da_dimensao(frame: pl.DataFrame, dimensao: str, filtro: pl.Expr) -> dict:
    """Sessões (dimensão temporal) ou filmes distintos que passam no filtro.

    Sessões: uma por entrada com data (invariante 2), a mais nova primeiro, com a
    nota daquela entrada. Filmes: o filme entra se qualquer sessão passa, mas a
    nota é a média de *todas* as suas sessões avaliadas, com a amostra
    (`avaliacoes`); do mais bem avaliado, sem nota por último, depois ano e
    título, para a ordem não depender da ordem do arquivo.
    """
    if dimensao in DIMENSOES_TEMPORAIS:
        itens = [
            _cartao_de_filme(linha)
            for linha in dated(frame).filter(filtro)
            .sort(["data_assistida", "titulo"], descending=[True, False]).to_dicts()
        ]
        return {"tipo": "sessoes", "total": len(itens), "itens": itens}
    ids = frame.filter(filtro)["tmdb_id"].drop_nulls().unique().to_list()
    itens = [
        _cartao_de_filme(linha)
        for linha in _um_por_filme(frame.filter(pl.col("tmdb_id").is_in(ids)), "origem")
        .with_columns(pl.col("nota").round(2))
        .sort(["nota", "ano", "titulo"], descending=[True, False, False], nulls_last=True)
        .to_dicts()
    ]
    return {"tipo": "filmes", "total": len(itens), "itens": itens}


def ranking_completo(frame: pl.DataFrame, dimensao: str) -> list[dict]:
    """Ranking inteiro de uma dimensão em lista; os cartões mostram só o começo.

    `valor` é o que volta a `analisar_dimensao`; `nome`, o que se lê. Temas só
    entram com `MIN_AMOSTRA` filmes, como no cartão: um histórico real tem
    milhares de palavras-chave vistas uma vez.
    """
    if dimensao == "duracao":
        d = duracao_dos_filmes(frame)
        return [{"valor": chave, "nome": f["valor"], "detalhe": f["detalhe"],
                 "filmes": f["filmes"], "pct": f["pct"]}
                for chave, f in zip(_CHAVES_DE_DURACAO, d["faixas"])] if d else []
    ranking = {"genero": por_genero, "idioma": por_idioma, "tema": por_keyword}[dimensao](frame)
    return [{"valor": linha.get("codigo", linha["valor"]), "nome": linha["valor"],
             "filmes": linha["filmes"], "pct": None if dimensao == "tema" else linha["pct"]}
            for linha in ranking.to_dicts()]


def por_idioma(frame: pl.DataFrame) -> pl.DataFrame:
    """Idioma original é escalar, não lista — por isso não passa pelo `_ranking`.

    `valor` é o nome em português (o código cru, "ka" ou "cn", não diz nada) e
    `codigo`, o código do TMDB, para quem precisa voltar a ele. Idioma vazio é
    tratado como "xx" (sem idioma).
    """
    return (
        frame.drop_nulls("idioma")
        .with_columns(pl.col("idioma").replace("", "xx"))
        .group_by("idioma")
        .agg(pl.len().alias("filmes"), pl.col("nota").mean().round(2).alias("nota_media"))
        .sort("filmes", descending=True)
        .with_columns(
            pl.col("idioma").alias("codigo"),
            pl.col("idioma").map_elements(nome_do_idioma, return_dtype=pl.String).alias("valor"),
            # Mesma base do `_ranking`, para o cartão poder ficar ao lado do de
            # gêneros falando a mesma língua.
            (100 * pl.col("filmes") / max(frame.height, 1)).round(1).alias("pct"),
        )
        .drop("idioma")
    )


def explorados(ranking: pl.DataFrame) -> dict:
    """Quantos valores distintos há num ranking e qual é o menos frequente.

    Empate pelo nome, para o resultado não depender da ordem do arquivo. Serve
    aos cartões de gêneros e idiomas; `entradas` é quantas entradas o menos
    frequente tem.
    """
    if not ranking.height:
        return {"total": 0, "menos": None, "entradas": 0}
    menos = ranking.sort(["filmes", "valor"]).row(0, named=True)
    return {"total": ranking.height, "menos": menos["valor"], "entradas": menos["filmes"]}


# Fama de um filme = votos no TMDB, que é estável. A `popularity` do TMDB mede o
# que está em alta agora e chamaria um clássico dos anos 70 de obscuro. A escala
# é logarítmica entre um piso e um teto: quase nenhum filme registrado tem menos
# de ~100 votos, então é dali que a escala útil começa (com o piso em 0 todo
# filme de arte já pontuava alto); com `VOTOS_TETO` votos a fama é máxima.
VOTOS_PISO = 100
VOTOS_TETO = 30000

# Até que pontuação (inclusive) vale cada perfil. Tabela única, para limiares e
# frases não se separarem.
PERFIS_DE_FAMA = [
    (44, "Garimpeiro", "Vive atrás de filmes que pouca gente viu"),
    (64, "Equilibrado", "Mistura achados com filmes conhecidos"),
    (79, "Segue o público", "Prefere filmes populares e conhecidos"),
    (100, "Arrasa-quarteirão", "Assiste ao que todo mundo está assistindo"),
]

# Nota mínima (média das sessões) para um filme pouco conhecido virar "achado".
NOTA_DE_ACHADO = 4.0
ACHADOS = 2


def perfil_de_fama(pontuacao: int) -> tuple[str, str]:
    """Rótulo e frase de quem tem a pontuação dada (0 a 100)."""
    _, rotulo, frase = next(t for t in PERFIS_DE_FAMA if pontuacao <= t[0])
    return rotulo, frase


def _fama(votos: int) -> float:
    piso, teto = math.log10(VOTOS_PISO), math.log10(VOTOS_TETO)
    return min(1.0, max(0.0, (math.log10(max(votos, 1)) - piso) / (teto - piso)))


def popular_ou_garimpo(frame: pl.DataFrame) -> dict | None:
    """Pontuação de fama do histórico, o perfil dela e os "achados".

    Conta filmes distintos que têm metadados (o idioma só falta quando o filme
    não resolveu no TMDB). Filme com até `VOTOS_PISO` votos (inclusive os que
    ninguém avaliou) tem fama 0: aqui isso é obscuridade, não ausência de dado.
    Os achados são os filmes que você avaliou bem (média das sessões) e que
    quase ninguém conhece, do menos votado para o mais.
    """
    filmes = (
        frame.drop_nulls(["tmdb_id", "idioma"])
        .group_by("tmdb_id", maintain_order=True)
        .agg(
            pl.col("titulo").first(), pl.col("ano").first(), pl.col("poster").first(),
            pl.col("votos_tmdb").first(),
            pl.col("nota").mean().round(2),
            pl.col("nota").count().alias("avaliacoes"),
        )
    )
    if not filmes.height:
        return None
    pontuacao = round(100 * sum(_fama(v) for v in filmes["votos_tmdb"]) / filmes.height)
    rotulo, frase = perfil_de_fama(pontuacao)
    achados = (
        filmes.filter(pl.col("nota") >= NOTA_DE_ACHADO)
        .sort(["votos_tmdb", "nota", "titulo"], descending=[False, True, False])
        .head(ACHADOS)
    )
    return {
        "pontuacao": pontuacao, "rotulo": rotulo, "frase": frase,
        "filmes": filmes.height,
        "achados": [_cartao_de_filme(linha) for linha in achados.to_dicts()],
    }


# Faixas de duração: limite superior (inclusive) em minutos, nome e legenda. Os
# limites não se sobrepõem: 90 é "Padrão", 121 é "Longos".
FAIXAS_DE_DURACAO = [
    (89, "Curtos", "< 90 min"),
    (120, "Padrão", "90–120 min"),
    (150, "Longos", "121–150 min"),
    (math.inf, "Épicos", "> 150 min"),
]


def duracao_dos_filmes(frame: pl.DataFrame) -> dict | None:
    """Média, faixas de duração e os filmes mais curto e mais longo.

    Filmes distintos com duração conhecida, a mesma base (e a mesma média) do
    cartão "Duração média" da visão geral. Empate por título. `detalhe` é a faixa
    em minutos, que a interface mostra menor ao lado do nome.
    """
    conhecidos = _duracoes_conhecidas(frame)
    if not conhecidos.height:
        return None
    contagem = Counter(
        next(i for i, (limite, _, _) in enumerate(FAIXAS_DE_DURACAO) if minutos <= limite)
        for minutos in conhecidos["duracao"]
    )

    def extremo(*, maior: bool) -> dict:
        linha = conhecidos.sort(["duracao", "titulo"], descending=[maior, False]).row(0, named=True)
        return {k: linha[k] for k in ("titulo", "ano", "tmdb_id", "duracao")}

    return {
        **_duracao_media(frame),
        "faixas": [
            {"valor": nome, "detalhe": legenda, "filmes": contagem[i],
             "pct": round(100 * contagem[i] / conhecidos.height, 1)}
            for i, (_, nome, legenda) in enumerate(FAIXAS_DE_DURACAO)
        ],
        "curto": extremo(maior=False),
        "longo": extremo(maior=True),
    }


# Quantas fatias de gênero a barra dos revistos mostra; o resto vira "Outros".
GENEROS_DOS_REVISTOS = 5


def revisitas(frame: pl.DataFrame) -> dict:
    """Filmes revistos, o mais revisto, o diretor mais revisto e os gêneros.

    Um filme é *revisto* se tem 2 ou mais sessões (entradas com o mesmo `tmdb_id`)
    ou se alguma entrada dele leva a marca `rewatch` do diário (quem marca vê o
    filme contado mesmo com uma sessão só, "×1"). Entrada sem `tmdb_id` não se
    identifica e fica de fora.

    Uma *sessão de revisita* é a marcada `rewatch` ou qualquer uma que não seja a
    mais antiga do filme (as sem data vêm depois das datadas; se todas são sem
    data, só uma escapa). "Tempo revendo" soma duração × sessões de revisita
    (duração desconhecida conta 0).

    O destaque é o filme com mais sessões; empate pela maior nota média (sem nota
    perde de com nota), depois a sessão mais recente e o título. Sua linha do
    tempo traz as sessões datadas, da mais antiga à mais nova, e as sem data no
    fim. O diretor mais revisto é o de mais filmes revistos distintos, depois mais
    sessões de revisita, depois o nome; cada diretor de um filme conta.
    """
    sessoes_do_filme = (
        frame.drop_nulls("tmdb_id")
        .sort("data_assistida", nulls_last=True, maintain_order=True)
        .with_columns(
            (pl.col("rewatch") | (pl.int_range(pl.len()).over("tmdb_id") > 0)).alias("revisita")
        )
    )
    filmes = (
        sessoes_do_filme.group_by("tmdb_id", maintain_order=True)
        .agg(
            pl.len().alias("sessoes"), pl.col("revisita").sum().alias("revisitas"),
            pl.col("titulo").first(), pl.col("ano").first(), pl.col("poster").first(),
            pl.col("duracao").first(), pl.col("generos").first(), pl.col("direcao").first(),
            pl.col("nota").mean().round(2).alias("nota_media"),
            pl.col("nota").count().alias("avaliadas"),
            pl.col("data_assistida").max().alias("ultima"),
        )
        .filter(pl.col("revisitas") >= 1)
        .sort(["sessoes", "nota_media", "ultima", "titulo"],
              descending=[True, True, True, False], nulls_last=True)
    )
    if not filmes.height:
        return {"filmes": 0}
    minutos = int((filmes["revisitas"] * filmes["duracao"].fill_null(0)).sum())

    topo = filmes.row(0, named=True)
    sessoes = (
        sessoes_do_filme.filter(pl.col("tmdb_id") == topo["tmdb_id"])
        .select("data_assistida", "nota").to_dicts()
    )
    generos = (
        filmes.select("generos").explode("generos", empty_as_null=True).drop_nulls("generos")
        .group_by("generos").agg(pl.len().alias("n")).sort(["n", "generos"], descending=[True, False])
        .to_dicts()
    )
    fatias = [{"nome": g["generos"], "n": g["n"]} for g in generos[:GENEROS_DOS_REVISTOS]]
    if outros := sum(g["n"] for g in generos[GENEROS_DOS_REVISTOS:]):
        fatias.append({"nome": "Outros", "n": outros})
    total = sum(f["n"] for f in fatias)
    for f in fatias:
        f["pct"] = round(100 * f["n"] / total, 1)

    por_diretor = (
        filmes.select("direcao", "revisitas", "tmdb_id", "titulo")
        .explode("direcao", empty_as_null=True).drop_nulls("direcao")
        .group_by("direcao", maintain_order=True)
        .agg(pl.len().alias("filmes"), pl.col("revisitas").sum(),
             pl.struct("tmdb_id", "titulo").alias("titulos"))
        .sort(["filmes", "revisitas", "direcao"], descending=[True, True, False])
    )
    diretor = por_diretor.row(0, named=True) if por_diretor.height else None

    return {
        "filmes": filmes.height,
        "diretores": por_diretor.height,
        "minutos": minutos,
        "destaque": {
            "tmdb_id": topo["tmdb_id"], "titulo": topo["titulo"], "ano": topo["ano"],
            "poster": topo["poster"], "sessoes": topo["sessoes"],
            "nota_media": topo["nota_media"], "avaliadas": topo["avaliadas"],
            "linha_do_tempo": [{"data": linha["data_assistida"], "nota": linha["nota"]}
                               for linha in sessoes],
        },
        "diretor": diretor and {"nome": diretor["direcao"], "filmes": diretor["filmes"],
                                "titulos": diretor["titulos"]},
        "generos": fatias,
        "lista": [{k: linha[k] for k in ("tmdb_id", "titulo", "ano", "poster", "sessoes")}
                  for linha in filmes.to_dicts()],
    }


def por_decada(frame: pl.DataFrame) -> pl.DataFrame:
    """Década de lançamento do filme — não depende de quando foi assistido."""
    return (
        frame.drop_nulls("decada")
        .group_by("decada")
        .agg(pl.len().alias("filmes"), pl.col("nota").mean().round(2).alias("nota_media"))
        .sort("decada")
        .rename({"decada": "valor"})
    )


def distribuicao_de_notas(frame: pl.DataFrame) -> pl.DataFrame:
    return (
        frame.drop_nulls("nota")
        .group_by("nota")
        .agg(pl.len().alias("filmes"))
        .sort("nota")
    )


def discordancias(frame: pl.DataFrame, limite: int = 10) -> pl.DataFrame:
    """Onde sua nota mais se afasta da média do TMDB.

    A nota do Letterboxd vai de 0,5 a 5 e a do TMDB de 0 a 10, então a sua é
    dobrada antes da comparação. Positivo = você gostou mais que a maioria.

    Filmes com menos de `MIN_VOTOS_TMDB` votos ficam de fora: um filme sem
    votos chega com média 0,0 e produziria a maior discordância possível sem
    que ninguém tenha discordado de nada.
    """
    return (
        frame.drop_nulls(["nota", "nota_tmdb"])
        .filter(pl.col("votos_tmdb") >= MIN_VOTOS_TMDB)
        .with_columns((pl.col("nota") * 2 - pl.col("nota_tmdb")).round(2).alias("diferenca"))
        .select("titulo", "ano", "tmdb_id", "poster", "nota", "nota_tmdb", "votos_tmdb", "diferenca")
        .sort("diferenca", descending=True)
        .pipe(lambda f: pl.concat([f.head(limite), f.tail(limite)]))
    )


# --- daqui para baixo, tudo depende de data assistida ---

def por_ano(frame: pl.DataFrame) -> pl.DataFrame:
    """Filmes assistidos por ano-calendário."""
    return (
        dated(frame)
        .with_columns(pl.col("data_assistida").dt.year().alias("ano_assistido"))
        .group_by("ano_assistido")
        .agg(pl.len().alias("filmes"), pl.col("nota").mean().round(2).alias("nota_media"))
        .sort("ano_assistido")
    )


def por_mes(frame: pl.DataFrame) -> pl.DataFrame:
    return (
        dated(frame)
        .with_columns(pl.col("data_assistida").dt.truncate("1mo").alias("mes"))
        .group_by("mes")
        .agg(pl.len().alias("filmes"), pl.col("nota").mean().round(2).alias("nota_media"))
        .sort("mes")
    )


def por_dia_da_semana(frame: pl.DataFrame) -> pl.DataFrame:
    """Em que dias você assiste. `weekday` do Polars: 1 = segunda, 7 = domingo."""
    nomes = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
    return (
        dated(frame)
        .with_columns(pl.col("data_assistida").dt.weekday().alias("dia"))
        .group_by("dia")
        .agg(pl.len().alias("filmes"))
        .sort("dia")
        .with_columns(
            pl.col("dia").map_elements(lambda d: nomes[d - 1], return_dtype=pl.String).alias("nome")
        )
    )
