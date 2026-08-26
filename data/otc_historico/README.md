# OTC interno — histórico 2025-2026

Operaciones OTC intermediadas por Reental **antes** de que existiera el sistema
de reservas de `pages/02_OTC.py`, que se creó en junio de 2026. Se llevaban en
hojas aparte, así que **no se solapan con la hoja de Reservas**: son periodos
disjuntos y se suman sin contar dos veces.

Cubren del **03/06/2025 al 13/05/2026**: 256 operaciones y 1.586.001 USD.

## Dos formatos, un mismo destino

Los registros manuales no comparten esquema, y el script los adapta a ambos:

| Fichero | Qué es | Esquema |
|---|---|---|
| `otc_2025.csv` | Ventas entre inversores | comprador y vendedor, importe en EUR |
| `otc_2025-2026_recompras.csv` | **Recompras de Reental** | solo la wallet del inversor (que vende); divisa del proyecto y de pago separadas, con pagos en EUR y en USDT |

En las recompras el comprador es siempre la wallet de custodia, y por eso el
registro no lo anota.

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
| **Separadores ambiguos** en los importes: `€413.00`, `10.044 usdt`, `8,820,00 EURO` conviven en la misma columna | varias | Se generan las lecturas posibles y se elige la más cercana a `Tokens × Valor del token` |
| **La cadena casa el `Transfer` equivocado** (la TX mueve el token por otro motivo) | 1 | Si el precio resultante es disparatado frente al importe pagado, gana el registro |

**Erratas de ID detectadas** (transposiciones de letras, confirmadas en cadena):

```
CLMV-1, CVLM-1 → CLVM-1      DBX-1 → DXB-1      DBX-2    → DXB-2
MBL-1          → MRB-1       SAL-2 → SLA-2      RENTAS 1 → RET-1
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
p5 99,35 $   ·   mediana 103,03 $   ·   p95 121,48 $
operaciones fuera de la banda 50–250 $/token: 1
```

Ese contador de anomalías es la comprobación rápida de que la normalización ha
funcionado: llegó a estar en 26 mientras los lotes se repartían mal. La que
queda es una fila realmente vacía en el origen (0 tokens, importe 0 €).
