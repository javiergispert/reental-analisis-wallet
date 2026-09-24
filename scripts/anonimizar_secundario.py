#!/usr/bin/env python3
"""
Sustituye por seudónimos las direcciones de los datos del mercado secundario.

CUÁNDO SE EJECUTA
-----------------
Cada vez que entra una exportación nueva, que es una vez al mes. El flujo es:

    1. Dejar la exportación en bruto en `data/rnt_p2p/crudo/` (está en
       .gitignore: nunca se commitea).
    2. `python3 scripts/enriquecer_p2p.py`   — reconstruye desde la cadena el
       detalle que la plataforma purga. NECESITA las direcciones reales, por eso
       va antes que esto.
    3. `python3 scripts/anonimizar_secundario.py`  — este script.
    4. Commitear solo lo que deja en `exports/` y `enriquecido.csv`.

QUÉ HACE
--------
  * Las columnas de contraparte (vendedor, comprador, maker, taker, «Wallet
    del inversor»…) pasan a `inv_xxxxxxxx`, estable para la misma dirección.
  * Las columnas de hash de transacción se vacían: son públicas en la cadena,
    pero llevan a la dirección en dos clics y no se usan para nada que se
    muestre.
  * `token_address` se deja intacta: son contratos de Reental, públicos y
    necesarios para cruzar con el maestro.

Nada de esto cambia una sola cifra de las que ve el usuario: las direcciones
solo se usan para CONTAR cuántas distintas hay, y un seudónimo estable cuenta
igual. El script lo comprueba y lo dice.

Uso:
    python3 scripts/anonimizar_secundario.py            # aplica
    python3 scripts/anonimizar_secundario.py --revisar  # solo informa
"""
import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from dotenv import load_dotenv

import seudonimos

load_dotenv()

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATOS = os.path.join(RAIZ, "data")

# Nombre de columna (en minúsculas) → qué hacer con ella.
CONTRAPARTES = {
    "vendedor", "comprador", "maker", "taker",
    "wallet comprador", "wallet vendedor", "wallet del inversor",
    "wallet_inversor", "wallet_origen",
}
HASHES = {
    "tx_hash", "matchedtxhash", "hash transacción de los tokens",
    "hash tx de los tokens recibidos a recomprar",
}
# `hash` a secas es el identificador de la ORDEN en RNTP2P, no una transacción:
# es la clave por la que se deduplican las exportaciones y no se toca.


def ingerir_crudo(aplicar: bool) -> list:
    """Pasa a `exports/` lo que haya en `crudo/`, ya seudonimizado.

    `crudo/` está en .gitignore y es donde aterriza la exportación mensual tal
    como la da la plataforma. Su copia en `exports/` es la que se versiona, así
    que las direcciones reales no llegan nunca al repositorio.
    """
    hechos = []
    for origen_dir, destino_dir in ((os.path.join(DATOS, "rnt_p2p", "crudo"),
                                     os.path.join(DATOS, "rnt_p2p", "exports")),
                                    (os.path.join(DATOS, "otc_historico", "crudo"),
                                     os.path.join(DATOS, "otc_historico", "exports"))):
        for origen in sorted(glob.glob(os.path.join(origen_dir, "*.csv"))):
            destino = os.path.join(destino_dir, os.path.basename(origen))
            hechos.append((os.path.relpath(origen, RAIZ), os.path.relpath(destino, RAIZ)))
            if aplicar:
                os.makedirs(destino_dir, exist_ok=True)
                pd.read_csv(origen, dtype=str, keep_default_na=False).to_csv(destino, index=False)
    return hechos


def ficheros() -> list:
    patrones = [
        os.path.join(DATOS, "rnt_p2p", "*.csv"),
        os.path.join(DATOS, "rnt_p2p", "exports", "*.csv"),
        os.path.join(DATOS, "otc_historico", "*.csv"),
        os.path.join(DATOS, "otc_historico", "exports", "*.csv"),
    ]
    return sorted(f for p in patrones for f in glob.glob(p))


def procesar(ruta: str, aplicar: bool) -> dict:
    df = pd.read_csv(ruta, dtype=str, keep_default_na=False)
    resumen = {"fichero": os.path.relpath(ruta, RAIZ), "direcciones": 0,
               "columnas": [], "hashes": []}

    for col in df.columns:
        nombre = str(col).strip().lower()
        if nombre in CONTRAPARTES:
            n = int(df[col].map(seudonimos.es_direccion).sum())
            if n:
                resumen["direcciones"] += n
                resumen["columnas"].append(f"{col} ({n})")
                if aplicar:
                    df[col] = df[col].map(seudonimos.seudonimo)
        elif nombre in HASHES:
            n = int((df[col].astype(str).str.strip() != "").sum())
            if n:
                resumen["hashes"].append(f"{col} ({n})")
                if aplicar:
                    df[col] = ""

    if aplicar and (resumen["columnas"] or resumen["hashes"]):
        df.to_csv(ruta, index=False)
    return resumen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--revisar", action="store_true",
                    help="solo informa de lo que haría, sin tocar nada")
    args = ap.parse_args()
    aplicar = not args.revisar

    if aplicar:
        try:
            seudonimos.seudonimo("0x" + "0" * 40)      # falla pronto si no hay clave
        except seudonimos.FaltaLaClave as e:
            print(f"{e}", file=sys.stderr)
            return 1

    # Primero se copia lo nuevo de `crudo/` a `exports/`; la seudonimización de
    # después lo alcanza igual que a todo lo demás.
    for origen, destino in ingerir_crudo(aplicar):
        print(f"  {'copiado' if aplicar else 'se copiaría'}: {origen} → {destino}")

    total_dir = total_hash = 0
    for ruta in ficheros():
        r = procesar(ruta, aplicar)
        if not r["columnas"] and not r["hashes"]:
            continue
        total_dir += r["direcciones"]
        total_hash += sum(int(h.split("(")[1].rstrip(")")) for h in r["hashes"])
        print(f"  {r['fichero']}")
        if r["columnas"]:
            print(f"      contrapartes: {', '.join(r['columnas'])}")
        if r["hashes"]:
            print(f"      hashes:       {', '.join(r['hashes'])}")

    verbo = "seudonimizadas" if aplicar else "pendientes de seudonimizar"
    print(f"\n{total_dir:,} direcciones {verbo} · {total_hash:,} hashes "
          f"{'vaciados' if aplicar else 'a vaciar'}")
    if not aplicar and total_dir:
        print("Nada se ha modificado (--revisar).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
