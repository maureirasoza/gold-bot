#!/usr/bin/env python3
"""MODO SOMBRA IA (9-oct-2026): opinion de Claude sobre cada entrada de los bots, SIN tocar la operacion.

Corre como paso SEPARADO del workflow, DESPUES del bot: lee la salida del bot (bot.log); si el bot coloco una
orden en esta corrida, le pasa a Claude la senal + las ultimas velas y le pide que busque en la web el contexto
del momento (noticias, datos economicos, sentimiento) y diga si ESA entrada le parece buena o no. El veredicto
solo se REGISTRA (linea 'SOMBRA_IA {json}' + archivo sombra_ia.jsonl que el workflow sube como artifact).
La orden ya esta colocada: la IA no puede abrir, cerrar ni modificar nada.

Tras unas semanas, sombra_eval.py cruza los veredictos con el resultado real de cada operacion para medir si
la IA distingue las buenas entradas de las malas (la unica forma honesta de probarla: no hay backtest posible).

Sin ANTHROPIC_API_KEY (secret del repo) -> no hace nada. Cualquier error -> se registra y sale con codigo 0.
Uso (en el workflow): python sombra_ia.py BOT bot.log
"""
import sys, os, re, json, time
from datetime import datetime, timezone

MODELO = os.environ.get("SOMBRA_MODELO", "claude-opus-5-5")
MAX_BUSQUEDAS = 3

# Que hace cada bot (para que la IA entienda la logica de la entrada que esta juzgando)
BOTS = {
    "sp500":   ("US500", "MINUTE_15", "S&P 500. Reversion a la media en velas de 15 min: entra cuando el precio cierra "
                "fuera de su banda de Bollinger (20, 2) en la direccion de un exceso, esperando que REBOTE hacia la media. "
                "Salida solo por stop dinamico (trailing) de 4 x ATR; sin objetivo fijo."),
    "us30":    ("US30", "MINUTE_15", "Dow Jones 30. Reversion a la media 15 min: banda de Bollinger (20, 2) + RSI(14) "
                "bajo 35 (compra) o sobre 65 (venta), esperando un rebote. Trailing 5 x ATR, sin objetivo fijo."),
    "us100":   ("US100", "MINUTE_15", "Nasdaq 100. Reversion a la media 15 min: Bollinger (20, 2) + RSI 35/65, esperando "
                "un rebote. Trailing 5 x ATR, sin objetivo fijo."),
    "rty":     ("RTY", "MINUTE_15", "Russell 2000. Reversion a la media 15 min: Bollinger (20, 2) + RSI 35/65, esperando "
                "un rebote. Trailing 4 x ATR (se aprieta a 2 x ATR tras +14 x ATR de ganancia), sin objetivo fijo."),
    "nl25":    ("NL25", "MINUTE_15", "AEX Holanda 25. Reversion a la media 15 min: Bollinger (26, 1.75) + RSI 30/70, "
                "esperando un rebote. Trailing 2 x ATR, sin objetivo fijo."),
    "us30rf":  ("US30", "HOUR", "Dow Jones 30, velas de 1 hora. 'Ruptura fallida': el precio rompio el maximo (o minimo) "
                "de las ultimas 48 horas y en <= 2 velas volvio a cerrar dentro del rango; entra en CONTRA de la ruptura. "
                "Trailing 3 x ATR (se aprieta a 1.75 x ATR tras +3.5 x ATR), sin objetivo fijo."),
    "trend":   ("GOLD", "HOUR", "Oro, velas de 1 hora. Seguidor de tendencia: entra cuando el precio rompe el maximo "
                "(o minimo) de las ultimas 15 horas a favor de la EMA de 200 horas. Trailing 5 x ATR, sale por canal "
                "de 8 horas; agrega una 2a unidad (piramide) a +2 x ATR."),
    "crypto":  (None, "HOUR_4", "Bitcoin / Ethereum, velas de 4 horas. Ruptura por volatilidad: entra si la vela de 4h "
                "se mueve mas de 3 x ATR a favor de la EMA de 200. Trailing 2 x ATR, piramide a +2 x ATR."),
    "bollinger_oro": ("GOLD", "MINUTE_15", "Oro, velas de 15 min. Reversion a la media: Bollinger + RSI con filtro de "
                "tendencia (EMA200/ADX), esperando un rebote. Trailing 5 x ATR, 2a unidad a +3 x ATR."),
}

SYSTEM = (
    "Eres un analista de mercados que revisa, en tiempo real, las entradas de bots de trading automaticos (cuenta "
    "demo). Cada vez que un bot entra, recibes su logica, la senal y las ultimas velas. Tu trabajo: buscar en la web "
    "el contexto ACTUAL de ese instrumento (noticias de las ultimas horas, datos economicos publicados hoy o por "
    "publicarse en las proximas horas, eventos de bancos centrales, sentimiento) y opinar si ESTA entrada, con la "
    "logica de ESE bot, tiene buenas probabilidades de salir bien en las proximas horas.\n"
    "Se concreto y escueto. Usa como maximo unas pocas busquedas. No inventes noticias: si no encuentras nada "
    "relevante, dilo y opina con lo que muestran las velas. No hay respuesta 'correcta' por defecto: di NO_ENTRAR "
    "cuando el contexto vaya claramente en contra de la idea del bot, y ENTRAR cuando la favorezca o sea neutral.\n"
    "Termina SIEMPRE con exactamente estas tres lineas:\n"
    "VEREDICTO: ENTRAR o NO_ENTRAR\n"
    "CONFIANZA: un numero de 0 a 100\n"
    "MOTIVO: una frase de maximo 30 palabras"
)


def registrar(d):
    d.setdefault("t", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))
    print("SOMBRA_IA " + json.dumps(d, ensure_ascii=False))
    with open("sombra_ia.jsonl", "a") as f:      # una linea por orden (cripto/piramide pueden ser 2)
        f.write(json.dumps(d, ensure_ascii=False) + "\n")


def ordenes_en_log(texto):
    """Lineas de orden colocada en esta corrida (mercado o limite). Lista vacia si el bot no entro."""
    return [l.strip() for l in texto.splitlines()
            if re.search(r"ORDEN (COLOCADA|LIMITE)|\bCOLOCADA (BUY|SELL)\b", l) and "NO colocada" not in l]


def parse_orden(linea):
    c = re.match(r"(\w+): .*COLOCADA (BUY|SELL) ([\d.]+)", linea)          # formato cripto: 'BTCUSD: ... COLOCADA BUY 0.013'
    if c:
        return {"side": c.group(2), "size": float(c.group(3)), "epic": c.group(1), "precio": None}
    m = re.search(r"(BUY|SELL)\s+([\d.]+)\s+(\S+)\s+(?:a mercado\s+)?@?\s*([\d.]+)?", linea)
    if not m:
        return {}
    return {"side": m.group(1), "size": float(m.group(2)), "epic": m.group(3),
            "precio": float(m.group(4)) if m.group(4) else None}


def velas(epic, res, n=24):
    """Ultimas n velas (mid) de capital.com como texto compacto. Si falla, texto vacio."""
    try:
        import capital_client as cc
        h = cc.login()
        r = cc.get(h, f"/api/v1/prices/{epic}?resolution={res}&max={n}")
        out = []
        for p in r.json().get("prices", []):
            mid = lambda x: round((x["bid"] + x["ask"]) / 2, 2)
            out.append(f"{p['snapshotTimeUTC']} O{mid(p['openPrice'])} H{mid(p['highPrice'])} "
                       f"L{mid(p['lowPrice'])} C{mid(p['closePrice'])}")
        return "\n".join(out)
    except Exception as e:  # el contexto de velas es opcional
        return f"(no se pudieron bajar velas: {e})"


def preguntar(prompt):
    import anthropic
    client = anthropic.Anthropic()
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_BUSQUEDAS}]
    messages = [{"role": "user", "content": prompt}]
    uso = {"in": 0, "out": 0, "busquedas": 0}
    for _ in range(4):                                   # reanuda pause_turn como maximo 3 veces
        resp = client.beta.messages.create(
            model=MODELO, max_tokens=16000, system=SYSTEM, tools=tools, messages=messages,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        uso["in"] += resp.usage.input_tokens or 0
        uso["out"] += resp.usage.output_tokens or 0
        stu = getattr(resp.usage, "server_tool_use", None)
        uso["busquedas"] += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        if resp.stop_reason != "pause_turn":
            break
        messages = [{"role": "user", "content": prompt}, {"role": "assistant", "content": resp.content}]
    texto = "\n".join(b.text for b in resp.content if b.type == "text")
    return resp, texto, uso


def main():
    if len(sys.argv) < 3:
        sys.exit("uso: python sombra_ia.py BOT bot.log")
    bot, logf = sys.argv[1], sys.argv[2]
    try:
        log = open(logf, encoding="utf-8", errors="replace").read()
    except OSError:
        print("sombra IA: no hay log del bot -> nada que evaluar."); return
    ordenes = ordenes_en_log(log)
    if not ordenes:
        print("sombra IA: el bot no entro en esta corrida -> nada que evaluar."); return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("sombra IA: falta el secret ANTHROPIC_API_KEY -> omitido (el bot opero normal)."); return
    epic_def, res, logica = BOTS.get(bot, (None, "MINUTE_15", "(bot sin descripcion)"))
    for linea in ordenes:
        o = parse_orden(linea); epic = o.get("epic") or epic_def
        base = {"bot": bot, "epic": epic, "side": o.get("side"), "size": o.get("size"), "precio": o.get("precio"),
                "orden": linea[:200], "modelo": MODELO}
        prompt = (f"Fecha y hora actual (UTC): {datetime.now(timezone.utc):%Y-%m-%d %H:%M}\n\n"
                  f"LOGICA DEL BOT:\n{logica}\n\n"
                  f"SALIDA DEL BOT EN ESTA CORRIDA (incluye la senal y la orden que acaba de colocar):\n{log[-3000:]}\n\n"
                  f"ULTIMAS VELAS {epic} ({res}, precio medio, UTC):\n{velas(epic, res) if epic else '(n/d)'}\n\n"
                  f"La orden es: {o.get('side')} {epic}. Busca el contexto actual y da tu veredicto.")
        t0 = time.time()
        try:
            resp, texto, uso = preguntar(prompt)
            ver = re.search(r"VEREDICTO:\s*(ENTRAR|NO_ENTRAR)", texto)
            con = re.search(r"CONFIANZA:\s*(\d+)", texto)
            mot = re.search(r"MOTIVO:\s*(.+)", texto)
            costo = uso["in"] * 4e-6 + uso["out"] * 20e-6 + uso["busquedas"] * 0.01   # tarifas Opus 5.5 + busqueda
            registrar({**base, "veredicto": ver.group(1) if ver else None,
                       "confianza": int(con.group(1)) if con else None,
                       "motivo": mot.group(1).strip()[:300] if mot else None,
                       "stop_reason": resp.stop_reason, "modelo_respuesta": resp.model,
                       "tokens_in": uso["in"], "tokens_out": uso["out"], "busquedas": uso["busquedas"],
                       "costo_usd_aprox": round(costo, 4), "segundos": round(time.time() - t0, 1)})
            print("---- analisis completo ----\n" + texto)
        except Exception as e:
            registrar({**base, "error": f"{type(e).__name__}: {str(e)[:300]}"})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:      # el modo sombra nunca debe marcar la corrida como fallida
        print(f"sombra IA: error inesperado {type(e).__name__}: {e}")
