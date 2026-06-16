# Monitor de Mercado Steam — Team VIX

Acompanha o **piso** (menor anúncio) de itens do Mercado da Steam e avisa no Discord
quando o preço **vira pra baixo** (stop móvel), pra vender perto do topo.
Multi-usuário: cada um cadastra seus itens + o Discord pra ser avisado.

## Como funciona

- **Painel** (`web/`, GitHub Pages): lista os itens com status (🟢 segura / 🟡 atenção / 🔴 vende),
  gráfico por item, e formulário "adicionar item". Lê/escreve no Supabase com a *publishable key* (RLS).
- **Motor** (`poll_all.py`, GitHub Actions a cada 3h): varre os itens ativos, puxa o piso na Steam,
  grava o histórico, calcula o stop móvel e dispara o webhook do Discord com @menção do dono na escalada.
- **Banco** (Supabase): `steam_watches` (itens + dono + estado) e `steam_price_points` (histórico).
  Schema em `db/schema.sql`.
- **Bot** (`/acompanhar` no teamvix-bot): cadastra pegando o Discord da pessoa automático.

## Sinal (stop móvel)

A cada ciclo: lê o piso algumas vezes e tira a mediana (mata ruído) → suaviza → guarda o **topo**
acumulado → calcula o **recuo** do topo. Estados:

- **HOLD** (segura): recuo < `watch_threshold_pct` (6%)
- **WATCH** (atenção): recuo ≥ 6%
- **SELL** (vende): recuo ≥ `sell_threshold_pct` (10%)

Alerta só dispara quando a severidade **sobe** (anti-spam) e re-arma quando recupera. Limites por item.

## Secrets (GitHub Actions)

`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `DISCORD_WEBHOOK_URL`, `DASHBOARD_URL`.
A *publishable key* (pública) fica em `web/config.js`. Veja `.env.example`.

## Local

```bash
python test_tracker.py                       # testa a lógica do sinal (sem rede)
SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python poll_all.py   # roda o motor
```
