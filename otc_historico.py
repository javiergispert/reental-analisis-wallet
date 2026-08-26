"""
OTC interno de 2025 — registro anterior al sistema de reservas.

El sistema de reservas de `pages/02_OTC.py` se creó en junio de 2026. Todo el
OTC anterior se llevaba en una hoja aparte, así que este histórico NO se solapa
con la hoja de Reservas: son periodos disjuntos y pueden sumarse sin contar dos
veces.

Importa porque cambia la foto del mercado: en 2025 el OTC movió del orden de
1,18 M USD, unas tres veces lo que el P2P en el mismo periodo. Sin estos datos,
la sección de profundidad del secundario mostraba solo la parte pequeña.

El fichero de origen está en `data/otc_historico/exports/` tal como se recibió,
y `scripts/normalizar_otc_historico.py` produce `normalizado.csv`, que es lo que
lee este módulo. La normalización resuelve varios problemas del registro manual
—cantidades corrompidas por el locale, IDs con erratas, importes vacíos, lotes
que liquidan varias operaciones en una transacción— apoyándose en la cadena.
Todo eso está documentado en el script y en el README del directorio.
"""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

_NORMALIZADO = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "otc_historico", "normalizado.csv")

COLUMNAS = ["fecha", "proyecto_id", "token_address", "tokens", "importe_usd",
            "precio_unitario", "vendedor", "comprador", "tx_hash", "origen_importe"]


@st.cache_data(show_spinner=False, ttl=3600)
def cargar() -> pd.DataFrame:
    """Operaciones OTC de 2025, ya en USD y con las cantidades verificadas."""
    if not os.path.exists(_NORMALIZADO):
        return pd.DataFrame(columns=COLUMNAS)
    df = pd.read_csv(_NORMALIZADO)
    num = lambda c: pd.to_numeric(df.get(c), errors="coerce")
    out = pd.DataFrame({
        "fecha":         pd.to_datetime(df.get("fecha"), errors="coerce"),
        "proyecto_id":   df.get("proyecto_id"),
        "token_address": df.get("token_address", pd.Series(dtype=str)).astype(str).str.lower(),
        "tokens":        num("tokens"),
        "importe_usd":   num("importe_usd"),
        "vendedor":      df.get("vendedor"),
        "comprador":     df.get("comprador"),
        "tx_hash":       df.get("tx_hash"),
        "origen_importe": df.get("origen_importe"),
    })
    out["precio_unitario"] = (out["importe_usd"] / out["tokens"]).where(out["tokens"] > 0)
    return out[COLUMNAS].dropna(subset=["fecha"]).sort_values("fecha").reset_index(drop=True)


def resumen() -> dict:
    """Cifras de cabecera, para poder declarar en la UI qué aporta esta fuente."""
    df = cargar()
    if df.empty:
        return {"ops": 0, "volumen": 0.0, "desde": None, "hasta": None, "estimados": 0}
    return {
        "ops": len(df),
        "volumen": float(df["importe_usd"].sum()),
        "desde": df["fecha"].min(),
        "hasta": df["fecha"].max(),
        # Operaciones sin importe en el registro, valoradas a precio de emisión.
        "estimados": int((df["origen_importe"] == "precio de emisión").sum()),
    }
