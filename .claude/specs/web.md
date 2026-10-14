# Aplicação web

Leia antes de mexer em `web/`, `jobs.py` ou `store.py`.

## Forma

Processo único e persistente. Stores e cliente HTTP vivem no `lifespan` e são
compartilhados por todos os requests, em vez de reabertos a cada um. As rotas são
finas: todo o trabalho está em `pipeline`, `stats` e `recommend`, que não sabem
que existe um servidor web.

`app.state.recursos` é o ponto de costura: se já estiver preenchido quando o
`lifespan` sobe, ele não constrói nada e quem injetou é responsável por fechar. É
o que permite testar as rotas sem rede e sem `.env`.

## Jobs em segundo plano

Enriquecer um export leva ~40s e montar o corpus de fundo leva minutos. Nada
disso cabe num request.

**Sem Celery, RQ ou Redis.** O app é processo único; uma fila externa
acrescentaria um contêiner, um broker e uma classe inteira de falhas para
resolver o que `asyncio` já resolve. O estado vai para SQLite só para sobreviver
a reinícios.

O que **não** sobrevive é a execução, e daí o estado `INTERROMPIDO`: um job
marcado como rodando sem tarefa viva é resto de processo morto, e a tela precisa
dizer "tente de novo" em vez de girar para sempre. `marcar_interrompidos()` roda
na subida.

Duas regras de ordem que já quebraram:

1. **`runner.encerrar()` antes de fechar os stores.** As tarefas canceladas ainda
   gravam o estado final; banco fechado vira exceção não tratada no desligamento.
2. **Trabalho em lote vira tarefa agendada, não corrotina crua.** Esperar
   corrotinas deixava órfãs as que nunca começaram — `coroutine was never
   awaited` em todo reinício.

Progresso é estrangulado: só grava quando o percentual inteiro muda. Relatar cada
um de 472 filmes geraria centenas de escritas por job.

## Rotas

| rota | papel |
|---|---|
| `GET /` | formulário (usuário + export opcional); **não lista perfis** |
| `POST /processar` | agenda o job, redireciona para o progresso |
| `GET /jobs/{id}` | tela de progresso |
| `GET /jobs/{id}/estado` | JSON consultado a cada segundo |
| `GET /p/{slug}` | página do perfil, as 4 abas de uma vez |
| `GET /p/{slug}/itens/{dimensao}.json?valor=…` | sessões ou filmes de um valor de uma dimensão (coluna, linha, país); só cache |
| `GET /p/{slug}/ranking/{dimensao}.json` | ranking inteiro de gênero, idioma, tema ou duração, para a lista do diálogo; só cache |
| `GET /p/{slug}/recomendacoes.json` | resultado pronto, ou o job que o produz |
| `GET /p/{slug}/recomendacoes` | redireciona para `/p/{slug}#recommendations` |

Cada **trabalho devolve o caminho da página que produziu** — a rota não deduz
isso a partir do tipo do job.

**O `{slug}` é um segredo**, não o nome de usuário: `secrets.token_urlsafe(16)`,
novo a cada processamento. O nome de usuário é público, então `/p/<usuário>`
abria o perfil de qualquer um — e a página inicial ainda listava todos. Quem
processa recebe o link pelo redirecionamento do job, e a página pede para
guardá-lo. `<meta name="referrer" content="no-referrer">` impede que ele vaze
no `Referer` para as CDNs de imagem do TMDB. Reprocessar gera link novo; a
versão anterior fica órfã no banco (ver pendências).

Recomendações são calculadas em job, nunca no request: são até 40 consultas de
relacionados mais centenas de metadados. Resultado guardado responde em ~21ms.
Recarregar cai no job já em voo (`JobStore.ativo`), não dispara um segundo.
Como cada processamento ganha slug novo, um perfil reprocessado nasce sem
recomendações — não há o que invalidar.

## Proteções para exposição pública

- teto de upload, validado antes de ler o corpo
- proteção contra zip-bomb (tamanho descomprimido e taxa de compressão)
- `validate_username` barra path traversal antes de montar qualquer URL
- rate limit de 5 pedidos por minuto por IP, em memória — para o servidor não
  virar proxy de flood contra o Letterboxd

## Design

Baseado num Figma do autor. Acento laranja, cartão de número com legenda
embaixo (rótulo em monoespaçada caixa alta), badge de posição nos rankings.

**Cabeçalho de cartão.** Todo cartão de painel único (Overview, Deepdive, People)
tem o título *dentro* dele: um ícone numa caixa de 32 px azul Letterboxd
(`--lb-azul`, traço e fundo translúcido do mesmo azul), o título em negrito (não mono, não caixa alta) e, se houver, um subtítulo
em `--suave`; o que ficava ao lado do título (o botão "Expandir" do mapa) vai
para a direita. Tudo por **uma macro**, `cartao_secao(titulo, icone, subtitulo,
extra, classe)`, usada com `{% call %}`; não se repete essa marcação em cartão
nenhum. O título continua sendo um `<h2>` de verdade (outline e leitor de tela
intactos). Ficam como estavam: os cartões de número (`cartao`), o painel do
mapa em tela cheia (que tem o próprio cabeçalho no `<dialog>`) e os títulos de
seção que agrupam vários cartões (Recomendações), que seguem como rótulo mono
cinza.

**Ícones: sprite único.** Linhas de 24x24 desenhadas à mão, definidas uma vez
como `<symbol id="i-…">` no começo do `<body>` de `base.html` e usadas por
`<svg class="icone"><use href="#i-…"/></svg>` (traço e cor vêm do CSS,
`currentColor`). Sem biblioteca e sem requisição externa; se algum dia entrar
path copiado de um conjunto de ícones, só com licença permissiva e o aviso dela
em comentário junto ao sprite. Não deixar símbolo sem uso: há teste que confere
que cada ícone usado existe e que cada símbolo é usado. Mapeamento: notas →
estrela; décadas → barras; atividade anual e mensal → calendário; dia da semana
e duração → relógio; recentes → olho; melhor avaliados → estrela; gêneros →
etiqueta; idiomas → balão; mapa → globo; popular ou garimpo → bússola; temas →
hashtag; revisitas → repetir; discordâncias → setas; direção e elenco → usuário; gênero da direção →
pizza; países de nascimento → pino.

**Monoespaçada do sistema, não webfont** — zero requisição externa, coerente com
o princípio de rede do servidor.

Imagens (pôsteres e retratos) vêm das CDNs do TMDB direto para o navegador do
visitante. O servidor nunca serve imagem; o que sai dele é HTML, JSON e **um**
arquivo estático: a biblioteca de exportar cartões (abaixo), montada em `/static`
(`StaticFiles`, sem listagem de diretório, sem dependência nova).

**Exportar cartão como imagem.** Todo cartão de `cartao_secao` tem, no lado direito
do cabeçalho, um botão de compartilhar (ícone `i-compartilhar`) que abre um menu
único da página (popover: Esc e clique fora fecham, setas navegam, o foco volta ao
botão): "Baixar PNG", "Copiar imagem" e "Compartilhar…". O último só aparece onde
`navigator.canShare({files})` é verdadeiro. **Instagram não tem API para
publicar daqui**: no celular, "Compartilhar…" abre o menu do sistema, onde o
Instagram aparece; no desktop restam baixar e copiar, e a interface não promete
mais que isso. O resultado (copiou, falhou) vem num aviso `aria-live`.
- **Biblioteca vendorada:** html-to-image 1.11.11 (MIT), UMD, em
  `static/vendor/html-to-image.js`, com a licença e o sha256 ao lado
  (`LICENSE-html-to-image`, `LEIA-ME.txt`; teste confere o hash). Só é baixada
  quando alguém abre o menu de um cartão (o script a injeta uma vez); página
  normal não a carrega. O site continua sem buscar nada de terceiros.
- **Largura fixa, igual do celular e do desktop:** o clone sai sempre com a largura
  do cartão no layout do desktop (840 px úteis, ou uma de duas colunas da `.duas`),
  calculada das medidas do CSS (`main.envelope`, `columnGap`), nunca do cartão vivo.
  As regras de tela estreita (`@media`) olham a janela, então `.exporta` as desfaz
  (grade de filmes em 3 colunas, números da linha de pessoa ao lado do nome). Dentro
  do clone o texto cortado **quebra linha** (`overflow-wrap: anywhere`) e `td.nome`
  perde o `max-width: 1px`: esse truque só funciona com linha única, e com quebra
  espremia a coluna do nome, que passava por cima da barra (bug visto no celular).
  Animações ficam desligadas na imagem: a linha de pessoa recém-aberta começava
  transparente e saía em branco.
- **Como a imagem sai:** o cartão é clonado para um contêiner fora da tela (o vivo
  não muda), sem o botão de compartilhar, o "Expandir" e o "Mostrar mais/menos";
  texto cortado aparece inteiro; o ícone do cabeçalho tem seu `<use>` trocado
  pelos traços do símbolo (a imagem isolada não enxerga o sprite). Fundo da
  página, margem e um rodapé `@usuario · letterboxdstats.party`, PNG em
  pixelRatio 2. A geração começa ao abrir o menu, para o gesto do usuário ainda
  valer quando o navegador exigir (copiar e compartilhar, no Safari sobretudo); a
  cópia entrega a *promessa* da imagem ao `ClipboardItem`.
- **Pôsteres:** o TMDB manda `Access-Control-Allow-Origin: *`, mas o navegador já
  guardou os pôsteres no cache pelas `<img>` comuns (sem CORS), e um `fetch` com
  CORS que reaproveita essa cópia falha. Por isso a exportação busca com
  `cache: 'reload'`.
- **Invariante 6:** o nome do arquivo (`letterboxd-stats-<título-do-cartão>.png`) e
  o rodapé usam só o nome de usuário, que é público. O código de exportar nunca
  toca o slug nem a URL; há teste que falha se tocar.

Três desvios conscientes do Figma, todos por honestidade com os dados:

| Figma | Implementado | Por quê |
|---|---|---|
| "★ 4.6 média letterboxd" | "média tmdb" | não temos notas do Letterboxd |
| "214 do objetivo de 250" | "214 com data registrada" | não existe conceito de meta |
| "sequência atual · 23 dias" | "países diferentes · 49" | sequência exige dados diários completos |

**Mapa dos países (de origem).** Choropleth em SVG renderizado no servidor (`web/mapa.py`),
com contornos do Natural Earth 110m pré-projetados em `web/mundo.json` (52 KB,
gerado uma vez por `scripts/gerar_mapa.py`; nada é buscado de fora). Cor em
escala **logarítmica**: os EUA costumam ter mais da metade dos filmes, e na
linear o resto do mapa ficava quase cinza. O que não tem contorno — países
extintos (`SU`), regiões (Irlanda do Norte), territórios pequenos (Hong Kong) —
é listado abaixo do mapa em vez de sumir.

**Mapa em tela cheia.** O botão "Expandir" do cartão abre um `<dialog>`
(`showModal`; Esc, botão "Fechar" e clique no fundo fecham, e o foco volta ao
botão). O SVG **não é repetido no HTML**: o script clona o do cartão ao abrir. Zoom e
arrasto mexem no `viewBox` (sem biblioteca): roda de mouse em torno do cursor,
arrastar, pinça, e os botões +/−/Reiniciar para teclado. Zoom preso entre 1x e
8x, e o recorte nunca sai do mapa; `vector-effect: non-scaling-stroke` mantém
as bordas finas ao aproximar. A dica de país funciona também lá dentro. Ao lado
(abaixo, no celular), o painel "Cinema pelo mundo" (`mapa.painel`): países
explorados (o mesmo número do cartão "Países diferentes"), o rótulo de explorador,
o país mais visto e o top 10 com ISO, nome, barra e "322 (69%)" (absoluto
primeiro, porcentagem das entradas), mais "+N países". Nome em português vindo do
`mundo.json`; sem contorno, o do TMDB em inglês, como na nota do mapa. Os
patamares do explorador (`mapa.EXPLORADORES`): até 10 "Fica em casa", 11–25
"Turista de fim de semana", 26–50 "Viajante", 51 ou mais "Desbravador". Não há
"índice de diversidade": não foi pedido.

**Escolher um país (só no mapa em tela cheia).** Clicar num contorno, ou numa
linha do top 10 do painel, lista os filmes daquele país de origem sob o mapa
(`GET /p/{slug}/itens/pais.json?valor=BR`). As linhas do painel são botões
(`aria-pressed`): são o caminho para países sem contorno (Hong Kong, União
Soviética) e para o teclado. Arrastar para mover o mapa **não** conta como
clique (só se o ponteiro andou menos de 4 px, e nunca em pinça). O escolhido ganha
contorno azul (movido para o fim do SVG, para não ficar sob os vizinhos); a seção
tem o título "<País> · N filmes" (`aria-live`, recebe o foco quando a escolha
vem do teclado), o botão "Limpar" e a grade com o mesmo cartão `.filme` das
pessoas, montada com `textContent`. País com 0 filmes mostra "Nenhum filme em X"
sem buscar; as respostas ficam num `Map` e escolher de novo não refaz o pedido.
A rota (a mesma dos gráficos expandidos, abaixo) valida o valor antes de qualquer
coisa e fica sob o slug secreto (invariante 6). Os filmes são distintos (rewatch pela média, "média de N"),
da melhor nota para a pior, sem nota por último; o N do título conta filmes, e
o número da lista do painel conta entradas.

**Gráficos expandidos (Overview e Deepdive).** Os cartões de Distribuição de notas,
Atividade anual/mensal/por dia da semana, Filmes por década, Gêneros, Idiomas,
Temas e Duração têm um botão de expandir (ícone de quatro setas, à esquerda do
de compartilhar; o do mapa é o mesmo botão e abre o diálogo do mapa). Eles abrem
**um `<dialog>` só**, reaproveitado (showModal; Esc, "Fechar" e clique no fundo
fecham, o foco volta ao botão; tela cheia no celular). **Sem compartilhar no
diálogo**: o botão só existe no cartão, e o código de exportar nunca olha para o
diálogo. Colunas: o gráfico do cartão é clonado e cada coluna vira botão
(`role=button`, `aria-pressed`, Enter/Espaço). Listas: o ranking *inteiro* vem de
`/ranking/{dimensao}.json` ao abrir (o cartão mostra só 10 ou 15; temas só com
`MIN_AMOSTRA` filmes, senão seriam milhares) numa lista que rola, cada linha um
botão. Escolher mostra "<valor> · N sessões" ou "N filmes" (`aria-live`, foco no
título quando vem do teclado), "Limpar", a grade `.filme`, carregando/erro, tudo
memorizado num `Map` e com resposta velha ignorada; é a mesma seção do mapa
(`criarSelecao`). **Sessões x filmes:** as dimensões temporais (`ano`, `mes`,
`dia`) devolvem *sessões* — uma por entrada com data (invariante 2), a mais nova
primeiro, com a data e a nota daquela entrada —, então a conta bate com a altura
da coluna; as demais devolvem filmes distintos com a nota média e "média de N",
da melhor nota para a pior, sem nota por último. Na nota, o filme entra se
qualquer sessão tem aquela nota (a média pode ser outra). Dimensões (lista
fechada; o resto é 404): `nota` (0,5 a 5 em meias), `ano`, `mes` (`AAAA-MM`),
`dia` (1 a 7), `decada`, `genero`, `idioma` (código ISO, nome em português),
`tema`, `duracao` (`curtos`, `padrao`, `longos`, `epicos`), `pais` (ISO).
`stats.analisar_dimensao` valida e converte o valor *antes* de qualquer carga
(números em faixa, chave conhecida, texto livre limitado); o valor vai em query
porque palavra-chave pode ter "/".

**Barras com porcentagem medem a porcentagem.** Em rankings com `pct`, a barra
vai até `pct`%, não até a distância do primeiro colocado — antes, 54% enchia a
barra inteira. O número ao lado traz a contagem absoluta primeiro: `255 (54%)`.

**Overview e Deepdive.** A Overview abre com três fileiras de cartões de número
(a última: filmes por semana, dia mais ativo, filmes diferentes, duração média),
a distribuição de notas, três gráficos de atividade em colunas, um por linha e
em largura total (`colunas`: pico em laranja, faixa de hover, foco de teclado) —
"Atividade anual" (anos vazios do meio incluídos), "Atividade mensal · ano" e
"Atividade por dia da semana" (seg a dom, os sete sempre presentes) — e dois
cartões de filmes lado a lado:
"Assistidos recentemente" (5 últimas *entradas* com data) e "Mais bem avaliados"
(5 filmes distintos; rewatch entra pela nota média, com a amostra ao lado;
empate resolve por data mais recente, sem data por último, depois título). Mapa,
gêneros e idiomas moram no Deepdive, logo depois de "Filmes por década".
**Invariante 2 nos gráficos de atividade:** são temporais (só entradas com data),
e um aviso único com `N entradas com data registrada (X%)` fica logo antes do
primeiro deles. O Deepdive não tem mais nenhuma seção temporal (década, mapa,
gêneros, idiomas, temas e discordâncias valem para o histórico todo), então
perdeu o aviso. O radar "Perfil de gêneros" foi removido: repetia "Gêneros
favoritos".

**Deepdive, em ordem:** "Filmes por década"; "Gêneros favoritos" e "Idiomas
originais" lado a lado, cada um com duas caixinhas no topo (`explorados`:
quantos distintos, e o menos assistido com "N entradas", empate pelo nome); o
mapa dos países; "Popular ou garimpo?" e "Duração dos filmes" lado a lado
(`.duas.iguais`); "Revisitas"; "Temas recorrentes"; "Onde você discorda do público".

**Revisitas.** Filme *revisto* = 2 ou mais sessões (entradas) com o mesmo `tmdb_id` **ou**
alguma entrada com a marca `rewatch` do diário (preenchida pelo export e pelo RSS); quem
marca vê o filme mesmo com uma sessão só, "×1" no pôster (o número é o de sessões
registradas). Entrada sem `tmdb_id` não se identifica e fica de fora. Uma *sessão de
revisita* é a marcada `rewatch` ou qualquer uma que não seja a mais antiga do filme (as sem
data vêm depois das datadas; se todas são sem data, só uma escapa). `stats.revisitas` devolve
dados puros: "Filmes revistos", "Diretores revistos" (distintos, entre os filmes revistos;
cada diretor de um filme conta) e "Tempo revendo" (duração × sessões de revisita, duração
desconhecida = 0), nas caixinhas grandes e centralizadas de sempre. Dois destaques lado a
lado no desktop, empilhados no celular: o **filme mais revisto** (mais sessões; empate: maior
nota média — sem nota perde de com nota —, depois sessão mais recente, depois título), com
pôster, "N sessões", a média com a amostra ("2/2 avaliadas", invariante 3) e a linha do
tempo das sessões (datadas do mais antigo ao mais novo, "sem data" no fim, cada uma com a
nota daquela sessão); e o **diretor mais revisto** (mais filmes revistos distintos; empate:
mais sessões de revisita, depois nome), com a foto do TMDB, "N filmes revistos" e os títulos
ligados ao TMDB. Os gêneros dos revistos vão numa barra só (cada filme uma vez por gênero;
5 gêneros e "Outros"; cores `--rev-*`), com a legenda trazendo os números. A grade mostra os
6 primeiros (na mesma ordem do destaque), com "×N" no pôster e o título (que corta com a
dica); o resto fica atrás do botão "Mostrar todos (N)", o mesmo `.ver-mais` dos rankings.
Sem revistos: "Nenhum filme revisto ainda.". Não tem botão de expandir (não é gráfico
categórico), mas exporta como imagem como os outros cartões.

**Idiomas pelo nome.** O TMDB entrega ISO 639-1 ("ka", e o não padrão "cn" para
cantonês); `idiomas.nome_do_idioma` traduz com uma tabela de ~70 códigos e cai
no próprio código quando não conhece. `por_idioma` já devolve o nome, então a
lista e as caixinhas falam a mesma língua.

**Popular ou garimpo?** Fama de um filme = votos no TMDB (`vote_count`), não a
`popularity` do TMDB, que mede o que está em alta e chamaria um clássico dos anos
70 de obscuro. Escala logarítmica entre um piso e um teto
(`VOTOS_PISO` = 100, `VOTOS_TETO` = 30000): `f = clamp((log10(votos) − log10(100)) /
(log10(30000) − log10(100)), 0, 1)`. O piso existe porque quase nenhum filme
registrado tem menos de ~100 votos; com a curva começando em 0, um filme de arte
como Taste of Cherry (741 votos) já dava 0,67 e quase todo mundo caía em
"Arrasa-quarteirão". Pontuação =
`round(100 × média de f)` sobre filmes distintos com metadados; até 100 votos vale f = 0
(obscuro de verdade). Perfis (`stats.PERFIS_DE_FAMA`, uma tabela só): 0–44
Garimpeiro, 45–64 Equilibrado, 65–79 Segue o público, 80–100
Arrasa-quarteirão. A interface diz o que mede ("pelo número de votos no TMDB de
cada um dos N filmes", invariante 5). "Seus achados": até 2 filmes com nota média
(rewatch pela média, "média de N") de 4,0 ou mais, do menos votado para o mais,
desempate pela nota maior; some se nenhum chega a 4,0.

**Duração dos filmes.** Média sobre filmes distintos com duração > 0, a mesma
base e o mesmo número do cartão "Duração média" da visão geral
(`_duracoes_conhecidas`). Faixas, limite superior inclusive: Curtos até 89 min,
Padrão 90–120, Longos 121–150, Épicos 151 ou mais (`FAIXAS_DE_DURACAO`), com
"N (P%)" sobre os filmes de duração conhecida. O mais curto e o mais longo
levam ao TMDB; empate pelo título. A duração se escreve "1h 50min", e "7min"
abaixo de uma hora.

**Onde você discorda do público** (Deepdive) usa a linha `.filme` com pôster,
título ligado ao TMDB, ano e, à direita, sua nota (★ verde), a média "tmdb" na
escala de 5 (invariante 5) e a diferença (laranja se você deu mais, azul se
menos). Só filmes com 50 votos ou mais; `stats.discordancias` já traz `tmdb_id`
e `poster`.
"Filmes por semana" e "Dia mais ativo" são temporais e dizem sobre quantas
entradas falam; a janela nunca é menor que uma semana. "Filmes diferentes" e
"Duração média" contam filmes distintos.

**People conta filmes, não entradas.** `_ranking(..., por_filme=True)` colapsa
as entradas de um mesmo `tmdb_id` antes de explodir as listas, com a média das
notas do filme. Sem isso, rever um filme 8 vezes dava ao diretor "8 filmes".
Gêneros, idiomas, países e keywords continuam por entrada, de propósito.

**People tem duas sub-abas, Diretores e Elenco** (tablist como as abas
principais, alternância no cliente). Cada uma tem dois cartões de ranking com o
mesmo tratamento, sem destaque para o #1: "mais assistido/frequente" (filmes
distintos, empate pelo nome) e "melhor avaliado" (nota média, exigindo
`MIN_AMOSTRA` avaliações — invariante 3). Cada cartão mostra os 5 primeiros e um
botão ("Mostrar mais"/"Mostrar menos", `aria-expanded` + `aria-controls`) sempre
no fim do cartão revela até o 10º. Cada linha é um
`<details>`/`<summary>` com três números rotulados (filmes, horas, nota média com
a amostra "6/7 avaliados"; no celular descem para uma linha própria) e, ao abrir,
a grade de filmes da pessoa: cartões `.filme` sem data nem gêneros, ordenados por
ano e título, ligados ao TMDB como os da Overview (pôster e título abrem em nova
aba). **Só Diretores** tem, no fim, a pizza de gênero e os países de nascimento
(mesma altura, lado a lado, `.duas.iguais`). `stats.aba_de_pessoas` monta tudo.
Detalhes que já quebraram ou que importam:

- **Horas contam as entradas** (rever é tempo gasto); filmes e nota contam
  filmes distintos (rewatch pela média, "média de N").
- **Pizza**: cada diretor conta uma vez (pelo id do TMDB, não pelo nome). SVG com
  `pathLength="100"` calculado no servidor; o destaque de hover da legenda usa
  `:has()`. Cores: feminino verde e masculino azul do Letterboxd, não-binário no
  acento, desconhecido cinza (`--pizza-0..3`).
- **Países de nascimento**: porcentagem sobre quem tem país conhecido, mais uma
  linha com quantos não têm. Perfil processado antes do dado existir mostra
  "processe o perfil de novo" em vez de gráfico vazio; a página nunca vai ao
  TMDB por isso.

**Gráfico de colunas no celular.** Cada coluna tem largura mínima (`--c`, caracteres do
maior rótulo, x `--ch`); sobrando espaço elas esticam como no desktop, faltando o
gráfico rola na horizontal *dentro* do cartão (`.colunas-rolagem`, barra fina,
`overscroll-behavior-x: contain`), sem rolar a página. Como o contêiner que rola
cortaria uma dica posicionada em CSS, a dica da coluna (`data-dica="nome|valor"`)
usa o mesmo popover único do texto cortado; foco de teclado numa coluna a traz à
vista e a dica acompanha a rolagem. Na imagem exportada não há rolagem: as colunas
encolhem para caber.

**Celular (360 px).** Grades de filmes usam `minmax(0, 1fr)` (o `1fr` puro alargava a
coluna até o título). A fileira de abas principais rola: a ativa vem para o meio ao
selecionar e ao abrir (`#recommendations`), e a borda direita esmaece só enquanto houver
aba escondida. Rótulos mono pequenos (`.rotulo`, `.amostra`, `.eixo`, `.posicao`) sobem
para .72rem em até 560 px (a imagem exportada os mantém no tamanho do desktop), a
legenda do mapa quebra "escala logarítmica" para a linha de baixo, e o `summary` da
home tem 33 px de altura para o toque.

**Texto cortado: dica, não quebra de linha.** Quem corta com reticências usa a
classe `.corta` (títulos de filme, nomes em linhas de ranking e em tabelas) e o
texto fica cortado sempre, sem reflow. Um único elemento flutuante
(`#dica-corta`) e uma única escuta delegada na página mostram o texto inteiro
quando o mouse ou o foco está num `.corta` cujo `scrollWidth > clientWidth`;
nada aparece se o texto cabe. Visual igual ao da dica do mapa, posicionado junto
ao elemento e mantido dentro da janela. É um *popover* (camada de topo) para
aparecer também sobre o `<dialog>`, e é `aria-hidden`: o leitor de tela já lê o
texto completo do DOM.

**Cores do Letterboxd.** `--lb-verde` (#00e055) pinta as estrelas (`.estrela`);
`--lb-azul` (#40bcf4) pinta a aba ativa, principal e sub-aba, e é também o
`--link` (links, logo, fatia Masculino): um azul só. O resto segue no acento
laranja.

Hover em colunas, linhas de ranking e países. Tudo também
responde a foco de teclado e desliga sob `prefers-reduced-motion`.

As quatro abas vivem numa página só e alternam no cliente; a aba fica no hash da
URL, então `/p/<slug>#people` abre direto na aba. Só a de recomendações carrega
sob demanda — quem quer estatísticas não deve disparar 15s de trabalho sem pedir.
