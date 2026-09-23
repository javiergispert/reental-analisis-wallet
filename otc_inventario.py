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
    """(balances, últimos envíos, fecha de lectura) de la wallet de custodia.

      - balances  {token_address: {"nombre", "id", "saldo", "divisa", …}}
        Los aTokens de Aave se consolidan sobre el token subyacente: un token
        colateralizado sigue siendo del mismo proyecto.
      - envíos    {token_address: [{"hash", "to", "value", "ts"}]}

    Etherscan corta `tokentx` en 1.000 resultados, así que se pagina: sin eso
    los saldos se calculaban solo con los movimientos más antiguos.
    """
    txs = fetch_all_token_txs(wallet, api_key)
    conocidas = set(project_by_addr.keys())
    nombre_a_addr = {row["nombre"].lower(): addr for addr, row in project_by_addr.items()
                     if row.get("nombre")}

    brutos, mapa_atoken, envios = {}, {}, {}
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
    return resultado, envios, datetime.now(timezone.utc)


def disponibles_reental(balances: dict, reservas: list) -> dict:
    """El stock propio menos lo ya comprometido.

    Las reservas contra ofertas de TERCEROS salen de la wallet del inversor que
    publicó, no del inventario de Reental: contarlas aquí hacía aparecer el
    stock propio mermado por tokens que nunca fueron suyos. Las reservas
    antiguas no llevan `tipo_origen` y son todas de Reental.
    """
    reservado = {}
    for r in (reservas or []):
        if r.get("estado") in ("completada", "cancelada"):
            continue
        if r.get("tipo_origen") == "tercero":
            continue
        addr = (r.get("token_address") or "").lower()
        reservado[addr] = reservado.get(addr, 0.0) + float(r.get("n_tokens", 0) or 0)
    return {addr: {**d,
                   "reservado": reservado.get(addr, 0.0),
                   "disponible": max(0.0, d["saldo"] - reservado.get(addr, 0.0))}
            for addr, d in (balances or {}).items()}


def catalogo(balances: dict, reservas: list, ofertas: list,
             api_key: str, en_wallet_fn) -> dict:
    """Lo comprometible hoy de cada proyecto, por dirección de token.

    Devuelve {token_address: {"id", "nombre", "reental", "terceros", "total"}},
    donde `terceros` es la lista de ofertas vivas con su disponible. Un
    proyecto puede tener stock propio, ofertas de terceros o ambas cosas.
    """
    propio = disponibles_reental(balances, reservas)
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
        est = _saldos.estado_oferta(o, reservas, api_key, en_wallet_fn)
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
