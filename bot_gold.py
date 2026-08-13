#!/usr/bin/env python3
"""
Bot ORO intradia (15 min) — capital.com DEMO.

Estrategia validada (barrido + robustez split-half sobre 72 dias de oro 15m):
reversion Bollinger + RSI, pero con salida rapida (corta perdidas, deja correr ganancias):
  - Entra LARGO si el cierre 15m rompe bajo la banda inferior (BB20/2) con RSI < 35.
  - Entra CORTO si rompe sobre la banda superior con RSI > 65.
  - Stop Loss = 1xATR (ajustado, corto).  Take Profit = 1.5xATR.
  - Una posicion a la vez; candado: max 1 orden por vela 15m.
Datos: precios GOLD 15m de la propia capital.com (mismo instrumento que operamos).

Uso:
  python bot_gold.py            # evalua y opera si hay senal
  python bot_gold.py --status   # solo muestra indicadores/senal
  python bot_gold.py --dry-run  # evalua pero NO coloca la orden
"""
import sys, math
from datetime import datetime, timezone, timedelta
import capital_client as cc

EPIC      = "GOLD"
SIZE      = 2.0            # tamano de la orden (ajustable) — ~$37 TP / ~$25 SL, riesgo 2.5% cuenta
BB_LEN    = 20
BB_MULT   = 1.75          # banda mas angosta -> entra antes (aflojado, robusto en backtest)
RSI_LEN   = 14
RSI_LOW   = 38            # RSI menos extremo -> mas entradas (~3.1/dia vs 2.2)
RSI_HIGH  = 62
ATR_LEN   = 14
SL_MULT   = 1.0           # Stop Loss = 1.0 x ATR
TP_MULT   = 1.5           # Take Profit = 1.5 x ATR
BAR_MIN   = 15            # velas de 15 minutos


def _mid(x):
    return (x["bid"] + x["ask"]) / 2 if isinstance(x, dict) else x


def current_bar_start():
    """Inicio de la vela 15m en curso (UTC, naive) = cierre de la ultima vela cerrada."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(minute=(now.minute // BAR_MIN) * BAR_MIN, second=0, microsecond=0)


def fetch_closed(h):
    """OHLC (mid) SOLO de velas 15m ya cerradas, en orden. Usa la propia capital.com."""
    r = cc.get(h, f"/api/v1/prices/{EPIC}?resolution=MINUTE_15&max=100")
    if r.status_code != 200:
        sys.exit(f"No se pudo bajar precios ({r.status_code}): {r.text}")
    bar0 = current_bar_start()
    o = h_ = l = c = None
    O, H, L, C = [], [], [], []
    for p in r.json().get("prices", []):
        t = (p.get("snapshotTimeUTC") or p.get("snapshotTime") or "").replace("Z", "")
        try:
            bt = datetime.fromisoformat(t)
        except ValueError:
            continue
        if bt >= bar0:      # vela en curso -> fuera
            continue
        O.append(_mid(p["openPrice"])); H.append(_mid(p["highPrice"]))
        L.append(_mid(p["lowPrice"]));  C.append(_mid(p["closePrice"]))
    return O, H, L, C


# ---- indicadores (identicos a Pine ta.*) ----
def sma(s, n, i): return sum(s[i-n+1:i+1]) / n
def stdev_pop(s, n, i):
    m = sma(s, n, i); return math.sqrt(sum((x-m)**2 for x in s[i-n+1:i+1]) / n)
def _rma(s, n):
    out = [None]*len(s)
    if len(s) < n: return out
    p = sum(s[:n]) / n; out[n-1] = p
    for i in range(n, len(s)): p = (p*(n-1) + s[i]) / n; out[i] = p
    return out
def rsi_series(c, n):
    g, l = [0.0], [0.0]
    for i in range(1, len(c)):
        d = c[i]-c[i-1]; g.append(max(d, 0.0)); l.append(max(-d, 0.0))
    ag, al = _rma(g, n), _rma(l, n); out = [None]*len(c)
    for i in range(len(c)):
        if ag[i] is None: continue
        out[i] = 100.0 if al[i] == 0 else 100 - 100/(1 + ag[i]/al[i])
    return out
def atr_series(h, l, c, n):
    tr = [h[0]-l[0]]
    for i in range(1, len(c)):
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return _rma(tr, n)


def evaluate(h):
    o, hi, lo, c = fetch_closed(h)
    if len(c) < BB_LEN + 2:
        sys.exit("Pocas velas para calcular.")
    i = len(c) - 1
    basis = sma(c, BB_LEN, i); dev = BB_MULT * stdev_pop(c, BB_LEN, i)
    upper, lower = basis + dev, basis - dev
    rsi = rsi_series(c, RSI_LEN); atr = atr_series(hi, lo, c, ATR_LEN)
    a = atr[i]

    def longC(j):
        b = sma(c, BB_LEN, j); d = BB_MULT*stdev_pop(c, BB_LEN, j)
        return c[j] < (b-d) and rsi[j] is not None and rsi[j] < RSI_LOW
    def shortC(j):
        b = sma(c, BB_LEN, j); d = BB_MULT*stdev_pop(c, BB_LEN, j)
        return c[j] > (b+d) and rsi[j] is not None and rsi[j] > RSI_HIGH

    long_e  = longC(i)  and not longC(i-1)
    short_e = shortC(i) and not shortC(i-1)
    side = "BUY" if long_e else ("SELL" if short_e else None)
    close = c[i]
    if side == "BUY":
        sl = round(close - SL_MULT*a, 1); tp = round(close + TP_MULT*a, 1)
    elif side == "SELL":
        sl = round(close + SL_MULT*a, 1); tp = round(close - TP_MULT*a, 1)
    else:
        sl = tp = None
    return {"close": round(close, 1), "upper": round(upper, 1), "lower": round(lower, 1),
            "rsi": round(rsi[i], 1) if rsi[i] else None, "atr": round(a, 1) if a else None,
            "side": side, "sl": sl, "tp": tp}


def _mysize(v):
    """True si el tamano corresponde a ESTE bot (para no chocar con el bot FVG)."""
    try:
        return abs(float(v) - SIZE) < 1e-9
    except (TypeError, ValueError):
        return False


def has_open_position(h):
    pos = cc.get(h, "/api/v1/positions").json().get("positions", [])
    return any(p["market"]["epic"] == EPIC and _mysize(p["position"]["size"]) for p in pos)


def acted_this_bar(h, bar0):
    frm = (bar0 - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    r = cc.get(h, f"/api/v1/history/activity?from={frm}")
    if r.status_code != 200:
        return False
    for a in r.json().get("activities", []):
        if a.get("epic") != EPIC or a.get("type") not in ("POSITION", "WORKING_ORDER"):
            continue
        if not _mysize(a.get("details", {}).get("size")):   # solo mis ordenes (tamano 0.5)
            continue
        try:
            d = datetime.strptime(a["dateUTC"], "%Y-%m-%dT%H:%M:%S.%f")
        except (KeyError, ValueError):
            continue
        if d >= bar0:
            return True
    return False


def main():
    dry = "--dry-run" in sys.argv
    status = "--status" in sys.argv
    h = cc.login()
    sig = evaluate(h)
    print(f"[ORO 15m GOLD] close={sig['close']} banda[{sig['lower']}..{sig['upper']}] "
          f"RSI={sig['rsi']} ATR={sig['atr']}")
    if sig["side"]:
        print(f"  >> SENAL {sig['side']}  SL={sig['sl']}  TP={sig['tp']}")
    else:
        print("  >> sin senal en la ultima vela cerrada")
    if status or not sig["side"]:
        return
    if has_open_position(h):
        print("  Ya hay posicion abierta en GOLD -> no abro otra."); return
    bar0 = current_bar_start()
    if acted_this_bar(h, bar0):
        print(f"  Ya se opero en esta vela 15m (cierre {bar0}Z) -> candado."); return
    if dry:
        print("  [DRY-RUN] No coloco la orden."); return
    # Recalcular SL/TP desde el precio ACTUAL (no el cierre viejo): evita que el nivel
    # quede del lado equivocado si el mercado se movio entre el cierre y la orden.
    snap = cc.get(h, f"/api/v1/markets/{EPIC}").json().get("snapshot", {})
    a = sig["atr"]
    if sig["side"] == "BUY":
        entry = snap.get("offer"); sl = round(entry - SL_MULT * a, 1); tp = round(entry + TP_MULT * a, 1)
    else:
        entry = snap.get("bid"); sl = round(entry + SL_MULT * a, 1); tp = round(entry - TP_MULT * a, 1)
    body = {"epic": EPIC, "direction": sig["side"], "size": SIZE, "stopLevel": sl, "profitLevel": tp}
    r = cc.post(h, "/api/v1/positions", body)
    if r.status_code not in (200, 201):
        print(f"  Orden NO colocada ({r.status_code}): {r.text} -> se reintenta en la proxima vela.")
        return
    ref = r.json().get("dealReference")
    conf = cc.get(h, f"/api/v1/confirms/{ref}").json()
    print(f"  ORDEN COLOCADA: {sig['side']} {SIZE} {EPIC} @ {entry} SL={sl} TP={tp} ref={ref} status={conf.get('dealStatus')}")


if __name__ == "__main__":
    main()
