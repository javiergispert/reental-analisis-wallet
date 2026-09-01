# Foto diaria del mercado RNT Lend

`snapshot.json` es el estado del pool ya digerido. La página **Mercado Aave** lo
lee en vez de reconstruirlo, y por eso carga en segundos en lugar de en minutos.

## Por qué

Esa página no depende de ningún input del usuario —siempre mira los mismos
datos—, así que reconstruirlos en cada visita era repetir el mismo trabajo una y
otra vez:

| Pieza | Llamadas | Tiempo |
|---|---|---|
| Lista de reservas + config de cada una | ~104 | ~42 s |
| `totalSupply` de cada reserva | ~103 | ~41 s |
| Histórico de eventos `Transfer` | ~40 páginas | ~40 s |
| Posición de cada prestatario | ~337 | ~135 s |

De todo eso **solo el histórico es acumulativo**, y la desproporción es enorme:

```
eventos acumulados desde el despliegue:  19.972
eventos de un día cualquiera:                21
```

Reconstruirlo entero cada vez era leer veinte mil eventos para encontrar
veintiuno.

## Qué se guarda y qué no

| Dato | Origen | Frescura |
|---|---|---|
| Aportado, prestado, APR, utilización | **En vivo** (6 llamadas, ~3 s) | Al segundo |
| Histórico, concentración, colateral, salud | `snapshot.json` | Diaria |
| Nombres, ubicación, rentas estimadas | CSV maestro | Al abrir la página |

Los KPIs de cabecera no se guardan: son el titular de la página y cuestan tres
segundos. Y del maestro no se guarda nada, de forma que corregir una fecha o una
tipología se ve al instante sin regenerar la foto.

La página **siempre dice de cuándo es el dato**, y avisa si pasa de 36 horas sin
actualizarse.

## Actualización

`.github/workflows/snapshot_aave.yml` corre a las **04:30 UTC** y commitea el
fichero si ha cambiado. También se puede lanzar a mano desde la pestaña Actions.

En local:

```bash
python3 scripts/snapshot_aave.py             # incremental, ~3 min
python3 scripts/snapshot_aave.py --completo  # desde el bloque 0, ~15 min
```

El pase incremental arranca en el último bloque leído, guardado en el propio
fichero. Usa `--completo` solo si se sospecha que el acumulado se corrompió.

Si el pase falla, **no toca el fichero**: es preferible la foto de ayer a
ninguna. Y si el fichero no existe, la página lo detecta y cae al camino en vivo
de siempre — más lento, pero nunca se queda sin datos.

## Requisito

La Action necesita el secreto **`ETHERSCAN_API_KEY`** en el repositorio
(*Settings → Secrets and variables → Actions*). Sin él, el pase falla y la
página sigue tirando de la última foto commiteada.

## Estructura

```
esquema        versión del formato; si no coincide, se ignora el fichero entero
generado       ISO-8601 UTC
bloque         último bloque de Polygon leído
tokens_stable  USDT/USDC → direcciones de reserva, aToken y debt token
stables        circulante y tipos de cada stablecoin
colateral      [{token_address, colateral_tokens}] de cada proyecto Reental
estados        por aToken/debtToken: saldos por dirección, serie diaria,
               total en circulación y último bloque procesado
salud          agregado de riesgo (ver aave_lend.salud_agregada)
tipos          serie histórica de APR por reserva
```

`estados` es lo que hace posible lo incremental: guarda el resultado de plegar
los eventos, no los eventos. Guardarlos crudos ocuparía megas sin aportar nada
que no se pueda reconstruir aplicando los nuevos sobre este acumulado.
