from datetime import date

from letterboxd.models import DiaryEntry, FilmRef, Profile, Source, normalize_title


def test_normalize_title_ignora_acento_e_pontuacao():
    assert normalize_title("WALL·E") == normalize_title("WALL-E")
    assert normalize_title("Amélie") == normalize_title("amelie")
    assert normalize_title("Cidade de Deus") == "cidade de deus"


def test_merge_nao_duplica_a_mesma_sessao():
    """O mesmo filme na mesma data, vindo das duas fontes, é uma entrada só."""
    film = FilmRef(title="Stalker", year=1979)
    rss = Profile("g", [DiaryEntry(film=FilmRef("Stalker", 1979, tmdb_id=1398),
                                   watched_date=date(2024, 3, 1), liked=True,
                                   source=Source.RSS)], sources={Source.RSS})
    export = Profile("g", [DiaryEntry(film=film, rating=5.0,
                                      watched_date=date(2024, 3, 1),
                                      source=Source.EXPORT)], sources={Source.EXPORT})
    merged = rss.merge(export)
    assert len(merged.entries) == 1
    entrada = merged.entries[0]
    # Cada fonte contribui o que só ela tem: o RSS traz o tmdb_id e o like,
    # o export traz a nota.
    assert entrada.film.tmdb_id == 1398
    assert entrada.liked is True
    assert entrada.rating == 5.0
    assert merged.sources == {Source.RSS, Source.EXPORT}


def test_merge_preserva_sessoes_em_datas_diferentes():
    rss = Profile("g", [DiaryEntry(film=FilmRef("Stalker", 1979),
                                   watched_date=date(2024, 5, 9), source=Source.RSS)])
    export = Profile("g", [DiaryEntry(film=FilmRef("Stalker", 1979),
                                      watched_date=date(2024, 3, 1), source=Source.EXPORT)])
    assert len(rss.merge(export).entries) == 2
