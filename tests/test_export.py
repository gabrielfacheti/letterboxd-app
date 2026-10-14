import io
import zipfile

import pytest

from letterboxd.sources.export import InvalidExport, parse_export


def test_rewatch_gera_entradas_distintas(export_zip):
    """Assistir de novo é um evento novo, não uma duplicata a ser descartada."""
    profile = parse_export(export_zip, "gabriel")
    stalker = [e for e in profile.entries if e.film.title == "Stalker"]
    assert len(stalker) == 2
    assert {e.rating for e in stalker} == {5.0, 4.5}


def test_casa_titulos_com_grafias_diferentes(export_zip):
    """'WALL·E' no diário e 'WALL-E' no ratings.csv são o mesmo filme."""
    profile = parse_export(export_zip, "gabriel")
    walle = next(e for e in profile.entries if "WALL" in e.film.title)
    assert walle.rating == 4.0


def test_inclui_filme_ausente_do_diario(export_zip):
    """Histórico importado do IMDb entra só em watched.csv, sem data."""
    profile = parse_export(export_zip, "gabriel")
    sem_diario = [e for e in profile.entries if e.film.title == "Sem Diário"]
    assert len(sem_diario) == 1
    assert sem_diario[0].watched_date is None
    assert sem_diario[0].rating == 3.5


def test_entradas_sem_data_vao_para_o_fim(export_zip):
    profile = parse_export(export_zip, "gabriel")
    assert profile.entries[-1].watched_date is None
    assert profile.entries[0].watched_date is not None


def test_likes_e_tags(export_zip):
    profile = parse_export(export_zip, "gabriel")
    stalker = [e for e in profile.entries if e.film.title == "Stalker"]
    assert all(e.liked for e in stalker)
    assert sorted(e.tags for e in stalker if e.tags) == [["arte", "russo"]]


def test_liked_fica_indefinido_sem_arquivo_de_likes(export_zip):
    """Sem likes/films.csv não dá para afirmar que o usuário não curtiu nada."""
    original = zipfile.ZipFile(io.BytesIO(export_zip))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as novo:
        for item in original.infolist():
            if item.filename != "likes/films.csv":
                novo.writestr(item.filename, original.read(item.filename))
    profile = parse_export(buffer.getvalue(), "gabriel")
    assert all(e.liked is None for e in profile.entries)


def test_watchlist(export_zip):
    profile = parse_export(export_zip, "gabriel")
    assert [f.title for f in profile.watchlist] == ["Solaris"]


def test_rejeita_arquivo_que_nao_e_zip():
    with pytest.raises(InvalidExport):
        parse_export(b"isso nao e um zip", "gabriel")


def test_rejeita_zip_sem_csvs_do_letterboxd():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("aleatorio.txt", "oi")
    with pytest.raises(InvalidExport, match="não parece um export"):
        parse_export(buffer.getvalue(), "gabriel")


def test_rejeita_zip_bomb():
    """Um ZIP que descomprime para muito além do declarado é ataque."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diary.csv", "a" * (50 * 1024 * 1024))
    with pytest.raises(InvalidExport):
        parse_export(buffer.getvalue(), "gabriel")


def test_ignora_diary_das_subpastas_deleted_e_orphaned(export_zip):
    """O export real traz `deleted/diary.csv` e `orphaned/diary.csv`.

    São filmes removidos da conta e registros órfãos — não fazem parte do
    histórico. A leitura por nome exato já os exclui; este teste existe para
    que ninguém troque isso por uma busca por sufixo sem perceber o estrago.
    """
    import io
    import zipfile

    original = zipfile.ZipFile(io.BytesIO(export_zip))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as novo:
        for item in original.infolist():
            novo.writestr(item.filename, original.read(item.filename))
        novo.writestr("deleted/diary.csv", (
            "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n"
            "2023-01-01,Filme Apagado,2000,https://boxd.it/zzz,5,,,2023-01-01\n"))
        novo.writestr("orphaned/diary.csv", (
            "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n"
            "2023-02-01,Filme Orfao,2001,https://boxd.it/yyy,4,,,2023-02-01\n"))

    profile = parse_export(buffer.getvalue(), "gabriel")
    titulos = {e.film.title for e in profile.entries}
    assert "Filme Apagado" not in titulos
    assert "Filme Orfao" not in titulos
