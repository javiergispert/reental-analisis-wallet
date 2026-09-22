"""
Conversión de importes a la divisa del informe.

POR QUÉ NO VALE LO QUE HABÍA
----------------------------
El tipo de cambio salía del precio de Tether en euros en CoinGecko, cuyo plan
gratuito solo sirve los últimos 365 días. Para una operación más antigua se
recurría al tipo de cambio de HOY, que es exactamente lo que un informe fiscal
no puede hacer: un dividendo de 2023 quedaba convertido al cambio de 2026 sin
que nada lo advirtiera.

Aquí se usa Frankfurter, que publica las referencias diarias del Banco Central
Europeo desde 1999. Es gratis, no pide clave y es la fuente que una hacienda
europea reconoce. Cubre 30 divisas —las que publica el BCE—, así que un
inversor mexicano o brasileño puede tener su informe en su moneda; un peso
argentino o colombiano no está, y es mejor decirlo que inventar un tipo.

CONVENIO
--------
`tipo` son unidades de la divisa por 1 USD, de modo que el importe convertido
es siempre una multiplicación. El BCE solo publica en días hábiles: para un
sábado, un domingo o un festivo se usa la última referencia anterior, que es
lo que hace cualquier contabilidad.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import requests

BASE = "https://api.frankfurter.dev/v1"

# Las 30 del BCE. Se dejan escritas para no gastar una llamada en pintar el
# desplegable, y porque esta lista no cambia de un mes para otro.
MONEDAS = {
    "EUR": "Euro", "USD": "Dólar estadounidense", "GBP": "Libra esterlina",
    "CHF": "Franco suizo", "MXN": "Peso mexicano", "BRL": "Real brasileño",
    "AUD": "Dólar australiano", "CAD": "Dólar canadiense", "JPY": "Yen japonés",
    "CNY": "Yuan chino", "SEK": "Corona sueca", "NOK": "Corona noruega",
    "DKK": "Corona danesa", "PLN": "Zloty polaco", "CZK": "Corona checa",
    "HUF": "Florín húngaro", "RON": "Leu rumano", "TRY": "Lira turca",
    "ILS": "Nuevo séquel israelí", "INR": "Rupia india", "IDR": "Rupia indonesia",
    "KRW": "Won surcoreano", "SGD": "Dólar de Singapur", "HKD": "Dólar de Hong Kong",
    "MYR": "Ringgit malayo", "PHP": "Peso filipino", "THB": "Baht tailandés",
    "NZD": "Dólar neozelandés", "ZAR": "Rand sudafricano", "ISK": "Corona islandesa",
}


def serie(divisa: str, desde: str, hasta: str) -> dict:
    """{fecha: tipo} para todo el rango, en UNA llamada. `desde`/`hasta` en
    formato YYYY-MM-DD. Vacío si la consulta falla: sin tipo es preferible
    dejar la columna en blanco a rellenarla con una aproximación."""
    if divisa == "USD":
        return {}
    try:
        r = requests.get(f"{BASE}/{desde}..{hasta}",
                         params={"base": "USD", "symbols": divisa}, timeout=20)
        r.raise_for_status()
        return {f: v[divisa] for f, v in r.json().get("rates", {}).items()
                if divisa in v}
    except Exception:       # noqa: BLE001
        return {}


def tipo_en(fecha: str, tabla: dict) -> float | None:
    """Tipo aplicable a una fecha: el del día, o el del último día hábil
    anterior si ese no cotizó (fin de semana, festivo)."""
    if not tabla:
        return None
    dia = fecha[:10]
    if dia in tabla:
        return tabla[dia]
    previos = [d for d in tabla if d <= dia]
    return tabla[max(previos)] if previos else None


def rango_necesario(fechas) -> tuple:
    """El intervalo que hay que pedir para cubrir esas fechas, con unos días de
    margen por detrás para que el primer día caiga en festivo y aun así haya
    referencia anterior."""
    dias = sorted({f[:10] for f in fechas if f})
    if not dias:
        hoy = date.today().isoformat()
        return hoy, hoy
    inicio = (datetime.strptime(dias[0], "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    fin = min(dias[-1], date.today().isoformat())
    return inicio, max(inicio, fin)
