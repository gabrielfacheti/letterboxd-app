# Recomendação: o modelo, e por que não é o óbvio

Leia antes de mexer em `recommend.py`. O módulo tem um erro instrutivo no
histórico, e repeti-lo é fácil.

## A primeira versão falhou

Pesava cada traço de filme (diretor, keyword, país) pela **média das notas** que
a pessoa deu aos filmes que o têm. Medido contra a watchlist: **AUC 0,53** —
acaso. Uma grade de 24 configurações não salvou; o melhor resultado era o mais
apagado (`shrinkage=20`), sinal clássico de modelo sem sinal.

O erro era conceitual. A nota responde *"dado que assisti, gostei mais que minha
média?"*. Recomendação precisa de *"eu escolheria assistir isso?"*. Como ninguém
assiste ao acaso, a nota quase não varia — um histórico típico vive entre 3,5 e
4,5 — e o que sobra é ruído de decepção.

Houve também um erro de medição no caminho: a primeira validação comparou a
watchlist contra **candidatos gerados a partir dos favoritos do usuário**. Isso
não é grupo de controle. O controle tem que ser neutro.

## O modelo atual

Peso de cada traço é o **log-odds de presença** contra um corpus de fundo:

```
lift(traço) = log( P(traço | assistidos) / P(traço | fundo) )
```

Tarantino aparece em 7 dos 472 filmes do histórico e em 0 dos 758 do fundo:
distintivo. "Drama" aparece em metade dos dois: lift ≈ 0, **sem precisar de regra
especial contra gêneros genéricos**.

Score de um candidato é a **média** (não a soma) dos pesos dos seus traços.
Medido: média 0,835 · raiz 0,826 · soma 0,816. Somar premia acumulação de
keywords.

### Números medidos

| avaliação | AUC |
|---|---|
| 5-fold, assistidos retidos | **0,877** ± 0,015 |
| watchlist vs. fundo neutro | 0,835 |
| baseline: ano de lançamento | 0,724 |
| baseline: nota média do TMDB | 0,666 |
| baseline: popularidade | 0,603 |

Remover o traço de década derruba o AUC em apenas 0,014 — o trabalho é feito pelo
conteúdo, não pela recência.

Reproduzir: `uv run python scripts/avaliar_recomendacao.py data/letterboxd-*.zip`

### Decisões medidas, não escolhidas

- **`peso_nota = 0.0`.** Somar a afinidade de nota ao score baixou o AUC de 0,835
  para 0,816. A afinidade continua no perfil, mas serve às explicações.
- **IDF foi removido do modelo.** A versão por nota pesava traços por IDF sobre o
  próprio histórico, o que penaliza exatamente os traços que a pessoa mais
  persegue. Corrigir isso sozinho não mudou nada (0,485 → 0,481): o IDF era um
  defeito real, mas não era o que travava o modelo. O log-odds contra o fundo
  substituiu a ideia inteira — `TasteConfig` hoje tem apenas `alfa`, `peso_nota`,
  `shrinkage`, `min_ocorrencias_motivo` e `elenco_considerado`.
- **Positivos saem do fundo antes de medir.** Deixar a watchlist no corpus de
  fundo coloca positivos no grupo de negativos e subestima o AUC em ~0,065.

## Geração de candidatos

Sementes são os filmes melhor avaliados, **com teto de 2 por diretor**. Isso
existe por um erro concreto: ordenar por nota e cortar no top 25 escolhia 25
filmes empatados em 5,0 numa ordem arbitrária, que por acaso trouxe muita
animação infantil — e as recomendações saíram PAW Patrol e Gabby's Dollhouse para
alguém que gosta de Wong Kar-Wai e Kiarostami.

## Ressalvas honestas

- O corpus de fundo vem do `/discover` ordenado por popularidade. Representa
  "filmes que existem e alguém viu", não "todos os filmes". Um fundo mais
  obscuro deslocaria os pesos.
- O fundo é montado uma vez e serve a todos os perfis. Leva minutos; é um job.
