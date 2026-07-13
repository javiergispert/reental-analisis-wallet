"""
Análisis del mercado RNT Lend — Reental Wealth
RNT Lend es el mercado de colateralización propio de Reental (arquitectura Aave V3),
desplegado en Polygon con su propio contrato Pool: no es el pool público de Aave.

Pool RNT Lend: 0x67dc8037db6309dd5571d82c65f5f593f7da1505 (Polygon)
Identificado a partir de la wallet 0xCB906D02cF0D4031C36BCbfC95DBA6786fB77baD,
leyendo la función POOL() de sus aTokens "aMatReental-…" on-chain.

Todos los datos aquí son un SNAPSHOT ACTUAL (no histórico): no existe ningún
indexador externo (DeFiLlama, subgraph público, etc.) para este pool privado.
Reconstruir un histórico exigiría escanear eventos on-chain de mint/burn de cada
uno de los ~100 contratos del mercado desde su despliegue — quedó pendiente
como posible segunda iteración.
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

RAY = 10 ** 27

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

    return {"stables": stables_out, "colateral": colateral_rows}


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
