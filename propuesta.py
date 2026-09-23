"""
El cálculo de una propuesta de cartera: agregados, reparto y escenarios.

Sin Streamlit y sin red, para que se pueda probar contra propuestas reales.
Lo que entra son proyectos del maestro con un número de tokens; lo que sale
son las cifras que van al documento.

DOS DECISIONES QUE SE APARTAN DE LA PLANTILLA ACTUAL
----------------------------------------------------
1. La rentabilidad de la cartera se pondera por IMPORTE INVERTIDO, no por
   número de tokens. La hoja pondera por tokens, lo cual funciona mientras
   todos valgan 100, pero un token de 100 € y otro de 100 $ no son la misma
   inversión: hoy el europeo pesa un 14% más. Con la mezcla habitual la
   diferencia es de dos décimas, y crece con ella.

2. Se dan DOS rentabilidades del escenario con reinversión: sobre la cartera
   inmobiliaria —que es la que compara estatus entre sí— y sobre el capital
   total desplegado, que incluye lo que cuesta adquirir el estatus. La hoja
   solo publica la primera, y eso infla el salto: la diferencia entre pagar
   por SuperReentel y no pagarlo sale casi tres veces mayor de lo que es.
   El RNT no se consume —se conserva y además se stakea—, así que tampoco es
   un gasto: por eso se muestran las dos y se explica cada una.
"""
from __future__ import annotations

from collections import defaultdict

import maestro

MESES_ESCENARIO = (6, 12, 24, 36)


# ─── Importes ────────────────────────────────────────────────────────────────

def importe_proyecto(proy: dict, tokens: float, eurusd: float) -> dict:
    """Lo que cuesta esa posición, en las dos divisas.

    `eurusd` son dólares por euro. El precio de emisión está en la divisa del
    proyecto, así que se convierte hacia la otra, nunca al revés."""
    pe = proy.get("precio_emision") or 0.0
    nativo = tokens * pe
    if proy.get("divisa") == "EUR":
        eur = nativo
        usd = nativo * eurusd if eurusd else None
    else:
        usd = nativo
        eur = nativo / eurusd if eurusd else None
    return {"eur": eur, "usd": usd, "nativo": nativo, "divisa": proy.get("divisa", "USD")}


def coste_estatus(rnts: float, precio_rnt: float, eurusd: float,
                  ya_tiene: float = 0.0) -> dict:
    """Lo que cuesta alcanzar un estatus, descontando el RNT que ya se posee.

    Si el inversor ya llega al umbral no hay nada que comprar. Cobrarle otra
    vez la adquisición completa de un estatus que ya tiene es el error más caro
    que puede cometer una propuesta, y es el que comete cualquier plantilla que
    no sepa qué hay en su wallet.
    """
    faltan = max(0.0, (rnts or 0.0) - (ya_tiene or 0.0))
    usd = faltan * (precio_rnt or 0.0)
    return {"usd": usd, "eur": usd / eurusd if eurusd else None,
            "rnts": faltan, "rnts_umbral": rnts or 0.0, "rnts_ya": ya_tiene or 0.0}


def estatus_actual(rnt_en_cartera: float, umbrales: dict) -> str:
    """El estatus que ya tiene alguien con ese RNT, contando lo que está en
    staking: bloquearlo no lo hace desaparecer."""
    alcanzados = [n for n, _ in maestro.ESTATUS
                  if (rnt_en_cartera or 0.0) + 1e-9 >= (umbrales.get(n) or 0.0)]
    return alcanzados[-1] if alcanzados else maestro.ESTATUS[0][0]


# ─── Cartera ─────────────────────────────────────────────────────────────────

def construir(seleccion: list, eurusd: float) -> dict:
    """Agrega la cartera propuesta.

    `seleccion` son tuplas (proyecto, nº de tokens) o, cuando se parte de una
    cartera existente, (proyecto, nº de tokens propuestos, nº que ya tiene).
    En ese caso cada línea guarda además la variación, que es lo que de verdad
    hay que comprar o vender: una propuesta puede no traer ningún inmueble
    nuevo y consistir solo en mover cantidades de los que ya están.
    """
    lineas, tot_eur, tot_usd, tot_tokens = [], 0.0, 0.0, 0.0
    hay_actuales = False
    for item in seleccion:
        proy, tokens = item[0], item[1]
        # Un proyecto NUEVO dentro de una ampliación llega con `None`: no es que
        # falte el dato, es que hoy tiene cero. Se conserva la distinción
        # porque el modo sí lleva cartera previa aunque esta línea no la tenga.
        if len(item) > 2:
            hay_actuales = True
            actuales = float(item[2] or 0.0)
        else:
            actuales = None
        if tokens is None or tokens < 0:
            continue
        if not tokens and not actuales:
            continue
        imp = importe_proyecto(proy, tokens, eurusd)
        linea = {"proyecto": proy, "tokens": float(tokens), "importe": imp,
                 "tokens_actuales": actuales}
        if actuales is not None:
            linea["delta"] = float(tokens) - actuales
            linea["importe_delta"] = importe_proyecto(proy, linea["delta"], eurusd)
        lineas.append(linea)
        tot_eur += imp["eur"] or 0.0
        tot_usd += imp["usd"] or 0.0
        tot_tokens += float(tokens)

    if not lineas:
        return {"lineas": [], "n": 0, "tokens": 0.0, "eur": 0.0, "usd": 0.0,
                "meses_medios": 0.0, "rentabilidad": {}, "reparto": {},
                "con_cartera_previa": hay_actuales, "delta_eur": 0.0, "delta_tokens": 0.0}

    # El peso de cada línea es su importe en una divisa única, no su número de
    # tokens: es lo que de verdad ha puesto el inversor en cada proyecto.
    for l in lineas:
        l["peso"] = (l["importe"]["eur"] or 0.0) / tot_eur if tot_eur else 0.0

    rent = {}
    for nombre, suf in maestro.ESTATUS:
        rent[nombre] = {
            campo: sum(l["peso"] * (maestro.rentabilidad(l["proyecto"], suf, campo) or 0.0)
                       for l in lineas)
            for campo in ("anual", "total", "recurr", "plusvalia")
        }

    def reparto(clave, etiqueta=None):
        acc = defaultdict(float)
        for l in lineas:
            k = etiqueta(l["proyecto"]) if etiqueta else (l["proyecto"].get(clave) or "—")
            acc[k] += l["peso"]
        return dict(sorted(acc.items(), key=lambda kv: -kv[1]))

    # También ponderada por importe: una media simple da el mismo peso a un
    # proyecto de 500 € que a otro de 50.000 €.
    con_meses = [l for l in lineas if l["proyecto"].get("meses_pendientes") is not None]
    peso_meses = sum(l["peso"] for l in con_meses)

    delta_eur = sum((l["importe_delta"]["eur"] or 0.0) for l in lineas if "importe_delta" in l)
    return {
        "lineas": lineas,
        "n": len(lineas),
        "con_cartera_previa": hay_actuales,
        "delta_eur": delta_eur,
        "delta_tokens": sum(l.get("delta", 0.0) for l in lineas),
        "tokens": tot_tokens,
        "eur": tot_eur,
        "usd": tot_usd,
        "meses_medios": (sum(l["peso"] * l["proyecto"]["meses_pendientes"] for l in con_meses)
                         / peso_meses) if peso_meses else 0.0,
        "rentabilidad": rent,
        "reparto": {
            "ubicacion":  reparto("ubicacion"),
            "emision":    reparto("emision"),
            "divisa":     reparto("divisa"),
            "dividendo":  reparto("tipologia_dividendo"),
            "explotacion": reparto("tipologia_explotacion"),
        },
    }


# ─── Escenarios con reinversión ──────────────────────────────────────────────
#
# No se aplica la rentabilidad de la cartera durante tres años seguidos: cada
# proyecto tiene su propio calendario y su propia forma de pagar. Uno de renta
# mensual reparte desde el primer mes; uno de plusvalía no paga nada hasta que
# vende. Aplanar eso sobre el horizonte adelanta dinero que todavía no existe,
# y en una cartera con proyectos que vencen a los 7 meses y a los 30 la
# diferencia no es menor.
#
# Se modela el flujo de caja de cada proyecto y se reinvierte lo cobrado a la
# tasa que fije el asesor. La ganancia es el patrimonio final menos el capital
# puesto, que es la única definición que no depende de dónde se mire.

def flujos(linea: dict, sufijo: str, horizonte: int) -> list:
    """(mes, importe) de todo lo que cobra esa posición hasta el horizonte.

    La renta recurrente del maestro es ANUALIZADA y se cobra mes a mes; la
    plusvalía es un porcentaje TOTAL sobre la vida del proyecto y se cobra al
    cerrarlo, junto con la devolución del principal.
    """
    proy = linea["proyecto"]
    capital = linea["importe"]["eur"] or 0.0
    if not capital:
        return []
    fin = proy.get("meses_pendientes")
    fin = int(round(fin)) if fin else horizonte
    recurr = maestro.rentabilidad(proy, sufijo, "recurr") or 0.0
    plusval = maestro.rentabilidad(proy, sufijo, "plusvalia") or 0.0

    movimientos = []
    for mes in range(1, min(fin, horizonte) + 1):
        if recurr:
            movimientos.append((mes, capital * recurr / 12.0))
    if fin <= horizonte:
        movimientos.append((fin, capital * plusval + capital))   # plusvalía + principal
    return movimientos


def patrimonio(cartera: dict, sufijo: str, tasa_reinversion: float, horizonte: int) -> float:
    """Patrimonio al final del horizonte reinvirtiendo todo lo que se cobra."""
    total = 0.0
    for linea in cartera.get("lineas", []):
        capital = linea["importe"]["eur"] or 0.0
        fin = linea["proyecto"].get("meses_pendientes")
        fin = int(round(fin)) if fin else horizonte
        for mes, importe in flujos(linea, sufijo, horizonte):
            meses_restantes = horizonte - mes
            total += importe * ((1 + tasa_reinversion / 12.0) ** meses_restantes
                                if tasa_reinversion else 1.0)
        if fin > horizonte:
            total += capital          # sigue invertido: se cuenta a valor de entrada
    return total


def escenarios(cartera: dict, tasas: dict, costes: dict,
               tasa_staking: float = 0.0, meses=MESES_ESCENARIO) -> list:
    """Una fila por estatus con la ganancia y las dos rentabilidades.

    `tasas` es {estatus: tasa anual a la que se reinvierte lo cobrado} y
    `costes` {estatus: coste de adquirir el estatus, en €}.

    La rentabilidad «sobre cartera» es la que compara estatus entre sí; la
    «sobre capital total» incluye lo que cuesta el estatus y es la que de
    verdad obtiene quien lo compra.
    """
    capital = cartera.get("eur") or 0.0
    filas = []
    for nombre, suf in maestro.ESTATUS:
        tasa = tasas.get(nombre) or 0.0
        coste = costes.get(nombre) or 0.0
        fila = {"estatus": nombre, "tasa": tasa, "coste_estatus": coste, "puntos": []}
        for m in meses:
            gan = patrimonio(cartera, suf, tasa, m) - capital
            # El RNT del estatus no se gasta: se conserva y además se stakea.
            stk = coste * ((1 + tasa_staking / 12.0) ** m - 1) if (coste and tasa_staking) else 0.0
            fila["puntos"].append({
                "meses": m,
                "ganancia": gan,
                "ganancia_con_staking": gan + stk,
                "sobre_cartera": gan / capital if capital else 0.0,
                "sobre_total": (gan + stk) / (capital + coste) if (capital + coste) else 0.0,
            })
        filas.append(fila)
    return filas


# ─── Track record ────────────────────────────────────────────────────────────

def track_record(proyectos: list) -> dict:
    """Lo que pasó con los proyectos ya cerrados: si cumplieron la
    rentabilidad estimada y si cerraron cuando dijeron que cerrarían."""
    cerrados = [p for p in proyectos
                if p.get("estado", "").upper() == "CERRADO" and p.get("real_anual_rnt") is not None]
    if not cerrados:
        return {"n": 0, "proyectos": []}

    filas = []
    for p in cerrados:
        est, real = p.get("est_anual_rnt"), p.get("real_anual_rnt")
        f_est, f_real = p.get("fecha_fin_estimada"), p.get("fecha_fin_real")
        desv_meses = ((f_real - f_est).days / 30.44) if (f_est and f_real) else None
        filas.append({
            "id": p["label"], "nombre": p.get("nombre", ""), "ubicacion": p.get("ubicacion", ""),
            "tipologia": p.get("tipologia_dividendo", ""),
            "fecha_estimada": f_est, "fecha_real": f_real, "desv_meses": desv_meses,
            "est_rnt": est, "real_rnt": real,
            "var_rnt": (real - est) if (est is not None and real is not None) else None,
            "est_sr": p.get("est_anual_sr"), "real_sr": p.get("real_anual_sr"),
            "motivo": p.get("motivo_cierre", ""),
        })
    filas.sort(key=lambda f: f["fecha_real"] or f["fecha_estimada"] or "", reverse=True)

    con_var = [f for f in filas if f["var_rnt"] is not None]
    con_plazo = [f for f in filas if f["desv_meses"] is not None]
    medias = lambda campo: (sum(f[campo] for f in filas if f[campo] is not None)
                            / max(1, sum(1 for f in filas if f[campo] is not None)))
    return {
        "n": len(filas),
        "proyectos": filas,
        "pct_cumplieron": (100.0 * sum(1 for f in con_var if f["var_rnt"] >= 0) / len(con_var)
                           if con_var else 0.0),
        "pct_en_plazo": (100.0 * sum(1 for f in con_plazo if f["desv_meses"] <= 0) / len(con_plazo)
                         if con_plazo else 0.0),
        "media_est_rnt": medias("est_rnt"),
        "media_real_rnt": medias("real_rnt"),
        "media_est_sr": medias("est_sr"),
        "media_real_sr": medias("real_sr"),
    }
