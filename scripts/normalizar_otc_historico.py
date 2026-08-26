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

# Wallet de custodia de Reental: es el comprador en las recompras, que el
# registro no anota porque siempre es la misma.
WALLET_OTC = os.getenv("OTC_WALLET", "0xce0719ec1bda336ba069c6961ad167767829301a").lower()

STABLES_ADDR = {
    "0xc2132d05d31c914a87c6611c10748aeb04b58e8f", "0xe84baaebd135cde0d03b974d3224a742570834af",
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174", "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
    "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063",
}


# Erratas de tecleo del registro manual. Cada una se confirmó leyendo qué token
# se movió realmente en la transacción de esa fila: son transposiciones de
# letras (DBX→DXB, SAL→SLA, MBL→MRB, CLMV/CVLM→CLVM).
ALIAS_ID = {
    "CLMV-1": "CLVM-1", "CVLM-1": "CLVM-1",
    "DBX-1":  "DXB-1",  "DBX-2":  "DXB-2",
    "MBL-1":  "MRB-1",  "SAL-2":  "SLA-2", "RENTAS 1": "RET-1",
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


# ── Adaptadores de esquema ───────────────────────────────────────────────────
# Los registros manuales no comparten formato: el de 2025 anota comprador y
# vendedor de una venta entre inversores; el de 2025-26 son RECOMPRAS de Reental
# y solo traen la wallet del inversor, con la divisa del proyecto y la del pago
# separadas. Cada uno se traduce a un mismo diccionario intermedio para que el
# resto del proceso —cadena, lotes, tipo de cambio— sea idéntico.

def _num_simple(v):
    try:
        return float(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _candidatos_importe(num: str):
    """Todas las lecturas plausibles de un número con separadores ambiguos.

    Este registro mezcla convenciones dentro de la MISMA columna: «€413.00»
    (punto decimal), «10.044 usdt» (punto de miles) y hasta «8,820,00 EURO»
    (coma repetida con decimales al final). No hay regla que acierte a ciegas,
    así que se generan las lecturas posibles y decide quien tenga con qué
    contrastarlas.
    """
    vistos, salida = set(), []

    def añadir(v):
        if v is not None and v not in vistos:
            vistos.add(v)
            salida.append(v)

    solo_digitos = re.sub(r"[^0-9]", "", num)
    for sep in (",", "."):
        if num.count(sep) >= 1:
            izq, _, der = num.rpartition(sep)
            try:
                añadir(float(re.sub(r"[^0-9]", "", izq) + "." + der))
            except ValueError:
                pass
    try:
        añadir(float(solo_digitos))          # todos los separadores son de miles
    except ValueError:
        pass
    return salida


def _importe_y_divisa(txt, esperado=None):
    """'€413.00' → (413.00, 'EUR') · 'USDT 10,300.00' → (10300.0, 'USD')

    Convive el formato español del fichero de 2025 (punto de miles, coma
    decimal) con el inglés de este (coma de miles, punto decimal), así que se
    decide por cuál de los dos separadores aparece más a la derecha.
    """
    if not isinstance(txt, str) or not txt.strip():
        return None, None
    t = txt.strip()
    divisa = "USD" if ("USDT" in t.upper() or "$" in t) else "EUR"
    num = re.sub(r"[^0-9.,]", "", t)
    if "," in num and "." in num:
        # Con ambos separadores, el de más a la derecha es el decimal.
        decimal = "," if num.rfind(",") > num.rfind(".") else "."
    else:
        sep = "," if "," in num else ("." if "." in num else "")
        # Un separador repetido solo puede ser de miles (1.234.567). Si aparece
        # una vez es decimal: el número de decimales varía mucho en estos
        # registros —hay importes con 7— y exigir uno o dos convertía
        # «655.2808907» en 6.552.808.907.
        decimal = "" if (sep and num.count(sep) > 1) else sep
    if decimal == ",":
        base = num.replace(".", "").replace(",", ".")
    elif decimal == ".":
        base = num.replace(",", "")
    else:
        base = num.replace(".", "").replace(",", "")
    try:
        valor = float(base)
    except ValueError:
        valor = None

    # Con un importe esperado (tokens x valor del token) se elige la lectura más
    # cercana en vez de fiarse de la heurística: es lo que distingue «10.044
    # usdt» = 10.044 de 10,044, un factor de mil.
    if esperado and esperado > 0:
        cands = _candidatos_importe(num)
        if cands:
            valor = min(cands, key=lambda v: abs(v - esperado) / esperado)
    return valor, divisa


def filas_normalizadas(df: pd.DataFrame, wallet_otc: str) -> list:
    """Traduce cualquiera de los dos formatos a un esquema común."""
    cols = set(df.columns)
    if "Wallet Comprador" in cols:                      # formato venta 2025
        return [{
            "fecha": r.get("Fecha de la op."), "proyecto": r.get("Inmueble"),
            "tokens_txt": r.get("Nº Tokens"),
            "importe": importe_eur(r.get("Total Pago €")), "divisa": "EUR",
            "vendedor": str(r.get("Wallet Vendedor", "")).strip().lower(),
            "comprador": str(r.get("Wallet Comprador", "")).strip().lower(),
            "hash": r.get("Hash Transacción de los tokens"),
        } for _, r in df.iterrows()]
    if "Wallet del inversor" in cols:                   # formato recompra
        filas = []
        for _, r in df.iterrows():
            # tokens x valor del token da el importe esperado en la divisa del
            # proyecto; sirve de referencia aunque el pago fuera en otra.
            try:
                esperado = (float(str(r.get("Tokens", "")).replace(",", ".")) *
                            float(str(r.get("Valor del token", "")).replace(",", ".")))
            except (TypeError, ValueError):
                esperado = None
            imp, div = _importe_y_divisa(r.get("Amount"), esperado)
            filas.append({
                "fecha": r.get("Fecha"), "proyecto": r.get("Proyecto"),
                "tokens_txt": r.get("Tokens"), "importe": imp, "divisa": div,
                # Recompra: vende el inversor y compra Reental. El comprador no
                # figura en el registro porque siempre es el mismo.
                "vendedor": str(r.get("Wallet del inversor", "")).strip().lower(),
                "comprador": wallet_otc, "valor_token": _num_simple(r.get("Valor del token")),
                "hash": r.get("Hash TX de los tokens recibidos a recomprar"),
            })
        return filas
    raise SystemExit(f"Formato no reconocido. Columnas: {sorted(cols)}")


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
    print(f"ficheros: {[os.path.basename(f) for f in ficheros]}")

    master = load_master_projects()
    addr_por_id = {str(r["id"]).strip().upper(): (r.get("token_address") or "").lower()
                   for _, r in master.iterrows()}
    precio_por_id = {str(r["id"]).strip().upper(): r.get("precio_emision")
                     for _, r in master.iterrows()}
    id_por_addr = {(r.get("token_address") or "").lower(): str(r["id"]).strip().upper()
                   for _, r in master.iterrows() if r.get("token_address")}

    filas, sin_addr, sin_cadena = [], 0, 0
    registros = []
    for f in ficheros:
        d = pd.read_csv(f, dtype=str)
        registros += filas_normalizadas(d, WALLET_OTC)
    print(f"operaciones tras adaptar esquemas: {len(registros):,}")

    for i, reg in enumerate(registros):
        pid   = str(reg["proyecto"]).strip().upper()
        pid   = ALIAS_ID.get(pid, pid)
        addr  = addr_por_id.get(pid, "")
        fecha = parse_fecha(reg["fecha"])
        tx_m  = re.search(r"0x[0-9a-fA-F]{64}", str(reg["hash"] or ""))
        tx    = tx_m.group(0).lower() if tx_m else None
        vendedor, comprador = reg["vendedor"], reg["comprador"]

        # Cada operación tiene su PROPIO Transfer dentro de la transacción, así
        # que se cruza por vendedor y comprador. Sumar todos los del token
        # asignaba el total del lote a cada fila.
        tokens, origen_tokens = None, "cadena"
        if tx:
            movs = transfers_de(tx)
            if not addr:
                # Proyecto sin dirección conocida (fila incompleta del registro):
                # se deduce del propio movimiento si la TX solo mueve un token
                # que no sea stablecoin.
                cand = {m[0] for m in movs if m[0] not in STABLES_ADDR}
                if len(cand) == 1:
                    addr = cand.pop()
                    pid = id_por_addr.get(addr, pid)
                    origen_tokens = "cadena (proyecto deducido)"
            if addr:
                exacto = [m for m in movs if m[0] == addr and m[1] == vendedor and m[2] == comprador]
                if not exacto:
                    exacto = [m for m in movs if m[0] == addr and m[2] == comprador]
                if not exacto:
                    exacto = [m for m in movs if m[0] == addr and m[1] == vendedor]
                if not exacto:
                    candidatos = [m for m in movs if m[0] == addr]
                    exacto = candidatos if len(candidatos) == 1 else []
                    if exacto:
                        origen_tokens = "cadena (sin casar wallets)"
                if exacto:
                    tokens = round(sum(m[3] for m in exacto), 8)
        if not addr:
            sin_addr += 1

        importe, divisa = reg["importe"], reg["divisa"] or "EUR"
        pe_ref = precio_por_id.get(pid)
        try:
            pe_ref = float(pe_ref) if pe_ref else None
        except (TypeError, ValueError):
            pe_ref = None
        if tokens is None:
            tokens, origen_tokens = tokens_desde_texto(reg["tokens_txt"], importe, pe_ref)
            sin_cadena += 1

        # La cadena manda, pero no a ciegas: si la cantidad que devuelve implica
        # un precio disparatado frente al importe pagado, es que se ha casado el
        # Transfer equivocado (una TX puede mover el mismo token por otros
        # motivos). En ese caso gana el registro, que sí cuadra con lo pagado.
        ref = reg.get("valor_token") or pe_ref
        if tokens and importe and ref and ref > 0:
            precio = importe / tokens
            if not (0.4 * ref <= precio <= 2.5 * ref):
                alt, _ = tokens_desde_texto(reg["tokens_txt"], importe, ref)
                if alt and 0.4 * ref <= (importe / alt) <= 2.5 * ref:
                    tokens, origen_tokens = alt, "registro (cadena incoherente)"

        origen_importe = "registro"
        if importe is None:
            if pe_ref and tokens:
                importe, divisa = round(tokens * pe_ref, 2), "EUR"
                origen_importe = "precio de emisión"
            else:
                origen_importe = "sin importe"

        # Solo lo pagado en euros necesita conversión; lo pagado en USDT ya va
        # en dólares y aplicarle el tipo lo falsearía.
        fstr = fecha.strftime("%Y-%m-%d") if pd.notna(fecha) else None
        if divisa == "USD":
            tasa, usd, eur = None, importe, None
        else:
            tasa = eur_usd(fstr) if fstr else None
            eur  = importe
            usd  = round(importe * tasa, 2) if (importe is not None and tasa) else None

        filas.append({
            "fecha": fstr, "proyecto_id": pid, "token_address": addr or None,
            "tokens": tokens, "importe_eur": eur, "eur_usd": tasa, "importe_usd": usd,
            "divisa_pago": divisa,
            "vendedor": vendedor, "comprador": comprador,
            "tx_hash": tx, "origen_importe": origen_importe,
            "origen_tokens": origen_tokens,
        })
        if (i + 1) % 40 == 0:
            print(f"  {i+1}/{len(registros)}…")

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
