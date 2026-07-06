"""
Análisis de Oportunidades P2P — Reental Wealth
Fuente de datos: wallet OTC de Reental (tokens disponibles - reservas activas)
                + ofertas activas de terceros publicadas en esta herramienta.
"""

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
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from utils import load_master_projects, parse_pct, parse_float_val, strip_accents

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

# ── Google Sheets ─────────────────────────────────────────────────────────────

def _get_gsheet_client():
    creds_json = os.getenv("GSHEET_CREDENTIALS_JSON", "")
    if not creds_json:
        return None
    creds = Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds)

@st.cache_data(show_spinner=False, ttl=CACHE_TTL)
def _read_gsheet_tab_cached(tab: str) -> list:
    try:
        gc = _get_gsheet_client()
        if not gc:
            return []
        ws  = gc.open_by_key(SPREADSHEET_ID).worksheet(tab)
        val = ws.acell("A1").value
        if not val:
            return []
        parsed = json.loads(val)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []

def _read_gsheet_tab_fresh(tab: str) -> list:
    """Sin caché — para datos que cambian frecuentemente (reservas, ofertas)."""
    try:
        gc = _get_gsheet_client()
        if not gc:
            return []
        ws  = gc.open_by_key(SPREADSHEET_ID).worksheet(tab)
        val = ws.acell("A1").value
        if not val:
            return []
        parsed = json.loads(val)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []

def load_reservas_otc() -> list:
    return _read_gsheet_tab_fresh(TAB_RESERVAS)

def load_ofertas_otc() -> list:
    return _read_gsheet_tab_fresh(TAB_OFERTAS)

# ── Saldos wallet OTC desde Etherscan ─────────────────────────────────────────

@st.cache_data(show_spinner=False, ttl=CACHE_TTL)
def fetch_otc_disponibles(wallet: str, api_key: str,
                           project_by_addr_items: tuple,
                           project_by_id_items: tuple) -> dict:
    """
    Devuelve {contract_addr: saldo_bruto} para tokens Reental en la wallet OTC.
    Los aTokens Aave se consolidan sobre su subyacente.
    """
    project_by_addr = dict(project_by_addr_items)
    project_by_id   = dict(project_by_id_items)
    known_addresses = set(project_by_addr.keys())
    nombre_to_addr  = {row["nombre"].lower(): addr for addr, row in project_by_addr.items()}

    params = {
        "chainid": POLYGON_CHAIN, "module": "account", "action": "tokentx",
        "address": wallet, "startblock": 0, "endblock": 99999999,
        "sort": "asc", "apikey": api_key,
    }
    try:
        resp = requests.get(ETHERSCAN_BASE, params=params, timeout=30)
        result = resp.json().get("result")
        txs = result if isinstance(result, list) else []
    except Exception:
        return {}

    raw_balances = {}
    atoken_map   = {}

    for tx in txs:
        contract  = tx["contractAddress"].lower()
        sym       = tx.get("tokenSymbol", "")
        name      = tx.get("tokenName", "")
        dec       = int(tx.get("tokenDecimal") or 18)
        value     = int(tx["value"]) / (10 ** dec)
        to_addr   = tx["to"].lower()
        from_addr = tx["from"].lower()

        is_atoken = sym.startswith("aMatReental-") or name.startswith("Aave Matic Reental-")
        if is_atoken and contract not in atoken_map:
            suffix = (name[len("Aave Matic Reental-"):] if name.startswith("Aave Matic Reental-")
                      else sym[len("aMatReental-"):]).strip().lower()
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

        if to_addr == wallet and from_addr != wallet:
            raw_balances[effective] = raw_balances.get(effective, 0.0) + value
        elif from_addr == wallet and to_addr != wallet:
            raw_balances[effective] = raw_balances.get(effective, 0.0) - value

    return {addr: round(saldo, 6) for addr, saldo in raw_balances.items() if saldo >= 0.001}


def construir_disponibilidad(master_df: pd.DataFrame) -> list:
    """
    Devuelve lista de dicts con los tokens disponibles (OTC Reental + terceros).
    Cada dict: {project_id, nombre, divisa, precio_p2p, tokens_disponibles, fuente}
    """
    # Índices del master
    project_by_addr = {}
    project_by_id   = {}
    for _, row in master_df.iterrows():
        if row.get("token_address"):
            project_by_addr[row["token_address"]] = row.to_dict()
        project_by_id[row["id"].lower()] = row.to_dict()

    # 1. Saldos brutos wallet OTC
    brutos = fetch_otc_disponibles(
        OTC_WALLET, API_KEY,
        tuple(project_by_addr.items()),
        tuple(project_by_id.items()),
    )

    # 2. Restar reservas activas
    reservas = load_reservas_otc()
    reservado = {}
    for r in reservas:
        if r.get("estado") in ("completada", "cancelada"):
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
        n_tokens = float(o.get("n_tokens", 0))
        precio   = float(o.get("precio_acordado") or o.get("precio_venta") or 0)
        addr     = o.get("token_address", "").lower()
        pid      = o.get("proyecto_id", "")
        if n_tokens < 0.001 or precio <= 0 or not pid:
            continue
        disponibles.append({
            "project_id":        pid,
            "token_address":     addr,
            "tokens_disponibles": n_tokens,
            "precio_p2p":        precio,
            "fuente":            "Tercero",
        })

    return disponibles


@st.cache_data(show_spinner=False, ttl=CACHE_TTL)
def _load_precios_otc_cached() -> dict:
    try:
        gc  = _get_gsheet_client()
        if not gc:
            return {}
        ws  = gc.open_by_key(SPREADSHEET_ID).worksheet("precios_otc")
        val = ws.acell("A1").value
        if not val:
            return {}
        parsed = json.loads(val)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


# ── Cálculo del ranking ───────────────────────────────────────────────────────

def calcular_ranking(master_df: pd.DataFrame, disponibles: list,
                     categoria: str, tipo_renta: str, top_n: int) -> pd.DataFrame:
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

        meses = m.get("meses_pendientes") or 0
        if meses <= 0:
            continue

        r_hoy_ann_raw   = m.get(cols["r_hoy_ann"])
        r_hoy_total_raw = m.get(cols["r_hoy_total"])
        r_rec_ann_raw   = m.get(cols["r_rec_ann"])
        r_plusv_raw     = m.get(cols["r_plusv"])

        if r_hoy_ann_raw is None:
            continue

        # Ajuste por precio P2P vs emisión (ambos en divisa nativa del token)
        adj = precio_emision / precio_p2p

        r_hoy_ann        = (r_hoy_ann_raw   or 0) * adj
        r_hoy_total      = (r_hoy_total_raw or 0) * adj
        r_rec_ann        = (r_rec_ann_raw   or 0) * adj
        r_plusv          = (r_plusv_raw     or 0) * adj
        r_alquiler_pend  = r_rec_ann * (meses / 12)

        rows.append({
            "_id":              m["id"],
            "_nombre":          m["nombre"],
            "_precio_p2p":      precio_p2p,
            "_divisa":          m["divisa"],
            "_meses":           meses,
            "_tipo_renta":      m["tipo_renta"],
            "_tip_dividendo":   m["tip_dividendo"],
            "_tokens_disp":     d["tokens_disponibles"],
            "_colateralizable": m.get("colateralizable", False),
            "_fuente":          d["fuente"],
            "_r_hoy_ann":       r_hoy_ann,
            "_r_hoy_total":     r_hoy_total,
            "_r_rec_ann":       r_rec_ann,
            "_r_alquiler_pend": r_alquiler_pend,
            "_r_plusv":         r_plusv,
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).sort_values("_r_hoy_ann", ascending=False).head(top_n).reset_index(drop=True)
    df["_score"] = range(1, len(df) + 1)
    return df


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

def generar_pdf(df: pd.DataFrame, categoria: str) -> bytes:
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
        ("Rent. total anualizada est. *", [fmt_pct(r["_r_hoy_ann"])                                    for _,r in df.iterrows()]),
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
        "** La rent. al final es la ganancia patrimonial esperada en el cierre del proyecto.",
        f"— Categoría aplicada: {categoria}. Las rentabilidades varían según la categoría del inversor.",
        f"— Solo se incluyen proyectos con más de {MIN_TOKENS} tokens disponibles en el OTC interno de Reental.",
        "— Score: A menor puntuación, mejor oportunidad (ordenado por rentabilidad total anualizada pendiente).",
        "— Este ranking no debe ser tomado como consejo de inversión. Todas las rentabilidades son meras estimaciones.",
    ]
    for nota in notas:
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
        "Rent. anualizada estimada":      fmt_pct(r["_r_hoy_ann"]),
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
st.caption(f"\\*\\* La rent. al final es la ganancia patrimonial esperada al cierre · Mínimo {MIN_TOKENS} tokens disponibles para entrar en el análisis")

# ── Exportar PDF ──────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("📄 Exportar informe")
if st.button("📥 Generar PDF", type="primary"):
    with st.spinner("Generando PDF…"):
        pdf_bytes = generar_pdf(df_ops, categoria)
    st.download_button(
        label="⬇️ Descargar PDF",
        data=pdf_bytes,
        file_name=f"Reental_Oportunidades_P2P_{date.today().strftime('%Y%m%d')}.pdf",
        mime="application/pdf",
        type="primary",
    )
