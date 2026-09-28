"""
Qué hay disponible en OTC: el stock propio de Reental y las ofertas de terceros.

POR QUÉ ESTÁ AQUÍ
-----------------
La gestión OTC lo calculaba para su propia página. El constructor de
propuestas necesita exactamente lo mismo —cuántos tokens de cada proyecto se
pueden comprometer hoy— y copiarlo habría creado dos verdades que se separan
en cuanto una se toque. Es la regla del repositorio: lo que usan dos páginas
vive en un módulo común.

El catálogo de proyectos entra como parámetro en vez de leerse aquí: cada
página ya tiene el suyo y no hace falta una tercera lectura del maestro.
"""
from __future__ import annotations

from datetime import datetime, timezone

from reental_tokens import codigo_proyecto_atoken
from utils import fetch_all_account_txs, fetch_all_token_txs

import otc_saldos as _saldos


def saldo_en_wallet(wallet: str, token_address: str, api_key: str) -> float:
    """Tokens del proyecto que el inversor tiene sueltos en su wallet.

    Se apoya en `fetch_all_account_txs`, que pagina: la consulta directa a
    Etherscan se corta a 10.000 resultados y en una wallet con mucho histórico
    devolvía un saldo incompleto.
    """
    wallet = wallet.lower()
    token  = token_address.lower()
    try:
        txs = fetch_all_account_txs(wallet, api_key, action="tokentx",
                                    contractaddress=token)
    except Exception:
        return -1.0   # -1 indica error de consulta, NO saldo cero
    if txs is None:
        return -1.0
    bal = 0.0
    for tx in txs:
        dec   = int(tx.get("tokenDecimal") or 18)
        value = int(tx["value"]) / (10 ** dec)
        to_   = tx["to"].lower()
        from_ = tx["from"].lower()
        if to_ == wallet and from_ != wallet:
            bal += value
        elif from_ == wallet and to_ != wallet:
            bal -= value
    return round(bal, 6)


def balances_de_wallet(wallet: str, api_key: str, project_by_addr: dict,
                       project_by_id: dict) -> tuple:
    """(balances, envíos, entradas, fecha de lectura) de la wallet de custodia.

      - balances  {token_address: {"nombre", "id", "saldo", "divisa", …}}
        Los aTokens de Aave se consolidan sobre el token subyacente: un token
        colateralizado sigue siendo del mismo proyecto.
      - envíos    {token_address: [{"hash", "to", "value", "ts"}]}
      - entradas  {token_address: [{"hash", "from", "value", "ts"}]}

    Las entradas hacen falta para saber cuándo los tokens de una oferta de
    tercero han llegado a la custodia. Se registran aquí porque el bucle ya
    recorre cada transferencia y ya tiene el remitente, el importe y la fecha:
    no cuesta ni una llamada más.

    Etherscan corta `tokentx` en 1.000 resultados, así que se pagina: sin eso
    los saldos se calculaban solo con los movimientos más antiguos.
    """
    txs = fetch_all_token_txs(wallet, api_key)
    conocidas = set(project_by_addr.keys())
    nombre_a_addr = {row["nombre"].lower(): addr for addr, row in project_by_addr.items()
                     if row.get("nombre")}

    brutos, mapa_atoken, envios, entradas = {}, {}, {}, {}
    w = (wallet or "").lower()

    for tx in txs:
        contrato = tx["contractAddress"].lower()
        sym, nombre = tx.get("tokenSymbol", ""), tx.get("tokenName", "")
        dec = int(tx.get("tokenDecimal") or 18)
        valor = int(tx["value"]) / (10 ** dec)
        ts = int(tx.get("timeStamp", 0))
        to_addr, from_addr = tx["to"].lower(), tx["from"].lower()

        # La grafía del aToken varía entre proyectos (aMatReental-CME-1 frente a
        # aMatREENTAL-CAR-2), así que el reconocimiento ignora mayúsculas.
        sufijo = codigo_proyecto_atoken(sym, nombre)
        if sufijo and contrato not in mapa_atoken:
            s = sufijo.lower()
            subyacente = (project_by_id.get(s) or {}).get("token_address")
            if not subyacente:
                subyacente = next((a for n, a in nombre_a_addr.items() if s in n), None)
            if subyacente:
                mapa_atoken[contrato] = subyacente.lower()

        efectiva = mapa_atoken.get(contrato, contrato if contrato in conocidas else None)
        if efectiva is None:
            continue

        if to_addr == w and from_addr != w:
            brutos[efectiva] = brutos.get(efectiva, 0.0) + valor
            if contrato in conocidas:      # los aTokens no son compras
                entradas.setdefault(efectiva, []).append(
                    {"hash": tx["hash"], "from": from_addr, "value": valor, "ts": ts})
        elif from_addr == w and to_addr != w:
            brutos[efectiva] = brutos.get(efectiva, 0.0) - valor
            if contrato in conocidas:      # los aTokens no son envíos al inversor
                envios.setdefault(efectiva, []).append(
                    {"hash": tx["hash"], "to": to_addr, "value": valor, "ts": ts})
        # from == to == wallet: autotransferencia, saldo neto cero

    resultado = {}
    for addr, saldo in brutos.items():
        if saldo < 0.001:
            continue
        proj = project_by_addr.get(addr, {})
        fin = proj.get("fecha_fin")
        resultado[addr] = {
            "nombre": proj.get("nombre", addr[:12] + "…"),
            "id": proj.get("id", "—"),
            "saldo": round(saldo, 6),
            "divisa": proj.get("divisa", "EUR"),
            "precio_emision": proj.get("precio_emision") or 0,
            "ubicacion": proj.get("ubicacion", "—"),
            "estado": proj.get("estado", "—"),
            "fecha_fin": fin.strftime("%Y/%m") if fin else "—",
            "tipo_renta": proj.get("tipo_renta", "—"),
        }
    return resultado, envios, entradas, datetime.now(timezone.utc)


def llegadas_de_terceros(reservas: list, ofertas: list, entradas: dict) -> dict:
    """Cuántos tokens de cada reserva de tercero están YA en la custodia.

    Devuelve {id_de_reserva: tokens llegados}. Es un dato DERIVADO de la
    cadena, no un estado guardado: se recalcula en cada carga, así que no puede
    quedarse obsoleto ni hay nada que marcar a mano.

    POR QUÉ HACE FALTA
    ------------------
    Los tokens de un tercero no van directos al comprador: pasan por la wallet
    OTC. Durante esa escala aparecían como stock libre y otro comercial podía
    reservarlos por segunda vez. Y como además Reental paga al recibirlos, en
    ese momento ya son suyos: lo único que falta es entregarlos.

    POR QUÉ ESTAS CUATRO CONDICIONES Y NO «entró algo»
    --------------------------------------------------
    Porque a la custodia entran constantemente tokens que Reental compra para
    su propio libro —en el histórico hay 467 entradas desde 230 wallets
    distintas— y darlas todas por llegadas de reservas sería un disparate. Se
    exige que la transferencia sea de ESE token, desde LA wallet de la oferta
    de ESA reserva, posterior a la reserva y con una reserva viva detrás. Una
    compra propia no cumple la segunda ni la cuarta.

    El resto que llegue por encima de lo reservado NO se atribuye: si el
    tercero envía 400 y la reserva era de 100, los otros 300 son de Reental
    —ya pagados— y quedan como stock libre, que es lo correcto.
    """
    por_id = {o.get("id"): o for o in (ofertas or []) if o.get("id")}
    vivas = [r for r in (reservas or [])
             if r.get("tipo_origen") == "tercero"
             and r.get("estado") not in ("completada", "cancelada", "eliminada")
             and r.get("oferta_id") in por_id]
    # Más antigua primero: si dos reservas de la misma oferta compiten por una
    # llegada, la que se hizo antes tiene prioridad. Es un criterio arbitrario
    # pero estable; la página avisa cuando hay empate.
    vivas.sort(key=lambda r: str(r.get("fecha_reserva") or ""))

    consumido: dict = {}      # (token, wallet origen) -> ya atribuido
    salida: dict = {}
    for r in vivas:
        oferta = por_id[r["oferta_id"]]
        token = (r.get("token_address") or oferta.get("token_address") or "").lower()
        origen = (oferta.get("wallet_inversor") or "").lower()
        if not token or not origen:
            continue
        desde = _momento(r.get("fecha_reserva"))
        disponible = sum(
            e["value"] for e in (entradas.get(token) or [])
            if e["from"] == origen and (desde is None or e["ts"] >= desde))
        clave = (token, origen)
        disponible = max(0.0, disponible - consumido.get(clave, 0.0))
        atribuido = min(float(r.get("n_tokens", 0) or 0), disponible)
        if atribuido > 0.001:
            consumido[clave] = consumido.get(clave, 0.0) + atribuido
            salida[r.get("id")] = round(atribuido, 6)
    return salida


def _momento(fecha_str) -> float | None:
    """La fecha de una reserva como timestamp, o None si no se entiende.

    Sin fecha legible no se filtra por tiempo: es preferible atribuir de más
    —que bloquea una venta— a no detectar la llegada y permitir una reserva
    duplicada.
    """
    for formato in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(fecha_str), formato).replace(
                tzinfo=timezone.utc).timestamp()
        except (ValueError, TypeError):
            continue
    return None


def disponibles_reental(balances: dict, reservas: list, llegadas: dict | None = None) -> dict:
    """El stock propio menos lo ya comprometido.

    Una reserva descuenta cuando sus tokens están en esta wallet. Las de
    Reental, siempre. Las de tercero, solo la parte que ya haya LLEGADO, que
    viene en `llegadas` (ver `llegadas_de_terceros`).

    Antes se ignoraban por completo las de tercero, con el argumento de que
    esos tokens salían de la wallet del inversor y no del inventario propio.
    Era falso: en el proceso real pasan siempre por la custodia, y durante esa
    escala aparecían libres. Dos comerciales podían reservar los mismos.

    Las reservas antiguas no llevan `tipo_origen` y son todas de Reental.
    """
    llegadas = llegadas or {}
    reservado = {}
    for r in (reservas or []):
        if r.get("estado") in ("completada", "cancelada", "eliminada"):
            continue
        addr = (r.get("token_address") or "").lower()
        if r.get("tipo_origen") == "tercero":
            cantidad = float(llegadas.get(r.get("id"), 0.0) or 0.0)
        else:
            cantidad = float(r.get("n_tokens", 0) or 0)
        if cantidad:
            reservado[addr] = reservado.get(addr, 0.0) + cantidad
    return {addr: {**d,
                   "reservado": reservado.get(addr, 0.0),
                   "disponible": max(0.0, d["saldo"] - reservado.get(addr, 0.0))}
            for addr, d in (balances or {}).items()}


def catalogo(balances: dict, reservas: list, ofertas: list,
             api_key: str, en_wallet_fn, entradas: dict | None = None) -> dict:
    """Lo comprometible hoy de cada proyecto, por dirección de token.

    Devuelve {token_address: {"id", "nombre", "reental", "terceros", "total"}},
    donde `terceros` es la lista de ofertas vivas con su disponible. Un
    proyecto puede tener stock propio, ofertas de terceros o ambas cosas.
    """
    llegadas = llegadas_de_terceros(reservas, ofertas, entradas or {})
    propio = disponibles_reental(balances, reservas, llegadas)
    cat = {}
    for addr, d in propio.items():
        if d["disponible"] <= 0.001:
            continue
        cat[addr] = {"id": d.get("id", "—"), "nombre": d.get("nombre", ""),
                     "reental": d["disponible"], "terceros": [], "total": d["disponible"]}

    for o in (ofertas or []):
        if o.get("estado") != "activa":
            continue
        addr = (o.get("token_address") or "").lower()
        recibido = sum(v for rid, v in llegadas.items()
                       if any(r.get("id") == rid and r.get("oferta_id") == o.get("id")
                              for r in (reservas or [])))
        est = _saldos.estado_oferta(o, reservas, api_key, en_wallet_fn, recibido)
        # Sin lectura fiable de la cadena no se afirma que haya nada disponible:
        # una propuesta que ofrece tokens que no existen es peor que una corta.
        if not est.get("ok") or est["disponible"] <= 0.001:
            continue
        entrada = cat.setdefault(addr, {
            "id": o.get("proyecto_id", "—"), "nombre": o.get("proyecto_nombre", ""),
            "reental": 0.0, "terceros": [], "total": 0.0})
        entrada["terceros"].append({
            "oferta_id": o.get("id"), "inversor": o.get("inversor", ""),
            "wallet": o.get("wallet_inversor", ""),
            "disponible": est["disponible"],
            "precio": float(o.get("precio_venta") or 0.0),
            "divisa": o.get("divisa", "USD"),
        })
        entrada["total"] += est["disponible"]
    return cat
