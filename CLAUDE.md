# Notas para quien retome este proyecto

Escrito para la siguiente persona o IA que trabaje aquí. El `README.md` explica
**qué es** la herramienta; esto explica **cómo trabajar en ella** y, sobre todo,
qué ya ha salido mal. Cada punto de la sección de trampas corresponde a un error
que llegó a producción o estuvo a punto.

---

## El repositorio es PÚBLICO

`github.com/javiergispert/reental-analisis-wallet` es público desde junio de
2026. Cualquiera puede leer el código y los ficheros de `data/`, y Google los
indexa.

Consecuencias al trabajar aquí:

- **Nada de enlaces a hojas internas ni identificadores de documentos en el
  código.** Van a variables de entorno. No abren la puerta a nadie —esas hojas
  tienen sus propios permisos— pero dicen que la puerta existe y dónde está, que
  es lo que necesita una suplantación. El caso concreto: el Excel de OFF-RAMP,
  con nombre, correo, IBAN y certificado de titularidad de cada inversor, estuvo
  enlazado a fuego en `otc_protocolos.py` durante 27 días.
- **Las direcciones de `data/` van seudonimizadas.** Había 952 wallets de
  inversores con su histórico de compraventas e importes. Ahora aparecen como
  `inv_xxxxxxxx` y las exportaciones en bruto viven en `data/*/crudo/`, que está
  en `.gitignore`. Antes de commitear una exportación nueva hay que pasar
  `scripts/anonimizar_secundario.py` — el flujo mensual está en el README.
  **Aviso: el historial de git conserva las versiones antiguas con las
  direcciones reales.** Limpiarlo de verdad exige reescribir el historial
  (`git filter-repo`), que cambia todos los hashes de commit; está sin hacer.
- Sin licencia, nadie puede reutilizar el código legalmente aunque lo vea.

## Quién la usa y para qué

La usa el equipo **Reental Wealth** —gestores de cartera y comerciales— y en
parte el equipo de operaciones. No es una herramienta de desarrollo: lo que sale
por pantalla se le enseña a inversores reales y lo que se guarda compromete
tokens de verdad.

Eso marca dos prioridades por encima de la elegancia del código:

1. **Ningún número inventado.** Si un dato no se puede medir, se deja en blanco y
   se dice por qué. Nunca se rellena con una aproximación silenciosa — hay varios
   casos abajo donde eso fue exactamente el fallo.
2. **Nada que comprometa dinero sin confirmación explícita.** Las reservas OTC y
   los documentos que salen al inversor llevan paso de confirmación.

## Cómo está escrito el código

- **Todo en español**: nombres, comentarios y docstrings. Mantenlo.
- **Los docstrings explican el porqué, no el qué.** El patrón es: qué hace, por
  qué existe y qué alternativa se descartó. Si añades una función que corrige un
  error sutil, deja escrito cuál era.
- **Regla del repositorio: la lógica que usan dos páginas vive en un módulo
  común, nunca duplicada.** Es la regla que más se ha incumplido y la que más
  errores ha provocado (ver trampa 2).
- Los módulos de cálculo (`coste_prestamo`, `propuesta`, `maestro`, `divisas`)
  **no importan Streamlit**, para poder probarlos desde un script.

## Cómo probar

No hay suite de tests. Lo que funciona:

- **Scripts sueltos** que importan el módulo y comparan contra una cifra
  conocida. El motor de propuestas se validó reproduciendo una propuesta real
  del equipo: cuadraba al céntimo en importes y dentro del 0,5 % en escenarios.
- **La aplicación de verdad**: `streamlit run app.py` y recorrer el flujo. Varios
  fallos solo aparecen ahí (ver trampa 10).
- Si tocas una página que escribe datos —OTC—, pruébala sin llegar a guardar.

---

## Trampas conocidas

### 1. Los datos OTC no están en disco

**La fuente de verdad es Google Sheets**, vía `otc_storage.read_list()`. Nunca
un fichero local.

Pasó de verdad: existían en la raíz un `otc_ofertas.json` y un
`otc_reservas.json` con una copia de junio de 2026 —ignorados por git, pero ahí—
y alguien analizó una oferta de 400 tokens de Atlanta 1 leyéndolos. En el
almacén real había **cero** ofertas activas. Los ficheros ya se han borrado y
siguen en `.gitignore`: si vuelven a aparecer, no son datos, son residuo.

### 2. Tres lectores del maestro, tres verdades

Conviven `utils.load_master_projects` (4 páginas), `maestro.proyectos` (lo nuevo)
y `load_tokens` dentro del Analizador. Cada uno trae un subconjunto distinto de
columnas.

Pasó de verdad: la hoja de patrimonio del informe fiscal etiquetaba la emisión de
tokenización deduciéndola de la divisa del proyecto, porque el lector que se
estaba usando no traía la columna R. **23 de 127 proyectos salían mal** — hay 22
proyectos de la emisión estadounidense denominados en euros y uno español en
dólares. Emisión, divisa y ubicación son tres cosas distintas.

**Los dos lectores resuelven ya las columnas por el nombre de su cabecera** y
solo caen a la posición si no la encuentran o si el nombre está repetido. Es la
defensa contra lo que de verdad pasa: el maestro lo editan personas y una
columna insertada en medio desplaza todo lo que viene detrás **sin que nada
falle**. Comprobado simulando esa inserción: el lector antiguo devolvía
`ubicacion='nan'` y la ubicación metida en la columna de tipología; el de ahora
devuelve ambas bien.

Lo que sigue pendiente es tener **un solo lector**. `load_master_projects`
deriva campos propios —tipo de renta, si el proyecto es colateralizable, y la
preferencia de dato real sobre estimado en los cerrados— de los que dependen
dos páginas, así que fundirlo con `maestro.proyectos` no es mecánico.

### 3. `eth_call` a un bloque antiguo devuelve el estado de HOY

El nodo público de Etherscan ignora la etiqueta de bloque y responde con datos
actuales **sin error**. Es peor que fallar: devuelve cifras verosímiles.

Comprobado: el mismo precio del RNT para 2023, 2024, 2025 y 2026.

Todo lo histórico se reconstruye de **eventos** (`getLogs`): reservas del pool
desde `Sync`, supply desde los `Transfer` de emisión y quema. Ver `pool_rnt.py`.

### 4. Las ventanas de las APIs gratuitas

- **CoinGecko**: solo los últimos 365 días. El código antiguo caía al tipo de
  cambio de **hoy** para fechas anteriores, sin avisar — un dividendo de 2023 se
  convertía al cambio de 2026 en un informe fiscal.
- **GeckoTerminal**: 180 días. No sirve como alternativa.
- La solución fue el **BCE** para divisas (`divisas.py`, histórico completo) y el
  **pool** para el RNT (`pool_rnt.py`, sin límite).

Ya unificado: el precio del RNT sale siempre del pool y el tipo de cambio
siempre del BCE, en toda la herramienta. Las fuentes antiguas —CoinGecko para el
RNT, er-api para las divisas— quedan solo como respaldo si la principal falla, y
cuando se usan se dice. **No vuelvas a introducir una segunda fuente para un
dato que ya tiene la suya**: las dos cifras divergen y acaban en documentos
distintos para la misma operación.

### 5. Un hash sin clave no anonimiza nada

Seudonimizar direcciones con `sha256(direccion)` aquí no protegería: el conjunto
de candidatos es **enumerable**. Cualquiera puede listar desde la cadena todas
las direcciones que han tenido un token de Reental, hashearlas y cruzarlas. La
reidentificación sería completa y en minutos.

Por eso `seudonimos.py` usa HMAC con una clave secreta (`SEUDONIMO_SALT`, fuera
del repositorio) y **se niega a ejecutarse sin ella**: un valor por defecto en
un repositorio público equivale a no tener clave, y el fallo sería silencioso
—los ficheros parecerían anonimizados.

La clave hay que conservarla. Si se pierde, los seudónimos nuevos dejan de
cuadrar con los antiguos y una misma wallet se contaría dos veces.

### 6. Los tokens de un tercero PASAN por la wallet OTC

`disponibles_reental` ignoraba las reservas contra ofertas de terceros, con
este argumento escrito en el código: *«salen de la wallet del inversor, no del
inventario de Reental»*. **Era falso.** En el proceso real, todo token de un
tercero viaja primero a la custodia de Reental y de ahí al comprador.

Durante esa escala aparecía como stock libre y otro comercial lo reservaba: la
misma cantidad comprometida dos veces. Y de paso, como el saldo del inversor
caía a cero, la oferta se marcaba 🔴 «faltan 400» justo cuando el tercero
acababa de hacer lo correcto.

Dos hechos del proceso que hay que tener presentes:

- **Reental paga al recibir**, aunque el comprador todavía no haya pagado. En
  cuanto los tokens entran en la custodia ya son de Reental; lo único que falta
  es entregarlos.
- **A la custodia entran constantemente tokens que Reental compra para su
  propio libro** —467 entradas desde 230 wallets distintas en el histórico—.
  Por eso NO vale una regla del tipo «entró algo, luego llegó una reserva».

La atribución exige las cuatro condiciones a la vez: ese token, desde la wallet
de la oferta de esa reserva, después de la reserva, y con reserva viva. Va en
`otc_inventario.llegadas_de_terceros` y es **derivada, no almacenada**: se
recalcula en cada carga desde la cadena, así que no hay nada que marcar a mano
ni banderas que se queden obsoletas.

Queda un caso ambiguo: un inversor con oferta publicada que además le venda a
Reental por su cuenta el mismo token. Ahí se atribuiría de más y se bloquearía
una venta — el error cae del lado seguro, nunca duplica una reserva.

### 7. Un fallo de red que parece «no hay datos»

Una función que devuelve lista vacía tanto si no hay resultados como si la API
falló es una bomba de relojería. `pool_rnt._logs` lo hacía: un límite de
peticiones a media reconstrucción cortaba el recorrido y la serie se guardaba
**como si estuviera completa**. Un supply corto no se nota a simple vista y
además INFLA el valor de cada participación, porque se divide por él.

Ahora levanta `ConsultaFallida` tras reintentar con espera creciente, y
`scripts/snapshot_pool_rnt.py` **contrasta la serie reconstruida contra el
`totalSupply` del contrato antes de escribir**: si no cuadra, no guarda.

La regla general: cuando reconstruyas un estado sumando eventos, busca una
cifra independiente contra la que contrastarlo y compárala antes de dar el
resultado por bueno.

### 8. Cada lectura del Sheet costaba tres viajes

`otc_storage` reconstruía las credenciales y llamaba a `authorize` —un token
OAuth por red— en CADA lectura, y además reabría el libro con `open_by_key`,
que es otra llamada. Con tres pestañas que leer en cada recarga de la página
de OTC, eran nueve viajes a Google antes de pintar nada. Y como las pestañas
de la lista de reservas son botones que llaman a `st.rerun()`, cambiar de
«Activas» a «Completadas» pagaba la cuenta entera.

Ahora el cliente y el libro se guardan con `cache_resource` —son conexiones,
no datos— y las tres pestañas se traen en **una sola** petición con
`values_batch_get`. La caché de lectura sigue siendo de 6 segundos y se
invalida en cada escritura.

**No subas ese TTL para ganar velocidad.** La comprobación de disponibilidad
al crear una reserva se hace contra esos datos, y alargar la ventana aumenta
el riesgo de comprometer los mismos tokens dos veces. La velocidad se gana
quitando viajes, no mirando datos más viejos.

### 9. Las reservas se pisan entre sí

Hay un incidente documentado de pérdida de reservas (22/07/2026) por guardar la
copia leída al pintar la página. **Releer la lista fresca justo antes de
escribir**: `_store.read_list(TAB, fresh=True)` y añadir encima.

### 10. Streamlit: cuatro cosas que no son evidentes

- **`st.cache_data` indexa por los argumentos, no por el cuerpo de la función.**
  Si cambias la forma de lo que devuelve, la caché vieja sigue sirviéndose y
  revienta al usarla. Pasa un número de esquema **como argumento explícito y
  sin guion bajo delante** —en Streamlit el guion bajo significa «no formes
  parte de la clave», que es justo lo contrario de lo que hace falta.
  Ha pasado dos veces: un `KeyError` con el dato de salud de Aave, y un
  `ValueError` en la página de OTC al pasar `fetch_otc_balances` de devolver
  tres elementos a cuatro. En local no se ve, porque el servidor arranca con
  la caché vacía; en Streamlit Cloud, no. Ver `ESQUEMA_SALDOS` y
  `ESQUEMA_CATALOGO`.
- **No recarga los módulos propios tras un despliegue.** Para eso está
  `recarga.refrescar("modulo")`. Limitación: arregla `modulo.funcion()` pero no
  `from modulo import funcion`, que queda enlazado al objeto viejo. Si aparece
  HTML en crudo o un `AttributeError` raro después de desplegar, es esto: un
  **Reboot** desde *Manage app* lo resuelve.
- **`st.download_button` reejecuta el script entero al pulsarlo.** El fichero se
  baja al instante pero la página se queda pensando varios segundos rehaciendo
  todo. Envuélvelo en `@st.fragment` y pasa los bytes como argumento.
- **Desempaquetar tuplas en los bucles ata el código a su forma.** Una selección
  pasó de `(proyecto, tokens)` a `(proyecto, tokens, actuales)` y quedó un sitio
  desempaquetando dos. Accede por índice.

### 11. Criterios de cálculo que no son obvios

- **La cartera se pondera por importe invertido, no por número de tokens.** Un
  token de 100 € y otro de 100 $ no son la misma inversión; hoy el europeo pesa
  un 14 % más. La plantilla de Excel del equipo pondera por tokens.
- **El coste de adquirir un estatus va en el denominador.** Publicar la
  rentabilidad solo sobre la cartera inmobiliaria hace que el salto a
  SuperReentel parezca casi tres veces mayor de lo que es. Se dan las dos cifras,
  con su explicación.
- **El RNT del estatus no se consume**: se conserva y genera rendimiento en
  staking. No es un gasto, y por eso no se presenta como tal.
- **Los escenarios de reinversión modelan el flujo de cada proyecto**: renta
  recurrente mes a mes y plusvalía al cierre. Aplanar la rentabilidad de la
  cartera sobre el horizonte adelanta dinero que todavía no existe.
- **El FIFO de plusvalías recorre todo el histórico anterior** aunque el informe
  sea de un solo ejercicio: hace falta para conocer el coste de los lotes.

---

## Pendiente y consciente

Cosas decididas a medias o sabidas y no hechas:

- **Legal y Compliance** no ha revisado ni el informe de Mercado Aave ni el
  dossier de propuestas. Ambos salen con **marca de agua de borrador interno** y
  el interruptor para quitarla no debe tocarse hasta que haya visto bueno. El
  motivo es no dar pie a que la CNMV lo lea como material comercial.
- **El generador de propuestas de Ainhoa** (una plantilla de Google Sheets con
  Apps Script, menú "⭐ Reental Wealth") sigue existiendo. Hay dos generadores y
  hay que decidir cuál manda.
- **Las cuatro direcciones que repartían RNT ya están identificadas** (producto,
  30/09/2026), y eran tres cosas distintas. Queda pendiente **revisar los
  informes fiscales ya entregados**, porque el criterio ha cambiado:

  | Dirección | Qué es | Cómo se trata ahora |
  |---|---|---|
  | `0x21aaf98e74f2ad1ca487dc20f598e6bdd89e24ad` | Vesting de RNT, tramo oct-2024 → oct-2026 | **Renta** — `Vesting de RNT liberado` |
  | `0xcb6420b380b7ceb0317208f3568c2c5009bd6c25` | Vesting de RNT, tramo oct-2023 → oct-2025 | **Renta** — `Vesting de RNT liberado` |
  | `0xed5b64603e254aab6d2dd7f6128fee8d8d567d5e` | Wallet de un beneficiario del vesting (persona física) | Sigue sin calificar — `Recepción de RNT` |
  | `0x51e3d44172868acc60d68ca99591ce4230bc75e0` | Hot wallet de un exchange centralizado, ajena a Reental | **No es renta**, y el coste va a «Por completar» |

  Los dos primeros son el mismo contrato desplegado dos veces
  (`RNTDistributionVaultMerkleVesting`, proxy UUPS). Lo que liberan es
  retribución en especie: el beneficiario no pagó por esos tokens. La
  herramienta aporta importe y fecha; **la categoría concreta —trabajo,
  actividad o capital— depende de la relación del beneficiario con Reental y la
  pone el asesor**, no el código.

  El tercero enseña por qué no vale agrupar por volumen: parecía un distribuidor
  porque movía 196.514 RNT, y es un particular repartiendo lo que le fue
  liberando su propio vesting. Una transferencia entre particulares puede ser
  compra, pago o donación, y cada una tributa distinto, así que se queda sin
  calificar a propósito.

  El cuarto es el aviso de que no todo lo que mueve RNT es de Reental. Un RNT
  que llega de un exchange no es renta —ya era del inversor— pero su coste se
  fijó dentro del exchange y no consta en la cadena: si no se pide, una venta
  posterior calcula la plusvalía **sobre un coste de cero**, que es el error más
  caro que puede cometer este informe.

  **La lección, que es la de siempre aquí:** las cuatro se habían dejado sin
  contar «hasta saber qué son», lo cual era prudente, pero nadie preguntó
  durante meses. Lo que destrabó el asunto fue un tercero —un despacho fiscal—
  declarando como renta 246.351,69 RNT de 2025 que la herramienta ignoraba.
  Cuando algo queda «pendiente de identificar», hay que ponerle fecha para
  preguntar.

- **El SLP repartido en los claims de staking** ya se valora (parte proporcional
  de las reservas del pool), pero conviene contrastarlo con Reental.
- **`data/pool_rnt/supply.json` no tiene workflow todavía**: el fichero YAML
  está escrito y pendiente de subir desde la web de GitHub (un token personal no
  puede crear workflows). Mientras tanto se actualiza a mano con
  `scripts/snapshot_pool_rnt.py`, y conviene no olvidarlo: en ocho días sin
  actualizar, el supply se quedó un 5 % corto.
- **`requirements.txt` sin versiones fijadas** salvo `kaleido`.
- **Redundancia de estilos de PDF**: tres páginas repiten paleta, estilos y tabla
  estándar (~150 líneas ×3).
- **`pages/Analizador_de_Wallets.py` tiene 4.883 líneas**, el 29 % del
  repositorio. El informe fiscal (~1.000) saldría limpio a su propio módulo.

---

## Convenciones de git

- Mensajes de commit **en español**, explicando el porqué del cambio y no solo el
  qué. Ver el historial: son largos a propósito.
- El bot de la Action commitea la foto de Aave a `main`. Antes de `push`, `git
  pull --rebase`.
- Nunca commitear `.env`, los `otc_*.json` ni ficheros `RESTAURAR_*`, que llevan
  datos personales de inversores.
