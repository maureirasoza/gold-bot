#!/usr/bin/env python3
"""
Bot ORO intradia (15 min) — capital.com DEMO.

ESTADO 19-sep-2026 (tarde): **REACTIVADO con MEJORA** — trailing 2.0x -> **5.0xATR** y size 2.0 -> **0.8**.
  Busqueda de la mejor mejora sobre 300d REALES (capital-demo/bollinger_mejora.py: 198 variantes de
  entrada/lado/salida con el signal_last real): la entrada actual se mantiene; lo que cambia es dar
  AIRE al rebote. Con trailing 5.0x: +1987 pts, PF 1.70, acierto 39%, maxDD -297, tercios
  +1045/+462/+479 -> ROB3, ~3.8 trades/sem (meseta 5-6x ROB3; 4.5x y 7x caen a ROB2). Cron 8252402
  re-habilitado. Size 0.8 para que el maxDD sea ~23% de la cuenta (a 2.0 seria 57%).
  Historia del hallazgo que llevo a la pausa (misma manana):
  RE-VALIDACION sobre el INSTRUMENTO REAL (capital-demo/backtest_real.py --bollinger --source
  capital --sweep: GOLD 15m de la propia capital.com, 300 dias, 19395 velas, nov-2025 -> sep-2026,
  mismo signal_last de este bot):
    trailing 1.0x -22 | 1.25x -128 | 1.5x -184 (valor viejo) | 1.75x -569 | 2.0x -244 (valor
    actual) | 2.5x -42  -> TODOS ROB1 (pierden). Solo 3.0x: +771, PF 1.27, ROB2 (tercio medio -280).
  => Las validaciones previas (72d / 60d de Yahoo) eran ARTEFACTOS del regimen reciente, no un
     edge: la estrategia entera (BB26/1.75 + RSI + ADX/EMA) no gana sobre 10 meses reales.
     No es culpa del cambio 1.5->2.0x del 18-sep (ambos pierden). LECCION: 60d de 15m no alcanzan;
     validar con --source capital antes de tocar o desplegar. Reactivar solo si un re-diseno
     (p.ej. alrededor de 3x) pasa ROB3 en 300d reales. Nota: FVG si pasa (ROB3, PF 1.42).

Estrategia validada ORIGINALMENTE (barrido + robustez split-half sobre 72 dias de oro 15m):
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
SIZE      = 0.8            # 19-sep: bajado de 2.0 al pasar el trailing a 5xATR (stop ~46 pts con ATR
                           # mediano 9.2): a 2.0 el maxDD del backtest real (-297 pts) seria 57% de la
                           # cuenta; a 0.8 es 23% y ~$37 de riesgo inicial/trade. 0.5 = opcion conservadora.
                           # Unico en GOLD (trend 0.5, FVG 1.0) -> identifica al bot en candado y tracker.
BB_LEN    = 26            # banda mas larga/estable: filtra falsos extremos (20-ago->26)
                          # Backtest 71d robusto: +405 vs +311 (BB20), 44% acierto, mitades +252/+153.
BB_MULT   = 1.75          # banda mas angosta -> entra antes (aflojado, robusto en backtest)
RSI_LEN   = 14
RSI_LOW   = 38            # RSI menos extremo -> mas entradas (~3.1/dia vs 2.2)
RSI_HIGH  = 62
ATR_LEN   = 14
SL_MULT   = 1.15          # Stop Loss = 1.15 x ATR (25-ago: stop un poco mas ancho; salva
                          # algunas mechas. Efecto neto marginal en backtest, pero robusto.)
TP_MULT   = 1.5           # Take Profit = 1.5 x ATR (26-ago: bajado de 1.55). Con spread
                          # realista (cierre al bid, ~$0.30-0.60 gold), 1.5 es mas ROBUSTO:
                          # empata al 1.55 con spread apretado y le gana claro con spread
                          # ancho (sesiones finas). Captura casi-TP que el bid deja cortos.
TRAIL_ATR = 5.0           # SALIDA UNICA: trailing nativo = 5.0 x ATR, sin TP. Elegido 19-sep por el
                          # BACKTEST REAL (capital-demo/bollinger_mejora.py + backtest_real.py
                          # --bollinger --source capital: GOLD 15m de capital.com, 300d, 19395 velas,
                          # mismo signal_last): con ESTA entrada, 2.0x -244 ROB1 | 3x +771 ROB2 |
                          # 4x +1117 ROB2 | 4.5x +1605 ROB2 | 5.0x +1987 PF1.70 maxDD-297 ROB3
                          # (+1045/+462/+479) | 5.5x +1969 ROB3 | 6x +2387 PF1.95 ROB3 | 7x ROB2.
                          # Meseta robusta 5-6x; 5.0 = menor drawdown y tercios mas parejos. Mismo
                          # patron que el trend (5x): la reversion tambien necesita AIRE para el rebote.
                          # Alternativas (solo largo sin filtro, SL/TP fijo) son ROB3 pero PF 1.1-1.3.
BAR_MIN   = 15            # velas de 15 minutos
# FILTRO DIRECCIONAL DE REGIMEN: la reversion muere en tendencia fuerte, entonces NO se
# fadea contra ella. Si ADX>=ADX_MIN (tendencia con fuerza): no VENDER sobre la EMA_TREND
# (no shortear un uptrend) ni COMPRAR bajo ella (no comprar un downtrend). Validado en
# backtest (71d, robusto split-half): +375 vs +267 sin filtro, payoff 1.83 vs 1.52.
ADX_LEN   = 14
ADX_MIN   = 20           # umbral de "tendencia con fuerza"
EMA_TREND = 200          # referencia de la tendencia mayor


def _mid(x):
    return (x["bid"] + x["ask"]) / 2 if isinstance(x, dict) else x


def current_bar_start():
    """Inicio de la vela 15m en curso (UTC, naive) = cierre de la ultima vela cerrada."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(minute=(now.minute // BAR_MIN) * BAR_MIN, second=0, microsecond=0)


def fetch_closed(h):
    """OHLC (mid) SOLO de velas 15m ya cerradas, en orden. Usa la propia capital.com."""
    r = cc.get(h, f"/api/v1/prices/{EPIC}?resolution=MINUTE_15&max=300")
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


def ema_series(s, k):
    out = [s[0]]; a = 2 / (k + 1)
    for i in range(1, len(s)):
        out.append(s[i] * a + out[-1] * (1 - a))
    return out


def adx_last(h, l, c, n):
    """Ultimo valor de ADX (Wilder). None si no hay suficientes velas."""
    if len(c) < 2 * n + 2:
        return None
    pdm = [0.0]; ndm = [0.0]; tr = [h[0]-l[0]]
    for i in range(1, len(c)):
        up = h[i]-h[i-1]; dn = l[i-1]-l[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        ndm.append(dn if (dn > up and dn > 0) else 0.0)
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    sp = _rma(pdm, n); sn = _rma(ndm, n); st = _rma(tr, n)
    dx = [None] * len(c)
    for i in range(len(c)):
        if st[i] and st[i] > 0:
            pdi = 100*sp[i]/st[i]; ndi = 100*sn[i]/st[i]; tot = pdi+ndi
            dx[i] = 0.0 if tot == 0 else 100*abs(pdi-ndi)/tot
    start = next((i for i, x in enumerate(dx) if x is not None), len(c))
    if start + n > len(c):
        return None
    p = sum(dx[start:start+n]) / n
    for i in range(start+n, len(c)):
        p = (p*(n-1) + dx[i]) / n
    return p


def signal_last(o, hi, lo, c):
    """Senal Bollinger en la ULTIMA vela de los arrays dados. PURA (sin red): misma logica
    que el bot en vivo. El backtester importa ESTA funcion -> test identico al bot real."""
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

    # FILTRO DIRECCIONAL: no fadear contra una tendencia fuerte.
    ema_t = ema_series(c, EMA_TREND)[i]
    adxv = adx_last(hi, lo, c, ADX_LEN)
    filtrado = False
    if side and adxv is not None and adxv >= ADX_MIN:
        if (side == "SELL" and close > ema_t) or (side == "BUY" and close < ema_t):
            side = None; filtrado = True   # seria fade contra tendencia fuerte -> se descarta

    if side == "BUY":
        sl = round(close - SL_MULT*a, 1); tp = round(close + TP_MULT*a, 1)
    elif side == "SELL":
        sl = round(close + SL_MULT*a, 1); tp = round(close - TP_MULT*a, 1)
    else:
        sl = tp = None
    return {"close": round(close, 1), "upper": round(upper, 1), "lower": round(lower, 1),
            "rsi": round(rsi[i], 1) if rsi[i] else None, "atr": round(a, 1) if a else None,
            "adx": round(adxv, 1) if adxv is not None else None,
            "ema_trend": round(ema_t, 1), "filtrado": filtrado,
            "side": side, "sl": sl, "tp": tp}


def evaluate(h):
    o, hi, lo, c = fetch_closed(h)
    if len(c) < BB_LEN + 2:
        sys.exit("Pocas velas para calcular.")
    return signal_last(o, hi, lo, c)


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
          f"RSI={sig['rsi']} ATR={sig['atr']} ADX={sig['adx']} EMA{EMA_TREND}={sig['ema_trend']}")
    if sig["side"]:
        print(f"  >> SENAL {sig['side']}  (salida: TRAILING 1.5xATR sin TP)")
    elif sig.get("filtrado"):
        print(f"  >> senal DESCARTADA por filtro direccional (ADX>={ADX_MIN} contra tendencia mayor)")
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
    # SALIDA por TRAILING NATIVO ADAPTATIVO = TRAIL_ATR x ATR (constante de modulo, ver arriba),
    # SIN TP. Historial: 18pts fijo (28-ago) -> 1.5xATR -> 1.0 -> 1.5 -> 2.0 (18-sep, sobre 60d de
    # Yahoo: ARTEFACTO) -> 5.0 (19-sep, sobre 300d REALES de capital.com). El backtester lee la
    # misma constante (backtest_real.py --bollinger --source capital) -> no puede desincronizarse.
    trail_pts = round(TRAIL_ATR * sig["atr"], 1)
    snap = cc.get(h, f"/api/v1/markets/{EPIC}").json().get("snapshot", {})
    entry = snap.get("offer") if sig["side"] == "BUY" else snap.get("bid")
    body = {"epic": EPIC, "direction": sig["side"], "size": SIZE,
            "trailingStop": True, "stopDistance": trail_pts}
    r = cc.post(h, "/api/v1/positions", body)
    if r.status_code not in (200, 201):
        # FALLBACK: si el trailing nativo falla, entrar con stop fijo (SL_MULT x ATR) + TP fijo.
        print(f"  Trailing POST fallo ({r.status_code}): {r.text} -> reintento con stop fijo")
        a = sig["atr"]
        if sig["side"] == "BUY":
            sl = round(entry - SL_MULT * a, 1); tp = round(entry + TP_MULT * a, 1)
        else:
            sl = round(entry + SL_MULT * a, 1); tp = round(entry - TP_MULT * a, 1)
        r = cc.post(h, "/api/v1/positions",
                    {"epic": EPIC, "direction": sig["side"], "size": SIZE, "stopLevel": sl, "profitLevel": tp})
        if r.status_code not in (200, 201):
            print(f"  Orden NO colocada ({r.status_code}): {r.text} -> se reintenta en la proxima vela.")
            return
    ref = r.json().get("dealReference")
    conf = cc.get(h, f"/api/v1/confirms/{ref}").json()
    print(f"  ORDEN COLOCADA: {sig['side']} {SIZE} {EPIC} @ {entry} TRAILING {trail_pts}pts ({TRAIL_ATR}xATR) sin TP "
          f"ref={ref} status={conf.get('dealStatus')}")


if __name__ == "__main__":
    main()
