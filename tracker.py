#!/usr/bin/env python3
"""
Monitor do piso de uma figurinha no Mercado Steam (ex.: Sticker | FalleN (Gold) | Cologne 2026).

Roda de tempos em tempos (GitHub Actions). A cada execucao:
  1. Le o menor preco anunciado (piso) na Steam, em BRL, varias vezes e tira a mediana (mata ruido).
  2. Anexa o ponto ao historico (data/history.json).
  3. Suaviza, calcula o TOPO acumulado e o recuo (drawdown) a partir do topo -> stop movel.
  4. Define o sinal: HOLD (segura) / WATCH (atencao) / SELL (vende).
  5. So quando o sinal PIORA (ex.: HOLD->SELL) dispara um alerta no Discord.

Sem banco, sem segredo no codigo: estado fica em data/state.json, alerta usa env DISCORD_WEBHOOK_URL.
Stdlib pura (urllib) -> nao precisa instalar nada.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
HISTORY_PATH = ROOT / "data" / "history.json"
STATE_PATH = ROOT / "data" / "state.json"

USER_AGENT = "Mozilla/5.0 (sticker-tracker; +https://github.com/bowjoww)"
PRICEOVERVIEW = "https://steamcommunity.com/market/priceoverview/"


# ----------------------------------------------------------------------------- IO
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------------------- parsing
def parse_money_brl(text: str) -> float | None:
    """'R$ 1.092,50' -> 1092.50. Formato BR: '.' milhar, ',' decimal."""
    if not text:
        return None
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch in ".,")
    if not cleaned:
        return None
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


# ----------------------------------------------------------------------------- Steam
def fetch_overview(cfg: dict) -> dict | None:
    params = urllib.parse.urlencode(
        {
            "appid": cfg["appid"],
            "currency": cfg["currency"],
            "market_hash_name": cfg["market_hash_name"],
        }
    )
    req = urllib.request.Request(
        f"{PRICEOVERVIEW}?{params}", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - rede instavel, queremos seguir vivo
        print(f"[WARN] falha ao chamar Steam: {exc}", file=sys.stderr)
        return None
    if not data.get("success"):
        print(f"[WARN] Steam retornou success=false: {data}", file=sys.stderr)
        return None
    return data


def sample_floor(cfg: dict) -> dict | None:
    """Le o piso N vezes e devolve a mediana, pra nao reagir a um anuncio solto."""
    floors, medians, volume = [], [], None
    n = max(1, int(cfg.get("samples_per_run", 3)))
    for i in range(n):
        ov = fetch_overview(cfg)
        if ov:
            low = parse_money_brl(ov.get("lowest_price", ""))
            med = parse_money_brl(ov.get("median_price", ""))
            if low is not None:
                floors.append(low)
            if med is not None:
                medians.append(med)
            if ov.get("volume"):
                volume = ov["volume"]
        if i < n - 1:
            time.sleep(float(cfg.get("sample_sleep_seconds", 4)))
    if not floors:
        return None
    return {
        "floor": round(statistics.median(floors), 2),
        "median_sale": round(statistics.median(medians), 2) if medians else None,
        "volume": volume,
        "samples": floors,
    }


# ----------------------------------------------------------------------------- sinal
def classify(drawdown_pct: float, cfg: dict) -> str:
    if drawdown_pct >= cfg["sell_threshold_pct"]:
        return "SELL"
    if drawdown_pct >= cfg["watch_threshold_pct"]:
        return "WATCH"
    return "HOLD"


SEVERITY = {"HOLD": 0, "WATCH": 1, "SELL": 2}


def evaluate(history: list[dict], cfg: dict) -> dict:
    """Recebe o historico (ja com o ponto novo no fim) e devolve o estado atual."""
    floors = [p["floor"] for p in history]
    window = max(1, int(cfg.get("smoothing_window", 3)))
    smoothed = round(statistics.median(floors[-window:]), 2)

    # topo acumulado calculado sobre a serie suavizada
    smoothed_series, running = [], []
    for i in range(len(floors)):
        running.append(floors[i])
        smoothed_series.append(statistics.median(running[-window:]))
    peak = round(max(smoothed_series), 2)

    drawdown_pct = round((peak - smoothed) / peak * 100, 2) if peak else 0.0
    signal = classify(drawdown_pct, cfg)

    # tendencia: inclinacao recente da serie suavizada (ultimos ~3 pontos)
    trend = "flat"
    if len(smoothed_series) >= 3:
        delta = smoothed_series[-1] - smoothed_series[-3]
        if delta > 0.5:
            trend = "up"
        elif delta < -0.5:
            trend = "down"

    stop_price = round(peak * (1 - cfg["sell_threshold_pct"] / 100), 2)
    return {
        "current_floor": history[-1]["floor"],
        "smoothed": smoothed,
        "peak": peak,
        "drawdown_pct": drawdown_pct,
        "stop_price": stop_price,
        "signal": signal,
        "trend": trend,
        "median_sale": history[-1].get("median_sale"),
        "volume": history[-1].get("volume"),
        "updated_at": history[-1]["ts"],
        "points": len(history),
        "item": cfg["market_hash_name"],
        "currency_label": cfg.get("currency_label", "BRL"),
        "listing_url": cfg.get("listing_url", ""),
        "dashboard_url": cfg.get("dashboard_url", ""),
        "watch_threshold_pct": cfg["watch_threshold_pct"],
        "sell_threshold_pct": cfg["sell_threshold_pct"],
    }


def should_alert(state: dict, prev: dict, cfg: dict) -> bool:
    """Alerta so quando a severidade SOBE acima do ultimo alerta ja enviado.
    Re-arma quando o recuo volta abaixo de rearm_threshold_pct."""
    last_alerted = prev.get("last_alert_signal", "HOLD")
    if state["drawdown_pct"] < cfg.get("rearm_threshold_pct", 4.0):
        last_alerted = "HOLD"  # recuperou -> pode alertar de novo no futuro
    fire = SEVERITY[state["signal"]] > SEVERITY[last_alerted]
    state["last_alert_signal"] = state["signal"] if fire else last_alerted
    return fire


# ----------------------------------------------------------------------------- Discord
def discord_payload(state: dict, mention_user_id: str | None = None) -> dict:
    cur = state["currency_label"]
    color = {"SELL": 0xE11D48, "WATCH": 0xF59E0B, "HOLD": 0x22C55E}[state["signal"]]
    head = {
        "SELL": "🔴 SINAL DE VENDA",
        "WATCH": "🟡 ATENÇÃO — piso recuando",
        "HOLD": "🟢 Segura",
    }[state["signal"]]
    lines = [
        f"**{state['item']}**",
        f"Piso agora: **{cur} {state['current_floor']:.2f}**  (suavizado {cur} {state['smoothed']:.2f})",
        f"Topo: {cur} {state['peak']:.2f}  ·  recuo do topo: **−{state['drawdown_pct']:.1f}%**",
        f"Stop móvel ({state['sell_threshold_pct']:.0f}%): {cur} {state['stop_price']:.2f}",
    ]
    if state["signal"] == "SELL":
        lines.append("\n➡️ O piso recuou além do limite — considere **vender** perto do topo.")
    fields = []
    if state.get("listing_url"):
        fields.append({"name": "Anúncio Steam", "value": state["listing_url"], "inline": False})
    if state.get("dashboard_url"):
        fields.append({"name": "Painel", "value": state["dashboard_url"], "inline": False})
    payload = {
        "embeds": [
            {
                "title": head,
                "description": "\n".join(lines),
                "color": color,
                "fields": fields,
                "footer": {"text": "Monitor Mercado Steam · stop móvel"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }
    if mention_user_id:
        payload["content"] = f"<@{mention_user_id}>"
        payload["allowed_mentions"] = {"users": [str(mention_user_id)]}
    return payload


def send_discord(url: str, payload: dict) -> bool:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ok = 200 <= resp.status < 300
            print(f"[INFO] Discord HTTP {resp.status}")
            return ok
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] falha ao enviar Discord: {exc}", file=sys.stderr)
        return False


# ----------------------------------------------------------------------------- main
def run_once(cfg: dict, sample: dict | None = None) -> dict:
    """Uma iteracao completa. `sample` injetavel para testes."""
    if sample is None:
        sample = sample_floor(cfg)
    if sample is None:
        print("[WARN] sem leitura valida nesta rodada; historico intacto.", file=sys.stderr)
        return load_json(STATE_PATH, {})

    history = load_json(HISTORY_PATH, [])
    history.append(
        {
            "ts": now_iso(),
            "floor": sample["floor"],
            "median_sale": sample.get("median_sale"),
            "volume": sample.get("volume"),
        }
    )
    prev_state = load_json(STATE_PATH, {})
    state = evaluate(history, cfg)
    fire = should_alert(state, prev_state, cfg)

    save_json(HISTORY_PATH, history)
    save_json(STATE_PATH, state)

    cur = state["currency_label"]
    print(
        f"[{state['updated_at']}] piso={cur} {state['current_floor']:.2f} "
        f"suav={state['smoothed']:.2f} topo={state['peak']:.2f} "
        f"recuo=-{state['drawdown_pct']:.1f}% sinal={state['signal']} "
        f"tend={state['trend']} pts={state['points']} alerta={'SIM' if fire else 'nao'}"
    )

    if fire:
        url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        if url:
            send_discord(url, discord_payload(state))
        else:
            print("[WARN] DISCORD_WEBHOOK_URL nao definido; alerta nao enviado.", file=sys.stderr)
    return state


def main() -> int:
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        print("[ERROR] config.json ausente/invalido", file=sys.stderr)
        return 1
    run_once(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
