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

    # Anchos repartidos según lo que ocupa cada etiqueta: la casilla de
    # deducibles necesita sitio o el texto se parte en tres líneas.
    c1, c2, c3, c4, c5 = st.columns([1.15, 1.15, 1.05, 0.65, 1.0])

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
    _plazo = c4.number_input(
        "Plazo (meses)", 1, 360, 60, 1, key="um_plazo",
        help="Cuánto tiempo se mantiene abierta la posición apalancada.",
    )
    _frec = c5.number_input(
        "Pagar intereses cada (meses)", 1, 360, 1, 1, key="um_frec",
        help=("Cada cuánto se saldan los intereses. Es lo que determina el tipo real: "
              "lo que se paga no capitaliza, lo que se deja correr sí.\n\n"
              "Pagando cada mes el coste se queda casi en el APR; una vez al año, en el "
              "APY; y si se pone igual o mayor que el plazo, equivale a no pagar nada "
              "hasta el final."),
    )

    r = _coste.resumen(apr, _frec, _plazo, rentabilidad_bruta=_bruto,
                       tipo_marginal=_t, deducible=_deducible)
    _total, _total_sin = r["coste_total"], r["coste_total_sin_pagar"]

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
              f"con el APY publicado sería {r['equilibrio_apy'] * 100:,.2f}%",
              delta_color="off",
              help=("Lo que tiene que rendir la inversión SOLO PARA EMPATAR, **en el "
                    "escenario configurado**: se calcula sobre el coste efectivo de pagar "
                    "cada {} mes{}, no sobre el APY publicado.\n\n"
                    "· Referencia de mercado (APY, pagando una vez al año): "
                    "**{:.2f}%**\n"
                    "· Tu escenario (pagando cada {} mes{}): **{:.2f}%**\n\n"
                    "Si los intereses no se deducen, se pagan con dinero ya tributado "
                    "mientras la ganancia sí tributa, y el umbral sube a coste ÷ (1 − tipo)."
                    ).format(_frec, "es" if _frec != 1 else "",
                             r["equilibrio_apy"] * 100,
                             _frec, "es" if _frec != 1 else "",
                             r["equilibrio"] * 100))
    k3.metric("📊 Margen neto anual", f"{r['margen'] * 100:+,.2f} pp",
              "por cada euro prestado", delta_color="off",
              help=("Lo que queda al año tras impuestos y tras el coste del préstamo. "
                    "En negativo, la operación apalancada destruye valor aunque la "
                    "inversión en sí sea rentable."))
    # En tasa anual como los otros tres, para poder compararlos de un vistazo; el
    # acumulado, que es la cifra grande y llamativa, va debajo en pequeño.
    k4.metric(f"⏳ Coste real pagando cada {_frec} m",
              f"{r['coste_anualizado'] * 100:,.2f}%",
              f"{_total * 100:,.0f}% del principal en {_plazo} meses",
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

    # ── Cómo evoluciona el coste con el tiempo ───────────────────────────────
    # El eje es TIEMPO TRANSCURRIDO, no la frecuencia de pago. La versión
    # anterior barría frecuencias y ponía meses en el eje, y se leía como una
    # línea temporal: parecía decir que pagando cada mes el coste se disparaba,
    # cuando es justo al revés.
    #
    # Con una frecuencia fija el coste anual es CONSTANTE —cada pago devuelve la
    # deuda al principal y corta la capitalización—, así que sale una recta
    # horizontal. La única línea que sube es la de no pagar nada: ahí el interés
    # se acumula sobre sí mismo y el tipo efectivo crece con el plazo.
    _xs = list(range(1, int(_plazo) + 1))

    def _necesaria(meses_frec: float) -> float:
        """Rentabilidad bruta que hace falta con esa frecuencia de pago."""
        return _coste.rentabilidad_de_equilibrio(
            _coste.coste_anualizado(apr, meses_frec), _t, _deducible)

    fig = go.Figure()
    for _n, _etq, _col in ((1, "Pagando cada mes", "#16a34a"),
                           (3, "Trimestral", "#3B82F6"),
                           (12, "Anual", "#8b5cf6")):
        if _n > _plazo:
            continue
        _y = _necesaria(_n) * 100
        fig.add_trace(go.Scatter(
            x=_xs, y=[_y] * len(_xs), mode="lines", name=_etq,
            line=dict(color=_col, width=2), hovertemplate="%{y:.2f}%<extra></extra>",
        ))
    fig.add_trace(go.Scatter(
        x=_xs, y=[_necesaria(m) * 100 for m in _xs],
        mode="lines", name="Sin pagar hasta el final",
        line=dict(color="#dc2626", width=3), hovertemplate="%{y:.2f}%<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=_xs, y=[_bruto * 100] * len(_xs), mode="lines",
        name="Rentabilidad bruta esperada",
        line=dict(color="#0f766e", width=2.5, dash="dash"),
        hovertemplate="%{y:.2f}%<extra></extra>",
    ))
    fig.update_layout(
        height=400, margin=dict(t=60, b=20, l=10, r=10),
        xaxis_title="Meses transcurridos desde que se pide el préstamo",
        yaxis_title="Rentabilidad bruta anual necesaria (%)",
        # La leyenda va ARRIBA. Debajo chocaba con el título del eje horizontal
        # hiciera lo que hiciera con el margen, porque ambos compiten por la
        # misma banda.
        legend=dict(orientation="h", y=1.1, yanchor="bottom", x=0),
        # Con el ratón en cualquier punto se ven TODAS las líneas de esa
        # vertical, con su marcador: comparar exigía acertar encima de cada
        # línea una por una.
        hovermode="x unified",
        hoverlabel=dict(bgcolor="rgba(255,255,255,.96)", font_size=12),
    )
    fig.update_xaxes(showspikes=True, spikemode="across", spikethickness=1,
                     spikedash="dot", spikecolor="#94a3b8")
    fig.update_traces(mode="lines+markers", marker=dict(size=1),
                      hoverlabel=dict(namelength=-1))
    st.markdown("**Qué rentabilidad hace falta según cada cuánto se pague**", help=(
        "Todo en el mismo eje —rentabilidad bruta anual— para que se pueda comparar "
        "directamente con lo que se espera ganar. Cada línea es lo que habría que rendir "
        "para no perder dinero con esa forma de pagar.\n\n"
        "Las de frecuencia fija son **planas**: cada pago devuelve la deuda al principal y "
        "corta la capitalización, así que la exigencia no crece con el tiempo. La única "
        "que sube es la de no pagar nada, y lo hace de forma **convexa** —en cinco años se "
        "desvía menos de 0,3 pp de una recta, por eso lo parece; a veinte se dispara.\n\n"
        "Donde una curva cruza la línea verde discontinua, esa forma de operar deja de "
        "compensar."))
    st.plotly_chart(fig, use_container_width=True)

    # El corte, en el mismo eje que el gráfico.
    _corte = next((m for m in _xs if _necesaria(m) >= _bruto), None)
    _msg = (f"Pagando **cada {_frec} mes{'es' if _frec != 1 else ''}** haría falta un "
            f"**{r['equilibrio'] * 100:,.2f}%** durante todo el plazo, frente al "
            f"{_bruto * 100:,.1f}% esperado. Coste total: {_total * 100:,.0f}% del "
            f"principal en {_plazo} meses.")
    if _corte:
        st.caption(
            _msg + f" Dejando de pagar, la exigencia sube y supera el {_bruto * 100:,.1f}% "
            f"a los **{_corte} meses**: a partir de ahí el préstamo cuesta más de lo que "
            f"rinde lo comprado con él, y en {_plazo} meses habría acumulado "
            f"{_total_sin * 100:,.0f}% del principal."
        )
    else:
        st.caption(
            _msg + f" Ni dejando de pagar durante los {_plazo} meses completos la exigencia "
            f"alcanza el {_bruto * 100:,.1f}% esperado, aunque el coste acumulado sería del "
            f"{_total_sin * 100:,.0f}% del principal frente al {_total * 100:,.0f}% pagando "
            f"con la frecuencia elegida."
        )

    with st.expander("Cómo se calcula, y por qué el diferencial aparente engaña"):
        st.markdown(f"""
**1 · El APR no es lo que se paga.** La deuda variable capitaliza de forma continua,
así que el tipo efectivo es `e^APR − 1`. Con el {r['apr'] * 100:.2f}% actual, el coste
real es del **{r['apy'] * 100:.2f}%**.

**2 · La frecuencia de pago cambia el tipo.** Lo que se paga no capitaliza; lo que se
deja correr, sí. Pagando cada {_frec} mes{'es' if _frec != 1 else ''} el coste efectivo
es del **{r['coste_anualizado'] * 100:.2f}% anual** —{_total * 100:.0f}% del principal en
{_plazo} meses—, mientras que no pagando nada sube al
{_coste.coste_anualizado(apr, _plazo) * 100:.2f}% anual y {_total_sin * 100:.0f}% en total.
Es la misma acumulación que determina cuándo se liquida la posición, mirada como gasto en
vez de como riesgo.

**3 · Los impuestos rompen la simetría.** Si los intereses no son deducibles, se pagan
con dinero ya tributado mientras la ganancia tributa entera. El umbral pasa de
`coste` a `coste ÷ (1 − tipo)` = **{r['equilibrio'] * 100:.2f}%** en el escenario
configurado ({r['equilibrio_apy'] * 100:.2f}% si se tomara el APY publicado como
referencia).

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
