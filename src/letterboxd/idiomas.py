"""Nome em português dos idiomas, a partir do código do TMDB.

O TMDB entrega `original_language` em ISO 639-1 ("en", "ja"), que numa lista
vira "menos assistido: ka" — sem sentido para quem não decorou os códigos. A
tabela cobre os idiomas mais comuns no cinema; o que não estiver nela volta
como veio, em vez de sumir.
"""

IDIOMAS = {
    "en": "Inglês", "ja": "Japonês", "fr": "Francês", "ko": "Coreano",
    "es": "Espanhol", "it": "Italiano", "de": "Alemão", "zh": "Chinês",
    # O TMDB usa "cn", que não é ISO 639-1, para o cantonês.
    "cn": "Cantonês", "pt": "Português", "ru": "Russo", "hi": "Hindi",
    "sv": "Sueco", "da": "Dinamarquês", "no": "Norueguês",
    "fa": "Persa", "ar": "Árabe", "tr": "Turco", "pl": "Polonês",
    "nl": "Neerlandês", "th": "Tailandês", "he": "Hebraico", "el": "Grego",
    "hu": "Húngaro", "cs": "Tcheco", "ro": "Romeno", "fi": "Finlandês",
    "id": "Indonésio", "tl": "Tagalo", "uk": "Ucraniano", "ka": "Georgiano",
    "bn": "Bengali", "ta": "Tâmil", "te": "Télugo", "ml": "Malaiala",
    "mr": "Marati", "pa": "Punjabi", "ur": "Urdu", "vi": "Vietnamita",
    "ms": "Malaio", "sr": "Sérvio", "hr": "Croata", "bg": "Búlgaro",
    "sk": "Eslovaco", "sl": "Esloveno", "lt": "Lituano", "lv": "Letão",
    "et": "Estoniano", "is": "Islandês", "ca": "Catalão", "eu": "Basco",
    "gl": "Galego", "la": "Latim", "sq": "Albanês", "hy": "Armênio",
    "kk": "Cazaque", "mn": "Mongol", "ne": "Nepalês", "sw": "Suaíli",
    "af": "Africâner", "bs": "Bósnio", "mk": "Macedônio", "ga": "Irlandês",
    "cy": "Galês", "yi": "Iídiche", "ku": "Curdo",
    # "xx" é o código do TMDB para filme sem diálogo; vazio, para não informado.
    "xx": "Sem idioma", "": "Sem idioma",
}


def nome_do_idioma(codigo: str) -> str:
    """Nome em português do idioma, ou o próprio código se não for conhecido."""
    return IDIOMAS.get(codigo, codigo)
