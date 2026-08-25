"""
Recarga de módulos propios tras un despliegue.

Streamlit reejecuta el script de la página en cada interacción, pero NO
reimporta lo que ya está en `sys.modules`. Tras un despliegue puede convivir una
página ya actualizada con una versión anterior de los módulos que importa, y
entonces una función recién añadida no existe aunque esté en el commit. Ha
pasado dos veces: `utils.codigo_proyecto_atoken` y
`mercado_secundario.resumen_por_token`.

`refrescar()` vuelve a importar un módulo propio solo si su fichero ha cambiado
desde que se cargó, así que el coste es una recarga por despliegue y no una por
interacción.

LÍMITE IMPORTANTE: esto arregla el acceso por atributo (`modulo.funcion()`),
no `from modulo import funcion`, que falla en el import y no llega hasta aquí.
Por eso conviene importar los módulos propios que aún evolucionan como módulo
—`import mercado_secundario as _mkt`— en vez de extraer nombres sueltos.

El orden importa: primero las dependencias y luego quien las usa, porque un
módulo ya cargado conserva la referencia que tenía.
"""
from __future__ import annotations

import importlib
import os
import sys


def refrescar(*nombres: str) -> list:
    """Recarga los módulos indicados si su fichero cambió. Devuelve los nombres
    de los que se hayan recargado, para poder registrarlo si hiciera falta."""
    recargados = []
    for nombre in nombres:
        modulo = sys.modules.get(nombre)
        fichero = getattr(modulo, "__file__", None) if modulo else None
        if not fichero or not os.path.exists(fichero):
            continue
        try:
            mtime = os.path.getmtime(fichero)
        except OSError:
            continue
        if getattr(modulo, "_mtime_cargado", None) == mtime:
            continue
        try:
            nuevo = importlib.reload(modulo)
        except Exception:
            # Un fallo recargando no debe tumbar la página: se sigue con lo que
            # hubiera cargado, que es exactamente el comportamiento de antes.
            continue
        nuevo._mtime_cargado = mtime
        recargados.append(nombre)
    return recargados
