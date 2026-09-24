# Herramienta interna de Reental

Aplicación Streamlit de uso interno para el equipo **Reental Wealth**. Reúne en
un sitio lo que antes estaba repartido entre hojas de cálculo y consultas
manuales a la cadena: qué tiene cada inversor, qué se le puede proponer, qué hay
disponible para vender, cómo va el mercado de préstamos y qué debe declarar.

Todo lo que muestra sale de tres sitios: la **cadena** (Polygon), el **maestro de
inmuebles** (una hoja de Google) y el **almacén OTC** (otra hoja de Google). No
hay base de datos.

---

## Puesta en marcha

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env     # y rellenar (ver más abajo)
.venv/bin/streamlit run app.py
```

### Variables de entorno (`.env`)

| Variable | Para qué |
|---|---|
| `ETHERSCAN_API_KEY` | Todas las consultas a la cadena. Etherscan V2, `chainid=137` (Polygon) |
| `GSHEET_CSV_URL` | Maestro de inmuebles publicado como CSV |
| `GSHEET_WALLETS_URL` | Hoja con las wallets conocidas de Reental (opcional) |
| `OTC_WALLET` | Wallet de custodia del stock OTC |
| `OTC_ADMIN_PIN` | PIN del panel de precios mínimos OTC |
| `OFFRAMP_SHEET_URL` | Enlace al Excel de OFF-RAMP que muestra el aviso de protocolo OTC. La herramienta **no lee datos** de esa hoja: solo la enlaza. Sin ella el protocolo aparece igual, sin enlace |

En Streamlit Cloud, además, el almacén OTC necesita la credencial de servicio de
Google en `st.secrets["gcp_service_account"]`.

---

## El menú, página a página

| Página | Qué hace |
|---|---|
| **Analizador de Wallets** | La página grande. Reconstruye la cartera de una o varias wallets desde la cadena: tokens inmobiliarios, dividendos, ecosistema RNT (staking, farming, pool), posición en RNT Lend y coste real de un préstamo abierto. Genera el **informe fiscal** (XLSX de 10 hojas + CSV granular) |
| **OTC interno Reental** | Stock de la wallet de custodia, reservas para comerciales y ofertas de tokens de terceros. **Escribe datos reales**: es la única página que compromete tokens |
| **Análisis Oportunidades P2P** | Cruza lo disponible (stock propio + ofertas de terceros) con el mercado secundario. Exporta PDF |
| **Mercado Aave** | Foto del mercado de préstamos propio (RNT Lend, arquitectura Aave V3 sobre Polygon — **no** es el pool público de Aave). Exporta PDF |
| **Simulador rentabilidad** | Calculadora de estrategias empotrada (HTML de terceros) más el análisis del umbral de rentabilidad que hace falta para que un préstamo salga a cuenta |
| **Constructor de propuestas** | Dossier comercial en PDF para un inversor, de alta o de ampliación sobre su cartera real. Puede dejar reservados en OTC los tokens que propone |

---

## Los módulos

La regla del repositorio: **lo que usan dos páginas vive en un módulo común,
nunca duplicado**. Cada módulo lleva en su docstring el porqué de existir.

### Datos de origen

- **`maestro.py`** — el maestro de inmuebles. Lee cada columna **por el nombre de
  su cabecera** y solo cae a la posición si no la encuentra. Normaliza
  porcentajes y fechas. Es la lectura buena; ver *Trampas*.
- **`divisas.py`** — tipos de cambio del Banco Central Europeo (vía Frankfurter),
  30 divisas con histórico completo.
- **`pool_rnt.py`** — el pool SushiSwap RNT/USDT: precio del RNT (actual e
  histórico) y valor de una participación (SLP). Es la fuente buena del precio
  del RNT, no CoinGecko.
- **`utils.py`** — paginación de Etherscan y un lector antiguo del maestro
  (`load_master_projects`) que todavía usan cuatro páginas.
- **`reental_tokens.py`** — cómo se llaman los tokens de Reental en la cadena. La
  nomenclatura no es consistente entre proyectos y esto lo centraliza.

### Dominio

- **`aave_lend.py`** — primitivas on-chain y matemática de riesgo de RNT Lend.
- **`aave_snapshot.py`** — la foto diaria del mercado, leída de disco.
- **`coste_prestamo.py`** — matemática del coste de un préstamo (APR→APY,
  frecuencia de pago, umbral de rentabilidad). Sin Streamlit, verificable desde un script.
- **`otc_storage.py`** — persistencia OTC sobre Google Sheets. **Única fuente de
  verdad** de reservas, ofertas y precios mínimos.
- **`otc_saldos.py`** — saldo real de un inversor y disponibilidad de una oferta.
- **`otc_inventario.py`** — qué hay comprometible hoy (stock propio + terceros).
- **`otc_historico.py`**, **`p2p_mercado.py`**, **`mercado_secundario.py`** —
  histórico de operaciones anterior al sistema actual y mercado secundario.
- **`propuesta.py`** — cálculo de una propuesta: agregados, repartos, escenarios
  de reinversión y track record. Sin Streamlit, probado contra propuestas reales.
- **`propuesta_pdf.py`** — el dossier comercial en PDF.
- **`simulador_status.py`** — empotra la calculadora HTML.

### Presentación e infraestructura

- **`ui_kpi.py`** — las tarjetas KPI, compartidas por todas las páginas.
- **`otc_protocolos.py`** — los avisos de protocolo operativo del OTC.
- **`recarga.py`** — fuerza la recarga de módulos propios tras un despliegue.

---

## Los datos en disco (`data/`)

Cada carpeta tiene su propio `README.md`. En resumen:

| Fichero | Lo produce | Cada cuánto |
|---|---|---|
| `data/aave/snapshot.json` | `scripts/snapshot_aave.py` | **GitHub Action diaria** (04:30 UTC), commitea si cambia |
| `data/pool_rnt/supply.json` | `scripts/snapshot_pool_rnt.py` | **A mano** — no tiene workflow todavía |
| `data/simulador/calculadora.html` | `scripts/preparar_simulador.py` | Solo si Jesús González entrega una versión nueva |
| `data/rnt_p2p/`, `data/otc_historico/` | Exports normalizados | Puntual |

El snapshot de Aave existe porque reconstruirlo en cada visita son 4-5 minutos de
llamadas a Etherscan. Con el fichero, la página carga en segundos.

---

## Despliegue

Streamlit Cloud, desde la rama `main`. Cada push despliega.

La foto diaria del mercado la genera un GitHub Action que **commitea el fichero**
a `main`, así que el repositorio tiene commits automáticos del bot: al hacer
`git push` conviene `git pull --rebase` primero.

---

## Trampas conocidas

Cosas que ya han causado un error real. Están explicadas con más detalle en
`CLAUDE.md`, pero estas son las que más duelen:

1. **Los JSON de la raíz no son los datos reales.** `otc_ofertas.json` y
   `otc_reservas.json` son copias locales de junio de 2026, están en
   `.gitignore` y **no** reflejan el estado. Lo real vive en Google Sheets, vía
   `otc_storage`.
2. **Emisión ≠ divisa ≠ ubicación.** En el maestro, 23 de 127 proyectos tienen
   una emisión de tokenización que no se corresponde con su moneda. Deducir una
   de otra da cifras falsas.
3. **`eth_call` a un bloque antiguo miente.** El nodo público de Etherscan ignora
   la etiqueta de bloque y devuelve el estado de hoy **sin dar error**. Lo
   histórico se lee de eventos (`getLogs`), nunca de `eth_call`.
4. **CoinGecko gratuito solo cubre 365 días** y la tentación es caer al precio de
   hoy. Para un informe fiscal eso es inaceptable: usar el pool o el BCE.
5. **Las reservas se pisan.** Releer la lista fresca justo antes de escribir; hay
   un incidente documentado de pérdida de reservas por no hacerlo.

---

*Documento mantenido junto al código. Si cambias cómo se obtiene un dato o
añades una página, actualízalo aquí antes de que se te olvide por qué.*
