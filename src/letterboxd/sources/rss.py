"""Leitura do feed RSS público de um usuário do Letterboxd.

O feed vive em `https://letterboxd.com/{usuario}/rss/`, é público, não pede
autenticação e é oferecido pelo próprio Letterboxd — é o caminho sancionado
para ler a atividade de um perfil.

Duas características definem o que dá para fazer com ele:

- traz `<tmdb:movieId>` em cada entrada, o que dispensa adivinhar qual filme do
  TMDB corresponde ao registro (o export não tem esse campo);
- devolve apenas as ~50 entradas mais recentes do diário e não pagina, então
  não serve para estatísticas de vida inteira — só o export serve.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from xml.etree import ElementTree

import httpx

from letterboxd.models import DiaryEntry, FilmRef, Profile, Source

FEED_URL = "https://letterboxd.com/{username}/rss/"

# Os prefixos são declarados na raiz do feed; o ElementTree exige a forma
# expandida `{uri}tag` na busca, então mapeamos uma vez aqui.
NS = {
    "letterboxd": "https://letterboxd.com",
    "tmdb": "https://themoviedb.org",
}

# O feed é pequeno (algumas centenas de KB). O teto existe para que uma
# resposta inesperadamente enorme não vire consumo de memória no servidor.
MAX_FEED_BYTES = 8 * 1024 * 1024

USERNAME_RE = re.compile(r"^[a-z0-9_]{2,32}$", re.IGNORECASE)


class InvalidUsername(ValueError):
    """Usuário fora do formato aceito pelo Letterboxd."""


class ProfileNotFound(LookupError):
    """Perfil inexistente, privado ou sem feed público."""


def validate_username(username: str) -> str:
    """Valida e normaliza o nome de usuário antes de montar qualquer URL.

    Serve como barreira contra injeção de caminho: o valor vem de um campo de
    formulário aberto ao público e é concatenado em uma URL.
    """
    candidate = username.strip().lstrip("@").casefold()
    if not USERNAME_RE.match(candidate):
        raise InvalidUsername(f"nome de usuário inválido: {username!r}")
    return candidate


def _text(item: ElementTree.Element, path: str) -> str | None:
    """Extrai o texto de um filho opcional, devolvendo None se ausente/vazio."""
    found = item.find(path, NS)
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


def _parse_yes_no(value: str | None) -> bool | None:
    """Converte os campos "Yes"/"No" do feed em booleano."""
    if value is None:
        return None
    return value.strip().casefold() == "yes"


def _parse_watched_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_pub_date(value: str | None) -> date | None:
    """Converte o `<pubDate>` em formato RFC 822 usado pelo RSS."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%a, %d %b %Y %H:%M:%S %z").date()
    except ValueError:
        return None


def _strip_poster(description: str | None) -> str | None:
    """Isola o texto da review, descartando o `<img>` do pôster.

    O `<description>` do feed é sempre o pôster seguido, opcionalmente, do
    texto da review. Sem review, sobra só a linha "Watched on ...".
    """
    if not description:
        return None
    without_images = re.sub(r"<img[^>]*>", "", description)
    without_tags = re.sub(r"<[^>]+>", " ", without_images)
    text = re.sub(r"\s+", " ", without_tags).strip()
    # Entradas sem review rendem exatamente esta frase gerada pelo Letterboxd.
    if not text or re.fullmatch(r"Watched on \w+ \w+ \d+, \d{4}\.?", text):
        return None
    return text


def parse_feed(xml: str | bytes, username: str) -> Profile:
    """Transforma o XML do feed em um `Profile`.

    Separado do download de propósito: permite testar com um feed salvo em
    disco, sem depender de rede.
    """
    root = ElementTree.fromstring(xml)
    entries: list[DiaryEntry] = []

    for item in root.iterfind("./channel/item"):
        title = _text(item, "letterboxd:filmTitle")
        if title is None:
            # Listas e outras atividades também viram <item>, mas sem os campos
            # de filme. Só entradas de diário interessam aqui.
            continue

        year_text = _text(item, "letterboxd:filmYear")
        # `tmdb:tvId` aparece no lugar de `movieId` quando o registro é de série.
        tmdb_text = _text(item, "tmdb:movieId")
        rating_text = _text(item, "letterboxd:memberRating")

        film = FilmRef(
            title=title,
            year=int(year_text) if year_text and year_text.isdigit() else None,
            tmdb_id=int(tmdb_text) if tmdb_text and tmdb_text.isdigit() else None,
            letterboxd_uri=_text(item, "link"),
        )

        entries.append(
            DiaryEntry(
                film=film,
                rating=float(rating_text) if rating_text else None,
                watched_date=_parse_watched_date(_text(item, "letterboxd:watchedDate")),
                logged_date=_parse_pub_date(_text(item, "pubDate")),
                rewatch=_parse_yes_no(_text(item, "letterboxd:rewatch")) or False,
                liked=_parse_yes_no(_text(item, "letterboxd:memberLike")),
                review=_strip_poster(_text(item, "description")),
                source=Source.RSS,
            )
        )

    return Profile(username=username, entries=entries, sources={Source.RSS})


async def fetch_profile(username: str, client: httpx.AsyncClient) -> Profile:
    """Baixa e interpreta o feed de um usuário.

    Recebe o `client` de fora para que a aplicação reaproveite conexões e
    aplique seus próprios limites de tempo e de taxa — acessos ao Letterboxd
    vindos de um app público precisam ser contidos em um ponto só.
    """
    username = validate_username(username)
    response = await client.get(FEED_URL.format(username=username))

    if response.status_code == 404:
        raise ProfileNotFound(f"perfil não encontrado ou privado: {username}")
    response.raise_for_status()

    if len(response.content) > MAX_FEED_BYTES:
        raise ValueError(f"feed acima do limite de {MAX_FEED_BYTES} bytes")

    return parse_feed(response.content, username)
