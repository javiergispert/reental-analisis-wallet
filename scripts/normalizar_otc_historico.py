#!/usr/bin/env python3
"""
Normaliza el histórico de OTC interno de 2025 para poder sumarlo al mercado
secundario.

El fichero de origen (`data/otc_historico/exports/*.csv`) es un registro manual
anterior al sistema de reservas —que se creó en junio de 2026—, así que no se
solapa con la hoja de Reservas. Trae comprador, vendedor, inmueble, importe y
hash, pero necesita tres arreglos antes de ser utilizable:

1. CANTIDAD DE TOKENS CORRUPTA. En 82 de 201 filas el valor perdió el separador
   decimal por un problema de locale: `29,94342341` quedó como `2.994.342.341`.
   En vez de dividir a ojo, se recupera de la cadena leyendo el Transfer real de
   la transacción. Comprobado que coincide al decimal.

   OJO: 29 transacciones liquidan VARIAS operaciones a la vez (mismo comprador,
   misma fecha, distintos inmuebles y vendedores). Por eso no vale con sumar
   todas las transferencias de la TX: hay que quedarse con la del token del
   proyecto de esa fila concreta.

2. IMPORTES VACÍOS. 11 filas no traen pago. Se rellenan a precio de emisión del
   proyecto, y quedan marcadas en `origen_importe` para que se sepa cuáles son
   estimadas y cuáles vienen del registro.

3. DIVISA. Los importes están en EUR y el resto de la herramienta trabaja en
   USD. Se convierte con el tipo del BCE DE CADA FECHA (frankfurter.dev), no con
   uno fijo: entre junio de 2025 y noviembre de 2025 el EUR/USD se movió lo
   suficiente como para que un tipo plano falsee la comparación por meses.

Uso:
    python3 scripts/normalizar_otc_historico.py
"""
from __future__ import annotations

import glob
import os
import re
import sys
import time

import pandas as pd
import requests
from dotenv import load_dotenv

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
load_dotenv(os.path.join(RAIZ, ".env"))

from utils import load_master_projects   # noqa: E402
import aave_lend as _al                  # noqa: E402

DIR_DATOS   = os.path.join(RAIZ, "data", "otc_historico")
DIR_EXPORTS = os.path.join(DIR_DATOS, "exports")
SALIDA      = os.path.join(DIR_DATOS, "normalizado.csv")

API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
BASE    = "https://api.etherscan.io/v2/api"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
FX_URL  = "https://api.frankfurter.dev/v1/{fecha}?base=EUR&symbols=USD"


# Erratas de tecleo del registro manual. Cada una se confirmó leyendo qué token
# se movió realmente en la transacción de esa fila: son transposiciones de
# letras (DBX→DXB, SAL→SLA, MBL→MRB, CLMV/CVLM→CLVM).
ALIAS_ID = {
    "CLMV-1": "CLVM-1", "CVLM-1": "CLVM-1",
    "DBX-1":  "DXB-1",  "MBL-1":  "MRB-1", "SAL-2": "SLA-2",
}


def tokens_desde_texto(bruto, pago_eur, precio_emision):
    """Recupera la cantidad de tokens de un valor corrompido por el locale.

    El registro guardaba decimales con coma («34,97953197») y al exportar se
    perdió el separador, quedando «3.497.953.197». El número de decimales
    original NO es constante —hay filas con 8 y otras con 5—, así que no vale
    dividir por una potencia fija: se prueba cada una y se elige la que deja un
    precio por token más cercano al de emisión del proyecto.

    Solo se usa cuando la cadena no puede resolverlo, que es lo preferente.
    """
    if not isinstance(bruto, str):
        return None, "no disponible"
    limpio = bruto.replace(".", "").replace(",", "").strip()
    if not limpio.isdigit():
        return None, "no disponible"
    entero = int(limpio)
    if "," not in bruto and "." not in bruto:
        return float(entero), "texto"          # número simple, sin corromper
    if not (pago_eur and precio_emision):
        # Sin precio con el que contrastar se asume el caso más frecuente del
        # fichero (8 decimales) y se marca como no verificado.
        return round(entero / 1e8, 8), "texto (escala asumida)"
    mejor, mejor_err = None, None
    for k in range(0, 11):
        cand = entero / (10 ** k)
        if cand <= 0:
            continue
        err = abs((pago_eur / cand) - precio_emision)
        if mejor_err is None or err < mejor_err:
            mejor, mejor_err = cand, err
    return (round(mejor, 8), "texto (escala por precio)") if mejor else (None, "no disponible")


def parse_fecha(v):
    """Fecha del registro, tolerando erratas de tecleo en el año.

    Hay una fila con «02/08/20025»: un cero de más. Si el año tiene cinco
    dígitos y empieza por 20, se reconstruye como 20 + los dos últimos. Sin
    esto la fila se quedaba sin fecha y, con ella, sin tipo de cambio.
    """
    txt = str(v).strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4,5})$", txt)
    if m and len(m.group(3)) == 5 and m.group(3).startswith("20"):
        txt = f"{m.group(1)}/{m.group(2)}/20{m.group(3)[-2:]}"
    return pd.to_datetime(txt, format="%d/%m/%Y", errors="coerce")


def importe_eur(v) -> float | None:
    """'5.311,05€' → 5311.05 (formato español: punto de miles, coma decimal)."""
    if not isinstance(v, str):
        return None
    s = v.replace("€", "").replace(".", "").replace(",", ".").strip()
    try:
        return float(s)
    except ValueError:
        return None


_fx_cache: dict = {}


def eur_usd(fecha: str) -> float | None:
    """Tipo EUR/USD del BCE para esa fecha. Cacheado por fecha: el fichero tiene
    201 filas pero muchas menos fechas distintas."""
    if fecha in _fx_cache:
        return _fx_cache[fecha]
    for intento in range(3):
        try:
            r = requests.get(FX_URL.format(fecha=fecha), timeout=15)
            tasa = float(r.json()["rates"]["USD"])
            _fx_cache[fecha] = tasa
            return tasa
        except Exception:
            time.sleep(1.0 * (intento + 1))
    _fx_cache[fecha] = None
    return None


_rec_cache: dict = {}


def transfers_de(tx: str) -> list:
    """Transferencias ERC-20 de una transacción: [(contrato, from, to, valor)]."""
    if tx in _rec_cache:
        return _rec_cache[tx]
    salida = []
    try:
        r = requests.get(BASE, params={
            "chainid": 137, "module": "proxy", "action": "eth_getTransactionReceipt",
            "txhash": tx, "apikey": API_KEY}, timeout=25)
        for lg in (r.json().get("result") or {}).get("logs", []):
            tp = lg.get("topics", [])
            if len(tp) < 3 or tp[0].lower() != TRANSFER:
                continue
            salida.append((lg["address"].lower(), "0x" + tp[1][-40:].lower(),
                           "0x" + tp[2][-40:].lower(), int(lg["data"], 16) / 1e18))
    except Exception:
        pass
    _rec_cache[tx] = salida
    return salida


def main() -> None:
    if not API_KEY:
        sys.exit("Falta ETHERSCAN_API_KEY (revisa el .env)")

    ficheros = sorted(glob.glob(os.path.join(DIR_EXPORTS, "*.csv")))
    if not ficheros:
        sys.exit(f"No hay ficheros en {DIR_EXPORTS}")
    df = pd.concat([pd.read_csv(f, dtype=str) for f in ficheros], ignore_index=True)
    print(f"operaciones a normalizar: {len(df):,}")

    master = load_master_projects()
    addr_por_id = {str(r["id"]).strip().upper(): (r.get("token_address") or "").lower()
                   for _, r in master.iterrows()}
    precio_por_id = {str(r["id"]).strip().upper(): r.get("precio_emision")
                     for _, r in master.iterrows()}

    filas, sin_addr, sin_cadena = [], 0, 0
    for i, r in df.iterrows():
        pid   = str(r["Inmueble"]).strip().upper()
        pid   = ALIAS_ID.get(pid, pid)
        addr  = addr_por_id.get(pid, "")
        fecha = parse_fecha(r["Fecha de la op."])
        tx_m  = re.search(r"0x[0-9a-fA-F]{64}", str(r.get("Hash Transacción de los tokens", "")))
        tx    = tx_m.group(0).lower() if tx_m else None

        # Cantidad: siempre desde la cadena, filtrando por el token del proyecto
        # de ESTA fila (una TX puede liquidar varias operaciones distintas).
        vendedor  = str(r["Wallet Vendedor"]).strip().lower()
        comprador = str(r["Wallet Comprador"]).strip().lower()

        # Cada operación tiene su PROPIO Transfer dentro de la transacción, así
        # que se cruza por vendedor y comprador. Sumar todos los del token
        # asignaba el total del lote a cada fila: cinco operaciones de DNB-1 del
        # 22/09 salían con 122,112 tokens cada una (el total) en vez de sus
        # 50 / 25 / 11,79 / 23,58 / 11,75 reales, y el precio se desplomaba a
        # 8 €/token en vez de los ~85 que fueron.
        tokens, origen_tokens = None, "cadena"
        if tx and addr:
            movs = transfers_de(tx)
            exacto = [m for m in movs if m[0] == addr and m[1] == vendedor and m[2] == comprador]
            if not exacto:
                exacto = [m for m in movs if m[0] == addr and m[2] == comprador]
            if not exacto:
                exacto = [m for m in movs if m[0] == addr and m[1] == vendedor]
            if not exacto:
                # Última opción: el token está pero no casan las wallets. Solo
                # vale si es el único movimiento de ese token en la TX.
                candidatos = [m for m in movs if m[0] == addr]
                exacto = candidatos if len(candidatos) == 1 else []
                if exacto:
                    origen_tokens = "cadena (sin casar wallets)"
            if exacto:
                tokens = round(sum(m[3] for m in exacto), 8)
        if not addr:
            sin_addr += 1

        eur = importe_eur(r.get("Total Pago €"))
        pe_ref = precio_por_id.get(pid)
        try:
            pe_ref = float(pe_ref) if pe_ref else None
        except (TypeError, ValueError):
            pe_ref = None
        if tokens is None:
            # La cadena no lo resuelve (fila sin hash, o el enlace apunta al
            # token en vez de a la transacción): se recupera del texto.
            tokens, origen_tokens = tokens_desde_texto(r.get("Nº Tokens"), eur, pe_ref)
            sin_cadena += 1
        origen_importe = "registro"
        if eur is None:
            # Sin importe registrado: se valora a precio de emisión, y se marca.
            pe = pe_ref
            if pe and tokens:
                eur = round(tokens * pe, 2)
                origen_importe = "precio de emisión"
            else:
                origen_importe = "sin importe"

        fstr = fecha.strftime("%Y-%m-%d") if pd.notna(fecha) else None
        tasa = eur_usd(fstr) if fstr else None
        usd  = round(eur * tasa, 2) if (eur is not None and tasa) else None

        filas.append({
            "fecha": fstr, "proyecto_id": pid, "token_address": addr or None,
            "tokens": tokens, "importe_eur": eur, "eur_usd": tasa, "importe_usd": usd,
            "vendedor": vendedor, "comprador": comprador,
            "tx_hash": tx, "origen_importe": origen_importe,
            "origen_tokens": origen_tokens,
        })
        if (i + 1) % 40 == 0:
            print(f"  {i+1}/{len(df)}…")

    out = pd.DataFrame(filas)

    # Cuando varias filas comparten EXACTAMENTE el mismo Transfer (mismo lote,
    # mismo vendedor y mismo comprador), a todas se les asignó la cantidad
    # entera. Se reparte en proporción a lo pagado, que es la única referencia
    # disponible para separarlas.
    # La clave NO incluye vendedor: hay lotes en los que Reental actúa de
    # intermediaria y la cadena registra un único Transfer desde su wallet,
    # aunque el registro anote dos vendedores distintos. El guardián de
    # `nunique() == 1` impide repartir lo que ya se separó bien por comprador.
    clave = ["tx_hash", "token_address"]
    dup = out.dropna(subset=["tx_hash"]).groupby(clave).filter(lambda g: len(g) > 1)
    repartidas = 0
    for _, g in dup.groupby(clave):
        total_eur = g["importe_eur"].sum()
        if not total_eur or g["tokens"].nunique() != 1:
            continue
        cantidad = g["tokens"].iloc[0]
        for idx, fila in g.iterrows():
            out.at[idx, "tokens"] = round(cantidad * fila["importe_eur"] / total_eur, 8)
            out.at[idx, "origen_tokens"] = "cadena (lote repartido)"
            repartidas += 1
    if repartidas:
        print(f"  filas de lote repartidas por importe: {repartidas}")

    os.makedirs(DIR_DATOS, exist_ok=True)
    out.to_csv(SALIDA, index=False)

    print(f"\nguardado en {os.path.basename(SALIDA)}: {len(out):,} filas")
    print(f"  con cantidad de tokens : {out['tokens'].notna().sum():,}")
    print(f"  sin token_address      : {sin_addr}")
    print(f"  resueltos del texto    : {sin_cadena}")
    print(out["origen_tokens"].value_counts().to_string())
    print(f"  importes del registro  : {(out.origen_importe=='registro').sum():,}")
    print(f"  a precio de emisión    : {(out.origen_importe=='precio de emisión').sum():,}")
    print(f"  sin importe            : {(out.origen_importe=='sin importe').sum():,}")
    print(f"  total EUR {out.importe_eur.sum():,.2f} → USD {out.importe_usd.sum():,.2f}")
    print(f"  tipos EUR/USD usados: {len({v for v in _fx_cache.values() if v})} fechas distintas")


if __name__ == "__main__":
    main()
