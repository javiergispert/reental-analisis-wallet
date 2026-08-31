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
from __future__ import annotations


import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from utils import load_master_projects

# ── Constantes ────────────────────────────────────────────────────────────────
ETHERSCAN_BASE   = "https://api.etherscan.io/v2/api"
API_KEY          = os.getenv("ETHERSCAN_API_KEY", "")
POLYGON_CHAIN_ID = 137

# Primitivas on-chain y constantes del pool: módulo común compartido con
# Analizador_de_Wallets.py (ver aave_lend.py). Una única implementación evita
# que las páginas se desincronicen y hace que compartan la misma caché.
import aave_lend as _al

RNT_LEND_POOL = _al.RNT_LEND_POOL

STABLES = _al.STABLES

# Selectores de función (4 primeros bytes de keccak256 de la firma)
SEL_GET_RESERVES_LIST = _al.SEL_GET_RESERVES_LIST   # getReservesList()
SEL_GET_RESERVE_DATA  = _al.SEL_GET_RESERVE_DATA    # getReserveData(address)
SEL_TOTAL_SUPPLY      = _al.SEL_TOTAL_SUPPLY        # totalSupply()

TRANSFER_TOPIC              = _al.TRANSFER_TOPIC
RESERVE_DATA_UPDATED_TOPIC  = _al.RESERVE_DATA_UPDATED_TOPIC
ZERO_ADDR      = _al.ZERO_ADDR

RAY = _al.RAY

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

# Colores reportlab (PDF) — mismos valores, formato HexColor
PDF_DORADO    = colors.HexColor(DORADO)
PDF_NAVY_OSC  = colors.HexColor(NAVY_OSC)
PDF_GRIS_CLAR = colors.HexColor("#F2F4F8")
PDF_BLANCO    = colors.white

st.title("🏦 Mercado RNT Lend (Aave) — Reental")
st.caption(
    "Foto actual del mercado propio de colateralización de Reental sobre arquitectura Aave V3 "
    f"(pool `{RNT_LEND_POOL}` en Polygon), no el pool público de Aave."
)


# ── Llamadas on-chain vía Etherscan (eth_call) ───────────────────────────────

# El throttle global (compartido entre hilos y entre páginas) vive en aave_lend.
_throttle = _al._throttle


def _eth_call(to: str, data: str, retries: int = 6) -> str:
    return _al.eth_call(to, data, API_KEY, retries=retries)


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

def _fetch_logs_range(address: str, topic0: str, from_block: int, to_block: int,
                       topic1: str = None, depth: int = 0) -> list:
    """Descarga todos los logs de `address`/`topic0` en [from_block, to_block],
    partiendo el rango recursivamente si excede el límite de 10.000 resultados."""
    return _al.fetch_logs_range(address, topic0, from_block, to_block, API_KEY,
                                topic1=topic1, depth=depth)


def fetch_latest_block() -> int:
    return _al.fetch_latest_block(API_KEY)


@st.cache_data(show_spinner=False, ttl=21600)
def fetch_all_transfers(token_address: str) -> list:
    """Todos los eventos Transfer de un token desde el bloque 0. Devuelve
    [{"block": int, "ts": datetime, "from": addr, "to": addr, "value_raw": int}]."""
    latest_block = fetch_latest_block()
    if not latest_block:
        return []
    raw_logs = _fetch_logs_range(token_address, TRANSFER_TOPIC, 0, latest_block)
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


def fetch_rate_history(reserve_address: str) -> pd.DataFrame:
    """Media histórica acumulada de los tipos supply/borrow de una reserva.
    Implementación (y caché de 6 h) en aave_lend, compartida con el analizador."""
    return _al.fetch_rate_history(reserve_address, API_KEY)


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
        STABLES[a.lower()]: {"atoken": c["atoken"], "debt_token": c["variable_debt_token"], "reserve": a.lower()}
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
    """Recorre aToken + debt token + eventos de tipos de USDT y USDC en paralelo:
    el cuello de botella es la latencia de red de Etherscan (no CPU), así que
    fetches concurrentes reducen el tiempo total frente a hacerlos uno a uno."""
    transfer_jobs = []  # (sym, "supply"|"borrow", token_addr)
    rate_jobs = []       # (sym, reserve_addr)
    for sym, addrs in stable_tokens.items():
        transfer_jobs.append((sym, "supply", addrs["atoken"]))
        transfer_jobs.append((sym, "borrow", addrs["debt_token"]))
        rate_jobs.append((sym, addrs["reserve"]))

    total_workers = min(6, len(transfer_jobs) + len(rate_jobs)) or 1
    with ThreadPoolExecutor(max_workers=total_workers) as pool:
        transfers_future = pool.map(lambda j: fetch_all_transfers(j[2]), transfer_jobs)
        rates_future = pool.map(lambda j: fetch_rate_history(j[1]), rate_jobs)
        transfers_list = list(transfers_future)
        rates_list = list(rates_future)

    resultado = {}
    for (sym, kind, _), transfers in zip(transfer_jobs, transfers_list):
        entry = resultado.setdefault(sym, {})
        entry[f"{kind}_series"] = build_daily_supply_series(transfers, decimals=6)
        entry[f"{kind}_holders"] = build_holder_balances(transfers, decimals=6)

    for (sym, _), rate_df in zip(rate_jobs, rates_list):
        resultado.setdefault(sym, {})["rate_series"] = rate_df

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


def tramo_de(valor: float) -> str:
    for nombre, lo, hi in TIERS:
        if lo <= valor < hi:
            return nombre
    return "—"


def holders_a_dataframe(holders: dict) -> pd.DataFrame:
    """Detalle por wallet (dirección, saldo, tramo), ordenado de mayor a menor saldo."""
    filas = [{"Wallet": addr, "Saldo (USD)": val, "Tramo": tramo_de(val)} for addr, val in holders.items()]
    return pd.DataFrame(filas).sort_values("Saldo (USD)", ascending=False).reset_index(drop=True)


def combinar_holders(*dicts: dict) -> dict:
    """Suma balances de la misma dirección a través de varios tokens (ej. USDT+USDC)."""
    combinado = {}
    for d in dicts:
        for addr, val in d.items():
            combinado[addr] = combinado.get(addr, 0.0) + val
    return combinado


def build_historical_fig(df_m: pd.DataFrame, sym: str, dark: bool = True) -> go.Figure:
    """Construye el gráfico de aportado/prestado/disponible + APR medio acumulado.
    `dark=False` genera la variante clara usada al exportar la imagen al PDF."""
    template = TEMPLATE_PLOTLY if dark else "plotly_white"
    font_color = None if dark else NAVY_OSC

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
    if "supply_apr_medio" in df_m.columns:
        fig.add_trace(go.Scatter(
            x=df_m["fecha"], y=df_m["supply_apr_medio"] * 100, mode="lines", name="APR Supply (media acum.)",
            line=dict(color=DORADO, width=2, dash="dash"), yaxis="y2",
        ))
        fig.add_trace(go.Scatter(
            x=df_m["fecha"], y=df_m["borrow_apr_medio"] * 100, mode="lines", name="APR Borrow (media acum.)",
            line=dict(color=AZUL_MED, width=2, dash="dash"), yaxis="y2",
        ))
    fig.update_layout(
        title=dict(text=f"{sym} — capital aportado, prestado y disponible", y=0.97, x=0.02, xanchor="left"),
        template=template, height=420,
        margin=dict(t=90, b=20, l=10, r=10),
        yaxis=dict(title="USD"),
        yaxis2=dict(title="APR % (media acumulada)", overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        font=dict(color=font_color) if font_color else {},
    )
    return fig


# ── Exportación: PDF e informe WhatsApp ──────────────────────────────────────

def generar_pdf_aave(stables: dict, df_col: pd.DataFrame, supply_holders: dict, borrow_holders: dict,
                      historical_resumen: dict = None, historical_dfs: dict = None,
                      salud: dict = None) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                             leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                             topMargin=1.5 * cm, bottomMargin=1.5 * cm)

    tit_s    = ParagraphStyle("t",  fontSize=17, leading=21, alignment=TA_LEFT,   fontName="Helvetica-Bold", textColor=PDF_NAVY_OSC)
    fecha_s  = ParagraphStyle("f",  fontSize=9,  leading=12, alignment=TA_RIGHT,  fontName="Helvetica",      textColor=PDF_NAVY_OSC)
    sub_s    = ParagraphStyle("s",  fontSize=11, leading=14, alignment=TA_LEFT,   fontName="Helvetica-Bold", textColor=PDF_BLANCO)
    cell_s   = ParagraphStyle("c",  fontSize=8,  leading=11, alignment=TA_CENTER, fontName="Helvetica",      textColor=PDF_NAVY_OSC)
    cell_lbl = ParagraphStyle("cl", fontSize=8,  leading=11, alignment=TA_LEFT,   fontName="Helvetica-Bold", textColor=PDF_BLANCO)
    nota_s   = ParagraphStyle("n",  fontSize=7,  leading=9.5, alignment=TA_LEFT, fontName="Helvetica",       textColor=PDF_NAVY_OSC)

    def seccion(titulo: str):
        t = Table([[Paragraph(titulo, sub_s)]], colWidths=["100%"])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), PDF_NAVY_OSC),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        return t

    def tabla_estandar(header: list, filas: list, col_widths=None):
        data = [[Paragraph(h, cell_lbl) for h in header]] + [
            [Paragraph(str(v), cell_s) for v in fila] for fila in filas
        ]
        t = Table(data, colWidths=col_widths)
        ts = [
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")),
            ("BACKGROUND", (0, 0), (-1, 0), PDF_NAVY_OSC),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]
        for i in range(1, len(data)):
            ts.append(("BACKGROUND", (0, i), (-1, i), PDF_GRIS_CLAR if i % 2 == 0 else PDF_BLANCO))
        t.setStyle(TableStyle(ts))
        return t

    story = []

    ht = Table([[
        Paragraph(f"<font color='{DORADO}'><b>Reental</b></font> Wealth · Informe Mercado RNT Lend", tit_s),
        Paragraph(f"Generado: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}", fecha_s),
    ]], colWidths=["65%", "35%"])
    ht.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story += [ht, Spacer(1, 0.3 * cm), HRFlowable(width="100%", thickness=3, color=PDF_DORADO), Spacer(1, 0.4 * cm)]

    # KPIs USDT/USDC
    story.append(seccion("Estado actual — USDT / USDC"))
    story.append(Spacer(1, 0.2 * cm))
    filas_kpi = []
    for sym in ("USDT", "USDC"):
        info = stables.get(sym)
        if not info:
            continue
        util_txt = f"{info['utilizacion'] * 100:.1f}%" if info.get("utilizacion") is not None else "—"
        filas_kpi.append([
            sym, f"${info['supply_total']:,.0f}", f"{info['supply_apr'] * 100:.2f}%",
            f"${info['borrow_total']:,.0f}", f"{info['borrow_apr'] * 100:.2f}%", util_txt,
        ])
    story.append(tabla_estandar(
        ["Activo", "Total aportado", "APR supply", "Total prestado", "APR borrow", "Utilización"],
        filas_kpi,
    ))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph(
        "El APR mostrado arriba es el tipo instantáneo en el momento de generación del informe: al ser "
        "un tipo variable, fluctúa en cada depósito, préstamo o repago. La tabla siguiente muestra la "
        "<b>media histórica acumulada</b> desde el despliegue del contrato, más representativa de lo que "
        "experimenta un inversor a largo plazo.",
        nota_s,
    ))
    story.append(Spacer(1, 0.2 * cm))

    filas_hist = []
    for sym in ("USDT", "USDC"):
        r = (historical_resumen or {}).get(sym)
        if not r:
            continue
        delta_aportado = r["aportado_hoy"] - r["aportado_inicio"]
        delta_prestado = r["prestado_hoy"] - r["prestado_inicio"]
        filas_hist.append([
            sym,
            f"{r['supply_apr_medio'] * 100:.2f}%" if r.get("supply_apr_medio") is not None else "—",
            f"{r['borrow_apr_medio'] * 100:.2f}%" if r.get("borrow_apr_medio") is not None else "—",
            f"+${delta_aportado:,.0f}",
            f"+${delta_prestado:,.0f}",
            f"{r['dias_periodo']} días",
        ])
    if filas_hist:
        story.append(tabla_estandar(
            ["Activo", "APR supply medio", "APR borrow medio", "Δ Aportado", "Δ Prestado", "Periodo analizado"],
            filas_hist,
        ))
        primera_fecha = min(
            r["fecha_inicio"] for r in historical_resumen.values() if r.get("fecha_inicio") is not None
        )
        story.append(Spacer(1, 0.15 * cm))
        story.append(Paragraph(
            f"Histórico reconstruido on-chain desde {primera_fecha.strftime('%d/%m/%Y')} "
            "(eventos Transfer y ReserveDataUpdated del contrato).",
            nota_s,
        ))
    story.append(Spacer(1, 0.5 * cm))

    # ── Salud agregada del mercado ───────────────────────────────────────────
    # Va aquí, antes de los gráficos, en el mismo orden que la web: es el dato
    # de cabecera para explicar la exposición del mercado de una sola vez.
    story.append(seccion("Salud del mercado — el pool como una sola cuenta"))
    story.append(Spacer(1, 0.2 * cm))

    if not salud or not salud.get("n_posiciones"):
        story.append(Paragraph(
            "No se incluyó en este informe. Se calcula bajo demanda desde la sección "
            "«Salud del mercado» de la herramienta (requiere consultar la posición de cada "
            "prestatario en el pool); una vez calculada, se incorpora automáticamente al PDF.",
            nota_s,
        ))
        story.append(Spacer(1, 0.5 * cm))
    else:
        hf_pdf = salud["health_factor"]
        _, etiqueta_pdf, _ = _al.nivel_riesgo(hf_pdf)

        # Franja de cifras de cabecera: mismo lenguaje visual que las secciones
        # de arriba, con el valor grande sobre la etiqueta.
        kpi_lbl = ParagraphStyle("kl", fontSize=7.5, leading=10, alignment=TA_CENTER,
                                 fontName="Helvetica-Bold", textColor=PDF_BLANCO)
        kpi_val = ParagraphStyle("kv", fontSize=14, leading=17, alignment=TA_CENTER,
                                 fontName="Helvetica-Bold", textColor=PDF_NAVY_OSC)
        kpi_sub = ParagraphStyle("ks", fontSize=6.5, leading=9, alignment=TA_CENTER,
                                 fontName="Helvetica", textColor=colors.HexColor("#64748B"))

        cabecera = [
            ("Health Factor agregado", f"{hf_pdf:,.3f}", f"{etiqueta_pdf} · liquidación si &lt; 1"),
            ("Colateral respaldando deuda", f"${salud['colateral_usd']:,.0f}",
             f"umbral medio {salud['umbral_medio'] * 100:,.1f}%"),
            ("Deuda total", f"${salud['deuda_usd']:,.0f}",
             f"{salud['n_posiciones']} posiciones abiertas"),
            ("Sobrecolateralización", f"{salud['sobrecolateral']:,.2f}&#215;",
             f"LTV medio {salud['ltv_medio'] * 100:,.1f}%"),
        ]
        t_kpi = Table(
            [[Paragraph(l, kpi_lbl) for l, _, _ in cabecera],
             [Paragraph(v, kpi_val) for _, v, _ in cabecera],
             [Paragraph(x, kpi_sub) for _, _, x in cabecera]],
            colWidths=["25%"] * 4,
        )
        t_kpi.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), PDF_NAVY_OSC),
            ("BACKGROUND", (0, 1), (-1, -1), PDF_GRIS_CLAR),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")),
            ("TOPPADDING", (0, 0), (-1, 0), 5), ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
            ("TOPPADDING", (0, 1), (-1, 1), 6), ("BOTTOMPADDING", (0, 2), (-1, 2), 6),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(t_kpi)
        story.append(Spacer(1, 0.3 * cm))

        en_riesgo_pdf = sum(t["pct_deuda"] for t in salud["tramos"] if t["desde"] < 1.3)
        story.append(Paragraph(
            "El Health Factor no se promedia entre carteras, pero sí se reconstruye para el conjunto "
            "con su propia definición: <b>HF = &#931;(colateral &#215; umbral de liquidación) &#247; "
            "&#931;(deuda)</b>. El resultado mide la solvencia del mercado <b>como bloque</b>: por encima "
            "de 1, el colateral aportado cubre la deuda viva. El colateral se valora con el oráculo del "
            "pool, que es el precio con el que el protocolo decide las liquidaciones.",
            nota_s,
        ))
        story.append(Spacer(1, 0.15 * cm))
        story.append(Paragraph(
            f"<b>Matiz importante:</b> un agregado holgado no impide liquidaciones, porque cada posición "
            f"se liquida por su cuenta cuando su propio HF baja de 1. A día de hoy, con el conjunto en "
            f"{hf_pdf:,.3f}, un <b>{en_riesgo_pdf:,.1f}% de la deuda</b> está en posiciones por debajo de "
            f"1.3. Por eso el reparto y el test de estrés que siguen acompañan siempre a la cifra de arriba.",
            nota_s,
        ))
        story.append(Spacer(1, 0.3 * cm))

        # Reparto de la deuda por tramo. Helvetica no tiene emoji: el nivel se
        # marca con una banda de color en la primera columna.
        filas_tramos = []
        for t in salud["tramos"]:
            # Separador decimal con punto, como el resto del informe.
            rango = ("< 1.0" if t["desde"] == 0 else
                     f"&#8805; {t['desde']:.1f}" if t["hasta"] is None else
                     f"{t['desde']:.1f} &#8211; {t['hasta']:.1f}")
            filas_tramos.append([
                "", t["etiqueta"], rango, str(t["posiciones"]),
                f"${t['deuda']:,.0f}", f"{t['pct_deuda']:,.1f}%",
            ])
        t_tr = tabla_estandar(
            ["", "Tramo", "Health Factor", "Posiciones", "Deuda", "% de la deuda"],
            filas_tramos,
            col_widths=["4%", "20%", "18%", "16%", "22%", "20%"],
        )
        estilo_color = [("BACKGROUND", (0, i + 1), (0, i + 1), colors.HexColor(t["color"]))
                        for i, t in enumerate(salud["tramos"])]
        t_tr.setStyle(TableStyle(estilo_color))
        story.append(Paragraph(
            "<b>Dónde está la deuda, por riesgo de la posición.</b> Se reparte la deuda y no el número "
            "de prestatarios: doscientos préstamos de mil dólares no pesan lo que uno de seiscientos mil.",
            nota_s,
        ))
        story.append(Spacer(1, 0.15 * cm))
        story.append(t_tr)
        story.append(Spacer(1, 0.3 * cm))

        filas_estres = [[
            f"&#8722;{e['caida_pct']}%", str(e["posiciones"]),
            f"${e['deuda']:,.0f}", f"{e['pct_deuda']:,.1f}%",
        ] for e in salud["estres"]]
        story.append(Paragraph(
            "<b>Test de estrés.</b> El Health Factor es proporcional al valor del colateral, así que una "
            "caída del x% deja cada posición en HF &#215; (1 &#8722; x). La tabla mide qué parte de la "
            "deuda quedaría liquidable en cada escenario. No es una previsión: es la sensibilidad de la "
            "cartera actual a una bajada del precio de los inmuebles.",
            nota_s,
        ))
        story.append(Spacer(1, 0.15 * cm))
        story.append(tabla_estandar(
            ["Caída del colateral", "Posiciones afectadas", "Deuda liquidable", "% de la deuda"],
            filas_estres,
            col_widths=["25%", "25%", "25%", "25%"],
        ))
        story.append(Spacer(1, 0.2 * cm))
        story.append(Paragraph(
            f"El conjunto llega al umbral de liquidación con una caída del "
            f"<b>{salud['margen_caida'] * 100:,.1f}%</b>, pero las primeras liquidaciones individuales "
            f"empiezan mucho antes. Posición más grande: ${salud['mayor_posicion']:,.0f} "
            f"({salud['pct_mayor']:,.1f}% de la deuda) &#183; HF mínimo {salud['hf_minimo']:,.3f} &#183; "
            f"mediana {salud['hf_mediana']:,.3f}."
            + (f" {salud['fallidas']} carteras no se pudieron consultar y quedan fuera del cálculo."
               if salud.get("fallidas") else ""),
            nota_s,
        ))
        story.append(Spacer(1, 0.5 * cm))

    # Gráficos de evolución histórica (renderizados con kaleido a partir de las
    # mismas figuras que se muestran en la web, en variante clara para imprimir)
    if historical_dfs:
        story.append(seccion("Evolución histórica — aportado, prestado y APR medio"))
        story.append(Spacer(1, 0.2 * cm))
        content_width = A4[0] - 3 * cm
        for sym in ("USDT", "USDC"):
            df_m = historical_dfs.get(sym)
            if df_m is None or df_m.empty:
                continue
            fig = build_historical_fig(df_m, sym, dark=False)
            try:
                png_bytes = fig.to_image(format="png", width=1000, height=420, scale=2)
            except Exception as e:
                # No se deja caer en silencio: si el renderizador de imágenes falla
                # (p.ej. kaleido sin Chromium disponible en el entorno), se deja constancia
                # explícita en el propio informe en vez de omitir el gráfico sin avisar.
                story.append(Paragraph(
                    f"⚠️ No se pudo generar el gráfico de {sym} ({type(e).__name__}: {str(e)[:150]}).",
                    nota_s,
                ))
                story.append(Spacer(1, 0.2 * cm))
                continue
            img_buf = io.BytesIO(png_bytes)
            img_h = content_width * (420 / 1000)
            story.append(Image(img_buf, width=content_width, height=img_h))
            story.append(Spacer(1, 0.3 * cm))

    # Concentración
    story.append(seccion("Concentración de holders (USDT + USDC)"))
    story.append(Spacer(1, 0.2 * cm))
    df_sup_tiers = clasificar_tiers(supply_holders)
    df_bor_tiers = clasificar_tiers(borrow_holders)
    # Helvetica no soporta emoji: se usa solo el nombre del tramo en el PDF.
    df_sup_tiers["Tramo"] = df_sup_tiers["Tramo"].str.split(" ").str[-1]
    df_bor_tiers["Tramo"] = df_bor_tiers["Tramo"].str.split(" ").str[-1]
    filas_conc = []
    for (_, r_s), (_, r_b) in zip(df_sup_tiers.iterrows(), df_bor_tiers.iterrows()):
        filas_conc.append([
            r_s["Tramo"], str(r_s["Nº holders"]), f"${r_s['Valor total']:,.0f}",
            str(r_b["Nº holders"]), f"${r_b['Valor total']:,.0f}",
        ])
    story.append(Paragraph(
        f"Suministradores: {len(supply_holders)} direcciones · Prestatarios: {len(borrow_holders)} direcciones",
        nota_s,
    ))
    story.append(Spacer(1, 0.15 * cm))
    story.append(tabla_estandar(
        ["Tramo", "Nº suministradores", "Valor suministrado", "Nº prestatarios", "Valor prestado"],
        filas_conc,
    ))
    story.append(Spacer(1, 0.5 * cm))

    # Colateral por proyecto (top 15)
    story.append(seccion("Colateral depositado por proyecto Reental (top 15)"))
    story.append(Spacer(1, 0.2 * cm))
    if not df_col.empty:
        filas_col = [
            [r.get("proyecto", "—"), f"{r['colateral_tokens']:,.1f}", f"${r['valor_estimado']:,.0f}"]
            for _, r in df_col.head(15).iterrows()
        ]
        story.append(tabla_estandar(["Proyecto", "Tokens colateralizados", "Valor estimado"], filas_col))
        story.append(Spacer(1, 0.2 * cm))
        story.append(Paragraph(f"<b>Colateral total estimado (todos los proyectos): ${df_col['valor_estimado'].sum():,.0f}</b>", nota_s))
    else:
        story.append(Paragraph("Sin datos de colateral disponibles en este momento.", nota_s))
    story.append(Spacer(1, 0.5 * cm))

    notas = [
        "— RNT Lend es el mercado de colateralización propio de Reental (arquitectura Aave V3) en Polygon, "
        f"contrato Pool {RNT_LEND_POOL}. No es el pool público de Aave.",
        "— El estado actual y el colateral por proyecto son un snapshot en el momento de generación. "
        "La concentración de holders se reconstruye on-chain a partir de eventos Transfer.",
        "— La salud agregada se calcula consultando la posición de cada prestatario en el pool en el "
        "momento de generar el informe. El colateral se valora con el oráculo del pool (el que decide las "
        "liquidaciones), a diferencia del colateral por proyecto de arriba, que usa el precio de emisión "
        "del máster: por eso ambas cifras no tienen por qué coincidir.",
        "— Este informe no constituye consejo de inversión. Las cifras son estimaciones a partir de datos on-chain.",
    ]
    for nota in notas:
        story.append(Paragraph(nota, nota_s))

    doc.build(story)
    return buf.getvalue()


# ── Salud agregada del mercado ───────────────────────────────────────────────

def render_salud_agregada(borrow_holders: dict) -> dict | None:
    """El mercado entero como una sola cuenta: exposición y cobertura.

    Va detrás de un botón a propósito. Son ~340 consultas al pool (una por
    prestatario) y tardan un par de minutos; quien entra a mirar los tipos o la
    concentración no tiene por qué pagarlas. Una vez calculado queda en caché
    media hora, así que las recargas son instantáneas.
    """
    st.subheader("🩺 Salud del mercado — el pool como una sola cuenta")
    st.caption(
        "Cuánto capital respalda el total de la deuda, agregando las posiciones de todos "
        "los prestatarios. Útil para explicar la exposición del mercado de una sola vez."
    )

    if not borrow_holders:
        st.info("No hay prestatarios que analizar en este momento.")
        return None

    if not st.session_state.get("salud_calculada"):
        st.caption(
            f"Requiere consultar la posición de los **{len(borrow_holders)} prestatarios** "
            "uno a uno en el pool: tarda un par de minutos la primera vez y luego queda "
            "en caché 30 minutos."
        )
        if st.button("🩺 Calcular salud agregada", type="primary", key="btn_salud"):
            st.session_state["salud_calculada"] = True
            st.rerun()
        return None

    with st.spinner(f"Consultando {len(borrow_holders)} posiciones en el pool…"):
        salud = _al.salud_agregada(tuple(sorted(borrow_holders)), API_KEY)

    if not salud or not salud.get("n_posiciones"):
        st.warning("No se pudo reconstruir la posición agregada en este momento.")
        return None

    hf = salud["health_factor"]
    emoji, etiqueta, color = _al.nivel_riesgo(hf)

    # delta_color="off": estos subtítulos son contexto, no variaciones. Sin
    # apagarlo, Streamlit los pinta con flecha verde hacia arriba y "liquidación
    # si < 1" acaba leyéndose como una buena noticia.
    k1, k2, k3, k4 = st.columns(4)
    k1.metric(f"{emoji} Health Factor agregado", f"{hf:,.3f}",
              f"{etiqueta} · liquidación si < 1", delta_color="off",
              help=(
                  "El mercado entero tratado como una sola cuenta:\n\n"
                  "**HF = Σ(colateral × umbral) ÷ Σ(deuda)**\n\n"
                  "No es una media de los Health Factor individuales —eso no tendría "
                  "sentido matemático—, sino el mismo cociente que aplica el protocolo, "
                  "calculado sobre las sumas. Por encima de 1 el conjunto está cubierto; "
                  "por debajo, la deuda superaría lo que el colateral puede respaldar.\n\n"
                  "⚠️ Que el agregado vaya holgado NO impide liquidaciones: cada posición "
                  "se liquida por su cuenta cuando SU HF baja de 1."))
    k2.metric("🏠 Colateral respaldando deuda", f"${salud['colateral_usd']:,.0f}",
              f"umbral medio {salud['umbral_medio'] * 100:,.1f}%", delta_color="off",
              help=(
                  "Valor de los tokens inmobiliarios depositados por quienes TIENEN deuda "
                  "viva. No incluye el colateral de quien solo aporta y no ha pedido "
                  "prestado, porque ese no respalda ningún préstamo.\n\n"
                  "Se valora con el **oráculo del pool**, que es el precio con el que el "
                  "protocolo decide las liquidaciones — no con el precio de emisión del "
                  "máster, que puede diferir.\n\n"
                  "El *umbral medio* es el porcentaje del colateral que el protocolo "
                  "reconoce como respaldo efectivo, ponderado por la cesta de cada uno."))
    k3.metric("💳 Deuda total", f"${salud['deuda_usd']:,.0f}",
              f"{salud['n_posiciones']} posiciones abiertas", delta_color="off",
              help=(
                  "Suma de USDT y USDC prestados y aún no devueltos, **con los intereses "
                  "ya devengados incluidos**: es la cifra que el protocolo usaría hoy para "
                  "liquidar, no el principal que se pidió en su día.\n\n"
                  "Cuenta solo posiciones con deuda viva por encima de un céntimo."))
    k4.metric("🛡️ Sobrecolateralización", f"{salud['sobrecolateral']:,.2f}×",
              f"LTV medio {salud['ltv_medio'] * 100:,.1f}%", delta_color="off",
              help=(
                  "Cuántos dólares de inmueble hay depositados por cada dólar prestado. "
                  "Es la lectura directa de la cobertura, sin ponderar por el umbral de "
                  "liquidación.\n\n"
                  "El **LTV medio** es la misma relación al revés: de cada 100 $ de "
                  "inmueble aportado, cuántos están prestados. Es el dato que se compara "
                  "con la política de riesgo del protocolo."))

    # El agregado por sí solo se lee como una tranquilidad que no ha demostrado:
    # es un cociente de sumas, y una posición al borde no lo mueve. El reparto
    # de abajo es lo que dice dónde está el riesgo de verdad.
    en_riesgo = sum(t["pct_deuda"] for t in salud["tramos"] if t["desde"] < 1.3)
    st.info(
        f"El agregado ({hf:,.3f}) mide la solvencia del pool **como bloque**, no la de "
        f"cada prestatario: las liquidaciones ocurren posición a posición. Ahora mismo "
        f"un **{en_riesgo:,.1f}% de la deuda** está en posiciones con Health Factor por "
        f"debajo de 1,3, aunque el conjunto vaya holgado."
    )

    g1, g2 = st.columns(2)

    with g1:
        st.markdown("**Dónde está la deuda, por riesgo de la posición**",
                    help=(
                        "Cada prestatario tiene su propio Health Factor. Aquí se reparte "
                        "la deuda —no el número de prestatarios— entre los cinco tramos "
                        "de riesgo.\n\n"
                        "Se pondera por deuda a propósito: doscientos préstamos de mil "
                        "dólares no pesan lo mismo que uno de seiscientos mil, y contar "
                        "posiciones daría una foto tranquilizadora que no se sostiene.\n\n"
                        "**Cómo leerlo:** lo que hay a la izquierda de la barra (rojo y "
                        "naranja) es la deuda que se liquidaría con caídas pequeñas del "
                        "colateral. Es la métrica que un LP mira antes que el agregado."))
        fig = go.Figure()
        for t in salud["tramos"]:
            if t["deuda"] <= 0:
                continue
            fig.add_trace(go.Bar(
                x=[t["pct_deuda"]], y=["Deuda"], orientation="h", name=t["etiqueta"],
                marker_color=t["color"],
                hovertemplate=(f"{t['etiqueta']}<br>${t['deuda']:,.0f}"
                               f"<br>{t['posiciones']} posiciones<extra></extra>"),
            ))
        fig.update_layout(
            template=TEMPLATE_PLOTLY, barmode="stack", height=200,
            margin=dict(t=10, b=10, l=10, r=10),
            xaxis_title="% de la deuda total", yaxis=dict(showticklabels=False),
            legend=dict(orientation="h", y=-0.35),
        )
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(
            pd.DataFrame([{
                "": t["emoji"], "Tramo": t["etiqueta"],
                "HF": (f"< {t['hasta']:.1f}" if t["desde"] == 0 else
                       f"≥ {t['desde']:.1f}" if t["hasta"] is None else
                       f"{t['desde']:.1f} – {t['hasta']:.1f}"),
                "Posiciones": t["posiciones"], "Deuda": t["deuda"], "% deuda": t["pct_deuda"],
            } for t in salud["tramos"]]).style.format({"Deuda": "${:,.0f}", "% deuda": "{:,.1f}%"}),
            use_container_width=True, hide_index=True,
        )

    with g2:
        st.markdown("**Si el valor del colateral cayera…**",
                    help=(
                        "Test de estrés. El Health Factor es proporcional al valor del "
                        "colateral, así que una caída del x% deja cada posición en "
                        "**HF × (1 − x)**. Se cuenta qué deuda quedaría por debajo de 1 "
                        "—es decir, liquidable— en cada escenario.\n\n"
                        "No predice nada: mide la sensibilidad de la cartera actual a una "
                        "bajada del precio de los inmuebles. Es la pregunta que hará "
                        "cualquier LP, y conviene tener la respuesta antes que él."))
        est = salud["estres"]
        fig2 = go.Figure(go.Bar(
            x=[f"−{e['caida_pct']}%" for e in est],
            y=[e["pct_deuda"] for e in est],
            marker_color=[t for t in ("#ca8a04", "#ea580c", "#dc2626", "#dc2626", "#991b1b")],
            text=[f"{e['pct_deuda']:,.0f}%" for e in est], textposition="outside",
            customdata=[[e["deuda"], e["posiciones"]] for e in est],
            hovertemplate=("Caída %{x}<br>Deuda liquidable: $%{customdata[0]:,.0f}"
                           "<br>%{customdata[1]} posiciones<extra></extra>"),
        ))
        fig2.update_layout(
            template=TEMPLATE_PLOTLY, height=260, margin=dict(t=30, b=10, l=10, r=10),
            yaxis_title="% de la deuda que quedaría liquidable",
            yaxis_range=[0, 105],
        )
        st.plotly_chart(fig2, use_container_width=True)
        st.caption(
            f"El Health Factor cae en proporción directa al valor del colateral, así que una "
            f"caída del x% deja cada posición en HF×(1−x). El conjunto llega al umbral con una "
            f"caída del **{salud['margen_caida'] * 100:,.1f}%**, pero las primeras liquidaciones "
            f"empiezan mucho antes."
        )

    st.caption(
        f"Posición más grande: ${salud['mayor_posicion']:,.0f} "
        f"({salud['pct_mayor']:,.1f}% de la deuda) · HF mínimo {salud['hf_minimo']:,.3f} · "
        f"mediana {salud['hf_mediana']:,.3f}. El colateral se valora con el **oráculo del pool**, "
        f"que es el que decide las liquidaciones — no con el precio de emisión del máster."
        + (f" · ⚠️ {salud['fallidas']} wallets no se pudieron consultar."
           if salud.get("fallidas") else "")
    )

    # Se devuelve para que el informe PDF pueda incluir exactamente lo mismo
    # que se está viendo, sin recalcularlo.
    return salud


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

# La salud agregada necesita la lista de prestatarios, que sale del escaneo
# histórico de más abajo. Se reserva el hueco aquí para que el titular del
# mercado quede arriba, donde se busca, y se rellena cuando el dato existe.
st.markdown("---")
_hueco_salud = st.container()
salud_mercado = None   # lo rellena render_salud_agregada si el usuario la calcula

st.markdown("---")

# ── Histórico de capital aportado / prestado / disponible ────────────────────
st.subheader("📈 Evolución histórica — USDT / USDC")
st.caption(
    "Capital reconstruido on-chain a partir de los eventos Transfer (mint/burn) de los aTokens y "
    "debt tokens; tipos (eje derecho) a partir de los eventos ReserveDataUpdated del Pool. "
    "Todo desde el despliegue del contrato. Puede tardar más la primera vez (se cachea 6h). "
    "⚠️ El capital refleja depósitos menos retiradas (principal); no incorpora el interés acumulado "
    "día a día, por lo que queda ligeramente por debajo de las cifras del snapshot en vivo de más arriba. "
    "📐 Los tipos se muestran como media acumulada desde el despliegue hasta cada momento (no el tipo "
    "puntual): al ser un interés variable, esta media refleja mejor lo que experimenta un inversor a "
    "largo plazo que el tipo instantáneo, muy ruidoso al cambiar en cada depósito/préstamo/repago."
)

stable_tokens = snapshot.get("stable_tokens", {})
historical = {}
if stable_tokens:
    with st.spinner("Reconstruyendo histórico on-chain (puede tardar 1-2 minutos la primera vez)…"):
        historical = build_historical_series_batch(stable_tokens)

historical_resumen = {}  # resumen por activo, reutilizado en el PDF y el mensaje de WhatsApp
historical_dfs = {}      # df_m por activo, reutilizado para renderizar el gráfico en el PDF

for sym in ("USDT", "USDC"):
    hist = historical.get(sym)
    if not hist or hist["supply_series"].empty:
        st.info(f"No se pudo reconstruir el histórico de {sym} en este momento.")
        continue

    df_s = hist["supply_series"].rename(columns={"total": "aportado"})
    df_b = hist["borrow_series"].rename(columns={"total": "prestado"})
    df_m = pd.merge(df_s, df_b, on="fecha", how="outer").sort_values("fecha").ffill().fillna(0)
    df_m["disponible"] = df_m["aportado"] - df_m["prestado"]

    df_r = hist.get("rate_series")
    if df_r is not None and not df_r.empty:
        df_r = df_r.rename(columns={"supply_apr": "supply_apr_medio", "borrow_apr": "borrow_apr_medio"})
        df_m = pd.merge(df_m, df_r, on="fecha", how="left").sort_values("fecha")
        df_m[["supply_apr_medio", "borrow_apr_medio"]] = df_m[["supply_apr_medio", "borrow_apr_medio"]].ffill()

    historical_resumen[sym] = {
        "fecha_inicio": df_m["fecha"].iloc[0],
        "dias_periodo": (df_m["fecha"].iloc[-1] - df_m["fecha"].iloc[0]).days,
        "aportado_inicio": df_m["aportado"].iloc[0],
        "aportado_hoy": df_m["aportado"].iloc[-1],
        "prestado_inicio": df_m["prestado"].iloc[0],
        "prestado_hoy": df_m["prestado"].iloc[-1],
        "supply_apr_medio": df_m["supply_apr_medio"].iloc[-1] if "supply_apr_medio" in df_m.columns else None,
        "borrow_apr_medio": df_m["borrow_apr_medio"].iloc[-1] if "borrow_apr_medio" in df_m.columns else None,
    }
    historical_dfs[sym] = df_m

    fig = build_historical_fig(df_m, sym, dark=True)
    st.plotly_chart(fig, use_container_width=True)

    st.download_button(
        f"⬇️ Descargar CSV — {sym}",
        data=df_m.to_csv(index=False).encode("utf-8"),
        file_name=f"rnt_lend_{sym.lower()}_historico_{date.today().strftime('%Y%m%d')}.csv",
        mime="text/csv",
        key=f"csv_hist_{sym}",
    )

st.markdown("---")

# ── Concentración de holders (suministradores de liquidez y prestatarios) ────
st.subheader("🐋 Concentración de holders")
st.caption(
    "Distribución de direcciones por tramos de valor (USDT + USDC combinados), a partir de los "
    "balances actuales reconstruidos on-chain. Ballenas ≥ \\$100k · Tiburones \\$25k–100k · "
    "Delfines \\$5k–25k · Peces < \\$5k."
)

supply_holders, borrow_holders = {}, {}
if historical:
    supply_holders = combinar_holders(*[h["supply_holders"] for h in historical.values()])
    borrow_holders = combinar_holders(*[h["borrow_holders"] for h in historical.values()])

    with _hueco_salud:
        salud_mercado = render_salud_agregada(borrow_holders)

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
            df_wallets = holders_a_dataframe(holders)
            st.download_button(
                "⬇️ Descargar CSV — saldos por wallet",
                data=df_wallets.to_csv(index=False).encode("utf-8"),
                file_name=f"rnt_lend_{titulo.split()[1].lower()}_{date.today().strftime('%Y%m%d')}.csv",
                mime="text/csv",
                key=f"csv_holders_{titulo}",
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

df_col = pd.DataFrame()
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

st.markdown("---")

# ── Exportar informe ──────────────────────────────────────────────────────────
st.subheader("📤 Exportar informe")

st.markdown("""
    <style>
    .st-key-btn_crear_wa_aave button {
        background-color: #25D366 !important;
        color: #ffffff !important;
        border: none !important;
        font-weight: 700 !important;
        border-radius: 8px !important;
    }
    .st-key-btn_crear_wa_aave button:hover {
        background-color: #1EBE5D !important;
        color: #ffffff !important;
    }
    .st-key-btn_crear_wa_aave button:active {
        background-color: #128C7E !important;
    }
    </style>
""", unsafe_allow_html=True)

col_pdf, col_wa = st.columns([1, 1])

with col_pdf:
    if st.button("📥 Generar PDF", type="primary", use_container_width=True):
        with st.spinner("Generando PDF…"):
            pdf_bytes = generar_pdf_aave(
                stables, df_col, supply_holders, borrow_holders, historical_resumen,
                historical_dfs, salud_mercado,
            )
        st.download_button(
            label="⬇️ Descargar PDF",
            data=pdf_bytes,
            file_name=f"Reental_RNT_Lend_{date.today().strftime('%Y%m%d')}.pdf",
            mime="application/pdf",
            type="primary",
        )

with col_wa:
    if st.button("💬 Crear mensaje para enviar por WhatsApp", key="btn_crear_wa_aave", use_container_width=True):
        lineas = []
        lineas.append("🏦 *Reental Wealth — Mercado RNT Lend*")
        lineas.append(f"📅 {date.today().strftime('%d/%m/%Y')}")
        lineas.append("─" * 30)

        for sym in ("USDT", "USDC"):
            info = stables.get(sym)
            if not info:
                continue
            hist_r = historical_resumen.get(sym)
            util_txt = f" · Util. {info['utilizacion'] * 100:.1f}%" if info.get("utilizacion") is not None else ""
            lineas.append(f"\n💰 *{sym}*")
            lineas.append(f"  Aportado: *${info['supply_total']:,.0f}* (APR actual {info['supply_apr'] * 100:.2f}%)")
            lineas.append(f"  Prestado: *${info['borrow_total']:,.0f}* (APR actual {info['borrow_apr'] * 100:.2f}%{util_txt})")
            if hist_r and hist_r.get("supply_apr_medio") is not None:
                lineas.append(
                    f"  📊 APR medio histórico: Supply {hist_r['supply_apr_medio'] * 100:.2f}% · "
                    f"Borrow {hist_r['borrow_apr_medio'] * 100:.2f}% ({hist_r['dias_periodo']} días)"
                )
            if hist_r:
                delta = hist_r["aportado_hoy"] - hist_r["aportado_inicio"]
                lineas.append(f"  📈 Crecimiento del aportado en el periodo: +${delta:,.0f}")

        lineas.append("\n🐋 *Concentración de holders*")
        lineas.append(f"  💰 Suministradores: {len(supply_holders)} direcciones")
        lineas.append(f"  📉 Prestatarios: {len(borrow_holders)} direcciones")

        if not df_col.empty:
            top1 = df_col.iloc[0]
            lineas.append("\n🔒 *Colateral*")
            lineas.append(f"  Total estimado: *${df_col['valor_estimado'].sum():,.0f}*")
            lineas.append(f"  Mayor proyecto: {top1['proyecto']} (${top1['valor_estimado']:,.0f})")

        lineas.append("\n" + "─" * 30)
        lineas.append("_⚠️ Datos on-chain del pool RNT Lend. No constituye consejo de inversión._")

        mensaje_wa = "\n".join(lineas)

        # El navegador solo permite escribir al portapapeles desde un gesto del
        # usuario DENTRO del iframe, así que el botón de copiar vive en el componente.
        msg_js = json.dumps(mensaje_wa)
        st.components.v1.html(f"""
        <button id="copiar-wa-aave" style="
            width:100%; padding:12px 20px; font-size:16px; font-weight:700;
            font-family:'Source Sans Pro',sans-serif; cursor:pointer;
            background:#25D366; color:#fff; border:none; border-radius:8px;">
            📋 Copiar al portapapeles
        </button>
        <div id="copiado-ok-aave" style="display:none; margin-top:8px; text-align:center;
            font-family:'Source Sans Pro',sans-serif; color:#25D366; font-weight:600;">
            ✅ ¡Copiado! Pégalo directamente en WhatsApp.
        </div>
        <script>
        const MSG = {msg_js};
        document.getElementById('copiar-wa-aave').addEventListener('click', function() {{
            function ok() {{
                document.getElementById('copiado-ok-aave').style.display = 'block';
            }}
            if (navigator.clipboard && navigator.clipboard.writeText) {{
                navigator.clipboard.writeText(MSG).then(ok).catch(function() {{ fallback(); }});
            }} else {{
                fallback();
            }}
            function fallback() {{
                var ta = document.createElement('textarea');
                ta.value = MSG;
                ta.style.position = 'fixed'; ta.style.left = '-9999px';
                document.body.appendChild(ta);
                ta.focus(); ta.select();
                try {{ document.execCommand('copy'); ok(); }} catch(e) {{}}
                document.body.removeChild(ta);
            }}
        }});
        </script>
        """, height=100)

st.caption(
    f"Última actualización: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')} · "
    f"Fuente: contrato RNT Lend Pool `{RNT_LEND_POOL}` en Polygon (lectura on-chain vía Etherscan)."
)
