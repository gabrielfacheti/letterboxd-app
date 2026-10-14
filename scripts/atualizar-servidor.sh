#!/usr/bin/env bash
# Atualiza a stack em produção: puxa o código, reconstrói e só declara sucesso
# depois que o app responde. Funciona chamado de qualquer pasta.
set -euo pipefail

STACK=/opt/stacks/letterboxd-analysis
cd "$STACK"

antes=$(git rev-parse --short HEAD)
# --ff-only: se houver commit feito direto no servidor, para em vez de mesclar.
git pull --ff-only
depois=$(git rev-parse --short HEAD)
echo "código: $antes -> $depois"

docker compose up -d --build

# O container sobe mesmo quando o app cai logo depois (ex.: data/ sem
# permissão); sem esperar a resposta, um loop de reinício passaria por sucesso.
for _ in $(seq 30); do
  if docker compose exec -T app python -c \
      "import urllib.request; urllib.request.urlopen('http://localhost:8000/', timeout=2)" \
      2>/dev/null; then
    # Só as imagens desta stack: um prune geral levaria as de outras stacks.
    docker image prune -f --filter label=com.docker.compose.project=letterboxd >/dev/null
    echo "no ar: https://letterboxdstats.party"
    exit 0
  fi
  sleep 1
done

echo "o app não respondeu em 30s; últimas linhas do log:" >&2
docker compose logs --tail 30 app >&2
exit 1
