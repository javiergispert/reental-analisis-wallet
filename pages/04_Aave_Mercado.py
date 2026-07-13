"""
Análisis del mercado RNT Lend — Reental Wealth
RNT Lend es el mercado de colateralización propio de Reental (arquitectura Aave V3),
desplegado en Polygon con su propio contrato Pool: no es el pool público de Aave.

Pool RNT Lend: 0x67dc8037db6309dd5571d82c65f5f593f7da1505 (Polygon)
Identificado a partir de la wallet 0xCB906D02cF0D4031C36BCbfC95DBA6786fB77baD,
leyendo la función POOL() de sus aTokens "aMatReental-…" on-chain.

El estado del mercado (KPIs, colateral por proyecto) es un SNAPSHOT ACTUAL, ya
que no existe ningún indexador externo (DeFiLlama, subgraph público, etc.) para
este pool privado. El histórico de USDT/USDC y el análisis de concentración de
holders sí se reconstruyen on-chain, escaneando eventos Transfer (mint/burn) de
los aTokens y debt tokens de USDT/USDC desde su despliegue.
"""

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

from utils import load_master_projects

# ── Constantes ────────────────────────────────────────────────────────────────
ETHERSCAN_BASE   = "https://api.etherscan.io/v2/api"
API_KEY          = os.getenv("ETHERSCAN_API_KEY", "")
POLYGON_CHAIN_ID = 137

RNT_LEND_POOL = "0x67dc8037db6309dd5571d82c65f5f593f7da1505"

STABLES = {
    "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": "USDT",
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": "USDC",
}

# Selectores de función (4 primeros bytes de keccak256 de la firma)
SEL_GET_RESERVES_LIST = "0xd1946dbc"   # getReservesList()
SEL_GET_RESERVE_DATA  = "0x35ea6a75"   # getReserveData(address)
SEL_TOTAL_SUPPLY      = "0x18160ddd"   # totalSupply()

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDR      = "0x0000000000000000000000000000000000000000"

RAY = 10 ** 27

# Tramos de concentración por valor en USD (ballenas/tiburones/delfines/peces)
TIERS = [
    ("🐋 Ballenas",  100_000, float("inf")),
    ("🦈 Tiburones",  25_000, 100_000),
    ("🐬 Delfines",    5_000,  25_000),
    ("🐟 Peces",            0,   5_000),
]

# Paleta corporativa Reental
DORADO   = "#F5A623"
NAVY_OSC = "#0D1B2E"
AZUL_MED = "#3B82F6"

TEMPLATE_PLOTLY = "plotly_dark"

st.title("🏦 Mercado RNT Lend (Aave) — Reental")
st.caption(
    "Foto actual del mercado propio de colateralización de Reental sobre arquitectura Aave V3 "
    f"(pool `{RNT_LEND_POOL}` en Polygon), no el pool público de Aave."
)


# ── Llamadas on-chain vía Etherscan (eth_call) ───────────────────────────────

# La key de Etherscan usada admite ~3 llamadas/seg; se limita el ritmo globalmente
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


def _eth_call(to: str, data: str, retries: int = 6) -> str:
    for attempt in range(retries):
        _throttle()
        try:
            r = requests.get(ETHERSCAN_BASE, params={
                "chainid": POLYGON_CHAIN_ID, "module": "proxy", "action": "eth_call",
                "to": to, "data": data, "tag": "latest", "apikey": API_KEY,
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


@st.cache_data(show_spinner=False, ttl=86400)
def fetch_reserves_list() -> list:
    """Direcciones de todos los activos subyacentes listados en el pool RNT Lend."""
    raw = _eth_call(RNT_LEND_POOL, SEL_GET_RESERVES_LIST)
    if not raw:
        return []
    hexres = raw[2:]
    length = int(hexres[64:128], 16)
    start = 128
    return [
        "0x" + hexres[start + i * 64: start + (i + 1) * 64][-40:]
        for i in range(length)
    ]


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_reserve_config(asset: str) -> dict:
    """aToken, debt token y tipos actuales (supply/borrow APR) de una reserva del pool."""
    data = SEL_GET_RESERVE_DATA + "000000000000000000000000" + asset[2:]
    raw = _eth_call(RNT_LEND_POOL, data)
    if not raw:
        return {}
    hexres = raw[2:]
    words = [hexres[i:i + 64] for i in range(0, len(hexres), 64)]
    if len(words) < 11:
        return {}
    return {
        "liquidity_rate_apr": int(words[2], 16) / RAY,
        "borrow_rate_apr":    int(words[4], 16) / RAY,
        "atoken":             "0x" + words[8][-40:],
        "variable_debt_token": "0x" + words[10][-40:],
    }


@st.cache_data(show_spinner=False, ttl=600)
def fetch_total_supply(token_address: str, decimals: int = 18) -> float:
    raw = _eth_call(token_address, SEL_TOTAL_SUPPLY)
    if not raw:
        return 0.0
    return int(raw, 16) / (10 ** decimals)


# ── Escaneo de eventos Transfer (histórico + holders) ────────────────────────
# Etherscan limita getLogs a 10.000 resultados por consulta (page × offset).
# Para tokens con más eventos, se parte el rango de bloques recursivamente.

def _get_logs_page(address: str, from_block: int, to_block, page: int, retries: int = 6) -> list:
    for attempt in range(retries):
        _throttle()
        try:
            r = requests.get(ETHERSCAN_BASE, params={
                "chainid": POLYGON_CHAIN_ID, "module": "logs", "action": "getLogs",
                "address": address, "topic0": TRANSFER_TOPIC,
                "fromBlock": from_block, "toBlock": to_block,
                "page": page, "offset": 1000, "apikey": API_KEY,
            }, timeout=25)
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


def _fetch_logs_range(address: str, from_block: int, to_block: int, depth: int = 0) -> list:
    """Descarga todos los Transfer logs de `address` en [from_block, to_block],
    partiendo el rango recursivamente si excede el límite de 10.000 resultados."""
    all_logs = []
    hit_cap = False
    page = 1
    while page <= 10:
        chunk = _get_logs_page(address, from_block, to_block, page)
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
        left = _fetch_logs_range(address, from_block, mid, depth + 1)
        right = _fetch_logs_range(address, mid + 1, to_block, depth + 1)
        return left + right
    return all_logs


@st.cache_data(show_spinner=False, ttl=300)
def fetch_latest_block() -> int:
    for attempt in range(4):
        _throttle()
        try:
            r = requests.get(ETHERSCAN_BASE, params={
                "chainid": POLYGON_CHAIN_ID, "module": "proxy", "action": "eth_blockNumber",
                "apikey": API_KEY,
            }, timeout=15)
            result = r.json().get("result")
            if isinstance(result, str) and result.startswith("0x"):
                return int(result, 16)
        except Exception:
            pass
        time.sleep(0.5 * (attempt + 1))
    return 0


@st.cache_data(show_spinner=False, ttl=21600)
def fetch_all_transfers(token_address: str) -> list:
    """Todos los eventos Transfer de un token desde el bloque 0. Devuelve
    [{"block": int, "ts": datetime, "from": addr, "to": addr, "value_raw": int}]."""
    latest_block = fetch_latest_block()
    if not latest_block:
        return []
    raw_logs = _fetch_logs_range(token_address, 0, latest_block)
    parsed = []
    for log in raw_logs:
        try:
            topics = log["topics"]
            from_addr = "0x" + topics[1][-40:]
            to_addr = "0x" + topics[2][-40:]
            value = int(log["data"], 16)
            block = int(log["blockNumber"], 16)
            ts = datetime.fromtimestamp(int(log["timeStamp"], 16), tz=timezone.utc)
            parsed.append({"block": block, "ts": ts, "from": from_addr, "to": to_addr, "value_raw": value})
        except Exception:
            continue
    parsed.sort(key=lambda x: x["block"])
    return parsed


def build_daily_supply_series(transfers: list, decimals: int) -> pd.DataFrame:
    """Suma total en circulación día a día a partir de eventos mint (from=0x0)
    y burn (to=0x0). Las transferencias entre direcciones no nulas no alteran el total."""
    if not transfers:
        return pd.DataFrame()
    rows = []
    for tx in transfers:
        if tx["from"] == ZERO_ADDR and tx["to"] != ZERO_ADDR:
            delta = tx["value_raw"]
        elif tx["to"] == ZERO_ADDR and tx["from"] != ZERO_ADDR:
            delta = -tx["value_raw"]
        else:
            continue
        rows.append({"fecha": tx["ts"].replace(tzinfo=None), "delta": delta / (10 ** decimals)})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("fecha")
    df["total"] = df["delta"].cumsum()
    daily = df.set_index("fecha")["total"].resample("1D").last().ffill().reset_index()
    return daily


def build_holder_balances(transfers: list, decimals: int) -> dict:
    """Balance neto actual por dirección a partir del histórico completo de Transfer."""
    balances = {}
    for tx in transfers:
        val = tx["value_raw"] / (10 ** decimals)
        if tx["from"] != ZERO_ADDR:
            balances[tx["from"]] = balances.get(tx["from"], 0.0) - val
        if tx["to"] != ZERO_ADDR:
            balances[tx["to"]] = balances.get(tx["to"], 0.0) + val
    return {addr: bal for addr, bal in balances.items() if bal > 0.01}


MAX_WORKERS = 3  # la key de Etherscan usada limita a ~3 llamadas/seg


@st.cache_data(show_spinner=False, ttl=1800)
def build_market_snapshot() -> dict:
    """Recorre las ~100 reservas del pool: separa stablecoins (USDT/USDC) de
    tokens inmobiliarios Reental y obtiene su totalSupply actual (colateral/borrow).
    Las llamadas a Etherscan se paralelizan (I/O-bound) para que la carga sea rápida."""
    reservas = fetch_reserves_list()
    if not reservas:
        return {"stables": {}, "colateral": []}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        configs = list(pool.map(fetch_reserve_config, reservas))

    stables_out = {}

    # Segunda ronda paralela: totalSupply de todos los aTokens/debt tokens necesarios
    stable_jobs = []   # (sym, "supply"|"borrow", token_addr)
    colateral_jobs = []  # asset_lower, atoken_addr

    for asset, cfg in zip(reservas, configs):
        if not cfg:
            continue
        asset_lower = asset.lower()
        if asset_lower in STABLES:
            sym = STABLES[asset_lower]
            stable_jobs.append((sym, "supply", cfg["atoken"], cfg["liquidity_rate_apr"], cfg["borrow_rate_apr"]))
            stable_jobs.append((sym, "borrow", cfg["variable_debt_token"], cfg["liquidity_rate_apr"], cfg["borrow_rate_apr"]))
        else:
            colateral_jobs.append((asset_lower, cfg["atoken"]))

    stable_tokens = {
        STABLES[a.lower()]: {"atoken": c["atoken"], "debt_token": c["variable_debt_token"]}
        for a, c in zip(reservas, configs) if c and a.lower() in STABLES
    }

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        stable_supplies = list(pool.map(lambda j: fetch_total_supply(j[2], 6), stable_jobs))
        colateral_supplies = list(pool.map(lambda j: fetch_total_supply(j[1], 18), colateral_jobs))

    for (sym, kind, _, supply_apr, borrow_apr), total in zip(stable_jobs, stable_supplies):
        entry = stables_out.setdefault(sym, {"supply_apr": supply_apr, "borrow_apr": borrow_apr})
        entry[f"{kind}_total"] = total

    for sym, entry in stables_out.items():
        s, b = entry.get("supply_total", 0), entry.get("borrow_total", 0)
        entry["utilizacion"] = (b / s) if s else None

    colateral_rows = [
        {"token_address": asset_lower, "colateral_tokens": total}
        for (asset_lower, _), total in zip(colateral_jobs, colateral_supplies)
    ]

    return {"stables": stables_out, "colateral": colateral_rows, "stable_tokens": stable_tokens}


# ── Histórico y concentración (reconstruidos on-chain) ───────────────────────

def build_historical_series_batch(stable_tokens: dict) -> dict:
    """Recorre aToken + debt token de USDT y USDC en paralelo: el cuello de botella
    es la latencia de red de Etherscan (no CPU), así que 4 fetches concurrentes
    reducen el tiempo total frente a hacerlos uno detrás de otro."""
    jobs = []  # (sym, "supply"|"borrow", token_addr)
    for sym, addrs in stable_tokens.items():
        jobs.append((sym, "supply", addrs["atoken"]))
        jobs.append((sym, "borrow", addrs["debt_token"]))

    with ThreadPoolExecutor(max_workers=min(4, len(jobs)) or 1) as pool:
        transfers_list = list(pool.map(lambda j: fetch_all_transfers(j[2]), jobs))

    resultado = {}
    for (sym, kind, _), transfers in zip(jobs, transfers_list):
        entry = resultado.setdefault(sym, {})
        entry[f"{kind}_series"] = build_daily_supply_series(transfers, decimals=6)
        entry[f"{kind}_holders"] = build_holder_balances(transfers, decimals=6)
    return resultado


def clasificar_tiers(holders: dict) -> pd.DataFrame:
    """Cuenta direcciones y suma de valor por tramo (ballena/tiburón/delfín/pez)."""
    filas = []
    for nombre, lo, hi in TIERS:
        addrs_en_tramo = [v for v in holders.values() if lo <= v < hi]
        filas.append({
            "Tramo": nombre, "Nº holders": len(addrs_en_tramo),
            "Valor total": sum(addrs_en_tramo),
        })
    return pd.DataFrame(filas)


def combinar_holders(*dicts: dict) -> dict:
    """Suma balances de la misma dirección a través de varios tokens (ej. USDT+USDC)."""
    combinado = {}
    for d in dicts:
        for addr, val in d.items():
            combinado[addr] = combinado.get(addr, 0.0) + val
    return combinado


# ── Carga de datos ────────────────────────────────────────────────────────────

if not API_KEY:
    st.error("Falta configurar ETHERSCAN_API_KEY.")
    st.stop()

with st.spinner("Consultando el pool RNT Lend on-chain (Polygon)… puede tardar unos segundos"):
    snapshot = build_market_snapshot()
    master_df = load_master_projects()

stables = snapshot.get("stables", {})
colateral_rows = snapshot.get("colateral", [])

if not stables and not colateral_rows:
    st.error(
        "No se pudo leer el pool RNT Lend en este momento (Etherscan puede estar "
        "limitando peticiones). Vuelve a intentarlo en unos minutos."
    )
    st.stop()

# ── KPIs USDT / USDC ──────────────────────────────────────────────────────────
st.subheader("📊 Estado actual — USDT / USDC")
cols = st.columns(4)
for i, sym in enumerate(("USDT", "USDC")):
    info = stables.get(sym)
    if not info:
        continue
    with cols[i * 2]:
        st.metric(
            f"💰 {sym} — Total aportado",
            f"${info['supply_total']:,.0f}",
            f"APR {info['supply_apr'] * 100:.2f}%",
        )
    with cols[i * 2 + 1]:
        util_txt = f" · Util. {info['utilizacion'] * 100:.1f}%" if info["utilizacion"] is not None else ""
        st.metric(
            f"📉 {sym} — Total prestado",
            f"${info['borrow_total']:,.0f}",
            f"APR {info['borrow_apr'] * 100:.2f}%{util_txt}",
        )

st.caption(
    "⚠️ Tipos mostrados como APR simple (tasa anual sin componer), tal como los "
    "almacena el contrato del pool. Es una foto actual, no una serie histórica."
)

st.markdown("---")

# ── Histórico de capital aportado / prestado / disponible ────────────────────
st.subheader("📈 Evolución histórica — USDT / USDC")
st.caption(
    "Reconstruido on-chain a partir de los eventos Transfer (mint/burn) de los aTokens y "
    "debt tokens del pool desde su despliegue. Puede tardar más la primera vez (se cachea 6h). "
    "⚠️ Refleja depósitos menos retiradas (principal); no incorpora el interés acumulado día a día, "
    "por lo que queda ligeramente por debajo de las cifras del snapshot en vivo de más arriba."
)

stable_tokens = snapshot.get("stable_tokens", {})
historical = {}
if stable_tokens:
    with st.spinner("Reconstruyendo histórico on-chain (puede tardar 1-2 minutos la primera vez)…"):
        historical = build_historical_series_batch(stable_tokens)

for sym in ("USDT", "USDC"):
    hist = historical.get(sym)
    if not hist or hist["supply_series"].empty:
        st.info(f"No se pudo reconstruir el histórico de {sym} en este momento.")
        continue

    df_s = hist["supply_series"].rename(columns={"total": "aportado"})
    df_b = hist["borrow_series"].rename(columns={"total": "prestado"})
    df_m = pd.merge(df_s, df_b, on="fecha", how="outer").sort_values("fecha").ffill().fillna(0)
    df_m["disponible"] = df_m["aportado"] - df_m["prestado"]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df_m["fecha"], y=df_m["aportado"], mode="lines", name="Aportado",
        line=dict(color=DORADO, width=2), fill="tozeroy", fillcolor="rgba(245,166,35,0.12)",
    ))
    fig.add_trace(go.Scatter(
        x=df_m["fecha"], y=df_m["prestado"], mode="lines", name="Prestado",
        line=dict(color=AZUL_MED, width=2), fill="tozeroy", fillcolor="rgba(59,130,246,0.12)",
    ))
    fig.add_trace(go.Scatter(
        x=df_m["fecha"], y=df_m["disponible"], mode="lines", name="Disponible",
        line=dict(color="#25D366", width=2, dash="dot"),
    ))
    fig.update_layout(
        title=f"{sym} — capital aportado, prestado y disponible",
        template=TEMPLATE_PLOTLY, height=380, margin=dict(t=50, b=20, l=10, r=10),
        yaxis_title="USD", legend=dict(orientation="h", y=1.15),
    )
    st.plotly_chart(fig, use_container_width=True)

st.markdown("---")

# ── Concentración de holders (suministradores de liquidez y prestatarios) ────
st.subheader("🐋 Concentración de holders")
st.caption(
    "Distribución de direcciones por tramos de valor (USDT + USDC combinados), a partir de los "
    "balances actuales reconstruidos on-chain. Ballenas ≥ \\$100k · Tiburones \\$25k–100k · "
    "Delfines \\$5k–25k · Peces < \\$5k."
)

if historical:
    supply_holders = combinar_holders(*[h["supply_holders"] for h in historical.values()])
    borrow_holders = combinar_holders(*[h["borrow_holders"] for h in historical.values()])

    col_a, col_b = st.columns(2)
    for col, holders, titulo in (
        (col_a, supply_holders, "💰 Suministradores de liquidez"),
        (col_b, borrow_holders, "📉 Prestatarios"),
    ):
        with col:
            st.markdown(f"**{titulo}** ({len(holders)} direcciones)")
            if not holders:
                st.info("Sin datos.")
                continue
            df_tiers = clasificar_tiers(holders)
            fig = go.Figure(go.Bar(
                x=df_tiers["Tramo"], y=df_tiers["Nº holders"],
                marker_color=DORADO, text=df_tiers["Nº holders"], textposition="outside",
            ))
            fig.update_layout(
                template=TEMPLATE_PLOTLY, height=320, margin=dict(t=20, b=20, l=10, r=10),
                yaxis_title="Nº de direcciones",
            )
            st.plotly_chart(fig, use_container_width=True)
            st.dataframe(
                df_tiers.style.format({"Valor total": "${:,.0f}"}),
                use_container_width=True, hide_index=True,
            )
else:
    st.info("No se pudo construir el análisis de concentración en este momento.")

st.markdown("---")

# ── Colateral depositado por proyecto Reental ────────────────────────────────
st.subheader("🔒 Colateral depositado por proyecto Reental — foto actual")
st.caption(
    "Tokens inmobiliarios de Reental depositados ahora mismo como colateral en RNT Lend, "
    "cruzados con el precio de emisión del CSV máster para estimar su valor en USD/EUR."
)

if colateral_rows and not master_df.empty:
    df_col = pd.DataFrame(colateral_rows)
    df_col = df_col[df_col["colateral_tokens"] > 0.001]

    master_slim = master_df[["token_address", "nombre", "id", "divisa", "precio_emision"]].copy()
    master_slim["token_address"] = master_slim["token_address"].astype(str).str.lower()

    df_col = df_col.merge(master_slim, on="token_address", how="left")
    df_col["valor_estimado"] = df_col["colateral_tokens"] * df_col["precio_emision"]
    df_col["proyecto"] = df_col["id"].fillna(df_col["token_address"])
    df_col = df_col.dropna(subset=["valor_estimado"]).sort_values("valor_estimado", ascending=False)

    if len(df_col) > 5:
        top_n = st.slider("Nº de proyectos a mostrar", 5, min(50, len(df_col)), min(20, len(df_col)))
        df_top = df_col.head(top_n)
    else:
        df_top = df_col

    fig = go.Figure(go.Bar(
        x=df_top["valor_estimado"],
        y=df_top["proyecto"],
        orientation="h",
        marker_color=DORADO,
        customdata=df_top["colateral_tokens"],
        hovertemplate="%{y}<br>Valor: $%{x:,.0f}<br>Tokens: %{customdata:,.1f}<extra></extra>",
    ))
    fig.update_layout(
        template=TEMPLATE_PLOTLY, height=max(350, 26 * len(df_top)),
        margin=dict(t=20, b=20, l=10, r=10),
        xaxis_title="Valor estimado del colateral (según precio de emisión)",
        yaxis=dict(autorange="reversed"),
    )
    st.plotly_chart(fig, use_container_width=True)

    st.metric("💎 Colateral total estimado (todos los proyectos)", f"${df_col['valor_estimado'].sum():,.0f}")

    with st.expander("Ver tabla completa"):
        st.dataframe(
            df_col[["proyecto", "nombre", "colateral_tokens", "divisa", "precio_emision", "valor_estimado"]]
            .rename(columns={
                "proyecto": "Proyecto", "nombre": "Nombre", "colateral_tokens": "Tokens colateralizados",
                "divisa": "Divisa", "precio_emision": "Precio emisión", "valor_estimado": "Valor estimado",
            })
            .style.format({
                "Tokens colateralizados": "{:,.2f}", "Precio emisión": "{:,.2f}", "Valor estimado": "${:,.0f}",
            }),
            use_container_width=True, hide_index=True,
        )
else:
    st.info("No se pudo construir el desglose de colateral por proyecto en este momento.")

st.caption(
    f"Última actualización: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')} · "
    f"Fuente: contrato RNT Lend Pool `{RNT_LEND_POOL}` en Polygon (lectura on-chain vía Etherscan)."
)
