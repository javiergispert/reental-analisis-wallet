"""
Funciones compartidas entre app.py y el simulador.
"""
import os
import io
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, date

import requests
import streamlit as st
import pandas as pd

# ── Constantes ────────────────────────────────────────────────────────────────

ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"
POLYGON_CHAIN_ID  = 137
ZERO_ADDRESS      = "0x0000000000000000000000000000000000000000"

def _master_url() -> str:
    """La URL se lee en cada llamada, no al importar el módulo. Si se leyera al
    importar, bastaría con que una página importase `utils` antes de su
    `load_dotenv()` para que quedara vacía durante toda la vida del proceso —y
    el catálogo no cargara en ninguna página."""
    return os.getenv("GSHEET_CSV_URL", "")
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

# Posición → nombre de cabecera de cada columna que se lee del maestro.
#
# Todo esto se leía por posición, y el maestro lo editan personas: una columna
# insertada en medio desplaza cuanto viene detrás y las cifras salen corridas
# SIN que nada falle, que es la peor forma de fallar. Con el nombre delante, la
# posición pasa a ser solo el plan B.
_COLUMNAS_MAESTRO = {
    0: "ID",
    1: "Nombre del proyecto",
    2: "ESTADO",
    3: "LANZAMIENTO",
    5: "Estimación fecha de fin desde Lanzamiento",
    8: "Estimación fecha fin desde Financiación",
    9: "Nº de Tokens",
    10: "Px Emisión  Token",
    11: "Divisa",
    14: "Ubicación",
    15: "Tipología de explotación",
    16: "Tipología de Dividendo",
    18: "Token Address",
    21: "Permite colateralización",
    23: "Estimación Rentab. Rendim. Recurr. anualizados Reentel",
    24: "Estimación Rentab. Plusvalía Reentel",
    25: "Estimación Rentab. Total (Alq. + Plusv.) anualizado Reentel",
    28: "Estimación Rentab. Plusvalía RP",
    32: "Estimación Rentab. Plusvalía SR",
    44: "Estimación Nº Meses pendientes de renta hasta Estimación fin",
    45: "Rentab. Estimada Total de hoy hasta final para Reentel",
    46: "Rentab. Estimada Total Anualizada de hoy hasta final para Reentel",
    47: "Rentab. Estimada Total de hoy hasta final para RP",
    48: "Rentab. Estimada Total Anualizada de hoy hasta final para RP",
    49: "Rentab. Estimada Total de hoy hasta final SR",
    50: "Rentab. Estimada Total Anualizada de hoy hasta final SR",
    51: "Rendimientos Totales recurrentes por token Reentel",
    53: "Real Rentab. por Rendimientos recurrentes Anualizados Reentel",
    54: "Variación rentab. Real en curso vs estimada alquiler Reentel",
    55: "Real fecha de fin",
    60: "Real Rentab. Plusvalía Reentel",
    61: "Real Rentab. Total Anualizada Reentel",
    75: "Link a inmueble en la web publica",
    83: "Real Rentab. por Rendimientos recurrentes Anualizados RP",
    87: "Real Rentab. por Rendimientos recurrentes Anualizados SR",
    102: "Estimación fecha Inicio de Renta desde Financiación",
    105: "Meses hasta el fin",
    106: "Tasa mensual de acumulación en prórroga",
}


def _indice(cabeceras: dict, pos: int) -> int:
    """La posición real de una columna: la que dice su cabecera si se puede
    identificar sin ambigüedad, y si no, la de siempre.

    Un nombre repetido en la hoja no sirve para decidir —no sabríamos cuál de
    las dos es—, así que en ese caso manda la posición.
    """
    nombre = _COLUMNAS_MAESTRO.get(pos)
    if nombre:
        encontrados = cabeceras.get(nombre)
        if encontrados and len(encontrados) == 1:
            return encontrados[0]
    return pos


@st.cache_data(show_spinner=False, ttl=3600)
def load_master_projects() -> pd.DataFrame:
    """
    Carga y normaliza el CSV máster de propiedades.
    Devuelve un DataFrame con columnas tipadas y texto limpio (sin tildes garbled).
    """
    url = _master_url()
    if not url:
        return pd.DataFrame()
    r = requests.get(url, timeout=15, allow_redirects=True)
    r.raise_for_status()
    raw = pd.read_csv(io.BytesIO(r.content), header=None, encoding="utf-8")

    hrow = next(
        (i for i, row in raw.iterrows() if any("Token Address" in str(v) for v in row.values)), None
    )
    if hrow is None:
        return pd.DataFrame()
    # Nombre de cabecera → posiciones en las que aparece. Se guardan todas para
    # poder descartar los nombres ambiguos.
    cabeceras: dict = {}
    for i, v in enumerate(raw.iloc[hrow].tolist()):
        nombre = str(v).strip()
        if nombre and nombre.lower() != "nan":
            cabeceras.setdefault(nombre, []).append(i)
    col = {pos: _indice(cabeceras, pos) for pos in _COLUMNAS_MAESTRO}

    raw.columns = raw.iloc[hrow]
    raw = raw.iloc[hrow + 1:].reset_index(drop=True)

    projects = []
    for _, row in raw.iterrows():
        estado = strip_accents(str(row.iloc[col[2]]).strip()).upper()
        if estado in ("NAN", ""):
            continue

        divisa_raw = strip_accents(str(row.iloc[col[11]]).strip())
        divisa = "USD" if "$" in divisa_raw else "EUR"

        tip_div = strip_accents(str(row.iloc[col[16]]).strip()).lower()
        if "final" in tip_div and "mensual" not in tip_div and "trimestral" not in tip_div:
            tipo_renta = "final"
        elif "final" in tip_div:
            tipo_renta = "mixto"
        else:
            tipo_renta = "recurrente"

        is_cerrado = "CERRAD" in estado

        # Preferimos datos reales para proyectos cerrados
        r_rec = parse_pct(row.iloc[col[53]]) if is_cerrado else None
        r_rec = r_rec or parse_pct(row.iloc[col[23]])

        r_plusv = parse_pct(row.iloc[col[60]]) if is_cerrado else None
        r_plusv = r_plusv or parse_pct(row.iloc[col[24]])

        r_total_ann = parse_pct(row.iloc[col[61]]) if is_cerrado else None
        r_total_ann = r_total_ann or parse_pct(row.iloc[col[25]])

        precio_emision = parse_float_val(str(row.iloc[col[10]]))
        div_anual_token = (precio_emision or 0) * (r_rec or 0) if r_rec is not None else None

        fecha_fin_real = parse_fecha_util(str(row.iloc[col[55]]))
        fecha_fin_est  = parse_fecha_util(str(row.iloc[col[5]]))

        token_addr = str(row.iloc[col[18]]).strip().lower()
        token_addr = token_addr if token_addr.startswith("0x") and len(token_addr) == 42 else None

        if "FINANCIANDOSE" in estado or "PRELANZAMIENTO" in estado:
            fuente = "lanzamiento"
        elif "EXPLOTACION" in estado or "CONSTRUCCION" in estado or "REFORMA" in estado:
            fuente = "en_marcha"
        elif "CERRAD" in estado:
            fuente = "cerrado"
        else:
            fuente = "otro"

        colateralizable_raw = strip_accents(str(row.iloc[col[21]]).strip()).lower()
        colateralizable = "colateralizable" in colateralizable_raw

        projects.append({
            "id":               str(row.iloc[col[0]]).strip(),
            "nombre":           strip_accents(str(row.iloc[col[1]]).strip()),
            "estado":           estado,
            "divisa":           divisa,
            # Sin strip_accents: son campos de PRESENTACIÓN —acaban en tablas,
            # gráficos y en el PDF que se manda a un inversor—, y ninguna
            # comparación depende de ellos. «Espana» y «Prestamo promotor»
            # quedaban feos en un informe. El mapa de banderas del analizador ya
            # acepta las dos formas, así que no se rompe nada.
            "ubicacion":        str(row.iloc[col[14]]).strip(),
            "tip_explotacion":  str(row.iloc[col[15]]).strip(),
            "tip_dividendo":    tip_div,
            "tipo_renta":       tipo_renta,
            "precio_emision":   precio_emision,
            "n_tokens_total":   parse_float_val(str(row.iloc[col[9]])),
            "colateralizable":  colateralizable,
            # Rentabilidades por categoría (Reentel / RP / SR)
            # Pendiente desde hoy hasta fin (cols 45-50)
            "r_hoy_total_reentel":  parse_pct(row.iloc[col[45]]),
            "r_hoy_ann_reentel":    parse_pct(row.iloc[col[46]]),
            "r_hoy_total_rp":       parse_pct(row.iloc[col[47]]),
            "r_hoy_ann_rp":         parse_pct(row.iloc[col[48]]),
            "r_hoy_total_sr":       parse_pct(row.iloc[col[49]]),
            "r_hoy_ann_sr":         parse_pct(row.iloc[col[50]]),
            # Rentabilidad recurrente anualizada real por categoría
            "r_rec_ann_reentel":    parse_pct(row.iloc[col[53]]),
            "r_rec_ann_rp":         parse_pct(row.iloc[col[83]]),
            "r_rec_ann_sr":         parse_pct(row.iloc[col[87]]),
            # Plusvalía estimada por categoría
            "r_plusv_reentel":      parse_pct(row.iloc[col[24]]),
            "r_plusv_rp":           parse_pct(row.iloc[col[28]]),
            "r_plusv_sr":           parse_pct(row.iloc[col[32]]),
            # Campos legacy (usados en otras páginas)
            "r_rec_anualizada": r_rec,
            "r_plusvalia":      r_plusv,
            "r_total_anualizada": r_total_ann,
            "r_hoy_total":      parse_pct(row.iloc[col[45]]),
            "r_hoy_anualizada": parse_pct(row.iloc[col[46]]),
            "div_anual_token":  div_anual_token,
            # AS (44) cuenta meses de RENTA pendientes; DB (105) cuenta los que
            # faltan hasta el cierre. Para anualizar manda DB: el dinero del
            # inversor sigue inmovilizado aunque la renta ya haya terminado.
            "meses_pendientes": parse_float_val(str(row.iloc[col[44]])),
            "meses_hasta_fin":  parse_float_val(str(row.iloc[col[105]])) if len(row) > 105 else None,
            # DC: ritmo al que se asume que el activo sigue acumulando valor una
            # vez sobrepasada su fecha de fin. Es una HIPÓTESIS de quien fija el
            # precio, no un dato observado, y por eso se rellena a mano y solo
            # en los proyectos donde el precio la incorpora.
            "tasa_acum_prorroga": parse_pct(row.iloc[col[106]]) if len(row) > 106 else None,
            # Fecha de fin ORIGINAL: ancla desde la que se cuenta el retraso. No
            # sirve CA, que es la reestimación: la acumulación empieza cuando
            # vence el plazo que se prometió.
            "fecha_fin_original": (parse_fecha_util(str(row.iloc[col[8]]))
                                   or parse_fecha_util(str(row.iloc[col[5]]))),
            "div_pagado_token": parse_float_val(str(row.iloc[col[51]])),
            "fecha_fin":        fecha_fin_real or fecha_fin_est,
            # Comportamiento de las rentas recurrentes: lo estimado al lanzar
            # (X), lo que realmente lleva pagado anualizado (BB) y la desviación
            # entre ambos (BC), tal cual la calcula el maestro. Solo tiene
            # sentido en proyectos que ya deberían estar pagando: en uno en obra
            # el real es 0 y la desviación sale -100% sin que nadie incumpla.
            "r_rec_ann_estimada": parse_pct(row.iloc[col[23]]),
            "r_rec_ann_real":     parse_pct(row.iloc[col[53]]),
            "var_rec_real_est":   parse_pct(row.iloc[col[54]]) if len(row) > 54 else None,
            "fecha_fin_real":     fecha_fin_real,
            "fecha_inicio_renta": (parse_fecha_util(str(row.iloc[col[102]])) if len(row) > 102 else None),
            "fecha_lanzamiento": parse_fecha_util(str(row.iloc[col[3]])),
            "token_address":    token_addr,
            "link_web":         str(row.iloc[col[75]]).strip() if str(row.iloc[col[75]]).strip() not in ("nan", "") else None,
            "fuente":           fuente,
        })

    return pd.DataFrame(projects)


# ── Blockchain ────────────────────────────────────────────────────────────────

# La nomenclatura de los tokens Reental vive en su propio módulo. Se reexporta
# aquí para no romper a quien ya la importaba desde utils.
from reental_tokens import (  # noqa: F401
    codigo_proyecto_atoken, es_atoken_reental, es_token_reental,
)


def fetch_all_account_txs(wallet: str, api_key: str, action: str = "tokentx",
                           contractaddress: str = None, max_rounds: int = 40) -> list:
    """
    Descarga TODO el histórico de una wallet vía Etherscan (tokentx, txlist,
    tokennfttx...). Etherscan limita cada llamada a 1000 resultados y la ventana
    page×offset a 10.000, así que se pagina (hasta 10 páginas por ronda) y, si
    se agota la ventana, se reanuda desde el último bloque visto en una nueva
    ronda, deduplicando el solape del bloque frontera.

    Cada página se reintenta ante fallo transitorio (rate-limit del plan
    gratuito, timeout de red): Etherscan responde esos casos con un `result`
    en forma de mensaje de texto, no de lista. Sin reintento, un solo fallo a
    mitad de un histórico largo (habitual cuando el análisis ya ha hecho muchas
    llamadas previas) truncaba silenciosamente el resto — p. ej. perdiendo años
    de histórico de un vault. Una lista vacía legítima ("sin más transacciones")
    sigue devolviendo `[]` inmediatamente, sin reintentar.

    Devuelve la lista de txs en orden ascendente, o [] si la API sigue fallando
    tras los reintentos.
    """
    if not api_key:
        return []
    wallet = wallet.lower()
    all_txs = []
    # Cuántas veces se ha aceptado ya cada transferencia. No basta con un set de
    # claves únicas: `tokentx` NO devuelve logIndex, así que dos eventos Transfer
    # distintos de la misma TX con idéntico emisor, receptor e importe (una compra
    # troceada en lotes iguales, un reparto por lotes) producen exactamente la
    # misma clave y el segundo se descartaba en silencio, infravalorando el saldo.
    # Se cuenta cuántas veces aparece cada clave POR RONDA y se conserva el
    # máximo, no la suma: dentro de una ronda las repeticiones legítimas se
    # aceptan, y el solape entre rondas no las duplica porque no supera la cuenta
    # ya registrada.
    aceptadas = defaultdict(int)
    startblock = 0

    for _ in range(max_rounds):
        hit_window = True
        vistas_ronda  = defaultdict(int)
        ultimo_bloque = None
        nuevas_ronda  = 0
        for page in range(1, 11):
            params = {
                "chainid": POLYGON_CHAIN_ID, "module": "account", "action": action,
                "address": wallet, "startblock": startblock, "endblock": 99999999,
                "sort": "asc", "page": page, "offset": 1000, "apikey": api_key,
            }
            if contractaddress:
                params["contractaddress"] = contractaddress.lower()

            result = None
            for intento, espera in enumerate((0, 0.6, 1.2, 2.0, 3.0)):
                if espera:
                    time.sleep(espera)
                try:
                    resp = requests.get(ETHERSCAN_V2_BASE, params=params, timeout=30)
                    result = resp.json().get("result")
                except Exception:
                    result = None
                    continue
                if isinstance(result, list):
                    break  # éxito (incluye lista vacía legítima)
                # result no es una lista: rate-limit u otro error transitorio → reintentar
            if not isinstance(result, list):
                # Se agotaron los reintentos: devolvemos lo acumulado hasta ahora
                # en vez de fingir que el histórico termina aquí.
                return all_txs
            for tx in result:
                # logIndex se mantiene en la clave por si el endpoint lo trae
                # (entonces desambigua por sí solo); cuando falta, el recuento
                # por ronda es lo que evita perder repeticiones legítimas.
                key = (tx.get("hash"), tx.get("contractAddress"), tx.get("from"),
                       tx.get("to"), tx.get("value"), tx.get("tokenID", ""), tx.get("logIndex", ""))
                vistas_ronda[key] += 1
                if vistas_ronda[key] > aceptadas[key]:
                    all_txs.append(tx)
                    nuevas_ronda += 1
                ultimo_bloque = int(tx["blockNumber"])
            if len(result) < 1000:
                hit_window = False
                break
        for k, n in vistas_ronda.items():
            if n > aceptadas[k]:
                aceptadas[k] = n
        if not hit_window:
            break
        if ultimo_bloque is None:
            break
        # Ventana agotada: nueva ronda desde el último bloque visto (incluido,
        # para no perder txs del mismo bloque; el recuento evita duplicarlas).
        if ultimo_bloque == startblock and nuevas_ronda == 0:
            break   # la ronda no avanzó ni aportó nada: no hay más que traer
        startblock = ultimo_bloque

    return all_txs


def fetch_all_token_txs(wallet: str, api_key: str, max_rounds: int = 40) -> list:
    """Descarga TODOS los transfers ERC-20 de una wallet (ver fetch_all_account_txs)."""
    return fetch_all_account_txs(wallet, api_key, action="tokentx", max_rounds=max_rounds)


