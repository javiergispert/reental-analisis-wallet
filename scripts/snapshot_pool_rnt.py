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

    # Contraste contra el contrato antes de escribir nada.
    #
    # La serie se reconstruye sumando eventos, y cualquier evento que se pierda
    # por el camino —un límite de peticiones, una página truncada— deja un
    # supply corto que NO se nota: el fichero parece bueno y el valor de cada
    # participación sale inflado, porque se divide por él. Comparar con lo que
    # dice el contrato convierte ese fallo silencioso en uno ruidoso.
    ultima = max(datos["serie"])
    reconstruido = datos["serie"][ultima]
    on_chain = pool_rnt.supply_on_chain(clave)
    if on_chain is None:
        print("Aviso: no se pudo contrastar con el contrato; se guarda igual.", file=sys.stderr)
    elif abs(on_chain - reconstruido) > 1e-6:
        print(f"La serie reconstruida ({reconstruido:.9f}) no cuadra con el contrato "
              f"({on_chain:.9f}): faltan {on_chain - reconstruido:+.9f} SLP. "
              f"No se guarda. Reintenta, y si persiste rehaz el recorrido con --completo.",
              file=sys.stderr)
        return 1

    pool_rnt.guardar(datos)
    ultima = max(datos["serie"])
    print(f"Serie del pool RNT/USDT: {len(datos['serie'])} días, "
          f"última {ultima} ({datos['serie'][ultima]:.9f} SLP en circulación), "
          f"bloque {datos['ultimo_bloque']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
