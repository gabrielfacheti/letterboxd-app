"""Gera `src/letterboxd/web/mundo.json` a partir do Natural Earth.

Roda uma vez, à mão; o resultado é versionado. O site serve o mapa direto do
servidor, sem buscar nada de fora na hora da visita.

Fonte (domínio público): ne_110m_admin_0_countries.geojson, versão 5.1.2 de
https://github.com/nvkelso/natural-earth-vector

    python scripts/gerar_mapa.py caminho/ne_110m_admin_0_countries.geojson
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

DESTINO = Path(__file__).resolve().parents[1] / "src/letterboxd/web/mundo.json"
LARGURA = 960


def natural_earth(lon: float, lat: float) -> tuple[float, float]:
    """Projeção Natural Earth I (Šavrič et al.), a mesma do d3.geoNaturalEarth1.

    Equiretangular seria mais simples, mas estica a Europa e encolhe os
    trópicos — e é justamente a Europa que concentra boa parte dos filmes.
    """
    lam, phi = math.radians(lon), math.radians(lat)
    phi2 = phi * phi
    phi4 = phi2 * phi2
    x = lam * (0.8707 - 0.131979 * phi2
               + phi4 * (-0.013791 + phi4 * (0.003971 * phi2 - 0.001529 * phi4)))
    y = phi * (1.007226 + phi2 * (0.015085
               + phi4 * (-0.044475 + 0.028874 * phi2 - 0.005916 * phi4)))
    return x, y


def aneis(geometria: dict) -> list[list[list[float]]]:
    if geometria["type"] == "Polygon":
        return geometria["coordinates"]
    return [anel for poligono in geometria["coordinates"] for anel in poligono]


def main(origem: Path) -> None:
    features = [
        f for f in json.loads(origem.read_text())["features"]
        # A Antártida ocupa um quarto da altura e nenhum filme vem de lá.
        if f["properties"]["ISO_A2_EH"] != "AQ"
    ]

    projetados = [
        [[natural_earth(lon, lat) for lon, lat in anel] for anel in aneis(f["geometry"])]
        for f in features
    ]
    xs = [x for p in projetados for anel in p for x, _ in anel]
    ys = [y for p in projetados for anel in p for _, y in anel]
    escala = LARGURA / (max(xs) - min(xs))
    altura = math.ceil((max(ys) - min(ys)) * escala)

    paises = []
    for feature, poligonos in zip(features, projetados):
        partes = []
        for anel in poligonos:
            # Coordenadas inteiras e relativas: a 960 px de largura, meio pixel
            # de erro é invisível, e o arquivo cai para menos da metade.
            pontos = [(round((x - min(xs)) * escala), round((max(ys) - y) * escala))
                      for x, y in anel]
            pontos = [p for i, p in enumerate(pontos) if i == 0 or p != pontos[i - 1]]
            if len(pontos) < 3:
                continue
            (x0, y0), passos = pontos[0], []
            for (xa, ya), (xb, yb) in zip(pontos, pontos[1:]):
                passos.append(f"{xb - xa} {yb - ya}")
            partes.append(f"M{x0} {y0}l" + " ".join(passos) + "z")
        if not partes:
            continue
        props = feature["properties"]
        iso = props["ISO_A2_EH"]
        paises.append({
            # Territórios sem código (Chipre do Norte, Somalilândia) entram no
            # desenho, mas não recebem filmes: o TMDB não os usa.
            "iso": iso if iso != "-99" else None,
            "nome": props["NAME_PT"] or props["NAME"],
            "d": "".join(partes),
        })

    DESTINO.write_text(json.dumps(
        {"largura": LARGURA, "altura": altura, "paises": paises},
        ensure_ascii=False, separators=(",", ":"),
    ))
    print(f"{len(paises)} países, {DESTINO.stat().st_size // 1024} KB -> {DESTINO}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
