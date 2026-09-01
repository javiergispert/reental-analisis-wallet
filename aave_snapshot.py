"""
Foto diaria del mercado RNT Lend, guardada en disco.

POR QUÉ EXISTE
--------------
La página de Mercado Aave siempre mira los mismos datos —no depende de ningún
input del usuario, a diferencia del analizador de wallets—, y reconstruirlos en
cada visita costaba entre cuatro y cinco minutos:

    lista de reservas + config de cada una   ~104 llamadas   ~42 s
    totalSupply de cada reserva              ~103 llamadas   ~41 s
    histórico de eventos Transfer            ~40 páginas     ~40 s
    posición de cada prestatario             ~337 llamadas  ~135 s

De todo eso, solo el histórico es acumulativo. Y la desproporción es brutal:

    eventos acumulados desde el despliegue:  19.972
    eventos de un día cualquiera:                21

Reconstruirlo entero cada vez es leer veinte mil eventos para encontrar
veintiuno. Aquí se guarda el estado ya digerido —saldos por dirección, serie
diaria, último bloque leído— y cada actualización pide solo lo nuevo.

Lo demás (totalSupply, Health Factor) NO es acumulativo: es estado actual y no
se puede pedir «desde el último bloque». Para eso la solución no es incremental
sino declarar la frescura: se guarda una foto al día y la página dice de cuándo
es. Para explicarle la exposición del mercado a un inversor, que el dato sea de
esta mañana no cambia nada.

QUÉ NO ENTRA AQUÍ
-----------------
Los KPIs de cabecera (aportado, prestado, APR, utilización) NO salen de esta
foto: son 6 llamadas y ~3 segundos, y la página los pide en vivo. Es el titular
de la sección y no merece la pena servirlo con horas de retraso.

Tampoco entra nada del maestro: el fichero guarda solo lo que hay en la cadena
y la página lo cruza con el CSV al pintar. Así el maestro se puede corregir sin
tener que regenerar la foto.

CÓMO SE ACTUALIZA
-----------------
`.github/workflows/snapshot_aave.yml` ejecuta cada noche:

    python3 scripts/snapshot_aave.py

y commitea `data/aave/snapshot.json` si ha cambiado. La app solo lee.

Si el fichero no existe —repo recién clonado, antes del primer pase— la página
lo detecta y cae al camino en vivo de siempre. Nunca se queda sin datos.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pandas as pd

import aave_lend as al

RUTA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "data", "aave", "snapshot.json")

# Sube cuando cambie la forma del fichero, para que un snapshot viejo se
# detecte como incompatible en vez de leerse a medias.
ESQUEMA = 1

ZERO_ADDR = al.ZERO_ADDR


# ── Lectura ──────────────────────────────────────────────────────────────────

def cargar(ruta: str = RUTA) -> dict:
    """La foto guardada, o {} si no hay ninguna utilizable.

    Nunca lanza: un fichero corrupto o de otra versión equivale a no tenerlo, y
    la página cae al camino en vivo.
    """
    try:
        with open(ruta, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    if d.get("esquema") != ESQUEMA:
        return {}
    return d


def edad_horas(snap: dict) -> float | None:
    """Horas transcurridas desde que se generó la foto."""
    try:
        t = datetime.fromisoformat(str(snap["generado"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


# ── Estado incremental de un token ───────────────────────────────────────────
# De cada aToken / debtToken se guarda lo YA DIGERIDO, no los eventos crudos:
# saldo por dirección, serie diaria del total en circulación y hasta qué bloque
# se leyó. Guardar los 20.000 eventos ocuparía megas y no aporta nada que no se
# pueda reconstruir plegando los nuevos sobre este estado.

def _plegar(estado: dict, eventos: list, decimals: int) -> dict:
    """Incorpora eventos nuevos al estado acumulado de un token.

    `estado` se modifica y se devuelve. Los eventos deben venir ordenados y ser
    todos posteriores al último bloque ya procesado: es responsabilidad de quien
    los pide, y por eso se pide siempre desde `ultimo_bloque + 1`.
    """
    saldos = estado.setdefault("saldos", {})
    serie  = estado.setdefault("serie", [])
    total  = float(estado.get("total", 0.0))
    puntos = {}          # fecha ISO → total en circulación al cierre de ese día

    for ev in eventos:
        val = ev["value_raw"] / (10 ** decimals)
        f, t = ev["from"], ev["to"]
        if f != ZERO_ADDR:
            saldos[f] = saldos.get(f, 0.0) - val
        if t != ZERO_ADDR:
            saldos[t] = saldos.get(t, 0.0) + val
        # Solo mint y burn mueven el total en circulación; una transferencia
        # entre dos direcciones reales lo deja igual.
        if f == ZERO_ADDR and t != ZERO_ADDR:
            total += val
        elif t == ZERO_ADDR and f != ZERO_ADDR:
            total -= val
        else:
            continue
        puntos[ev["ts"][:10]] = total

    # Los saldos a cero se descartan: son direcciones que ya salieron, y
    # arrastrarlas haría crecer el fichero sin aportar nada.
    estado["saldos"] = {a: round(b, 8) for a, b in saldos.items() if b > 0.01}
    estado["total"]  = total
    for fecha, tot in sorted(puntos.items()):
        if serie and serie[-1][0] == fecha:
            serie[-1][1] = tot
        else:
            serie.append([fecha, tot])
    estado["serie"] = serie
    if eventos:
        estado["ultimo_bloque"] = max(e["block"] for e in eventos)
    return estado


def _eventos_desde(token: str, desde: int, hasta: int, api_key: str) -> list:
    """Transfers de un token en (desde, hasta], ya parseados y ordenados."""
    if desde >= hasta:
        return []
    crudos = al.fetch_logs_range(token, al.TRANSFER_TOPIC, desde + 1, hasta, api_key)
    out = []
    for log in crudos:
        try:
            out.append({
                "block": int(log["blockNumber"], 16),
                "ts": datetime.fromtimestamp(int(log["timeStamp"], 16),
                                             tz=timezone.utc).isoformat(),
                "from": "0x" + log["topics"][1][-40:],
                "to":   "0x" + log["topics"][2][-40:],
                "value_raw": int(log["data"], 16),
            })
        except (KeyError, IndexError, ValueError):
            continue
    out.sort(key=lambda x: x["block"])
    return out


# ── Construcción de la foto ──────────────────────────────────────────────────

def construir(api_key: str, previo: dict | None = None, log=print) -> dict:
    """Genera la foto del mercado, reutilizando la anterior si se le pasa.

    Con `previo`, el escaneo de eventos arranca en el último bloque ya leído en
    vez de en el 0. El resto —totalSupply, posiciones, tipos— se recalcula
    entero: no es acumulativo y no hay forma de pedir «lo que cambió».

    Este proceso corre de noche y sin nadie esperando, así que prioriza ser
    completo sobre ser rápido.
    """
    previo = previo or {}
    bloque = al.fetch_latest_block(api_key)
    if not bloque:
        raise RuntimeError("No se pudo leer el bloque actual de Polygon.")
    log(f"bloque actual: {bloque:,}")

    reservas = al.reservas_del_pool(api_key)
    if not reservas:
        raise RuntimeError("El pool no devolvió ninguna reserva.")
    log(f"reservas en el pool: {len(reservas)}")

    stables, colateral, tokens_stable = {}, [], {}
    for asset in reservas:
        cfg = al.config_de_reserva(asset, api_key)
        if not cfg:
            continue
        bajo = asset.lower()
        if bajo in al.STABLES:
            sym = al.STABLES[bajo]
            tokens_stable[sym] = {"atoken": cfg["atoken"],
                                  "debt_token": cfg["variable_debt_token"],
                                  "reserve": bajo}
            stables[sym] = {
                "supply_apr": cfg["liquidity_rate_apr"],
                "borrow_apr": cfg["borrow_rate_apr"],
                "supply_total": al.total_supply(cfg["atoken"], 6, api_key),
                "borrow_total": al.total_supply(cfg["variable_debt_token"], 6, api_key),
            }
        else:
            tot = al.total_supply(cfg["atoken"], 18, api_key)
            if tot > 0.001:
                colateral.append({"token_address": bajo, "colateral_tokens": tot})
    for sym, e in stables.items():
        s, b = e.get("supply_total", 0), e.get("borrow_total", 0)
        e["utilizacion"] = (b / s) if s else None
    log(f"stablecoins: {list(stables)} · proyectos con colateral: {len(colateral)}")

    # Eventos: la única parte incremental.
    estados = dict(previo.get("estados") or {})
    nuevos_total = 0
    for sym, addrs in tokens_stable.items():
        for clase, token in (("supply", addrs["atoken"]), ("borrow", addrs["debt_token"])):
            est = dict(estados.get(token) or {})
            desde = int(est.get("ultimo_bloque", 0))
            evs = _eventos_desde(token, desde, bloque, api_key)
            nuevos_total += len(evs)
            log(f"  {sym} {clase}: {len(evs):,} eventos nuevos desde el bloque {desde:,}")
            est = _plegar(est, evs, 6)
            est["ultimo_bloque"] = max(int(est.get("ultimo_bloque", 0)), bloque)
            est["simbolo"], est["clase"] = sym, clase
            estados[token] = est
    log(f"eventos nuevos en total: {nuevos_total:,}")

    # Prestatarios vivos: salen de los saldos de los debt tokens, ya plegados.
    prestatarios = set()
    for token, est in estados.items():
        if est.get("clase") == "borrow":
            prestatarios.update(est.get("saldos", {}))
    log(f"prestatarios con deuda viva: {len(prestatarios)}")

    salud = al.salud_agregada.__wrapped__(tuple(sorted(prestatarios)), api_key) \
        if prestatarios else {}
    if salud:
        log(f"health factor agregado: {salud.get('health_factor'):.3f}")

    # Tipos: se recalcula entero. Son dos reservas y el pase es nocturno.
    tipos = {}
    for sym, addrs in tokens_stable.items():
        df = al.fetch_rate_history.__wrapped__(addrs["reserve"], api_key)
        if df is not None and not df.empty:
            d = df.copy()
            d["fecha"] = pd.to_datetime(d["fecha"]).dt.strftime("%Y-%m-%d")
            tipos[sym] = d.to_dict(orient="list")

    return {
        "esquema":  ESQUEMA,
        "generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bloque":   bloque,
        "tokens_stable": tokens_stable,
        "stables":  stables,
        "colateral": colateral,
        "estados":  estados,
        "salud":    salud,
        "tipos":    tipos,
    }


def guardar(snap: dict, ruta: str = RUTA) -> None:
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


# ── Lo que la página necesita, ya en su forma ────────────────────────────────

def stables_en_vivo(snap: dict, api_key: str) -> dict:
    """Los KPIs de cabecera, pedidos a la cadena AHORA.

    Son seis llamadas —config de las dos reservas y circulante de sus cuatro
    tokens— y unos tres segundos. Es el titular de la página, así que no merece
    la pena servirlo con horas de retraso solo por ahorrarlas.

    Las direcciones salen de la foto, que es lo que evita tener que recorrer las
    103 reservas del pool para descubrir cuáles son las stablecoins.

    Devuelve {} si algo falla: el llamante se queda con lo guardado.
    """
    tokens = (snap or {}).get("tokens_stable") or {}
    if not tokens:
        return {}
    out = {}
    for sym, addrs in tokens.items():
        cfg = al.config_de_reserva(addrs["reserve"], api_key)
        if not cfg:
            return {}
        sup = al.total_supply(addrs["atoken"], 6, api_key)
        bor = al.total_supply(addrs["debt_token"], 6, api_key)
        out[sym] = {
            "supply_apr": cfg["liquidity_rate_apr"],
            "borrow_apr": cfg["borrow_rate_apr"],
            "supply_total": sup, "borrow_total": bor,
            "utilizacion": (bor / sup) if sup else None,
        }
    return out


def tipos(snap: dict, simbolo: str) -> pd.DataFrame:
    """Serie histórica de tipos de una reserva, tal como la guardó la foto."""
    d = ((snap or {}).get("tipos") or {}).get(simbolo)
    if not d:
        return pd.DataFrame()
    df = pd.DataFrame(d)
    if "fecha" in df.columns:
        df["fecha"] = pd.to_datetime(df["fecha"])
    return df


def holders(snap: dict, clase: str) -> dict:
    """Saldo por dirección, sumando USDT y USDC. `clase` es 'supply' o 'borrow'."""
    out = {}
    for est in (snap.get("estados") or {}).values():
        if est.get("clase") != clase:
            continue
        for addr, bal in (est.get("saldos") or {}).items():
            out[addr] = out.get(addr, 0.0) + bal
    return out


def historico(snap: dict) -> dict:
    """Lo mismo que devolvía el escaneo en vivo, pero leído de la foto.

    Se mantiene la forma exacta que ya consumía la página —series, tipos y
    holders por símbolo— para no tener que tocar todo lo que hay aguas abajo:
    gráficos, tablas de concentración, PDF y mensaje de WhatsApp siguen
    recibiendo lo de siempre.
    """
    out = {}
    for sym in (snap.get("tokens_stable") or {}):
        sup = serie_diaria(snap, sym, "supply")
        bor = serie_diaria(snap, sym, "borrow")
        if sup.empty and bor.empty:
            continue
        out[sym] = {
            "supply_series": sup, "borrow_series": bor,
            "rate_series": tipos(snap, sym),
            "supply_holders": _saldos(snap, sym, "supply"),
            "borrow_holders": _saldos(snap, sym, "borrow"),
        }
    return out


def _saldos(snap: dict, simbolo: str, clase: str) -> dict:
    for est in (snap.get("estados") or {}).values():
        if est.get("simbolo") == simbolo and est.get("clase") == clase:
            return dict(est.get("saldos") or {})
    return {}


def serie_diaria(snap: dict, simbolo: str, clase: str) -> pd.DataFrame:
    """Serie diaria del total en circulación de un token, para el gráfico."""
    for est in (snap.get("estados") or {}).values():
        if est.get("simbolo") == simbolo and est.get("clase") == clase:
            s = est.get("serie") or []
            if not s:
                break
            df = pd.DataFrame(s, columns=["fecha", "total"])
            df["fecha"] = pd.to_datetime(df["fecha"])
            # Se rellenan los días sin movimiento: el total sigue siendo el
            # mismo, y sin esto el gráfico dibujaría saltos donde no los hay.
            return (df.set_index("fecha")["total"].resample("1D").last()
                      .ffill().reset_index())
    return pd.DataFrame()
