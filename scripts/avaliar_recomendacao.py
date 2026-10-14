"""Mede a qualidade do modelo de recomendação contra dados reais.

Duas avaliações, porque cada uma controla um viés diferente:

1. **Watchlist vs. fundo retido.** O positivo é o que a pessoa declarou querer
   ver; o modelo nunca viu isso. Mede utilidade direta, mas watchlist e
   histórico compartilham um viés cultural que o corpus do TMDB não tem.
2. **5-fold com assistidos retidos.** Ajusta em 80% do histórico e testa nos
   20% restantes. Não envolve a watchlist, então isola o viés acima.

Em ambos os casos os positivos são **removidos do fundo** antes de qualquer
cálculo. Sem isso, filmes da watchlist acabam no grupo de controle e o AUC é
subestimado — foi o que aconteceu na primeira medição (0,77 em vez de 0,84).

Uso:
    uv run python scripts/avaliar_recomendacao.py data/letterboxd-*.zip
"""

from __future__ import annotations

import asyncio
import random
import statistics
import sys
from pathlib import Path

import httpx

from letterboxd import recommend, stats
from letterboxd.config import load_settings
from letterboxd.sources.export import parse_export
from letterboxd.tmdb.cache import SQLiteCache
from letterboxd.tmdb.client import TMDBClient

FOLDS = 5
FRACAO_TREINO = 0.8


def _auc_de(taste: recommend.TasteProfile, positivos, negativos) -> float:
    return recommend.auc(
        [recommend.score_film(taste, d).score for d in positivos],
        [recommend.score_film(taste, d).score for d in negativos],
    )


async def main(caminho: Path) -> None:
    settings = load_settings()
    profile = parse_export(caminho, caminho.stem)

    cache = SQLiteCache(settings.cache_path)
    async with httpx.AsyncClient(timeout=30) as http:
        client = TMDBClient(settings.tmdb_key, http, cache)
        meta = await client.enrich(e.film for e in profile.entries)
        frame = stats.build_frame(profile, meta)

        ids = cache.get_background()
        fundo = await recommend.fetch_background(client, ids or None)
        if not ids:
            cache.put_background(fundo)

        vistos = recommend.vistos_de(frame)
        watchlist = {
            i: d for i, d in (await client.enrich(profile.watchlist)).items()
            if i not in vistos
        }
    cache.close()

    # O fundo tem que ser neutro: fora tudo que é positivo em qualquer avaliação.
    neutro = [
        d for i, d in fundo.items()
        if i not in vistos and i not in watchlist
    ]
    historico = list(meta.values())
    print(f"histórico {len(historico)} | watchlist {len(watchlist)} | fundo neutro {len(neutro)}\n")

    print("=== 1. watchlist vs. fundo retido ===")
    itens = neutro[:]
    random.Random(42).shuffle(itens)
    meio = len(itens) // 2
    taste = recommend.build_taste(frame, itens[:meio])
    auc_wl = _auc_de(taste, watchlist.values(), itens[meio:])
    print(f"  AUC {auc_wl:.3f}")

    print(f"\n=== 2. {FOLDS}-fold, assistidos retidos ===")
    marcas = []
    for fold in range(FOLDS):
        rnd = random.Random(100 + fold)
        h, f = historico[:], neutro[:]
        rnd.shuffle(h)
        rnd.shuffle(f)
        corte_h, corte_f = int(len(h) * FRACAO_TREINO), len(f) // 2

        # O perfil precisa ser montado só com o trecho de treino, e `build_taste`
        # recebe a tabela — então a tabela também é recortada.
        ids_treino = {d.tmdb_id for d in h[:corte_h]}
        frame_treino = frame.filter(
            frame["tmdb_id"].is_in(list(ids_treino))
        )
        taste_fold = recommend.build_taste(frame_treino, f[:corte_f])
        marca = _auc_de(taste_fold, h[corte_h:], f[corte_f:])
        marcas.append(marca)
        print(f"  fold {fold}: AUC {marca:.3f}")
    print(f"  média {statistics.mean(marcas):.3f}  desvio {statistics.pstdev(marcas):.3f}")

    print("\n=== 3. baselines: o modelo bate o trivial? ===")
    rnd = random.Random(7)
    h, f = historico[:], neutro[:]
    rnd.shuffle(h)
    rnd.shuffle(f)
    retidos, controle = h[int(len(h) * FRACAO_TREINO):], f[len(f) // 2:]
    for nome, extrair in [
        ("nota média do TMDB", lambda d: d.vote_average or 0),
        ("popularidade", lambda d: d.popularity or 0),
        ("ano de lançamento", lambda d: d.release_year or 0),
        ("duração", lambda d: d.runtime or 0),
    ]:
        marca = recommend.auc([extrair(d) for d in retidos], [extrair(d) for d in controle])
        print(f"  {nome:<22} AUC {marca:.3f}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(Path(sys.argv[1])))
