from datetime import date

import pytest

from letterboxd.models import DiaryEntry, FilmRef, Profile, Source
from letterboxd.store import ProfileStore


@pytest.fixture
def store(tmp_path):
    with ProfileStore(tmp_path / "perfis.sqlite") as s:
        yield s


@pytest.fixture
def perfil():
    return Profile(
        username="Gabriel_Fac",
        entries=[
            DiaryEntry(film=FilmRef("Stalker", 1979, tmdb_id=1398),
                       rating=5.0, watched_date=date(2024, 3, 1), rewatch=True,
                       liked=True, review="que filme", tags=["arte"],
                       source=Source.EXPORT),
            DiaryEntry(film=FilmRef("Sem Data", 1999), rating=None),
        ],
        watchlist=[FilmRef("Solaris", 1972, tmdb_id=1000)],
        sources={Source.EXPORT, Source.RSS},
    )


def test_ida_e_volta_preserva_tudo(store, perfil):
    slug = store.salvar(perfil)
    lido = store.obter(slug)
    assert lido is not None

    original, recuperado = perfil.entries[0], lido.profile.entries[0]
    assert recuperado.film.title == original.film.title
    assert recuperado.film.tmdb_id == original.film.tmdb_id
    assert recuperado.rating == original.rating
    assert recuperado.watched_date == original.watched_date
    assert recuperado.rewatch is True
    assert recuperado.liked is True
    assert recuperado.review == "que filme"
    assert recuperado.tags == ["arte"]
    assert lido.profile.sources == {Source.EXPORT, Source.RSS}
    assert [f.title for f in lido.profile.watchlist] == ["Solaris"]


def test_entrada_sem_data_e_sem_nota_sobrevive(store, perfil):
    lido = store.obter(store.salvar(perfil))
    sem_data = lido.profile.entries[1]
    assert sem_data.watched_date is None
    assert sem_data.rating is None
    assert sem_data.liked is None


def test_cada_processamento_ganha_link_proprio(store, perfil):
    """O nome de usuário é público; derivar o endereço dele expõe o perfil.

    Reaproveitar o slug também não serve: quem processasse o mesmo nome de
    usuário depois receberia o link de quem processou antes.
    """
    primeiro = store.salvar(perfil)
    perfil.entries = perfil.entries[:1]
    segundo = store.salvar(perfil)

    assert primeiro != segundo
    assert "gabriel" not in primeiro.casefold()
    assert len(primeiro) >= 20, "curto demais para não ser adivinhado"
    assert len(store.obter(primeiro).profile.entries) == 2
    assert len(store.obter(segundo).profile.entries) == 1


def test_obter_inexistente(store):
    assert store.obter("ninguem") is None


def test_apagar(store, perfil):
    slug = store.salvar(perfil)
    assert store.apagar(slug) is True
    assert store.obter(slug) is None
    assert store.apagar(slug) is False


def test_recomendacoes_ida_e_volta(store, perfil):
    slug = store.salvar(perfil)
    store.salvar_recomendacoes(slug, {"itens": [{"titulo": "Solaris"}], "perfil_gosto": {}})
    assert store.obter_recomendacoes(slug)["itens"][0]["titulo"] == "Solaris"


def test_recomendacoes_vencem(store, perfil, monkeypatch):
    """O corpus de fundo é recarregado a cada seis meses e muda os pesos."""
    from datetime import timedelta

    import letterboxd.store as modulo

    slug = store.salvar(perfil)
    store.salvar_recomendacoes(slug, {"itens": [], "perfil_gosto": {}})
    monkeypatch.setattr(modulo, "VALIDADE_RECOMENDACOES", timedelta(seconds=-1))
    assert store.obter_recomendacoes(slug) is None


def test_apagar_leva_as_recomendacoes_junto(store, perfil):
    slug = store.salvar(perfil)
    store.salvar_recomendacoes(slug, {"itens": [], "perfil_gosto": {}})
    store.apagar(slug)
    assert store.obter_recomendacoes(slug) is None


def test_recomendacoes_de_perfil_desconhecido(store):
    assert store.obter_recomendacoes("ninguem") is None
