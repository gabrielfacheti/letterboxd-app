import csv
import io
import zipfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def feed_xml() -> bytes:
    """Feed sintético com a estrutura do real: 3 entradas de diário + 1 item de lista.

    As entradas cobrem rewatch sem review (só o pôster e a linha "Watched on"),
    nota e curtida, e uma entrada sem nota. Tudo inventado, só os filmes são reais.
    """
    return (FIXTURES / "feed.xml").read_bytes()


def _csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


@pytest.fixture
def export_zip() -> bytes:
    """Export sintético cobrindo os casos que o parser precisa acertar.

    Inclui de propósito: um rewatch (duas entradas do mesmo filme), um filme
    presente só em `watched.csv`, e um título grafado de formas diferentes
    entre `diary.csv` ("WALL·E") e `ratings.csv` ("WALL-E").
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("diary.csv", _csv_bytes(
            ["Date", "Name", "Year", "Letterboxd URI", "Rating", "Rewatch", "Tags", "Watched Date"],
            [["2024-03-02", "Stalker", "1979", "https://boxd.it/aaa", "5", "", "arte, russo", "2024-03-01"],
             ["2024-05-10", "Stalker", "1979", "https://boxd.it/bbb", "4.5", "Yes", "", "2024-05-09"],
             ["2024-06-01", "WALL·E", "2008", "https://boxd.it/ccc", "", "", "", "2024-05-31"]]))
        archive.writestr("watched.csv", _csv_bytes(
            ["Date", "Name", "Year", "Letterboxd URI"],
            [["2020-01-01", "Stalker", "1979", "https://boxd.it/aaa"],
             ["2020-01-02", "Sem Diário", "1999", "https://boxd.it/ddd"]]))
        archive.writestr("ratings.csv", _csv_bytes(
            ["Date", "Name", "Year", "Letterboxd URI", "Rating"],
            [["2024-06-01", "WALL-E", "2008", "https://boxd.it/ccc", "4"],
             ["2020-01-02", "Sem Diário", "1999", "https://boxd.it/ddd", "3.5"]]))
        archive.writestr("likes/films.csv", _csv_bytes(
            ["Date", "Name", "Year", "Letterboxd URI"],
            [["2024-03-02", "Stalker", "1979", "https://boxd.it/aaa"]]))
        archive.writestr("watchlist.csv", _csv_bytes(
            ["Date", "Name", "Year", "Letterboxd URI"],
            [["2024-07-01", "Solaris", "1972", "https://boxd.it/eee"]]))
    return buffer.getvalue()
