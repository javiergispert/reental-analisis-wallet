"""
Valoración del pool RNT/USDT: precio histórico del RNT y valor del SLP.

POR QUÉ EXISTE
--------------
Un claim de staking reparte RNT, SLP (la participación en el pool) y a veces
stablecoin. Las dos primeras no se podían valorar:

  * el RNT, porque el plan gratuito de CoinGecko solo sirve los últimos 365
    días, así que los claims antiguos se quedaban sin precio;
  * el SLP, porque no cotiza en ningún sitio.

Las dos salen del propio pool, que es donde de verdad se forma el precio del
RNT. Es un par de SushiSwap al uso: token0 = RNT, token1 = USDT, 50/50 en
valor. Con las reservas y el supply de participaciones:

    precio del RNT  =  reserva_USDT / reserva_RNT
    valor de 1 SLP  =  2 × reserva_USDT / supply

CÓMO SE LEE EL PASADO
---------------------
No se puede con `eth_call` a un bloque antiguo: el nodo público de Etherscan
ignora la etiqueta de bloque y devuelve el estado de HOY sin avisar —probado,
y es peligroso justamente porque no falla: devuelve cifras verosímiles. Así
que todo sale de eventos, que sí son históricos:

  * las RESERVAS, del evento `Sync` que el par emite en cada operación: una
    consulta acotada a los bloques anteriores a la fecha, y se toma el último;
  * el SUPPLY, sumando las emisiones y quemas de SLP (`Transfer` desde/hacia
    la dirección cero) desde el origen del pool. Eso es un recorrido largo, así
    que se hace una vez, se guarda como serie diaria y después solo se añade lo
    nuevo. El recorrido completo son ~24 consultas y unos 40 segundos.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import requests

ESQUEMA = 1

BASE = "https://api.etherscan.io/v2/api"
CHAIN = 137

PAR          = "0x4097073e82edac2d758ecfd594139a891340d59d"   # SushiSwap RNT/USDT
BLOQUE_ORIGEN = 49059599                                       # primer evento del par
DEC_RNT, DEC_USDT, DEC_SLP = 18, 6, 18

TOPIC_SYNC     = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TOPIC_CERO     = "0x" + "0" * 64

RUTA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "data", "pool_rnt", "supply.json")


# ─── Acceso a la API ─────────────────────────────────────────────────────────

def _consulta(api_key: str, **kw) -> dict:
    params = {"chainid": CHAIN, "apikey": api_key}
    params.update(kw)
    return requests.get(BASE, params=params, timeout=45).json()


class ConsultaFallida(RuntimeError):
    """La cadena no contestó. Distinto de «no hay eventos»."""


def _logs(api_key: str, desde: int, **extra) -> list:
    """Una página de eventos del par a partir de `desde` (1.000 como máximo).

    Si la API no devuelve una lista, se levanta una excepción en vez de
    devolver vacío. Devolver vacío era indistinguible de «ya no hay más
    eventos»: un límite de peticiones alcanzado a media serie cortaba el
    recorrido y el resultado se guardaba como si estuviera completo. Y un
    supply corto INFLA el valor de cada participación, porque se divide por él.
    """
    motivo = ""
    for intento in range(4):
        j = _consulta(api_key, module="logs", action="getLogs", address=PAR,
                      fromBlock=desde, toBlock="latest", page=1, offset=1000, **extra)
        res = j.get("result")
        if isinstance(res, list):
            return res
        # Etherscan pone el motivo en `result` cuando no tiene resultados que dar.
        motivo = str(j.get("message") or res or "respuesta inesperada")
        # El límite de peticiones es pasajero y no debe tumbar el pase diario:
        # se espera cada vez más antes de volver a intentarlo.
        time.sleep(1.5 * (intento + 1))
    raise ConsultaFallida(motivo)


# ─── Serie de supply ─────────────────────────────────────────────────────────

def cargar(ruta: str = RUTA) -> dict:
    try:
        with open(ruta, encoding="utf-8") as f:
            d = json.load(f)
        return d if d.get("esquema") == ESQUEMA else {}
    except (OSError, ValueError):
        return {}


def guardar(datos: dict, ruta: str = RUTA) -> None:
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def construir(api_key: str, previo: dict | None = None, pausa: float = 0.22) -> dict:
    """Recorre las emisiones y quemas de SLP y devuelve la serie diaria de
    supply. Si se le pasa el resultado anterior, arranca donde lo dejó."""
    previo = previo or {}
    serie = dict(previo.get("serie") or {})
    saldo = int(previo.get("saldo_bruto") or 0)
    desde = int(previo.get("ultimo_bloque") or BLOQUE_ORIGEN)

    # Se reanuda en el bloque SIGUIENTE al último guardado, no en él.
    #
    # El saldo que viene de `previo` ya incluye todo lo de ese bloque —dentro de
    # un pase, la paginación vuelve sobre el último bloque hasta agotarlo—, así
    # que arrancar ahí lo contaba dos veces. El signo del error dependía de lo
    # que hubiera en ese bloque: una quema repetida dejaba el supply BAJO y, como
    # el valor de una participación se obtiene dividiendo por él, las posiciones
    # en SLP salían infladas.
    # Se reanuda en el bloque SIGUIENTE al último guardado, no en él: el saldo
    # que viene de `previo` ya incluye todo lo de ese bloque, así que arrancar
    # ahí lo aplicaba dos veces. Medido sobre el bloque 93833625, que tenía una
    # emisión y una quema: el pase incremental salía 0,0156 SLP corto.
    arranque = desde + 1 if previo.get("ultimo_bloque") else desde

    eventos: dict[tuple, tuple] = {}     # (bloque, logIndex) -> (ts, signo·valor)
    for filtro, signo in (({"topic0": TOPIC_TRANSFER, "topic1": TOPIC_CERO,
                            "topic0_1_opr": "and"}, +1),
                          ({"topic0": TOPIC_TRANSFER, "topic2": TOPIC_CERO,
                            "topic0_2_opr": "and"}, -1)):
        cursor = arranque
        while True:
            pagina = _logs(api_key, cursor, **filtro)
            if not pagina:
                break
            for l in pagina:
                blq = int(l["blockNumber"], 16)
                # La clave incluye el índice del log: sin él, al reanudar por
                # bloque se perdían los eventos que compartían bloque con el
                # último de la página anterior.
                eventos[(blq, int(l["logIndex"], 16), signo)] = (
                    int(l["timeStamp"], 16), signo * int(l["data"], 16))
            ultimo = int(pagina[-1]["blockNumber"], 16)
            if len(pagina) < 1000:
                break
            # Si los 1.000 eventos caben en un solo bloque, repetir desde él no
            # avanzaría nunca. Se salta al siguiente: los de ese bloque ya están
            # todos en esta página, que es la que ha llenado.
            cursor = ultimo + 1 if ultimo <= cursor else ultimo
            time.sleep(pausa)

    tope = desde
    for (blq, _idx, _s), (ts, delta) in sorted(eventos.items()):
        saldo += delta
        serie[datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")] = saldo / 10 ** DEC_SLP
        tope = max(tope, blq)

    # El sello solo se mueve si los datos se han movido.
    #
    # Sellar cada pase hacía que el fichero cambiara SIEMPRE aunque el pool no
    # se hubiera tocado, y eso anula el «commitea solo si ha cambiado» del pase
    # diario: un commit al día para siempre por una marca de tiempo que no lee
    # nadie. Así `actualizado` significa lo útil —cuándo cambió el dato— y
    # cuándo se comprobó lo dice el registro del Action.
    sin_cambios = (previo.get("serie") == serie
                   and previo.get("saldo_bruto") == saldo
                   and previo.get("ultimo_bloque") == tope)
    sello = (previo.get("actualizado") if sin_cambios and previo.get("actualizado")
             else datetime.utcnow().strftime("%Y-%m-%d %H:%M"))
    return {"esquema": ESQUEMA, "serie": serie, "saldo_bruto": saldo,
            "ultimo_bloque": tope, "actualizado": sello}


def supply_on_chain(api_key: str) -> float | None:
    """Participaciones en circulación ahora mismo, leídas del contrato.

    Sirve de contraste: la serie se reconstruye sumando eventos y esta cifra
    dice si esa suma cuadra. Para el presente `eth_call` sí es fiable; lo que
    el nodo público no honra es la etiqueta de un bloque pasado.
    """
    try:
        j = _consulta(api_key, module="proxy", action="eth_call", to=PAR,
                      data="0x18160ddd", tag="latest")       # totalSupply()
        res = str(j.get("result") or "")
        return int(res, 16) / 10 ** DEC_SLP if res.startswith("0x") else None
    except Exception:       # noqa: BLE001
        return None


def supply_en(fecha: str, datos: dict) -> float | None:
    """Participaciones en circulación a una fecha (YYYY-MM-DD): el último valor
    conocido que no sea posterior. El supply solo cambia cuando alguien entra o
    sale del pool, así que arrastrar el anterior es exacto, no una estimación."""
    serie = (datos or {}).get("serie") or {}
    previos = [d for d in serie if d <= fecha[:10]]
    return serie[max(previos)] if previos else None


# ─── Reservas ────────────────────────────────────────────────────────────────

def reservas_en_bloque(bloque: int, api_key: str, ventana: int = 20000) -> tuple | None:
    """(RNT, USDT) en el pool en el último `Sync` anterior al bloque dado.

    La ventana se ensancha si hace falta: el pool se mueve a diario, pero en sus
    primeras semanas podía pasar tiempo sin una sola operación."""
    for ancho in (ventana, ventana * 10, ventana * 100):
        j = _consulta(api_key, module="logs", action="getLogs", address=PAR,
                      topic0=TOPIC_SYNC, fromBlock=max(BLOQUE_ORIGEN, bloque - ancho),
                      toBlock=bloque, page=1, offset=1000)
        res = j.get("result")
        if isinstance(res, list) and res:
            datos = res[-1]["data"][2:]
            return (int(datos[0:64], 16) / 10 ** DEC_RNT,
                    int(datos[64:128], 16) / 10 ** DEC_USDT)
    return None


def bloque_de_fecha(fecha: str, api_key: str) -> int | None:
    """Último bloque del día anterior a esa fecha (YYYY-MM-DD, UTC)."""
    try:
        ts = int(datetime.strptime(fecha[:10], "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None
    j = _consulta(api_key, module="block", action="getblocknobytime",
                  timestamp=ts, closest="before")
    res = str(j.get("result") or "")
    return int(res) if res.isdigit() else None


# ─── Lo que usa la página ────────────────────────────────────────────────────

def precio_rnt(bloque: int, api_key: str) -> float | None:
    """Precio del RNT en USDT en ese punto de la cadena, según el propio pool."""
    r = reservas_en_bloque(bloque, api_key)
    if not r or r[0] <= 0:
        return None
    return r[1] / r[0]


def precio_actual(api_key: str) -> float | None:
    """Precio del RNT en USDT ahora mismo, leyendo las reservas del par.

    Para el presente sí sirve `eth_call`: lo que el nodo público no honra es la
    etiqueta de un bloque pasado. Una sola consulta, sin recorrer eventos."""
    try:
        j = _consulta(api_key, module="proxy", action="eth_call", to=PAR,
                      data="0x0902f1ac", tag="latest")     # getReserves()
        h = str(j.get("result") or "")[2:]
        if len(h) < 128:
            return None
        rnt = int(h[0:64], 16) / 10 ** DEC_RNT
        usdt = int(h[64:128], 16) / 10 ** DEC_USDT
        return usdt / rnt if rnt else None
    except Exception:       # noqa: BLE001
        return None


def valor_lp(bloque: int, fecha: str, api_key: str, datos: dict) -> float | None:
    """Valor en USD de UNA participación del pool (SLP) en esa fecha.

    Son las dos mitades del pool —RNT y USDT— repartidas entre las
    participaciones que había. Como el par es 50/50 en valor, basta con doblar
    el lado en USDT: así el resultado no depende de ningún precio externo."""
    supply = supply_en(fecha, datos)
    if not supply or supply <= 0:
        return None
    r = reservas_en_bloque(bloque, api_key)
    if not r:
        return None
    return 2.0 * r[1] / supply
