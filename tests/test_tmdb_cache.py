from datetime import datetime, timedelta, timezone

import pytest

from letterboxd.tmdb.cache import CACHE_TTL, SQLiteCache
from letterboxd.tmdb.models import FilmDetails


@pytest.fixture
def cache(tmp_path):
    with SQLiteCache(tmp_path / "c.sqlite") as c:
        yield c


def test_roundtrip_preserva_os_campos(cache):
    film = FilmDetails(tmdb_id=1, title="Stalker", genres=["Drama"],
                       countries=["SU"], origin_countries=["SU"],
                       directors=["Andrei Tarkovsky"], keywords=["zone"], runtime=162,
                       pessoas={})
    cache.put_film(film)
    lido = cache.get_film(1)
    assert lido is not None
    assert (lido.title, lido.genres, lido.directors, lido.runtime, lido.origin_countries) == (
        "Stalker", ["Drama"], ["Andrei Tarkovsky"], 162, ["SU"])


def test_entrada_vencida_e_ignorada(cache):
    """Os termos do TMDB limitam o cache a 6 meses; vencido conta como ausente."""
    velho = FilmDetails(tmdb_id=2, title="Antigo",
                        fetched_at=datetime.now(timezone.utc) - CACHE_TTL - timedelta(days=1))
    cache.put_film(velho)
    assert cache.get_film(2) is None


def test_resolucao_negativa_e_lembrada(cache):
    """'Pesquisei e não existe' precisa ser distinguível de 'nunca pesquisei'."""
    assert cache.get_resolution("Filme Inexistente", 1999) == (False, None)
    cache.put_resolution("Filme Inexistente", 1999, None)
    assert cache.get_resolution("Filme Inexistente", 1999) == (True, None)


def test_resolucao_ignora_grafia_do_titulo(cache):
    cache.put_resolution("WALL·E", 2008, 10681)
    assert cache.get_resolution("WALL-E", 2008) == (True, 10681)


def test_ano_nulo_nao_colide_com_ano_preenchido(cache):
    cache.put_resolution("Stalker", None, 111)
    cache.put_resolution("Stalker", 1979, 222)
    assert cache.get_resolution("Stalker", None) == (True, 111)
    assert cache.get_resolution("Stalker", 1979) == (True, 222)


def test_entrada_sem_pais_de_origem_conta_como_ausente(cache):
    """Gravada antes do campo existir: precisa ser buscada de novo, não servida
    sem origem — senão o mapa ficaria vazio para quem já estava no cache."""
    cache.put_film(FilmDetails(tmdb_id=3, title="Antiga", origin_countries=None))
    assert cache.get_film(3) is None
    cache.put_film(FilmDetails(tmdb_id=4, title="Nova", origin_countries=["SU"], pessoas={}))
    assert cache.get_film(4).origin_countries == ["SU"]


def test_entrada_sem_pessoas_conta_como_ausente_a_menos_que_se_aceite(cache):
    """Gravada antes de `pessoas` existir: o job rebusca; a página aceita como está."""
    cache.put_film(FilmDetails(tmdb_id=5, title="Antiga", origin_countries=["US"]))
    assert cache.get_film(5) is None
    assert cache.get_film(5, aceitar_incompleto=True).title == "Antiga"


def test_aceitar_incompleto_nao_serve_entrada_vencida(cache):
    """O teto de 180 dias não tem exceção, nem para a página."""
    velho = FilmDetails(tmdb_id=6, title="Vencida", origin_countries=["US"],
                        fetched_at=datetime.now(timezone.utc) - CACHE_TTL - timedelta(days=1))
    cache.put_film(velho)
    assert cache.get_film(6, aceitar_incompleto=True) is None


def test_pessoas_roundtrip_e_diferenca_entre_ausente_e_sem_local(cache):
    cache.put_person(1, {"place_of_birth": "London, England, UK"})
    cache.put_person(2, {"place_of_birth": None})
    lidos = cache.get_people([1, 2, 3])
    assert lidos == {1: {"place_of_birth": "London, England, UK"},
                     2: {"place_of_birth": None}}, "3 nunca foi buscada: não aparece"
    assert cache.get_people([]) == {}


def test_pessoa_vencida_e_ignorada(cache):
    cache.put_person(7, {"place_of_birth": "Paris, France"})
    antigo = (datetime.now(timezone.utc) - CACHE_TTL - timedelta(days=1)).isoformat()
    cache._conn.execute("UPDATE people SET fetched_at = ?", (antigo,))
    assert cache.get_people([7]) == {}
