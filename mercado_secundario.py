"""
Mercado secundario de tokens Reental — vista unificada de los dos canales.

Un inversor puede vender sus tokens por dos vías, y hasta ahora se miraban por
separado:

  * **OTC**: intermediado por Reental. Queda registrado en la hoja de Reservas y
    tenemos el embudo completo — sabemos también lo ofertado y lo reservado, no
    solo lo cerrado.
  * **RNTP2P**: directo entre inversores en p2p.rnt.finance. Solo se conoce lo
    ejecutado; las ofertas abiertas son órdenes firmadas fuera de la cadena y no
    dejan rastro.

Este módulo los normaliza a un mismo esquema para poder compararlos y sumarlos.
Esa asimetría —en OTC hay oferta viva, en P2P no— no se disimula: se expone en
`ADVERTENCIA_COBERTURA` para que la UI la muestre en lugar de dejar que el
usuario asuma que ambos canales miden lo mismo.

Esquema de `operaciones()`:
    fecha · canal · proyecto · token_address · tokens · importe_usd ·
    precio_unitario · vendedor · comprador · detalle_ok
"""
from __future__ import annotations

import pandas as pd

import otc_historico
import p2p_mercado

CANAL_P2P = "RNTP2P"
CANAL_OTC = "OTC Reental"

ADVERTENCIA_COBERTURA = (
    "En **OTC** se conoce todo el embudo (ofertado, reservado y cerrado). En "
    "**RNTP2P** solo las operaciones cerradas: publicar una oferta allí no deja "
    "rastro en la cadena. Al comparar canales, se comparan cierres con cierres."
)

COLUMNAS = ["fecha", "canal", "proyecto", "token_address", "tokens",
            "importe_usd", "precio_unitario", "vendedor", "comprador", "detalle_ok"]


def _vacio() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNAS)


def operaciones_p2p(project_by_addr: dict | None = None) -> pd.DataFrame:
    df = p2p_mercado.cargar()
    if df.empty:
        return _vacio()
    if project_by_addr:
        df = p2p_mercado.completar_proyecto(df, project_by_addr)
    out = pd.DataFrame({
        "fecha": df["fecha"], "canal": CANAL_P2P,
        "proyecto": df["proyecto"], "token_address": df["token_address"],
        "tokens": df["tokens"], "importe_usd": df["importe_usd"],
        "precio_unitario": df["precio_unitario"],
        "vendedor": df["vendedor"], "comprador": df["comprador"],
    })
    # Sin cantidad de tokens la operación cuenta para volumen pero no para
    # precio ni para desglose por proyecto. Se marca para poder decirlo en la UI.
    out["detalle_ok"] = out["tokens"].fillna(0) > 0
    return out[COLUMNAS]


def operaciones_otc_historico() -> pd.DataFrame:
    """OTC de 2025, anterior al sistema de reservas (creado en junio de 2026).
    Periodos disjuntos, así que se suma sin riesgo de contar dos veces."""
    df = otc_historico.cargar()
    if df.empty:
        return _vacio()
    out = pd.DataFrame({
        "fecha": df["fecha"], "canal": CANAL_OTC,
        "proyecto": df["proyecto_id"], "token_address": df["token_address"],
        "tokens": df["tokens"], "importe_usd": df["importe_usd"],
        "precio_unitario": df["precio_unitario"],
        "vendedor": df["vendedor"], "comprador": df["comprador"],
    })
    out["detalle_ok"] = out["tokens"].fillna(0) > 0
    return out[COLUMNAS]


def operaciones_otc(reservas: list) -> pd.DataFrame:
    """Reservas OTC ya cerradas. Las activas son compromiso, no operación: si se
    contaran, el volumen incluiría ventas que aún pueden caerse."""
    filas = []
    for r in reservas or []:
        if r.get("estado") != "completada":
            continue
        try:
            tokens = float(r.get("n_tokens") or 0)
            importe = float(r.get("total_usd") or 0)
        except (TypeError, ValueError):
            continue
        fecha = None
        for campo in ("fecha_envio", "fecha_reserva"):
            fecha = pd.to_datetime(r.get(campo), format="%d/%m/%Y %H:%M", errors="coerce")
            if pd.notna(fecha):
                break
        filas.append({
            "fecha": fecha, "canal": CANAL_OTC,
            "proyecto": r.get("proyecto_nombre"),
            "token_address": (r.get("token_address") or "").lower(),
            "tokens": tokens, "importe_usd": importe,
            "precio_unitario": (importe / tokens) if tokens > 0 else None,
            # En OTC de tercero vende el inversor; en stock propio, Reental.
            "vendedor": (r.get("wallet_inversor") or "").lower()
                        if r.get("tipo_origen") == "tercero" else "reental",
            "comprador": None,
            "detalle_ok": tokens > 0,
        })
    return pd.DataFrame(filas)[COLUMNAS] if filas else _vacio()


def operaciones(reservas: list, project_by_addr: dict | None = None) -> pd.DataFrame:
    """Los dos canales en una sola tabla, ordenada por fecha."""
    partes = [d for d in (operaciones_p2p(project_by_addr),
                          operaciones_otc(reservas),
                          operaciones_otc_historico())
              if not d.empty]        # concatenar vacíos altera los dtypes
    if not partes:
        return _vacio()
    df = pd.concat(partes, ignore_index=True)
    # `detalle_ok` debe ser booleano de verdad: si la concatenación lo deja como
    # objeto, `~columna` opera bit a bit sobre enteros y los recuentos salen
    # negativos en vez de contar filas.
    df["detalle_ok"] = df["detalle_ok"].fillna(False).astype(bool)
    return df.dropna(subset=["fecha"]).sort_values("fecha").reset_index(drop=True)


def filtrar(df: pd.DataFrame, desde=None, hasta=None,
            token_address: str | None = None) -> pd.DataFrame:
    """Filtro por período y proyecto. `hasta` es inclusivo: el usuario elige un
    día, no un instante, y excluirlo dejaría fuera lo ocurrido ese mismo día."""
    if df.empty:
        return df
    out = df
    if desde is not None:
        out = out[out["fecha"] >= pd.Timestamp(desde)]
    if hasta is not None:
        out = out[out["fecha"] < pd.Timestamp(hasta) + pd.Timedelta(days=1)]
    if token_address:
        out = out[out["token_address"].fillna("").str.lower() == token_address.lower()]
    return out


def kpis(df: pd.DataFrame, precio_emision: float | None = None) -> dict:
    """Métricas de un conjunto de operaciones ya filtrado.

    El precio se pondera por importe, no por operación: una venta de 100 tokens
    y otra de 0,3 no deben pesar igual en el precio medio del mercado.
    """
    if df.empty:
        return {"ops": 0, "volumen": 0.0, "tokens": 0.0, "precio_medio": None,
                "ticket_medio": None, "vendedores": 0, "compradores": 0,
                "prima_pct": None, "sin_detalle": 0}
    ok = df["detalle_ok"].fillna(False).astype(bool)
    con = df[ok & df["precio_unitario"].notna()]
    precio = (con["importe_usd"].sum() / con["tokens"].sum()) if len(con) and con["tokens"].sum() > 0 else None
    prima = ((precio / precio_emision - 1) * 100
             if precio and precio_emision and precio_emision > 0 else None)
    return {
        "ops": len(df),
        "volumen": float(df["importe_usd"].sum()),
        "tokens": float(con["tokens"].sum()) if len(con) else 0.0,
        "precio_medio": precio,
        "ticket_medio": float(df["importe_usd"].mean()),
        "vendedores": int(df["vendedor"].dropna().nunique()),
        "compradores": int(df["comprador"].dropna().nunique()),
        "prima_pct": prima,
        "sin_detalle": int((~ok).sum()),
    }


def serie_mensual(df: pd.DataFrame) -> pd.DataFrame:
    """Volumen y operaciones por mes y canal, para el gráfico de evolución."""
    if df.empty:
        return pd.DataFrame(columns=["mes", "canal", "volumen", "ops"])
    g = (df.assign(mes=df["fecha"].dt.to_period("M").dt.to_timestamp())
           .groupby(["mes", "canal"])
           .agg(volumen=("importe_usd", "sum"), ops=("importe_usd", "size"))
           .reset_index())
    return g


def resumen_por_token(df: pd.DataFrame, meses: int = 12) -> dict:
    """Liquidez del secundario por proyecto en los últimos `meses`.

    Alimenta la segunda hoja del informe comercial. Devuelve un dict indexado
    por dirección de token para poder cruzarlo con el ranking sin depender del
    nombre, que varía entre el maestro y la plataforma.

    Se devuelve SIEMPRE lo que hay, aunque sea poco: un proyecto con dos
    operaciones debe poder decir "2", no desaparecer. Ocultar los proyectos sin
    profundidad convertiría el informe en argumentario.
    """
    if df.empty:
        return {}
    corte = df["fecha"].max() - pd.Timedelta(days=30 * meses)
    reciente = df[df["fecha"] >= corte]
    out = {}
    for addr, g in reciente.groupby(reciente["token_address"].fillna("").str.lower()):
        if not addr:
            continue
        ok = g["detalle_ok"].fillna(False).astype(bool)
        con = g[ok & g["precio_unitario"].notna()]
        out[addr] = {
            "ops": len(g),
            "tokens": float(con["tokens"].sum()) if len(con) else 0.0,
            "volumen": float(g["importe_usd"].sum()),
            "compradores": int(g["comprador"].dropna().nunique()),
            "vendedores": int(g["vendedor"].dropna().nunique()),
            # Precio medio ponderado por importe, no por operación.
            "precio_medio": (con["importe_usd"].sum() / con["tokens"].sum())
                            if len(con) and con["tokens"].sum() > 0 else None,
        }
    return out
