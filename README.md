# PriceTracker Discord Bot (v1.1)

Aplicação para Discord voltada à consulta, comparação global e monitoramento automatizado de preços de jogos digitais nas plataformas Steam e Nintendo eShop com suporte multi-região (Brasil, Estados Unidos, Portugal/Europa, Reino Unido, Japão, Canadá, Argentina), cache LRU/TTL em memória, alertas em DM, auto-recuperação no boot e backup a quente para provedores com disco efêmero (Koyeb, Render, Railway, Fly.io).

---

## 1. Descrição do Projeto

O PriceTracker é um bot para Discord que realiza consultas de preços, comparativos simultâneos entre múltiplos países, rastreamento de histórico de valores e alertas customizados de promoções para jogos da Steam e da Nintendo eShop. As respostas aos usuários utilizam estrutura Multi-Embed com metadados da loja e gráficos de histórico gerados sob demanda na moeda local com gestão rigorosa de memória RAM (< 120 MB).

---

## 2. Arquitetura e Tecnologias

### Componentes Principais

* **Linguagem:** Python 3.11+
* **discord.py (v2.x):** Interface com a Discord API, gerenciamento de Slash Commands (`app_commands`), componentes de UI interativos (`discord.ui.View`, `Button`, `Select`) e tarefas em background (`tasks.loop`).
* **httpx:** Cliente HTTP assíncrono com connection pooling persistente, backoff exponencial e jitter em respostas com status `429` ou `5xx`.
* **aiosqlite / sqlite3:** Camada de persistência assíncrona sobre SQLite operando em modo WAL com isolamento de histórico por região (`country_code`), suporte a alertas em DM (`is_dm`) e módulo de backup a quente via SQLite Online Backup API.
* **matplotlib:** Geração sob demanda de gráficos de tendência de preços em formato PNG renderizados diretamente em memória (`io.BytesIO`) com backend headless (`Agg`), formatação monetária nativa no Eixo Y e execução isolada via `asyncio.to_thread`.
* **InMemoryTTLCache:** Cache leve LRU com TTL de 10 minutos baseado em `collections.OrderedDict` para autocomplete em tempo real, reduzindo em até 80% as chamadas externas.

### Comportamento dos Workers em Segundo Plano

1. **Worker de Preços (a cada 4 horas):**
   * Varredura unificada por combinação única de `(platform, game_id, country_code)` para evitar requisições redundantes às APIs externas.
   * Intervalo de 1 segundo entre requisições consecutivas para mitigar riscos de rate limiting.
   * Envio de notificação no canal cadastrado ou via Mensagem Direta (DM) conforme configuração (`is_dm`), com fallback gracioso para o canal caso a DM do usuário esteja fechada (`discord.Forbidden`).
   * Mecanismo de auto-purge: remoção automática de registros de monitoramento associados a canais deletados ou sem permissão de envio (`discord.NotFound`, `discord.Forbidden`).

2. **Worker de Backup a Quente (a cada 24 horas):**
   * Consolida transações do SQLite (`-wal` e `-shm`) em um snapshot íntegro em memória RAM via `sqlite3.backup()`.
   * Envia o snapshot consolidado como anexo `.db` para o canal privado configurado em `BACKUP_CHANNEL_ID`.
   * Realiza a limpeza de mensagens de backup anteriores enviadas pelo bot no canal.

---

## 3. Pré-requisitos e Variáveis de Ambiente

### Variáveis de Ambiente (`.env`)

| Variável | Tipo | Obrigatória | Padrão | Descrição |
| :--- | :--- | :---: | :--- | :--- |
| `DISCORD_TOKEN` | String | Sim | — | Token de autenticação do bot gerado no Discord Developer Portal. |
| `DB_PATH` | String | Não | `bot_database.db` | Caminho do arquivo de banco de dados SQLite local ou volume montado (ex: `/data/bot_database.db`). |
| `BACKUP_CHANNEL_ID` | String / Int | Não | — | ID de canal de texto privado no Discord para envio de backups diários e auto-recuperação no boot. |
| `ITAD_API_KEY` | String | Não | — | Chave de API opcional do IsThereAnyDeal para enriquecer consultas da Steam com o Menor Preço Histórico Real (All-Time Low). |

### Permissões no Discord Developer Portal

No painel de OAuth2 URL Generator do Discord Developer Portal, selecione os escopos `bot` e `applications.commands` com as seguintes permissões:

* `Send Messages` (Enviar Mensagens)
* `Embed Links` (Inserir Links)
* `Attach Files` (Anexar Arquivos)
* `Read Message History` (Ver Histórico de Mensagens)
* `Use Slash Commands` (Usar Comandos de Barra)

---

## 4. Slash Commands (v1.1)

| Comando | Argumentos | Comportamento |
| :--- | :--- | :--- |
| `/steam` | `jogo` (String, obrigatório), `regiao` (Choice, opcional, padrão: `BR`) | Consulta o jogo na Steam API na região indicada (BR, US, PT, GB, JP, CA, AR) com autocomplete via cache TTL. Exibe embed estilizado (com destaque dourado para 100% OFF) e botão de monitoramento direto. Se houver $\ge 2$ registros na região, anexa gráfico de tendência. |
| `/eshop` | `jogo` (String, obrigatório), `regiao` (Choice, opcional, padrão: `BR`) | Consulta o jogo na API da Nintendo eShop oficial do país indicado (com roteamento Algolia/Solr e fallbacks). Exibe valores monetários locais e botão de monitoramento. Se houver $\ge 2$ registros na região, anexa gráfico de tendência. |
| `/comparar` | `plataforma` (Literal: `Steam`, `eShop`), `jogo` (String, obrigatório) | Dispara 4 consultas simultâneas em paralelo via `asyncio.gather` para **Brasil (BR)**, **Estados Unidos (US)**, **Portugal/Europa (PT)** e **Japão (JP)**, gerando uma matriz comparativa única com preços oficiais e badges de desconto. |
| `/historico` | `plataforma` (Literal: `Steam`, `eShop`), `jogo` (String, obrigatório), `regiao` (Choice, opcional, padrão: `BR`) | Retorna a análise temporal detalhada e o gráfico de variação de preço para o título especificado na região selecionada. |
| `/monitorar` | `plataforma` (Literal), `jogo` (String), `preco_alvo` (Float, opcional), `regiao` (Choice, opcional), `privado` (Bool, opcional) | Registra alerta personalizado. Se `privado=True`, o alerta é entregue diretamente via DM com fallback automático para o canal caso a DM esteja fechada. |
| `/listar` | Nenhum | Exibe a lista paginada de todos os monitoramentos ativos cadastrados pelo usuário no canal atual (com indicação de bandeira, moeda e badge de DM). |
| `/remover` | Nenhum | Apresenta um menu suspenso (`Select Menu`) contendo os jogos rastreados pelo usuário no canal para exclusão imediata do alerta. |
| `/gratis` | Nenhum | Rastreador de Giveaways e promoções temporárias de 100% OFF (*Free to Keep*) na Steam. Exibe valor original riscado, data de término do resgate e botões de link direto para adicionar à biblioteca. |
| `/status` | Nenhum | Painel de telemetria e diagnóstico exibindo latência do WebSocket, tempo de atividade (*Uptime*), pico de consumo de memória RAM do processo e contadores do SQLite. |

---

## 5. Execução e Deploy

### 5.1. Execução Local

```bash
# 1. Clonar o repositório e acessar o diretório
git clone <url-do-repositorio>
cd PriceTracker

# 2. Criar e ativar o ambiente virtual
python -m venv .venv
# No Linux/macOS:
source .venv/bin/activate
# No Windows (PowerShell):
.venv\Scripts\Activate.ps1

# 3. Instalar dependências
pip install -r requirements.txt

# 4. Configurar as variáveis de ambiente
cp .env.example .env
# Edite o arquivo .env inserindo o DISCORD_TOKEN e opcionalmente o BACKUP_CHANNEL_ID

# 5. Executar a aplicação
python bot.py
```

### 5.2. Execução via Docker

```bash
# 1. Construir a imagem Docker
docker build -t pricetracker-bot .

# 2. Executar o container com passagem de variáveis de ambiente
docker run -d \
  --name pricetracker \
  --env-file .env \
  --restart unless-stopped \
  -v $(pwd)/data:/app/data \
  -e DB_PATH=/app/data/bot_database.db \
  pricetracker-bot
```

### 5.3. Execução em Provedores com Disco Efêmero (Zero-Cloud Cost)

Em plataformas como **Koyeb, Render, Railway e Fly.io** (onde o filesystem é reinicializado a cada restart/deploy):
1. Crie um canal de texto privado no seu servidor do Discord reservado para backups.
2. Defina a variável de ambiente `BACKUP_CHANNEL_ID=<ID_DO_CANAL>`.
3. Ao reiniciar, o bot detectará a ausência do arquivo local de banco e fará o download automático do snapshot `.db` mais recente antes de disponibilizar os comandos.

---

## 6. Persistência de Dados

O banco de dados SQLite é inicializado em modo WAL (Write-Ahead Logging) com isolamento estrito de preços e moedas por região.

### Configurações de Conexão (PRAGMA)

* `PRAGMA journal_mode = WAL;`
* `PRAGMA busy_timeout = 5000;`
* `PRAGMA synchronous = NORMAL;`

### Esquema Relacional

#### Tabela `tracked_games`
Armazena as inscrições ativas de monitoramento vinculadas a usuários, canais, territórios e modalidade de entrega (canal vs DM).

```sql
CREATE TABLE IF NOT EXISTS tracked_games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    game_id TEXT NOT NULL,
    game_title TEXT NOT NULL,
    target_price REAL,
    last_price REAL,
    currency TEXT DEFAULT 'BRL',
    country_code TEXT DEFAULT 'BR',
    notify_on_any_sale INTEGER DEFAULT 1,
    is_dm INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(channel_id, user_id, platform, game_id, country_code)
);
```

#### Tabela `price_history`
Armazena a série temporal de preços coletados para geração dos gráficos de tendência por moeda e país.

```sql
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT DEFAULT 'BRL',
    country_code TEXT DEFAULT 'BR',
    recorded_at TEXT NOT NULL
);
```

### Índices Otimizados

* `idx_tracked_platform_game_country`: `tracked_games(platform, game_id, country_code)`
* `idx_tracked_user_channel`: `tracked_games(user_id, channel_id)`
* `idx_tracked_channel`: `tracked_games(channel_id)`
* `idx_price_history_game_region`: `price_history(platform, game_id, country_code, recorded_at)`
