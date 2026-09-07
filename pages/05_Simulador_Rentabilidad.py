"""
Simulador de rentabilidad por status — calculadora empotrada.

Herramienta de apoyo comercial: compara qué patrimonio proyecta un inversor
según el status que contrate (Reentel / ReentelPro / SuperReentel), con o sin
apalancamiento en ReenLever.

La calculadora es un HTML autocontenido que no hace llamadas de red: se
renderiza en un iframe y calcula todo en el navegador. La página no consulta
nada on-chain — solo lee la foto diaria del mercado Aave, que ya está cacheada,
para inyectarle el interés del préstamo y el umbral de liquidación reales en
lugar de los que traía congelados.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
import streamlit.components.v1 as components
import plotly.graph_objects as go

import aave_snapshot as _snap
import simulador_status as _sim
import coste_prestamo as _coste
import recarga as _recarga

# Streamlit no reimporta lo que ya está en sys.modules: tras un despliegue esta
# página podría convivir con una versión anterior de sus módulos.
_recarga.refrescar("aave_lend", "aave_snapshot", "simulador_status", "coste_prestamo")

@st.fragment
def _umbral_rentabilidad(apr: float) -> None:
    """A partir de qué rentabilidad compensa apalancarse.

    Va ANTES de la calculadora a propósito. La calculadora enseña cuánto se
    gana; esto enseña cuánto hay que ganar para no perder, y ese orden importa:
    quien ya ha visto una proyección a diez años difícilmente vuelve atrás a
    comprobar el umbral.

    Es un fragmento porque es lo único interactivo fuera del iframe: sin
    aislarlo, cada movimiento de un control reenviaría los 130 KB de la
    calculadora al navegador.
    """
    st.markdown("### 🧮 ¿A partir de qué rentabilidad compensa apalancarse?")
    st.caption(
        "El préstamo solo aporta si lo que se compra con él rinde más de lo que cuesta. "
        "Y cuesta más de lo que parece: la deuda capitaliza sola, y los impuestos no tratan "
        "igual a la ganancia que al interés."
    )

    c1, c2, c3, c4 = st.columns([1.3, 1, 1, 1])

    _opciones = [f"{e}  ·  {t * 100:.0f}%" for e, t in _coste.TRAMOS_AHORRO] + ["Otro…"]
    _sel = c1.selectbox(
        "Tramo del inversor", _opciones, index=1, key="um_tramo",
        help=("Tipo marginal con el que tributaría la ganancia. Los tramos son los de la "
              "base del ahorro del IRPF español y se ofrecen solo como referencia: "
              "elige «Otro…» si el inversor tributa en otra jurisdicción o es una "
              "sociedad.\n\n**Esto no es asesoramiento fiscal**: es un supuesto que "
              "introduces tú para ver la sensibilidad del resultado."),
    )
    if _sel == "Otro…":
        _t = c1.number_input("Tipo marginal (%)", 0.0, 60.0, 21.0, 0.5,
                             key="um_tipo_otro") / 100
    else:
        _t = _coste.TRAMOS_AHORRO[_opciones.index(_sel)][1]

    _deducible = c2.checkbox(
        "Intereses deducibles", value=False, key="um_deducible",
        help=("Marca solo si el inversor puede deducir los intereses del préstamo contra "
              "la ganancia. Para una persona física en España es lo habitual que **no** "
              "lo sean, pero depende del caso —una sociedad o una actividad económica "
              "cambian la respuesta— y quien lo confirma es un asesor fiscal.\n\n"
              "Si son deducibles, el umbral vuelve a ser el coste financiero puro: eso es "
              "exactamente lo que significa poder deducirlos."),
    )
    _bruto = c3.number_input(
        "Rentabilidad bruta esperada (%)", 0.0, 60.0, 17.0, 0.5, key="um_bruto",
        help="Lo que se espera que rinda al año aquello en lo que se reinvierte el "
             "préstamo. Por defecto, el 17% de SuperReentel.",
    ) / 100
    _meses = c4.number_input(
        "Meses entre pagos de intereses", 1, 360, 120, 1, key="um_meses",
        help=("Cada cuánto se atienden los intereses. Lo que no se paga capitaliza "
              "sobre la propia deuda, así que la frecuencia de pago **cambia el tipo "
              "que se acaba asumiendo**.\n\n"
              "12 meses equivale al APY; pagar en continuo equivale al APR. Cualquier "
              "otra frecuencia cae entre medias o por encima: 120 meses sin pagar "
              "salen a un 23,6% anual con los tipos de hoy."),
    )

    r = _coste.resumen(apr, _meses, rentabilidad_bruta=_bruto,
                       tipo_marginal=_t, deducible=_deducible)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("💸 Coste real ANUAL del préstamo", f"{r['apy'] * 100:,.2f}%",
              f"APR publicado {r['apr'] * 100:,.2f}%", delta_color="off",
              help=("El APR es el tipo que publica el contrato; el APY es lo que se paga "
                    "de verdad en **un año**. Aave acumula el interés en cada bloque sobre "
                    "el saldo ya acumulado, así que el efectivo anual es e^APR − 1.\n\n"
                    "Es una tasa anual: **no cambia** al mover los años sin pagar. Lo que "
                    "cambia con el plazo es cuánto se acumula, y eso es el último "
                    "recuadro — que con 1 año da exactamente esta misma cifra."))
    k2.metric("🎯 Rentabilidad bruta necesaria", f"{r['equilibrio'] * 100:,.2f}%",
              (f"+{r['sobrecoste_fiscal'] * 100:,.2f} pp por fiscalidad"
               if r["sobrecoste_fiscal"] > 0.0001 else "sin recargo fiscal"),
              delta_color="off",
              help=("Lo que tiene que rendir la inversión SOLO PARA EMPATAR. Si los "
                    "intereses no se deducen, se pagan con dinero ya tributado mientras la "
                    "ganancia sí tributa, así que el umbral sube a APY ÷ (1 − tipo)."))
    k3.metric("📊 Margen neto anual", f"{r['margen'] * 100:+,.2f} pp",
              "por cada euro prestado", delta_color="off",
              help=("Lo que queda al año tras impuestos y tras el coste del préstamo. "
                    "En negativo, la operación apalancada destruye valor aunque la "
                    "inversión en sí sea rentable."))
    # En tasa anual como los otros tres, para poder compararlos de un vistazo; el
    # acumulado, que es la cifra grande y llamativa, va debajo en pequeño.
    k4.metric(f"⏳ Coste real pagando cada {_meses} m",
              f"{r['coste_anualizado'] * 100:,.2f}%",
              f"acumulado {r['coste_acumulado'] * 100:,.1f}% en {_meses} meses",
              delta_color="off",
              help=("El tipo anual que se acaba asumiendo con esa frecuencia de pago. Lo "
                    "que no se paga capitaliza, así que espaciar los pagos encarece el "
                    "préstamo:\n\n"
                    "· pagando en continuo → el APR\n"
                    "· cada 12 meses → el APY\n"
                    "· cada 120 meses → un 23,6% anual\n\n"
                    "Por eso el APR y el APY no son dos convenciones rivales: son el mismo "
                    "coste con dos frecuencias distintas, y el tipo real del inversor está "
                    "donde caiga su forma de operar."))

    if r["sale_a_cuenta"]:
        _holgura = (_bruto - r["equilibrio"]) * 100
        (st.success if _holgura >= 2 else st.warning)(
            f"Con un {_bruto * 100:,.1f}% bruto, la operación deja **{r['margen'] * 100:+,.2f} "
            f"puntos** netos al año — una holgura de {_holgura:,.2f} pp sobre el umbral."
            + ("" if _holgura >= 2 else
               " **El margen es estrecho**: una desviación pequeña en la rentabilidad "
               "real lo borra entero. Conviene contrastarlo con lo que están rindiendo "
               "de verdad los proyectos antes de plantearlo.")
        )
    else:
        st.error(
            f"Con un {_bruto * 100:,.1f}% bruto **la operación pierde dinero**: haría falta "
            f"un {r['equilibrio'] * 100:,.2f}% solo para empatar. El margen es de "
            f"{r['margen'] * 100:+,.2f} puntos al año por cada euro prestado."
        )

    # ── La curva del coste según cada cuánto se paga ─────────────────────────
    # Es el punto que resume todo lo anterior: el coste no es un número, es una
    # curva, y cada inversor se sitúa en un punto según cómo opere. Enfrentarla
    # a la rentabilidad esperada convierte la pregunta «¿me compensa?» en «¿por
    # dónde corto la línea?».
    _xs = list(range(1, max(int(_meses) + 1, 25)))
    _ys = [_coste.coste_anualizado(apr, m) * 100 for m in _xs]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=_xs, y=_ys, mode="lines", name="Coste real del préstamo",
        line=dict(color="#dc2626", width=3),
        hovertemplate="Pagando cada %{x} meses<br>coste %{y:.2f}% anual<extra></extra>",
    ))
    # La rentabilidad esperada y el umbral: donde la curva roja los cruza, deja
    # de compensar. Es la lectura de un vistazo que se buscaba.
    fig.add_hline(y=_bruto * 100, line=dict(color="#16a34a", width=2),
                  annotation_text=f"Rentabilidad bruta esperada {_bruto * 100:,.1f}%",
                  annotation_position="top left")
    if r["sobrecoste_fiscal"] > 0.0001:
        fig.add_hline(y=r["equilibrio"] * 100, line=dict(color="#ea580c", width=2, dash="dash"),
                      annotation_text=f"Umbral con fiscalidad {r['equilibrio'] * 100:,.2f}%",
                      annotation_position="bottom left")
    fig.add_hline(y=r["apr"] * 100, line=dict(color="#64748b", width=1, dash="dot"),
                  annotation_text=f"APR {r['apr'] * 100:,.2f}% (suelo, pagando en continuo)",
                  annotation_position="bottom right")
    # Dónde está el inversor según lo que haya elegido arriba.
    fig.add_trace(go.Scatter(
        x=[_meses], y=[r["coste_anualizado"] * 100], mode="markers+text",
        marker=dict(color="#dc2626", size=13, line=dict(color="#fff", width=2)),
        text=[f"  cada {_meses} m"], textposition="middle right",
        textfont=dict(color="#dc2626", size=12), showlegend=False,
        hovertemplate=f"Tu caso: {r['coste_anualizado'] * 100:.2f}% anual<extra></extra>",
    ))
    fig.update_layout(
        height=340, margin=dict(t=30, b=10, l=10, r=10), showlegend=False,
        xaxis_title="Meses entre pagos de intereses",
        yaxis_title="Coste efectivo anual (%)",
        hovermode="x unified",
    )
    st.markdown("**El coste no es un número, es una curva**", help=(
        "El eje horizontal es cada cuánto se atienden los intereses. Cuanto más se "
        "espacian, más capitaliza la deuda y más caro sale el préstamo en términos "
        "anuales.\n\n"
        "Donde la curva roja cruza la línea verde, el préstamo deja de dejar margen. "
        "Si hay recargo fiscal, la línea naranja marca el corte de verdad, que llega "
        "antes."))
    st.plotly_chart(fig, use_container_width=True)

    _corte = next((m for m, y in zip(_xs, _ys) if y / 100 >= r["equilibrio"]), None)
    if _corte:
        st.caption(
            f"Con una rentabilidad bruta del {_bruto * 100:,.1f}%, el préstamo deja de "
            f"compensar si se tarda **más de {_corte} meses** en atender los intereses. "
            f"A partir de ahí lo que cuesta la deuda supera lo que rinde lo comprado con ella."
        )
    else:
        st.caption(
            f"Con una rentabilidad bruta del {_bruto * 100:,.1f}%, el préstamo compensa "
            f"en todo el rango representado: ni esperando {_xs[-1]} meses el coste alcanza "
            f"el umbral del {r['equilibrio'] * 100:,.2f}%."
        )

    with st.expander("Cómo se calcula, y por qué el diferencial aparente engaña"):
        st.markdown(f"""
**1 · El APR no es lo que se paga.** La deuda variable capitaliza de forma continua,
así que el tipo efectivo es `e^APR − 1`. Con el {r['apr'] * 100:.2f}% actual, el coste
real es del **{r['apy'] * 100:.2f}%**.

**2 · La frecuencia de pago cambia el tipo.** Lo que no se paga capitaliza sobre la
deuda, así que pagando cada {_meses} meses el coste efectivo es del
**{r['coste_anualizado'] * 100:.2f}% anual**, no del {r['apr'] * 100:.2f}%. En total,
{r['coste_acumulado'] * 100:.1f}% del principal frente al {r['coste_lineal'] * 100:.1f}%
que daría multiplicar el APR por el plazo. Es la misma acumulación que determina cuándo
se liquida la posición, mirada como gasto en vez de como riesgo.

**3 · Los impuestos rompen la simetría.** Si los intereses no son deducibles, se pagan
con dinero ya tributado mientras la ganancia tributa entera. El umbral pasa de
`APY` a `APY ÷ (1 − tipo)` = **{r['equilibrio'] * 100:.2f}%**.

---

**Por qué importa.** Un {_bruto * 100:.0f}% frente a un préstamo al
{r['apr'] * 100:.2f}% parece un diferencial de {(_bruto - r['apr']) * 100:.1f} puntos.
Descontando la capitalización y la fiscalidad, el margen real es de
**{r['margen'] * 100:+.2f}**. La mayor parte del diferencial aparente no existe.

*Los supuestos fiscales los introduces tú; esta herramienta solo hace la aritmética.
El tratamiento aplicable a cada inversor lo confirma un asesor fiscal.*
        """)


st.title("📈 Simulador de rentabilidad por status")
st.caption(
    "Compara el patrimonio proyectado según el status del inversor y simula el efecto "
    "del apalancamiento. Herramienta de apoyo a la conversación comercial."
)

_foto = _snap.cargar()
_cfg = _sim.configuracion(_foto)
_html = _sim.html(_foto)

if not _html:
    st.error(
        "No se encuentra la calculadora en `data/simulador/calculadora.html`. "
        "Se genera con `python3 scripts/preparar_simulador.py <html original>`."
    )
    st.stop()

# Los supuestos que usa se declaran fuera del iframe, donde se leen sin tener
# que abrir el desplegable de apalancamiento.
st.caption(_sim.resumen_config(_cfg))

# Aviso de uso: proyecta rentabilidades a años vista e incluye apalancamiento,
# así que es material sensible mientras Legal no lo revise. Va FUERA del iframe
# a propósito: dentro se perdería en el desplazamiento y aquí se ve siempre.
st.warning(
    "**Simulación orientativa de uso interno.** No constituye asesoramiento financiero, "
    "ni una oferta o recomendación de inversión. Las proyecciones parten de supuestos "
    "editables y de rentabilidades históricas que no garantizan resultados futuros; el "
    "apalancamiento amplifica tanto la ganancia como el riesgo de liquidación. "
    "Pendiente de revisión por Legal y Cumplimiento Normativo antes de compartir "
    "cualquier resultado con un inversor."
)

_umbral_rentabilidad(_cfg.get("rlApr", 0.12))

st.markdown("---")

# Sin barra propia: el contenido se mide a sí mismo y estira el iframe (ver
# `_AUTOALTURA` en simulador_status), de modo que la única barra de
# desplazamiento es la de la página. La altura inicial es solo el punto de
# partida hasta que el script ajusta.
components.html(_html, height=1400, scrolling=False)

st.caption(
    "Calculadora mantenida por Jesús González · empotrada tal cual, con los supuestos de "
    "mercado sustituidos por los datos reales del pool. Para actualizarla a una versión "
    "nueva: `python3 scripts/preparar_simulador.py <html>`."
)
