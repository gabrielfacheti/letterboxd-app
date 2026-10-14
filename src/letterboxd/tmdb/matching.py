"""Heurística que decide qual resultado de busca do TMDB é o filme procurado.

Fica fora do `TMDBClient` porque é lógica pura sobre os dicts que a busca
devolve: é a parte mais sujeita a ajuste fino, e assim se testa sem HTTP nem
mock. O que precisa de rede (`resolve`, títulos alternativos) segue no cliente.
"""

from __future__ import annotations

from letterboxd.models import normalize_title


def titles_of(item: dict) -> set[str]:
    """Títulos normalizados que a busca devolve para um resultado."""
    return {
        normalize_title(item.get("title") or ""),
        normalize_title(item.get("original_title") or ""),
    }


def year_of(item: dict) -> int | None:
    date_str = item.get("release_date") or ""
    return int(date_str[:4]) if date_str[:4].isdigit() else None


def pick_match(results: list[dict], title: str, year: int | None) -> dict | None:
    """Escolhe qual resultado da busca é o filme procurado.

    A busca do TMDB devolve por relevância, mas o primeiro resultado nem
    sempre é o certo — títulos genéricos ("Solaris", "Alice") trazem
    remakes e homônimos na frente. A ordem de preferência é:

    1. título idêntico (normalizado) e ano idêntico;
    2. título idêntico, ano com 1 de diferença — comum, porque o Letterboxd
       usa o ano de produção e o TMDB usa a data de lançamento, que podem
       cair em anos distintos para filmes de festival;
    3. título idêntico, qualquer ano, o mais popular;
    4. nada — melhor não adivinhar do que atribuir o filme errado.
    """
    target = normalize_title(title)
    exact = [item for item in results if target in titles_of(item)]
    if not exact:
        return None

    if year is not None:
        same_year = [item for item in exact if year_of(item) == year]
        if same_year:
            return max(same_year, key=lambda i: i.get("popularity") or 0)

        off_by_one = [
            item for item in exact
            if year_of(item) is not None and abs(year_of(item) - year) == 1
        ]
        if off_by_one:
            return max(off_by_one, key=lambda i: i.get("popularity") or 0)

    return max(exact, key=lambda i: i.get("popularity") or 0)
