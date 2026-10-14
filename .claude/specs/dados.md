# Dados: de onde vêm e onde eles mentem

Leia antes de mexer em `sources/`, `models.py` ou `tmdb/`.

## Não existe API

A [API do Letterboxd](https://letterboxd.com/api-beta/) está em beta fechado e a
política exclui **explicitamente** projetos de análise de dados, visualização,
recomendação e uso pessoal. Nosso caso cai em quatro dessas exclusões. Pedir
acesso é grátis, mas não construa nada contando com isso.

Os Termos de Uso também proíbem raspagem: *"you must not employ any robot,
spider, scraper... to access, acquire, copy, or monitor any portion of the
Service"*. O projeto respeita isso — só usa o feed RSS público e o export que o
próprio usuário baixa.

## Duas fontes, forças opostas

| | RSS (`sources/rss.py`) | Export (`sources/export.py`) |
|---|---|---|
| Cobertura | ~50 entradas recentes, sem paginação | histórico completo |
| Identificação | **traz `<tmdb:movieId>`** | só título e ano |
| Extras | likes, review | notas, tags, rewatch, watchlist |
| Obtenção | público, por usuário | upload do próprio usuário |

O `<tmdb:movieId>` no RSS é o achado que elimina o problema mais chato do
projeto. Confirmado num feed real: 50 de 50 entradas trazem o campo.

`Profile.merge()` funde os dois. O RSS entra como `self` porque é mais rico;
o export como `other`. A identidade de uma entrada é **(título normalizado, ano,
data assistida)** — sem a data, rewatches seriam colapsados em um só.

## Armadilhas do export real

Descobertas abrindo um ZIP de verdade, não a documentação:

- **`deleted/` e `orphaned/` contêm arquivos de nome idêntico** — `deleted/diary.csv`,
  `orphaned/diary.csv`. São filmes removidos da conta. A leitura é por nome exato
  justamente por isso; trocar por busca por sufixo contaminaria o histórico. Há
  teste de regressão.
- **`profile.csv` contém e-mail, nome completo, localização e bio.** Nunca é
  lido. O nome de usuário vem do formulário, que o fluxo híbrido precisa de todo
  jeito para o RSS.
- **Listas (`lists/`) são ignoradas de propósito.** Já foram contadas e viravam
  um cartão no perfil; saíram por decisão de produto — o app não fala de
  listas. Se voltarem: usam outro formato (a linha `Letterboxd list export v7`
  vem *antes* do cabeçalho) e as apagadas ficam em `deleted/lists/`.
- **A mesma grafia varia entre arquivos do mesmo ZIP** — `WALL·E` no `diary.csv`
  e `WALL-E` no `ratings.csv`. Daí `normalize_title()`: casefold, remoção de
  acentos, colapso de pontuação.
- **`watched.csv` tem filmes que o `diary.csv` não tem**, sem data — típico de
  quem importou do IMDb. É a origem das entradas sem data, que são ~metade de um
  histórico real.

## Resolução título → TMDB

Só o export precisa disso. `TMDBClient.resolve()` tenta quatro degraus, nessa
ordem, e **desiste em vez de chutar** — atribuir o filme errado corrompe a
estatística em silêncio, o que é pior que um buraco visível:

1. título exato (ou `original_title`) + ano exato
2. título exato + ano com 1 de diferença (Letterboxd usa ano de produção, TMDB
   usa data de lançamento; divergem em filme de festival)
3. título exato, qualquer ano, o mais popular
4. `/alternative_titles` dos candidatos cujo ano bate

O degrau 4 existe por um caso concreto: *The Secret World of Arrietty* é
`Arrietty` no TMDB, com `original_title` em japonês. A busca **achava** o filme;
era a heurística que o rejeitava. Isso levou a taxa de 99,8% para **100%**
(472/472 num histórico real).

Metadados vêm com `append_to_response=credits,keywords` — uma requisição por
filme em vez de três.

## País: produção não é origem

`production_countries` traz **todo coprodutor**. The Dark Knight é GB e US;
2001, Blade Runner e boa parte de Kubrick e Nolan também. Num histórico real o
Reino Unido aparecia em 73 filmes, dos quais só 5 eram só britânicos. Média de
1,5 país por filme.

`origin_country` é o país de origem (US para todos esses), e é o que o mapa e
o cartão "Países diferentes" usam — com ele o Reino Unido caiu para 29.

O modelo de recomendação continua com os países de **produção**: foi com eles
que o AUC foi medido, e coprodução diz algo sobre o filme. Trocar ali exige
medir de novo.

O campo entrou no cache depois de existirem entradas sem ele. `get_film` trata
`origin_countries is None` (e `pessoas is None`, abaixo) como ausente, e o filme
é rebuscado uma vez — no **job**. O **caminho da página** passa
`aceitar_incompleto=True` (`get_film`, `get_films`, `enrich`): serve a entrada
fresca de esquema antigo como está, com o campo ausente, e **nunca rebusca**.
Antes, abrir um perfil processado antes de uma mudança de esquema rebuscava ~470
filmes dentro do request (~45 s, perto do teto de 100 s do Cloudflare). Entrada
**vencida** (180 dias) nunca é servida, nem com a flag. Regra para campos novos:
`None` = gravado antes do campo existir; o job completa; a página mostra "sem
dado" e pede para reprocessar.

## Pessoas: gênero, id e local de nascimento

Dos créditos (sem requisição extra), `FilmDetails.pessoas` guarda
`{nome: {"id", "genero"}}` para a direção e o elenco guardado (10 primeiros). Só
a direção tem consumidor hoje (gênero e local de nascimento); o do elenco fica
guardado porque vem dos créditos que já são pedidos. Gênero é a convenção do TMDB: 0 desconhecido, 1 feminino, 2 masculino, 3
não-binário; o 0 é uma fatia própria na interface, nunca descartada. `None`
marca entrada anterior ao campo; `{}`, um filme cujos créditos vieram sem ids.

O **local de nascimento** vem de `/person/{id}` (`place_of_birth`, texto livre ou
nulo) e fica numa tabela própria do cache (`people`, mesmo TTL de 180 dias).
Quem não tem local também é guardado: pedir de novo não adiantaria, e o 404
vira "sem local" pelo mesmo motivo. É buscado **só no job**
(`TMDBClient.get_pessoas`, etapa "buscando pessoas", pelo mesmo mecanismo de
concorrência e retry) e **nunca no carregamento da página**. Quem é buscado
(`stats.diretores_de_filmes`): **só a direção** (~365 requisições num histórico
de ~470 filmes). O elenco não é consultado: o local de nascimento dele não
aparece na interface, e um histórico real tem milhares de atores — uma
requisição por pessoa seria abuso. Falha nessa etapa não derruba o job (como a
watchlist); o que já foi buscado fica em cache.

`nascimento.pais_de_nascimento()` extrai o país: último trecho depois da
vírgula, unificando grafias num nome em português (USA/U.S./United States →
Estados Unidos; England/Scotland/UK → Reino Unido; West Germany → Alemanha...).
Um `[now X]` **no fim** do texto vale o X ("USSR [now Russia]" → Rússia, "USSR"
sozinho → União Soviética); no meio ele renomeia a cidade ("Ujiyamada [now Ise],
Mie, Japan" → Japão). O dicionário é pequeno (~85 grafias); o que não está nele
volta como veio, em inglês, em vez de sumir.

## Bugs que já aconteceram aqui

- `enrich()` deduplicava por filme mas só preenchia o `tmdb_id` da **primeira**
  referência. Entradas de rewatch ficavam sem metadados e sumiam de todas as
  estatísticas, em silêncio.
- `vote_average = 0.0` significa "ninguém votou", não "nota zero". Sem
  `vote_count`, filmes obscuros fabricavam as maiores discordâncias possíveis.
