"""
Fontes de preco de mercados de TERCEIROS (valor "real", mais fiel que a Steam).

A Steam costuma estar inflada (taxa 15% + saldo preso). Buff163/CSFloat/etc. mostram
o que o item vale de verdade no mercado. Buff exige login (nao automatizavel de graca);
aqui ficam as fontes que dao pra puxar headless:

- CSFloat    : API oficial, precisa de API key (gratis no perfil > Developer). USD cents.
- DMarket    : API publica, sem key. So aceita USD (centavos) -> convertemos pra BRL.
- White.Market: export JSON publico (10MB), sem key. So lista itens COM anuncio ativo.

Cada funcao devolve {source, price_brl, price_native, currency, url} ou None (sem anuncio).
Stdlib pura (urllib). Item raro pode nao ter anuncio em nenhum -> None e dado valido.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

UA = "teamvix-steam-tracker/1.0"
DM_GAME = "a8db"  # CS2 na DMarket


def _get(url: str, headers: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ----------------------------------------------------------------------------- cambio
_fx = {"ts": 0.0, "rate": None}


def usd_to_brl() -> float:
    """Cotacao USD->BRL (cache 1h, fonte gratis sem key). Fallback se a API cair."""
    now = time.time()
    if _fx["rate"] and now - _fx["ts"] < 3600:
        return _fx["rate"]
    try:
        d = _get("https://open.er-api.com/v6/latest/USD")
        rate = float(d["rates"]["BRL"])
        _fx.update(ts=now, rate=rate)
        return rate
    except Exception:  # noqa: BLE001
        return _fx["rate"] or 5.1


def _brl(usd: float) -> float:
    return round(usd * usd_to_brl(), 2)


# ----------------------------------------------------------------------------- DMarket
def dmarket_price(market_hash_name: str) -> dict | None:
    q = urllib.parse.urlencode({
        "gameId": DM_GAME, "currency": "USD", "limit": 1,
        "orderBy": "price", "orderDir": "asc", "title": market_hash_name,
    })
    try:
        d = _get(f"https://api.dmarket.com/exchange/v1/market/items?{q}")
    except Exception:  # noqa: BLE001
        return None
    objs = d.get("objects") or []
    cents = objs[0].get("price", {}).get("USD") if objs else None
    if not cents:
        return None
    usd = int(cents) / 100
    return {
        "source": "DMarket", "currency": "USD", "price_native": round(usd, 2),
        "price_brl": _brl(usd),
        "url": "https://dmarket.com/ingame-items/item-list/csgo-skins?title="
        + urllib.parse.quote(market_hash_name),
    }


# ----------------------------------------------------------------------------- White.Market
_wm = {"ts": 0.0, "map": None}


def _whitemarket_map() -> dict:
    """Indexa o export publico por nome. Cache 30min (e 10MB, baixa 1x por ciclo)."""
    now = time.time()
    if _wm["map"] is not None and now - _wm["ts"] < 1800:
        return _wm["map"]
    try:
        arr = _get("https://s3.white.market/export/v1/prices/730.json", timeout=60)
    except Exception:  # noqa: BLE001
        return _wm["map"] or {}
    m = {x["market_hash_name"]: x for x in arr if isinstance(x, dict) and "market_hash_name" in x}
    _wm.update(ts=now, map=m)
    return m


def whitemarket_price(market_hash_name: str) -> dict | None:
    # Moeda do export = USD (confirmado: AWP Asiimov FT = US$127.90, na faixa certa).
    row = _whitemarket_map().get(market_hash_name)
    if not row:
        return None
    try:
        usd = float(row["price"])
    except Exception:  # noqa: BLE001
        return None
    return {
        "source": "White.Market", "currency": "USD", "price_native": round(usd, 2),
        "price_brl": _brl(usd),
        "url": row.get("market_product_link", "https://white.market/"),
    }


# ----------------------------------------------------------------------------- CSFloat
def csfloat_price(market_hash_name: str, api_key: str | None) -> dict | None:
    if not api_key:
        return None
    q = urllib.parse.urlencode({
        "sort_by": "lowest_price", "limit": 1, "type": "buy_now",
        "market_hash_name": market_hash_name,
    })
    try:
        d = _get(
            f"https://csfloat.com/api/v1/listings?{q}",
            headers={"User-Agent": UA, "Authorization": api_key},
        )
    except Exception:  # noqa: BLE001
        return None
    arr = d.get("data") if isinstance(d, dict) else d
    cents = arr[0].get("price") if arr else None
    if not cents:
        return None
    usd = int(cents) / 100
    return {
        "source": "CSFloat", "currency": "USD", "price_native": round(usd, 2),
        "price_brl": _brl(usd),
        "url": "https://csfloat.com/search?market_hash_name=" + urllib.parse.quote(market_hash_name),
    }


# ----------------------------------------------------------------------------- agregacao
def all_prices(market_hash_name: str, csfloat_key: str | None = None) -> dict:
    """Junta as fontes. Devolve {fonte: dado|None} + a menor (valor de mercado real)."""
    res = {
        "DMarket": dmarket_price(market_hash_name),
        "White.Market": whitemarket_price(market_hash_name),
        "CSFloat": csfloat_price(market_hash_name, csfloat_key),
    }
    valid = [v for v in res.values() if v and v.get("price_brl")]
    res["lowest"] = min(valid, key=lambda v: v["price_brl"]) if valid else None
    return res
