import pytest

from letterboxd.models import Source
from letterboxd.sources.rss import InvalidUsername, parse_feed, validate_username


def test_ignora_itens_que_nao_sao_diario(feed_xml):
    """Listas também viram <item>, mas sem os campos de filme."""
    profile = parse_feed(feed_xml, "alguem")
    assert len(profile.entries) == 3
    assert profile.sources == {Source.RSS}


def test_extrai_tmdb_id(feed_xml):
    """O ID do TMDB no feed é o que dispensa adivinhar o filme por título."""
    profile = parse_feed(feed_xml, "alguem")
    assert all(entry.film.tmdb_id is not None for entry in profile.entries)


def test_campos_da_entrada(feed_xml):
    entry = parse_feed(feed_xml, "alguem").entries[0]
    assert entry.film.title
    assert entry.film.year and 1870 < entry.film.year < 2100
    assert entry.rating is None or 0.5 <= entry.rating <= 5.0
    assert entry.watched_date is not None
    assert isinstance(entry.rewatch, bool)


def test_entradas_ordenadas_da_mais_recente(feed_xml):
    dates = [e.watched_date for e in parse_feed(feed_xml, "alguem").entries]
    assert dates == sorted(dates, reverse=True)


@pytest.mark.parametrize("nome", ["../admin", "a", "x" * 40, "user name", "", "a/b"])
def test_rejeita_usuario_invalido(nome):
    """O nome vem de formulário público e entra numa URL: precisa ser barrado."""
    with pytest.raises(InvalidUsername):
        validate_username(nome)


@pytest.mark.parametrize("entrada,esperado", [("Gabriel", "gabriel"), ("@exemplo", "exemplo"), ("  jose_2  ", "jose_2")])
def test_normaliza_usuario(entrada, esperado):
    assert validate_username(entrada) == esperado


def test_variantes_do_diario(feed_xml):
    """Rewatch sem review, nota e curtida, e entrada sem nota."""
    sem_review, com_review, sem_nota = parse_feed(feed_xml, "exemplo").entries
    assert sem_review.rewatch is True and sem_review.review is None, (
        "só o pôster e a linha 'Watched on' não são review")
    assert (sem_review.rating, sem_review.liked) == (3.0, False)
    assert (com_review.rating, com_review.liked, com_review.rewatch) == (4.5, True, False)
    assert com_review.review == "Quiet and generous. The routine is the plot."
    assert sem_nota.rating is None and sem_nota.film.title == "The Holdovers"
    assert [e.film.tmdb_id for e in (sem_review, com_review, sem_nota)] == [1398, 976893, 840430]
