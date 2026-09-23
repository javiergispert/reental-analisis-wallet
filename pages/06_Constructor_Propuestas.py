"""
Constructor de propuestas — Reental Wealth.

Genera el dossier comercial que hasta ahora se hacía con una plantilla de
Google Sheets y un Ctrl+P. Lo que cambia no es el documento —el guion es el
mismo que el equipo ya usa— sino de dónde salen los números:

    proyectos        → el maestro de inmuebles, leído por nombre de columna
    precio del RNT   → el pool RNT/USDT, en vivo
    tipo de cambio   → referencia diaria del Banco Central Europeo
    track record     → los proyectos ya cerrados del propio maestro

Dos modos: proponer una cartera a un inversor nuevo, o partir de la cartera
real de una wallet para proponer una ampliación. El segundo solo lo puede
hacer esta herramienta.
"""
from __future__ import annotations

import os
from datetime import date

import streamlit as st
from dotenv import load_dotenv

import divisas as _fx
import maestro
import pool_rnt
import propuesta
import propuesta_pdf
import recarga as _recarga
import ui_kpi
from ui_kpi import kpi_card

load_dotenv()
_recarga.refrescar("maestro", "propuesta", "propuesta_pdf", "pool_rnt", "divisas")

API_KEY = os.getenv("ETHERSCAN_API_KEY", "")


st.title("📑 Constructor de propuestas")
st.caption(
    "Dossier de simulación de cartera para un inversor. Los datos de cada proyecto salen del "
    "maestro de inmuebles, el precio del RNT del pool RNT/USDT y el tipo de cambio del Banco "
    "Central Europeo: no hay ningún supuesto tecleado a mano salvo los que elijas abajo."
)
ui_kpi.inyectar_css()


# ── Datos de partida ─────────────────────────────────────────────────────────

@st.cache_data(show_spinner="Leyendo el maestro de inmuebles…", ttl=3600)
def _proyectos(_dia: str) -> list:
    return maestro.proyectos()


@st.cache_data(show_spinner=False, ttl=3600)
def _eurusd(_dia: str):
    """Dólares por euro, referencia del BCE. None si no se pudo obtener."""
    tabla = _fx.serie("EUR", "2024-01-01", _dia)
    t = _fx.tipo_en(_dia, tabla)
    return (1.0 / t) if t else None


@st.cache_data(show_spinner=False, ttl=1800)
def _precio_rnt(_dia: str):
    return pool_rnt.precio_actual(API_KEY) if API_KEY else None


_hoy = date.today().isoformat()
try:
    PROYECTOS = _proyectos(_hoy)
except Exception as e:      # noqa: BLE001
    st.error(f"No se pudo leer el maestro de inmuebles: {e}")
    st.stop()

if not PROYECTOS:
    st.error("El maestro de inmuebles está vacío o no es accesible. Revisa `GSHEET_CSV_URL`.")
    st.stop()

ABIERTOS = [p for p in PROYECTOS if p["abierto"]]
POR_ID = {p["label"]: p for p in PROYECTOS}


# ── 1. Inversor y modo ───────────────────────────────────────────────────────

st.markdown("##### 1 · Inversor")
c1, c2, c3 = st.columns([2, 1.2, 1])
titular = c1.text_input("Titular", placeholder="Nombre y apellidos del inversor")
estatus = c2.selectbox("Estatus propuesto", [n for n, _ in maestro.ESTATUS], index=2)
modo = c3.selectbox("Punto de partida", ["Inversor nuevo", "Ampliación sobre una wallet"])

# La ampliación se apoya en lo que ya analizó el Analizador de Wallets: lo deja
# en sesión, así que no hace falta volver a consultar la cadena.
cartera_actual, rnt_actual, alias_wallet = {}, 0.0, ""
if modo == "Ampliación sobre una wallet":
    token_data = st.session_state.get("token_data") or {}
    if not token_data:
        st.warning(
            "Para partir de una cartera real, analiza antes la wallet en **Analizador de Wallets**. "
            "Lo que encuentre allí aparecerá aquí sin volver a consultar la cadena."
        )
    else:
        alias_wallet = ", ".join(a or d[:8] for d, a in st.session_state.get("wallets_analyzed", []))
        for d in token_data.values():
            info, saldo = d["info"], round(d.get("balance", 0.0), 6)
            if saldo > 1e-6 and not info.get("is_aave"):
                cartera_actual[info.get("label", "")] = saldo
        pos = st.session_state.get("posicion_rnt") or {}
        rnt_actual = float(pos.get("total") or 0.0)
        _detalle_rnt = ""
        if rnt_actual:
            _detalle_rnt = (f" · **{rnt_actual:,.0f} RNT** en cartera "
                            f"({pos.get('liquido', 0):,.0f} líquidos + "
                            f"{pos.get('staking', 0):,.0f} en staking)")
        st.success(
            f"Cartera actual de **{alias_wallet}**: {len(cartera_actual)} proyectos con saldo"
            f"{_detalle_rnt}. Se parte de ella: lo que propongas se compara con lo que ya tiene."
        )
        if not pos:
            st.caption(
                "⚠️ No consta la posición de RNT de esta wallet. Vuelve a analizarla en "
                "**Analizador de Wallets** para que el estatus que ya tenga se descuente del coste."
            )


# ── 2. Supuestos de mercado ──────────────────────────────────────────────────

st.markdown("##### 2 · Supuestos de mercado")
fx_auto = _eurusd(_hoy)
rnt_auto = _precio_rnt(_hoy)

c1, c2, c3, c4 = st.columns(4)
eurusd = c1.number_input(
    "Tipo de cambio (USD por 1 €)", min_value=0.5, max_value=2.5,
    value=float(fx_auto or 1.10), step=0.0001, format="%.4f",
    help="Referencia diaria del BCE. Se puede forzar otro valor si el acuerdo con el inversor lo exige.")
precio_rnt = c2.number_input(
    "Precio del RNT (USDT)", min_value=0.0001, max_value=100.0,
    value=float(rnt_auto or 0.31), step=0.0001, format="%.4f",
    help="Leído del pool RNT/USDT, que es donde se forma el precio.")
tasa_staking = c3.number_input("Rendimiento del staking de RNT (% anual)",
                               min_value=0.0, max_value=50.0, value=2.5, step=0.25) / 100.0
c4.markdown(kpi_card("🔗", "Origen de los supuestos",
                     "En vivo" if (fx_auto and rnt_auto) else "Parcial",
                     sublabel=("BCE + pool RNT/USDT" if (fx_auto and rnt_auto)
                               else "alguno no se pudo leer: revisa los valores")),
            unsafe_allow_html=True)
if not fx_auto:
    st.caption("⚠️ No se pudo obtener el tipo de cambio del BCE; se usa un valor por defecto editable.")
if not rnt_auto:
    st.caption("⚠️ No se pudo leer el precio del RNT del pool; se usa un valor por defecto editable.")


# ── 3. Cartera propuesta ─────────────────────────────────────────────────────

st.markdown("##### 3 · Cartera propuesta")
st.caption(
    f"Solo se ofrecen los **{len(ABIERTOS)} proyectos abiertos** del maestro "
    "(en explotación, en reforma, en construcción, financiándose o en prelanzamiento). "
    "Los cerrados no se pueden suscribir, así que no aparecen."
)

# Los que el inversor ya tiene se marcan en la propia etiqueta: en una lista de
# 75 proyectos, saber cuáles son «los suyos» de un vistazo es la diferencia
# entre construir sobre su cartera y empezar de cero sin darse cuenta.
def _etiqueta(p: dict) -> str:
    marca = "📁 EN CARTERA · " if p["label"] in cartera_actual else ""
    tiene = f" — tiene {cartera_actual[p['label']]:,.0f}" if p["label"] in cartera_actual else ""
    return f"{marca}{p['label']} · {p['nombre']} ({p['ubicacion']}, {p['estado'].lower()}){tiene}"


# Primero los que ya están en cartera, para no tener que buscarlos.
_orden = sorted(ABIERTOS, key=lambda x: (x["label"] not in cartera_actual, x["label"]))
etiquetas = {_etiqueta(p): p["label"] for p in _orden}
previa = [k for k, v in etiquetas.items() if v in cartera_actual]

# Un proyecto de la cartera puede estar CERRADO y por tanto no ser suscribible,
# pero sigue formando parte del patrimonio: se avisa en vez de ignorarlo.
_fuera = [lbl for lbl in cartera_actual if lbl not in {p["label"] for p in ABIERTOS}]
if _fuera:
    st.caption(
        f"ℹ️ {len(_fuera)} proyecto(s) de su cartera no admiten suscripción hoy "
        f"({', '.join(sorted(_fuera)[:6])}{'…' if len(_fuera) > 6 else ''}): "
        "están cerrados o fuera de periodo, así que no aparecen en la lista."
    )

elegidos = st.multiselect("Proyectos", list(etiquetas), default=previa,
                          help="Los marcados con 📁 ya están en la cartera del inversor.")

seleccion = []
if elegidos:
    cols = st.columns(min(4, len(elegidos)))
    for i, etiqueta in enumerate(elegidos):
        lbl = etiquetas[etiqueta]
        p = POR_ID[lbl]
        pe = p.get("precio_emision") or 0.0
        actuales = cartera_actual.get(lbl)
        col = cols[i % len(cols)]
        rotulo = f"📁 {lbl} · tokens" if actuales is not None else f"{lbl} · tokens"
        tokens = col.number_input(
            rotulo, min_value=0.0, step=1.0,
            value=float(actuales if actuales is not None else 10.0),
            key=f"tok_{lbl}",
            help=f"{p['nombre']} — {pe:,.0f} {p['divisa']} por token"
                 + (f" · ya tiene {actuales:,.3f}" if actuales is not None else ""))
        if actuales is not None:
            d = tokens - actuales
            if abs(d) < 1e-9:
                col.caption(f"Tiene {actuales:,.0f} · **sin cambios**")
            elif d > 0:
                col.caption(f"Tiene {actuales:,.0f} · :green[**compra {d:,.0f}**]")
            else:
                col.caption(f"Tiene {actuales:,.0f} · :red[**vende {abs(d):,.0f}**]")
        seleccion.append((p, tokens, actuales) if modo == "Ampliación sobre una wallet"
                         else (p, tokens))

if not seleccion or all(t <= 0 for _, t in seleccion):
    st.info("Elige al menos un proyecto y asígnale tokens para ver la propuesta.")
    st.stop()


# ── 4. Estatus y reinversión ─────────────────────────────────────────────────

st.markdown("##### 4 · Estatus y reinversión")
st.caption(
    "Los RNT que hay que adquirir para cada categoría y la tasa a la que se supone reinvertido "
    "lo que se va cobrando. Son supuestos comerciales, no medidos: quedan escritos en el documento."
)
cols = st.columns(len(maestro.ESTATUS) * 2)
rnts, tasas = {}, {}
for i, (nombre, _suf) in enumerate(maestro.ESTATUS):
    por_defecto = {"Reentel": 0, "ReentelPro": 14000, "SuperReentel": 28000}[nombre]
    rnts[nombre] = cols[i * 2].number_input(f"RNT · {nombre}", min_value=0, step=500,
                                            value=por_defecto, key=f"rnt_{nombre}")
    tasas[nombre] = cols[i * 2 + 1].number_input(
        f"Reinversión · {nombre} (%)", min_value=0.0, max_value=60.0,
        value={"Reentel": 11.0, "ReentelPro": 13.0, "SuperReentel": 16.0}[nombre],
        step=0.5, key=f"tasa_{nombre}") / 100.0

_actual = propuesta.estatus_actual(rnt_actual, rnts) if rnt_actual else None
costes = {n: propuesta.coste_estatus(r, precio_rnt, eurusd, rnt_actual)
          for n, r in rnts.items()}
if rnt_actual:
    _c = costes[estatus]
    if _c["rnts"] <= 0:
        st.success(
            f"Este inversor ya tiene **{rnt_actual:,.0f} RNT**, así que **ya es {_actual}**: "
            f"la propuesta no le cobra ninguna adquisición de estatus."
        )
    else:
        st.info(
            f"Este inversor ya es **{_actual}** con {rnt_actual:,.0f} RNT. Para llegar a "
            f"**{estatus}** solo necesita comprar **{_c['rnts']:,.0f} RNT** más, no los "
            f"{_c['rnts_umbral']:,.0f} del umbral: la propuesta cobra únicamente la diferencia."
        )
cartera = propuesta.construir(seleccion, eurusd)
escenarios = propuesta.escenarios(
    cartera, tasas, {n: c["eur"] for n, c in costes.items()}, tasa_staking)
track = propuesta.track_record(PROYECTOS)


# ── 5. Vista previa ──────────────────────────────────────────────────────────

st.markdown("---")
st.markdown("##### 5 · Así queda la propuesta")

r_prop = cartera["rentabilidad"][estatus]
coste_ep = costes[estatus]
total_eur = (cartera["eur"] or 0) + (coste_ep["eur"] or 0)

k = st.columns(5)
k[0].markdown(kpi_card("🏠", "Inmuebles", str(cartera["n"]),
                       sublabel=f"{cartera['tokens']:,.0f} tokens"), unsafe_allow_html=True)
k[1].markdown(kpi_card("💶", "En inmuebles", f"{cartera['eur']:,.2f} €",
                       sublabel=f"${cartera['usd']:,.2f}"), unsafe_allow_html=True)
_sub_estatus = (f"{coste_ep['rnts']:,.0f} RNT · {estatus}" if coste_ep["rnts"] > 0
                else f"ya es {_actual or estatus}")
k[2].markdown(kpi_card("⭐", "Coste del estatus", f"{coste_ep['eur'] or 0:,.2f} €",
                       sublabel=_sub_estatus),
              unsafe_allow_html=True)
k[3].markdown(kpi_card("💰", "Capital total", f"{total_eur:,.2f} €",
                       sublabel="inmuebles + estatus"), unsafe_allow_html=True)
k[4].markdown(kpi_card("📈", "Rent. anualizada", f"{r_prop['anual'] * 100:,.2f} %",
                       sublabel=f"{estatus} · base {cartera['rentabilidad']['Reentel']['anual'] * 100:,.2f} %"),
              unsafe_allow_html=True)

st.caption(
    "Las rentabilidades se ponderan por **importe invertido** en cada proyecto, no por número de "
    "tokens: un token de 100 € y otro de 100 $ no son la misma inversión."
)

# El salto de estatus, con las dos lecturas. Publicar solo la primera hace que
# parezca mayor de lo que es, porque deja fuera lo que cuesta el estatus.
ultimo = escenarios[0]["puntos"][-1]["meses"]
filas = []
for fila in escenarios:
    p = fila["puntos"][-1]
    filas.append({
        "Estatus": fila["estatus"],
        f"Ganancia a {ultimo} meses": f"{p['ganancia_con_staking']:,.2f} €",
        "Rent. sobre la cartera": f"{p['sobre_cartera'] * 100:,.2f} %",
        "Rent. sobre el capital total": f"{p['sobre_total'] * 100:,.2f} %",
        "Coste del estatus": f"{fila['coste_estatus']:,.2f} €",
    })
st.dataframe(filas, hide_index=True, use_container_width=True)
st.caption(
    "**Sobre la cartera** compara estatus entre sí; **sobre el capital total** incluye lo que "
    "cuesta adquirir el estatus, que es lo que de verdad desembolsa el inversor. El RNT comprado "
    "no se consume —se conserva y genera rendimiento en staking, ya contado—, por eso se dan las dos."
)

if track.get("n"):
    st.caption(
        f"El documento incluye el historial de los **{track['n']} proyectos ya cerrados**: "
        f"{track['pct_cumplieron']:.0f} % cumplieron o superaron la rentabilidad estimada y "
        f"{track['pct_en_plazo']:.0f} % cerraron en plazo o antes."
    )

borrador = st.checkbox(
    "Marcar como borrador interno (pendiente de Legal y Compliance)", value=True,
    help="Mientras no haya visto bueno, el documento sale con marca de agua para que no circule "
         "como material comercial si se reenvía por error.")


@st.fragment
def _descarga(datos: dict, nombre: str) -> None:
    """En un fragmento porque pulsar descargar reejecuta el script entero, y
    aquí eso significa releer el maestro y rehacer el PDF por nada."""
    if st.button("🧾 Generar propuesta en PDF", type="primary", use_container_width=True):
        with st.spinner("Componiendo el documento…"):
            st.session_state["_pdf_propuesta"] = propuesta_pdf.construir(datos)
    pdf = st.session_state.get("_pdf_propuesta")
    if pdf:
        st.download_button("⬇️ Descargar propuesta (PDF)", data=pdf, file_name=nombre,
                           mime="application/pdf", type="primary", use_container_width=True)


_slug = "".join(c if c.isalnum() else "_" for c in (titular or "propuesta")).strip("_")[:40]
_descarga(
    {"titular": titular, "estatus": estatus, "cartera": cartera, "escenarios": escenarios,
     "track": track, "precio_rnt": precio_rnt, "eurusd": eurusd,
     "coste_estatus": coste_ep, "tasa_staking": tasa_staking, "borrador": borrador,
     "fecha": date.today()},
    f"propuesta_reental_{_slug or 'sin_nombre'}_{date.today():%Y%m%d}.pdf")
