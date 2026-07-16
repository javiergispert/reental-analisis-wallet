"""
Funciones compartidas entre app.py y el simulador.
"""
import os
import io
import unicodedata
from datetime import datetime, date

import requests
import streamlit as st
import pandas as pd

# ── Constantes ────────────────────────────────────────────────────────────────

ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"
POLYGON_CHAIN_ID  = 137
ZERO_ADDRESS      = "0x0000000000000000000000000000000000000000"

GSHEET_MASTER_URL = os.getenv("GSHEET_CSV_URL", "")
GSHEET_P2P_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vQBD3VY1WO-MUn22MhdwbN3PKAuLQL5oGutMuZO2uwNc0CkMdr1UU9kjAozdM3U8njLlls6lMlarFG1"
    "/pub?gid=1247043615&single=true&output=csv"
)

ESTADO_NUEVOS    = {"FINANCIANDOSE", "NO LANZADO"}
ESTADO_EN_MARCHA = {"EN EXPLOTACION", "EN CONSTRUCCION", "EN REFORMA"}

# ── Utilidades de texto ───────────────────────────────────────────────────────

def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", str(text))
        if not unicodedata.combining(c)
    )


def parse_pct(val) -> float:
    """'9.62%' → 0.0962. Devuelve None si no es parseable."""
    try:
        s = str(val).strip().replace("%", "").replace(",", ".")
        if s.upper() in ("NAN", "CERRADO", ""):
            return None
        return float(s) / 100
    except Exception:
        return None


def parse_float_val(val) -> float:
    try:
        return float(str(val).strip().replace(",", "."))
    except Exception:
        return None


def parse_fecha_util(val: str) -> date:
    val = str(val).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(val, fmt).date()
        except Exception:
            pass
    return None


def add_months(dt: date, n: int) -> date:
    """Suma n meses a una fecha sin depender de dateutil."""
    month = dt.month - 1 + n
    year  = dt.year + month // 12
    month = month % 12 + 1
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    return dt.replace(year=year, month=month, day=min(dt.day, last_day))


# ── Financiero ────────────────────────────────────────────────────────────────

def calculate_irr(cash_flows: list, max_iter: int = 1000, tol: float = 1e-8):
    """
    TIR anualizada por Newton-Raphson.
    cash_flows: [(datetime, float)] — negativo = salida, positivo = entrada.
    Devuelve tasa anual como decimal (0.05 = 5%) o None.
    """
    if not cash_flows:
        return None
    if not any(v > 0 for _, v in cash_flows) or not any(v < 0 for _, v in cash_flows):
        return None

    t0 = cash_flows[0][0]

    def years(dt):
        delta = dt - t0 if isinstance(dt, datetime) else datetime.combine(dt, datetime.min.time()) - t0
        return delta.days / 365.25

    def npv(r):
        return sum(cf / ((1 + r) ** years(dt)) for dt, cf in cash_flows)

    def dnpv(r):
        result = 0.0
        for dt, cf in cash_flows:
            t = years(dt)
            if t > 0:
                result -= t * cf / ((1 + r) ** (t + 1))
        return result

    for r0 in (0.1, 0.5, -0.1, 0.01, 2.0):
        r = r0
        for _ in range(max_iter):
            f, fp = npv(r), dnpv(r)
            if abs(fp) < 1e-15:
                break
            step = f / fp
            r = max(r - step, -0.9999)
            if abs(step) < tol:
                if -0.9999 < r < 50:
                    return r
                break
    return None


# ── Carga de datos del máster ─────────────────────────────────────────────────

@st.cache_data(show_spinner=False, ttl=3600)
def load_master_projects() -> pd.DataFrame:
    """
    Carga y normaliza el CSV máster de propiedades.
    Devuelve un DataFrame con columnas tipadas y texto limpio (sin tildes garbled).
    """
    if not GSHEET_MASTER_URL:
        return pd.DataFrame()
    r = requests.get(GSHEET_MASTER_URL, timeout=15, allow_redirects=True)
    r.raise_for_status()
    raw = pd.read_csv(io.BytesIO(r.content), header=None, encoding="utf-8")

    hrow = next(
        (i for i, row in raw.iterrows() if any("Token Address" in str(v) for v in row.values)), None
    )
    if hrow is None:
        return pd.DataFrame()
    raw.columns = raw.iloc[hrow]
    raw = raw.iloc[hrow + 1:].reset_index(drop=True)

    projects = []
    for _, row in raw.iterrows():
        estado = strip_accents(str(row.iloc[2]).strip()).upper()
        if estado in ("NAN", ""):
            continue

        divisa_raw = strip_accents(str(row.iloc[11]).strip())
        divisa = "USD" if "$" in divisa_raw else "EUR"

        tip_div = strip_accents(str(row.iloc[16]).strip()).lower()
        if "final" in tip_div and "mensual" not in tip_div and "trimestral" not in tip_div:
            tipo_renta = "final"
        elif "final" in tip_div:
            tipo_renta = "mixto"
        else:
            tipo_renta = "recurrente"

        is_cerrado = "CERRAD" in estado

        # Preferimos datos reales para proyectos cerrados
        r_rec = parse_pct(row.iloc[53]) if is_cerrado else None
        r_rec = r_rec or parse_pct(row.iloc[23])

        r_plusv = parse_pct(row.iloc[60]) if is_cerrado else None
        r_plusv = r_plusv or parse_pct(row.iloc[24])

        r_total_ann = parse_pct(row.iloc[61]) if is_cerrado else None
        r_total_ann = r_total_ann or parse_pct(row.iloc[25])

        precio_emision = parse_float_val(str(row.iloc[10]))
        div_anual_token = (precio_emision or 0) * (r_rec or 0) if r_rec is not None else None

        fecha_fin_real = parse_fecha_util(str(row.iloc[55]))
        fecha_fin_est  = parse_fecha_util(str(row.iloc[5]))

        token_addr = str(row.iloc[18]).strip().lower()
        token_addr = token_addr if token_addr.startswith("0x") and len(token_addr) == 42 else None

        if "FINANCIANDOSE" in estado or "PRELANZAMIENTO" in estado:
            fuente = "lanzamiento"
        elif "EXPLOTACION" in estado or "CONSTRUCCION" in estado or "REFORMA" in estado:
            fuente = "en_marcha"
        elif "CERRAD" in estado:
            fuente = "cerrado"
        else:
            fuente = "otro"

        colateralizable_raw = strip_accents(str(row.iloc[21]).strip()).lower()
        colateralizable = "colateralizable" in colateralizable_raw

        projects.append({
            "id":               str(row.iloc[0]).strip(),
            "nombre":           strip_accents(str(row.iloc[1]).strip()),
            "estado":           estado,
            "divisa":           divisa,
            "ubicacion":        strip_accents(str(row.iloc[14]).strip()),
            "tip_explotacion":  strip_accents(str(row.iloc[15]).strip()),
            "tip_dividendo":    tip_div,
            "tipo_renta":       tipo_renta,
            "precio_emision":   precio_emision,
            "n_tokens_total":   parse_float_val(str(row.iloc[9])),
            "colateralizable":  colateralizable,
            # Rentabilidades por categoría (Reentel / RP / SR)
            # Pendiente desde hoy hasta fin (cols 45-50)
            "r_hoy_total_reentel":  parse_pct(row.iloc[45]),
            "r_hoy_ann_reentel":    parse_pct(row.iloc[46]),
            "r_hoy_total_rp":       parse_pct(row.iloc[47]),
            "r_hoy_ann_rp":         parse_pct(row.iloc[48]),
            "r_hoy_total_sr":       parse_pct(row.iloc[49]),
            "r_hoy_ann_sr":         parse_pct(row.iloc[50]),
            # Rentabilidad recurrente anualizada real por categoría
            "r_rec_ann_reentel":    parse_pct(row.iloc[53]),
            "r_rec_ann_rp":         parse_pct(row.iloc[83]),
            "r_rec_ann_sr":         parse_pct(row.iloc[87]),
            # Plusvalía estimada por categoría
            "r_plusv_reentel":      parse_pct(row.iloc[24]),
            "r_plusv_rp":           parse_pct(row.iloc[28]),
            "r_plusv_sr":           parse_pct(row.iloc[32]),
            # Campos legacy (usados en otras páginas)
            "r_rec_anualizada": r_rec,
            "r_plusvalia":      r_plusv,
            "r_total_anualizada": r_total_ann,
            "r_hoy_total":      parse_pct(row.iloc[45]),
            "r_hoy_anualizada": parse_pct(row.iloc[46]),
            "div_anual_token":  div_anual_token,
            "meses_pendientes": parse_float_val(str(row.iloc[44])),
            "div_pagado_token": parse_float_val(str(row.iloc[51])),
            "fecha_fin":        fecha_fin_real or fecha_fin_est,
            "fecha_lanzamiento": parse_fecha_util(str(row.iloc[3])),
            "token_address":    token_addr,
            "link_web":         str(row.iloc[75]).strip() if str(row.iloc[75]).strip() not in ("nan", "") else None,
            "fuente":           fuente,
        })

    return pd.DataFrame(projects)


@st.cache_data(show_spinner=False, ttl=1800)
def load_p2p_listings(_master_df: pd.DataFrame) -> pd.DataFrame:
    """
    Carga el CSV de P2P y cruza con el máster.
    Devuelve solo filas con tokens_disponibles > 0.
    El parámetro _master_df tiene prefijo _ para excluirlo del hash de caché de Streamlit.
    """
    r = requests.get(GSHEET_P2P_URL, timeout=15, allow_redirects=True)
    r.raise_for_status()
    raw = pd.read_csv(io.BytesIO(r.content), header=None, encoding="utf-8")
    raw.columns = raw.iloc[0]
    raw = raw.iloc[1:].reset_index(drop=True)

    master_by_id = {row["id"]: row.to_dict() for _, row in _master_df.iterrows()}
    listings = []

    for _, row in raw.iterrows():
        pid            = str(row.iloc[0]).strip()
        tokens_disp    = parse_float_val(str(row.iloc[3]))
        precio_p2p     = parse_float_val(str(row.iloc[4]))

        if not tokens_disp or tokens_disp <= 0:
            continue
        if pid not in master_by_id:
            continue

        m = dict(master_by_id[pid])
        m.update({
            "tokens_disponibles":   tokens_disp,
            "tokens_en_propuestas": parse_float_val(str(row.iloc[2])) or 0,
            "precio_p2p_usdt":      precio_p2p,
            "fuente":               "p2p_real",
        })
        listings.append(m)

    return pd.DataFrame(listings) if listings else pd.DataFrame()


# ── Blockchain ────────────────────────────────────────────────────────────────

def fetch_all_token_txs(wallet: str, api_key: str, max_rounds: int = 40) -> list:
    """
    Descarga TODOS los transfers ERC-20 de una wallet vía Etherscan tokentx.
    Etherscan limita cada llamada a 1000 resultados y la ventana page×offset a
    10.000, así que se pagina (hasta 10 páginas por ronda) y, si se agota la
    ventana, se reanuda desde el último bloque visto en una nueva ronda,
    deduplicando el solape del bloque frontera.
    Devuelve la lista de txs en orden ascendente, o [] si la API falla.
    """
    if not api_key:
        return []
    wallet = wallet.lower()
    all_txs = []
    seen = set()
    startblock = 0

    for _ in range(max_rounds):
        hit_window = True
        for page in range(1, 11):
            params = {
                "chainid": POLYGON_CHAIN_ID, "module": "account", "action": "tokentx",
                "address": wallet, "startblock": startblock, "endblock": 99999999,
                "sort": "asc", "page": page, "offset": 1000, "apikey": api_key,
            }
            try:
                resp = requests.get(ETHERSCAN_V2_BASE, params=params, timeout=30)
                result = resp.json().get("result")
            except Exception:
                return all_txs
            if not isinstance(result, list):
                # "No transactions found" u otro error → no hay más datos fiables
                return all_txs
            for tx in result:
                key = (tx.get("hash"), tx.get("contractAddress"), tx.get("from"),
                       tx.get("to"), tx.get("value"), tx.get("tokenID", ""))
                if key in seen:
                    continue
                seen.add(key)
                all_txs.append(tx)
            if len(result) < 1000:
                hit_window = False
                break
        if not hit_window:
            break
        if not all_txs:
            break
        # Ventana agotada: nueva ronda desde el último bloque visto (incluido,
        # para no perder txs del mismo bloque; el dedupe evita duplicados).
        startblock = int(all_txs[-1]["blockNumber"])

    return all_txs


def fetch_wallet_token_balances(wallet: str, known_addresses: set, api_key: str) -> dict:
    """
    Devuelve {contract_address_lower: net_balance} para tokens Reental en la wallet.
    Solo incluye tokens con saldo > 0.
    """
    if not api_key:
        return {}
    wallet = wallet.lower()
    txs = fetch_all_token_txs(wallet, api_key)

    balances = {}
    for tx in txs:
        contract = tx["contractAddress"].lower()
        if contract not in known_addresses:
            continue
        dec   = int(tx["tokenDecimal"]) if tx.get("tokenDecimal") else 18
        value = int(tx["value"]) / (10 ** dec)
        if tx["to"].lower() == wallet:
            balances[contract] = balances.get(contract, 0.0) + value
        elif tx["from"].lower() == wallet:
            balances[contract] = balances.get(contract, 0.0) - value

    return {k: round(v, 6) for k, v in balances.items() if v > 0.001}
