"""
Piezas comunes de los informes en PDF.

POR QUÉ EXISTE
--------------
Cada generador de PDF traía su propia paleta, sus `ParagraphStyle`, su tabla
estándar y su marca de borrador: unas ciento cincuenta líneas repetidas por
documento. Cambiar el dorado corporativo obligaba a tocarlo en varios sitios y
bastaba con olvidar uno para que dos informes de la misma casa salieran
distintos.

Aquí vive lo que no depende del contenido. Lo que sí —qué secciones lleva cada
informe y en qué orden— sigue en su módulo.

Las páginas de P2P y Mercado RNT Lend tienen todavía su propia copia: migrarlas
es mecánico pero toca documentos que ya funcionan, y no había motivo para
hacerlo a la vez que esto.
"""
from __future__ import annotations

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, Table, TableStyle

# ─── Paleta ──────────────────────────────────────────────────────────────────

DORADO    = "#F5A623"
NAVY      = "#0D1B2E"
AZUL      = "#3B82F6"
VERDE     = "#4DE4A0"
VERDE_OSC = "#0F9960"
ROJO      = "#C0392B"

PDF_DORADO = colors.HexColor(DORADO)
PDF_NAVY   = colors.HexColor(NAVY)
PDF_AZUL   = colors.HexColor(AZUL)
PDF_GRIS   = colors.HexColor("#F2F4F8")
PDF_BORDE  = colors.HexColor("#CBD5E1")

PALETA = ["#F5A623", "#3B82F6", "#4DE4A0", "#1E4080", "#E05C38", "#7ECBA1", "#CC88FF"]

PAGINA = landscape(A4)
ANCHO_UTIL = PAGINA[0] - 3.0 * cm


# ─── Formato de cifras ───────────────────────────────────────────────────────
#
# Todo en formato español —miles con punto, decimales con coma— porque el
# documento se lee en España y una cifra con el separador cambiado se entiende
# mal sin que nadie lo note.

def _es(txt: str) -> str:
    return txt.replace(",", "@").replace(".", ",").replace("@", ".")


def eur(v, dec: int = 2):
    return "—" if v is None else _es(f"{v:,.{dec}f}") + " €"


def usd(v, dec: int = 2):
    return "—" if v is None else "$" + _es(f"{v:,.{dec}f}")


def pct(v, dec: int = 2):
    """`v` como fracción: 0,1234 → «12,34 %»."""
    return "—" if v is None else f"{v * 100:,.{dec}f} %".replace(".", ",")


def pct_directo(v, dec: int = 1):
    """`v` ya en porcentaje: 12,34 → «12,3 %»."""
    return "—" if v is None else f"{v:,.{dec}f} %".replace(".", ",")


def num(v, dec: int = 0):
    return "—" if v is None else _es(f"{v:,.{dec}f}")


def fecha_corta(d):
    if not d:
        return "—"
    meses = ["ene", "feb", "mar", "abr", "may", "jun",
             "jul", "ago", "sep", "oct", "nov", "dic"]
    return f"{meses[d.month - 1]} {d.year}"


# ─── Estilos y bloques ───────────────────────────────────────────────────────

def estilos() -> dict:
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
        "celR":  ParagraphStyle("celR", fontSize=8, leading=10.5, fontName="Helvetica",
                                textColor=PDF_NAVY, alignment=TA_RIGHT),
        "cab":   ParagraphStyle("cab", fontSize=8, leading=10.5, fontName="Helvetica-Bold",
                                textColor=colors.white, alignment=TA_CENTER),
        "cabL":  ParagraphStyle("cabL", fontSize=8, leading=10.5, fontName="Helvetica-Bold",
                                textColor=colors.white, alignment=TA_LEFT),
        "kpiv":  ParagraphStyle("kpiv", fontSize=22, leading=25, fontName="Helvetica-Bold",
                                textColor=PDF_DORADO, alignment=TA_CENTER),
        "kpil":  ParagraphStyle("kpil", fontSize=8, leading=11, fontName="Helvetica",
                                textColor=PDF_NAVY, alignment=TA_CENTER),
    }


def banda(titulo: str, ancho: float = ANCHO_UTIL) -> Table:
    """La barra oscura que abre cada sección."""
    t = Table([[Paragraph(titulo, ParagraphStyle("b", fontSize=11, leading=14,
                                                 fontName="Helvetica-Bold",
                                                 textColor=colors.white))]],
              colWidths=[ancho])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), PDF_NAVY),
                           ("TOPPADDING", (0, 0), (-1, -1), 7),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10)]))
    return t


def tabla(cabecera: list, filas: list, anchos: list, E: dict,
          primera_izquierda: bool = True) -> Table:
    """Tabla con cabecera oscura y filas alternas."""
    est_cab = lambda i: E["cabL"] if (i == 0 and primera_izquierda) else E["cab"]
    est_cel = lambda i: E["celL"] if (i == 0 and primera_izquierda) else E["cel"]
    data = [[Paragraph(str(c), est_cab(i)) for i, c in enumerate(cabecera)]]
    for f in filas:
        data.append([Paragraph(str(v), est_cel(i)) for i, v in enumerate(f)])
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


def kpis(items: list, E: dict, ancho: float = ANCHO_UTIL) -> Table:
    """Fila de tarjetas: (valor, etiqueta) o (valor, etiqueta, color)."""
    celdas = []
    for it in items:
        valor, etiqueta = it[0], it[1]
        color = it[2] if len(it) > 2 else PDF_DORADO
        est = ParagraphStyle("k", parent=E["kpiv"], textColor=color)
        celdas.append(Table([[Paragraph(valor, est)], [Paragraph(etiqueta, E["kpil"])]],
                            colWidths=[ancho / len(items)]))
    t = Table([celdas], colWidths=[ancho / len(items)] * len(items))
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0, colors.white),
                           ("BACKGROUND", (0, 0), (-1, -1), PDF_GRIS),
                           ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.white),
                           ("TOPPADDING", (0, 0), (-1, -1), 10),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 10)]))
    return t


def pie(hoy, borrador: bool, izquierda: str, pagina=PAGINA):
    """Devuelve la función de dibujo del pie y, si procede, de la marca de agua.

    El borrador se marca en tres sitios a la vez porque cada uno cubre un fallo
    distinto: la banda superior se lee en la primera ojeada, la diagonal
    sobrevive a una captura de media página, y el pie acompaña a cada hoja si
    alguien imprime y las separa.
    """
    def dibujar(canvas, doc_):
        canvas.saveState()
        ancho, alto = pagina
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
        canvas.drawString(1.5 * cm, 0.9 * cm, izquierda)
        canvas.drawRightString(ancho - 1.5 * cm, 0.9 * cm, str(canvas.getPageNumber()))
        canvas.restoreState()
    return dibujar
