# Notas para quien retome este proyecto

Escrito para la siguiente persona o IA que trabaje aquí. El `README.md` explica
**qué es** la herramienta; esto explica **cómo trabajar en ella** y, sobre todo,
qué ya ha salido mal. Cada punto de la sección de trampas corresponde a un error
que llegó a producción o estuvo a punto.

---

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
  fallos solo aparecen ahí (ver trampa 6).
- Si tocas una página que escribe datos —OTC—, pruébala sin llegar a guardar.

---

## Trampas conocidas

### 1. Los JSON de la raíz no son los datos

`otc_ofertas.json` y `otc_reservas.json` están en `.gitignore` y contienen una
copia de junio de 2026. **La fuente de verdad es Google Sheets**, vía
`otc_storage.read_list()`.

Pasó de verdad: alguien analizó una oferta de 400 tokens de Atlanta 1 leyendo el
JSON y razonó sobre ella durante un rato. En el almacén real había **cero**
ofertas activas.

### 2. Tres lectores del maestro, tres verdades

Conviven `utils.load_master_projects` (4 páginas), `maestro.proyectos` (lo nuevo)
y `load_tokens` dentro del Analizador. Cada uno trae un subconjunto distinto de
columnas.

Pasó de verdad: la hoja de patrimonio del informe fiscal etiquetaba la emisión de
tokenización deduciéndola de la divisa del proyecto, porque el lector que se
estaba usando no traía la columna R. **23 de 127 proyectos salían mal** — hay 22
proyectos de la emisión estadounidense denominados en euros y uno español en
dólares. Emisión, divisa y ubicación son tres cosas distintas.

`maestro.py` lee **por el nombre de la cabecera** y solo cae a la posición si no
la encuentra, precisamente porque el maestro lo editan personas y una columna
insertada en medio desplaza todo lo que viene detrás sin que nada falle.

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

Queda pendiente: el Analizador todavía usa CoinGecko para el precio del RNT
actual, y `pages/02_OTC.py` usa CoinGecko para su tipo de cambio. Hoy difieren
del BCE y del pool en 0,28 % y 0,46 % respectivamente.

### 5. Las reservas se pisan entre sí

Hay un incidente documentado de pérdida de reservas (22/07/2026) por guardar la
copia leída al pintar la página. **Releer la lista fresca justo antes de
escribir**: `_store.read_list(TAB, fresh=True)` y añadir encima.

### 6. Streamlit: cuatro cosas que no son evidentes

- **`st.cache_data` indexa por los argumentos, no por el cuerpo de la función.**
  Si cambias la forma del diccionario que devuelve, la caché vieja sigue
  sirviéndose y revienta con un `KeyError`. Pasa un número de esquema como
  argumento explícito.
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

### 7. Criterios de cálculo que no son obvios

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
- **Pagos de RNT sin identificar**: la wallet `0xed5b6460…567d5e` envía RNT
  semanalmente a inversores (196.514 RNT a una sola wallet entre ago-2025 y
  sep-2026) y `0x51e3d441…bc75e0` otros 32.814. No son contratos y no están en la
  lista de direcciones conocidas, así que el informe fiscal **no los cuenta como
  renta**. Hasta saber qué son, quedan fuera.
- **El SLP repartido en los claims de staking** ya se valora (parte proporcional
  de las reservas del pool), pero conviene contrastarlo con Reental.
- **`pages/01_Simulador.py`** está muerto: 627 líneas inalcanzables tras un
  `st.stop()`. Pendiente de borrar junto con las funciones de `utils.py` que
  quedan huérfanas.
- **`data/pool_rnt/supply.json` no tiene workflow**: se actualiza a mano con
  `scripts/snapshot_pool_rnt.py`.
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
