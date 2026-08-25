# Histórico del mercado secundario RNTP2P

Operaciones cerradas en [p2p.rnt.finance](https://p2p.rnt.finance), el mercado
donde los inversores intercambian tokens inmobiliarios entre ellos.

## Actualización mensual — qué hacer

1. Descargar la exportación **Finalized** desde p2p.rnt.finance.
2. Guardarla en `exports/` con el nombre `finalized_AAAA-MM-DD.csv`
   (la fecha es la de la exportación, no la del último dato).
3. Ejecutar:

   ```bash
   python3 scripts/enriquecer_p2p.py
   ```

4. Commitear el CSV nuevo y `enriquecido.csv` actualizado.

No hay que borrar exportaciones antiguas ni tocar código: se cargan todas y se
deduplica por `hash`. El enriquecimiento es incremental — solo trabaja las
operaciones que aún no estén resueltas.

## Por qué la cadencia importa

**La plataforma purga el detalle de las operaciones pasadas unas semanas.** En
la exportación del 25/08/2026, de 4.605 operaciones solo 116 conservaban
`propertyName`, `tokenAddress`, `amount` y `listingTime`; las demás traían esos
campos vacíos. Las 116 eran justo las de los 20 días anteriores.

Lo que se exporta a tiempo se conserva completo. Lo que no, hay que
reconstruirlo desde la cadena — que se puede, pero cuesta más y no recupera la
**fecha de listado**, así que se pierde para siempre el tiempo que tardó en
venderse esa operación.

## Ficheros

| Fichero | Qué es |
|---|---|
| `exports/finalized_*.csv` | Exportaciones tal cual salen de la plataforma. No se editan. |
| `enriquecido.csv` | Generado por `scripts/enriquecer_p2p.py`: proyecto, cantidad de tokens, vendedor y comprador reconstruidos desde la cadena. |

## Cómo leer los campos (esto ya lo hace `p2p_mercado.py`)

| Campo | Decimales | Ojo |
|---|---|---|
| `matchedPrice` | 6 (USDT/USDC) | Es el **importe TOTAL** de la operación, no el precio unitario. |
| `amount` | 18 | Cantidad de tokens. Vale `0` en las filas purgadas. |

Verificado contra la cadena en tres puntos del histórico (10/01/2024,
29/05/2025 y 25/08/2026), comparando con los `Transfer` reales de cada
transacción.

**`maker` y `taker` NO son vendedor y comprador.** Son los roles de la orden: el
maker unas veces publica una venta y otras una compra. Quién vendió solo se sabe
por la dirección del `Transfer` del token, y por eso el enriquecimiento guarda
`vendedor` y `comprador` aparte.

## Contrato

Las operaciones se ejecutan contra `0x77ff7fcf6b581be21c6a88c36883a788b9f2a99f`
(método `atomicMatch_`, `0xab834bab`), el mismo desde enero de 2024. Los tokens
van directos de wallet a wallet, sin custodia intermedia.

**Publicar una oferta no deja rastro en la cadena**: son órdenes firmadas fuera
de ella y solo la ejecución se registra. Por eso este histórico contiene
operaciones cerradas, y el libro de órdenes abierto de RNTP2P no es medible
desde aquí. En el OTC propio sí lo tenemos.

No confundir con estos otros contratos, que también mueven tokens y stablecoins
pero **no son mercado secundario**:

| Contrato | Qué es |
|---|---|
| `0x16199f7c6c7441224fa16b52a696b16be0cc7302` | Router de venta primaria |
| `0xe5e9a22e93f6d6ab533e7c699c22c766a2536da0` | Vault de inventario de Reental |
| `0xfd1fb402260da84435325b2acd5ec026a1b2dbb3` | Tesorería (Gnosis Safe) |
| `0x67dc8037db6309dd5571d82c65f5f593f7da1505` | Pool de Aave (colateral) |
