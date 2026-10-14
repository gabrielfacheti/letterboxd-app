# letterboxd-analysis

Site que lê o histórico de um perfil do Letterboxd e devolve estatísticas e
recomendações. Projeto pessoal, roda num servidor caseiro.

Este arquivo é carregado automaticamente. As especificações detalhadas ficam em
`.claude/specs/` e devem ser lidas sob demanda — cada uma diz quando importa:

- @.claude/specs/dados.md — de onde vêm os dados, e as armadilhas do export real
- @.claude/specs/recomendacao.md — o modelo de gosto, as medições, o que falhou
- @.claude/specs/web.md — aplicação, jobs em segundo plano, design
- @.claude/specs/deploy.md — hospedagem, isolamento, o que falta
- @.claude/specs/pendencias.md — o que está em aberto

## Comandos

```bash
uv run pytest -q                                 # 234 testes, nenhum toca a rede
uv run uvicorn letterboxd.web.app:app --reload   # app em http://127.0.0.1:8000
uv run python scripts/avaliar_recomendacao.py data/letterboxd-*.zip   # mede o modelo
```

## Convenções

- **Comentários e docstrings em português**, explicando *por que*, não *o que*.
  Nomes de símbolos seguem o domínio: português quando o conceito é do projeto
  (`vistos_de`, `por_genero`), inglês quando vem de fora (`FilmDetails`, `rank`).
- **Mensagens de commit em inglês**, estilo conventional (`feat:`, `fix:`),
  explicando a razão da mudança e não só o conteúdo dela. Sem linhas de
  atribuição.
- **Polars, não pandas.** Menos memória e paralelismo real nos 4 núcleos do
  servidor.
- **Testes não acessam a rede.** O cliente HTTP é testado com `httpx.MockTransport`;
  há uma fixture recortada de um feed RSS real em `tests/fixtures/`.
- **Nunca commitar `.env`** nem o conteúdo de `data/`.

## Invariantes

Coisas que já quebraram ou que a interface promete ao usuário. Alterar qualquer
uma exige decisão consciente, não refatoração distraída:

1. **`profile.csv` nunca é lido.** No export real ele contém e-mail, nome
   completo, localização e bio. O ZIP é parseado em memória e descartado; nada
   dele vai para o disco. A página inicial promete isso por escrito, e há teste
   que falha se o aviso sumir.
2. **Entradas sem data entram nas estatísticas não-temporais e saem das
   temporais.** Metade de um histórico típico não tem data assistida. O filtro
   está isolado em `stats.dated()`, e `overview()` expõe `cobertura_temporal`
   para a interface poder avisar sobre quantos dados cada gráfico fala.
3. **Toda média de notas aparece com a amostra ao lado.** "4,50" pode ser uma
   única avaliação entre seis filmes. Rankings por nota exigem `min_avaliados`,
   que é diferente de `min_amostra` (filmes).
4. **O cache do TMDB vence em 180 dias.** É teto dos termos de uso deles. Não
   aumentar.
5. **Notas de filmes exibidas são do TMDB**, nunca rotuladas como do Letterboxd —
   essas não são acessíveis sem raspar o site.
6. **Um perfil só é alcançável pelo link secreto de quem o processou.** Nada
   lista perfis, e o endereço nunca deriva do nome de usuário, que é público.
   A página inicial promete isso, e há teste que falha se `/p/<usuário>` abrir.
