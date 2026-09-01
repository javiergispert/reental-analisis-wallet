#!/usr/bin/env python3
"""
Genera la foto diaria del mercado RNT Lend en `data/aave/snapshot.json`.

Lo ejecuta `.github/workflows/snapshot_aave.yml` cada noche, y commitea el
fichero si ha cambiado. La página web solo lee ese fichero, y por eso carga en
segundos en vez de en minutos.

Reutiliza la foto anterior si existe: el escaneo de eventos arranca en el último
bloque ya leído en vez de en el 0, que es la diferencia entre pedir veinte mil
eventos y pedir veintiuno. Con `--completo` se ignora lo guardado y se
reconstruye todo desde cero (útil si se sospecha que el acumulado se corrompió).

Uso:
    python3 scripts/snapshot_aave.py
    python3 scripts/snapshot_aave.py --completo
"""
from __future__ import annotations

import argparse
import os
import sys
import time

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

from dotenv import load_dotenv   # noqa: E402

load_dotenv(os.path.join(RAIZ, ".env"))

import aave_snapshot as snap     # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--completo", action="store_true",
                    help="reconstruye desde el bloque 0, ignorando la foto anterior")
    ap.add_argument("--salida", default=snap.RUTA)
    args = ap.parse_args()

    api_key = os.getenv("ETHERSCAN_API_KEY", "")
    if not api_key:
        print("ERROR: falta ETHERSCAN_API_KEY", file=sys.stderr)
        return 1

    previo = {} if args.completo else snap.cargar(args.salida)
    if previo:
        edad = snap.edad_horas(previo)
        print(f"foto anterior del bloque {previo.get('bloque'):,}"
              + (f" ({edad:.1f} h)" if edad is not None else ""))
    else:
        print("sin foto previa utilizable: reconstrucción completa")

    t0 = time.time()
    try:
        nueva = snap.construir(api_key, previo)
    except Exception as e:
        # Si falla, NO se toca el fichero: mejor una foto de ayer que ninguna.
        print(f"ERROR generando la foto: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    snap.guardar(nueva, args.salida)
    tam = os.path.getsize(args.salida) / 1024
    print(f"\nguardado en {args.salida} ({tam:,.0f} KB) en {time.time() - t0:.0f}s")
    print(f"  bloque {nueva['bloque']:,} · "
          f"{len(nueva.get('colateral', []))} proyectos con colateral · "
          f"{(nueva.get('salud') or {}).get('n_posiciones', 0)} posiciones")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
