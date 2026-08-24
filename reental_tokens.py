"""
Nomenclatura de los tokens de Reental en la cadena — ÚNICA fuente de verdad.

La cadena NO es consistente: los proyectos antiguos se emitieron como
`Reental-CME-1` → `aMatReental-CME-1`, y los nuevos como `REENTAL-CAR-2` →
`aMatREENTAL-CAR-2` (nombre "Aave Matic REENTAL-CAR-2"). Cualquier comprobación
sensible a mayúsculas hace desaparecer en silencio a los proyectos con la grafía
nueva, y como el analizador, OTC y P2P resolvían el aToken cada uno por su
cuenta, el fallo aparecía en las tres a la vez.

Vive en su propio módulo, y no dentro de `utils`, por dos razones: es un
concepto con entidad propia (cómo se llaman las cosas en la cadena), y así las
páginas no dependen de que un módulo grande y muy importado esté actualizado
para arrancar.
"""
from __future__ import annotations

_PREFIJOS_ATOKEN_SYM  = ("amatreental-", "apolreental-")
_PREFIJOS_ATOKEN_NAME = ("aave matic reental-", "aave polygon reental-")


def codigo_proyecto_atoken(tx_symbol: str, tx_name: str) -> str:
    """Código de proyecto de un aToken de colateral Reental: 'CME-1', 'CAR-2'…

    Compara en minúsculas para aceptar cualquier grafía. Devuelve "" si no es un
    aToken de Reental.
    """
    for texto, prefijos in ((tx_symbol or "", _PREFIJOS_ATOKEN_SYM),
                            (tx_name or "", _PREFIJOS_ATOKEN_NAME)):
        bajo = texto.lower()
        for p in prefijos:
            if bajo.startswith(p):
                return texto[len(p):].strip()
    return ""


def es_atoken_reental(tx_symbol: str, tx_name: str) -> bool:
    """¿Es el aToken con el que Aave representa el colateral de un token Reental?"""
    return bool(codigo_proyecto_atoken(tx_symbol, tx_name))


def es_token_reental(tx_symbol: str, tx_name: str) -> bool:
    """¿El símbolo o nombre delatan un token de Reental, en cualquier grafía?"""
    return "reental" in f"{tx_symbol or ''} {tx_name or ''}".lower()
