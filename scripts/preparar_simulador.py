#!/usr/bin/env python3
"""
Prepara la calculadora de status para empotrarla en la herramienta.

La calculadora la mantiene Jesús González y llega como un HTML suelto. Este
script la deja lista sin tocarla a mano, para que cuando llegue una versión
nueva baste con volver a ejecutarlo en vez de rehacer los ajustes de memoria.

Hace dos cosas:

1. QUITA EL LOGO. El fichero original pesa 2 MB, y el 95% es un GIF animado de
   800x800 y 202 fotogramas... que se muestra a 42 píxeles en la cabecera.
   Dentro de la herramienta no aporta —el usuario ya sabe dónde está— y se
   elimina entero: el fichero baja de 1.952 KB a 159, que es lo que viaja al
   navegador cada vez que se abre la sección.

2. PARAMETRIZA LOS SUPUESTOS. La calculadora trae congelados el interés del
   préstamo y el umbral de liquidación. Nuestra aplicación conoce los valores
   reales, así que esas constantes pasan a leerse de `window.__RNT_CFG` con el
   valor original como respaldo. Si el objeto no llega, la calculadora se
   comporta exactamente como el original.

   NO se tocan las rentabilidades por status (12/14/17%): son un parámetro
   comercial del producto, no un dato que midamos nosotros, y además el usuario
   ya puede editarlas en la propia interfaz.

Uso:
    python3 scripts/preparar_simulador.py ~/Downloads/Calculadora\\ Status\\ v13.html
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import re
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SALIDA = os.path.join(RAIZ, "data", "simulador", "calculadora.html")


# Cada sustitución se verifica: si el HTML nuevo ya no contiene el texto exacto,
# el script falla en vez de generar en silencio una calculadora sin parametrizar.
SUSTITUCIONES = [
    # Interés del préstamo por defecto
    ("let rlApr=0.12,",
     "let rlApr=(window.__RNT_CFG&&window.__RNT_CFG.rlApr)||0.12,"),
    # Umbral de liquidación del pool
    ("const LIQ_THRESHOLD=0.80;",
     "const LIQ_THRESHOLD=(window.__RNT_CFG&&window.__RNT_CFG.liqThreshold)||0.80;"),
    # La etiqueta que acompaña al selector de interés
    ('Media histórica: <b style="color:#fff">12%</b> · máximo: 17%',
     'Media histórica: <b style="color:#fff" id="rnt_aprMedio">12%</b> '
     '· máximo: <span id="rnt_aprMax">17%</span>'),
]


def quitar_logo(html: str, log=print) -> str:
    """Elimina el GIF animado de la cabecera.

    Era 1,3 MB para pintar un icono de 42 px. Dentro de la herramienta el logo
    no aporta —ya se sabe de qué aplicación es— y quitarlo deja el fichero en
    una sexta parte.
    """
    antes = len(html)
    nuevo = re.sub(r'<img id="rntLogo".*?>', "", html, count=1, flags=re.S)
    if nuevo == html:
        log("  (no se encontró el logo; puede que ya se haya quitado)")
        return html
    log(f"  logo eliminado: -{(antes - len(nuevo)) / 1024:,.0f} KB")
    return nuevo


def parametrizar(html: str, log=print) -> str:
    for viejo, nuevo in SUSTITUCIONES:
        if html.count(viejo) != 1:
            raise SystemExit(
                f"ERROR: se esperaba encontrar exactamente una vez este texto y hay "
                f"{html.count(viejo)}:\n  {viejo[:90]}\n"
                f"La calculadora ha cambiado. Revisa SUSTITUCIONES en este script."
            )
        html = html.replace(viejo, nuevo, 1)
    log(f"  parametrizadas {len(SUSTITUCIONES)} constantes")
    return html


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("origen", help="HTML original de la calculadora")
    ap.add_argument("--salida", default=SALIDA)
    args = ap.parse_args()

    if not os.path.exists(args.origen):
        print(f"ERROR: no existe {args.origen}", file=sys.stderr)
        return 1

    html = open(args.origen, encoding="utf-8", errors="replace").read()
    print(f"origen: {len(html)/1024:,.0f} KB")
    html = quitar_logo(html)
    html = parametrizar(html)

    os.makedirs(os.path.dirname(args.salida), exist_ok=True)
    with open(args.salida, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nguardado en {args.salida}: {os.path.getsize(args.salida)/1024:,.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
