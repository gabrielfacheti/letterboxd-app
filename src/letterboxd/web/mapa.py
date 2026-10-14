"""Mapa-múndi dos países de origem, pintado pela quantidade de filmes.

Os contornos vêm de `mundo.json`, gerado uma vez por `scripts/gerar_mapa.py` a
partir do Natural Earth. Ficam no servidor para a página não depender de nada
de fora.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

MUNDO = json.loads((Path(__file__).parent / "mundo.json").read_text())
NO_MAPA = {p["iso"] for p in MUNDO["paises"] if p["iso"]}
NOMES = {p["iso"]: p["nome"] for p in MUNDO["paises"] if p["iso"]}

# Quantos países de origem diferentes até cada patamar. A tabela mora aqui, num
# lugar só: o rótulo e a frase aparecem juntos no painel "Cinema pelo mundo".
EXPLORADORES = [
    (10, "Fica em casa", "Quase tudo vem de poucos lugares"),
    (25, "Turista de fim de semana", "Sai de casa de vez em quando"),
    (50, "Viajante", "Já passou por muitos cantos do mundo"),
    (math.inf, "Desbravador", "Vai aonde o cinema estiver"),
]

# Quantos países o painel lista; o resto vira "+N países".
PAINEL_TOP = 10


def nome_do_pais(iso: str, nomes: dict[str, str]) -> str:
    """Nome em português do mapa; sem contorno, o do TMDB (inglês); sem nada, o código."""
    return NOMES.get(iso) or nomes.get(iso, iso)


def explorador(paises: int) -> tuple[str, str]:
    """Rótulo e frase de quem viu filmes de `paises` países diferentes."""
    _, rotulo, frase = next(t for t in EXPLORADORES if paises <= t[0])
    return rotulo, frase


def montar(contagem: dict[str, int], nomes: dict[str, str]) -> dict:
    """Junta contagem por país e contornos.

    A intensidade é logarítmica: num histórico típico um país só (os EUA) tem
    mais da metade dos filmes, e numa escala linear todo o resto sairia quase
    da cor de quem não tem filme nenhum.

    `fora` lista o que o mapa não consegue mostrar — países extintos (União
    Soviética, Iugoslávia) e os pequenos demais para esta escala (Hong Kong,
    Singapura). Somem do desenho, mas não podem sumir em silêncio.
    """
    maximo = max(contagem.values(), default=0)
    paises = [
        {**pais, "filmes": (filmes := contagem.get(pais["iso"], 0)),
         "intensidade": round(math.log1p(filmes) / math.log1p(maximo), 3) if filmes else 0}
        for pais in MUNDO["paises"]
    ]
    fora = sorted(
        ((nomes.get(iso, iso), filmes) for iso, filmes in contagem.items() if iso not in NO_MAPA),
        key=lambda par: -par[1],
    )
    return {"largura": MUNDO["largura"], "altura": MUNDO["altura"],
            "paises": paises, "maximo": maximo, "fora": fora}


def painel(contagem: dict[str, int], nomes: dict[str, str], entradas: int) -> dict:
    """Dados do painel "Cinema pelo mundo": explorador, país mais visto e top 10.

    O nome vem do mapa (em português) quando o país tem contorno; sem ele, do
    TMDB (em inglês) — a mesma regra da nota sobre o que ficou fora do mapa. A
    porcentagem é sobre as entradas, como nas outras listas, e o número absoluto
    vai antes dela.
    """
    ordenados = sorted(
        ((iso, filmes, nome_do_pais(iso, nomes)) for iso, filmes in contagem.items()),
        key=lambda t: (-t[1], t[2]),
    )
    rotulo, frase = explorador(len(contagem))
    return {
        "paises": len(contagem),
        "rotulo": rotulo,
        "frase": frase,
        "mais_visto": ordenados[0][2] if ordenados else None,
        "top": [{"iso": iso, "nome": nome, "filmes": filmes,
                 "pct": round(100 * filmes / max(entradas, 1))}
                for iso, filmes, nome in ordenados[:PAINEL_TOP]],
        "restantes": max(len(ordenados) - PAINEL_TOP, 0),
    }
