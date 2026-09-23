"""
El documento de propuesta: un dossier comercial en PDF.

Mantiene el guion de la plantilla que usa hoy el equipo —portada, resumen de
la cuenta, el efecto del interés compuesto, el análisis de la cartera, la
tabla de activos, las fichas por ubicación y los beneficios del estatus— y le
añade una página de track record con los proyectos ya cerrados, que es lo que
convierte una promesa en un historial.

Se genera con reportlab, igual que los informes de P2P y Mercado RNT Lend, en
vez de montar un HTML y confiar en que cada persona acierte con el diálogo de
impresión: el resultado es idéntico salga de donde salga, y se descarga de un
clic. Los gráficos se incrustan como imagen (plotly + kaleido), así que el
documento no depende de ninguna librería externa para verse.
"""
from __future__ import annotations

import io
from datetime import date

import plotly.graph_objects as go
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
    Spacer, Table, TableStyle)

import maestro

DORADO   = "#F5A623"
NAVY     = "#0D1B2E"
AZUL     = "#3B82F6"
VERDE    = "#4DE4A0"

PDF_DORADO = colors.HexColor(DORADO)
PDF_NAVY   = colors.HexColor(NAVY)
PDF_AZUL   = colors.HexColor(AZUL)
PDF_GRIS   = colors.HexColor("#F2F4F8")
PDF_BORDE  = colors.HexColor("#CBD5E1")

PAGINA = landscape(A4)
ANCHO_UTIL = PAGINA[0] - 3.0 * cm

PALETA = ["#F5A623", "#3B82F6", "#4DE4A0", "#1E4080", "#E05C38", "#7ECBA1", "#CC88FF"]


# ─── Formato ─────────────────────────────────────────────────────────────────

def eur(v):
    return "—" if v is None else f"{v:,.2f} €".replace(",", "@").replace(".", ",").replace("@", ".")


def usd(v):
    return "—" if v is None else f"${v:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")


def pct(v, dec=2):
    return "—" if v is None else f"{v * 100:,.{dec}f} %".replace(".", ",")


def num(v, dec=0):
    return "—" if v is None else f"{v:,.{dec}f}".replace(",", "@").replace(".", ",").replace("@", ".")


def _fecha(d):
    if not d:
        return "—"
    meses = ["ene", "feb", "mar", "abr", "may", "jun",
             "jul", "ago", "sep", "oct", "nov", "dic"]
    return f"{meses[d.month - 1]} {d.year}"


# ─── Gráficos ────────────────────────────────────────────────────────────────

def _png(fig, ancho=1100, alto=430) -> bytes:
    fig.update_layout(template="plotly_white", margin=dict(l=45, r=25, t=35, b=40),
                      font=dict(family="Helvetica", size=14, color=NAVY),
                      paper_bgcolor="white", plot_bgcolor="white")
    return fig.to_image(format="png", width=ancho, height=alto, scale=2)


def grafico_reinversion(escenarios: list) -> bytes:
    """Ganancia acumulada por estatus a lo largo del horizonte."""
    meses = [p["meses"] for p in escenarios[0]["puntos"]]
    fig = go.Figure()
    for fila, color in zip(escenarios, (AZUL, "#1E4080", DORADO)):
        fig.add_bar(name=fila["estatus"], x=[f"{m} meses" for m in meses],
                    y=[p["ganancia"] for p in fila["puntos"]],
                    marker_color=color,
                    text=[f"{p['ganancia']:,.0f} €" for p in fila["puntos"]],
                    textposition="outside", textfont=dict(size=12))
    fig.update_layout(barmode="group", yaxis_title="Ganancia acumulada (€)",
                      legend=dict(orientation="h", y=1.12, x=0))
    return _png(fig, 1100, 420)


def grafico_reparto(reparto: dict, titulo: str) -> bytes:
    """Un anillo con el peso de cada categoría en la cartera."""
    etiquetas = list(reparto.keys())
    valores = [v * 100 for v in reparto.values()]
    fig = go.Figure(go.Pie(labels=etiquetas, values=valores, hole=0.55,
                           marker=dict(colors=PALETA[:len(etiquetas)]),
                           textinfo="label+percent", textfont=dict(size=13),
                           sort=False))
    fig.update_layout(title=dict(text=titulo, x=0.5, font=dict(size=15)),
                      showlegend=False)
    return _png(fig, 520, 420)


def grafico_track(track: dict) -> bytes:
    """Estimado frente a real en los proyectos ya cerrados."""
    fig = go.Figure()
    fig.add_bar(name="Estimado", x=["Reentel", "SuperReentel"],
                y=[track["media_est_rnt"] * 100, track["media_est_sr"] * 100],
                marker_color="#1E4080",
                text=[pct(track["media_est_rnt"]), pct(track["media_est_sr"])],
                textposition="outside")
    fig.add_bar(name="Real", x=["Reentel", "SuperReentel"],
                y=[track["media_real_rnt"] * 100, track["media_real_sr"] * 100],
                marker_color=DORADO,
                text=[pct(track["media_real_rnt"]), pct(track["media_real_sr"])],
                textposition="outside")
    fig.update_layout(barmode="group", yaxis_title="Rentabilidad anualizada (%)",
                      legend=dict(orientation="h", y=1.15, x=0))
    return _png(fig, 620, 400)


# ─── Documento ───────────────────────────────────────────────────────────────

def _estilos():
    return {
        "h1":    ParagraphStyle("h1", fontSize=30, leading=34, fontName="Helvetica-Bold",
                                textColor=colors.white, alignment=TA_LEFT),
        "h1sub": ParagraphStyle("h1s", fontSize=14, leading=18, fontName="Helvetica",
                                textColor=colors.HexColor("#AAB6C8"), alignment=TA_LEFT),
        "sec":   ParagraphStyle("sec", fontSize=12, leading=15, fontName="Helvetica-Bold",
                                textColor=PDF_NAVY, alignment=TA_LEFT),
        "txt":   ParagraphStyle("txt", fontSize=9.5, leading=13, fontName="Helvetica",
                                textColor=PDF_NAVY, alignment=TA_LEFT),
        "nota":  ParagraphStyle("nota", fontSize=7.5, leading=10, fontName="Helvetica",
                                textColor=colors.HexColor("#5A6675"), alignment=TA_LEFT),
        "cel":   ParagraphStyle("cel", fontSize=8, leading=10.5, fontName="Helvetica",
                                textColor=PDF_NAVY, alignment=TA_CENTER),
        "celL":  ParagraphStyle("celL", fontSize=8, leading=10.5, fontName="Helvetica",
                                textColor=PDF_NAVY, alignment=TA_LEFT),
        "cab":   ParagraphStyle("cab", fontSize=8, leading=10.5, fontName="Helvetica-Bold",
                                textColor=colors.white, alignment=TA_CENTER),
        "cabL":  ParagraphStyle("cabL", fontSize=8, leading=10.5, fontName="Helvetica-Bold",
                                textColor=colors.white, alignment=TA_LEFT),
        "kpiv":  ParagraphStyle("kpiv", fontSize=22, leading=25, fontName="Helvetica-Bold",
                                textColor=PDF_DORADO, alignment=TA_CENTER),
        "kpil":  ParagraphStyle("kpil", fontSize=8, leading=11, fontName="Helvetica",
                                textColor=PDF_NAVY, alignment=TA_CENTER),
    }


def _banda(titulo, E):
    t = Table([[Paragraph(titulo, ParagraphStyle("b", fontSize=11, leading=14,
                                                 fontName="Helvetica-Bold",
                                                 textColor=colors.white))]],
              colWidths=[ANCHO_UTIL])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), PDF_NAVY),
                           ("TOPPADDING", (0, 0), (-1, -1), 7),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10)]))
    return t


def _tabla(cab, filas, anchos, E, alineaciones=None):
    data = [[Paragraph(str(c), E["cabL"] if i == 0 else E["cab"]) for i, c in enumerate(cab)]]
    for f in filas:
        data.append([Paragraph(str(v), E["celL"] if i == 0 else E["cel"])
                     for i, v in enumerate(f)])
    t = Table(data, colWidths=anchos, repeatRows=1)
    ts = [("GRID", (0, 0), (-1, -1), 0.4, PDF_BORDE),
          ("BACKGROUND", (0, 0), (-1, 0), PDF_NAVY),
          ("TOPPADDING", (0, 0), (-1, -1), 4),
          ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
          ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
    for i in range(1, len(data)):
        ts.append(("BACKGROUND", (0, i), (-1, i), PDF_GRIS if i % 2 == 0 else colors.white))
    t.setStyle(TableStyle(ts))
    return t


def _kpis(items, E):
    """Fila de tarjetas: (valor, etiqueta) o (valor, etiqueta, color)."""
    celdas = []
    for it in items:
        valor, etiqueta = it[0], it[1]
        color = it[2] if len(it) > 2 else PDF_DORADO
        est = ParagraphStyle("k", parent=E["kpiv"], textColor=color)
        celdas.append(Table([[Paragraph(valor, est)], [Paragraph(etiqueta, E["kpil"])]],
                            colWidths=[(ANCHO_UTIL - 0.6 * cm * (len(items) - 1)) / len(items)]))
    t = Table([celdas], colWidths=[ANCHO_UTIL / len(items)] * len(items))
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0, colors.white),
                           ("BACKGROUND", (0, 0), (-1, -1), PDF_GRIS),
                           ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.white),
                           ("TOPPADDING", (0, 0), (-1, -1), 10),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 10)]))
    return t


def construir(datos: dict) -> bytes:
    """El PDF completo.

    `datos` trae: titular, estatus, cartera, escenarios, track, precio_rnt,
    eurusd, coste_estatus, tasas y si va marcado como borrador.
    """
    E = _estilos()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=PAGINA,
                            leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                            topMargin=1.3 * cm, bottomMargin=1.6 * cm,
                            title=f"Propuesta Reental — {datos.get('titular', '')}",
                            author="Reental Wealth")

    cartera = datos["cartera"]
    ep = datos["estatus"]
    hoy = datos.get("fecha") or date.today()
    story = []

    # ── 1. Portada ───────────────────────────────────────────────────────────
    portada = Table(
        [[Paragraph("SIMULACIÓN DE CARTERA INMOBILIARIA", E["h1"])],
         [Spacer(1, 0.4 * cm)],
         [Paragraph(f"Propuesta para <b>{datos.get('titular') or '—'}</b>", E["h1sub"])],
         [Paragraph(f"Estatus propuesto: <font color='{DORADO}'><b>{ep}</b></font>", E["h1sub"])],
         [Spacer(1, 1.2 * cm)],
         [Paragraph(f"Elaborado por el servicio <b>Reental Wealth</b> · "
                    f"{hoy.strftime('%d/%m/%Y')}", E["h1sub"])]],
        colWidths=[ANCHO_UTIL])
    portada.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), PDF_NAVY),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 28),
                                 ("RIGHTPADDING", (0, 0), (-1, -1), 28),
                                 ("TOPPADDING", (0, 0), (0, 0), 46),
                                 ("BOTTOMPADDING", (0, -1), (-1, -1), 46)]))
    story += [portada, Spacer(1, 0.5 * cm),
              Paragraph(
                  "Los inversores de Reental se agrupan en tres categorías. SuperReentel es la más alta "
                  "y puede alcanzar hasta un 50 % más de rendimiento en cada proyecto; después "
                  "ReentelPro y, como categoría base, Reentel. Más información en "
                  "<font color='#3B82F6'>reental.co/rnt-token</font>.", E["nota"]),
              PageBreak()]

    # ── 2. Resumen de la cuenta ──────────────────────────────────────────────
    coste = datos["coste_estatus"]
    total_eur = (cartera["eur"] or 0) + (coste.get("eur") or 0)
    total_usd = (cartera["usd"] or 0) + (coste.get("usd") or 0)
    r_base = cartera["rentabilidad"]["Reentel"]
    r_prop = cartera["rentabilidad"][ep]

    story += [_banda("RESUMEN DE LA CUENTA", E), Spacer(1, 0.4 * cm),
              _kpis([(num(cartera["n"]), "inmuebles en cartera"),
                     (num(cartera["tokens"]), "tokens en total"),
                     (num(cartera["meses_medios"], 1), "meses medios hasta el fin de los proyectos"),
                     (pct(r_prop["anual"]), f"rentabilidad anualizada estimada · {ep}")], E),
              Spacer(1, 0.5 * cm)]

    story += [_tabla(
        ["Concepto", "Importe (€)", "Importe ($)"],
        [["Inversión en inmuebles", eur(cartera["eur"]), usd(cartera["usd"])],
         [f"Adquisición del estatus {ep} ({num(coste.get('rnts'))} RNT)",
          eur(coste.get("eur")), usd(coste.get("usd"))],
         ["<b>Capital total desplegado</b>", f"<b>{eur(total_eur)}</b>", f"<b>{usd(total_usd)}</b>"]],
        [ANCHO_UTIL * 0.5, ANCHO_UTIL * 0.25, ANCHO_UTIL * 0.25], E),
        Spacer(1, 0.5 * cm)]

    story += [_tabla(
        ["Rentabilidad estimada de la cartera", "Reentel (base)", f"{ep} (propuesto)"],
        [["Anualizada (renta + plusvalía)", pct(r_base["anual"]), pct(r_prop["anual"])],
         ["Total sobre la vida de los proyectos", pct(r_base["total"]), pct(r_prop["total"])],
         ["Solo renta recurrente, anualizada", pct(r_base["recurr"]), pct(r_prop["recurr"])],
         ["Solo plusvalía, sobre la vida del proyecto", pct(r_base["plusvalia"]), pct(r_prop["plusvalia"])]],
        [ANCHO_UTIL * 0.5, ANCHO_UTIL * 0.25, ANCHO_UTIL * 0.25], E),
        Spacer(1, 0.35 * cm),
        Paragraph(
            "Cada rentabilidad se pondera por el importe invertido en cada proyecto, no por el número de "
            f"tokens. Tipo de cambio aplicado: 1 € = {datos['eurusd']:.4f} $. Precio del RNT tomado del "
            f"pool RNT/USDT: {datos['precio_rnt']:.4f} $.".replace(".", ","), E["nota"]),
        PageBreak()]

    # ── 3. Reinversión ───────────────────────────────────────────────────────
    esc = datos["escenarios"]
    story += [_banda("LA CARTERA CON REINVERSIÓN: EL EFECTO DEL INTERÉS COMPUESTO", E),
              Spacer(1, 0.35 * cm)]
    try:
        img = grafico_reinversion(esc)
        story.append(Image(io.BytesIO(img), width=ANCHO_UTIL * 0.92,
                           height=ANCHO_UTIL * 0.92 * 420 / 1100))
    except Exception:       # noqa: BLE001 — sin gráfico, la tabla ya lo cuenta
        pass
    story.append(Spacer(1, 0.35 * cm))

    meses = [p["meses"] for p in esc[0]["puntos"]]
    filas = []
    for fila in esc:
        filas.append([f"Ganancia acumulada · {fila['estatus']}"] +
                     [eur(p["ganancia"]) for p in fila["puntos"]])
    for fila in esc:
        if fila["coste_estatus"]:
            filas.append([f"Rentabilidad sobre el capital total · {fila['estatus']}"] +
                         [pct(p["sobre_total"]) for p in fila["puntos"]])
        else:
            filas.append([f"Rentabilidad sobre el capital total · {fila['estatus']}"] +
                         [pct(p["sobre_cartera"]) for p in fila["puntos"]])
    story += [_tabla(["Escenario"] + [f"{m} meses" for m in meses], filas,
                     [ANCHO_UTIL * 0.4] + [ANCHO_UTIL * 0.15] * len(meses), E),
              Spacer(1, 0.3 * cm),
              Paragraph(
                  "Cada proyecto aporta según su propio calendario: la renta recurrente entra mes a mes y la "
                  "plusvalía al cerrarse el proyecto, junto con la devolución del capital. Lo que se va "
                  "cobrando se reinvierte a la tasa indicada. La rentabilidad se calcula sobre el "
                  "<b>capital total desplegado</b>, que incluye la compra del estatus — el RNT adquirido no "
                  "se consume: se conserva y además genera rendimiento en staking, que ya está contado aquí.",
                  E["nota"]),
              PageBreak()]

    # ── 4. Análisis de la cartera ────────────────────────────────────────────
    story += [_banda("ANÁLISIS DE LA CARTERA PROPUESTA", E), Spacer(1, 0.35 * cm)]
    imgs = []
    for clave, titulo in (("ubicacion", "Ubicación del inmueble"),
                          ("emision", "Emisión de tokenización"),
                          ("dividendo", "Tipología de dividendo")):
        rep = cartera["reparto"].get(clave) or {}
        if not rep:
            continue
        try:
            png = grafico_reparto(rep, titulo)
            imgs.append(Image(io.BytesIO(png), width=ANCHO_UTIL / 3.25,
                              height=ANCHO_UTIL / 3.25 * 420 / 520))
        except Exception:   # noqa: BLE001
            pass
    if imgs:
        t = Table([imgs], colWidths=[ANCHO_UTIL / len(imgs)] * len(imgs))
        t.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        story.append(t)
    story += [Spacer(1, 0.3 * cm),
              Paragraph(
                  "La <b>emisión de tokenización</b> indica bajo qué estructura se emitió cada token y no "
                  "coincide necesariamente con la ubicación del inmueble ni con su moneda: hay proyectos de "
                  "la emisión estadounidense situados en España y denominados en euros.", E["nota"]),
              PageBreak()]

    # ── 5. Resumen de activos ────────────────────────────────────────────────
    suf_ep = dict(maestro.ESTATUS)[ep]
    story += [_banda("RESUMEN DE ACTIVOS", E), Spacer(1, 0.35 * cm)]
    filas = []
    for l in cartera["lineas"]:
        p = l["proyecto"]
        filas.append([
            f"<b>{p['label']}</b> · {p.get('nombre', '')}",
            p.get("ubicacion", "—"), p.get("estado", "—"),
            num(l["tokens"]), pct(l["peso"], 1),
            eur(l["importe"]["eur"]),
            _fecha(p.get("fecha_fin_estimada")),
            pct(maestro.rentabilidad(p, "rnt", "anual")),
            pct(maestro.rentabilidad(p, suf_ep, "anual")),
        ])
    story.append(_tabla(
        ["Inmueble", "Ubicación", "Estado", "Tokens", "% cartera", "Inversión (€)",
         "Fin estimado", "Rent. Reentel", f"Rent. {ep}"],
        filas,
        [ANCHO_UTIL * c for c in (0.26, 0.10, 0.11, 0.07, 0.08, 0.12, 0.08, 0.09, 0.09)], E))
    story.append(PageBreak())

    # ── 6. Fichas por ubicación ──────────────────────────────────────────────
    por_region = {}
    for l in cartera["lineas"]:
        por_region.setdefault(l["proyecto"].get("ubicacion") or "Otros", []).append(l)
    for region, lineas in por_region.items():
        story.append(_banda(f"ANÁLISIS POR UBICACIÓN: {region.upper()}", E))
        story.append(Spacer(1, 0.3 * cm))
        for l in lineas:
            p = l["proyecto"]
            izq = [Paragraph(f"<b>{p.get('nombre', '')}</b>  ·  {p['label']}", E["sec"]),
                   Spacer(1, 0.15 * cm),
                   Paragraph(f"{p.get('ubicacion', '')} · {p.get('tipologia_explotacion', '')} · "
                             f"{p.get('tipologia_dividendo', '')}", E["txt"]),
                   Paragraph(f"{p.get('emision', '')} · moneda del proyecto: {p.get('divisa', '')} · "
                             f"{p.get('estado', '')}", E["txt"]),
                   Spacer(1, 0.15 * cm),
                   Paragraph((p.get("descripcion") or "")[:420], E["nota"])]
            der = _tabla(["Concepto", "Valor"],
                         [["Tokens", num(l["tokens"])],
                          ["Inversión", eur(l["importe"]["eur"])],
                          ["Inicio de renta", _fecha(p.get("fecha_inicio_renta"))],
                          ["Fin estimado", _fecha(p.get("fecha_fin_estimada"))],
                          ["Rent. anualizada Reentel", pct(maestro.rentabilidad(p, "rnt", "anual"))],
                          [f"Rent. anualizada {ep}", pct(maestro.rentabilidad(p, suf_ep, "anual"))]],
                         [ANCHO_UTIL * 0.20, ANCHO_UTIL * 0.16], E)
            ficha = Table([[izq, der]], colWidths=[ANCHO_UTIL * 0.62, ANCHO_UTIL * 0.38])
            ficha.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                       ("BACKGROUND", (0, 0), (0, 0), PDF_GRIS),
                                       ("LEFTPADDING", (0, 0), (0, 0), 10),
                                       ("RIGHTPADDING", (0, 0), (0, 0), 10),
                                       ("TOPPADDING", (0, 0), (-1, -1), 8),
                                       ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
            story += [KeepTogether(ficha), Spacer(1, 0.3 * cm)]
        story.append(PageBreak())

    # ── 7. Track record ──────────────────────────────────────────────────────
    track = datos.get("track") or {}
    if track.get("n"):
        story += [_banda("HISTORIAL: LO QUE PASÓ CON LOS PROYECTOS YA CERRADOS", E),
                  Spacer(1, 0.4 * cm),
                  _kpis([(num(track["n"]), "proyectos cerrados"),
                         (f"{track['pct_cumplieron']:.0f} %".replace(".", ","),
                          "cumplieron o superaron la rentabilidad estimada", colors.HexColor(VERDE)),
                         (f"{track['pct_en_plazo']:.0f} %".replace(".", ","),
                          "cerraron en plazo o antes", PDF_AZUL),
                         (pct(track["media_real_rnt"]), "rentabilidad real media anualizada")], E),
                  Spacer(1, 0.4 * cm)]
        try:
            png = grafico_track(track)
            story.append(Image(io.BytesIO(png), width=ANCHO_UTIL * 0.45,
                               height=ANCHO_UTIL * 0.45 * 400 / 620))
        except Exception:   # noqa: BLE001
            pass
        story += [Spacer(1, 0.3 * cm),
                  Paragraph(
                      "Cifras reales de los proyectos que Reental ya ha cerrado y liquidado, no estimaciones. "
                      "La rentabilidad real se compara con la que se estimó al lanzarlos.", E["nota"]),
                  PageBreak()]

        ultimos = track["proyectos"][:12]
        story += [_banda("HISTORIAL — DETALLE DE LOS ÚLTIMOS PROYECTOS CERRADOS", E),
                  Spacer(1, 0.35 * cm)]
        filas = []
        for f in ultimos:
            desv = f["desv_meses"]
            filas.append([
                f"<b>{f['id']}</b> · {f['nombre']}", f["ubicacion"],
                _fecha(f["fecha_estimada"]), _fecha(f["fecha_real"]),
                "—" if desv is None else f"{desv:+.0f} m".replace(".", ","),
                pct(f["est_rnt"]), pct(f["real_rnt"]),
                "—" if f["var_rnt"] is None else f"{f['var_rnt'] * 100:+.1f} pp".replace(".", ","),
            ])
        story.append(_tabla(
            ["Inmueble", "Ubicación", "Fin estimado", "Fin real", "Desv.",
             "Rent. estimada", "Rent. real", "Dif."],
            filas, [ANCHO_UTIL * c for c in (0.28, 0.12, 0.10, 0.10, 0.08, 0.11, 0.11, 0.10)], E))
        story.append(PageBreak())

    # ── 8. Aviso ─────────────────────────────────────────────────────────────
    story += [_banda("CONDICIONES DE ESTA SIMULACIÓN", E), Spacer(1, 0.4 * cm),
              Paragraph(
                  "Este documento es una <b>simulación con fines informativos</b> y no constituye "
                  "asesoramiento financiero, fiscal ni una oferta de inversión. Las rentabilidades "
                  "indicadas como estimadas son proyecciones basadas en los datos de cada proyecto en la "
                  "fecha de emisión de este documento y <b>no garantizan resultados futuros</b>. La "
                  "inversión inmobiliaria tokenizada conlleva riesgo de pérdida del capital.", E["txt"]),
              Spacer(1, 0.3 * cm),
              Paragraph(
                  f"Fuentes y supuestos: datos de proyecto del maestro de inmuebles de Reental; precio del "
                  f"RNT tomado del pool RNT/USDT ({datos['precio_rnt']:.4f} $); tipo de cambio de referencia "
                  f"del Banco Central Europeo (1 € = {datos['eurusd']:.4f} $); tasas de reinversión "
                  f"empleadas: "
                  + " · ".join(f"{f['estatus']} {pct(f['tasa'])}" for f in esc)
                  + f"; rendimiento de staking del RNT {pct(datos.get('tasa_staking', 0))}. "
                    "Los importes en la divisa distinta a la del proyecto son conversiones a la fecha de "
                    "emisión y variarán con el tipo de cambio.", E["nota"])]

    # ── Marca de borrador y pie ──────────────────────────────────────────────
    borrador = datos.get("borrador", True)

    def _pie(canvas, doc_):
        canvas.saveState()
        ancho, alto = PAGINA
        if borrador:
            canvas.setFont("Helvetica-Bold", 60)
            canvas.setFillColor(colors.HexColor("#DC2626"))
            canvas.setFillAlpha(0.09)
            canvas.translate(ancho / 2, alto / 2)
            canvas.rotate(22)
            canvas.drawCentredString(0, 0, "BORRADOR INTERNO")
            canvas.rotate(-22)
            canvas.translate(-ancho / 2, -alto / 2)
            canvas.setFillAlpha(1)
            canvas.setFont("Helvetica-Bold", 7)
            canvas.setFillColor(colors.HexColor("#DC2626"))
            canvas.drawString(1.5 * cm, alto - 0.8 * cm,
                              "BORRADOR DE USO EXCLUSIVAMENTE INTERNO PARA LA COMPAÑÍA REENTAL "
                              "— PENDIENTE DE REVISIÓN POR LEGAL Y COMPLIANCE")
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#8A94A6"))
        canvas.drawString(1.5 * cm, 0.9 * cm,
                          f"Reental Wealth · Simulación de cartera · {hoy.strftime('%d/%m/%Y')} · "
                          "No constituye asesoramiento ni oferta de inversión")
        canvas.drawRightString(ancho - 1.5 * cm, 0.9 * cm, str(canvas.getPageNumber()))
        canvas.restoreState()

    doc.build(story, onFirstPage=_pie, onLaterPages=_pie)
    return buf.getvalue()
