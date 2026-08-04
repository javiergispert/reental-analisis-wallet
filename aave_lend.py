"""
RNT Lend (arquitectura Aave V3) — primitivas on-chain y matemática de riesgo.

RNT Lend es el mercado de colateralización propio de Reental desplegado en
Polygon: el inversor deposita tokens inmobiliarios como garantía y toma prestado
USDT/USDC contra ellos. NO es el pool público de Aave.

Módulo común importado por `pages/04_Aave_Mercado.py` (foto de mercado) y
`pages/Analizador_de_Wallets.py` (posición de riesgo por wallet). Mantener aquí
la única implementación evita que las páginas se desincronicen y hace que
compartan la misma caché: si una ya reconstruyó el histórico, la otra lo obtiene
gratis.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st

ETHERSCAN_BASE   = "https://api.etherscan.io/v2/api"
POLYGON_CHAIN_ID = 137

RNT_LEND_POOL = "0x67dc8037db6309dd5571d82c65f5f593f7da1505"

# Stablecoins prestables del pool (dirección del activo subyacente → símbolo)
STABLES = {
    "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": "USDT",
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": "USDC",
}

# Selectores de función (4 primeros bytes de keccak256 de la firma)
SEL_GET_RESERVES_LIST    = "0xd1946dbc"   # getReservesList()
SEL_GET_RESERVE_DATA     = "0x35ea6a75"   # getReserveData(address)
SEL_TOTAL_SUPPLY         = "0x18160ddd"   # totalSupply()
SEL_GET_USER_ACCOUNT     = "0xbf92857c"   # getUserAccountData(address)
SEL_ADDRESSES_PROVIDER   = "0x0542975c"   # ADDRESSES_PROVIDER()
SEL_GET_PRICE_ORACLE     = "0xfca513a8"   # getPriceOracle()
SEL_GET_ASSET_PRICE      = "0xb3596f07"   # getAssetPrice(address)
SEL_UNDERLYING_ASSET     = "0xb16a19de"   # UNDERLYING_ASSET_ADDRESS()
SEL_BALANCE_OF           = "0x70a08231"   # balanceOf(address)

TRANSFER_TOPIC             = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
RESERVE_DATA_UPDATED_TOPIC = "0x804c9b842b2748a22bb64b345453a3de7ca54a6ca45ce00d415894979e22897a"
ZERO_ADDR = "0x0000000000000000000000000000000000000000"

RAY = 10 ** 27
WAD = 10 ** 18
# El oráculo del pool cotiza en USD con 8 decimales (BASE_CURRENCY_UNIT = 1e8),
# verificado on-chain. Convierte internamente los proyectos denominados en EUR,
# así que los precios que devuelve ya son comparables entre sí en dólares.
BASE_UNIT = 10 ** 8

# APR máximo del modelo de tipos de interés de este mercado.
APR_MAXIMO = 0.16


# ── Llamadas on-chain vía Etherscan (eth_call) ───────────────────────────────

# La key de Etherscan admite ~3 llamadas/seg; se limita el ritmo globalmente
# (entre hilos) en vez de confiar solo en el nº de workers, que puede generar ráfagas.
_RATE_LOCK = threading.Lock()
_RATE_MIN_INTERVAL = 0.4  # ~2.5 llamadas/seg, margen de seguridad
_last_call_ts = [0.0]


def _throttle():
    with _RATE_LOCK:
        wait = _last_call_ts[0] + _RATE_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call_ts[0] = time.monotonic()


def eth_call(to: str, data: str, api_key: str, retries: int = 6) -> str:
    for attempt in range(retries):
        _throttle()
        try:
            r = requests.get(ETHERSCAN_BASE, params={
                "chainid": POLYGON_CHAIN_ID, "module": "proxy", "action": "eth_call",
                "to": to, "data": data, "tag": "latest", "apikey": api_key,
            }, timeout=20)
            payload = r.json()
            result = payload.get("result")
            if isinstance(result, str) and result.startswith("0x"):
                return result
            # "Max calls per sec rate limit reached" u otros errores de la key → reintentar
            time.sleep(0.5 * (attempt + 1))
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return ""


def _addr_arg(address: str) -> str:
    """Codifica una dirección como argumento ABI (32 bytes)."""
    return "000000000000000000000000" + address[2:].lower()


# ── Posición de riesgo de una wallet ─────────────────────────────────────────

@st.cache_data(show_spinner=False, ttl=120)
def get_user_account_data(wallet: str, api_key: str) -> dict:
    """Posición viva del usuario en el pool, en UNA sola llamada.

    getUserAccountData devuelve 6 palabras de 32 bytes:
      0 totalCollateralBase · 1 totalDebtBase · 2 availableBorrowsBase
      3 currentLiquidationThreshold (bps) · 4 ltv (bps) · 5 healthFactor (wad)

    El LTV y el umbral son la MEDIA PONDERADA de la cesta de colateral de ese
    usuario concreto, no un parámetro global: si tiene varios proyectos con
    parámetros de riesgo distintos, el valor ya viene mezclado como corresponde.

    Devuelve {} si la llamada falla, para que el llamante lo distinga de una
    posición vacía (que sí devuelve datos, con deuda 0).
    """
    raw = eth_call(RNT_LEND_POOL, SEL_GET_USER_ACCOUNT + _addr_arg(wallet), api_key)
    if not raw or len(raw) < 2 + 64 * 6:
        return {}
    h = raw[2:]
    w = [h[i:i + 64] for i in range(0, len(h), 64)]
    try:
        colateral = int(w[0], 16) / BASE_UNIT
        deuda     = int(w[1], 16) / BASE_UNIT
        disponible = int(w[2], 16) / BASE_UNIT
        umbral    = int(w[3], 16) / 10000
        ltv       = int(w[4], 16) / 10000
        hf_raw    = int(w[5], 16)
    except Exception:
        return {}
    # Sin deuda, el contrato devuelve uint256 máximo: no es un HF real.
    hf = None if hf_raw >= 2 ** 255 else hf_raw / WAD
    return {
        "colateral_usd":     colateral,
        "deuda_usd":         deuda,
        "disponible_usd":    disponible,
        "umbral_liquidacion": umbral,
        "ltv":               ltv,
        "health_factor":     hf,
        "tiene_deuda":       deuda > 0.01,
    }


@st.cache_data(show_spinner=False, ttl=300)
def balance_of(token_address: str, wallet: str, decimals: int, api_key: str) -> float:
    """Saldo actual de un token para una wallet, leído del contrato.

    Imprescindible para los aTokens: su saldo CRECE solo según se devenga el
    interés, y ese crecimiento no emite ningún Transfer. Sumar los eventos de
    mint y burn da el principal aportado, nunca el interés acumulado, así que
    lo devengado y no retirado solo se ve preguntando al contrato.

    TTL corto (5 min) porque el saldo cambia de forma continua.
    """
    res = eth_call(token_address, SEL_BALANCE_OF + _addr_arg(wallet), api_key)
    if not res or res == "0x":
        return 0.0
    try:
        return int(res, 16) / (10 ** decimals)
    except (TypeError, ValueError):
        return 0.0


@st.cache_data(show_spinner=False, ttl=86400)
def underlying_asset(token_address: str, api_key: str) -> str:
    """Activo subyacente de un aToken o debtToken de Aave, preguntado al propio
    contrato. Permite identificar un aToken por lo que representa en vez de por
    cómo se llame: Aave nombra los suyos de forma inconsistente entre mercados
    (aMatUSDT / "Aave Matic USDT" frente a aUSDCn / "USDCn"), así que cualquier
    filtro por prefijo se rompe en silencio al aparecer un mercado nuevo.

    Devuelve "" si el contrato no expone la función (no es un token de Aave).
    """
    res = eth_call(token_address, SEL_UNDERLYING_ASSET, api_key)
    if not res or len(res) < 42:
        return ""
    return "0x" + res[-40:].lower()


@st.cache_data(show_spinner=False, ttl=86400)
def get_price_oracle(api_key: str) -> str:
    """Dirección del oráculo de precios, resuelta vía el AddressesProvider del pool
    (en vez de fijarla a fuego, que se rompería si Reental lo actualiza)."""
    prov = eth_call(RNT_LEND_POOL, SEL_ADDRESSES_PROVIDER, api_key)
    if not prov or len(prov) < 42:
        return ""
    prov_addr = "0x" + prov[-40:]
    orc = eth_call(prov_addr, SEL_GET_PRICE_ORACLE, api_key)
    if not orc or len(orc) < 42:
        return ""
    return "0x" + orc[-40:]


@st.cache_data(show_spinner=False, ttl=600)
def get_asset_prices(assets: tuple, api_key: str) -> dict:
    """{token_address: precio_usd} según el oráculo del pool.

    El oráculo ya convierte a USD los proyectos denominados en EUR, así que no
    hay que aplicar ningún tipo de cambio por fuera (verificado: los proyectos
    en EUR cotizan a ~114 $ frente a los 100 $ de los denominados en dólares).
    `assets` es una tupla para que la caché de Streamlit pueda hashearla.
    """
    oracle = get_price_oracle(api_key)
    if not oracle:
        return {}
    precios = {}
    for addr in assets:
        raw = eth_call(oracle, SEL_GET_ASSET_PRICE + _addr_arg(addr), api_key)
        if not raw:
            continue
        try:
            precios[addr.lower()] = int(raw, 16) / BASE_UNIT
        except Exception:
            continue
    return precios


# ── Matemática de riesgo (sin llamadas on-chain) ─────────────────────────────

def dias_hasta_liquidacion(hf: float, apr: float) -> float | None:
    """Días hasta que el Health Factor cae a 1 por acumulación de intereses,
    asumiendo colateral estable y sin repagos.

    La deuda variable de Aave capitaliza de forma continua, así que crece como
    D(t) = D0 · e^(r·t). Con el colateral constante:
        HF(t) = C·LT / (D0·e^(r·t)) = HF0 · e^(-r·t)
    Igualando a 1:   t = ln(HF0) / r     (t en años)

    Devuelve None si no aplica (sin deuda, APR<=0 o ya liquidable).
    """
    if hf is None or apr is None or apr <= 0 or hf <= 1:
        return None
    import math
    return (math.log(hf) / apr) * 365.0


def margen_caida_colateral(hf: float) -> float | None:
    """Cuánto puede caer el valor del colateral antes de la liquidación.
    HF = C·LT/D → el HF llega a 1 cuando C cae un (1 - 1/HF)."""
    if hf is None or hf <= 0:
        return None
    return max(0.0, 1.0 - 1.0 / hf)


def capacidad_retirada(colateral: float, deuda: float, umbral: float,
                       hf_objetivo: float) -> float:
    """Colateral (USD) retirable dejando el HF en `hf_objetivo`.

    La retirada se valida contra el HF (umbral de liquidación), no contra el LTV,
    por lo que sí puede apuntarse a un HF por debajo de LT/LTV.
    """
    if deuda <= 0 or umbral <= 0:
        return max(0.0, colateral)      # sin deuda se puede retirar todo
    colateral_necesario = hf_objetivo * deuda / umbral
    return max(0.0, colateral - colateral_necesario)


def capacidad_prestamo(colateral: float, deuda: float, umbral: float, ltv: float,
                       hf_objetivo: float) -> dict:
    """Préstamo adicional (USD) posible, y el HF al que deja la posición.

    Ojo: el protocolo limita los préstamos NUEVOS por el LTV, no por el umbral de
    liquidación. Por eso el HF mínimo alcanzable pidiendo prestado es siempre
    LT/LTV (aquí 0.80/0.75 = 1.0667) y no puede bajarse de ahí, aunque la
    fórmula del HF objetivo permitiese más.
    """
    if colateral <= 0:
        return {"maximo": 0.0, "por_hf": 0.0, "limitado_por_ltv": False,
                "hf_resultante": None, "hf_minimo_posible": None}
    por_ltv = max(0.0, colateral * ltv - deuda)
    por_hf  = max(0.0, (colateral * umbral / hf_objetivo) - deuda) if hf_objetivo > 0 else 0.0
    maximo  = min(por_ltv, por_hf) if por_hf > 0 else por_ltv
    # Con el máximo permitido por LTV, ¿en qué HF queda?
    deuda_final = deuda + por_ltv
    hf_resultante = (colateral * umbral / deuda_final) if deuda_final > 0 else None
    return {
        "maximo":            por_ltv if por_hf >= por_ltv else por_hf,
        "por_ltv":           por_ltv,
        "por_hf":            por_hf,
        "limitado_por_ltv":  por_hf > por_ltv,
        "hf_resultante":     hf_resultante,
        "hf_minimo_posible": (umbral / ltv) if ltv > 0 else None,
    }


def repago_para_hf(colateral: float, deuda: float, umbral: float,
                   hf_objetivo: float) -> float:
    """Deuda (USD) a repagar para subir el HF hasta `hf_objetivo`."""
    if deuda <= 0 or hf_objetivo <= 0:
        return 0.0
    deuda_objetivo = colateral * umbral / hf_objetivo
    return max(0.0, deuda - deuda_objetivo)


def nivel_riesgo(hf: float | None) -> tuple:
    """(emoji, etiqueta, color) según el margen hasta la liquidación."""
    if hf is None:
        return ("⚪", "Sin deuda", "#94a3b8")
    if hf < 1.0:
        return ("🔴", "Liquidable", "#dc2626")
    if hf < 1.1:
        return ("🔴", "Crítico", "#dc2626")
    if hf < 1.3:
        return ("🟠", "Alerta", "#ea580c")
    if hf < 1.5:
        return ("🟡", "Vigilar", "#ca8a04")
    return ("🟢", "Saludable", "#16a34a")


# ── Logs / histórico de tipos ────────────────────────────────────────────────

def _get_logs_page(address: str, topic0: str, from_block: int, to_block, page: int,
                   api_key: str, topic1: str = None, retries: int = 6) -> list:
    params = {
        "chainid": POLYGON_CHAIN_ID, "module": "logs", "action": "getLogs",
        "address": address, "topic0": topic0,
        "fromBlock": from_block, "toBlock": to_block,
        "page": page, "offset": 1000, "apikey": api_key,
    }
    if topic1:
        params["topic1"] = topic1
        params["topic0_1_opr"] = "and"
    for attempt in range(retries):
        _throttle()
        try:
            r = requests.get(ETHERSCAN_BASE, params=params, timeout=25)
            payload = r.json()
            if "too large" in str(payload.get("message", "")).lower():
                return None  # señal: hay que partir el rango de bloques
            result = payload.get("result")
            if isinstance(result, list):
                return result
            if result == "No records found" or payload.get("message") == "No records found":
                return []
            time.sleep(0.5 * (attempt + 1))
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return []


def fetch_logs_range(address: str, topic0: str, from_block: int, to_block: int,
                     api_key: str, topic1: str = None, depth: int = 0) -> list:
    """Descarga todos los logs de `address`/`topic0` en [from_block, to_block],
    partiendo el rango recursivamente si excede el límite de 10.000 resultados."""
    all_logs = []
    hit_cap = False
    page = 1
    while page <= 10:
        chunk = _get_logs_page(address, topic0, from_block, to_block, page, api_key, topic1=topic1)
        if chunk is None:
            hit_cap = True  # Etherscan rechazó explícitamente: rango demasiado grande
            break
        all_logs.extend(chunk)
        if len(chunk) < 1000:
            return all_logs  # última página parcial: no hay más datos
        page += 1
    else:
        # Se agotaron las 10 páginas y la última seguía llena (10.000 exactos):
        # no podemos distinguir "justo 10.000" de "hay más", así que partimos por seguridad.
        hit_cap = True

    if hit_cap:
        if depth > 40 or to_block <= from_block:
            return all_logs
        mid = (from_block + to_block) // 2
        left = fetch_logs_range(address, topic0, from_block, mid, api_key, topic1=topic1, depth=depth + 1)
        right = fetch_logs_range(address, topic0, mid + 1, to_block, api_key, topic1=topic1, depth=depth + 1)
        return left + right
    return all_logs


@st.cache_data(show_spinner=False, ttl=300)
def fetch_latest_block(api_key: str) -> int:
    for attempt in range(4):
        _throttle()
        try:
            r = requests.get(ETHERSCAN_BASE, params={
                "chainid": POLYGON_CHAIN_ID, "module": "proxy", "action": "eth_blockNumber",
                "apikey": api_key,
            }, timeout=15)
            result = r.json().get("result")
            if isinstance(result, str) and result.startswith("0x"):
                return int(result, 16)
        except Exception:
            pass
        time.sleep(0.5 * (attempt + 1))
    return 0


@st.cache_data(show_spinner=False, ttl=21600)
def fetch_rate_history(reserve_address: str, api_key: str) -> pd.DataFrame:
    """Media histórica acumulada (desde el despliegue hasta cada día) de los tipos
    supply/borrow de una reserva, a partir de los eventos ReserveDataUpdated que
    emite el Pool en cada operación sobre ese activo.

    Se usa la media acumulada en vez del tipo puntual porque, al ser un tipo de
    interés variable, refleja mejor lo que experimenta un inversor a largo plazo:
    el tipo instantáneo es muy ruidoso (cambia en cada depósito/préstamo/repago).

    OJO: escanea el histórico completo de logs; es la operación cara del módulo
    (~1-2 min en frío). Cacheada 6 h y compartida entre páginas.
    """
    latest_block = fetch_latest_block(api_key)
    if not latest_block:
        return pd.DataFrame()
    topic1 = "0x" + "0" * 24 + reserve_address[2:].lower()
    raw_logs = fetch_logs_range(RNT_LEND_POOL, RESERVE_DATA_UPDATED_TOPIC, 0, latest_block,
                                api_key, topic1=topic1)
    rows = []
    for log in raw_logs:
        try:
            hexd = log["data"][2:]
            words = [hexd[i:i + 64] for i in range(0, len(hexd), 64)]
            liquidity_rate = int(words[0], 16) / RAY
            borrow_rate = int(words[2], 16) / RAY
            ts = datetime.fromtimestamp(int(log["timeStamp"], 16), tz=timezone.utc).replace(tzinfo=None)
            rows.append({"fecha": ts, "supply_apr": liquidity_rate, "borrow_apr": borrow_rate})
        except Exception:
            continue
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("fecha")
    # Media dentro de cada día (para no sobreponderar días con mucha actividad)…
    daily_mean = df.set_index("fecha")[["supply_apr", "borrow_apr"]].resample("1D").mean()
    daily_mean = daily_mean.ffill()
    # …y luego media acumulada desde el primer día hasta cada uno de ellos.
    daily_avg_acumulada = daily_mean.expanding().mean().reset_index()
    return daily_avg_acumulada


@st.cache_data(show_spinner=False, ttl=21600)
def apr_borrow_medio_historico(reserve_address: str, api_key: str) -> float | None:
    """Último valor de la media acumulada del APR de préstamo de una reserva.
    Escalar ligero para quien solo necesita el número (no la serie completa)."""
    df = fetch_rate_history(reserve_address, api_key)
    if df is None or df.empty or "borrow_apr" not in df.columns:
        return None
    try:
        return float(df["borrow_apr"].iloc[-1])
    except Exception:
        return None
