#!/usr/bin/env python3
"""
Reconstruye desde la cadena el detalle que las exportaciones de RNTP2P pierden.

La plataforma purga `propertyName`, `tokenAddress` y `amount` de las operaciones
pasadas unas semanas: en la exportación de 25/08/2026 solo 116 de 4.605 filas
los conservaban. La cadena sí los tiene, y cada fila trae `matchedTxHash`.

En vez de pedir 4.500 recibos —uno por operación—, se descarga el histórico de
transferencias de cada `maker` (unos 400 en todo el histórico) y se cruza por
hash de transacción: son unas 10 veces menos llamadas. Vale con los makers
porque son parte de la operación, así que su `tokentx` la contiene siempre.

OJO: el `maker` NO es el vendedor. En RNTP2P unas veces publica una venta y
otras una compra, así que puede aparecer entregando o recibiendo el token.
Quién vende lo dice la dirección del propio Transfer, y por eso se guarda.

Es un proceso de una sola vez: el resultado se guarda en
`data/rnt_p2p/enriquecido.csv` y se versiona con el repo. Volver a ejecutarlo
solo trabaja las operaciones que aún no estén resueltas, así que tras cada
exportación mensual se puede relanzar sin rehacer nada.

Uso:
    python3 scripts/enriquecer_p2p.py            # incremental
    python3 scripts/enriquecer_p2p.py --todo     # ignora lo ya resuelto
"""
from __future__ import annotations

import glob
import os
import sys

import pandas as pd
from dotenv import load_dotenv

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
load_dotenv(os.path.join(RAIZ, ".env"))

from utils import fetch_all_account_txs          # noqa: E402
from reental_tokens import es_atoken_reental     # noqa: E402

DIR_DATOS   = os.path.join(RAIZ, "data", "rnt_p2p")
DIR_EXPORTS = os.path.join(DIR_DATOS, "exports")
SALIDA      = os.path.join(DIR_DATOS, "enriquecido.csv")

API_KEY = os.getenv("ETHERSCAN_API_KEY", "")

# Contrapartidas que NO son el token del inmueble: si aparecen en la misma TX,
# hay que descartarlas para quedarse con el activo realmente intercambiado.
STABLES = {
    "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
    "0xe84baaebd135cde0d03b974d3224a742570834af",
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
    "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063",
}


def cargar_operaciones() -> pd.DataFrame:
    ficheros = sorted(glob.glob(os.path.join(DIR_EXPORTS, "*.csv")))
    if not ficheros:
        sys.exit(f"No hay exportaciones en {DIR_EXPORTS}")
    df = pd.concat([pd.read_csv(f, dtype=str) for f in ficheros], ignore_index=True)
    df = df.drop_duplicates(subset=["hash"], keep="last")
    df["_tokens"] = pd.to_numeric(df["amount"], errors="coerce")
    return df


def main() -> None:
    if not API_KEY:
        sys.exit("Falta ETHERSCAN_API_KEY (revisa el .env)")
    rehacer_todo = "--todo" in sys.argv

    ops = cargar_operaciones()
    print(f"operaciones en las exportaciones: {len(ops):,}")

    ya = pd.DataFrame(columns=["hash", "token_address", "tokens", "vendedor", "comprador"])
    if os.path.exists(SALIDA) and not rehacer_todo:
        ya = pd.read_csv(SALIDA, dtype=str)
        print(f"ya resueltas en {os.path.basename(SALIDA)}: {len(ya):,}")

    # Se procesa TODA operación que no esté ya resuelta, no solo las que el
    # export dejó sin cantidad: las completas también necesitan pasar por aquí
    # para saber quién vendió y quién compró, que el export no distingue.
    pendientes = ops[~ops["hash"].isin(set(ya["hash"]))]
    print(f"pendientes de reconstruir: {len(pendientes):,}")
    if pendientes.empty:
        print("Nada que hacer.")
        return

    # Índice tx_hash → fila, para volcar lo que se encuentre al recorrer wallets.
    por_tx = {}
    for _, r in pendientes.iterrows():
        por_tx.setdefault((r["matchedTxHash"] or "").lower(), []).append(r["hash"])

    makers = sorted({(v or "").lower() for v in pendientes["maker"] if isinstance(v, str)})
    print(f"makers a consultar: {len(makers):,}\n")

    resueltas, sin_resolver = [], set(pendientes["hash"])
    for i, w in enumerate(makers, 1):
        try:
            txs = fetch_all_account_txs(w, API_KEY, action="tokentx")
        except Exception as e:                      # una wallet no debe tumbar el lote
            print(f"  [{i}/{len(makers)}] {w[:12]}… ERROR {type(e).__name__}")
            continue
        encontradas = 0
        for tx in txs:
            h = (tx.get("hash") or "").lower()
            if h not in por_tx:
                continue
            contrato = (tx.get("contractAddress") or "").lower()
            # El activo vendido es el token del inmueble: ni la stablecoin con la
            # que se paga, ni el aToken de Aave si la TX toca colateral.
            if contrato in STABLES or es_atoken_reental(tx.get("tokenSymbol", ""),
                                                        tx.get("tokenName", "")):
                continue
            dec = int(tx.get("tokenDecimal") or 18)
            cantidad = int(tx["value"]) / (10 ** dec)
            if cantidad <= 0:
                continue
            # NO se filtra por dirección: en RNTP2P el `maker` unas veces publica
            # una venta y otras una compra, así que puede aparecer entregando o
            # recibiendo el token. Quién vende lo dice el propio Transfer.
            for hid in por_tx[h]:
                if hid in sin_resolver:
                    resueltas.append({"hash": hid, "token_address": contrato,
                                      "tokens": round(cantidad, 8),
                                      "simbolo": tx.get("tokenSymbol", ""),
                                      "vendedor": (tx.get("from") or "").lower(),
                                      "comprador": (tx.get("to") or "").lower()})
                    sin_resolver.discard(hid)
                    encontradas += 1
        if i % 25 == 0 or encontradas:
            print(f"  [{i}/{len(makers)}] {w[:12]}…  +{encontradas}  "
                  f"(resueltas {len(resueltas):,} · faltan {len(sin_resolver):,})")

    nuevo = pd.DataFrame(resueltas)
    final = pd.concat([ya, nuevo], ignore_index=True).drop_duplicates(subset=["hash"], keep="last")
    os.makedirs(DIR_DATOS, exist_ok=True)
    final.to_csv(SALIDA, index=False)
    print(f"\nreconstruidas ahora: {len(nuevo):,}")
    print(f"total en {os.path.basename(SALIDA)}: {len(final):,}")
    if sin_resolver:
        print(f"sin resolver: {len(sin_resolver):,} — operaciones cuyo token la API no "
              f"devuelve en el histórico del maker.")


if __name__ == "__main__":
    main()
