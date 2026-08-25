"""
Tarjetas KPI de la herramienta — presentación compartida entre páginas.

Vive aquí, y no dentro de una página, porque el Analizador y Análisis P2P usan
las mismas tarjetas: tenerlas por duplicado haría que se separaran en cuanto una
de las dos se retocara.

`inyectar_css()` debe llamarse UNA vez por página antes de pintar tarjetas.
"""
from __future__ import annotations

import html

import streamlit as st


KPI_TOOLTIP_CSS = """
<style>
/* Streamlit recorta lo que sobresale de sus contenedores: sin esto el globo
   del tooltip quedaría cortado por el borde de la columna. */
div[data-testid="stVerticalBlock"], div[data-testid="stHorizontalBlock"],
div[data-testid="column"], div[data-testid="stVerticalBlockBorderWrapper"] {
    overflow: visible !important;
}
.kpi-card { position: relative; }
.kpi-card[data-tip]:hover::after {
    content: attr(data-tip);
    position: absolute; left: 0; top: 100%; margin-top: 6px; z-index: 9999;
    width: max-content; max-width: 330px;
    background: #0f172a; color: #f8fafc;
    font-size: 0.72rem; font-weight: 400; line-height: 1.4;
    letter-spacing: 0; text-transform: none; text-align: left;
    padding: 9px 11px; border-radius: 8px;
    box-shadow: 0 6px 18px rgba(15,23,42,.22);
    white-space: normal; pointer-events: none;
}
</style>
"""


def kpi_card(icon, label, value, value_color="#1e293b", sublabel="", badge="", help=""):
    """`help` se muestra en un globo al pasar el ratón por la tarjeta, para
    explicar la fórmula sin obligar a abrir las notas metodológicas. Se usa un
    tooltip CSS propio en vez del atributo `title` del navegador porque aquel
    exige mantener el puntero quieto casi un segundo y no reacciona al clic."""
    tip = f' data-tip="{html.escape(help, quote=True)}"' if help else ""
    cursor = "cursor:help;" if help else ""
    return f"""
    <div class="kpi-card"{tip} style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;
                padding:16px 18px;display:flex;flex-direction:column;gap:4px;{cursor}">
      <div style="font-size:0.72rem;font-weight:600;color:#64748b;
                  letter-spacing:0.05em;text-transform:uppercase;">{icon}&nbsp;{label}{"&nbsp;ⓘ" if help else ""}</div>
      <div style="font-size:1.45rem;font-weight:700;color:{value_color};
                  line-height:1.2;">{value}</div>
      <div style="font-size:0.72rem;color:#94a3b8;">{sublabel}&nbsp;{badge}</div>
    </div>"""


def inyectar_css() -> None:
    """Estilos del globo de ayuda. Idempotente: repetirlo no rompe nada."""
    st.markdown(KPI_TOOLTIP_CSS, unsafe_allow_html=True)
