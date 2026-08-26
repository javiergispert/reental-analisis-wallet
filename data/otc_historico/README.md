# OTC interno — histórico 2025

Operaciones OTC intermediadas por Reental **antes** de que existiera el sistema
de reservas de `pages/02_OTC.py`, que se creó en junio de 2026. Se llevaban en
una hoja aparte, así que **no se solapan con la hoja de Reservas**: son periodos
disjuntos y se suman sin contar dos veces.

## Por qué importa

En junio–noviembre de 2025 el OTC movió **1,18 M USD** frente a los 450 K del
P2P en el mismo periodo. Sin estos datos, la sección de profundidad del mercado
secundario mostraba menos de un tercio de lo que realmente se transaccionó.

## Cómo actualizar

1. Dejar el fichero en `exports/` con un nombre descriptivo (`otc_AAAA.csv`).
2. Ejecutar:

   ```bash
   python3 scripts/normalizar_otc_historico.py
   ```

3. Commitear el CSV nuevo y `normalizado.csv`.

Se cargan todos los ficheros de `exports/`, así que añadir otro periodo no
requiere tocar código.

## Qué arregla la normalización

El registro era manual y trae cuatro problemas. Todos se resuelven contra la
cadena, que es la fuente fiable, y cada fila queda marcada con el origen del
dato en `origen_tokens` y `origen_importe`.

| Problema | Alcance | Cómo se resuelve |
|---|---|---|
| **Cantidades corrompidas por el locale**: `29,94342341` quedó como `2.994.342.341` | 82 de 201 | Se lee el `Transfer` real de la transacción |
| **Lotes**: una transacción liquida varias operaciones | 29 transacciones | Se cruza por vendedor y comprador; si comparten un único `Transfer`, se reparte en proporción a lo pagado |
| **IDs con erratas de tecleo** | 5 | Mapa de alias, cada uno confirmado viendo qué token se movió |
| **Importes vacíos** | 11 | Se valoran a precio de emisión y se marcan como estimados |

**Erratas de ID detectadas** (transposiciones de letras, confirmadas en cadena):

```
CLMV-1, CVLM-1 → CLVM-1      DBX-1 → DXB-1
MBL-1          → MRB-1       SAL-2 → SLA-2
```

Y una fecha con un cero de más (`02/08/20025`), reparada al parsear.

## Divisa

Los importes originales están en **euros**. Se convierten a USD con el tipo del
BCE **de cada fecha** (`frankfurter.dev`), no con uno fijo: entre junio y
noviembre de 2025 el EUR/USD osciló un 3,8 % (1,1386 – 1,1818), suficiente para
falsear una comparación por meses.

Se conservan las tres columnas —`importe_eur`, `eur_usd`, `importe_usd`— para
poder auditar la conversión.

## Control de calidad

Tras normalizar, el precio por token queda en una banda coherente con los
precios de emisión:

```
p5 84,83 €   ·   mediana 88,79 €   ·   p95 110,00 €
operaciones fuera de la banda 50–200 €/token: 0
```

Ese contador de anomalías es la comprobación rápida de que la normalización ha
funcionado: llegó a estar en 26 mientras los lotes se repartían mal.
