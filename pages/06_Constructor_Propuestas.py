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
import otc_inventario as _inv
import otc_storage as _store
import pool_rnt
import propuesta
import propuesta_pdf
import recarga as _recarga
import ui_kpi
from ui_kpi import kpi_card

load_dotenv()
_recarga.refrescar("maestro", "propuesta", "propuesta_pdf", "pool_rnt", "divisas",
                   "otc_inventario", "otc_storage")

API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
OTC_WALLET = os.getenv("OTC_WALLET", "0xce0719ec1bda336ba069c6961ad167767829301a").lower()
TAB_RESERVAS, TAB_OFERTAS, TAB_PRECIOS = "Reservas", "Ofertas", "precios_otc"


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


@st.cache_data(show_spinner="Consultando el inventario OTC…", ttl=900)
def _catalogo_otc(_dia: str, _hora: int) -> dict:
    """Lo que hoy se puede comprometer, de stock propio y de ofertas de
    terceros. Es el mismo cálculo que usa la gestión OTC: el módulo es común
    para que las dos páginas no lleguen a cifras distintas."""
    if not API_KEY:
        return {}
    try:
        por_addr = {p["address"]: {"nombre": p["nombre"], "id": p["label"],
                                   "token_address": p["address"], "divisa": p["divisa"],
                                   "precio_emision": p.get("precio_emision") or 0,
                                   "ubicacion": p.get("ubicacion", ""),
                                   "estado": p.get("estado", ""), "fecha_fin": None,
                                   "tipo_renta": p.get("tipologia_dividendo", "")}
                    for p in PROYECTOS}
        por_id = {p["label"].lower(): por_addr[p["address"]] for p in PROYECTOS}
        balances, _envios, _ts = _inv.balances_de_wallet(OTC_WALLET, API_KEY, por_addr, por_id)
        reservas = _store.read_list(TAB_RESERVAS)
        ofertas = _store.read_list(TAB_OFERTAS)
        return _inv.catalogo(balances, reservas, ofertas, API_KEY, _inv.saldo_en_wallet)
    except Exception:       # noqa: BLE001 — sin inventario la propuesta se hace igual
        return {}


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

from datetime import datetime as _dt          # noqa: E402 — solo para la clave de caché
CATALOGO = _catalogo_otc(_hoy, _dt.utcnow().hour)
OTC_POR_ID = {v["id"]: {**v, "address": a} for a, v in CATALOGO.items()}


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
    otc = OTC_POR_ID.get(p["label"])
    stock = f" · 🏷️ {otc['total']:,.0f} en OTC" if otc else ""
    return (f"{marca}{p['label']} · {p['nombre']} "
            f"({p['ubicacion']}, {p['estado'].lower()}){tiene}{stock}")


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

# El inventario OTC se ofrece aparte porque responde a otra pregunta: no «qué
# le vendo» sino «qué puedo entregar hoy sin esperar a una emisión nueva».
if CATALOGO:
    _tot_otc = sum(v["total"] for v in CATALOGO.values())
    with st.expander(f"🏷️ Disponible ahora en OTC interno — "
                     f"{len(CATALOGO)} proyectos, {_tot_otc:,.0f} tokens", expanded=False):
        st.caption(
            "Tokens que se pueden comprometer hoy mismo: stock propio de Reental más las ofertas "
            "vivas de otros inversores, ya descontado lo reservado. Es el mismo cálculo que usa "
            "**OTC interno Reental**."
        )
        _filas_otc = []
        for lbl, v in sorted(OTC_POR_ID.items(), key=lambda kv: -kv[1]["total"]):
            terceros = v.get("terceros") or []
            _filas_otc.append({
                "Proyecto": f"{lbl} · {v['nombre']}",
                "Stock de Reental": round(v["reental"], 3),
                "Ofertas de terceros": round(sum(t["disponible"] for t in terceros), 3),
                "Total disponible": round(v["total"], 3),
                "Precio de oferta": (" · ".join(f"{t['precio']:,.2f} {t['divisa']}" for t in terceros)
                                     or "—"),
            })
        st.dataframe(_filas_otc, hide_index=True, use_container_width=True)

        _solo_otc = st.multiselect(
            "Añadir a la propuesta desde el inventario OTC",
            [f"{lbl} · {v['nombre']} — {v['total']:,.0f} disponibles"
             for lbl, v in sorted(OTC_POR_ID.items())],
            key="otc_pick",
            help="Se añaden a la selección de abajo con la cantidad máxima disponible.")
        if _solo_otc and st.button("➕ Añadir los seleccionados", key="otc_add"):
            for etq in _solo_otc:
                lbl = etq.split(" · ")[0]
                st.session_state[f"tok_{lbl}"] = float(OTC_POR_ID[lbl]["total"])
                st.session_state.setdefault("_otc_forzados", [])
                if lbl not in st.session_state["_otc_forzados"]:
                    st.session_state["_otc_forzados"].append(lbl)
            st.rerun()

_forzados = st.session_state.get("_otc_forzados") or []
previa += [k for k, v in etiquetas.items() if v in _forzados and k not in previa]
elegidos = st.multiselect(
    "Proyectos", list(etiquetas), default=previa,
    help="📁 = ya en la cartera del inversor · 🏷️ = hay stock disponible en OTC interno.")

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
        # Lo que hay en OTC se puede entregar hoy; por encima de eso hace falta
        # emisión nueva o que aparezca otra oferta. No se bloquea —una propuesta
        # legítima puede ir al mercado primario— pero se dice.
        otc = OTC_POR_ID.get(lbl)
        a_comprar = tokens - (actuales or 0.0)
        if otc:
            if a_comprar > otc["total"] + 1e-9:
                col.caption(f":orange[🏷️ En OTC solo hay {otc['total']:,.0f}: "
                            f"faltan {a_comprar - otc['total']:,.0f}]")
            elif a_comprar > 0:
                col.caption(f":green[🏷️ {otc['total']:,.0f} disponibles en OTC]")
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


def _reservables(lineas: list) -> list:
    """Lo de la propuesta que se puede comprometer hoy en OTC.

    Solo cuentan las COMPRAS: si la propuesta reduce una posición no hay nada
    que reservar. Cada línea se reparte primero contra el stock propio y
    después contra las ofertas de terceros, que es el orden que sigue la
    gestión OTC.
    """
    salida = []
    for l in lineas:
        lbl = l["proyecto"]["label"]
        otc = OTC_POR_ID.get(lbl)
        if not otc:
            continue
        pendiente = l["tokens"] - (l.get("tokens_actuales") or 0.0)
        if pendiente <= 0.001:
            continue
        if otc["reental"] > 0.001:
            n = min(pendiente, otc["reental"])
            salida.append({"proyecto": l["proyecto"], "address": otc["address"],
                           "tipo_origen": "reental", "oferta_id": None,
                           "origen": "Stock de Reental", "n_tokens": round(n, 3)})
            pendiente -= n
        for t in otc.get("terceros", []):
            if pendiente <= 0.001:
                break
            n = min(pendiente, t["disponible"])
            salida.append({"proyecto": l["proyecto"], "address": otc["address"],
                           "tipo_origen": "tercero", "oferta_id": t["oferta_id"],
                           "origen": f"Oferta de {t['inversor']}", "n_tokens": round(n, 3),
                           "precio_oferta": t["precio"], "divisa_oferta": t["divisa"]})
            pendiente -= n
    return salida


def _precio_minimo(address: str, proy: dict) -> float:
    """El mínimo que la gestión OTC admite para ese proyecto. Se lee de la
    misma tabla, no se replica la regla."""
    try:
        precios = _store.read_dict(TAB_PRECIOS)
    except Exception:       # noqa: BLE001
        precios = {}
    return float((precios.get(address) or {}).get("precio_otc")
                 or proy.get("precio_emision") or 0.0)


@st.fragment
def _descarga(datos: dict, nombre: str, reservables: list) -> None:
    """Generar el documento y, si procede, dejar anotadas las reservas.

    Va en un fragmento porque pulsar descargar reejecuta el script entero, y
    aquí eso significa releer el maestro, el inventario OTC y rehacer el PDF
    para nada.
    """
    pendiente = st.session_state.get("_confirmar_reservas")

    if not pendiente:
        if st.button("🧾 Generar propuesta en PDF", type="primary", use_container_width=True):
            # Si hay algo comprometible, se pregunta ANTES de componer: es el
            # momento en que el asesor tiene delante lo que acaba de acordar.
            if reservables:
                st.session_state["_confirmar_reservas"] = True
                st.rerun(scope="fragment")
            else:
                with st.spinner("Componiendo el documento…"):
                    st.session_state["_pdf_propuesta"] = propuesta_pdf.construir(datos)

    if pendiente:
        st.markdown("###### 🔖 ¿Reservamos estos tokens en OTC interno?")
        st.caption(
            "Lo que la propuesta compra y hoy está disponible en OTC. Reservar lo deja apartado "
            "para este inversor en **OTC interno Reental**; si no, la propuesta se genera igual y "
            "no se compromete nada."
        )
        c1, c2 = st.columns(2)
        comercial = c1.text_input("Comercial *", value=st.session_state.get("_com_prop", ""),
                                  key="_com_prop", placeholder="Quién cierra la operación")
        wallet_def = ""
        if st.session_state.get("wallets_analyzed"):
            wallet_def = st.session_state["wallets_analyzed"][0][0]
        wallet_inv = c2.text_input("Wallet del inversor *", value=wallet_def,
                                   key="_wallet_prop", placeholder="0x…")

        lineas_res = []
        for i, r in enumerate(reservables):
            p = r["proyecto"]
            minimo = _precio_minimo(r["address"], p)
            ref = r.get("precio_oferta") or minimo
            cc = st.columns([3, 1.2, 1.2])
            cc[0].markdown(
                f"**{p['label']} · {p['nombre']}** — {r['n_tokens']:,.3f} tokens · {r['origen']}")
            precio = cc[1].number_input(
                f"Precio ({r.get('divisa_oferta') or p['divisa']})", min_value=0.0,
                value=float(ref), step=0.01, format="%.2f", key=f"_pr_{i}")
            cc[2].markdown(f"<div style='padding-top:1.9rem;color:#64748b;font-size:.8rem'>"
                           f"mínimo {minimo:,.2f}</div>", unsafe_allow_html=True)
            lineas_res.append({**r, "precio": precio, "minimo": minimo})

        b1, b2 = st.columns(2)
        if b1.button("📄 Generar solo el PDF, sin reservar", use_container_width=True):
            with st.spinner("Componiendo el documento…"):
                st.session_state["_pdf_propuesta"] = propuesta_pdf.construir(datos)
            st.session_state["_confirmar_reservas"] = False
            st.rerun(scope="fragment")

        if b2.button("🔖 Reservar y generar el PDF", type="primary", use_container_width=True):
            errores = []
            if not comercial.strip():
                errores.append("El campo Comercial es obligatorio.")
            w = (wallet_inv or "").strip().lower()
            if not (w.startswith("0x") and len(w) == 42):
                errores.append("La wallet del inversor debe ser una dirección válida (0x… 42 caracteres).")
            if not (datos.get("titular") or "").strip():
                errores.append("Falta el titular de la propuesta.")
            for r in lineas_res:
                if r["minimo"] and r["precio"] < r["minimo"] - 1e-9:
                    errores.append(f"{r['proyecto']['label']}: el precio {r['precio']:,.2f} está por "
                                   f"debajo del mínimo OTC de {r['minimo']:,.2f}.")
            for e in errores:
                st.error(e)
            if not errores:
                ok, fallo = _guardar_reservas(lineas_res, comercial.strip(),
                                              datos["titular"].strip(), w, datos["eurusd"])
                if fallo:
                    st.error(f"No se anotó ninguna reserva: {fallo}")
                else:
                    st.success(f"✅ {ok} reserva(s) anotadas en OTC interno.")
                    _catalogo_otc.clear()
                with st.spinner("Componiendo el documento…"):
                    st.session_state["_pdf_propuesta"] = propuesta_pdf.construir(datos)
                st.session_state["_confirmar_reservas"] = False
                st.rerun(scope="fragment")

    pdf = st.session_state.get("_pdf_propuesta")
    if pdf:
        st.download_button("⬇️ Descargar propuesta (PDF)", data=pdf, file_name=nombre,
                           mime="application/pdf", type="primary", use_container_width=True)


def _guardar_reservas(lineas: list, comercial: str, inversor: str,
                      wallet: str, eurusd: float) -> tuple:
    """Anota las reservas con el mismo formato que la gestión OTC.

    La lista se relee FRESCA justo antes de escribir y se añade encima: si se
    guardara la copia que se leyó al pintar la página, dos personas reservando
    a la vez se pisarían y una de las dos reservas desaparecería.
    """
    from datetime import datetime as _d
    try:
        todas = _store.read_list(TAB_RESERVAS, fresh=True)
    except Exception as e:      # noqa: BLE001
        return 0, f"no se pudo leer el registro de reservas ({e})"

    nuevas = []
    for i, r in enumerate(lineas):
        p = r["proyecto"]
        divisa = r.get("divisa_oferta") or p.get("divisa", "USD")
        total = float(r["n_tokens"]) * float(r["precio"])
        nuevas.append({
            "id": f"RES-{_d.utcnow().strftime('%Y%m%d%H%M%S')}{i:02d}",
            "tipo_origen": r["tipo_origen"],
            "oferta_id": r.get("oferta_id"),
            "token_address": r["address"],
            "proyecto_nombre": p.get("nombre", ""),
            "proyecto_id": p["label"],
            "comercial": comercial,
            "inversor": inversor,
            "wallet_inversor": wallet,
            "wallet_pendiente": False,
            "n_tokens": float(r["n_tokens"]),
            "precio_acordado": float(r["precio"]),
            "divisa": divisa,
            "total_eur": round(total if divisa == "EUR" else total / eurusd, 2),
            "total_usd": round(total if divisa == "USD" else total * eurusd, 2),
            "eur_usd_rate": eurusd,
            "fecha_reserva": _d.utcnow().strftime("%d/%m/%Y %H:%M"),
            "estado": "activa",
            "notas": "Creada desde el constructor de propuestas.",
            "tx_envio": None,
            "fecha_envio": None,
        })
    try:
        if not _store.write(TAB_RESERVAS, todas + nuevas):
            return 0, "el servidor rechazó la escritura"
    except Exception as e:      # noqa: BLE001
        return 0, str(e)
    return len(nuevas), ""


_slug = "".join(c if c.isalnum() else "_" for c in (titular or "propuesta")).strip("_")[:40]
_reserv = _reservables(cartera["lineas"])
if _reserv:
    st.caption(
        f"🏷️ Al generar el documento se ofrecerá reservar **{sum(r['n_tokens'] for r in _reserv):,.0f} "
        f"tokens** en OTC interno, que es lo que esta propuesta compra y hoy está disponible."
    )

_descarga(
    {"titular": titular, "estatus": estatus, "cartera": cartera, "escenarios": escenarios,
     "track": track, "precio_rnt": precio_rnt, "eurusd": eurusd,
     "coste_estatus": coste_ep, "tasa_staking": tasa_staking, "borrador": borrador,
     "fecha": date.today()},
    f"propuesta_reental_{_slug or 'sin_nombre'}_{date.today():%Y%m%d}.pdf",
    _reserv)
