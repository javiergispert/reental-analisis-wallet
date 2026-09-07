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


def coste_anualizado(apr: float, meses: float) -> float:
    """Coste efectivo ANUAL de dejar correr el préstamo `meses` entre pagos.

    Cada cuánto se atienden los intereses cambia lo que se paga, porque lo que
    no se paga capitaliza. Si se salda cada `M` meses, en un año se hacen `12/M`
    pagos de `e^(r·M/12) − 1` sobre el principal:

        coste anual = (12 / M) · (e^(r·M/12) − 1)

    La función es creciente en `M`, y sus dos extremos son justo los tipos que
    ya se manejan:

        M → 0    →  el APR      (pagando en continuo, nunca capitaliza)
        M = 12   →  el APY      (pagando una vez al año)
        M = 120  →  23,6%       (sin pagar en diez años)

    O sea que el APR y el APY no son dos convenciones rivales: son el mismo
    coste con dos frecuencias de pago distintas, y el tipo que de verdad asume
    el inversor está donde caiga su forma de operar.
    """
    if apr is None or apr <= 0:
        return 0.0
    if meses is None or meses <= 0:
        return apr                      # límite de pago continuo
    anos = meses / 12.0
    return (math.exp(apr * anos) - 1.0) / anos


def coste_total(apr: float, plazo_meses: float, frecuencia_meses: float | None = None) -> float:
    """Interés total pagado en todo el plazo, como fracción del principal.

    Con `frecuencia_meses` se salda cada N meses, así que el principal nunca
    crece y el coste es lineal en el plazo: `(T/N) · (e^(r·N/12) − 1)`.

    Sin frecuencia —o con una igual al plazo— no se paga nada hasta el final y
    el interés capitaliza sobre sí mismo, que es el caso caro.

    Con los tipos de hoy, diez años pagando cada mes cuestan un 122% del
    principal; los mismos diez años sin pagar nada, un 236%.
    """
    if apr is None or apr <= 0 or not plazo_meses or plazo_meses <= 0:
        return 0.0
    if not frecuencia_meses or frecuencia_meses >= plazo_meses:
        return coste_acumulado(apr, plazo_meses / 12.0)
    n_periodos = plazo_meses / frecuencia_meses
    return n_periodos * (math.exp(apr * frecuencia_meses / 12.0) - 1.0)


def rentabilidad_de_equilibrio(coste_anual: float, tipo_marginal: float = 0.0,
                               deducible: bool = False) -> float:
    """Rentabilidad BRUTA anual que debe dar lo comprado con el préstamo para
    que la operación no pierda dinero.

    Recibe el COSTE ANUAL EFECTIVO, no el APR. Antes tomaba el APR y usaba
    siempre su APY, con lo que el umbral ignoraba la frecuencia de pago que
    hubiera elegido el usuario: alguien que pensara no pagar en cinco años veía
    un umbral calculado como si pagara cada año. Ahora se le pasa el coste que
    corresponda al escenario.

    Sin impuestos el umbral es el propio coste. Con impuestos hay dos casos:

      * Intereses NO deducibles: la ganancia tributa entera y el interés sale de
        dinero ya tributado, así que `y·(1−t) = coste`  →  `y = coste / (1−t)`.
      * Intereses deducibles: solo tributa el margen, `(y−coste)·(1−t)`, y el
        umbral vuelve a ser el coste. La deducibilidad neutraliza el efecto.

    Que el segundo caso salga idéntico al de sin impuestos no es un descuido: es
    exactamente lo que significa poder deducir.
    """
    if coste_anual is None or coste_anual <= 0:
        return 0.0
    if not tipo_marginal or tipo_marginal <= 0 or deducible:
        return coste_anual
    if tipo_marginal >= 1:
        return float("inf")
    return coste_anual / (1.0 - tipo_marginal)


def renta_disponible(rentabilidad_bruta: float, tipo_marginal: float = 0.0,
                     deducible: bool = False) -> float:
    """Lo que queda de la rentabilidad para atender intereses, expresado en el
    MISMO eje que el coste.

    Es el espejo del umbral y sirve para poder dibujarlos juntos: comparar en
    un gráfico una curva de coste con una línea de rentabilidad bruta solo es
    válido si no hay impuestos. Con ellos hay que descontarlos primero, y lo que
    de verdad está disponible para pagar el préstamo es `y·(1−t)`.
    """
    if rentabilidad_bruta is None:
        return 0.0
    if deducible:
        return rentabilidad_bruta
    return rentabilidad_bruta * (1.0 - (tipo_marginal or 0.0))


def margen_neto(rentabilidad_bruta: float, coste_anual: float,
                tipo_marginal: float = 0.0, deducible: bool = False) -> float:
    """Lo que queda al año, ya neto de impuestos y del coste del préstamo, por
    cada euro prestado. Negativo significa que la operación destruye valor.

    También recibe el coste efectivo, por el mismo motivo que el umbral."""
    if rentabilidad_bruta is None:
        return 0.0
    coste = coste_anual or 0.0
    t = tipo_marginal or 0.0
    if deducible:
        return (rentabilidad_bruta - coste) * (1.0 - t)
    return rentabilidad_bruta * (1.0 - t) - coste


def resumen(apr: float, frecuencia_meses: float, plazo_meses: float | None = None,
            rentabilidad_bruta: float | None = None,
            tipo_marginal: float = 0.0, deducible: bool = False) -> dict:
    """Todo lo anterior de una vez, para que la interfaz no repita el cálculo.

    Devuelve DOS umbrales, porque responden a preguntas distintas:

      * `equilibrio_apy`  — con el APY publicado, es decir, pagando una vez al
        año. Sirve de referencia de mercado, comparable entre operaciones.
      * `equilibrio`      — con el coste efectivo del escenario que ha
        configurado el usuario. Es el que decide si SU operación sale a cuenta.

    Mezclarlos fue un error de la primera versión: el umbral se calculaba
    siempre sobre el APY aunque el usuario dijera que no pensaba pagar en cinco
    años, con lo que el escenario configurado no afectaba a la conclusión.
    """
    plazo_meses = plazo_meses or frecuencia_meses
    coste_efectivo = coste_anualizado(apr, frecuencia_meses)
    coste_sin_pagar = coste_anualizado(apr, plazo_meses)
    equilibrio = rentabilidad_de_equilibrio(coste_efectivo, tipo_marginal, deducible)
    out = {
        "apr": apr,
        "apy": apy(apr),
        "frecuencia_meses": frecuencia_meses,
        "plazo_meses": plazo_meses,
        "coste_anualizado": coste_efectivo,
        "coste_sin_pagar": coste_sin_pagar,
        "coste_total": coste_total(apr, plazo_meses, frecuencia_meses),
        "coste_total_sin_pagar": coste_total(apr, plazo_meses, None),
        "equilibrio": equilibrio,
        "equilibrio_apy": rentabilidad_de_equilibrio(apy(apr), tipo_marginal, deducible),
        "sobrecoste_fiscal": max(0.0, equilibrio - coste_efectivo),
    }
    if rentabilidad_bruta is not None:
        out["rentabilidad_bruta"] = rentabilidad_bruta
        out["renta_disponible"] = renta_disponible(rentabilidad_bruta, tipo_marginal, deducible)
        out["margen"] = margen_neto(rentabilidad_bruta, coste_efectivo,
                                    tipo_marginal, deducible)
        out["sale_a_cuenta"] = rentabilidad_bruta > equilibrio
    return out
