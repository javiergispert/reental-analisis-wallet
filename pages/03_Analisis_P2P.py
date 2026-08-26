"""
Análisis de Oportunidades P2P — Reental Wealth
Fuente de datos: wallet OTC de Reental (tokens disponibles - reservas activas)
                + ofertas activas de terceros publicadas en esta herramienta.
"""
from __future__ import annotations   # permite `dict | None` en Python 3.9


import io
import json
import os
import sys
import time
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

import pandas as pd
import requests
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from utils import (fetch_all_account_txs, fetch_all_token_txs, load_master_projects,
                   parse_pct, parse_float_val, strip_accents)
from reental_tokens import codigo_proyecto_atoken
# Disponibilidad de las ofertas de terceros: misma fuente que la página OTC, para
# que las dos respondan lo mismo a "cuántos tokens se pueden vender de verdad".
import otc_saldos as _saldos
import mercado_secundario as _mkt
import recarga as _recarga
# Streamlit no reimporta lo que ya está en sys.modules: tras un despliegue esta
# página puede convivir con una versión anterior de sus módulos. Se refrescan en
# orden de dependencia (p2p_mercado lo usa mercado_secundario).
_recarga.refrescar("p2p_mercado", "otc_saldos", "otc_storage", "mercado_secundario")
import ui_kpi
from ui_kpi import kpi_card
import plotly.graph_objects as go

# ── Constantes ────────────────────────────────────────────────────────────────
OTC_WALLET     = os.getenv("OTC_WALLET", "0xce0719ec1bda336ba069c6961ad167767829301a").lower()
API_KEY        = os.getenv("ETHERSCAN_API_KEY", "")
ETHERSCAN_BASE = "https://api.etherscan.io/v2/api"
POLYGON_CHAIN  = 137
SPREADSHEET_ID = "13Q0n7egbAIJSU9UvwwDucd3MUQ48Q44eoMwsPT-PmGs"
TAB_RESERVAS   = "Reservas"
TAB_OFERTAS    = "Ofertas"
CACHE_TTL      = 3600
MIN_TOKENS     = 20   # mínimo tokens disponibles para entrar en el ranking

# Paleta corporativa Reental
DORADO    = colors.HexColor("#F5A623")   # acento primario
NAVY_OSC  = colors.HexColor("#0D1B2E")  # fondo principal / primera columna
NAVY_MED  = colors.HexColor("#112240")  # fondo secundario / cabecera datos
AZUL_MED  = colors.HexColor("#3B82F6")  # acento secundario
GRIS_CLAR = colors.HexColor("#F2F4F8")  # filas alternas
BLANCO    = colors.white

CATEGORIAS = {
    "SR (SuperReentel)": {"r_hoy_total": "r_hoy_total_sr",  "r_hoy_ann": "r_hoy_ann_sr",  "r_rec_ann": "r_rec_ann_sr",  "r_plusv": "r_plusv_sr"},
    "RP (ReentelPro)":   {"r_hoy_total": "r_hoy_total_rp",  "r_hoy_ann": "r_hoy_ann_rp",  "r_rec_ann": "r_rec_ann_rp",  "r_plusv": "r_plusv_rp"},
    "Reentel":           {"r_hoy_total": "r_hoy_total_reentel", "r_hoy_ann": "r_hoy_ann_reentel", "r_rec_ann": "r_rec_ann_reentel", "r_plusv": "r_plusv_reentel"},
}

TIPO_RENTA_LABELS = {
    "todas":      "Todas",
    "final":      "Solo renta final",
    "recurrente": "Solo renta recurrente",
    "mixto":      "Mixta (recurrente + final)",
}

# ── Google Sheets (módulo común otc_storage) ──────────────────────────────────
# Misma capa de persistencia que 02_OTC.py: lee el JSON troceado por la columna
# A y lo reensambla. ANTES esta página leía solo la celda A1, y al superar las
# reservas los 45.000 caracteres (2 celdas) obtenía un JSON truncado → lista
# vacía → no restaba reservas → mostraba saldos BRUTOS. Al centralizar la
# lectura, ese fallo no puede volver a desincronizarse entre páginas.
import otc_storage as _store

def load_reservas_otc() -> list:
    return _store.read_list(TAB_RESERVAS, fresh=True)

def load_ofertas_otc() -> list:
    return _store.read_list(TAB_OFERTAS, fresh=True)

# ── Saldos wallet OTC desde Etherscan ─────────────────────────────────────────

@st.cache_data(show_spinner=False, ttl=CACHE_TTL)
def fetch_otc_raw_balances(wallet: str, api_key: str) -> dict:
    """
    Devuelve {contract_addr_lower: saldo_bruto} para TODOS los tokens ERC-20
    de la wallet OTC. Solo parámetros simples para que el hash de caché funcione.
    La resolución de aTokens y filtrado por proyecto se hace en construir_disponibilidad.
    """
    # Etherscan limita tokentx a 1000 resultados por llamada: hay que paginar
    # o los saldos se calculan solo con los transfers más antiguos.
    txs = fetch_all_token_txs(wallet, api_key)

    raw = {}
    for tx in txs:
        contract  = tx["contractAddress"].lower()
        dec       = int(tx.get("tokenDecimal") or 18)
        value     = int(tx["value"]) / (10 ** dec)
        to_addr   = tx["to"].lower()
        from_addr = tx["from"].lower()
        sym       = tx.get("tokenSymbol", "")
        name      = tx.get("tokenName", "")
        if to_addr == wallet and from_addr != wallet:
            entry = raw.setdefault(contract, {"saldo": 0.0, "sym": sym, "name": name})
            entry["saldo"] += value
        elif from_addr == wallet and to_addr != wallet:
            entry = raw.setdefault(contract, {"saldo": 0.0, "sym": sym, "name": name})
            entry["saldo"] -= value
    return {k: v for k, v in raw.items() if v["saldo"] >= 0.001}


def construir_disponibilidad(master_df: pd.DataFrame) -> list:
    """
    Devuelve lista de dicts con los tokens disponibles (OTC Reental + terceros).
    Cada dict: {project_id, token_address, precio_p2p, tokens_disponibles, fuente}
    """
    # Índices del master
    project_by_addr = {}
    project_by_id   = {}
    for _, row in master_df.iterrows():
        if row.get("token_address"):
            project_by_addr[row["token_address"]] = row.to_dict()
        project_by_id[row["id"].lower()] = row.to_dict()

    known_addresses = set(project_by_addr.keys())
    nombre_to_addr  = {row["nombre"].lower(): addr for addr, row in project_by_addr.items()}

    # 1. Saldos brutos de todos los tokens en la wallet OTC
    raw_balances = fetch_otc_raw_balances(OTC_WALLET, API_KEY)

    # Resolver aTokens Aave → token subyacente y filtrar por tokens Reental conocidos
    atoken_map = {}
    brutos = {}
    for contract, data in raw_balances.items():
        sym  = data["sym"]
        name = data["name"]
        # La grafía del aToken varía entre proyectos (aMatReental-CME-1 vs
        # aMatREENTAL-CAR-2): el reconocimiento no distingue mayúsculas.
        _codigo = codigo_proyecto_atoken(sym, name)
        if _codigo:
            suffix = _codigo.lower()
            underlying = project_by_id.get(suffix, {}).get("token_address")
            if not underlying:
                for n, a in nombre_to_addr.items():
                    if suffix in n:
                        underlying = a
                        break
            if underlying:
                atoken_map[contract] = underlying.lower()

        effective = atoken_map.get(contract, contract if contract in known_addresses else None)
        if effective is None:
            continue
        brutos[effective] = brutos.get(effective, 0.0) + data["saldo"]

    # 2. Restar reservas activas
    reservas = load_reservas_otc()
    reservado = {}
    for r in reservas:
        if r.get("estado") in ("completada", "cancelada"):
            continue
        # Las reservas contra ofertas de TERCEROS salen de la wallet del
        # inversor, no de la custodia de Reental: contarlas aquí hacía que el
        # stock propio apareciera mermado por tokens que nunca fueron suyos.
        # Las reservas antiguas no llevan `tipo_origen` y son todas de Reental.
        if r.get("tipo_origen") == "tercero":
            continue
        addr = r.get("token_address", "").lower()
        reservado[addr] = reservado.get(addr, 0.0) + float(r.get("n_tokens", 0))

    disponibles = []

    # Wallet OTC
    try:
        precios_otc = _load_precios_otc_cached()
    except Exception:
        precios_otc = {}

    for addr, saldo_bruto in brutos.items():
        disp = max(0.0, saldo_bruto - reservado.get(addr, 0.0))
        if disp < 0.001:
            continue
        proj = project_by_addr.get(addr, {})
        pid  = proj.get("id", "")
        if not pid:
            continue
        precio_otc = precios_otc.get(addr, {}).get("precio_otc") or proj.get("precio_emision") or 0
        disponibles.append({
            "project_id":        pid,
            "token_address":     addr,
            "tokens_disponibles": disp,
            "precio_p2p":        precio_otc,
            "fuente":            "OTC Reental",
        })

    # Ofertas de terceros activas
    for o in load_ofertas_otc():
        if o.get("estado") != "activa":
            continue
        precio   = float(o.get("precio_acordado") or o.get("precio_venta") or 0)
        addr     = o.get("token_address", "").lower()
        pid      = o.get("proyecto_id", "")

        # Fallback: resolver proyecto_id desde token_address si no está guardado
        if not pid and addr:
            proj = project_by_addr.get(addr, {})
            pid  = proj.get("id", "")

        if precio <= 0 or not pid:
            continue

        # Lo vendible NO es la cifra que publicó el comercial, sino la MENOR
        # entre esa y el saldo real del inversor (wallet + colateral en Aave),
        # descontando lo ya reservado. Tomar `n_tokens` sin comprobar metía en
        # el ranking oportunidades que no se podían ejecutar.
        est = _saldos.estado_oferta(o, reservas, API_KEY, _saldo_en_wallet)
        if not est["ok"]:
            continue                    # sin saldo verificable no se ofrece
        if est["disponible"] < 0.001:
            continue                    # agotada o sin respaldo en la cadena

        disponibles.append({
            "project_id":        pid,
            "token_address":     addr,
            "tokens_disponibles": est["disponible"],
            "precio_p2p":        precio,
            "fuente":            "Tercero",
        })

    return disponibles


@st.cache_data(show_spinner=False, ttl=3600)
def _saldo_en_wallet(wallet: str, token_address: str, api_key: str) -> float:
    """Tokens del proyecto sueltos en la wallet del inversor. -1.0 si falla.
    Pagina, porque la consulta directa se corta a 10.000 resultados."""
    w = wallet.lower()
    try:
        txs = fetch_all_account_txs(w, api_key, action="tokentx",
                                    contractaddress=token_address.lower())
    except Exception:
        return -1.0
    if txs is None:
        return -1.0
    bal = 0.0
    for tx in txs:
        dec   = int(tx.get("tokenDecimal") or 18)
        value = int(tx["value"]) / (10 ** dec)
        to_, from_ = tx["to"].lower(), tx["from"].lower()
        if to_ == w and from_ != w:
            bal += value
        elif from_ == w and to_ != w:
            bal -= value
    return round(bal, 6)


def _load_precios_otc_cached() -> dict:
    # Delegado al módulo común (caché de 6 s incluida). Antes leía solo A1.
    return _store.read_dict(_store.TAB_PRECIOS)


# ── Cálculo del ranking ───────────────────────────────────────────────────────

def _num(v):
    """Convierte a float tratando NaN como ausencia.

    Pandas convierte los None de una columna numérica en NaN, y NaN es un mal
    centinela: `nan is None` es False, `nan <= 0` es False y `bool(nan)` es
    True, así que `nan or 0` devuelve nan. Con eso, los proyectos sin dato
    burlaban los guardianes y llegaban al ranking con todo a nan.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f          # f != f solo es cierto para NaN


def _filas_ranking(master_df: pd.DataFrame, disponibles: list,
                   categoria: str, tipo_renta: str) -> pd.DataFrame:
    """Construye TODAS las filas candidatas, en plazo y fuera de él. La separan
    `calcular_ranking` y `calcular_fuera_de_plazo`, que comparten así el ajuste
    por precio P2P en vez de duplicarlo."""
    cols = CATEGORIAS[categoria]

    master_by_id = {row["id"].lower(): row for _, row in master_df.iterrows()}

    rows = []
    for d in disponibles:
        if d["tokens_disponibles"] < MIN_TOKENS:
            continue

        pid   = d["project_id"].lower()
        m     = master_by_id.get(pid)
        if m is None:
            continue

        if tipo_renta != "todas" and m["tipo_renta"] != tipo_renta:
            continue

        precio_emision = m.get("precio_emision") or 0
        precio_p2p     = d["precio_p2p"]
        if precio_emision <= 0 or precio_p2p <= 0:
            continue

        # Se anualiza sobre los meses que faltan HASTA EL CIERRE (DB), no sobre
        # los de renta pendiente (AS): el dinero del inversor sigue inmovilizado
        # aunque la renta ya haya terminado. Un proyecto cuya fecha de fin ya
        # pasó no tiene horizonte sobre el que anualizar — se marca «fuera de
        # plazo» y se trata aparte en vez de descartarlo en silencio.
        # Un proyecto cerrado no es una oportunidad de inversión, por mucho que
        # queden tokens suyos en la wallet.
        if "CERRAD" in str(m.get("estado", "")).upper():
            continue

        meses = _num(m.get("meses_hasta_fin")) or 0.0
        fuera_de_plazo = meses <= 0

        r_hoy_ann_raw   = _num(m.get(cols["r_hoy_ann"]))
        r_hoy_total_raw = _num(m.get(cols["r_hoy_total"]))
        r_rec_ann_raw   = _num(m.get(cols["r_rec_ann"]))
        r_plusv_raw     = _num(m.get(cols["r_plusv"]))

        # Sin rentabilidad pendiente no hay nada que rankear. Se comprueba sobre
        # el TOTAL, no sobre la anualizada: un proyecto fuera de plazo tiene
        # total pero no anualizada, y exigir esta última lo expulsaría.
        if r_hoy_total_raw is None:
            continue

        # Ajuste por precio P2P vs precio de emisión.
        # El master calcula % sobre precio_emision. Si el inversor paga
        # un precio distinto, hay que recalcular correctamente:
        #
        # - Recurrentes: los dividendos en $ no cambian, solo cambia la base.
        #     r_rec_adj = r_rec_raw * (emision / p2p)
        #
        # - Plusvalía: valor_final = emision * (1 + r_plusv_raw)
        #     r_plusv_adj = valor_final/p2p - 1 = adj*(1+r_plusv_raw) - 1
        #
        # - Total pendiente: mismo principio que plusvalía.
        #     r_total_adj = adj*(1+r_hoy_total_raw) - 1
        #
        # - Alquiler pendiente: se deriva del total para garantizar consistencia.
        #     r_alquiler_pend = r_total_adj - r_plusv_adj
        #
        # - Anualizada: se recalcula desde el total ajustado, no se escala.
        #     r_ann = (1+r_total_adj)^(12/meses) - 1
        adj = precio_emision / precio_p2p

        # Acumulación en prórroga (columna DC del maestro). Cuando un proyecto
        # sobrepasa su fecha de fin, el precio OTC/P2P puede fijarse asumiendo
        # que el activo sigue revalorizándose. Si el precio la incorpora y este
        # cálculo no, se comparan dos bases distintas y sale un pendiente
        # negativo que no significa nada: CDS-1 daba −18,52% cotizando a 135
        # cuando su retorno real bajo esa hipótesis es +5,19%.
        #
        # El reloj arranca en la fecha de fin ORIGINAL, no en la reestimada: el
        # plazo prometido ya estaba cubierto por la plusvalía estimada.
        _tasa_acum = _num(m.get("tasa_acum_prorroga")) or 0.0
        _fin_orig  = m.get("fecha_fin_original")
        _retraso   = 0.0
        if _tasa_acum and _fin_orig:
            _retraso = max(0.0, (date.today() - _fin_orig).days / 30.44)
        # Se acumula por el retraso ya transcurrido (que el precio de hoy ya
        # refleja) y por lo que queda hasta el cierre (que es lo que gana quien
        # compra ahora).
        acumulado = _tasa_acum * (_retraso + max(0.0, meses))

        r_rec_ann       = (r_rec_ann_raw or 0) * adj
        r_plusv         = adj * (1 + (r_plusv_raw or 0) + acumulado) - 1
        r_hoy_total     = adj * (1 + (r_hoy_total_raw or 0) + acumulado) - 1
        # Sin la hipótesis, para poder contrastar en la UI qué parte del retorno
        # depende de ella.
        r_hoy_total_est = adj * (1 + (r_hoy_total_raw or 0)) - 1
        r_alquiler_pend = r_hoy_total - r_plusv
        # Sin horizonte no hay anualizada posible: depende de cuándo cierre, y
        # un número ahí sería una fecha inventada. Lo que SÍ es firme en un
        # proyecto fuera de plazo es la renta que sigue cobrando (`r_rec_ann`,
        # dato real observado), y eso es lo que se muestra como suelo.
        # DOS convenciones, ambas legítimas y ambas en uso:
        #   TIR (compuesta): (1+total)^(12/meses)-1. Es el rendimiento financiero
        #     real y lo único comparable entre proyectos de distinta duración,
        #     porque penaliza el plazo. Por eso el ranking ordena por esta.
        #   Simple: total/meses*12. Reparte linealmente, da un número mayor y es
        #     la que usa el maestro en su columna 33 y el material comercial.
        # Se muestran las dos: tenerlas conviviendo sin distinguir hacía que el
        # ranking contradijera a la ficha del proyecto (NCA-1: 15,32% vs 17,00%).
        r_hoy_ann        = None if fuera_de_plazo else (1 + r_hoy_total) ** (12 / meses) - 1
        r_hoy_ann_simple = None if fuera_de_plazo else r_hoy_total * 12 / meses

        rows.append({
            "_id":              m["id"],
            "_nombre":          m["nombre"],
            "_token_address":   (d.get("token_address") or "").lower(),
            "_precio_p2p":      precio_p2p,
            "_divisa":          m["divisa"],
            "_meses":           meses,
            "_tipo_renta":      m["tipo_renta"],
            "_tip_dividendo":   m["tip_dividendo"],
            "_tokens_disp":     d["tokens_disponibles"],
            "_colateralizable": m.get("colateralizable", False),
            "_fuente":          d["fuente"],
            "_r_hoy_ann":       r_hoy_ann,
            "_r_hoy_ann_simple": r_hoy_ann_simple,
            "_r_hoy_total":     r_hoy_total,
            "_r_rec_ann":       r_rec_ann,
            "_r_alquiler_pend": r_alquiler_pend,
            "_r_plusv":         r_plusv,
            "_fuera_plazo":     fuera_de_plazo,
            "_meses_retraso":   _retraso,
            "_tasa_acum":       _tasa_acum,
            "_acumulado":       acumulado,
            "_r_hoy_total_est": r_hoy_total_est,
        })

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(rows)


def calcular_ranking(master_df: pd.DataFrame, disponibles: list,
                     categoria: str, tipo_renta: str, top_n: int) -> pd.DataFrame:
    """Top de oportunidades ordenado por rentabilidad anualizada pendiente.

    Excluye los proyectos fuera de plazo: no tienen anualizada con la que
    ordenarse. Van en su propia tabla, no se pierden.
    """
    df = _filas_ranking(master_df, disponibles, categoria, tipo_renta)
    if df.empty:
        return df
    en_plazo = (df[~df["_fuera_plazo"]]
                .sort_values("_r_hoy_ann", ascending=False)
                .head(top_n).reset_index(drop=True))
    en_plazo["_score"] = range(1, len(en_plazo) + 1)
    return en_plazo


def calcular_fuera_de_plazo(master_df: pd.DataFrame, disponibles: list,
                            categoria: str, tipo_renta: str) -> pd.DataFrame:
    """Proyectos con saldo disponible cuya fecha estimada de fin ya pasó.

    Se calculan con la misma función para no duplicar la lógica de ajuste por
    precio P2P; aquí solo se filtra el otro lado.
    """
    todos = _filas_ranking(master_df, disponibles, categoria, tipo_renta)
    if todos.empty:
        return todos
    return (todos[todos["_fuera_plazo"]]
            .sort_values("_r_hoy_total", ascending=False).reset_index(drop=True))


# ── Formato ───────────────────────────────────────────────────────────────────

def fmt_pct(v) -> str:
    return "—" if v is None else f"{v * 100:.2f}%"

def fmt_precio(v, divisa) -> str:
    return f"{v:,.2f} {'€' if divisa == 'EUR' else '$'}"

def tip_dividendo_label(raw: str) -> str:
    r = raw.lower()
    if "mensual" in r and "final" in r:    return "Rendimientos mensuales + final"
    if "trimestral" in r and "final" in r: return "Rendimientos trimestrales + final"
    if "final" in r:                       return "Rendimientos a final del proyecto"
    if "mensual" in r:                     return "Rendimientos mensuales"
    return raw.capitalize()


# ── Generación del PDF ────────────────────────────────────────────────────────

def generar_pdf(df: pd.DataFrame, categoria: str,
                mercado: dict | None = None,
                mercado_global: dict | None = None) -> bytes:
    """`mercado` y `mercado_global` se reciben como parámetros en vez de leerse
    aquí: así el informe se puede generar (y probar) sin depender del estado de
    la página, y la hoja 2 simplemente no se añade si no se pasan."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=1.5*cm, rightMargin=1.5*cm,
                            topMargin=1.5*cm, bottomMargin=1.5*cm)

    cell_s    = ParagraphStyle("c",   fontSize=7.5, leading=10, alignment=TA_CENTER, fontName="Helvetica",      textColor=NAVY_OSC)
    cell_b    = ParagraphStyle("cb",  fontSize=7.5, leading=10, alignment=TA_CENTER, fontName="Helvetica-Bold",  textColor=NAVY_OSC)
    cell_lbl  = ParagraphStyle("cl",  fontSize=7.5, leading=10, alignment=TA_LEFT,   fontName="Helvetica-Bold",  textColor=BLANCO)   # primera columna: texto blanco
    head_val  = ParagraphStyle("hv",  fontSize=8,   leading=10, alignment=TA_CENTER, fontName="Helvetica-Bold",  textColor=NAVY_OSC) # cabecera rankings: texto oscuro sobre dorado
    nota_s    = ParagraphStyle("n",   fontSize=7,   leading=9.5, alignment=TA_LEFT,  fontName="Helvetica",       textColor=NAVY_OSC)
    tit_s     = ParagraphStyle("t",   fontSize=18,  leading=22, alignment=TA_LEFT,   fontName="Helvetica-Bold",  textColor=NAVY_OSC)
    fecha_s   = ParagraphStyle("f",   fontSize=9,   leading=12, alignment=TA_RIGHT,  fontName="Helvetica",       textColor=NAVY_OSC)
    sub_s     = ParagraphStyle("s",   fontSize=12,  leading=16, alignment=TA_CENTER, fontName="Helvetica-Bold",  textColor=BLANCO)

    story = []

    # Cabecera
    ht = Table([[
        Paragraph(f"<font color='#F5A623'><b>Reental</b></font> Wealth · Reporte Oportunidades P2P", tit_s),
        Paragraph(f"Fecha: {date.today().strftime('%d/%m/%Y')}", fecha_s),
    ]], colWidths=["70%", "30%"])
    ht.setStyle(TableStyle([("VALIGN", (0,0),(-1,-1),"MIDDLE")]))
    story += [ht, Spacer(1, 0.3*cm), HRFlowable(width="100%", thickness=3, color=DORADO), Spacer(1, 0.4*cm)]

    # Subtítulo — fondo navy oscuro, texto blanco
    st_t = Table([[Paragraph(f"Top {len(df)} mejores oportunidades P2P · Categoría {categoria}", sub_s)]],
                 colWidths=["100%"])
    st_t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(-1,-1), NAVY_OSC),
        ("TOPPADDING",    (0,0),(-1,-1), 8),
        ("BOTTOMPADDING", (0,0),(-1,-1), 8),
    ]))
    story += [st_t, Spacer(1, 0.5*cm)]

    # Tabla de datos
    n   = len(df)
    pw  = landscape(A4)[0] - 3*cm
    lw  = 5.5*cm
    dw  = (pw - lw) / n
    rnk = ["1º","2º","3º","4º","5º","6º","7º","8º","9º","10º"]

    field_rows = [
        ("Score",                         [str(int(r["_score"]))                                      for _,r in df.iterrows()]),
        ("ID Token",                      [r["_id"]                                                    for _,r in df.iterrows()]),
        ("Nombre Inmueble",               [r["_nombre"]                                                for _,r in df.iterrows()]),
        ("Precio/Token P2P",              [fmt_precio(r["_precio_p2p"], r["_divisa"])                  for _,r in df.iterrows()]),
        ("Divisa",                        ["€" if r["_divisa"]=="EUR" else "$"                         for _,r in df.iterrows()]),
        ("Est. Meses hasta fin",          [f"{r['_meses']:.1f}"                                        for _,r in df.iterrows()]),
        ("TIR anual (compuesta) *",       [fmt_pct(r["_r_hoy_ann"])                                    for _,r in df.iterrows()]),
        ("Rent. anualizada simple *",      [fmt_pct(r["_r_hoy_ann_simple"])                             for _,r in df.iterrows()]),
        ("Rent. total pendiente est. *",  [fmt_pct(r["_r_hoy_total"])                                  for _,r in df.iterrows()]),
        ("Rent. alquiler pendiente *",    [fmt_pct(r["_r_alquiler_pend"])                              for _,r in df.iterrows()]),
        ("Rent. alquiler anualiz. real *",[fmt_pct(r["_r_rec_ann"])                                    for _,r in df.iterrows()]),
        ("Rent. al final est. **",        [fmt_pct(r["_r_plusv"])                                      for _,r in df.iterrows()]),
        ("Tipología de Dividendos",       [tip_dividendo_label(r["_tip_dividendo"])                    for _,r in df.iterrows()]),
        ("Nº tokens disponibles",         [f"{int(r['_tokens_disp']):,}"                               for _,r in df.iterrows()]),
        ("Fuente",                        [r["_fuente"]                                                 for _,r in df.iterrows()]),
        ("¿Es Colateralizable?",          ["Colateralizable" if r["_colateralizable"] else "No"        for _,r in df.iterrows()]),
    ]

    # Fila de cabecera (rankings): fondo dorado, texto navy oscuro
    table_data = [[Paragraph("", cell_lbl)] + [Paragraph(rnk[i], head_val) for i in range(n)]]
    for label, values in field_rows:
        # Primera columna (etiqueta): fondo navy, texto blanco
        # Resto de columnas: texto oscuro sobre fondo claro/blanco alterno
        table_data.append([Paragraph(label, cell_lbl)] + [Paragraph(v, cell_s) for v in values])

    col_widths = [lw] + [dw]*n
    tabla = Table(table_data, colWidths=col_widths)
    ts = [
        ("GRID",          (0,0),(-1,-1), 0.4, colors.HexColor("#CBD5E1")),
        # Cabecera de rankings: dorado
        ("BACKGROUND",    (0,0),(-1,0),  DORADO),
        # Primera columna entera: navy oscuro con texto blanco
        ("BACKGROUND",    (0,0),(0,-1),  NAVY_OSC),
        ("TOPPADDING",    (0,0),(-1,-1), 5),
        ("BOTTOMPADDING", (0,0),(-1,-1), 5),
        ("LEFTPADDING",   (0,0),(-1,-1), 5),
        ("RIGHTPADDING",  (0,0),(-1,-1), 5),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
    ]
    # Filas alternas en columnas de datos (no afecta la primera columna)
    for i in range(1, len(table_data)):
        bg = GRIS_CLAR if i % 2 == 0 else BLANCO
        ts.append(("BACKGROUND", (1,i),(-1,i), bg))
    tabla.setStyle(TableStyle(ts))
    story += [tabla, Spacer(1, 0.5*cm)]

    notas = [
        "* Rentabilidades calculadas sobre precio P2P real. Se proyecta la tasa recurrente real acumulada hasta el vencimiento estimado.",
        "* <b>TIR anual (compuesta)</b>: (1 + pendiente) ^ (12 / meses) − 1. Es el rendimiento financiero real, "
        "comparable con cualquier otro producto, y penaliza el plazo. El ranking se ordena por esta cifra.",
        "* <b>Rentabilidad anualizada simple</b>: pendiente × 12 / meses. Reparte el total linealmente sin componer. "
        "Es la convención de la ficha comercial del proyecto. Resulta superior a la TIR en plazos de más de doce "
        "meses e inferior en plazos más cortos, donde componer amplifica en vez de diluir.",
        "** La rent. al final es la ganancia patrimonial esperada en el cierre del proyecto.",
        f"— Categoría aplicada: {categoria}. Las rentabilidades varían según la categoría del inversor.",
        f"— Solo se incluyen proyectos con más de {MIN_TOKENS} tokens disponibles en el OTC interno de Reental.",
        "— Score: A menor puntuación, mejor oportunidad (ordenado por rentabilidad total anualizada pendiente).",
        "— Este ranking no debe ser tomado como consejo de inversión. Todas las rentabilidades son meras estimaciones.",
    ]
    for nota in notas:
        story.append(Paragraph(nota, nota_s))

    # ── Hoja 2 — Liquidez del mercado secundario ──────────────────────────────
    # Responde a la pregunta que el inversor sí se hace ante un activo
    # inmobiliario tokenizado: «¿y si necesito salirme?». Se informa de la
    # liquidez OBSERVADA, sin convertirla en promesa: nada de proyectar, nada
    # de presentar la diferencia con el precio de emisión como rentabilidad.
    if mercado:
        story.append(PageBreak())
        story += [ht, Spacer(1, 0.3*cm),
                  HRFlowable(width="100%", thickness=3, color=DORADO), Spacer(1, 0.4*cm)]

        st2 = Table([[Paragraph("Liquidez del mercado secundario · últimos 12 meses", sub_s)]],
                    colWidths=["100%"])
        st2.setStyle(TableStyle([
            ("BACKGROUND",    (0,0),(-1,-1), NAVY_OSC),
            ("TOPPADDING",    (0,0),(-1,-1), 8),
            ("BOTTOMPADDING", (0,0),(-1,-1), 8),
        ]))
        story += [st2, Spacer(1, 0.4*cm)]

        intro = ("Los tokens de Reental se pueden vender antes del cierre del proyecto por dos vías: "
                 "el <b>mercado OTC</b>, intermediado por Reental, y <b>RNTP2P</b>, donde los inversores "
                 "negocian directamente entre ellos. Toda operación queda registrada en la cadena de "
                 "bloques y es verificable. A continuación, la actividad real observada en cada uno de "
                 "los proyectos de este informe.")
        story += [Paragraph(intro, nota_s), Spacer(1, 0.4*cm)]

        if mercado_global:
            gk = mercado_global
            resumen = [[
                Paragraph("Operaciones", cell_lbl), Paragraph(f"{gk['ops']:,}", cell_b),
                Paragraph("Volumen", cell_lbl), Paragraph(f"${gk['volumen']:,.0f}", cell_b),
                Paragraph("Vendedores distintos", cell_lbl), Paragraph(f"{gk['vendedores']:,}", cell_b),
                Paragraph("Compradores distintos", cell_lbl), Paragraph(f"{gk['compradores']:,}", cell_b),
            ]]
            tr = Table(resumen, colWidths=["12.5%"]*8)
            tr.setStyle(TableStyle([
                ("GRID",       (0,0),(-1,-1), 0.4, colors.HexColor("#CBD5E1")),
                ("BACKGROUND", (0,0),(0,-1),  NAVY_MED), ("BACKGROUND", (2,0),(2,-1), NAVY_MED),
                ("BACKGROUND", (4,0),(4,-1),  NAVY_MED), ("BACKGROUND", (6,0),(6,-1), NAVY_MED),
                ("TOPPADDING", (0,0),(-1,-1), 6), ("BOTTOMPADDING", (0,0),(-1,-1), 6),
                ("VALIGN",     (0,0),(-1,-1), "MIDDLE"),
            ]))
            story += [Paragraph("<b>Conjunto del mercado secundario</b>", nota_s), Spacer(1, 0.15*cm),
                      tr, Spacer(1, 0.5*cm)]

        cab = ["Inmueble", "Operaciones", "Tokens", "Compradores\ndistintos",
               "Precio medio\ncruzado", "Precio de\nemisión"]
        filas = [[Paragraph(c.replace("\n", "<br/>"), head_val) for c in cab]]
        for _, r in df.iterrows():
            m = mercado.get((r.get("_token_address") or "").lower())
            if m and m["ops"]:
                # Un proyecto con dos operaciones dice "2": esconder los que
                # tienen poca profundidad convertiría el informe en argumentario
                # y dejaría al inversor sin saber a qué se expone.
                vals = [f"{m['ops']:,}", f"{m['tokens']:,.1f}", f"{m['compradores']:,}",
                        fmt_precio(m["precio_medio"], "USD") if m["precio_medio"] else "—"]
            else:
                vals = ["Sin operaciones", "—", "—", "—"]
            filas.append([Paragraph(f"{r['_nombre']} ({r['_id']})", cell_lbl)]
                         + [Paragraph(v, cell_s) for v in vals]
                         + [Paragraph(fmt_precio(r["_precio_p2p"], r["_divisa"]), cell_s)])

        pw2 = landscape(A4)[0] - 3*cm
        t2 = Table(filas, colWidths=[pw2*0.30] + [pw2*0.14]*5)
        ts2 = [
            ("GRID",          (0,0),(-1,-1), 0.4, colors.HexColor("#CBD5E1")),
            ("BACKGROUND",    (0,0),(-1,0),  DORADO),
            ("BACKGROUND",    (0,1),(0,-1),  NAVY_OSC),
            ("TOPPADDING",    (0,0),(-1,-1), 5), ("BOTTOMPADDING", (0,0),(-1,-1), 5),
            ("LEFTPADDING",   (0,0),(-1,-1), 5), ("RIGHTPADDING",  (0,0),(-1,-1), 5),
            ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ]
        for i in range(1, len(filas)):
            ts2.append(("BACKGROUND", (1,i),(-1,i), GRIS_CLAR if i % 2 == 0 else BLANCO))
        t2.setStyle(TableStyle(ts2))
        story += [t2, Spacer(1, 0.45*cm)]

        notas2 = [
            "<b>Cómo leer esta hoja.</b> Las cifras son la actividad registrada en los últimos 12 meses "
            "en el mercado secundario, contando las dos vías (OTC y RNTP2P).",
            "— <b>Operaciones</b> y <b>compradores distintos</b> indican con qué frecuencia ha cambiado de "
            "manos cada inmueble y cuántas contrapartes diferentes han intervenido.",
            "— <b>Precio medio cruzado</b>: precio por token al que se han cerrado esas operaciones, "
            "ponderado por importe. Se muestra junto al precio de emisión únicamente como referencia "
            "de contexto.",
            "— Los importes se expresan en USD. Las operaciones en proyectos denominados en euros se "
            "liquidan igualmente en stablecoin (USDT/USDC).",
            f"— Datos a {date.today().strftime('%d/%m/%Y')}. <b>La actividad pasada no garantiza que en el "
            "futuro exista contrapartida ni a qué precio.</b> La liquidez de estos activos es limitada y "
            "puede variar: un inmueble con operaciones recientes puede no tener comprador cuando usted "
            "desee vender.",
            "— Este documento es informativo y no constituye asesoramiento ni recomendación de inversión.",
        ]
        for nota in notas2:
            story.append(Paragraph(nota, nota_s))

    doc.build(story)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
# INTERFAZ
# ══════════════════════════════════════════════════════════════════════════════

st.title("📊 Análisis de Oportunidades P2P")
st.caption(
    "Ranking en tiempo real basado en los tokens disponibles del OTC interno de Reental "
    "(wallet custodia + ofertas de inversores) ordenados por rentabilidad anualizada pendiente."
)

ui_kpi.inyectar_css()   # estilos de las tarjetas KPI (una vez por página)

# Cargar datos
with st.spinner("Cargando catálogo de proyectos…"):
    master_df = load_master_projects()

if master_df.empty:
    st.error("No se ha podido cargar el catálogo de proyectos.")
    st.stop()

with st.spinner("Consultando disponibilidad en OTC…"):
    disponibles = construir_disponibilidad(master_df)

if not disponibles:
    st.warning("No hay tokens disponibles en el OTC en este momento.")
    st.stop()

with st.expander("🔍 Diagnóstico: tokens detectados en el OTC", expanded=False):
    for d in sorted(disponibles, key=lambda x: x["project_id"]):
        st.write(f"**{d['project_id']}** · {d['tokens_disponibles']:.0f} tokens · "
                 f"{d['precio_p2p']:.2f} · fuente: {d['fuente']}")

# ── Filtros ───────────────────────────────────────────────────────────────────
st.subheader("⚙️ Configuración del informe")
f1, f2, f3 = st.columns([2, 2, 1])

categoria = f1.selectbox(
    "Categoría del inversor",
    list(CATEGORIAS.keys()),
    index=0,
    help="SR = SuperReentel (máxima rentabilidad) · RP = ReentelPro · Reentel = categoría base.",
)
tipo_renta_sel = f2.selectbox(
    "Tipo de renta",
    list(TIPO_RENTA_LABELS.keys()),
    format_func=lambda x: TIPO_RENTA_LABELS[x],
)
top_n = int(f3.number_input("Top N", min_value=1, max_value=10, value=5, step=1))

st.markdown("---")

# ── Calcular ranking ──────────────────────────────────────────────────────────
df_ops = calcular_ranking(master_df, disponibles, categoria, tipo_renta_sel, top_n)

if df_ops.empty:
    st.warning(f"No hay oportunidades con los filtros seleccionados (mínimo {MIN_TOKENS} tokens disponibles).")
    st.stop()

# ── Tabla ─────────────────────────────────────────────────────────────────────
st.subheader(f"🏆 Top {len(df_ops)} oportunidades · {categoria}")

rnk_labels = ["1º","2º","3º","4º","5º","6º","7º","8º","9º","10º"]
display_rows = []
for _, r in df_ops.iterrows():
    display_rows.append({
        "Ranking":                        rnk_labels[int(r["_score"]) - 1],
        "Score":                          int(r["_score"]),
        "ID":                             r["_id"],
        "Nombre":                         r["_nombre"],
        "Precio P2P":                     fmt_precio(r["_precio_p2p"], r["_divisa"]),
        "Divisa":                         "€" if r["_divisa"]=="EUR" else "$",
        "Meses hasta fin":                f"{r['_meses']:.1f}",
        "TIR anual (compuesta)":          fmt_pct(r["_r_hoy_ann"]),
        "Anualizada simple":              fmt_pct(r["_r_hoy_ann_simple"]),
        "Rent. total pendiente":          fmt_pct(r["_r_hoy_total"]),
        "Rent. alquiler pendiente":       fmt_pct(r["_r_alquiler_pend"]),
        "Rent. alquiler anualizada real": fmt_pct(r["_r_rec_ann"]),
        "Rent. al final":                 fmt_pct(r["_r_plusv"]),
        "Tipo dividendo":                 tip_dividendo_label(r["_tip_dividendo"]),
        "Tokens disponibles":             f"{int(r['_tokens_disp']):,}",
        "Fuente":                         r["_fuente"],
        "Colateralizable":                "✅" if r["_colateralizable"] else "—",
    })

st.dataframe(pd.DataFrame(display_rows), hide_index=True, use_container_width=True)

st.caption(f"\\* Rentabilidades calculadas sobre precio P2P, proyectando la tasa real acumulada hasta vencimiento · Categoría: {categoria}")
with st.expander("📐 TIR compuesta vs anualizada simple — cuál usar y por qué se muestran las dos"):
    st.markdown("""
Las dos anualizan la misma rentabilidad pendiente, pero con convenciones distintas:

**⚡ TIR anual (compuesta)** — `(1 + pendiente)^(12 / meses) − 1`

Es el rendimiento financiero **real**: lo que tendría que rendir el dinero cada año, reinvirtiendo,
para llegar a ese resultado. **Penaliza el plazo**, así que es lo único comparable entre proyectos
de duración distinta — y por eso **el ranking ordena por esta**. Es la cifra que un inversor puede
contrastar con cualquier otro producto financiero.

**📊 Anualizada simple** — `pendiente × 12 / meses`

Reparte el total linealmente entre los meses, sin componer. Es la convención que usa el maestro en su
columna *«Estimación Rentab. Total anualizado»* y, con ella, el material comercial.

**Cuál sale mayor depende del plazo**, no es siempre la misma:

| Plazo | Efecto de componer | Resultado |
|---|---|---|
| Menos de 12 meses | amplifica | **TIR > simple** |
| Exactamente 12 meses | neutro | iguales |
| Más de 12 meses | diluye | **TIR < simple** |

**Por qué conviven aquí.** Un mismo proyecto puede aparecer con 15,32 % en el ranking y 17,00 % en
su ficha sin que ninguna cifra esté mal: son dos formas de anualizar. Mostrar solo una hacía que el
comercial no supiera cuál llevar delante del inversor. Con las dos y su etiqueta, la elección es
consciente.

> **NCA-1** (34,7 meses, 51 % pendiente) → TIR **15,32 %** · simple **17,64 %**: la simple gana
> porque el plazo pasa de un año.
> **DXB-1** (5,8 meses, 24 % pendiente) → TIR **56,66 %** · simple **50,09 %**: aquí gana la TIR,
> porque componer por debajo del año amplifica en vez de diluir.
    """)
st.caption(f"\\*\\* La rent. al final es la ganancia patrimonial esperada al cierre · Mínimo {MIN_TOKENS} tokens disponibles para entrar en el análisis")

# ── Aviso de hipótesis de acumulación ────────────────────────────────────────
# Si el precio de un proyecto se fijó asumiendo que sigue revalorizándose en la
# prórroga, el retorno mostrado depende de esa hipótesis. Se dice, con la cifra
# alternativa al lado, para que nadie la tome por un hecho.
_con_hip = df_ops[df_ops["_acumulado"] > 0] if "_acumulado" in df_ops.columns else pd.DataFrame()
if not _con_hip.empty:
    _lineas = []
    for _, _r in _con_hip.iterrows():
        _lineas.append(
            f"- **{_r['_nombre']} ({_r['_id']})** — {fmt_pct(_r['_r_hoy_total'])} pendiente "
            f"asumiendo que sigue acumulando **{_r['_tasa_acum']*100:.2f}% mensual** durante la "
            f"prórroga ({_r['_meses_retraso']:.1f} meses de retraso). "
            f"Sin esa hipótesis sería **{fmt_pct(_r['_r_hoy_total_est'])}**."
        )
    with st.expander(f"ℹ️ {len(_con_hip)} proyecto(s) con rentabilidad sujeta a hipótesis de "
                     "acumulación — ver detalle", expanded=True):
        for _l in _lineas:
            st.markdown(_l)
        st.caption(
            "Su precio se fijó dando por hecho que el activo sigue revalorizándose pese a haber "
            "sobrepasado la fecha de fin. Es una hipótesis, no un dato observado: si no se cumple, "
            "el retorno es el segundo número. La tasa se define por proyecto en la columna **DC** "
            "del maestro."
        )

# ── Fuera de plazo ────────────────────────────────────────────────────────────
# Proyectos con saldo cuya fecha estimada de fin ya pasó. No tienen anualizada
# —depende de cuándo cierren, y una fecha inventada inventaría la TIR— así que
# van aparte en vez de desaparecer del análisis sin dejar rastro.
_fuera = calcular_fuera_de_plazo(master_df, disponibles, categoria, tipo_renta_sel)
if not _fuera.empty:
    st.markdown("---")
    st.markdown(
        '<div style="display:flex;align-items:center;gap:10px;margin-bottom:2px;">'
        '<span style="background:#dc2626;color:white;border-radius:5px;padding:3px 10px;'
        'font-size:0.75rem;font-weight:700;letter-spacing:.04em;">⏰ FUERA DE PLAZO</span>'
        f'<span style="font-weight:700;font-size:1.05rem;">{len(_fuera)} proyecto'
        f'{"s" if len(_fuera) > 1 else ""} con saldo disponible</span></div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Su fecha estimada de fin ya pasó y no hay una nueva confirmada, así que **no se puede "
        "calcular la rentabilidad anualizada**: depende enteramente de cuándo cierren. Lo que sí "
        "es firme es la renta que siguen pagando."
    )

    _fp_rows = []
    for _, r in _fuera.iterrows():
        _renta = r["_r_rec_ann"] or 0
        _fp_rows.append({
            "Token":            r["_id"],
            "Inmueble":         r["_nombre"],
            "Precio P2P":       fmt_precio(r["_precio_p2p"], r["_divisa"]),
            "Renta anual en curso": fmt_pct(_renta) if _renta > 0 else "no renta",
            "Plusvalía pendiente": fmt_pct(r["_r_plusv"]),
            "Total pendiente":  fmt_pct(r["_r_hoy_total"]),
            "Tokens disp.":     f"{int(r['_tokens_disp']):,}",
        })
    st.dataframe(pd.DataFrame(_fp_rows), hide_index=True, use_container_width=True)

    # Curva de sensibilidad: no hay UNA rentabilidad, hay una en función de
    # cuándo cierre. Enseñarla completa es más honesto que elegir una fecha.
    _sel_fp = st.selectbox(
        "Ver rentabilidad según cuándo cierre el proyecto",
        [f"{r['_nombre']} ({r['_id']})" for _, r in _fuera.iterrows()],
        key="fp_sel",
    )
    _r = _fuera.iloc[[f"{x['_nombre']} ({x['_id']})" for _, x in _fuera.iterrows()].index(_sel_fp)]
    _g = _r["_r_plusv"] or 0
    _rn = _r["_r_rec_ann"] or 0
    _meses_curva = [3, 6, 9, 12, 18, 24, 36]
    _curva = [((1 + _g + _rn * m / 12) ** (12 / m) - 1) for m in _meses_curva]
    fig_fp = go.Figure(go.Scatter(
        x=_meses_curva, y=[v * 100 for v in _curva], mode="lines+markers",
        line=dict(color="#dc2626", width=2), marker=dict(size=7),
        hovertemplate="Si cierra en %{x} meses<br><b>%{y:.1f}%</b> anualizado<extra></extra>",
    ))
    if _rn > 0:
        # La renta es el suelo: se cobra pase lo que pase con la fecha de cierre.
        fig_fp.add_hline(y=_rn * 100, line=dict(color="#16a34a", width=1.5, dash="dash"),
                         annotation_text=f"Renta en curso {_rn*100:.2f}% (suelo cierto)",
                         annotation_position="bottom right",
                         annotation_font=dict(size=10, color="#16a34a"))
    fig_fp.update_layout(
        height=290, margin=dict(t=30, b=10, l=8, r=8),
        xaxis=dict(title="Meses hasta el cierre", gridcolor="#e2e8f0",
                   tickmode="array", tickvals=_meses_curva),
        yaxis=dict(title="Rentabilidad anualizada (%)", gridcolor="#e2e8f0"),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", showlegend=False,
    )
    st.plotly_chart(fig_fp, use_container_width=True)
    st.caption(
        f"**{_r['_nombre']}**: plusvalía pendiente {fmt_pct(_g)}"
        + (f" más una renta real de {fmt_pct(_rn)} anual que sigue cobrando mientras espera. "
           "Esa renta es el suelo: se percibe con independencia de cuándo cierre. "
           if _rn > 0 else " y sin renta en curso, así que todo depende de la fecha de cierre. ")
        + "Para fijar una fecha y que vuelva al ranking, el equipo de Real Estate debe rellenar "
          "la columna **CA** del maestro con una reestimación."
    )

# Índice de proyectos por dirección de token, para resolver nombre, ubicación y
# precio de emisión en la sección de mercado secundario.
project_by_addr_global = {}
for _, _row in master_df.iterrows():
    if _row.get("token_address"):
        project_by_addr_global[str(_row["token_address"]).lower()] = _row.to_dict()

# ══════════════════════════════════════════════════════════════════════════════
# PROFUNDIDAD DEL MERCADO SECUNDARIO — OTC + RNTP2P
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("---")
st.subheader("📈 Profundidad del mercado secundario")
st.caption(
    "Operaciones **cerradas** en los dos canales por los que un inversor puede vender: "
    "el OTC que intermedia Reental y RNTP2P, donde los inversores negocian entre ellos. "
    + _mkt.ADVERTENCIA_COBERTURA
)

_ops_todas = _mkt.operaciones(load_reservas_otc(), project_by_addr_global)

if _ops_todas.empty:
    st.info("Todavía no hay operaciones registradas en ninguno de los dos canales.")
else:
    _cob = _ops_todas["detalle_ok"].mean() * 100
    _fmin, _fmax = _ops_todas["fecha"].min().date(), _ops_todas["fecha"].max().date()

    mf1, mf2, mf3 = st.columns([2, 1, 1])
    # El desplegable se construye con los proyectos que REALMENTE tienen
    # operaciones: ofrecer los 125 del maestro obligaría a probar uno a uno para
    # descubrir cuáles tienen datos.
    _con_proy = _ops_todas.dropna(subset=["proyecto"])
    _proyectos = sorted(_con_proy["proyecto"].unique())
    _opciones = {"— Todos los proyectos —": None}
    for _p in _proyectos:
        _addr = _con_proy[_con_proy["proyecto"] == _p]["token_address"].dropna()
        _opciones[_p] = _addr.iloc[0] if len(_addr) else None
    _sel_proy = mf1.selectbox("Proyecto", list(_opciones.keys()), key="mkt_proy")
    _desde = mf2.date_input("Desde", value=_fmin, min_value=_fmin, max_value=_fmax, key="mkt_desde")
    _hasta = mf3.date_input("Hasta", value=_fmax, min_value=_fmin, max_value=_fmax, key="mkt_hasta")

    _addr_sel = _opciones.get(_sel_proy)
    _ops = _mkt.filtrar(_ops_todas, _desde, _hasta, _addr_sel)

    # Precio de emisión del proyecto elegido, para poder decir si el secundario
    # cotiza con prima o con descuento.
    _pe = None
    if _addr_sel:
        _pe = (project_by_addr_global.get(_addr_sel) or {}).get("precio_emision")
        try:
            _pe = float(_pe) if _pe else None
        except (TypeError, ValueError):
            _pe = None

    if _ops.empty:
        st.warning("No hay operaciones con esos filtros. Prueba a ampliar el período.")
    else:
        _k_tot = _mkt.kpis(_ops, _pe)
        _k_p2p = _mkt.kpis(_ops[_ops["canal"] == _mkt.CANAL_P2P], _pe)
        _k_otc = _mkt.kpis(_ops[_ops["canal"] == _mkt.CANAL_OTC], _pe)

        _eur = lambda v: f"${v:,.0f}" if v is not None else "—"
        _pu  = lambda v: f"${v:,.2f}" if v is not None else "—"

        st.markdown("<div style='font-size:0.78rem;color:#64748b;font-weight:600;"
                    "margin:6px 0;'>🌐 Conjunto del mercado secundario</div>",
                    unsafe_allow_html=True)
        t1, t2, t3, t4 = st.columns(4)
        t1.markdown(kpi_card("💰", "Volumen", _eur(_k_tot["volumen"]),
                             sublabel=f"{_k_tot['ops']:,} operaciones",
                             help="Suma de los importes cerrados en ambos canales en el período."),
                    unsafe_allow_html=True)
        t2.markdown(kpi_card("🎟️", "Ticket medio", _eur(_k_tot["ticket_medio"]),
                             sublabel="por operación",
                             help="Volumen dividido entre el número de operaciones."),
                    unsafe_allow_html=True)
        t3.markdown(kpi_card("🏷️", "Precio medio", _pu(_k_tot["precio_medio"]),
                             sublabel="por token, ponderado",
                             help="Ponderado por IMPORTE, no por operación: una venta de 100 "
                                  "tokens y otra de 0,3 no deben pesar igual."),
                    unsafe_allow_html=True)
        _prima = _k_tot["prima_pct"]
        t4.markdown(kpi_card("📊", "Prima sobre emisión",
                             f"{_prima:+.1f} %" if _prima is not None else "—",
                             value_color="#16a34a" if (_prima or 0) >= 0 else "#dc2626",
                             sublabel="vs precio de emisión" if _prima is not None
                                      else "elige un proyecto",
                             help="Cuánto por encima o por debajo del precio de emisión se está "
                                  "pagando en el secundario. Solo se calcula al elegir un proyecto, "
                                  "porque cada uno tiene su propio precio de emisión."),
                    unsafe_allow_html=True)

        st.markdown("<div style='font-size:0.78rem;color:#64748b;font-weight:600;"
                    "margin:12px 0 6px;'>⚖️ Por canal</div>", unsafe_allow_html=True)
        c1, c2, c3, c4 = st.columns(4)
        _cuota = (_k_p2p["volumen"] / _k_tot["volumen"] * 100) if _k_tot["volumen"] else 0
        c1.markdown(kpi_card("🤝", "RNTP2P", _eur(_k_p2p["volumen"]),
                             sublabel=f"{_k_p2p['ops']:,} ops · {_cuota:.0f}% del volumen",
                             help="Operaciones directas entre inversores en p2p.rnt.finance."),
                    unsafe_allow_html=True)
        c2.markdown(kpi_card("🏢", "OTC Reental", _eur(_k_otc["volumen"]),
                             sublabel=f"{_k_otc['ops']:,} ops · {100-_cuota:.0f}% del volumen",
                             help="Reservas OTC completadas. Las activas no cuentan: son "
                                  "compromiso, no operación cerrada."),
                    unsafe_allow_html=True)
        c3.markdown(kpi_card("🏷️", "Precio P2P", _pu(_k_p2p["precio_medio"]),
                             sublabel="por token", help="Precio medio ponderado en RNTP2P."),
                    unsafe_allow_html=True)
        _dif = (_k_otc["precio_medio"] - _k_p2p["precio_medio"]
                if _k_otc["precio_medio"] and _k_p2p["precio_medio"] else None)
        c4.markdown(kpi_card("↔️", "Precio OTC", _pu(_k_otc["precio_medio"]),
                             sublabel=(f"{_dif:+,.2f} $ vs P2P" if _dif is not None else "por token"),
                             help="Precio medio ponderado en OTC. El diferencial con P2P es la "
                                  "señal de si el precio OTC está bien calibrado: si es muy "
                                  "superior, el inversor tiene incentivo para irse a P2P."),
                    unsafe_allow_html=True)

        st.markdown("<div style='font-size:0.78rem;color:#64748b;font-weight:600;"
                    "margin:12px 0 6px;'>👥 Amplitud</div>", unsafe_allow_html=True)
        a1, a2, a3 = st.columns(3)
        a1.markdown(kpi_card("🧍", "Vendedores únicos", f"{_k_tot['vendedores']:,}",
                             sublabel="wallets distintas",
                             help="Cuántos inversores distintos han vendido. Mide si el mercado "
                                  "está repartido o concentrado en unos pocos."),
                    unsafe_allow_html=True)
        a2.markdown(kpi_card("🛒", "Compradores únicos", f"{_k_tot['compradores']:,}",
                             sublabel="wallets distintas",
                             help="Solo se conoce en RNTP2P: en OTC el comprador se registra "
                                  "por nombre, no por wallet."),
                    unsafe_allow_html=True)
        a3.markdown(kpi_card("🪙", "Tokens transaccionados", f"{_k_tot['tokens']:,.2f}",
                             sublabel="con detalle disponible",
                             help="Solo cuenta las operaciones cuya cantidad se conoce."),
                    unsafe_allow_html=True)

        _serie = _mkt.serie_mensual(_ops)
        if len(_serie) > 1:
            fig_m = go.Figure()
            for canal, color in ((_mkt.CANAL_P2P, "#3b82f6"), (_mkt.CANAL_OTC, "#f5a623")):
                s = _serie[_serie["canal"] == canal]
                if s.empty:
                    continue
                fig_m.add_trace(go.Bar(x=s["mes"], y=s["volumen"], name=canal,
                                       marker_color=color,
                                       hovertemplate="<b>%{x|%b %Y}</b><br>"
                                                     f"{canal}: $%{{y:,.0f}}<extra></extra>"))
            # Acumulado de AMBOS canales sobre el eje derecho: las barras dicen
            # cómo va cada mes y la línea cuánto mercado se lleva construido.
            _acum = (_serie.groupby("mes")["volumen"].sum().sort_index().cumsum().reset_index())
            fig_m.add_trace(go.Scatter(
                x=_acum["mes"], y=_acum["volumen"], name="Acumulado",
                yaxis="y2", mode="lines+markers",
                line=dict(color="#0f172a", width=2), marker=dict(size=4),
                hovertemplate="<b>%{x|%b %Y}</b><br>Acumulado: $%{y:,.0f}<extra></extra>",
            ))
            fig_m.update_layout(
                barmode="stack", height=340,
                margin=dict(t=30, b=10, l=8, r=8),
                xaxis=dict(title=None),
                yaxis=dict(title="Volumen del mes (USD)", gridcolor="#e2e8f0"),
                # El acumulado va en su propio eje: en la misma escala que las
                # barras las aplastaría hasta hacerlas ilegibles.
                yaxis2=dict(title="Acumulado (USD)", overlaying="y", side="right",
                            showgrid=False, rangemode="tozero"),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
                hovermode="x unified",
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig_m, use_container_width=True)
            st.caption("Las barras son el volumen de cada mes por canal (eje izquierdo); "
                       "la línea, el acumulado de ambos (eje derecho). El acumulado se "
                       "calcula sobre el período y proyecto filtrados, no sobre todo el histórico.")

        if _k_tot["sin_detalle"]:
            st.caption(
                f"⚠️ **{_k_tot['sin_detalle']:,} de {_k_tot['ops']:,}** operaciones del período no "
                "traen cantidad de tokens, así que cuentan para el volumen pero no para el precio "
                "medio ni para el desglose por proyecto. Se recuperan ejecutando "
                "`scripts/enriquecer_p2p.py` (ver `data/rnt_p2p/README.md`)."
            )
        st.caption(f"Cobertura de detalle en todo el histórico: **{_cob:.1f} %** de las operaciones. "
                   f"Datos desde {_fmin:%d/%m/%Y} hasta {_fmax:%d/%m/%Y}.")

# ── Exportar ─────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("📤 Exportar informe")

st.markdown("""
    <style>
    .st-key-btn_crear_wa button {
        background-color: #25D366 !important;
        color: #ffffff !important;
        border: none !important;
        font-weight: 700 !important;
        border-radius: 8px !important;
    }
    .st-key-btn_crear_wa button:hover {
        background-color: #1EBE5D !important;
        color: #ffffff !important;
    }
    .st-key-btn_crear_wa button:active {
        background-color: #128C7E !important;
    }
    </style>
""", unsafe_allow_html=True)

col_pdf, col_wa = st.columns([1, 1])

with col_pdf:
    if st.button("📥 Generar PDF", type="primary", use_container_width=True):
        with st.spinner("Generando PDF…"):
            # La hoja 2 se alimenta del mismo cálculo que la sección web: una
            # sola fuente, para que informe y pantalla nunca discrepen.
            _mkt_pdf = _mkt.resumen_por_token(_ops_todas) if not _ops_todas.empty else {}
            _mkt_glob = _mkt.kpis(_mkt.filtrar(
                _ops_todas, pd.Timestamp.today() - pd.Timedelta(days=365), None
            )) if not _ops_todas.empty else None
            pdf_bytes = generar_pdf(df_ops, categoria, _mkt_pdf, _mkt_glob)
        st.download_button(
            label="⬇️ Descargar PDF",
            data=pdf_bytes,
            file_name=f"Reental_Oportunidades_P2P_{date.today().strftime('%Y%m%d')}.pdf",
            mime="application/pdf",
            type="primary",
        )

with col_wa:
    if st.button("💬 Crear mensaje para enviar por WhatsApp", key="btn_crear_wa", use_container_width=True):
        rnk_emoji = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        lineas = []
        lineas.append(f"🏠 *Reental Wealth — Top {len(df_ops)} Oportunidades P2P*")
        lineas.append(f"📅 {date.today().strftime('%d/%m/%Y')}  |  Categoría: *{categoria}*")
        lineas.append("─" * 30)

        for _, r in df_ops.iterrows():
            idx        = int(r["_score"]) - 1
            emoji      = rnk_emoji[idx] if idx < len(rnk_emoji) else f"{idx+1}."
            divisa_sym = "€" if r["_divisa"] == "EUR" else "$"
            lineas.append(f"\n{emoji} *{r['_nombre']}* ({r['_id']})")
            lineas.append(f"💰 Precio P2P: *{r['_precio_p2p']:,.0f} {divisa_sym}/token*")
            lineas.append(f"⏳ Tiempo hasta fin: *{r['_meses']:.0f} meses*")
            lineas.append(f"📈 TIR anual (compuesta): *{fmt_pct(r['_r_hoy_ann'])}*")
            lineas.append(f"📊 Anualizada simple: *{fmt_pct(r['_r_hoy_ann_simple'])}*")
            lineas.append(f"📊 Rent. total pendiente: *{fmt_pct(r['_r_hoy_total'])}*")
            if r["_r_alquiler_pend"] and r["_r_alquiler_pend"] > 0.001:
                lineas.append(f"   🏘️ Por alquiler: {fmt_pct(r['_r_alquiler_pend'])}")
            if r["_r_plusv"] and r["_r_plusv"] > 0.001:
                lineas.append(f"   🔚 Plusvalía al cierre: {fmt_pct(r['_r_plusv'])}")
            lineas.append(f"🪙 Tokens disponibles: *{int(r['_tokens_disp']):,}*")
            lineas.append(f"💳 {tip_dividendo_label(r['_tip_dividendo'])}")
            if r["_colateralizable"]:
                lineas.append("🔒 Colateralizable")

        lineas.append("\n" + "─" * 30)
        lineas.append("_⚠️ No constituye consejo de inversión. Rentabilidades son estimaciones._")
        lineas.append(f"_Rentabilidades para categoría {categoria}._")

        mensaje_wa = "\n".join(lineas)

        # El navegador solo permite escribir al portapapeles desde un gesto del
        # usuario DENTRO del iframe, así que el botón de copiar vive en el componente.
        # Sin session_state: el botón desaparece con cualquier otra interacción.
        import json as _json
        msg_js = _json.dumps(mensaje_wa)
        st.components.v1.html(f"""
        <button id="copiar-wa" style="
            width:100%; padding:12px 20px; font-size:16px; font-weight:700;
            font-family:'Source Sans Pro',sans-serif; cursor:pointer;
            background:#25D366; color:#fff; border:none; border-radius:8px;">
            📋 Copiar al portapapeles
        </button>
        <div id="copiado-ok" style="display:none; margin-top:8px; text-align:center;
            font-family:'Source Sans Pro',sans-serif; color:#25D366; font-weight:600;">
            ✅ ¡Copiado! Pégalo directamente en WhatsApp.
        </div>
        <script>
        const MSG = {msg_js};
        document.getElementById('copiar-wa').addEventListener('click', function() {{
            function ok() {{
                document.getElementById('copiado-ok').style.display = 'block';
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
