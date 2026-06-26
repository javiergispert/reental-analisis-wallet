"""
Simulador de cartera de tokens inmobiliarios Reental.
Permite analizar carteras existentes (via wallet) y proyectar nuevas inversiones.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, date

from utils import (
    load_master_projects, load_p2p_listings, fetch_wallet_token_balances,
    calculate_irr, add_months,
)

API_KEY = os.getenv("ETHERSCAN_API_KEY", "")

# ── Configuración de página ───────────────────────────────────────────────────

st.title("🏗️ Simulador de Cartera Inmobiliaria")
st.caption("Proyecta el rendimiento de tu cartera actual y evalúa nuevas oportunidades de inversión.")

st.warning("🚧 En Construcción", icon="🚧")
st.stop()

# ── Inicializar estado de sesión ──────────────────────────────────────────────

if "wallet_slots"   not in st.session_state: st.session_state.wallet_slots   = [{"address": "", "loaded": False}]
if "wallet_tokens"  not in st.session_state: st.session_state.wallet_tokens  = {}   # address → {token_addr: balance}
if "portfolio"      not in st.session_state: st.session_state.portfolio      = {}   # key → position dict

# ── Carga de datos ────────────────────────────────────────────────────────────

with st.spinner("Cargando catálogo de proyectos..."):
    master_df = load_master_projects()

if master_df.empty:
    st.error("No se pudo cargar el catálogo de proyectos. Comprueba la URL del CSV en .env")
    st.stop()

known_addresses = set(master_df["token_address"].dropna().tolist())
project_by_addr = {row["token_address"]: row.to_dict()
                   for _, row in master_df.iterrows() if row["token_address"]}

# ── Sidebar: configuración global ─────────────────────────────────────────────

with st.sidebar:
    st.header("⚙️ Configuración")
    eur_usd = st.number_input(
        "Tipo de cambio EUR/USD", value=1.08, min_value=0.5, max_value=2.0, step=0.01,
        help="Se usa para convertir entre EUR y USDT en proyectos con distinta divisa.",
    )
    moneda_display = st.radio("Mostrar importes en", ["EUR", "USDT"], horizontal=True)
    st.markdown("---")
    activos = master_df[master_df["fuente"].isin(["lanzamiento", "en_marcha"])]
    st.caption(f"Proyectos en catálogo: {len(master_df)}")
    st.caption(f"Proyectos activos: {len(activos)}")


def to_display(amount: float, divisa_original: str) -> float:
    if moneda_display == "EUR":
        return amount / eur_usd if divisa_original == "USD" else amount
    else:
        return amount * eur_usd if divisa_original == "EUR" else amount


def fmt(v: float, sym: str = "") -> str:
    return f"{v:,.2f} {sym or moneda_display}"


# ═══════════════════════════════════════════════════════════════════════════════
# PASO 1 — Carteras existentes
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("👛 Paso 1 — Tu cartera actual (opcional)")
st.caption("Añade una o varias wallets. Los tokens Reental detectados se incorporarán automáticamente a la simulación.")

def load_wallet(idx: int, address: str):
    """Carga los tokens de una wallet y los añade al portfolio."""
    w = address.strip().lower()
    with st.spinner(f"Consultando blockchain para {w[:10]}…"):
        balances = fetch_wallet_token_balances(w, known_addresses, API_KEY)
    st.session_state.wallet_tokens[w] = balances
    st.session_state.wallet_slots[idx]["loaded"] = True
    # Añadir tokens al portfolio a precio de emisión
    for addr, bal in balances.items():
        if addr in project_by_addr:
            proj = project_by_addr[addr]
            key  = f"wallet_{w[:8]}_{addr}"
            if key not in st.session_state.portfolio:
                st.session_state.portfolio[key] = {
                    **proj,
                    "n_tokens":       bal,
                    "precio_compra":  to_display(proj["precio_emision"] or 0, proj["divisa"]),
                    "fuente_display": f"Wallet {w[:8]}…",
                }

def remove_wallet(idx: int, address: str):
    """Elimina una wallet y sus tokens del portfolio."""
    w = address.strip().lower()
    keys_del = [k for k in st.session_state.portfolio if k.startswith(f"wallet_{w[:8]}")]
    for k in keys_del:
        del st.session_state.portfolio[k]
    if w in st.session_state.wallet_tokens:
        del st.session_state.wallet_tokens[w]
    st.session_state.wallet_slots.pop(idx)


for idx, slot in enumerate(st.session_state.wallet_slots):
    c_addr, c_load, c_del = st.columns([5, 1, 1])
    with c_addr:
        new_val = st.text_input(
            f"Wallet {idx + 1}",
            value=slot["address"],
            placeholder="0x...",
            key=f"wallet_field_{idx}",
            label_visibility="collapsed" if idx > 0 else "visible",
        )
        st.session_state.wallet_slots[idx]["address"] = new_val

    w_addr = new_val.strip().lower()
    is_valid = w_addr.startswith("0x") and len(w_addr) == 42
    already_loaded = slot.get("loaded") and w_addr in st.session_state.wallet_tokens

    with c_load:
        load_label = "✓ Cargada" if already_loaded else "Cargar"
        load_type  = "secondary" if already_loaded else "primary"
        if st.button(load_label, key=f"load_{idx}", use_container_width=True,
                     type=load_type, disabled=not is_valid or already_loaded):
            load_wallet(idx, new_val)
            st.rerun()

    with c_del:
        if st.button("✕", key=f"del_slot_{idx}", use_container_width=True,
                     help="Quitar esta wallet"):
            remove_wallet(idx, new_val)
            st.rerun()

    # Mostrar tokens encontrados en esta wallet
    if already_loaded:
        balances = st.session_state.wallet_tokens.get(w_addr, {})
        found = [project_by_addr[a]["nombre"] for a in balances if a in project_by_addr]
        if found:
            st.caption(f"   Tokens encontrados: {', '.join(found)}")
        else:
            st.caption("   No se encontraron tokens Reental en esta wallet.")
    elif not is_valid and new_val:
        st.caption("   ⚠️ Dirección inválida — debe empezar por 0x y tener 42 caracteres.")

if st.button("➕ Añadir otra wallet", use_container_width=False):
    st.session_state.wallet_slots.append({"address": "", "loaded": False})
    st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# PASO 2 — Catálogo de oportunidades
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("🔍 Paso 2 — Oportunidades a analizar")

hoy = date.today()

# Filtros globales
with st.expander("🎛️ Filtros de preferencias", expanded=True):
    st.caption("Todos los filtros son opcionales y acumulables. Deja en blanco para ver todas las opciones.")
    fc1, fc2, fc3, fc4, fc5 = st.columns(5)

    ubicaciones = sorted(master_df["ubicacion"].dropna().unique().tolist())
    divisas     = sorted(master_df["divisa"].dropna().unique().tolist())
    tipos_renta = ["recurrente", "mixto", "final"]

    f_ubicacion  = fc1.multiselect("📍 Geografía", ubicaciones,
                                    placeholder="Todas las ubicaciones")
    f_divisa     = fc2.multiselect("💱 Divisa", divisas,
                                    placeholder="EUR y USDT")
    f_tipo_renta = fc3.multiselect("📈 Tipo de renta", tipos_renta,
                                    placeholder="Todos los tipos",
                                    help="Recurrente = rentas periódicas · Final = cobro único al vencimiento · Mixto = ambos")
    f_horizonte  = fc4.number_input("⏳ Horizonte máx. (años)", value=10, min_value=1, max_value=20,
                                     help="Solo muestra proyectos con fecha de fin dentro de este plazo")
    f_inv_minima = fc5.number_input(
        f"💰 Inversión mínima ({moneda_display})", value=0, min_value=0, step=500,
        help="Para P2P real: filtra ofertas donde (tokens disponibles × precio) ≥ este importe. Para nuevos lanzamientos no aplica.",
    )


def aplicar_filtros(df: pd.DataFrame, es_p2p: bool = False) -> pd.DataFrame:
    if df.empty:
        return df
    result = df.copy()
    if f_ubicacion:
        result = result[result["ubicacion"].isin(f_ubicacion)]
    if f_divisa:
        result = result[result["divisa"].isin(f_divisa)]
    if f_tipo_renta:
        result = result[result["tipo_renta"].isin(f_tipo_renta)]
    if f_horizonte:
        limite = date(hoy.year + f_horizonte, hoy.month, hoy.day)
        has_fecha = result["fecha_fin"].notna()
        result = result[~has_fecha | (result["fecha_fin"] <= limite)]
    if es_p2p and f_inv_minima > 0:
        # Para P2P: filtra por valor total disponible (tokens × precio p2p en moneda display)
        def valor_total(row):
            precio = to_display(row.get("precio_p2p_usdt") or 0, "USD")
            return (row.get("tokens_disponibles") or 0) * precio
        result = result[result.apply(valor_total, axis=1) >= f_inv_minima]
    return result


def render_catalog(df: pd.DataFrame, fuente: str, search_key: str):
    """Renderiza el catálogo de proyectos con buscador y botón de añadir."""
    busqueda = st.text_input("🔍 Buscar proyecto por nombre", key=f"search_{search_key}",
                              placeholder="Escribe para filtrar…", label_visibility="collapsed")
    if busqueda:
        df = df[df["nombre"].str.contains(busqueda, case=False, na=False)]

    if df.empty:
        st.info("No hay proyectos disponibles con los filtros actuales.")
        return

    es_p2p = fuente == "p2p_real"

    for _, row in df.iterrows():
        key_base   = f"{fuente}_{row['id']}"
        already_in = key_base in st.session_state.portfolio

        with st.container():
            if es_p2p:
                c1, c2, c3, c4, c5, c6, c7, c8 = st.columns([2, 1, 1, 1, 1, 1, 1, 1])
            else:
                c1, c2, c3, c4, c5, c6 = st.columns([2, 1, 1, 1, 1, 1])

            # Nombre + metadatos
            c1.markdown(
                f"**{row['nombre']}** `{row['id']}`  \n"
                f"<small>📍 {row['ubicacion']} · {row['divisa']} · {row['tipo_renta']}</small>",
                unsafe_allow_html=True,
            )

            # Renta anualizada
            if row["tipo_renta"] == "final":
                r_val  = (row["r_hoy_anualizada"] or row["r_total_anualizada"] or 0) * 100
                r_help = "Rentabilidad anualizada estimada desde hoy hasta el vencimiento"
            else:
                r_val  = (row["r_rec_anualizada"] or 0) * 100
                r_help = "Rentabilidad recurrente anualizada (sobre precio de emisión)"
            c2.metric("Renta anual", f"{r_val:.1f}%", help=r_help)

            # Plusvalía
            plusv = row.get("r_plusvalia")
            c3.metric("Plusvalía est.", f"{plusv*100:.1f}%" if plusv else "—",
                      help="Revalorización estimada del inmueble al cierre del proyecto")

            # Precio de emisión
            precio_em = to_display(row["precio_emision"] or 0, row["divisa"])
            c4.metric(
                f"Precio emisión ({moneda_display})",
                f"{precio_em:,.2f}",
                help="Precio original al que se emitió el token en el lanzamiento del proyecto",
            )

            # Fecha de fin
            fecha_str = row["fecha_fin"].strftime("%m/%Y") if row.get("fecha_fin") else "—"
            c5.metric("Fin estimado", fecha_str)

            if es_p2p:
                # Precio P2P (precio actual de mercado secundario)
                precio_p2p_usdt = row.get("precio_p2p_usdt") or 0
                precio_p2p_d    = to_display(precio_p2p_usdt, "USD")
                c6.metric(
                    f"Precio P2P ({moneda_display})",
                    f"{precio_p2p_d:,.2f}",
                    delta=f"{(precio_p2p_d - precio_em):+.2f} vs emisión",
                    delta_color="inverse" if precio_p2p_d > precio_em else "normal",
                    help="Precio mínimo de venta en el mercado P2P (puede diferir del precio de emisión)",
                )
                # Tokens disponibles
                disp = int(row.get("tokens_disponibles") or 0)
                c7.metric("Disponibles", f"{disp} tokens",
                          help="Número de tokens disponibles para comprar ahora mismo en P2P")
                btn_col = c8
            else:
                btn_col = c6

            # Botón añadir / quitar
            btn_label = "✓ Añadido" if already_in else "＋ Añadir"
            btn_type  = "secondary" if already_in else "primary"
            if btn_col.button(btn_label, key=f"btn_{key_base}", type=btn_type, use_container_width=True):
                if already_in:
                    del st.session_state.portfolio[key_base]
                else:
                    if fuente == "p2p_real":
                        precio_compra = to_display(precio_p2p_usdt or row["precio_emision"], "USD")
                    else:
                        precio_compra = to_display(row["precio_emision"] or 0, row["divisa"])

                    fuente_labels = {
                        "lanzamiento": "Nuevo lanzamiento",
                        "en_marcha":   "P2P hipotetico",
                        "p2p_real":    "P2P real",
                    }
                    st.session_state.portfolio[key_base] = {
                        **row.to_dict(),
                        "n_tokens":       10,
                        "precio_compra":  precio_compra,
                        "fuente_display": fuente_labels.get(fuente, fuente),
                    }
                st.rerun()

        st.divider()


# Cargar P2P antes de los tabs para que esté disponible
with st.spinner("Cargando ofertas P2P…"):
    p2p_df = load_p2p_listings(master_df)

tab_nuevos, tab_marcha, tab_p2p = st.tabs(
    ["🆕 Nuevos lanzamientos", "📊 En marcha (P2P hipotetico)", "🔄 P2P real disponible"]
)

with tab_nuevos:
    st.caption("Proyectos actualmente en fase de financiacion o prelanzamiento. El inversor entraría a precio de emisión.")
    st.text_input("🔍 Buscar proyecto por nombre", key="search_hint_nuevos",
                  placeholder="Escribe para filtrar…", label_visibility="visible")
    df_nuevos = aplicar_filtros(master_df[master_df["fuente"] == "lanzamiento"])
    # Filtrar por búsqueda
    busq_n = st.session_state.get("search_hint_nuevos", "")
    if busq_n:
        df_nuevos = df_nuevos[df_nuevos["nombre"].str.contains(busq_n, case=False, na=False)]
    render_catalog(df_nuevos, "lanzamiento", "nuevos")

with tab_marcha:
    st.caption("Proyectos ya en funcionamiento. El inversor los compraría via P2P; precio de referencia = emisión.")
    st.text_input("🔍 Buscar proyecto por nombre", key="search_hint_marcha",
                  placeholder="Escribe para filtrar…", label_visibility="visible")
    df_marcha = aplicar_filtros(master_df[master_df["fuente"] == "en_marcha"])
    busq_m = st.session_state.get("search_hint_marcha", "")
    if busq_m:
        df_marcha = df_marcha[df_marcha["nombre"].str.contains(busq_m, case=False, na=False)]
    render_catalog(df_marcha, "en_marcha", "marcha")

with tab_p2p:
    st.caption("Ofertas reales con tokens disponibles ahora mismo en el mercado P2P de Reental.")
    st.text_input("🔍 Buscar proyecto por nombre", key="search_hint_p2p",
                  placeholder="Escribe para filtrar…", label_visibility="visible")
    if not p2p_df.empty:
        df_p2p = aplicar_filtros(p2p_df, es_p2p=True)
        busq_p = st.session_state.get("search_hint_p2p", "")
        if busq_p:
            df_p2p = df_p2p[df_p2p["nombre"].str.contains(busq_p, case=False, na=False)]
        render_catalog(df_p2p, "p2p_real", "p2p")
    else:
        st.info("No hay ofertas disponibles en el mercado P2P en este momento.")


# ═══════════════════════════════════════════════════════════════════════════════
# PASO 3 — Cartera a simular
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("🧺 Paso 3 — Cartera a simular")

if not st.session_state.portfolio:
    st.info("Añade proyectos desde el Paso 2 o carga una wallet en el Paso 1 para construir tu cartera.")
    st.stop()

st.caption("Ajusta el número de tokens y el precio de compra. Los cambios se aplican en tiempo real.")

portfolio_keys = list(st.session_state.portfolio.keys())
rows_edit = []
for k in portfolio_keys:
    p = st.session_state.portfolio[k]
    rows_edit.append({
        "Proyecto":                          p["nombre"],
        "Fuente":                            p.get("fuente_display", "—"),
        "Divisa":                            p["divisa"],
        f"Precio compra ({moneda_display})": p.get("precio_compra", p.get("precio_emision", 0)),
        "N tokens":                          int(p.get("n_tokens", 1)),
        "Fin estimado":                      p["fecha_fin"].strftime("%m/%Y") if p.get("fecha_fin") else "—",
    })

df_editor = pd.DataFrame(rows_edit)
edited = st.data_editor(
    df_editor,
    column_config={
        f"Precio compra ({moneda_display})": st.column_config.NumberColumn(min_value=0, step=0.01, format="%.2f"),
        "N tokens":      st.column_config.NumberColumn(min_value=1, step=1, format="%d"),
        "Proyecto":      st.column_config.TextColumn(disabled=True),
        "Fuente":        st.column_config.TextColumn(disabled=True),
        "Divisa":        st.column_config.TextColumn(disabled=True),
        "Fin estimado":  st.column_config.TextColumn(disabled=True),
    },
    hide_index=True, use_container_width=True,
)

for i, k in enumerate(portfolio_keys):
    st.session_state.portfolio[k]["precio_compra"] = float(edited.iloc[i][f"Precio compra ({moneda_display})"])
    st.session_state.portfolio[k]["n_tokens"]       = int(edited.iloc[i]["N tokens"])

col_sim, col_clear = st.columns([4, 1])
with col_clear:
    if st.button("🗑️ Vaciar cartera", use_container_width=True):
        st.session_state.portfolio      = {}
        st.session_state.wallet_slots   = [{"address": "", "loaded": False}]
        st.session_state.wallet_tokens  = {}
        st.rerun()
with col_sim:
    simular_btn = st.button("🚀 Simular cartera", type="primary", use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════════
# PASO 4 — Resultados de la simulación
# ═══════════════════════════════════════════════════════════════════════════════

if not simular_btn and "sim_results" not in st.session_state:
    st.stop()

if simular_btn:

    def simulate_position(p: dict) -> dict:
        n_tokens      = p.get("n_tokens", 0) or 0
        precio_compra = p.get("precio_compra", 0) or 0   # ya en moneda display
        precio_emision_orig = p.get("precio_emision", 0) or 0
        divisa        = p.get("divisa", "EUR")
        tipo_renta    = p.get("tipo_renta", "recurrente")
        fecha_fin     = p.get("fecha_fin")

        precio_emision_d = to_display(precio_emision_orig, divisa)
        if not n_tokens or not precio_compra:
            return None

        inversion_total = precio_compra * n_tokens
        fecha_inicio    = max(hoy, p.get("fecha_lanzamiento") or hoy)

        if fecha_fin is None or fecha_fin <= fecha_inicio:
            fecha_fin_sim  = date(hoy.year + 2, hoy.month, hoy.day)
            sin_fecha_fin  = True
        else:
            fecha_fin_sim = fecha_fin
            sin_fecha_fin = False

        anios_restantes = max(0.01, (fecha_fin_sim - fecha_inicio).days / 365.25)

        # Dividendo mensual por token (sobre precio de emisión, no de compra)
        r_rec = p.get("r_rec_anualizada")
        div_mensual_token = precio_emision_d * r_rec / 12 if tipo_renta in ("recurrente", "mixto") and r_rec else 0.0

        # Valor de retorno al vencimiento
        r_plusv = p.get("r_plusvalia") or 0
        if tipo_renta == "final":
            r_hoy = p.get("r_hoy_total") or (
                (p.get("r_hoy_anualizada") or p.get("r_total_anualizada") or 0) * anios_restantes
            )
            retorno_final_token = precio_emision_d * (1 + r_hoy)
        else:
            retorno_final_token = precio_emision_d * (1 + r_plusv)

        # Flujos de caja mensuales para TIR
        cash_flows  = [(datetime.combine(fecha_inicio, datetime.min.time()), -inversion_total)]
        monthly_cf  = []
        mes         = fecha_inicio.replace(day=1)
        meses       = 0

        while True:
            mes = add_months(mes, 1)
            if mes >= fecha_fin_sim:
                break
            meses += 1
            if div_mensual_token > 0:
                importe = div_mensual_token * n_tokens
                cash_flows.append((datetime.combine(mes, datetime.min.time()), importe))
                monthly_cf.append((mes, importe, p["nombre"], "Renta"))

        retorno_final_total = retorno_final_token * n_tokens
        cash_flows.append((datetime.combine(fecha_fin_sim, datetime.min.time()), retorno_final_total))
        monthly_cf.append((fecha_fin_sim, retorno_final_total, p["nombre"], "Capital + Plusvalia"))

        rentas_totales = div_mensual_token * n_tokens * meses
        ganancia_neta  = rentas_totales + retorno_final_total - inversion_total
        rent_pct       = ganancia_neta / inversion_total if inversion_total > 0 else 0
        tir            = calculate_irr(cash_flows)

        return {
            "nombre":          p["nombre"],
            "id":              p.get("id", ""),
            "fuente":          p.get("fuente_display", "—"),
            "divisa":          divisa,
            "tipo_renta":      tipo_renta,
            "ubicacion":       p.get("ubicacion", "—"),
            "n_tokens":        n_tokens,
            "precio_compra":   precio_compra,
            "inversion_total": inversion_total,
            "rentas_totales":  rentas_totales,
            "retorno_final":   retorno_final_total,
            "ganancia_neta":   ganancia_neta,
            "rent_pct":        rent_pct,
            "tir":             tir,
            "anios_restantes": anios_restantes,
            "monthly_cf":      monthly_cf,
            "sin_fecha_fin":   sin_fecha_fin,
        }

    results = [r for p in st.session_state.portfolio.values() if (r := simulate_position(p))]
    st.session_state.sim_results = results

results = st.session_state.get("sim_results", [])
if not results:
    st.warning("No se pudo simular ninguna posición. Revisa que los proyectos tengan datos de rentabilidad.")
    st.stop()

st.markdown("---")
st.subheader("📊 Resultados de la simulacion")

# ── KPIs globales ─────────────────────────────────────────────────────────────
total_inv      = sum(r["inversion_total"] for r in results)
total_rentas   = sum(r["rentas_totales"]  for r in results)
total_capital  = sum(r["retorno_final"]   for r in results)
total_ganancia = sum(r["ganancia_neta"]   for r in results)
rent_global    = total_ganancia / total_inv if total_inv > 0 else 0

tires_validas  = [(r["tir"], r["inversion_total"]) for r in results if r["tir"] is not None]
tir_pond       = (sum(t * i for t, i in tires_validas) / sum(i for _, i in tires_validas)
                  if tires_validas else None)

sym = moneda_display
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric(f"Total invertido ({sym})",          fmt(total_inv))
k2.metric(f"Rentas proyectadas ({sym})",       fmt(total_rentas))
k3.metric(f"Capital final proyectado ({sym})", fmt(total_capital))
k4.metric(f"Ganancia neta ({sym})",            fmt(total_ganancia),
          delta=f"{rent_global*100:.1f}% sobre inversion",
          delta_color="normal" if total_ganancia >= 0 else "inverse")
k5.metric("TIR media ponderada",
          f"{tir_pond*100:.2f}%" if tir_pond is not None else "—")

# ── Timeline de flujos ────────────────────────────────────────────────────────
st.markdown("#### 📅 Proyeccion de flujos mensuales")

all_cf = [(d, v, n, t) for r in results for d, v, n, t in r["monthly_cf"]]

if all_cf:
    df_cf = pd.DataFrame(all_cf, columns=["fecha", "importe", "proyecto", "tipo"])
    df_cf["fecha"] = pd.to_datetime(df_cf["fecha"])
    df_pivot = (df_cf.pivot_table(index="fecha", columns="proyecto", values="importe", aggfunc="sum")
                .fillna(0).sort_index())

    colores = ["#2563eb","#16a34a","#dc2626","#d97706","#7c3aed",
               "#0891b2","#db2777","#65a30d","#ea580c","#6d28d9"]
    fig = go.Figure()
    for i, col in enumerate(df_pivot.columns):
        fig.add_trace(go.Bar(name=col, x=df_pivot.index, y=df_pivot[col],
                             marker_color=colores[i % len(colores)]))

    cumul = df_cf.groupby("fecha")["importe"].sum().cumsum().reset_index()
    fig.add_trace(go.Scatter(name="Acumulado", x=cumul["fecha"], y=cumul["importe"],
                             mode="lines+markers", line=dict(color="black", width=2, dash="dot"),
                             yaxis="y2"))
    fig.update_layout(
        barmode="stack",
        yaxis=dict(title=f"Flujo ({sym})"),
        yaxis2=dict(title=f"Acumulado ({sym})", overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        height=400, margin=dict(t=40), hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)

# ── Diversificacion ───────────────────────────────────────────────────────────
col_g, col_t = st.columns(2)
with col_g:
    st.markdown("**Por geografia**")
    div_geo = {}
    for r in results:
        ub = r.get("ubicacion", "—")
        div_geo[ub] = div_geo.get(ub, 0) + r["inversion_total"]
    fig_g = go.Figure(go.Pie(labels=list(div_geo.keys()), values=list(div_geo.values()),
                              hole=0.4, textinfo="label+percent"))
    fig_g.update_layout(showlegend=False, height=260, margin=dict(t=0, b=0))
    st.plotly_chart(fig_g, use_container_width=True)

with col_t:
    st.markdown("**Por tipo de renta**")
    div_tipo = {}
    for r in results:
        t = r["tipo_renta"]
        div_tipo[t] = div_tipo.get(t, 0) + r["inversion_total"]
    fig_t = go.Figure(go.Pie(labels=list(div_tipo.keys()), values=list(div_tipo.values()),
                              hole=0.4, textinfo="label+percent",
                              marker_colors=["#2563eb","#16a34a","#d97706"]))
    fig_t.update_layout(showlegend=False, height=260, margin=dict(t=0, b=0))
    st.plotly_chart(fig_t, use_container_width=True)

# ── Tabla por proyecto ────────────────────────────────────────────────────────
st.markdown("#### 📋 Detalle por proyecto")

tabla_rows = []
for r in results:
    aviso = " *" if r.get("sin_fecha_fin") else ""
    tabla_rows.append({
        "Proyecto":               r["nombre"] + aviso,
        "Fuente":                 r["fuente"],
        "N tokens":               r["n_tokens"],
        f"Inversion ({sym})":     round(r["inversion_total"], 2),
        f"Rentas ({sym})":        round(r["rentas_totales"], 2),
        f"Capital final ({sym})": round(r["retorno_final"], 2),
        f"Ganancia ({sym})":      round(r["ganancia_neta"], 2),
        "Rentab. total":          f"{r['rent_pct']*100:.1f}%",
        "TIR anual":              f"{r['tir']*100:.2f}%" if r["tir"] is not None else "—",
        "Anos restantes":         f"{r['anios_restantes']:.1f}",
    })

def style_ganancia(val):
    try:
        v = float(str(val).replace(",","").replace("%","").strip())
        if v > 0: return "color: #16a34a; font-weight: 600"
        if v < 0: return "color: #dc2626; font-weight: 600"
    except Exception:
        pass
    return ""

styled = pd.DataFrame(tabla_rows).style.applymap(
    style_ganancia, subset=[f"Ganancia ({sym})", "Rentab. total", "TIR anual"]
)
st.dataframe(styled, hide_index=True, use_container_width=True)

if any(r.get("sin_fecha_fin") for r in results):
    st.caption("* Proyecto sin fecha de fin definida: se ha simulado un horizonte de 2 años.")

# ── Nota metodologica ─────────────────────────────────────────────────────────
with st.expander("📐 Metodologia de la simulacion"):
    st.markdown(f"""
**Inversion inicial:** precio de compra × n.º de tokens, en {moneda_display}.

**Rentas proyectadas (tipo recurrente/mixto):**
Rentabilidad recurrente anualizada (real si existe, estimada si no) × precio de emision × n.º tokens, pagada mensualmente hasta el vencimiento del proyecto.

**Rentas proyectadas (tipo final — flipping / prestamo promotor):**
Sin pagos periodicos. El retorno total se cobra integro al vencimiento: precio de emision × (1 + rentabilidad estimada desde hoy hasta fin).

**Retorno de capital al vencimiento (recurrente/mixto):**
Precio de emision × (1 + plusvalia estimada). La plusvalia es sobre precio de emision original con independencia del precio pagado en P2P.

**Efecto P2P:** las rentas absolutas por token son siempre las mismas (calculadas sobre precio de emision), pero como el denominador de la inversion cambia, la rentabilidad % varia segun el precio pagado.

**TIR:** Newton-Raphson sobre flujos fechados reales (mensual para recurrentes, unico al vencimiento para proyectos finales).

**Tipo de cambio EUR/USD:** {eur_usd:.4f} (configurable en el panel lateral).

> Los valores son estimaciones. No constituyen asesoramiento financiero.
    """)
