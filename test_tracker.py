#!/usr/bin/env python3
"""Teste do nucleo de sinal: simula subida + queda e confere os alertas.
Nao usa rede (injeta samples) nem envia Discord (webhook fora do env)."""
import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import tracker

CFG = json.loads((Path(__file__).resolve().parent / "config.json").read_text(encoding="utf-8"))


def feed(prices):
    """Roda o tracker ponto a ponto; devolve (estados, num_alertas)."""
    states, alerts = [], 0
    for p in prices:
        buf = io.StringIO()
        with redirect_stdout(buf):
            st = tracker.run_once(CFG, sample={"floor": float(p), "median_sale": None, "volume": None})
        states.append(st)
        if "alerta=SIM" in buf.getvalue():
            alerts += 1
    return states, alerts


def reset(tmp):
    tracker.HISTORY_PATH = Path(tmp) / "history.json"
    tracker.STATE_PATH = Path(tmp) / "state.json"


def main():
    with tempfile.TemporaryDirectory() as tmp:
        reset(tmp)

        # 1) Subida pura -> sempre HOLD, zero alertas
        up_states, up_alerts = feed([1000, 1100, 1200, 1300])
        assert all(s["signal"] == "HOLD" for s in up_states), [s["signal"] for s in up_states]
        assert up_alerts == 0, f"subida nao podia alertar, alertou {up_alerts}x"
        peak = up_states[-1]["peak"]
        print(f"OK  subida pura: HOLD o tempo todo, 0 alertas, topo={peak}")

        # 2) Queda forte -> escala pra SELL e alerta (WATCH depois SELL)
        down_states, down_alerts = feed([1250, 1150, 1050, 980])
        assert down_states[-1]["signal"] == "SELL", down_states[-1]["signal"]
        assert down_alerts >= 1, "queda deveria ter alertado"
        dd = down_states[-1]["drawdown_pct"]
        print(f"OK  queda: chegou em SELL com recuo -{dd}% e {down_alerts} alerta(s) de escalada")

        # 3) Continua caindo em nivel SELL -> NAO pode alertar de novo (anti-spam)
        _, spam = feed([950])
        assert spam == 0, f"nao podia re-alertar em SELL continuo, alertou {spam}x"
        print("OK  anti-spam: SELL continuo nao re-alerta")

        # 4) Recupera (re-arma) e cai de novo -> volta a alertar
        feed([1350, 1350, 1350])  # novo topo, recuo ~0 -> re-arma
        _, re_alerts = feed([1200, 1150, 1050])
        assert re_alerts >= 1, "apos recuperar, nova queda deveria re-alertar"
        print(f"OK  re-arme: apos recuperar e cair, alertou de novo ({re_alerts}x)")

    print("\n>>> TODOS OS TESTES PASSARAM <<<")


if __name__ == "__main__":
    main()
