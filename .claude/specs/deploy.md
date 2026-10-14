# Hospedagem

Leia antes de mexer na stack ou em rede. **No ar em
`https://letterboxdstats.party` desde 2026-10-08**, servido do servidor de casa
por Cloudflare Tunnel.

## O princípio em jogo

O servidor segue a regra **"nada exposto à internet"**: nenhuma porta aberta no
roteador, e port forwarding não é opção. Um app público contraria isso.

## A decisão

**Opção A agora, D como destino.**

| | arranjo | veredito |
|---|---|---|
| A | tudo no servidor + Cloudflare Tunnel | escolhido — simples, sem timeout, custo zero |
| B | tudo em PaaS | atrita: filesystem efêmero mata o cache SQLite, teto de 300s mata o enriquecimento |
| C | front em PaaS, API no servidor | pior dos dois: dois deploys, CORS, e o servidor segue no caminho crítico |
| D | servidor como worker, app gerenciado na frente | melhor fim de jogo — o servidor só faz conexões de saída |

Cloudflare Tunnel mantém **"nenhuma porta aberta no roteador" literalmente
verdadeiro**: a conexão é sempre de dentro para fora.

A opção D é estritamente melhor no princípio de rede — zero inbound, o servidor
faz polling de uma fila. Vale migrar se "público" deixar de ser figura de
linguagem.

## Isolamento obrigatório da stack

Numa pasta própria de stack, ao lado das outras do servidor. O compose fixa
`name: letterboxd`, então os
containers são `letterboxd-app-1` e `letterboxd-cloudflared-1` qualquer que seja a pasta.

- rede Docker própria, **sem** encostar na rede das outras stacks
- sem socket do Docker, sem bind mount fora de `data/`
- container non-root, `cap_drop: ALL`, `no-new-privileges`, rootfs read-only
- volume só para `data/` (os três SQLite), no **SSD** — pequeno e muito acessado
- ⚠️ **`mem_limit` explícito.** Limites de memória do usuário no systemd **não
  cobrem containers** (eles rodam sob o daemon do Docker). Sem teto, uma stack
  nova pode derrubar os outros serviços da máquina. Está em 512 MiB; o pico
  medido em produção foi **137 MiB**
  (export real + recomendações).
- `cloudflared` como segundo serviço da mesma stack

## Um risco que vale saber

Se o servidor busca os RSS, quem bate no Letterboxd é o **IP residencial**. Levar
rate limit ali afeta a navegação pessoal, não só o app. O limitador por IP da
aplicação existe em parte por isso.

## Subir do zero

1. Cloudflare Zero Trust → Networks → Tunnels → criar túnel e copiar o token.
   **Não rodar o `cloudflared service install` que o painel sugere** — ver
   armadilhas abaixo. Em Public Hostname: domínio `letterboxdstats.party`,
   serviço `HTTP` → `app:8000` (o nome do serviço no compose, não `localhost`).
   É esse passo que cria o registro DNS; sem ele o domínio resolve para nada.
2. No servidor:
   ```bash
   git clone https://github.com/gabrielfacheti/letterboxd-app.git /opt/stacks/letterboxd-analysis
   cd /opt/stacks/letterboxd-analysis   # o caminho que scripts/atualizar-servidor.sh espera
   cp .env.example .env && chmod 600 .env    # preencher TMDB_KEY e TUNNEL_TOKEN
   scp <dev>:projetos/letterboxd/data/tmdb-cache.sqlite data/   # opcional
   sudo chown -R 10001:10001 data            # uid da imagem; depois de copiar
   docker compose up -d --build
   ```
3. Conferir: painel do túnel com **1 réplica**, e

   ```bash
   curl --doh-url https://1.1.1.1/dns-query -s -o /dev/null -w "%{http_code}\n" https://letterboxdstats.party/
   ```

**Clonar, não copiar a pasta de desenvolvimento.** O `data/` local contém o
export bruto (com o `profile.csv`) e os bancos de perfis e jobs do ambiente de
dev. No clone, `data/` vem vazio (só `.gitkeep`) e o app cria os bancos. A
única coisa que vale levar é o `tmdb-cache.sqlite`: traz o corpus de fundo
pronto e poupa minutos e centenas de requisições na primeira subida. O TTL de
180 dias continua valendo, porque cada entrada guarda quando foi buscada.

## Atualizar

```bash
/opt/stacks/letterboxd-analysis/scripts/atualizar-servidor.sh
```

Puxa com `--ff-only`, reconstrói, espera o app responder (até 30 s) e só então
apaga as imagens antigas **desta** stack. Se o app não sobe, mostra o log e sai
com erro. Não há rollback: corrigir e rodar de novo.

Não é automático de propósito. Um timer do systemd chamando o mesmo script
seria trivial e só usaria conexões de saída, mas hoje poria no ar todo push em
`develop` sem rodar testes, e exigiria credencial do GitHub guardada no
servidor. Se automatizar, que seja a partir de `main`.

## Armadilhas que já aconteceram

- **Cache copiado depois do `chown`.** O arquivo ficou com o uid 1000 e o app
  entrou em loop de reinício com `attempt to write a readonly database`. O app
  escreve no cache logo ao subir (cria tabelas novas, como `related`), então
  banco só-leitura não degrada: derruba. Sempre `chown -R` por último.
- **Duas réplicas no mesmo túnel.** O painel sugere `cloudflared service
  install`, que instala um `cloudflared.service` no host. Junto com o container,
  o túnel ficou com duas réplicas e o Cloudflare dividia as visitas entre elas:
  a do host não resolve `app` e devolvia 502 para parte dos acessos. Remover:
  ```bash
  sudo cloudflared service uninstall
  sudo systemctl disable --now cloudflared-update.timer
  sudo rm -f /etc/cloudflared/token
  ```
- **O DNS do próprio servidor engana.** O host resolvia por um DNS de VPN, que
  falhava em parte das consultas ao domínio recém-criado — `curl` dava `000`,
  parecendo o site fora. Visitantes não são
  afetados, nem o app (o Docker tem DNS próprio; 0 falhas em 60 consultas de
  dentro do container). Testar de fora com `--doh-url`, como acima.
- **`.gitignore` com `*.zip` no lugar de `data/`** deixava os `-wal`/`-shm` do
  SQLite rastreáveis. Hoje é `data/*` com exceção para `data/.gitkeep`.

## Rate limit atrás do túnel

O limitador lê `CF-Connecting-IP`, porque toda conexão chega vinda do
`cloudflared`. Isso só é seguro enquanto o app **não publicar porta**: um
`ports:` no compose deixaria qualquer um forjar o cabeçalho e escapar do limite.

O log do uvicorn sempre mostra o IP do `cloudflared` (`172.22.0.x`), não o do
visitante — ele não lê o cabeçalho. Não confunda com o que o limitador usa.

## Ainda não verificado

- Consumo de memória ao montar o corpus de fundo do zero. A subida em produção
  aproveitou o cache copiado, então esse job não rodou.
- Que o limitador, em produção, separa visitantes de fato. O teste automatizado
  cobre o cabeçalho; ninguém confirmou com dois IPs reais.
