"""País de nascimento a partir do `place_of_birth` do TMDB.

O campo é texto livre, escrito por voluntários: "London, England, UK",
"Brooklyn, New York City, New York, USA", "Moscow, Russian SFSR, USSR [now
Russia]". Não há código de país, então o país é o último trecho depois da
vírgula — e o mesmo país aparece com várias grafias (USA, U.S., United States),
que contariam como países diferentes num ranking se não fossem unificadas.
"""

from __future__ import annotations

import re

# Chaves em minúsculas e sem pontos ("U.S.A." vira "usa"). Cobre as grafias que
# aparecem de fato; o que não estiver aqui volta como veio, em vez de sumir.
_PAISES = {
    **dict.fromkeys(["usa", "us", "united states", "united states of america",
                     "america"], "Estados Unidos"),
    # Dentro do Reino Unido o TMDB costuma parar na nação, não no país.
    **dict.fromkeys(["uk", "united kingdom", "great britain", "britain", "england",
                     "scotland", "wales", "northern ireland"], "Reino Unido"),
    # Alguns voluntários escrevem o país na língua dele.
    **dict.fromkeys(["germany", "west germany", "deutschland"], "Alemanha"),
    **dict.fromkeys(["italy", "italia"], "Itália"),
    **dict.fromkeys(["france", "francia"], "França"),
    **dict.fromkeys(["denmark", "danmark"], "Dinamarca"),
    **dict.fromkeys(["china", "cina", "peoples republic of china", "prc"], "China"),
    **dict.fromkeys(["south korea", "korea", "republic of korea"], "Coreia do Sul"),
    **dict.fromkeys(["russia", "russian federation"], "Rússia"),
    **dict.fromkeys(["ussr", "soviet union"], "União Soviética"),
    **dict.fromkeys(["netherlands", "the netherlands", "holland"], "Países Baixos"),
    "japan": "Japão", "canada": "Canadá",
    "spain": "Espanha", "mexico": "México", "brazil": "Brasil",
    "hong kong": "Hong Kong", "taiwan": "Taiwan", "india": "Índia",
    "australia": "Austrália", "new zealand": "Nova Zelândia", "sweden": "Suécia",
    "norway": "Noruega", "finland": "Finlândia",
    "iran": "Irã", "israel": "Israel", "turkey": "Turquia", "egypt": "Egito",
    "ireland": "Irlanda", "belgium": "Bélgica", "austria": "Áustria",
    "switzerland": "Suíça", "poland": "Polônia", "hungary": "Hungria",
    "romania": "Romênia", "ukraine": "Ucrânia", "czech republic": "República Tcheca",
    "yugoslavia": "Iugoslávia", "serbia": "Sérvia", "croatia": "Croácia",
    "greece": "Grécia", "portugal": "Portugal", "argentina": "Argentina",
    "chile": "Chile", "colombia": "Colômbia", "south africa": "África do Sul",
    "thailand": "Tailândia", "north korea": "Coreia do Norte",
    "malaysia": "Malásia", "lebanon": "Líbano", "uruguay": "Uruguai",
    "iceland": "Islândia", "estonia": "Estônia", "lithuania": "Lituânia",
    "latvia": "Letônia", "czechoslovakia": "Tchecoslováquia",
}

_NOTA = re.compile(r"\[([^\]]*)\]")
# Só a nota no fim do texto fala do país: em "Ujiyamada [now Ise], Mie, Japan" o
# "now" renomeia a cidade.
_NOTA_FINAL = re.compile(r"\[([^\]]*)\]\s*$")


def _ultimo_trecho(texto: str) -> str:
    return texto.rsplit(",", 1)[-1].strip()


def _nome_do_pais(trecho: str) -> str:
    chave = trecho.lower().replace(".", "").replace("'", "").strip()
    return _PAISES.get(chave, trecho)


def pais_de_nascimento(place_of_birth: str | None) -> str | None:
    """País (em português) do local de nascimento, ou None se não houver local.

    Quando o texto termina em "[now X]", vale o X: quem nasceu em "Kiev, USSR [now
    Ukraine]" nasceu onde hoje é a Ucrânia. Sem essa nota, "USSR" fica
    "União Soviética" — mapeá-lo para a Rússia seria chute.
    """
    if not place_of_birth or not place_of_birth.strip():
        return None
    nota = _NOTA_FINAL.search(place_of_birth)
    atual = re.match(r"now\b\s*(?:in\s+)?(.*)", nota.group(1).strip(), re.I) if nota else None
    if atual and atual.group(1).strip():
        # A nota pode trazer cidade: "[now Wrocław, Poland]".
        return _nome_do_pais(_ultimo_trecho(atual.group(1)))
    return _nome_do_pais(_ultimo_trecho(_NOTA.sub("", place_of_birth))) or None
