#!/usr/bin/env python3
"""Actualiza la serie de participaciones del pool RNT/USDT.

Sin argumentos añade solo lo nuevo desde la última vez (segundos). Con
`--completo` rehace el recorrido entero desde el origen del pool (~40 s).

Si falla, no toca el fichero: es preferible una serie de ayer a media serie.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

import pool_rnt

load_dotenv()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--completo", action="store_true",
                    help="rehacer desde el origen del pool")
    args = ap.parse_args()

    clave = os.getenv("ETHERSCAN_API_KEY")
    if not clave:
        print("Falta ETHERSCAN_API_KEY", file=sys.stderr)
        return 1

    previo = {} if args.completo else pool_rnt.cargar()
    try:
        datos = pool_rnt.construir(clave, previo)
    except Exception as e:                       # noqa: BLE001
        print(f"Error al construir la serie: {e}", file=sys.stderr)
        return 1

    if not datos.get("serie"):
        print("Serie vacía: no se guarda nada", file=sys.stderr)
        return 1

    pool_rnt.guardar(datos)
    ultima = max(datos["serie"])
    print(f"Serie del pool RNT/USDT: {len(datos['serie'])} días, "
          f"última {ultima} ({datos['serie'][ultima]:.9f} SLP en circulación), "
          f"bloque {datos['ultimo_bloque']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
