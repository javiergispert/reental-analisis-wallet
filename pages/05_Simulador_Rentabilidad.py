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
    _anos = c4.number_input("Horizonte (años)", 1, 30, 10, 1, key="um_anos",
                            help="Solo afecta al coste acumulado si no se atienden los "
                                 "intereses; el umbral anual no depende del plazo.")

    r = _coste.resumen(apr, _anos, rentabilidad_bruta=_bruto,
                       tipo_marginal=_t, deducible=_deducible)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("💸 Coste real del préstamo", f"{r['apy'] * 100:,.2f}%",
              f"APR publicado {r['apr'] * 100:,.2f}%", delta_color="off",
              help=("El APR es el tipo que publica el contrato; el APY es lo que se paga. "
                    "Aave acumula el interés en cada bloque sobre el saldo ya acumulado, "
                    "así que el efectivo anual es e^APR − 1."))
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
    k4.metric(f"⏳ Coste a {_anos} años sin pagar",
              f"{r['coste_acumulado'] * 100:,.0f}%",
              f"la cuenta lineal daría {r['coste_lineal'] * 100:,.0f}%", delta_color="off",
              help=("Si no se atienden los intereses, la deuda capitaliza sobre sí misma. "
                    "La diferencia con multiplicar el APR por los años es pequeña el "
                    "primer año y enorme a partir del quinto."))

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

    with st.expander("Cómo se calcula, y por qué el diferencial aparente engaña"):
        st.markdown(f"""
**1 · El APR no es lo que se paga.** La deuda variable capitaliza de forma continua,
así que el tipo efectivo es `e^APR − 1`. Con el {r['apr'] * 100:.2f}% actual, el coste
real es del **{r['apy'] * 100:.2f}%**.

**2 · Sin atender los intereses, la deuda crece sola.** A {_anos} años el coste no es
`APR × años` = {r['coste_lineal'] * 100:.0f}%, sino **{r['coste_acumulado'] * 100:.0f}%**
del principal. Es la misma acumulación que determina cuándo se liquida la posición,
mirada como gasto en vez de como riesgo.

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
