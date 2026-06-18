#!/usr/bin/env python3
"""
Motor multi-item: varre todos os itens cadastrados no Supabase, puxa o piso de
cada um na Steam, atualiza historico/estado e dispara alerta no Discord (com
@mencao do dono) quando o stop movel vira pra baixo.

Roda no GitHub Actions (cron). Stdlib pura.
Env necessarios:
  SUPABASE_URL, SUPABASE_SERVICE_KEY  -> acesso ao banco (service role, bypassa RLS)
  DISCORD_WEBHOOK_URL                 -> canal do time onde o alerta e postado
  DASHBOARD_URL (opcional)            -> link do painel incluido no alerta
"""
from __future__ import annotations

import os
import sys
import time
import json
import urllib.parse
import urllib.request

import tracker  # reaproveita parse/fetch/evaluate/should_alert/discord
import markets  # precos de terceiros (DMarket, White.Market, CSFloat) em BRL

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").strip()
CSFLOAT_KEY = os.environ.get("CSFLOAT_API_KEY", "").strip()  # opcional; vazio = pula CSFloat

# UA NAO-navegador: a secret key do Supabase e recusada se o UA parecer browser
SB_USER_AGENT = "teamvix-steam-tracker/1.0"

SAMPLES_PER_RUN = int(os.environ.get("SAMPLES_PER_RUN", "2"))
SAMPLE_SLEEP = float(os.environ.get("SAMPLE_SLEEP", "4"))
BETWEEN_WATCHES = float(os.environ.get("BETWEEN_WATCHES", "5"))  # respeita rate limit Steam

# colunas de estado que o poller persiste de volta no watch
STATE_COLS = [
    "current_floor", "smoothed", "peak", "drawdown_pct", "stop_price",
    "signal", "trend", "last_alert_signal",
]


# ----------------------------------------------------------------------------- Supabase REST
def sb(method: str, path: str, body=None, prefer: str | None = None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
        "User-Agent": SB_USER_AGENT,
    }
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=40) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


def get_active_watches() -> list[dict]:
    return sb("GET", "steam_watches?active=eq.true&select=*") or []


def get_history(watch_id: str) -> list[dict]:
    q = f"steam_price_points?watch_id=eq.{watch_id}&select=ts,floor,median_sale,volume&order=ts.asc"
    return sb("GET", q) or []


def insert_point(watch_id: str, ts: str, sample: dict) -> None:
    sb("POST", "steam_price_points", body={
        "watch_id": watch_id, "ts": ts,
        "floor": sample["floor"], "median_sale": sample.get("median_sale"),
        "volume": sample.get("volume"),
    }, prefer="return=minimal")


def update_state(watch_id: str, fields: dict) -> None:
    sb("PATCH", f"steam_watches?id=eq.{watch_id}", body=fields, prefer="return=minimal")


# ----------------------------------------------------------------------------- por item
def _num(v, default):
    """Usa o default so quando o campo e None (NAO quando e 0 — 'x or d' engole o 0)."""
    return default if v is None else v


def watch_to_cfg(w: dict) -> dict:
    return {
        "appid": w["appid"],
        "currency": w["currency"],
        "currency_label": w.get("currency_label", "BRL"),
        "market_hash_name": w["market_hash_name"],
        "listing_url": w.get("steam_url", ""),
        "dashboard_url": DASHBOARD_URL,
        "samples_per_run": SAMPLES_PER_RUN,
        "sample_sleep_seconds": SAMPLE_SLEEP,
        "smoothing_window": int(_num(w.get("smoothing_window"), 3)),
        "watch_threshold_pct": float(_num(w.get("watch_threshold_pct"), 6)),
        "sell_threshold_pct": float(_num(w.get("sell_threshold_pct"), 10)),
        "rearm_threshold_pct": float(_num(w.get("rearm_threshold_pct"), 4)),
    }


def market_snapshot(market_hash_name: str) -> dict | None:
    """Preco do item nos mercados de terceiros (valor real, mais fiel que a Steam)."""
    try:
        r = markets.all_prices(market_hash_name, CSFLOAT_KEY or None)
    except Exception as exc:  # noqa: BLE001 - mercado nao pode derrubar o ciclo
        print(f"[WARN] mercados {market_hash_name}: {exc}", file=sys.stderr)
        return None
    return {
        "checked_at": tracker.now_iso(),
        "fx_usd_brl": markets.usd_to_brl(),
        "sources": {k: r.get(k) for k in ("DMarket", "White.Market", "CSFloat")},
        "lowest": r.get("lowest"),
    }


def process_watch(w: dict) -> str:
    label = w.get("label") or w["market_hash_name"]
    cfg = watch_to_cfg(w)
    mp = market_snapshot(w["market_hash_name"])  # parceiros (independe da Steam)
    try:
        sample = tracker.sample_floor(cfg)
    except Exception as exc:  # noqa: BLE001
        sample = None
        print(f"[WARN] {label}: erro lendo Steam: {exc}", file=sys.stderr)

    if sample is None:
        update_state(w["id"], {"last_checked_at": tracker.now_iso(),
                               "last_error": "sem leitura valida da Steam",
                               "market_prices": mp})
        lo = mp and mp.get("lowest")
        return (f"{label}: SEM LEITURA Steam"
                + (f" | parceiro R$ {lo['price_brl']:.2f} ({lo['source']})" if lo else " | sem parceiro"))

    ts = tracker.now_iso()
    history = get_history(w["id"])
    history.append({"ts": ts, "floor": sample["floor"],
                    "median_sale": sample.get("median_sale"), "volume": sample.get("volume")})

    state = tracker.evaluate(history, cfg)
    fire = tracker.should_alert(state, w, cfg)  # usa w["last_alert_signal"] como prev

    insert_point(w["id"], ts, sample)
    fields = {c: state[c] for c in STATE_COLS}
    fields["last_checked_at"] = ts
    fields["last_error"] = None
    fields["market_prices"] = mp
    update_state(w["id"], fields)

    if fire and WEBHOOK:
        state["item"] = label
        tracker.send_discord(WEBHOOK, tracker.discord_payload(state, w.get("discord_user_id")))

    cur = state["currency_label"]
    return (f"{label}: {cur} {state['current_floor']:.2f} "
            f"(topo {state['peak']:.2f}, -{state['drawdown_pct']:.1f}%) "
            f"{state['signal']}{' [ALERTA]' if fire else ''}")


def main() -> int:
    if not SUPABASE_URL or not SERVICE_KEY:
        print("[ERROR] SUPABASE_URL/SUPABASE_SERVICE_KEY ausentes", file=sys.stderr)
        return 1
    watches = get_active_watches()
    print(f"[INFO] {len(watches)} item(ns) ativos")
    for i, w in enumerate(watches):
        try:
            print("  -", process_watch(w))
        except Exception as exc:  # noqa: BLE001 - um item nao pode derrubar os outros
            print(f"[ERROR] item {w.get('id')}: {exc}", file=sys.stderr)
        if i < len(watches) - 1:
            time.sleep(BETWEEN_WATCHES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
