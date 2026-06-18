#!/usr/bin/env python3
"""
Vigia do monitor: se o poller nao atualiza ha muito tempo (last_checked_at velho),
avisa no Discord. Pega o caso de "o cron parou e ninguem percebeu".

Roda num cron proprio (watchdog.yml), independente do poller.
Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, DISCORD_WEBHOOK_URL, WATCHDOG_MAX_HOURS (default 9).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

SB = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
MAX_HOURS = float(os.environ.get("WATCHDOG_MAX_HOURS", "9"))
DASHBOARD = os.environ.get("DASHBOARD_URL", "https://bowjoww.github.io/steam-market-tracker/")


def sb(path: str):
    req = urllib.request.Request(
        f"{SB}/rest/v1/{path}",
        headers={"apikey": KEY, "Authorization": f"Bearer {KEY}",
                 "User-Agent": "teamvix-watchdog/1.0"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def alert(msg: str) -> None:
    if not WEBHOOK:
        print("[WARN] sem DISCORD_WEBHOOK_URL; nao alertou", file=sys.stderr)
        return
    payload = {"content": msg, "allowed_mentions": {"parse": []}}
    req = urllib.request.Request(
        WEBHOOK, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    urllib.request.urlopen(req, timeout=30)
    print("[INFO] alerta enviado")


def main() -> int:
    if not SB or not KEY:
        print("[ERROR] SUPABASE_URL/SUPABASE_SERVICE_KEY ausentes", file=sys.stderr)
        return 1
    rows = sb("steam_watches?select=last_checked_at&active=eq.true&order=last_checked_at.desc&limit=1")
    if not rows or not rows[0].get("last_checked_at"):
        print("nenhum item ativo / sem last_checked_at; ok")
        return 0
    last = datetime.fromisoformat(rows[0]["last_checked_at"].replace("Z", "+00:00"))
    age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    print(f"ultima checagem ha {age_h:.1f}h (limite {MAX_HOURS:.0f}h)")
    if age_h > MAX_HOURS:
        alert(
            f"⚠️ **Monitor de Mercado Steam parado** — última atualização "
            f"há ~{age_h:.0f}h (limite {MAX_HOURS:.0f}h). O cron do GitHub pode ter sido "
            f"desligado ou quebrado. Painel: {DASHBOARD}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
