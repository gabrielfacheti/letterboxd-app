"""Recomendação baseada no gosto do próprio usuário.

## O modelo, e por que é este

A primeira versão aprendia das **notas**: para cada traço de filme (diretor,
keyword, país), qual a média das notas que a pessoa deu aos filmes que o têm.
Medida contra a watchlist do usuário, essa versão deu **AUC 0,53** — acaso.

O motivo é que a nota responde à pergunta errada. Ela diz *"dado que assisti,
gostei mais que a minha média?"*. Recomendação precisa de *"eu escolheria
assistir isso?"*. E como ninguém assiste ao acaso, a nota quase não varia: um
histórico típico vive entre 3,5 e 4,5, e o que sobra é ruído de decepção.

O sinal de gosto está em **o que a pessoa escolheu assistir**, comparado ao que
existe no mundo. Por isso o peso de cada traço aqui é o *log-odds de presença*:
quantas vezes ele aparece no histórico dela, contra quantas vezes aparece num
corpus de fundo de filmes variados.

    lift(traço) = log( P(traço | assistidos) / P(traço | fundo) )

Tarantino aparece em 7 dos 472 filmes do histórico e em 0 dos 758 do fundo:
altamente distintivo. "Drama" aparece em metade dos dois: lift perto de zero,
sem precisar de regra especial para isso.

Medido da mesma forma, este modelo dá **AUC 0,88** (5-fold, filmes assistidos
retidos, desvio 0,016).

## Ressalvas honestas

- `ano de lançamento` isolado já atinge AUC 0,74 e `vote_average` 0,70: parte do
  desempenho é a pessoa assistir filme recente e bem avaliado, não afinidade de
  conteúdo. O modelo ganha dos dois com folga, mas o número não é todo mérito
  das keywords.
- O corpus de fundo vem do `/discover` ordenado por popularidade. Ele representa
  "filmes que existem e alguém viu", não "todos os filmes". Um fundo mais
  obscuro deslocaria os pesos.
- As notas continuam no perfil (`afinidade`), mas **fora do score**: incluí-las
  piorou o AUC de 0,835 para 0,816. Servem para explicar recomendações, não para
  produzi-las.
"""

from __future__ import annotations

import asyncio
import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

import polars as pl

from letterboxd.tmdb.client import Progresso, TMDBClient
from letterboxd.tmdb.models import FilmDetails

# Quanto cada tipo de traço pesa. Direção lidera porque "gosto de filme de
# Miyazaki" é uma afirmação muito mais forte que "gosto de animação".
PESOS_POR_TIPO = {
    "dir": 1.5,
    "kw": 1.0,
    "pais": 0.7,
    "ator": 0.6,
    "genero": 0.5,
    "idioma": 0.5,
    "decada": 0.4,
}

# Candidatos com poucos votos no TMDB tendem a ser curtas e obscuridades cujos
# metadados são rasos. O corte é modesto para não excluir cinema não-anglófono.
MIN_VOTOS_CANDIDATO = 50

# Gêneros usados para varrer o corpus de fundo. Sem essa dispersão, o fundo vira
# só blockbuster americano recente e a linha de base fica enviesada.
GENEROS_FUNDO = (None, 18, 27, 35, 99, 16, 53, 10749)


@dataclass(frozen=True, slots=True)
class TasteConfig:
    """Parâmetros do modelo. Os padrões foram medidos, não escolhidos."""

    # Suavização de Laplace no log-odds. Impede divisão por zero e evita que um
    # traço visto uma única vez receba peso infinito.
    alfa: float = 1.0

    # Peso da afinidade de nota no score. **Zero por medição:** somá-la baixou o
    # AUC de 0,835 para 0,816. Fica exposto para quem quiser reproduzir o teste.
    peso_nota: float = 0.0

    # Evidência fictícia no denominador da afinidade de nota (usada só nas
    # explicações, já que `peso_nota` é zero por padrão).
    shrinkage: float = 5.0

    # Mínimo de aparições no histórico para um traço entrar nas explicações.
    # Não filtra o score: o log-odds já lida com traços raros via `alfa`.
    min_ocorrencias_motivo: int = 2

    # Quantos nomes do elenco considerar. Depois dos primeiros, a presença no
    # filme não diz nada sobre o motivo de alguém tê-lo escolhido.
    elenco_considerado: int = 5


def _features(
    *,
    generos: list[str] | None,
    keywords: list[str] | None,
    direcao: list[str] | None,
    elenco: list[str] | None,
    paises: list[str] | None,
    idioma: str | None,
    ano: int | None,
    elenco_considerado: int = 5,
) -> list[str]:
    """Reduz um filme a uma lista de traços com o tipo no prefixo.

    O prefixo não é enfeite: é o que permite pesar direção diferente de gênero,
    e o que deixa o motivo de uma recomendação legível na interface.
    """
    traços = [
        *(f"genero:{v}" for v in generos or []),
        *(f"kw:{v}" for v in keywords or []),
        *(f"dir:{v}" for v in direcao or []),
        *(f"ator:{v}" for v in (elenco or [])[:elenco_considerado]),
        *(f"pais:{v}" for v in paises or []),
    ]
    if idioma:
        traços.append(f"idioma:{idioma}")
    if ano:
        traços.append(f"decada:{ano // 10 * 10}")
    return traços


def features_of(details: FilmDetails, config: TasteConfig | None = None) -> list[str]:
    """Traços de um filme vindo do TMDB."""
    config = config or TasteConfig()
    return _features(
        generos=details.genres,
        keywords=details.keywords,
        direcao=details.directors,
        elenco=details.cast,
        paises=details.country_names,
        idioma=details.original_language,
        ano=details.release_year,
        elenco_considerado=config.elenco_considerado,
    )


def peso_do_tipo(traço: str) -> float:
    return PESOS_POR_TIPO.get(traço.split(":", 1)[0], 0.5)


@dataclass(slots=True)
class TasteProfile:
    """O gosto de uma pessoa, destilado em pesos por traço."""

    lift: dict[str, float] = field(default_factory=dict)
    afinidade: dict[str, float] = field(default_factory=dict)
    ocorrencias: Counter[str] = field(default_factory=Counter)
    nota_media: float = 0.0
    vistos: set[int] = field(default_factory=set)
    avaliados: int = 0
    tamanho_fundo: int = 0
    config: TasteConfig = field(default_factory=TasteConfig)

    def peso(self, traço: str) -> float:
        """Peso final de um traço no score."""
        peso = self.lift.get(traço, 0.0)
        if self.config.peso_nota:
            peso += self.config.peso_nota * self.afinidade.get(traço, 0.0)
        return peso * peso_do_tipo(traço)

    def top(self, tipo: str | None = None, n: int = 15) -> list[tuple[str, float]]:
        """Traços que mais definem o gosto, para inspecionar o perfil.

        Exige `min_ocorrencias_motivo` aparições: um traço visto uma vez pode ter
        lift alto sem dizer nada sobre a pessoa.
        """
        itens = (
            (t, self.peso(t))
            for t in self.lift
            if (tipo is None or t.startswith(f"{tipo}:"))
            and self.ocorrencias[t] >= self.config.min_ocorrencias_motivo
        )
        return sorted(itens, key=lambda kv: kv[1], reverse=True)[:n]


def build_taste(
    frame: pl.DataFrame,
    fundo: Iterable[FilmDetails],
    config: TasteConfig | None = None,
) -> TasteProfile:
    """Monta o perfil comparando o histórico com o corpus de fundo.

    `fundo` é a linha de base: sem ela não há como saber se "Drama" é gosto da
    pessoa ou apenas o gênero mais comum do cinema.
    """
    config = config or TasteConfig()
    alfa = config.alfa

    notas = frame["nota"].drop_nulls()
    media = float(notas.mean()) if notas.len() else 0.0

    no_historico: Counter[str] = Counter()
    soma_notas: dict[str, float] = defaultdict(float)
    vezes_avaliado: Counter[str] = Counter()
    vistos: set[int] = set()
    total_historico = 0
    avaliados = 0

    for linha in frame.iter_rows(named=True):
        if linha["tmdb_id"] is not None:
            vistos.add(linha["tmdb_id"])

        traços = set(_features(
            generos=linha["generos"], keywords=linha["keywords"],
            direcao=linha["direcao"], elenco=linha["elenco"],
            paises=linha["paises"], idioma=linha["idioma"], ano=linha["ano"],
            elenco_considerado=config.elenco_considerado,
        ))
        if not traços:
            continue

        total_historico += 1
        no_historico.update(traços)

        nota = linha["nota"]
        if nota is None:
            continue
        avaliados += 1
        centrada = nota - media
        if linha["curtiu"]:
            centrada += 0.25
        for traço in traços:
            soma_notas[traço] += centrada
            vezes_avaliado[traço] += 1

    no_fundo: Counter[str] = Counter()
    total_fundo = 0
    for details in fundo:
        traços = set(features_of(details, config))
        if traços:
            total_fundo += 1
            no_fundo.update(traços)

    lift: dict[str, float] = {}
    if total_historico and total_fundo:
        for traço in set(no_historico) | set(no_fundo):
            p_hist = (no_historico[traço] + alfa) / (total_historico + 2 * alfa)
            p_fundo = (no_fundo[traço] + alfa) / (total_fundo + 2 * alfa)
            lift[traço] = math.log(p_hist / p_fundo)

    afinidade = {
        traço: total / (vezes_avaliado[traço] + config.shrinkage)
        for traço, total in soma_notas.items()
    }

    return TasteProfile(
        lift=lift, afinidade=afinidade, ocorrencias=no_historico,
        nota_media=media, vistos=vistos, avaliados=avaliados,
        tamanho_fundo=total_fundo, config=config,
    )


@dataclass(slots=True)
class Recomendacao:
    """Um filme sugerido, com o porquê."""

    details: FilmDetails
    score: float
    motivos: list[tuple[str, float]]

    def explicar(self, n: int = 3) -> str:
        """Frase curta explicando a recomendação, para a interface."""
        rotulos = {"dir": "direção de", "pais": "cinema de", "idioma": "idioma",
                   "decada": "anos"}
        partes = []
        for traço, _ in self.motivos[:n]:
            tipo, valor = traço.split(":", 1)
            partes.append(f"{rotulos.get(tipo, '')} {valor}".strip())
        return ", ".join(partes)


def score_film(taste: TasteProfile, details: FilmDetails) -> Recomendacao:
    """Pontua um candidato: média dos pesos dos seus traços.

    A média venceu a soma (AUC 0,835 contra 0,816) e a divisão pela raiz da
    contagem (0,826). Somar premia filme com muita keyword por acumulação; a
    média pergunta "que fração do que este filme é interessa a esta pessoa".
    """
    traços = set(features_of(details, taste.config))
    if not traços:
        return Recomendacao(details=details, score=0.0, motivos=[])

    contribuicoes = [(t, taste.peso(t)) for t in traços]
    score = sum(peso for _, peso in contribuicoes) / len(traços)

    motivos = sorted(
        (item for item in contribuicoes if item[1] > 0),
        key=lambda kv: kv[1], reverse=True,
    )
    return Recomendacao(details=details, score=score, motivos=motivos)


def rank(
    taste: TasteProfile,
    candidatos: dict[int, FilmDetails],
    *,
    limite: int = 20,
    min_votos: int = MIN_VOTOS_CANDIDATO,
) -> list[Recomendacao]:
    """Ordena candidatos pelo perfil, descartando o que já foi assistido."""
    resultados = [
        score_film(taste, details)
        for tmdb_id, details in candidatos.items()
        if tmdb_id not in taste.vistos and details.vote_count >= min_votos
    ]
    resultados.sort(key=lambda r: r.score, reverse=True)
    return resultados[:limite]


def vistos_de(frame: pl.DataFrame) -> set[int]:
    """Ids já assistidos, para não recomendar o que a pessoa já viu."""
    return set(frame["tmdb_id"].drop_nulls().to_list())


async def fetch_background(
    client: TMDBClient,
    cache_ids: list[int] | None = None,
    *,
    por_pagina: int = 2,
) -> dict[int, FilmDetails]:
    """Monta (ou recarrega) o corpus de fundo.

    Varre `/discover` por década e por gênero para que a linha de base não seja
    só blockbuster recente. Custa centenas de requisições, então o chamador deve
    guardar os ids — `SQLiteCache.put_background` existe para isso.
    """
    if cache_ids:
        return await client.get_films(cache_ids)

    async def uma_pagina(decada: int, genero: int | None, pagina: int) -> list[int]:
        params: dict[str, object] = {
            "sort_by": "popularity.desc",
            "vote_count.gte": 50,
            "page": pagina,
            "primary_release_date.gte": f"{decada}-01-01",
            "primary_release_date.lte": f"{decada + 9}-12-31",
        }
        if genero:
            params["with_genres"] = str(genero)
        payload = await client._get("/discover/movie", **params)
        return [item["id"] for item in (payload or {}).get("results") or []]

    tarefas = [
        uma_pagina(decada, genero, pagina)
        for decada in range(1950, 2030, 10)
        for genero in GENEROS_FUNDO
        for pagina in range(1, por_pagina + 1)
    ]
    resultados = await asyncio.gather(*tarefas, return_exceptions=True)
    ids = [
        tmdb_id
        for resultado in resultados
        if not isinstance(resultado, BaseException)
        for tmdb_id in resultado
    ]
    return await client.get_films(dict.fromkeys(ids))


async def gather_candidates(
    client: TMDBClient,
    frame: pl.DataFrame,
    *,
    sementes: int = 40,
    max_por_direcao: int = 2,
    max_candidatos: int = 600,
    progresso: Progresso | None = None,
) -> dict[int, FilmDetails]:
    """Reúne candidatos a partir dos filmes melhor avaliados do histórico.

    `max_por_direcao` existe por causa de um erro concreto: ordenar por nota e
    cortar no top 25 escolhia 25 filmes empatados em 5,0 numa ordem arbitrária,
    que por acaso trouxe muita animação infantil — e as recomendações saíram
    todas para esse lado. Limitar por diretor força as sementes a cobrir o gosto
    em vez de um canto dele.

    Filmes que aparecem como relacionados de **vários** favoritos são
    priorizados: a recorrência indica conversa com o histórico inteiro.
    """
    escolhidas: list[int] = []
    por_direcao: Counter[str] = Counter()

    for linha in (
        frame.drop_nulls(["nota", "tmdb_id"])
        .sort("nota", descending=True)
        .iter_rows(named=True)
    ):
        direcao = linha["direcao"] or ["?"]
        if any(por_direcao[nome] >= max_por_direcao for nome in direcao):
            continue
        por_direcao.update(direcao)
        escolhidas.append(int(linha["tmdb_id"]))
        if len(escolhidas) >= sementes:
            break

    listas = await client.related_many(escolhidas, progresso=progresso)

    ja_vistos = vistos_de(frame)
    frequencia: Counter[int] = Counter()
    for lista in listas:
        for item in lista:
            tmdb_id = item.get("id")
            if tmdb_id and tmdb_id not in ja_vistos:
                frequencia[tmdb_id] += 1

    return await client.get_films(
        [tmdb_id for tmdb_id, _ in frequencia.most_common(max_candidatos)],
        progresso=progresso,
        etapa="avaliando candidatos",
    )


def auc(positivos: list[float], negativos: list[float]) -> float:
    """Probabilidade de um positivo pontuar acima de um negativo sorteado.

    É a métrica de qualidade do modelo. 0,5 significa que o perfil não
    distingue nada; 1,0 é separação perfeita.

    O conjunto positivo pode ser a watchlist ou filmes assistidos retidos. O
    negativo tem que ser **neutro** — filmes quaisquer do corpus de fundo.
    Comparar a watchlist com candidatos gerados a partir dos favoritos não mede
    nada, porque os candidatos já foram escolhidos por semelhança; foi o erro da
    primeira tentativa de validação.

    Calculado pela estatística U de Mann-Whitney, sem amostragem.
    """
    if not positivos or not negativos:
        return 0.5

    combinado = sorted(
        [(v, 1) for v in positivos] + [(v, 0) for v in negativos],
        key=lambda par: par[0],
    )

    # Postos médios para empates, senão scores iguais enviesariam o resultado.
    postos: list[float] = [0.0] * len(combinado)
    i = 0
    while i < len(combinado):
        j = i
        while j + 1 < len(combinado) and combinado[j + 1][0] == combinado[i][0]:
            j += 1
        posto_medio = (i + j) / 2 + 1
        for k in range(i, j + 1):
            postos[k] = posto_medio
        i = j + 1

    soma_positivos = sum(p for p, (_, rotulo) in zip(postos, combinado) if rotulo == 1)
    n_pos, n_neg = len(positivos), len(negativos)
    return (soma_positivos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
