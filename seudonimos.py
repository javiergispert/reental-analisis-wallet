"""
Seudónimos estables para las direcciones que aparecen en los datos publicados.

POR QUÉ NO VALE UN HASH A SECAS
-------------------------------
La tentación es publicar `sha256(direccion)` y darlo por anonimizado. Aquí eso
no protege nada: el conjunto de candidatos es **enumerable**. Cualquiera puede
listar desde la cadena todas las direcciones que han tenido un token de Reental
—son unos pocos miles—, calcular el hash de cada una y cruzarlo con el fichero.
La reidentificación sería completa y en minutos.

Con una clave secreta delante (HMAC) eso deja de funcionar: sin la clave no se
puede calcular el seudónimo de un candidato, por mucho que se conozca la lista
entera. La clave vive en `SEUDONIMO_SALT`, fuera del repositorio.

POR QUÉ ESTABLE
---------------
El mismo inversor debe dar el mismo seudónimo mes tras mes. Si cambiara, una
wallet que operó en marzo y en octubre se contaría como dos vendedores
distintos y el KPI de «vendedores únicos» se inflaría con cada exportación.
Por eso es un HMAC determinista y no un identificador aleatorio.

QUÉ NO RESUELVE
---------------
Seudonimizar no es anonimizar del todo: quien tenga la clave puede volver atrás,
y el patrón de operaciones de una wallet sigue siendo el mismo aunque se llame
de otra forma. Es una reducción de riesgo, no una desaparición del dato. Si
alguna vez hay que publicar esto fuera de la compañía, habría que agregar.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re

_DIRECCION = re.compile(r"^0x[0-9a-fA-F]{40}$")


class FaltaLaClave(RuntimeError):
    """Sin `SEUDONIMO_SALT` no se seudonimiza: se para."""


def _clave() -> bytes:
    """La clave secreta, o un error.

    No hay valor por defecto a propósito. Una clave por defecto en un
    repositorio público es exactamente lo mismo que no tener ninguna, y el
    fallo sería silencioso: los ficheros parecerían anonimizados.
    """
    salt = os.getenv("SEUDONIMO_SALT", "").strip()
    if len(salt) < 16:
        raise FaltaLaClave(
            "Falta SEUDONIMO_SALT (mínimo 16 caracteres) en la configuración. "
            "Sin ella los seudónimos serían reversibles por fuerza bruta, porque "
            "la lista de direcciones candidatas es pública en la cadena."
        )
    return salt.encode("utf-8")


def es_direccion(valor) -> bool:
    return bool(_DIRECCION.match(str(valor or "").strip()))


def seudonimo(direccion: str, prefijo: str = "inv") -> str:
    """`0xF486…805d` → `inv_7c1a9f04`. Igual siempre para la misma dirección.

    Lo que no sea una dirección se devuelve tal cual: así la función se puede
    aplicar a una columna entera sin tener que filtrar antes las celdas vacías
    o los textos sueltos.
    """
    if not es_direccion(direccion):
        return direccion
    firma = hmac.new(_clave(), str(direccion).strip().lower().encode("utf-8"),
                     hashlib.sha256).hexdigest()
    return f"{prefijo}_{firma[:8]}"
