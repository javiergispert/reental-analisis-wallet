"""
Lo que cuesta de verdad pedir prestado contra el colateral.

La herramienta ya explica bien el RIESGO de una posición apalancada —índice de
salud, umbral de liquidación, cuánto puede caer el colateral— pero no decía nada
de su COSTE. Y el coste es lo que decide si la operación tiene sentido, porque
un préstamo solo compensa si lo que se compra con él rinde más de lo que cuesta.

Hay tres cosas que se confunden con facilidad:

1. EL APR NO ES LO QUE PAGAS. Aave capitaliza la deuda variable de forma
   continua, así que el tipo efectivo anual es `e^APR − 1`, no el APR. Con los
   tipos actuales la diferencia es de medio punto largo — poco, y por eso pasa
   desapercibida.

2. SIN PAGAR INTERESES, LA DEUDA CRECE SOLA. Aquí es donde la diferencia deja de
   ser pequeña: a un 12,1% de APR, cinco años sin atender el préstamo no cuestan
   el 60,7% del principal (12,1 × 5) sino el 83,4%. A diez años, la deuda más
   que se triplica. Es la misma acumulación que ya usamos para calcular cuándo
   se liquida una posición, mirada como gasto en vez de como riesgo.

3. LOS IMPUESTOS ROMPEN LA SIMETRÍA. Si los intereses no son deducibles, se
   pagan con dinero que ya tributó, mientras la ganancia sí tributa. Entonces no
   basta con rendir más que el APY: hay que rendir `APY / (1 − t)`. Con un
   marginal del 21%, un préstamo al 12,90% efectivo exige un 16,32% bruto solo
   para empatar.

SOBRE LO FISCAL
---------------
Aquí no se afirma cuál es el tratamiento fiscal de nadie: eso depende del caso
concreto y no nos corresponde. Lo que se hace es ARITMÉTICA sobre supuestos que
introduce quien usa la herramienta —su tipo marginal, y si puede deducir los
intereses o no—, para que pueda ver la sensibilidad. La respuesta a «¿puedo
deducirlos?» es de un asesor fiscal, no de este módulo.
"""
from __future__ import annotations

import math

# Tramos de la base del ahorro del IRPF español, como SUGERENCIA para rellenar
# el tipo marginal. Son orientativos y el usuario puede escribir otro: alguien
# puede tributar en otra jurisdicción, o ser una sociedad.
TRAMOS_AHORRO = [
    ("Hasta 6.000 €", 0.19),
    ("6.000 – 50.000 €", 0.21),
    ("50.000 – 200.000 €", 0.23),
    ("200.000 – 300.000 €", 0.27),
    ("Más de 300.000 €", 0.30),
]


def apy(apr: float) -> float:
    """Tipo efectivo anual de una deuda que capitaliza en continuo.

    Es lo que se paga de verdad en un año, frente al APR que publica el
    contrato. Aave acumula el interés en cada bloque sobre el saldo ya
    acumulado, y el límite de esa capitalización es la exponencial.
    """
    if apr is None or apr <= 0:
        return 0.0
    return math.exp(apr) - 1.0


def coste_acumulado(apr: float, anos: float) -> float:
    """Coste total de no pagar intereses durante `anos`, como fracción del
    principal. Con 0,5 devuelve el coste a seis meses.

    Es la diferencia entre lo que la gente calcula de cabeza (APR × años) y lo
    que realmente debe al final.
    """
    if apr is None or apr <= 0 or anos is None or anos <= 0:
        return 0.0
    return math.exp(apr * anos) - 1.0


def coste_lineal(apr: float, anos: float) -> float:
    """La lectura ingenua —APR × años— para poder enseñar las dos juntas."""
    if apr is None or apr <= 0 or anos is None or anos <= 0:
        return 0.0
    return apr * anos


def rentabilidad_de_equilibrio(apr: float, tipo_marginal: float = 0.0,
                               deducible: bool = False) -> float:
    """Rentabilidad BRUTA anual que debe dar lo comprado con el préstamo para
    que la operación no pierda dinero.

    Sin impuestos el umbral es el propio APY. Con impuestos hay dos casos:

      * Intereses NO deducibles: la ganancia tributa entera y el interés sale de
        dinero ya tributado, así que `y·(1−t) = APY`  →  `y = APY / (1−t)`.
      * Intereses deducibles: solo tributa el margen, `(y−APY)·(1−t)`, y el
        umbral vuelve a ser el APY. La deducibilidad neutraliza el efecto.

    Que el segundo caso salga idéntico al de sin impuestos no es un descuido: es
    exactamente lo que significa poder deducir.
    """
    coste = apy(apr)
    if not tipo_marginal or tipo_marginal <= 0 or deducible:
        return coste
    if tipo_marginal >= 1:
        return float("inf")
    return coste / (1.0 - tipo_marginal)


def margen_neto(rentabilidad_bruta: float, apr: float,
                tipo_marginal: float = 0.0, deducible: bool = False) -> float:
    """Lo que queda al año, ya neto de impuestos y del coste del préstamo, por
    cada euro prestado. Negativo significa que la operación destruye valor."""
    if rentabilidad_bruta is None:
        return 0.0
    coste = apy(apr)
    t = tipo_marginal or 0.0
    if deducible:
        return (rentabilidad_bruta - coste) * (1.0 - t)
    return rentabilidad_bruta * (1.0 - t) - coste


def resumen(apr: float, anos: float, rentabilidad_bruta: float | None = None,
            tipo_marginal: float = 0.0, deducible: bool = False) -> dict:
    """Todo lo anterior de una vez, para que la interfaz no repita el cálculo."""
    equilibrio = rentabilidad_de_equilibrio(apr, tipo_marginal, deducible)
    out = {
        "apr": apr,
        "apy": apy(apr),
        "coste_acumulado": coste_acumulado(apr, anos),
        "coste_lineal": coste_lineal(apr, anos),
        "equilibrio": equilibrio,
        "sobrecoste_fiscal": max(0.0, equilibrio - apy(apr)),
    }
    out["exceso_sobre_lineal"] = out["coste_acumulado"] - out["coste_lineal"]
    if rentabilidad_bruta is not None:
        out["rentabilidad_bruta"] = rentabilidad_bruta
        out["margen"] = margen_neto(rentabilidad_bruta, apr, tipo_marginal, deducible)
        out["sale_a_cuenta"] = rentabilidad_bruta > equilibrio
    return out
