"""
Gestión OTC interna — Reental
Saldos en tiempo real de la wallet custodia + sistema de reservas para comerciales.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

import json
import time
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
import requests
import io
from datetime import datetime, timezone

from utils import load_master_projects, strip_accents

# ── Constantes ────────────────────────────────────────────────────────────────

OTC_WALLET      = os.getenv("OTC_WALLET", "0xce0719ec1bda336ba069c6961ad167767829301a").lower()
API_KEY         = os.getenv("ETHERSCAN_API_KEY", "")
ETHERSCAN_BASE  = "https://api.etherscan.io/v2/api"
POLYGON_CHAIN   = 137
CACHE_TTL_SECS   = 3600
POLYSCAN_TX_URL  = "https://polygonscan.com/tx/"
EXCHANGE_API_URL = "https://open.er-api.com/v6/latest/EUR"
OTC_ADMIN_PIN    = os.getenv("OTC_ADMIN_PIN", "1234")
SPREADSHEET_ID   = "13Q0n7egbAIJSU9UvwwDucd3MUQ48Q44eoMwsPT-PmGs"
TAB_RESERVAS     = "Reservas"
TAB_OFERTAS      = "Ofertas"
TAB_PRECIOS      = "precios_otc"

# ── Página ────────────────────────────────────────────────────────────────────

st.title("🏢 Gestión OTC Interna")
st.caption(
    f"Wallet custodia: `{OTC_WALLET}` · "
    "Saldos actualizados cada hora desde Etherscan · "
    "Reservas guardadas en servidor."
)

# ── Google Sheets — cliente por sesión ───────────────────────────────────────

def _get_client():
    """Cliente gspread independiente por sesión para evitar conflictos entre usuarios."""
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds  = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=scopes
    )
    return gspread.authorize(creds)

def _ws(tab: str):
    return _get_client().open_by_key(SPREADSHEET_ID).worksheet(tab)

def _raw_read(tab: str) -> str | None:
    """Lee el contenido de A1 con reintentos. Devuelve el string crudo o None."""
    for intento in range(4):
        try:
            val = _ws(tab).acell("A1").value
            return val
        except Exception:
            if intento < 3:
                time.sleep(1.5)
    return None

@st.cache_data(ttl=6, show_spinner=False)
def _cached_read(tab: str) -> str | None:
    """Caché de lectura de 6 segundos: evita golpes simultáneos a la API."""
    return _raw_read(tab)

def _read_list(tab: str) -> list:
    val = _cached_read(tab)
    if not val:
        return []
    try:
        parsed = json.loads(val)
    except Exception:
        return []
    if isinstance(parsed, list):
        return parsed
    # Formato antiguo: migrar (un solo dict en A1 = una sola reserva)
    if isinstance(parsed, dict):
        migrated = [parsed]
        _write(tab, migrated)
        _cached_read.clear()
        return migrated
    return []

def _read_dict(tab: str) -> dict:
    val = _cached_read(tab)
    if not val:
        return {}
    try:
        parsed = json.loads(val)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}

def _write(tab: str, data):
    for intento in range(4):
        try:
            _ws(tab).update("A1", [[json.dumps(data, ensure_ascii=False)]])
            _cached_read.clear()   # invalida caché inmediatamente tras escritura
            return
        except Exception:
            if intento < 3:
                time.sleep(1.5)

# ── Persistencia de reservas ──────────────────────────────────────────────────

def load_reservas() -> list:
    return _read_list(TAB_RESERVAS)

def save_reservas(reservas: list):
    _write(TAB_RESERVAS, reservas)

# ── Persistencia de precios OTC ───────────────────────────────────────────────

def load_precios_otc() -> dict:
    return _read_dict(TAB_PRECIOS)

def save_precios_otc(precios: dict):
    _write(TAB_PRECIOS, precios)

# ── Persistencia de ofertas de terceros ──────────────────────────────────────

def load_ofertas() -> list:
    return _read_list(TAB_OFERTAS)

def save_ofertas(ofertas: list):
    _write(TAB_OFERTAS, ofertas)

# ── Tipo de cambio EUR/USD ────────────────────────────────────────────────────

@st.cache_data(show_spinner=False, ttl=3600)
def get_eur_usd_rate() -> tuple:
    """Devuelve (tasa EUR→USD, fecha_str). Fallback a 1.10 si falla."""
    try:
        r = requests.get(EXCHANGE_API_URL, timeout=8)
        data = r.json()
        rate = data["rates"]["USD"]
        fecha = data.get("time_last_update_utc", "")[:16]
        return round(rate, 6), fecha
    except Exception:
        return 1.10, "API no disponible"

# ── Carga del catálogo master ─────────────────────────────────────────────────

with st.spinner("Cargando catálogo de proyectos..."):
    master_df = load_master_projects()

project_by_addr = {}
project_by_id   = {}   # clave en minúsculas para matching case-insensitive
if not master_df.empty:
    for _, row in master_df.iterrows():
        if row.get("token_address"):
            project_by_addr[row["token_address"]] = row.to_dict()
        project_by_id[row["id"].lower()] = row.to_dict()

known_addresses = set(project_by_addr.keys())

# Mapa aToken Aave → dirección del token subyacente Reental
# Los aTokens Reental en Aave tienen nombre "Aave Matic Reental-XXX" y su subyacente
# se identifica cruzando con project_by_addr por nombre de proyecto.
# Construimos {atoken_addr_lower: underlying_addr_lower} al vuelo desde las TX.

# ── Fetch saldos OTC desde Etherscan ─────────────────────────────────────────

@st.cache_data(show_spinner=False, ttl=CACHE_TTL_SECS)
def fetch_otc_balances(wallet: str, api_key: str) -> tuple:
    """
    Devuelve (balances_dict, last_txs_dict, fetch_ts):
      - balances_dict  {contract_addr: {"nombre", "id", "saldo", "divisa", ...}}
        Los saldos de aTokens Aave se consolidan sobre la dirección del token subyacente.
      - last_txs_dict  {contract_addr: [{"hash", "to", "value", "ts"}]}
      - fetch_ts       datetime UTC
    """
    params = {
        "chainid": POLYGON_CHAIN, "module": "account", "action": "tokentx",
        "address": wallet, "startblock": 0, "endblock": 99999999,
        "sort": "asc", "apikey": api_key,
    }
    try:
        resp = requests.get(ETHERSCAN_BASE, params=params, timeout=30)
        txs  = resp.json().get("result") or []
    except Exception:
        return {}, {}, datetime.now(timezone.utc)

    # nombre_proyecto_lower → token_address  (para resolver aTokens por nombre)
    nombre_to_addr = {
        row["nombre"].lower(): addr
        for addr, row in project_by_addr.items()
    }

    raw_balances = {}  # addr → float  (puede ser aToken o token real)
    atoken_map   = {}  # atoken_addr → underlying_addr  (se rellena al encontrar aTokens)
    last_txs     = {}  # underlying_addr → list of outgoing tx dicts

    for tx in txs:
        contract  = tx["contractAddress"].lower()
        sym       = tx.get("tokenSymbol", "")
        name      = tx.get("tokenName", "")
        dec       = int(tx.get("tokenDecimal") or 18)
        value     = int(tx["value"]) / (10 ** dec)
        ts        = int(tx.get("timeStamp", 0))
        to_addr   = tx["to"].lower()
        from_addr = tx["from"].lower()

        # Detectar si es aToken de Reental en Aave
        is_atoken = (
            sym.startswith("aMatReental-") or
            name.startswith("Aave Matic Reental-")
        )
        if is_atoken and contract not in atoken_map:
            # Extraer sufijo: "aMatReental-CME-1" → "CME-1"
            if name.startswith("Aave Matic Reental-"):
                suffix = name[len("Aave Matic Reental-"):].strip()
            else:
                suffix = sym[len("aMatReental-"):].strip()
            suffix_lower = suffix.lower()

            underlying = None
            # 1) Buscar directamente por ID (clave ya en minúsculas)
            if suffix_lower in project_by_id:
                underlying = project_by_id[suffix_lower].get("token_address")
            # 2) Buscar por coincidencia en nombre de proyecto
            if not underlying:
                for n, a in nombre_to_addr.items():
                    if suffix_lower in n:
                        underlying = a
                        break
            if underlying:
                atoken_map[contract] = underlying.lower()

        # Resolver dirección efectiva (aToken → subyacente si es posible)
        effective_addr = atoken_map.get(contract, contract if contract in known_addresses else None)
        if effective_addr is None:
            continue

        is_in  = to_addr == wallet and from_addr != wallet
        is_out = from_addr == wallet and to_addr != wallet
        if is_in:
            raw_balances[effective_addr] = raw_balances.get(effective_addr, 0.0) + value
        elif is_out:
            raw_balances[effective_addr] = raw_balances.get(effective_addr, 0.0) - value
            # Solo registrar TX salientes de tokens reales (no aTokens) para detección de envíos
            if contract in known_addresses:
                entry = {"hash": tx["hash"], "to": to_addr, "value": value, "ts": ts}
                last_txs.setdefault(effective_addr, []).append(entry)
        # auto-transferencias (from == to == wallet): se ignoran, saldo neto = 0

    # Montar resultado enriquecido
    result = {}
    for addr, saldo in raw_balances.items():
        if saldo < 0.001:
            continue
        proj = project_by_addr.get(addr, {})
        fecha_fin = proj.get("fecha_fin")
        result[addr] = {
            "nombre":         proj.get("nombre", addr[:12] + "…"),
            "id":             proj.get("id", "—"),
            "saldo":          round(saldo, 6),
            "divisa":         proj.get("divisa", "EUR"),
            "precio_emision": proj.get("precio_emision") or 0,
            "ubicacion":      proj.get("ubicacion", "—"),
            "estado":         proj.get("estado", "—"),
            "fecha_fin":      fecha_fin.strftime("%Y/%m") if fecha_fin else "—",
            "tipo_renta":     proj.get("tipo_renta", "—"),
        }

    return result, last_txs, datetime.now(timezone.utc)


@st.cache_data(show_spinner=False, ttl=CACHE_TTL_SECS)
def fetch_token_balance(wallet: str, token_address: str, api_key: str) -> float:
    """Saldo neto de un token específico en una wallet de tercero (sin aTokens)."""
    wallet = wallet.lower()
    token  = token_address.lower()
    params = {
        "chainid": POLYGON_CHAIN, "module": "account", "action": "tokentx",
        "contractaddress": token, "address": wallet,
        "startblock": 0, "endblock": 99999999, "sort": "asc", "apikey": api_key,
    }
    try:
        txs = requests.get(ETHERSCAN_BASE, params=params, timeout=15).json().get("result") or []
    except Exception:
        return -1.0   # -1 indica error de consulta
    bal = 0.0
    for tx in txs:
        dec   = int(tx.get("tokenDecimal") or 18)
        value = int(tx["value"]) / (10 ** dec)
        to_   = tx["to"].lower()
        from_ = tx["from"].lower()
        if to_ == wallet and from_ != wallet:
            bal += value
        elif from_ == wallet and to_ != wallet:
            bal -= value
    return round(bal, 6)


# ── Botón de refresco manual ──────────────────────────────────────────────────

col_ref, col_ts = st.columns([1, 5])
if col_ref.button("🔄 Actualizar saldos ahora", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

with st.spinner("Consultando blockchain…"):
    otc_balances, last_txs, fetch_ts = fetch_otc_balances(OTC_WALLET, API_KEY)

col_ts.caption(f"Última consulta: {fetch_ts.strftime('%d/%m/%Y %H:%M')} UTC")

if not otc_balances:
    st.warning("No se encontraron tokens Reental en la wallet OTC. Comprueba que la dirección es correcta.")

# ── Cargar reservas y detectar envíos ────────────────────────────────────────

reservas = load_reservas()
changed  = False
TOL      = 0.001   # tolerancia para considerar importe exacto

for r in reservas:
    if r.get("estado") in ("completada", "cancelada"):
        continue
    contract   = r.get("token_address", "").lower()
    wallet_inv = r.get("wallet_inversor", "").lower()
    n_reservado = float(r.get("n_tokens", 0))
    if not contract or not wallet_inv:
        continue

    txs_salientes = last_txs.get(contract, [])
    # Solo TX posteriores a la fecha de creación de la reserva
    try:
        ts_reserva = datetime.strptime(r["fecha_reserva"], "%d/%m/%Y %H:%M").timestamp()
    except Exception:
        ts_reserva = 0
    txs_match = [
        tx for tx in txs_salientes
        if tx["to"] == wallet_inv and tx["ts"] >= ts_reserva
    ]
    if not txs_match:
        continue

    # Preferir la TX cuyo importe coincida exactamente; si no, la más antigua
    tx_exacta = next((tx for tx in txs_match if abs(tx["value"] - n_reservado) <= TOL), None)
    tx_elegida = tx_exacta or txs_match[0]

    enviado     = tx_elegida["value"]
    diferencia  = round(enviado - n_reservado, 6)
    fecha_envio = datetime.utcfromtimestamp(tx_elegida["ts"]).strftime("%d/%m/%Y %H:%M")

    r["tx_envio"]       = tx_elegida["hash"]
    r["fecha_envio"]    = fecha_envio
    r["tokens_enviados"] = round(enviado, 3)

    if diferencia < -TOL:
        # Envío parcial: tokens enviados < reservados
        pendientes = round(abs(diferencia), 3)
        r["estado"]        = "completada"
        r["envio_parcial"] = True
        r["tokens_pendientes"] = pendientes
        r["nota_envio"] = (
            f"⚠️ Envío parcial: se enviaron {enviado:.3f} tokens de los {n_reservado:.3f} reservados. "
            f"Faltan {pendientes:.3f} tokens. Si el inversor tiene pendiente recibir más, "
            f"realiza una nueva reserva por los {pendientes:.3f} tokens pendientes."
        )
    else:
        # Envío exacto o con exceso
        r["estado"]        = "completada"
        r["envio_parcial"] = False
        if diferencia > TOL:
            r["nota_envio"] = (
                f"Se enviaron {enviado:.3f} tokens ({diferencia:+.3f} respecto a los {n_reservado:.3f} reservados)."
            )
        else:
            r["nota_envio"] = None

    changed = True

if changed:
    save_reservas(reservas)

# ── Calcular saldos reservados y disponibles ──────────────────────────────────

def calcular_disponibles(balances: dict, reservas: list) -> dict:
    """Devuelve {contract_addr: tokens_reservados} solo para reservas activas."""
    reservado = {}
    for r in reservas:
        if r.get("estado") in ("completada", "cancelada"):
            continue
        addr = r.get("token_address", "").lower()
        reservado[addr] = reservado.get(addr, 0.0) + float(r.get("n_tokens", 0))
    result = {}
    for addr, data in balances.items():
        res = reservado.get(addr, 0.0)
        result[addr] = {
            **data,
            "reservado":   res,
            "disponible":  max(0.0, data["saldo"] - res),
        }
    return result

saldos       = calcular_disponibles(otc_balances, reservas)
precios_otc  = load_precios_otc()
eur_usd, eur_usd_fecha = get_eur_usd_rate()

# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 1 — Saldos OTC
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("📦 Saldos en wallet OTC")

def kpi_card(icon, label, value, value_color="#1e293b", sublabel=""):
    return f"""
    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;
                padding:16px 18px;display:flex;flex-direction:column;gap:4px;">
      <div style="font-size:0.72rem;font-weight:600;color:#64748b;
                  letter-spacing:0.05em;text-transform:uppercase;">{icon}&nbsp;{label}</div>
      <div style="font-size:1.45rem;font-weight:700;color:{value_color};
                  line-height:1.2;">{value}</div>
      <div style="font-size:0.72rem;color:#94a3b8;">{sublabel}</div>
    </div>"""

total_tokens    = sum(d["saldo"]      for d in saldos.values())
total_reservado = sum(d["reservado"]  for d in saldos.values())
total_disp      = sum(d["disponible"] for d in saldos.values())
n_proyectos     = len(saldos)
reservas_activas = [r for r in reservas if r.get("estado") not in ("completada", "cancelada")]

c1, c2, c3, c4, c5 = st.columns(5)
c1.markdown(kpi_card("🏠", "Proyectos en cartera",  str(n_proyectos),          sublabel="tokens distintos"),             unsafe_allow_html=True)
c2.markdown(kpi_card("🪙", "Tokens en custodia",    f"{total_tokens:,.0f}",    sublabel="saldo bruto blockchain"),       unsafe_allow_html=True)
c3.markdown(kpi_card("📋", "Tokens reservados",     f"{total_reservado:,.0f}", sublabel="reservas activas pendientes",  value_color="#d97706"), unsafe_allow_html=True)
c4.markdown(kpi_card("✅", "Tokens disponibles",    f"{total_disp:,.0f}",      sublabel="libres para nueva reserva",    value_color="#16a34a"), unsafe_allow_html=True)
c5.markdown(kpi_card("🔖", "Reservas activas",      str(len(reservas_activas)), sublabel="pendientes de envío"),         unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# Tabla de saldos por proyecto
if saldos:
    filas = []
    for addr, d in saldos.items():
        disp_color  = "🟢" if d["disponible"] > 0 else "🔴"
        precio_otc  = precios_otc.get(addr, {}).get("precio_otc") or d["precio_emision"]
        filas.append({
            "":               disp_color,
            "Proyecto":       d["nombre"],
            "ID":             d["id"],
            "Fin estimado":   d.get("fecha_fin", "—"),
            "Tipo renta":     d.get("tipo_renta", "—"),
            "Divisa":         d["divisa"],
            "P. emisión":     d["precio_emision"],
            "P. OTC mín.":    precio_otc,
            "En custodia":    d["saldo"],
            "Reservados":     d["reservado"],
            "Disponibles":    d["disponible"],
            "Estado":         d["estado"],
            "Ubicación":      d["ubicacion"],
            "_addr":          addr,
        })
    df_saldos = pd.DataFrame(filas)

    def color_disponible(val):
        if val == 0:   return "color:#dc2626;font-weight:600"
        if val > 0:    return "color:#16a34a;font-weight:600"
        return ""

    def color_reservado(val):
        if val > 0:    return "color:#d97706;font-weight:600"
        return ""

    def color_precio_otc(val):
        return "color:#2563eb;font-weight:600"

    styled = (
        df_saldos.drop(columns=["_addr"])
        .style
        .applymap(color_disponible,  subset=["Disponibles"])
        .applymap(color_reservado,   subset=["Reservados"])
        .applymap(color_precio_otc,  subset=["P. OTC mín."])
        .format({
            "En custodia": "{:,.3f}", "Reservados": "{:,.3f}",
            "Disponibles": "{:,.3f}", "P. emisión": "{:,.2f}", "P. OTC mín.": "{:,.2f}",
        })
    )
    st.dataframe(styled, hide_index=True, use_container_width=True)
else:
    st.info("Sin tokens Reental en la wallet OTC en este momento.")

# ── Ofertas de terceros activas ───────────────────────────────────────────────

ofertas_activas = [o for o in load_ofertas() if o.get("estado") == "activa"]

if ofertas_activas:
    st.markdown("#### 👤 Tokens de terceros publicados para venta")
    st.caption("Estas ofertas son de inversores particulares y NO se suman al inventario de Reental.")

    filas_of = []
    for o in ofertas_activas:
        addr       = o["token_address"].lower()
        proj       = project_by_addr.get(addr, {})
        saldo_real = fetch_token_balance(o["wallet_inversor"].lower(), addr, API_KEY)
        n_oferta   = float(o["n_tokens"])
        fecha_fin  = proj.get("fecha_fin")

        if saldo_real < 0:
            alerta = "⚠️ Error"
        elif saldo_real < n_oferta - 0.001:
            alerta = "🔴"
        else:
            alerta = "🟢"

        filas_of.append({
            "":              alerta,
            "Proyecto":      o["proyecto_nombre"],
            "ID":            o["proyecto_id"],
            "Inversor":      o["inversor"],
            "Comercial":     o["comercial"],
            "Ubicación":     proj.get("ubicacion", "—"),
            "Estado":        proj.get("estado", "—"),
            "Fin estimado":  fecha_fin.strftime("%Y/%m") if fecha_fin else "—",
            "Tipo renta":    proj.get("tipo_renta", "—"),
            "Divisa":        o["divisa"],
            "P. emisión":    proj.get("precio_emision") or 0,
            "P. OTC mín.":   o["precio_venta"],
            "En oferta":     n_oferta,
            "Saldo real":    max(0.0, saldo_real) if saldo_real >= 0 else None,
            "Reservados":    0.0,
            "Disponibles":   n_oferta,
        })

    def color_saldo_real(val):
        if val is None: return "color:#94a3b8"
        return ""

    df_of = pd.DataFrame(filas_of)
    st.dataframe(
        df_of.style
        .applymap(color_saldo_real,  subset=["Saldo real"])
        .applymap(color_precio_otc,  subset=["P. OTC mín."])
        .applymap(color_reservado,   subset=["Reservados"])
        .applymap(color_disponible,  subset=["Disponibles"])
        .format({
            "P. emisión":  "{:,.2f}",
            "P. OTC mín.": "{:,.2f}",
            "En oferta":   "{:,.3f}",
            "Saldo real":  lambda v: f"{v:,.3f}" if v is not None else "—",
            "Reservados":  "{:,.3f}",
            "Disponibles": "{:,.3f}",
        }),
        hide_index=True, use_container_width=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 2 — Gestión de precios OTC (solo admin)
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
with st.expander("🔐 Gestión de precios OTC mínimos (acceso administrador)", expanded=False):
    pin_input = st.text_input("PIN de administrador", type="password", key="otc_pin")
    pin_ok    = pin_input == OTC_ADMIN_PIN and pin_input != ""

    if pin_input and not pin_ok:
        st.error("PIN incorrecto.")

    if pin_ok:
        st.success("Acceso concedido. Edita los precios mínimos de venta por proyecto.")
        st.caption(
            "El precio mínimo es el precio por debajo del cual los comerciales no pueden registrar una reserva. "
            "Si no se define, se usa el precio de emisión del proyecto como mínimo."
        )
        autor = st.text_input("Tu nombre (quedará registrado en el cambio)", key="otc_precio_autor")
        for addr, d in sorted(saldos.items(), key=lambda x: x[1]["nombre"]):
            precio_actual = precios_otc.get(addr, {}).get("precio_otc") or d["precio_emision"]
            updated_info  = precios_otc.get(addr, {})
            col_n, col_p, col_g, col_i = st.columns([3, 2, 1, 3])
            col_n.markdown(f"**{d['nombre']}** `{d['id']}` — {d['divisa']}")
            new_price = col_p.number_input(
                "Precio mínimo OTC",
                min_value=0.0, value=float(precio_actual),
                step=0.01, format="%.2f",
                key=f"precio_otc_{addr}",
                label_visibility="collapsed",
            )
            if col_g.button("💾", key=f"save_precio_{addr}", help="Guardar precio"):
                if not autor.strip():
                    st.warning("Indica tu nombre antes de guardar.")
                else:
                    precios_otc[addr] = {
                        "precio_otc":  new_price,
                        "updated_at":  datetime.utcnow().strftime("%d/%m/%Y %H:%M"),
                        "updated_by":  autor.strip(),
                    }
                    save_precios_otc(precios_otc)
                    st.success(f"✅ Precio de {d['nombre']} actualizado a {new_price:.2f} {d['divisa']}")
                    st.rerun()
            if updated_info:
                col_i.caption(f"Último cambio: {updated_info.get('updated_at','')} por {updated_info.get('updated_by','')}")


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 3b — Publicar oferta de tercero
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
with st.expander("📢 Publicar oferta de token de tercero", expanded=False):
    st.caption("Registra la intención de venta de un inversor para que el equipo comercial pueda gestionarla.")

    # Solo tokens activos (no cerrados)
    tokens_activos = master_df[master_df["fuente"].isin(["lanzamiento", "en_marcha"])] if not master_df.empty else pd.DataFrame()
    if tokens_activos.empty:
        st.info("No hay tokens activos disponibles en el catálogo.")
    else:
        opciones_token_tercero = {
            f"{row['nombre']} ({row['id']})": row["token_address"]
            for _, row in tokens_activos.sort_values("nombre").iterrows()
            if row.get("token_address")
        }

        of1, of2 = st.columns(2)
        of_comercial = of1.text_input("Comercial *", placeholder="Nombre del comercial", key="of_comercial")
        of_inversor  = of2.text_input("Inversor *",  placeholder="Nombre del inversor",  key="of_inversor")

        of3, of4 = st.columns(2)
        of_email  = of3.text_input("Email del inversor en Reental *", placeholder="correo@ejemplo.com", key="of_email")
        of_wallet = of4.text_input("Wallet del inversor *", placeholder="0x…", key="of_wallet")

        of5, of6, of7 = st.columns(3)
        of_token_sel = of5.selectbox("Token a vender *", list(opciones_token_tercero.keys()), key="of_token")
        of_addr      = opciones_token_tercero[of_token_sel]
        of_proj      = project_by_addr.get(of_addr, {})
        of_divisa    = of_proj.get("divisa", "EUR")
        of_ntokens   = of6.number_input("Cantidad de tokens *", min_value=0.001, value=1.0, step=1.0, format="%.3f", key="of_ntokens")
        of_precio    = of7.number_input(f"Precio por token ({of_divisa}) *", min_value=0.0,
                                         value=float(of_proj.get("precio_emision") or 0),
                                         step=0.01, format="%.2f", key="of_precio")

        of_total_divisa = of_ntokens * of_precio
        of_total_eur    = of_total_divisa / eur_usd if of_divisa == "USD" else of_total_divisa
        of_total_usd    = of_total_divisa * eur_usd if of_divisa == "EUR" else of_total_divisa

        st.markdown(
            f'<div style="background:#fefce8;border:1px solid #fde68a;border-radius:10px;'
            f'padding:12px 20px;margin:10px 0;font-size:0.88rem;color:#78350f;">'
            f'💱 Total oferta: <b>{of_total_eur:,.2f} EUR</b> · <b>{of_total_usd:,.2f} USD</b> &nbsp;|&nbsp; '
            f'{of_ntokens:.3f} tokens × {of_precio:.2f} {of_divisa} · Cambio: 1 EUR = {eur_usd:.4f} USD</div>',
            unsafe_allow_html=True,
        )

        of_wallet_clean = of_wallet.strip().lower()
        saldo_real_of   = None
        if of_wallet_clean.startswith("0x") and len(of_wallet_clean) == 42:
            saldo_real_of = fetch_token_balance(of_wallet_clean, of_addr, API_KEY)
            if saldo_real_of < 0:
                st.warning("⚠️ No se pudo verificar el saldo en blockchain. Se publicará la oferta igualmente.")
            elif saldo_real_of < of_ntokens - 0.001:
                st.error(
                    f"⚠️ El inversor solo tiene **{saldo_real_of:.3f} tokens** de {of_proj.get('nombre','')} "
                    f"en esa wallet, pero intenta ofrecer **{of_ntokens:.3f}**. "
                    f"Confirma con el inversor antes de publicar."
                )
            else:
                st.success(f"✅ Saldo verificado: {saldo_real_of:.3f} tokens en wallet.")

        if st.button("📢 Publicar oferta", type="primary", use_container_width=True, key="of_guardar"):
            errores_of = []
            if not of_comercial.strip(): errores_of.append("El campo Comercial es obligatorio.")
            if not of_inversor.strip():  errores_of.append("El campo Inversor es obligatorio.")
            if not of_email.strip():     errores_of.append("El campo Email es obligatorio.")
            if not (of_wallet_clean.startswith("0x") and len(of_wallet_clean) == 42):
                errores_of.append("La wallet del inversor no es una dirección válida.")
            for e in errores_of:
                st.error(e)
            if not errores_of:
                nueva_oferta = {
                    "id":                f"OFR-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
                    "comercial":         of_comercial.strip(),
                    "inversor":          of_inversor.strip(),
                    "email_inversor":    of_email.strip(),
                    "wallet_inversor":   of_wallet_clean,
                    "token_address":     of_addr,
                    "proyecto_nombre":   of_proj.get("nombre", "—"),
                    "proyecto_id":       of_proj.get("id", "—"),
                    "n_tokens":          round(float(of_ntokens), 6),
                    "precio_venta":      float(of_precio),
                    "divisa":            of_divisa,
                    "total_eur":         round(of_total_eur, 2),
                    "total_usd":         round(of_total_usd, 2),
                    "eur_usd_rate":      eur_usd,
                    "saldo_verificado":  saldo_real_of if saldo_real_of is not None and saldo_real_of >= 0 else None,
                    "fecha_publicacion": datetime.utcnow().strftime("%d/%m/%Y %H:%M"),
                    "estado":            "activa",
                }
                todas_ofertas = load_ofertas()
                todas_ofertas.append(nueva_oferta)
                save_ofertas(todas_ofertas)
                st.success(
                    f"✅ Oferta **{nueva_oferta['id']}** publicada — "
                    f"{of_ntokens:.3f} tokens de {of_proj.get('nombre','')} "
                    f"por {of_inversor.strip()} a {of_precio:.2f} {of_divisa}/token"
                )
                st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 3 — Nueva reserva
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
_exp_reserva = st.expander("➕ Crear nueva reserva", expanded=False)
with _exp_reserva:

    proyectos_con_saldo = {
        addr: d for addr, d in saldos.items() if d["disponible"] > 0
    }

    PREFIJO_OTC     = "🏢 Reental OTC"
    PREFIJO_TERCERO = "👤 Tercero"

    opciones_combinadas = {}

    for addr, d in sorted(proyectos_con_saldo.items(), key=lambda x: x[1]["nombre"]):
        label = f"{PREFIJO_OTC} — {d['nombre']} ({d['id']}) · {d['disponible']:,.3f} disp."
        opciones_combinadas[label] = {
            "tipo":           "reental",
            "token_address":  addr,
            "nombre":         d["nombre"],
            "id":             d["id"],
            "divisa":         d["divisa"],
            "disponible":     d["disponible"],
            "precio_ref":     precios_otc.get(addr, {}).get("precio_otc") or d["precio_emision"] or 0.0,
            "precio_min":     precios_otc.get(addr, {}).get("precio_otc") or d["precio_emision"] or 0.0,
            "wallet_origen":  OTC_WALLET,
        }

    for o in sorted(ofertas_activas, key=lambda x: x["proyecto_nombre"]):
        label = f"{PREFIJO_TERCERO} — {o['proyecto_nombre']} ({o['proyecto_id']}) · {o['n_tokens']:,.3f} disp. · {o['inversor']}"
        opciones_combinadas[label] = {
            "tipo":           "tercero",
            "oferta_id":      o["id"],
            "token_address":  o["token_address"],
            "nombre":         o["proyecto_nombre"],
            "id":             o["proyecto_id"],
            "divisa":         o["divisa"],
            "disponible":     float(o["n_tokens"]),
            "precio_ref":     float(o["precio_venta"]),
            "precio_min":     0.0,
            "wallet_origen":  o["wallet_inversor"],
            "inversor_origen": o["inversor"],
            "email_origen":   o.get("email_inversor", ""),
        }

    hay_opciones = bool(opciones_combinadas)

    if not hay_opciones:
        st.warning("No hay tokens disponibles para reservar en este momento (ni de Reental ni de terceros).")
    else:
        st.markdown("**Datos de la reserva**")
        fc1, fc2 = st.columns(2)
        proyecto_sel = fc1.selectbox("Proyecto *", list(opciones_combinadas.keys()), key="nr_proyecto")
        comercial    = fc2.text_input("Comercial *", placeholder="Nombre del comercial", key="nr_comercial")

        fd1, fd2, fd3 = st.columns([2, 2, 1])
        inversor        = fd1.text_input("Inversor *", placeholder="Nombre del inversor", key="nr_inversor")
        sin_wallet      = fd3.checkbox("Sin wallet\n(propuesta)", key="nr_sin_wallet")
        wallet_inv      = fd2.text_input(
            "Wallet inversor" + (" *" if not sin_wallet else " (pendiente)"),
            placeholder="0x…" if not sin_wallet else "Se rellenará cuando el inversor confirme",
            key="nr_wallet",
            disabled=sin_wallet,
        )

        sel           = opciones_combinadas[proyecto_sel]
        addr_sel      = sel["token_address"]
        disp_proyecto = sel["disponible"]
        divisa_proj   = sel["divisa"]
        precio_otc_min = sel["precio_min"]

        if sel["tipo"] == "tercero":
            st.info(
                f"👤 **Oferta de tercero** — Token del inversor **{sel.get('inversor_origen','')}** "
                f"({sel.get('email_origen','')}) · Wallet origen: `{sel.get('wallet_origen','')}`"
            )

        precio_min_efectivo = sel["precio_ref"] if sel["tipo"] == "tercero" else precio_otc_min

        fn1, fn2, fn3 = st.columns(3)
        _disp_f   = float(disp_proyecto)
        _ntok_min = 0.001

        if _disp_f < _ntok_min:
            fn1.error(f"Saldo disponible insuficiente ({_disp_f:.6f} tokens). No se puede crear una reserva.")
            n_tokens = 0.0
        else:
            n_tokens = fn1.number_input(
                f"Nº tokens * (máx. {_disp_f:,.3f} disp.)",
                min_value=_ntok_min,
                max_value=_disp_f,
                value=max(_ntok_min, min(1.0, _disp_f)),
                step=1.0, format="%.3f", key="nr_ntokens",
            )
            fn1.button(
                f"↩ Usar disponible ({_disp_f:,.3f})",
                key="nr_usar_disponible",
                on_click=lambda v=_disp_f: st.session_state.update({"nr_ntokens": v}),
                use_container_width=True,
            )
        precio_label = f"Precio acordado ({divisa_proj}) *"
        if precio_min_efectivo > 0:
            precio_label += f" — mín. {precio_min_efectivo:.2f}"
        precio_acordado = fn2.number_input(
            precio_label,
            min_value=precio_min_efectivo if precio_min_efectivo > 0 else 0.0,
            value=max(float(precio_min_efectivo), float(sel["precio_ref"])),
            step=0.01, format="%.2f", key="nr_precio",
        )
        notas = fn3.text_input("Notas (opcional)", placeholder="Observaciones…", key="nr_notas")

        total_en_divisa = float(n_tokens) * float(precio_acordado)
        total_usd = total_en_divisa if divisa_proj == "USD" else total_en_divisa * eur_usd
        total_eur = total_en_divisa if divisa_proj == "EUR" else total_en_divisa / eur_usd

        bg_calc = "#f0f9ff" if sel["tipo"] == "reental" else "#fefce8"
        br_calc = "#bae6fd" if sel["tipo"] == "reental" else "#fde68a"
        tx_calc = "#0c4a6e" if sel["tipo"] == "reental" else "#78350f"
        st.markdown(
            f'<div style="background:{bg_calc};border:1px solid {br_calc};border-radius:10px;'
            f'padding:14px 20px;margin:12px 0;">'
            f'<div style="font-size:0.7rem;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.05em;">Total operación</div>'
            f'<div style="font-size:1.5rem;font-weight:700;color:{tx_calc};">'
            f'{total_eur:,.2f} EUR &nbsp;·&nbsp; {total_usd:,.2f} USD</div>'
            f'<div style="font-size:0.72rem;color:#64748b;">'
            f'{n_tokens} tokens × {precio_acordado:.2f} {divisa_proj} &nbsp;|&nbsp; '
            f'1 EUR = {eur_usd:.4f} USD <em>({eur_usd_fecha} UTC)</em></div></div>',
            unsafe_allow_html=True,
        )

        if st.button("💾 Guardar reserva", type="primary", use_container_width=True, key="nr_guardar", disabled=(_disp_f < _ntok_min)):
            errores = []
            if not comercial.strip():
                errores.append("El campo Comercial es obligatorio.")
            if not inversor.strip():
                errores.append("El campo Inversor es obligatorio.")
            if sin_wallet:
                wallet_inv_clean = "pendiente"
            else:
                wallet_inv_clean = wallet_inv.strip().lower()
                if not (wallet_inv_clean.startswith("0x") and len(wallet_inv_clean) == 42):
                    errores.append("La wallet del inversor debe ser una dirección Ethereum válida (0x… 42 caracteres).")

            if sel["tipo"] == "reental":
                disp_actual = saldos.get(addr_sel, {}).get("disponible", 0)
                if float(n_tokens) > disp_actual:
                    errores.append(
                        f"No hay suficientes tokens disponibles. "
                        f"Has intentado reservar **{n_tokens:,}** pero solo quedan "
                        f"**{disp_actual:,.3f} disponibles**."
                    )
            else:
                saldo_tercero = fetch_token_balance(sel["wallet_origen"], addr_sel, API_KEY)
                if saldo_tercero >= 0 and float(n_tokens) > saldo_tercero:
                    errores.append(
                        f"El inversor solo tiene **{saldo_tercero:.3f} tokens** disponibles en su wallet. "
                        f"No puedes reservar {n_tokens:.3f}."
                    )

            if precio_min_efectivo > 0 and float(precio_acordado) < precio_min_efectivo:
                origen_precio = "ofertado por el tercero" if sel["tipo"] == "tercero" else "mínimo OTC"
                errores.append(
                    f"El precio acordado ({precio_acordado:.2f} {divisa_proj}) es inferior al precio {origen_precio} "
                    f"de **{precio_min_efectivo:.2f} {divisa_proj}**."
                )
            if errores:
                for e in errores:
                    st.error(e)
            else:
                nueva = {
                    "id":               f"RES-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
                    "tipo_origen":      sel["tipo"],
                    "oferta_id":        sel.get("oferta_id"),
                    "token_address":    addr_sel,
                    "proyecto_nombre":  sel["nombre"],
                    "proyecto_id":      sel["id"],
                    "comercial":        comercial.strip(),
                    "inversor":         inversor.strip(),
                    "wallet_inversor":  wallet_inv_clean,
                    "wallet_pendiente": sin_wallet,
                    "n_tokens":         float(n_tokens),
                    "precio_acordado":  float(precio_acordado),
                    "divisa":           divisa_proj,
                    "total_eur":        round(total_eur, 2),
                    "total_usd":        round(total_usd, 2),
                    "eur_usd_rate":     eur_usd,
                    "eur_usd_fecha":    eur_usd_fecha,
                    "fecha_reserva":    datetime.utcnow().strftime("%d/%m/%Y %H:%M"),
                    "estado":           "activa",
                    "notas":            notas.strip(),
                    "tx_envio":         None,
                    "fecha_envio":      None,
                }
                reservas_all = load_reservas()
                reservas_all.append(nueva)
                save_reservas(reservas_all)
                for _k in ["nr_proyecto", "nr_comercial", "nr_inversor", "nr_wallet",
                            "nr_ntokens", "nr_precio", "nr_notas", "nr_sin_wallet"]:
                    st.session_state.pop(_k, None)
                aviso = st.success(
                    f"✅ Reserva anotada — {n_tokens} tokens de **{sel['nombre']}** "
                    f"para **{inversor.strip()}** · "
                    f"{total_eur:,.2f} EUR / {total_usd:,.2f} USD"
                )
                time.sleep(2)
                st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 3 — Gestión de reservas
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("📋 Reservas")

reservas_all = load_reservas()

# ── Filtros ───────────────────────────────────────────────────────────────
proyectos_unicos   = sorted({r["proyecto_nombre"] for r in reservas_all})
comerciales_unicos = sorted({r.get("comercial", "") for r in reservas_all if r.get("comercial")})
inversores_unicos  = sorted({r.get("inversor", "") for r in reservas_all if r.get("inversor")})

fx1, fx2, fx3 = st.columns(3)
f_proyecto  = fx1.selectbox("🏠 Filtrar por proyecto",  ["Todos"] + proyectos_unicos,  key="rf_proyecto")
f_comercial = fx2.selectbox("👤 Filtrar por comercial", ["Todos"] + comerciales_unicos, key="rf_comercial")
f_inversor  = fx3.selectbox("🤝 Filtrar por inversor",  ["Todos"] + inversores_unicos,  key="rf_inversor")

def aplicar_filtros(lista):
    if f_proyecto  != "Todos": lista = [r for r in lista if r["proyecto_nombre"] == f_proyecto]
    if f_comercial != "Todos": lista = [r for r in lista if r.get("comercial") == f_comercial]
    if f_inversor  != "Todos": lista = [r for r in lista if r.get("inversor") == f_inversor]
    return lista

# ── Navegación por botones destacados ────────────────────────────────────
if "reservas_tab" not in st.session_state:
    st.session_state["reservas_tab"] = "activas"

_tabs_def = [
    ("activas",     "🟡 Activas"),
    ("completadas", "✅ Completadas"),
    ("canceladas",  "❌ Canceladas"),
    ("ofertas",     "📢 Ofertas de terceros"),
]

_active_tab = st.session_state["reservas_tab"]

_tab_cols = st.columns(len(_tabs_def))
for (_tid, _tlabel), _col in zip(_tabs_def, _tab_cols):
    _btn_type = "primary" if _tid == _active_tab else "secondary"
    if _col.button(_tlabel, key=f"tab_btn_{_tid}", use_container_width=True, type=_btn_type):
        st.session_state["reservas_tab"] = _tid
        st.rerun()

st.markdown("---")

def render_reservas(lista: list, editable: bool = False):
    if not lista:
        st.info("No hay reservas en este estado.")
        return

    _todas_ofs_cache = load_ofertas()

    for r in lista:
        # Detectar reservas activas con tokens ya no disponibles
        sin_disponibilidad = False
        if r["estado"] == "activa":
            if r.get("tipo_origen") == "tercero":
                oferta = next((o for o in _todas_ofs_cache if o["id"] == r.get("oferta_id")), None)
                if oferta is None or oferta.get("estado") != "activa":
                    sin_disponibilidad = True
            else:
                addr_r  = r.get("token_address", "").lower()
                info_r  = saldos.get(addr_r, {})
                if info_r.get("reservado", 0) > info_r.get("saldo", 0):
                    sin_disponibilidad = True

        border_style = "border:2px solid #7f1d1d;" if sin_disponibilidad else "border:1px solid #e2e8f0;"
        with st.container():
            if sin_disponibilidad:
                st.markdown(
                    '<div style="background:#fef2f2;' + border_style + 'border-radius:8px;'
                    'padding:6px 14px;margin-bottom:6px;font-size:0.82rem;'
                    'color:#991b1b;font-weight:600;">'
                    '⚠️ ATENCIÓN: Los tokens de esta reserva ya no están disponibles '
                    '(oferta eliminada o saldo agotado en wallet OTC)</div>',
                    unsafe_allow_html=True,
                )

            es_parcial_badge  = r["estado"] == "completada" and r.get("envio_parcial", False)
            es_tercero        = r.get("tipo_origen") == "tercero"
            es_propuesta      = r.get("wallet_pendiente", False)
            badge_color = (
                "#b45309" if es_parcial_badge else
                {"activa": "#d97706", "completada": "#16a34a", "cancelada": "#94a3b8"}.get(r["estado"], "#64748b")
            )
            badge_label = (
                "⚠️ PARCIAL" if es_parcial_badge else
                {"activa": "ACTIVA", "completada": "COMPLETADA", "cancelada": "CANCELADA"}.get(r["estado"], r["estado"].upper())
            )
            origen_badge    = ' <span style="background:#7c3aed;color:white;border-radius:4px;padding:1px 6px;font-size:0.65rem;font-weight:700;">👤 TERCERO</span>' if es_tercero else ""
            propuesta_badge = ' <span style="background:#0369a1;color:white;border-radius:4px;padding:1px 6px;font-size:0.65rem;font-weight:700;">📋 PROPUESTA</span>' if es_propuesta else ""

            header_html = (
                f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">'
                f'<span style="background:{badge_color};color:white;border-radius:5px;'
                f'padding:2px 8px;font-size:0.7rem;font-weight:700;">{badge_label}</span>'
                f'{origen_badge}{propuesta_badge}'
                f'<span style="font-weight:700;font-size:1rem;">{r["proyecto_nombre"]}</span>'
                f'<span style="color:#94a3b8;font-size:0.85rem;">{r["id"]}</span>'
                f'</div>'
            )
            st.markdown(header_html, unsafe_allow_html=True)

            i1, i2, i3, i4, i5, i6 = st.columns([2, 2, 2, 1, 1, 2])
            i1.markdown(f"**Inversor**  \n{r['inversor']}")
            i2.markdown(f"**Comercial**  \n{r['comercial']}")
            _w = r["wallet_inversor"]
            _w_display = "⏳ Pendiente" if r.get("wallet_pendiente") else f"`{_w[:18]}…`"
            i3.markdown(f"**Wallet inversor**  \n{_w_display}")
            i4.markdown(f"**Tokens**  \n{r['n_tokens']:,}")
            i5.markdown(f"**Precio**  \n{r['precio_acordado']:,.2f} {r['divisa']}")
            if r["estado"] == "completada" and r.get("fecha_envio"):
                i6.markdown(f"**Completado el**  \n{r['fecha_envio']}")
            else:
                i6.markdown(f"**Reservado el**  \n{r['fecha_reserva']}")

            # Totales EUR/USD si están guardados
            if r.get("total_eur") or r.get("total_usd"):
                t_eur  = r.get("total_eur", 0)
                t_usd  = r.get("total_usd", 0)
                tc     = r.get("eur_usd_rate", "—")
                tc_fch = r.get("eur_usd_fecha", "")
                st.markdown(
                    f'<div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;'
                    f'padding:8px 16px;font-size:0.85rem;color:#0c4a6e;margin:4px 0;">'
                    f'💱 <b>{t_eur:,.2f} EUR</b> · <b>{t_usd:,.2f} USD</b> &nbsp;|&nbsp; '
                    f'Cambio aplicado: 1 EUR = {tc} USD <em>({tc_fch})</em></div>',
                    unsafe_allow_html=True,
                )

            if r.get("notas"):
                st.caption(f"📝 {r['notas']}")

            if r["estado"] == "completada":
                tx_url       = POLYSCAN_TX_URL + r["tx_envio"] if r.get("tx_envio") else None
                tx_link      = f"[Ver TX en Polygonscan ↗]({tx_url})" if tx_url else "TX no disponible"
                enviados     = r.get("tokens_enviados")
                reservados   = r.get("n_tokens", "—")
                es_parcial   = r.get("envio_parcial", False)
                nota_envio   = r.get("nota_envio")

                resumen = (
                    f"**Enviados:** {enviados:.3f} tokens · **Reservados:** {reservados} tokens · "
                    f"{tx_link} · {r.get('fecha_envio', '—')}"
                    if enviados is not None
                    else f"{tx_link} · {r.get('fecha_envio', '—')}"
                )

                if es_parcial:
                    st.warning(f"⚠️ Envío parcial — {resumen}")
                    if nota_envio:
                        st.caption(nota_envio)
                else:
                    st.success(f"✅ Completada — {resumen}")
                    if nota_envio:
                        st.caption(nota_envio)

            if editable:
                btn1, btn2, btn3, btn4, _ = st.columns([1, 1, 1, 1.4, 3])

                with btn1:
                    if st.button("✏️ Editar", key=f"edit_{r['id']}"):
                        st.session_state[f"editing_{r['id']}"] = True

                with btn2:
                    if st.button("❌ Cancelar", key=f"cancel_{r['id']}"):
                        st.session_state[f"confirm_cancel_{r['id']}"] = True

                with btn3:
                    if st.button("🗑️ Eliminar", key=f"delete_{r['id']}"):
                        st.session_state[f"confirm_delete_{r['id']}"] = True

                with btn4:
                    if st.button("✅ Marcar completada", key=f"force_{r['id']}"):
                        st.session_state[f"confirm_force_{r['id']}"] = True
                    if st.session_state.get(f"confirm_force_{r['id']}"):
                        st.warning(
                            "Confirma que la operación se ha ejecutado fuera de la plataforma. "
                            "La reserva pasará a Completada sin hash de TX automático."
                        )
                        cf1, cf2 = st.columns(2)
                        tx_manual = cf1.text_input("Hash de TX (opcional)", placeholder="0x…", key=f"tx_manual_{r['id']}")
                        if cf1.button("Confirmar", key=f"yes_force_{r['id']}", type="primary"):
                            all_r = load_reservas()
                            for item in all_r:
                                if item["id"] == r["id"]:
                                    item["estado"]          = "completada"
                                    item["envio_parcial"]   = False
                                    item["tokens_enviados"] = float(item.get("n_tokens", 0))
                                    item["fecha_envio"]     = datetime.utcnow().strftime("%d/%m/%Y %H:%M")
                                    item["tx_envio"]        = tx_manual.strip() or None
                                    item["nota_envio"]      = "Completada manualmente por el equipo comercial."
                                    break
                            save_reservas(all_r)
                            st.session_state.pop(f"confirm_force_{r['id']}", None)
                            st.rerun()
                        if cf2.button("Cancelar", key=f"no_force_{r['id']}"):
                            st.session_state.pop(f"confirm_force_{r['id']}", None)
                            st.rerun()

                # Formulario de edición inline
                if st.session_state.get(f"editing_{r['id']}"):
                    addr_tok    = r["token_address"].lower()
                    disp_edit   = float(saldos.get(addr_tok, {}).get("disponible", 0)) + float(r["n_tokens"])
                    precio_min  = float(precios_otc.get(addr_tok, {}).get("precio_otc") or saldos.get(addr_tok, {}).get("precio_emision") or 0.0)
                    divisa_r    = r.get("divisa", "EUR")

                    col_e1, col_e2, col_e3 = st.columns(3)
                    new_tokens = col_e1.number_input(
                        "Nuevo nº de tokens",
                        min_value=0.001, max_value=disp_edit,
                        value=float(r["n_tokens"]),
                        step=1.0, format="%.3f",
                        key=f"edit_tok_{r['id']}",
                    )
                    new_precio = col_e2.number_input(
                        f"Precio ({divisa_r}) — mín. {precio_min:.2f}",
                        min_value=precio_min,
                        value=max(float(r["precio_acordado"]), precio_min),
                        step=0.01, format="%.2f",
                        key=f"edit_precio_{r['id']}",
                    )
                    new_notas = col_e3.text_input("Notas", value=r.get("notas", ""), key=f"edit_notas_{r['id']}")

                    # Campo wallet (editable si es propuesta pendiente o si quiere corregirla)
                    wallet_actual    = r.get("wallet_inversor", "")
                    es_pendiente_w   = r.get("wallet_pendiente", False)
                    wallet_label     = "Wallet inversor" + (" (⏳ pendiente — rellena ahora)" if es_pendiente_w else "")
                    new_wallet       = st.text_input(
                        wallet_label,
                        value="" if es_pendiente_w else wallet_actual,
                        placeholder="0x…",
                        key=f"edit_wallet_{r['id']}",
                    )

                    col_s, col_c = st.columns(2)
                    guardar       = col_s.button("💾 Guardar cambios", type="primary", use_container_width=True, key=f"edit_save_{r['id']}")
                    cancelar_edit = col_c.button("Cancelar", use_container_width=True, key=f"edit_cancel_{r['id']}")

                    if guardar:
                        errores_edit = []
                        wallet_limpia = new_wallet.strip().lower()
                        # Validar wallet si se ha rellenado o era obligatoria
                        wallet_guardada  = wallet_actual
                        pendiente_nueva  = es_pendiente_w
                        if wallet_limpia:
                            if not (wallet_limpia.startswith("0x") and len(wallet_limpia) == 42):
                                errores_edit.append("La wallet introducida no es válida (debe empezar por 0x y tener 42 caracteres).")
                            else:
                                wallet_guardada = wallet_limpia
                                pendiente_nueva = False
                        if errores_edit:
                            for e in errores_edit:
                                st.error(e)
                        else:
                            all_r = load_reservas()
                            for item in all_r:
                                if item["id"] == r["id"]:
                                    item["n_tokens"]         = float(new_tokens)
                                    item["precio_acordado"]  = float(new_precio)
                                    item["notas"]            = new_notas.strip()
                                    item["wallet_inversor"]  = wallet_guardada
                                    item["wallet_pendiente"] = pendiente_nueva
                                    break
                            save_reservas(all_r)
                            st.session_state.pop(f"editing_{r['id']}", None)
                            st.rerun()
                    if cancelar_edit:
                        st.session_state.pop(f"editing_{r['id']}", None)
                        st.rerun()

                # Confirmación cancelación
                if st.session_state.get(f"confirm_cancel_{r['id']}"):
                    st.warning(f"¿Cancelar la reserva **{r['id']}** de {r['inversor']}? La reserva quedará marcada como cancelada pero permanecerá en el historial.")
                    cc1, cc2, _ = st.columns([1, 1, 6])
                    if cc1.button("Sí, cancelar", key=f"yes_cancel_{r['id']}", type="primary"):
                        all_r = load_reservas()
                        for item in all_r:
                            if item["id"] == r["id"]:
                                item["estado"] = "cancelada"
                                break
                        save_reservas(all_r)
                        st.session_state.pop(f"confirm_cancel_{r['id']}", None)
                        st.rerun()
                    if cc2.button("No", key=f"no_cancel_{r['id']}"):
                        st.session_state.pop(f"confirm_cancel_{r['id']}", None)
                        st.rerun()

                # Confirmación eliminación
                if st.session_state.get(f"confirm_delete_{r['id']}"):
                    st.error(f"¿Eliminar definitivamente la reserva **{r['id']}**? Esta acción no se puede deshacer.")
                    cd1, cd2, _ = st.columns([1, 1, 6])
                    if cd1.button("Sí, eliminar", key=f"yes_delete_{r['id']}", type="primary"):
                        all_r = load_reservas()
                        all_r = [item for item in all_r if item["id"] != r["id"]]
                        save_reservas(all_r)
                        st.session_state.pop(f"confirm_delete_{r['id']}", None)
                        st.rerun()
                    if cd2.button("No", key=f"no_delete_{r['id']}"):
                        st.session_state.pop(f"confirm_delete_{r['id']}", None)
                        st.rerun()

        st.divider()

def _sort_key(r):
    try:
        return datetime.strptime(r["fecha_reserva"], "%d/%m/%Y %H:%M")
    except Exception:
        return datetime.min

def _sort_key_completada(r):
    # Ordenar por fecha de completado (fecha_envio), tanto si se detectó
    # automáticamente en blockchain como si se marcó manualmente.
    # Fallback a fecha_reserva si por alguna razón no existe fecha_envio.
    for campo in ("fecha_envio", "fecha_reserva"):
        try:
            return datetime.strptime(r[campo], "%d/%m/%Y %H:%M")
        except Exception:
            continue
    return datetime.min

activas     = aplicar_filtros(sorted([r for r in reservas_all if r["estado"] == "activa"],     key=_sort_key,            reverse=True))
completadas = aplicar_filtros(sorted([r for r in reservas_all if r["estado"] == "completada"], key=_sort_key_completada, reverse=True))
canceladas  = aplicar_filtros(sorted([r for r in reservas_all if r["estado"] == "cancelada"],  key=_sort_key,            reverse=True))

if _active_tab == "activas":
    if activas:
        for addr, d in saldos.items():
            if d["reservado"] > d["saldo"]:
                st.error(
                    f"⚠️ **{d['nombre']}**: tokens reservados ({d['reservado']:,.0f}) "
                    f"superan el saldo en custodia ({d['saldo']:,.0f}). Revisa las reservas."
                )
    render_reservas(activas, editable=True)

elif _active_tab == "completadas":
    render_reservas(completadas, editable=False)

elif _active_tab == "canceladas":
    render_reservas(canceladas, editable=False)

elif _active_tab == "ofertas":
    st.caption("Ofertas publicadas por inversores terceros. Elimínalas cuando el proceso finalice o el inversor las retire.")
    todas_ofertas_mgmt = load_ofertas()
    if not todas_ofertas_mgmt:
        st.info("No hay ofertas de terceros registradas.")
    else:
        for o in sorted(todas_ofertas_mgmt, key=lambda x: x.get("fecha_publicacion",""), reverse=True):
            estado_o    = o.get("estado", "activa")
            eliminada_o = estado_o == "eliminada"
            badge_o  = "🟡 ACTIVA" if estado_o == "activa" else "❌ ELIMINADA"
            color_o  = "#d97706"   if estado_o == "activa" else "#94a3b8"
            opacity  = "opacity:0.45;" if eliminada_o else ""

            st.markdown(
                f'<div style="{opacity}display:flex;align-items:center;gap:10px;margin-bottom:4px;">'
                f'<span style="background:{color_o};color:white;border-radius:5px;'
                f'padding:2px 8px;font-size:0.7rem;font-weight:700;">{badge_o}</span>'
                f'<span style="font-weight:700;">{o["proyecto_nombre"]}</span>'
                f'<span style="color:#94a3b8;font-size:0.85rem;">{o["id"]}</span></div>',
                unsafe_allow_html=True,
            )
            oc1, oc2, oc3, oc4, oc5, oc6 = st.columns([2, 2, 1, 1, 1, 1])
            oc1.markdown(f"**Inversor**  \n{o['inversor']}  \n{o.get('email_inversor','')}")
            oc2.markdown(f"**Comercial**  \n{o.get('comercial','—')}  \n`{o['wallet_inversor'][:14]}…`")
            oc3.markdown(f"**Tokens**  \n{o['n_tokens']:,.3f}")
            oc4.markdown(f"**Precio**  \n{o['precio_venta']:,.2f} {o['divisa']}")
            oc5.markdown(f"**Publicado**  \n{o['fecha_publicacion']}")

            # Saldo real en vivo (solo para activas, para no consumir llamadas API innecesarias)
            if not eliminada_o:
                saldo_v = fetch_token_balance(o["wallet_inversor"].lower(), o["token_address"].lower(), API_KEY)
                if saldo_v < 0:
                    oc6.warning("Sin datos")
                elif saldo_v < float(o["n_tokens"]) - 0.001:
                    oc6.error(f"⚠️ Solo {saldo_v:.3f} en wallet")
                else:
                    oc6.success(f"✅ {saldo_v:.3f} en wallet")
            else:
                oc6.markdown("<span style='color:#94a3b8;font-size:0.8rem;'>—</span>", unsafe_allow_html=True)

            if estado_o == "activa":
                bc1, bc2, _ = st.columns([1, 1, 6])
                if bc1.button("🗑️ Eliminar oferta", key=f"del_oferta_{o['id']}"):
                    st.session_state[f"confirm_del_oferta_{o['id']}"] = True
                if st.session_state.get(f"confirm_del_oferta_{o['id']}"):
                    st.error(f"¿Eliminar definitivamente la oferta **{o['id']}**?")
                    cx1, cx2, _ = st.columns([1, 1, 6])
                    if cx1.button("Sí, eliminar", key=f"yes_del_of_{o['id']}", type="primary"):
                        ofs = load_ofertas()
                        for _of in ofs:
                            if _of["id"] == o["id"]:
                                _of["estado"] = "eliminada"
                                break
                        save_ofertas(ofs)
                        st.session_state.pop(f"confirm_del_oferta_{o['id']}", None)
                        st.rerun()
                    if cx2.button("No", key=f"no_del_of_{o['id']}"):
                        st.session_state.pop(f"confirm_del_oferta_{o['id']}", None)
                        st.rerun()
            st.divider()


# ── Exportar reservas ─────────────────────────────────────────────────────────

st.markdown("---")
if reservas_all:
    export_rows = []
    for r in reservas_all:
        export_rows.append({
            "ID reserva":       r["id"],
            "Proyecto":         r["proyecto_nombre"],
            "Proyecto ID":      r["proyecto_id"],
            "Comercial":        r["comercial"],
            "Inversor":         r["inversor"],
            "Wallet inversor":  r["wallet_inversor"],
            "Tokens":           r["n_tokens"],
            "Precio acordado":  r["precio_acordado"],
            "Divisa":           r["divisa"],
            "Fecha reserva":    r["fecha_reserva"],
            "Estado":           r["estado"],
            "Notas":            r.get("notas", ""),
            "Fecha envío":      r.get("fecha_envio", ""),
            "TX envío":         r.get("tx_envio", ""),
        })
    csv_bytes = pd.DataFrame(export_rows).to_csv(index=False).encode("utf-8")
    st.download_button(
        "⬇️ Exportar todas las reservas (CSV)",
        data=csv_bytes,
        file_name=f"reservas_otc_{datetime.utcnow().strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )
