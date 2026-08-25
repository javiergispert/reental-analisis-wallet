"""
Mercado secundario RNTP2P — carga y normalización del histórico de operaciones.

Origen de los datos: exportaciones "Finalized" de p2p.rnt.finance, guardadas en
`data/rnt_p2p/exports/`. Cada exportación es acumulativa, así que se cargan
todas y se deduplica por `hash`: añadir la del mes siguiente no requiere tocar
código ni borrar la anterior.

DECIMALES (verificados contra la cadena en tres puntos del histórico —
10/01/2024, 29/05/2025 y 25/08/2026— comparando con los Transfer reales):

    matchedPrice → 6 decimales, e IMPORTE TOTAL de la operación, no precio
                   unitario. Leerlo como unitario multiplica el análisis por la
                   cantidad de tokens.
    amount       → 18 decimales, cantidad de tokens.

METADATOS INCOMPLETOS: la plataforma purga el detalle de las operaciones
pasadas unas semanas. En la exportación de 25/08/2026, solo 116 de 4.605 filas
traían `propertyName`, `tokenAddress`, `amount` y `listingTime`; el resto los
tenía vacíos. Por eso existe `data/rnt_p2p/enriquecido.csv`, reconstruido desde
la cadena por `scripts/enriquecer_p2p.py`, y por eso conviene exportar cada mes:
lo que no se captura a tiempo deja de estar en el origen.

Todo lo que devuelve este módulo lleva ya los decimales aplicados. Las páginas
no deben volver a dividir por 1e6 ni por 1e18.
"""
from __future__ import annotations

import glob
import os

import pandas as pd
import streamlit as st

_DIR_DATOS   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "rnt_p2p")
_DIR_EXPORTS = os.path.join(_DIR_DATOS, "exports")
_ENRIQUECIDO = os.path.join(_DIR_DATOS, "enriquecido.csv")

DEC_PRECIO = 10 ** 6    # USDT/USDC
DEC_TOKEN  = 10 ** 18

# Columnas que devuelve `cargar()`, ya normalizadas.
# `maker`/`taker` son los roles de la orden, NO vendedor/comprador: el maker
# unas veces publica una venta y otras una compra. Quién vendió solo se sabe por
# la dirección del Transfer, que aporta el enriquecimiento on-chain.
COLUMNAS = ["hash", "fecha", "maker", "taker", "vendedor", "comprador", "tx_hash",
            "bloque", "importe_usd", "tokens", "precio_unitario", "proyecto",
            "token_address", "pais", "fecha_listado", "dias_hasta_venta", "origen_detalle"]


def _leer_exports() -> pd.DataFrame:
    ficheros = sorted(glob.glob(os.path.join(_DIR_EXPORTS, "*.csv")))
    if not ficheros:
        return pd.DataFrame()
    partes = [pd.read_csv(f, dtype=str) for f in ficheros]
    df = pd.concat(partes, ignore_index=True)
    # Las exportaciones son acumulativas: la más reciente manda si una operación
    # aparece en varias (puede haber ganado metadatos por el camino).
    return df.drop_duplicates(subset=["hash"], keep="last")


def _leer_enriquecido() -> pd.DataFrame:
    if not os.path.exists(_ENRIQUECIDO):
        return pd.DataFrame(columns=["hash", "token_address", "tokens"])
    return pd.read_csv(_ENRIQUECIDO, dtype=str)


@st.cache_data(show_spinner=False, ttl=3600)
def cargar() -> pd.DataFrame:
    """Histórico de operaciones P2P con decimales aplicados y metadatos
    completados desde la cadena cuando el export los trae vacíos."""
    df = _leer_exports()
    if df.empty:
        return pd.DataFrame(columns=COLUMNAS)

    num = lambda s: pd.to_numeric(s, errors="coerce")
    out = pd.DataFrame({
        "hash":       df["hash"],
        "maker":      df["maker"].str.lower(),
        "taker":      df["taker"].str.lower(),
        "tx_hash":    df["matchedTxHash"].str.lower(),
        "bloque":     num(df["matchedBlockNumber"]),
        "fecha":      pd.to_datetime(num(df["matchedUnixtime"]), unit="s", errors="coerce"),
        "importe_usd": num(df["matchedPrice"]) / DEC_PRECIO,
        "tokens":     num(df["amount"]) / DEC_TOKEN,
        "proyecto":   df.get("propertyName"),
        "token_address": df.get("tokenAddress", pd.Series(dtype=str)).str.lower(),
        "pais":       df.get("country"),
        "fecha_listado": pd.to_datetime(num(df.get("listingTime")), unit="s", errors="coerce"),
    })
    out["origen_detalle"] = out["tokens"].gt(0).map({True: "export", False: ""})
    out["vendedor"] = pd.NA
    out["comprador"] = pd.NA

    # Completar desde la reconstrucción on-chain lo que el export dejó vacío.
    enr = _leer_enriquecido()
    if not enr.empty:
        enr = enr.assign(
            _tokens=pd.to_numeric(enr["tokens"], errors="coerce"),
            _addr=enr["token_address"].str.lower(),
        ).set_index("hash")
        falta = out["tokens"].isna() | out["tokens"].le(0)
        idx = out.loc[falta, "hash"]
        out.loc[falta, "tokens"] = idx.map(enr["_tokens"]).values
        out.loc[falta, "token_address"] = idx.map(enr["_addr"]).values
        out.loc[falta & out["tokens"].gt(0), "origen_detalle"] = "cadena"
        # Vendedor y comprador reales, que el export no distingue.
        for col in ("vendedor", "comprador"):
            if col in enr.columns:
                out[col] = out["hash"].map(enr[col])

    # El precio unitario solo tiene sentido con cantidad conocida; si no, NaN
    # (nunca 0, que se leería como "salió gratis").
    out["precio_unitario"] = (out["importe_usd"] / out["tokens"]).where(out["tokens"] > 0)
    out["dias_hasta_venta"] = ((out["fecha"] - out["fecha_listado"]).dt.total_seconds() / 86400)
    return out[COLUMNAS].sort_values("fecha").reset_index(drop=True)


def completar_proyecto(df: pd.DataFrame, project_by_addr: dict) -> pd.DataFrame:
    """Rellena `proyecto` desde el maestro para las filas cuyo nombre no venía en
    el export pero cuya dirección de token sí se conoce (por enriquecimiento)."""
    if df.empty or not project_by_addr:
        return df
    df = df.copy()
    falta = df["proyecto"].isna() & df["token_address"].notna()
    df.loc[falta, "proyecto"] = df.loc[falta, "token_address"].map(
        lambda a: (project_by_addr.get(a) or {}).get("nombre")
    )
    df.loc[falta, "pais"] = df.loc[falta, "pais"].fillna(
        df.loc[falta, "token_address"].map(
            lambda a: (project_by_addr.get(a) or {}).get("ubicacion")
        )
    )
    return df


def cobertura(df: pd.DataFrame) -> dict:
    """Qué parte del histórico tiene detalle por proyecto. Se muestra en la UI:
    un KPI por proyecto calculado sobre el 3 % de las operaciones se leería como
    si fuera el mercado entero."""
    total = len(df)
    con_detalle = int(df["tokens"].gt(0).sum()) if total else 0
    return {
        "total": total,
        "con_detalle": con_detalle,
        "pct": (con_detalle / total * 100) if total else 0.0,
        "sin_detalle": total - con_detalle,
    }
