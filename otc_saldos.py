"""
Saldo real de un inversor y disponibilidad de una oferta OTC — ÚNICA fuente de verdad.

Lo consumen `pages/02_OTC.py` (tabla de ofertas, selector y validación de reservas)
y `pages/03_Analisis_P2P.py` (ranking de oportunidades). Vive aquí porque las dos
páginas deben responder lo mismo a la pregunta «¿cuántos tokens de esta oferta se
pueden vender de verdad?»: cuando P2P tuvo su propia lectura del Sheet acabó
mostrando saldos brutos durante semanas, y duplicar este cálculo repetiría el error.

Dos ideas de fondo:

  * Los tokens COLATERALIZADOS en Aave cuentan como saldo. Siguen siendo del
    inversor y puede recuperarlos, así que para una venta OTC valen igual que los
    que tiene sueltos en la wallet. Ignorarlos marcaba en rojo ofertas válidas.

  * Manda la cifra MENOR entre lo ofertado y lo que el inversor tiene. Antes
    prevalecía siempre lo publicado, así que una oferta seguía apareciendo entera
    aunque el inversor ya hubiera vendido sus tokens por otra vía.

  * Una oferta NO sigue vendiendo lo que ya entregó. Lo entregado se mide en la
    CADENA —transferencias de la wallet de la oferta a la custodia de Reental
    desde que se publicó—, no en el registro de reservas: una entrega puede
    anotarse mal o no anotarse, y entonces la oferta seguía ofreciendo tokens
    que ya habían salido de la wallet, y encima marcaba al inversor en rojo por
    no conservarlos.

API pública:
    saldo_efectivo(wallet, token, api_key, en_wallet_fn) -> dict
    reservado_de_oferta(oferta_id, reservas) -> float
    entregado_de_oferta(oferta_id, reservas) -> float
    publicada_en(oferta) -> int
    estado_oferta(oferta, reservas, api_key, en_wallet_fn, recibido,
                  movimientos_fn, custodia) -> dict
"""
from __future__ import annotations

import streamlit as st

import aave_lend as _al

from datetime import datetime, timezone

# Pool de Aave del mercado de colateral inmobiliario de Reental.
AAVE_POOL = "0x67dc8037db6309dd5571d82c65f5f593f7da1505"

SEL_BALANCE_OF = "0x70a08231"
_TTL_SALDO  = 3600
_TTL_ATOKEN = 86400


@st.cache_data(show_spinner=False, ttl=_TTL_ATOKEN)
def atoken_de(token_address: str, api_key: str) -> str:
    """Dirección del aToken con el que Aave representa el colateral de este token.

    Se pregunta al pool (`getReserveData`) en vez de mantener una tabla: los
    proyectos nuevos entran solos. La respuesta se valida comprobando que ese
    aToken declara como subyacente el token consultado, así que un cambio en el
    orden de los campos del struct se detecta en vez de devolver basura.

    Devuelve "" si el proyecto no está listado en el pool, que NO es un error:
    simplemente no puede haber colateral.
    """
    if not api_key or not token_address:
        return ""
    res = _al.eth_call(AAVE_POOL, _al.SEL_GET_RESERVE_DATA + _al._addr_arg(token_address), api_key)
    if len(res) < 66:
        return ""
    palabras = [res[2:][i:i + 64] for i in range(0, len(res[2:]), 64)]
    if len(palabras) < 9:
        return ""
    atoken = "0x" + palabras[8][-40:]
    try:
        if int(atoken, 16) == 0:
            return ""
    except (TypeError, ValueError):
        return ""
    und = _al.underlying_asset(atoken, api_key)
    return atoken if und and und[-40:] == token_address.lower()[2:] else ""


@st.cache_data(show_spinner=False, ttl=_TTL_SALDO)
def colateral_de(wallet: str, token_address: str, api_key: str) -> float:
    """Tokens del proyecto depositados como garantía en Aave. -1.0 si falla la
    consulta; 0.0 si el proyecto no está listado (que no es un fallo)."""
    if not api_key:
        return -1.0
    atoken = atoken_de(token_address, api_key)
    if not atoken:
        return 0.0
    res = _al.eth_call(atoken, SEL_BALANCE_OF + _al._addr_arg(wallet), api_key)
    if not res:
        return -1.0
    if res == "0x":
        return 0.0
    try:
        return round(int(res, 16) / 1e18, 6)
    except (TypeError, ValueError):
        return -1.0


def saldo_efectivo(wallet: str, token_address: str, api_key: str, en_wallet_fn) -> dict:
    """Tokens que el inversor puede vender: los sueltos en su wallet MÁS los
    colateralizados. `en_wallet_fn(wallet, token, api_key)` la aporta el llamante
    porque cada página ya tiene su lectura cacheada de transferencias.

    `ok=False` significa que la cadena no se pudo consultar; nunca debe tratarse
    como cero. `motivo` dice QUÉ falló: un saldo en blanco sin explicación es
    indistinguible de «no tiene nada» y no da por dónde empezar a mirar.
    """
    w = (wallet or "").strip().lower()
    vacio = {"ok": False, "en_wallet": 0.0, "colateral": 0.0, "total": 0.0}
    if not (w.startswith("0x") and len(w) == 42):
        return {**vacio, "motivo": f"La wallet registrada en la oferta no es válida: «{wallet}»."}
    if not api_key:
        return {**vacio, "motivo": "Falta ETHERSCAN_API_KEY en la configuración del servidor."}

    en_wallet = en_wallet_fn(w, token_address, api_key)
    colateral = colateral_de(w, token_address, api_key)
    fallos = []
    if en_wallet is None or en_wallet < 0:
        fallos.append("el saldo en la wallet")
    if colateral < 0:
        fallos.append("el colateral en Aave")
    if fallos:
        return {**vacio, "motivo": f"No se pudo leer {' ni '.join(fallos)} en la cadena."}
    return {"ok": True, "en_wallet": max(0.0, en_wallet), "colateral": colateral,
            "total": round(max(0.0, en_wallet) + colateral, 6), "motivo": ""}


def reservado_de_oferta(oferta_id: str, reservas: list) -> float:
    """Tokens ya comprometidos contra una oferta concreta de tercero."""
    return sum(float(r.get("n_tokens", 0)) for r in (reservas or [])
               if r.get("estado") not in ("completada", "cancelada")
               and r.get("tipo_origen") == "tercero"
               and r.get("oferta_id") == oferta_id)


def publicada_en(oferta: dict) -> int:
    """Instante (unix UTC) en que se publicó la oferta, leído de su id.

    Los ids tienen la forma `OFR-AAAAMMDDHHMMSS`, que es la única fuente del
    dato: el campo `fecha` existe en el registro pero está vacío en las 24
    ofertas del almacén, así que fiarse de él sería fiarse de un None.

    Devuelve 0 si no se puede leer. Un 0 hace que se cuente TODO lo que esa
    wallet haya mandado nunca a la custodia, lo que descuenta de más y deja la
    oferta corta: el error cae del lado seguro, que es el único aceptable aquí.
    """
    ident = str(oferta.get("id") or "")
    marca = ident.split("-")[-1]
    if len(marca) != 14 or not marca.isdigit():
        return 0
    try:
        return int(datetime.strptime(marca, "%Y%m%d%H%M%S")
                   .replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0


def entregado_de_oferta(oferta_id: str, reservas: list) -> float:
    """Tokens de esta oferta que YA se entregaron al comprador.

    Son los de sus reservas completadas. Se lee `tokens_enviados` cuando existe
    —un envío pudo ser parcial— y se cae a lo reservado si no consta.

    Existe porque una oferta publicada NO se decrementa al cumplirse una reserva:
    sigue diciendo que vende lo mismo que el primer día. Comparar ese número con
    el saldo del inversor después de que haya entregado parte es compararlo con
    una cifra que ya no describe nada, y marcarlo en rojo por «faltan tokens»
    cuando lo que pasó es que cumplió.
    """
    total = 0.0
    for r in (reservas or []):
        if r.get("estado") != "completada" or r.get("oferta_id") != oferta_id:
            continue
        env = r.get("tokens_enviados")
        total += float(env if env is not None else (r.get("n_tokens") or 0))
    return round(total, 6)


def estado_oferta(oferta: dict, reservas: list, api_key: str, en_wallet_fn,
                  recibido: float = 0.0, movimientos_fn=None, custodia: str = "") -> dict:
    """Estado real de una oferta de tercero cruzando lo publicado con la cadena.

    `recibido` son los tokens de esta oferta que YA están en la wallet de
    custodia de Reental. Sin ese dato, en cuanto el inversor envía los tokens
    su saldo cae a cero y la oferta se marcaba en rojo —«faltan 400»— como si
    hubiera incumplido, justo cuando acababa de hacer lo que tenía que hacer.
    Lo enviado sigue respaldando la oferta: cambia de sitio, no desaparece.
    """
    n_publicado = float(oferta.get("n_tokens", 0) or 0)

    # Cuánto ha entregado ya el inversor por esta oferta. Se mira la CADENA, no
    # el registro de reservas: una entrega puede anotarse mal —ha pasado, como
    # reserva de stock propio de Reental en vez de contra la oferta— o no
    # anotarse, y entonces la oferta sigue vendiendo lo que ya salió de la
    # wallet. Lo que de verdad ocurrió está en la cadena.
    #
    # Se toma el MAYOR de las dos lecturas. Si una entrega consta por reserva
    # pero no llegó a la custodia, igual se descuenta. Descontar de más deja la
    # oferta corta; descontar de menos la deja vendiendo tokens que ya no están.
    # Entre quedarse corto y comprometer dos veces, siempre corto.
    recibido  = max(0.0, float(recibido or 0.0))
    entregado = entregado_de_oferta(oferta.get("id"), reservas)
    mov = None
    if movimientos_fn is not None and custodia:
        mov = movimientos_fn(oferta.get("wallet_inversor", ""),
                             (oferta.get("token_address") or "").lower(),
                             custodia, publicada_en(oferta))
        if mov.get("ok"):
            # A lo que ve la cadena se le resta `recibido`: son las entregas que
            # corresponden a reservas TODAVÍA VIVAS, y de esas ya se ocupa el
            # cálculo de abajo —suman al respaldo y restan como reservado—. Sin
            # esta resta se descontarían dos veces y la oferta aparecería más
            # corta de lo que es justo cuando el inversor acaba de cumplir.
            entregado = max(entregado, float(mov.get("entregado") or 0.0) - recibido)
    entregado = max(0.0, round(entregado, 6))
    # Lo que la oferta todavía puede vender. Lo entregado ya salió de la wallet
    # del inversor, así que exigirle que lo conserve es exigirle lo imposible.
    n_oferta  = max(0.0, round(n_publicado - entregado, 6))
    # El saldo sale de la MISMA lectura que el entregado cuando la hay: las dos
    # cifras se restan entre sí y tienen que venir de la misma foto.
    sal       = saldo_efectivo(oferta.get("wallet_inversor", ""),
                               (oferta.get("token_address") or "").lower(), api_key,
                               (lambda *_a, **_k: mov["saldo"]) if (mov and mov.get("ok")) else en_wallet_fn)
    reservado = reservado_de_oferta(oferta.get("id"), reservas)

    if not sal["ok"]:
        # Sin lectura fiable no se afirma nada: ni verde ni rojo.
        return {"ok": False, "alerta": "⚠️", "en_wallet": 0.0, "colateral": 0.0,
                "saldo_real": 0.0, "reservado": reservado, "disponible": 0.0,
                "recibido": recibido, "respaldo": 0.0, "entregado": entregado,
                "n_vivo": n_oferta, "publicado": n_publicado,
                "motivo": sal.get("motivo", "No se pudo consultar la cadena.")}

    # Lo que respalda la oferta es lo que el inversor conserva MÁS lo que ya ha
    # entregado a la custodia por esta misma oferta.
    respaldo   = min(n_oferta, sal["total"] + recibido)
    disponible = max(0.0, respaldo - reservado)
    falta      = n_oferta - sal["total"] - recibido

    if n_oferta <= 0.001:
        alerta = "⚪"
        motivo = (f"La oferta ya se ha entregado entera ({entregado:,.3f} de "
                  f"{n_publicado:,.3f}). No queda nada que vender: conviene cerrarla.")
    elif falta > 0.001:
        alerta = "🔴"
        _pend = (f" (de los {n_publicado:,.3f} publicados ya entregó {entregado:,.3f},"
                 f" así que la oferta viva es de {n_oferta:,.3f})" if entregado > 0.001 else "")
        motivo = (f"El inversor tiene {sal['total']:,.3f} tokens y la oferta es de "
                  f"{n_oferta:,.3f}: faltan {falta:,.3f}.{_pend}")
        if recibido > 0.001:
            motivo += f" ({recibido:,.3f} ya están en la custodia.)"
    elif recibido > 0.001:
        alerta = "🔵"
        motivo = (f"{recibido:,.3f} tokens ya están en la wallet OTC, pendientes de "
                  "entregar al comprador. El saldo del inversor baja por eso, no "
                  "porque haya incumplido.")
    elif disponible <= 0.001:
        alerta = "🟡"
        motivo = "La oferta está íntegramente reservada."
    else:
        alerta = "🟢"
        motivo = ""
    return {"ok": True, "alerta": alerta, "en_wallet": sal["en_wallet"],
            "colateral": sal["colateral"], "saldo_real": sal["total"],
            "reservado": reservado, "disponible": disponible,
            "recibido": recibido, "respaldo": respaldo,
            "entregado": entregado, "n_vivo": n_oferta, "publicado": n_publicado,
            "motivo": motivo}
