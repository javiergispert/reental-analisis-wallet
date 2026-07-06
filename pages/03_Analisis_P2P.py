"""
Análisis de Oportunidades P2P — Reental Wealth
Genera el ranking de mejores oportunidades disponibles en el mercado P2P
y permite exportar el informe en PDF.
"""

import io
import os
from datetime import date

import pandas as pd
import requests
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from utils import load_master_projects, load_p2p_listings, parse_pct, parse_float_val

# ── Colores de marca ──────────────────────────────────────────────────────────
NARANJA   = colors.HexColor("#F15A2B")
VERDE     = colors.HexColor("#1D8348")
GRIS_OSC  = colors.HexColor("#2C3E50")
GRIS_CLAR = colors.HexColor("#F2F3F4")
BLANCO    = colors.white

# ── Configuración de categorías ───────────────────────────────────────────────
CATEGORIAS = {
    "SR (SuperReentel)": {
        "r_hoy_total":  "r_hoy_total_sr",
        "r_hoy_ann":    "r_hoy_ann_sr",
        "r_rec_ann":    "r_rec_ann_sr",
        "r_plusv":      "r_plusv_sr",
    },
    "RP (ReentelPro)": {
        "r_hoy_total":  "r_hoy_total_rp",
        "r_hoy_ann":    "r_hoy_ann_rp",
        "r_rec_ann":    "r_rec_ann_rp",
        "r_plusv":      "r_plusv_rp",
    },
    "Reentel": {
        "r_hoy_total":  "r_hoy_total_reentel",
        "r_hoy_ann":    "r_hoy_ann_reentel",
        "r_rec_ann":    "r_rec_ann_reentel",
        "r_plusv":      "r_plusv_reentel",
    },
}

TIPO_RENTA_LABELS = {
    "todas":      "Todas",
    "final":      "Solo renta final",
    "recurrente": "Solo renta recurrente",
    "mixto":      "Mixta (recurrente + final)",
}

MIN_TOKENS_P2P = 20  # filtro mínimo de tokens disponibles


# ── Helpers de cálculo ────────────────────────────────────────────────────────

def calcular_oportunidades(master_df: pd.DataFrame, p2p_df: pd.DataFrame,
                            categoria: str, tipo_renta: str, top_n: int) -> pd.DataFrame:
    """
    Cruza master con P2P, ajusta rentabilidades al precio P2P real y
    devuelve el top N ordenado por score (mayor rentabilidad anualizada = mejor score).
    """
    cols = CATEGORIAS[categoria]

    rows = []
    for _, p in p2p_df.iterrows():
        pid            = str(p["id"]).strip()
        tokens_disp    = float(p.get("tokens_disponibles") or 0)
        precio_p2p     = float(p.get("precio_p2p_usdt") or 0)

        if tokens_disp < MIN_TOKENS_P2P or precio_p2p <= 0:
            continue

        # Buscar proyecto en master
        m_rows = master_df[master_df["id"] == pid]
        if m_rows.empty:
            continue
        m = m_rows.iloc[0]

        # Filtro tipo renta
        if tipo_renta != "todas" and m["tipo_renta"] != tipo_renta:
            continue

        precio_emision = m["precio_emision"] or 0
        if precio_emision <= 0:
            continue

        meses = m["meses_pendientes"] or 0
        if meses <= 0:
            continue

        # Factor de ajuste: rentabilidades del master están sobre precio de emisión.
        # Si el inversor paga precio_p2p distinto, se ajusta proporcionalmente.
        # Ambas cifras están en la divisa nativa del token (EUR o USD).
        adj = precio_emision / precio_p2p

        r_hoy_total_raw = m[cols["r_hoy_total"]]
        r_hoy_ann_raw   = m[cols["r_hoy_ann"]]
        r_rec_ann_raw   = m[cols["r_rec_ann"]]
        r_plusv_raw     = m[cols["r_plusv"]]

        if r_hoy_ann_raw is None:
            continue

        r_hoy_total = (r_hoy_total_raw or 0) * adj
        r_hoy_ann   = (r_hoy_ann_raw   or 0) * adj
        r_rec_ann   = (r_rec_ann_raw   or 0) * adj
        r_plusv     = (r_plusv_raw     or 0) * adj

        # Rentabilidad por alquiler pendiente = r_rec_ann * (meses/12)
        r_alquiler_pendiente = r_rec_ann * (meses / 12)

        rows.append({
            "_id":              pid,
            "_nombre":          m["nombre"],
            "_precio_p2p":      precio_p2p,
            "_divisa":          m["divisa"],
            "_meses":           meses,
            "_tipo_renta":      m["tipo_renta"],
            "_tip_dividendo":   m["tip_dividendo"],
            "_tokens_disp":     tokens_disp,
            "_colateralizable": m["colateralizable"],
            "_r_hoy_total":     r_hoy_total,
            "_r_hoy_ann":       r_hoy_ann,
            "_r_rec_ann":       r_rec_ann,
            "_r_alquiler_pend": r_alquiler_pendiente,
            "_r_plusv":         r_plusv,
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.sort_values("_r_hoy_ann", ascending=False).head(top_n).reset_index(drop=True)
    df["_score"] = range(1, len(df) + 1)
    return df


def fmt_pct(v) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.2f}%"

def fmt_precio(v, divisa) -> str:
    sym = "€" if divisa == "EUR" else "$"
    return f"{v:,.2f} {sym}"

def tip_dividendo_label(raw: str) -> str:
    r = raw.lower()
    if "mensual" in r and "final" in r:
        return "Rendimientos mensuales + final"
    if "trimestral" in r and "final" in r:
        return "Rendimientos trimestrales + final"
    if "final" in r:
        return "Rendimientos a final del proyecto"
    if "mensual" in r:
        return "Rendimientos mensuales"
    return raw.capitalize()


# ── Generación del PDF ────────────────────────────────────────────────────────

def generar_pdf(df: pd.DataFrame, categoria: str, top_n: int) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )

    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle(
        "cell", fontSize=7.5, leading=10, alignment=TA_CENTER,
        fontName="Helvetica",
    )
    cell_bold = ParagraphStyle(
        "cell_bold", fontSize=7.5, leading=10, alignment=TA_CENTER,
        fontName="Helvetica-Bold",
    )
    header_style = ParagraphStyle(
        "header", fontSize=8, leading=10, alignment=TA_CENTER,
        fontName="Helvetica-Bold", textColor=BLANCO,
    )
    nota_style = ParagraphStyle(
        "nota", fontSize=7, leading=9.5, alignment=TA_LEFT,
        fontName="Helvetica", textColor=GRIS_OSC,
    )

    story = []

    # ── Cabecera ──
    titulo_style = ParagraphStyle(
        "titulo", fontSize=20, leading=24, alignment=TA_LEFT,
        fontName="Helvetica-Bold", textColor=GRIS_OSC,
    )
    fecha_style = ParagraphStyle(
        "fecha", fontSize=9, leading=12, alignment=TA_RIGHT,
        fontName="Helvetica", textColor=GRIS_OSC,
    )
    header_row = Table(
        [[
            Paragraph("<font color='#F15A2B'>Reental</font> <b>Wealth</b> · Reporte Oportunidades P2P", titulo_style),
            Paragraph(f"Fecha: {date.today().strftime('%d/%m/%Y')}", fecha_style),
        ]],
        colWidths=["70%", "30%"],
    )
    header_row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story.append(header_row)
    story.append(Spacer(1, 0.3 * cm))
    story.append(HRFlowable(width="100%", thickness=2, color=NARANJA))
    story.append(Spacer(1, 0.4 * cm))

    # ── Subtítulo ──
    sub_style = ParagraphStyle(
        "sub", fontSize=13, leading=16, alignment=TA_CENTER,
        fontName="Helvetica-Bold", textColor=BLANCO,
    )
    sub_table = Table(
        [[Paragraph(f"Top {len(df)} mejores oportunidades en P2P — Categoría {categoria}", sub_style)]],
        colWidths=["100%"],
    )
    sub_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), VERDE),
        ("TOPPADDING",    (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("ROUNDEDCORNERS", [6]),
    ]))
    story.append(sub_table)
    story.append(Spacer(1, 0.5 * cm))

    # ── Tabla de datos ──
    n_cols = len(df)
    page_w = landscape(A4)[0] - 3 * cm  # ancho disponible
    label_w = 5.2 * cm
    data_w  = (page_w - label_w) / n_cols

    headers = [
        "Ranking", "Score", "ID Token", "Nombre Inmueble",
        "Precio/Token P2P", "Divisa",
        "Est. Nº Meses hasta fin",
        "Rent. total anualizada estimada *",
        "Rent. total pendiente estimada *",
        "Rent. por alquiler estimada *",
        "Rent. anualizada por alquiler real *",
        "Rent. al final estimada **",
        "Tipología de Dividendos",
        "Nº tokens disponibles",
        "¿Es Colateralizable?",
    ]

    ranking_labels = ["1º", "2º", "3º", "4º", "5º", "6º", "7º", "8º", "9º", "10º"]

    data_rows = [
        [Paragraph(h, cell_bold) for h in [""] + [ranking_labels[i] for i in range(n_cols)]],
    ]

    field_rows = [
        ("Score",          [str(int(r["_score"])) for _, r in df.iterrows()]),
        ("ID Token",       [str(r["_id"]) for _, r in df.iterrows()]),
        ("Nombre Inmueble",[str(r["_nombre"]) for _, r in df.iterrows()]),
        ("Precio/Token P2P", [fmt_precio(r["_precio_p2p"], r["_divisa"]) for _, r in df.iterrows()]),
        ("Divisa",         ["€" if r["_divisa"] == "EUR" else "$" for _, r in df.iterrows()]),
        ("Est. Meses hasta fin", [f"{r['_meses']:.1f}" for _, r in df.iterrows()]),
        ("Rent. total anualizada estimada *",   [fmt_pct(r["_r_hoy_ann"])      for _, r in df.iterrows()]),
        ("Rent. total pendiente estimada *",    [fmt_pct(r["_r_hoy_total"])    for _, r in df.iterrows()]),
        ("Rent. por alquiler estimada *",       [fmt_pct(r["_r_alquiler_pend"]) for _, r in df.iterrows()]),
        ("Rent. anualizada alquiler real *",    [fmt_pct(r["_r_rec_ann"])      for _, r in df.iterrows()]),
        ("Rent. al final estimada **",          [fmt_pct(r["_r_plusv"])        for _, r in df.iterrows()]),
        ("Tipología de Dividendos",             [tip_dividendo_label(r["_tip_dividendo"]) for _, r in df.iterrows()]),
        ("Nº tokens disponibles",               [f"{int(r['_tokens_disp']):,}" for _, r in df.iterrows()]),
        ("¿Es Colateralizable?",                ["Colateralizable" if r["_colateralizable"] else "No" for _, r in df.iterrows()]),
    ]

    for label, values in field_rows:
        row = [Paragraph(label, cell_bold)] + [Paragraph(v, cell_style) for v in values]
        data_rows.append(row)

    col_widths = [label_w] + [data_w] * n_cols
    tabla = Table(data_rows, colWidths=col_widths, repeatRows=1)

    # Estilo base
    ts = [
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.HexColor("#D5D8DC")),
        ("BACKGROUND",    (0, 0), (-1, 0),  VERDE),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  BLANCO),
        ("BACKGROUND",    (0, 0), (0, -1),  GRIS_OSC),
        ("TEXTCOLOR",     (0, 0), (0, -1),  BLANCO),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]
    # Filas alternas
    for i in range(1, len(data_rows)):
        bg = GRIS_CLAR if i % 2 == 0 else BLANCO
        ts.append(("BACKGROUND", (1, i), (-1, i), bg))

    # Destacar fila ranking y score con naranja suave
    ts.append(("BACKGROUND", (0, 0), (-1, 0), VERDE))

    tabla.setStyle(TableStyle(ts))
    story.append(tabla)
    story.append(Spacer(1, 0.5 * cm))

    # ── Notas al pie ──
    notas = [
        "* Las rentabilidades están calculadas en base a la rentabilidad real acumulada recurrente y proyectando al mismo nivel hasta su fin estimado, ajustadas al precio P2P.",
        "** La rentabilidad al final hace referencia únicamente a la ganancia patrimonial que se origina en el cierre del proyecto.",
        f"— Categoría aplicada: {categoria}. Las rentabilidades varían según la categoría del inversor.",
        "— Los inmuebles que entran en el análisis tienen que tener una oferta en el mercado P2P de más de 20 tokens.",
        "— Score: Puntuación ponderada en base al tiempo estimado restante y su rentabilidad. A menor puntuación, mejor.",
        "— Este ranking no debe ser tomado como consejo de inversión. Todas las rentabilidades indicadas son meras estimaciones.",
    ]
    for nota in notas:
        story.append(Paragraph(nota, nota_style))

    doc.build(story)
    return buf.getvalue()


# ── Interfaz Streamlit ────────────────────────────────────────────────────────

st.title("📊 Análisis de Oportunidades P2P")
st.caption("Ranking en tiempo real de las mejores oportunidades disponibles en el mercado P2P de Reental.")

# Cargar datos
with st.spinner("Cargando datos del mercado P2P…"):
    master_df = load_master_projects()
    p2p_df    = load_p2p_listings(master_df)

if master_df.empty or p2p_df.empty:
    st.error("No se han podido cargar los datos del master o del mercado P2P.")
    st.stop()

# ── Filtros ───────────────────────────────────────────────────────────────────
st.subheader("⚙️ Configuración del informe")
f1, f2, f3 = st.columns([2, 2, 1])

categoria = f1.selectbox(
    "Categoría del inversor",
    list(CATEGORIAS.keys()),
    index=0,
    help="Cada categoría tiene rentabilidades diferentes según las condiciones de Reental.",
)

tipo_renta_sel = f2.selectbox(
    "Tipo de renta",
    list(TIPO_RENTA_LABELS.keys()),
    format_func=lambda x: TIPO_RENTA_LABELS[x],
    index=0,
)

top_n = f3.number_input(
    "Top N oportunidades", min_value=1, max_value=10, value=5, step=1,
)

st.markdown("---")

# ── Cálculo ───────────────────────────────────────────────────────────────────
df_ops = calcular_oportunidades(master_df, p2p_df, categoria, tipo_renta_sel, int(top_n))

if df_ops.empty:
    st.warning("No hay oportunidades P2P disponibles con los filtros seleccionados (mínimo 20 tokens disponibles).")
    st.stop()

# ── Tabla interactiva ─────────────────────────────────────────────────────────
st.subheader(f"🏆 Top {len(df_ops)} oportunidades · {categoria}")

ranking_labels = ["1º", "2º", "3º", "4º", "5º", "6º", "7º", "8º", "9º", "10º"]

display_rows = []
for _, r in df_ops.iterrows():
    display_rows.append({
        "Ranking":                        ranking_labels[int(r["_score"]) - 1],
        "Score":                          int(r["_score"]),
        "ID":                             r["_id"],
        "Nombre":                         r["_nombre"],
        "Precio P2P":                     fmt_precio(r["_precio_p2p"], r["_divisa"]),
        "Divisa":                         "€" if r["_divisa"] == "EUR" else "$",
        "Meses hasta fin":                f"{r['_meses']:.1f}",
        "Rent. anualizada estimada":      fmt_pct(r["_r_hoy_ann"]),
        "Rent. total pendiente":          fmt_pct(r["_r_hoy_total"]),
        "Rent. alquiler pendiente":       fmt_pct(r["_r_alquiler_pend"]),
        "Rent. alquiler anualizada real": fmt_pct(r["_r_rec_ann"]),
        "Rent. al final":                 fmt_pct(r["_r_plusv"]),
        "Tipo dividendo":                 tip_dividendo_label(r["_tip_dividendo"]),
        "Tokens disponibles":             f"{int(r['_tokens_disp']):,}",
        "Colateralizable":                "✅" if r["_colateralizable"] else "—",
    })

df_display = pd.DataFrame(display_rows)
st.dataframe(df_display, hide_index=True, use_container_width=True)

# Notas
st.caption("\\* Rentabilidades calculadas sobre precio P2P, proyectando la tasa recurrente real hasta el vencimiento estimado.")
st.caption("\\*\\* La rent. al final es la ganancia patrimonial esperada en el cierre del proyecto.")
st.caption(f"Mínimo {MIN_TOKENS_P2P} tokens disponibles para entrar en el análisis · Categoría: {categoria}")

# ── Exportar PDF ─────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("📄 Exportar informe")
st.caption("Genera un PDF listo para compartir con el inversor.")

if st.button("📥 Generar PDF", type="primary"):
    with st.spinner("Generando PDF…"):
        pdf_bytes = generar_pdf(df_ops, categoria, int(top_n))
    fecha_str = date.today().strftime("%Y%m%d")
    nombre_archivo = f"Reental_Oportunidades_P2P_{fecha_str}.pdf"
    st.download_button(
        label="⬇️ Descargar PDF",
        data=pdf_bytes,
        file_name=nombre_archivo,
        mime="application/pdf",
        type="primary",
    )
