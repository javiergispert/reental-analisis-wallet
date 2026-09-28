"""
Capa de persistencia OTC sobre Google Sheets — ÚNICA fuente de verdad.

Las pestañas (Reservas, Ofertas, precios_otc) guardan su estado como UN JSON.
Como una celda de Google Sheets admite máx. 50.000 caracteres y la lista de
reservas ya rozaba ese techo, el blob se reparte en trozos por la columna A
(A1, A2…) y se reensambla al leer. Así el límite de una celda deja de ser un
techo para el número de reservas.

Importado por pages/02_OTC.py y pages/03_Analisis_P2P.py: mantener AQUÍ la única
implementación de lectura/escritura evita que las páginas se desincronicen (fue
lo que provocó que P2P mostrara saldos brutos: leía solo A1 de un dato que ya
ocupaba A1+A2).

API pública:
    read_list(tab, fresh=False) -> list
    read_dict(tab, fresh=False) -> dict
    write(tab, data) -> bool
    clear_cache()
"""
from __future__ import annotations

import json
import time

import gspread
import streamlit as st
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = "13Q0n7egbAIJSU9UvwwDucd3MUQ48Q44eoMwsPT-PmGs"
TAB_RESERVAS   = "Reservas"
TAB_OFERTAS    = "Ofertas"
TAB_PRECIOS    = "precios_otc"

_CHUNK_SIZE = 45000     # margen de seguridad bajo el límite duro de 50.000
_MAX_ROWS   = 60        # filas a leer/limpiar (≈2,7 M caracteres ≈ miles de reservas)


@st.cache_resource(show_spinner=False)
def _get_client():
    """Cliente gspread, construido una vez y reutilizado.

    Antes se rehacía en CADA lectura: credenciales nuevas y un `authorize` que
    pide un token OAuth por red. La página de OTC lee tres pestañas en cada
    recarga, así que eran tres apretones de manos antes de traer un solo dato.

    Compartirlo es correcto: la identidad es la misma para todos —una cuenta de
    servicio, no la del usuario— y el objeto de credenciales renueva su token
    solo. `cache_resource` es el mecanismo que Streamlit prevé para conexiones.
    """
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds  = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=scopes
    )
    return gspread.authorize(creds)


@st.cache_resource(show_spinner=False)
def _libro():
    """El libro abierto, reutilizado: `open_by_key` es otra llamada a la API
    que se repetía en cada lectura."""
    return _get_client().open_by_key(SPREADSHEET_ID)


def _ws(tab: str):
    return _libro().worksheet(tab)


def _reensamblar(filas) -> str | None:
    """Une las celdas de la columna A hasta el primer hueco."""
    partes = []
    for fila in (filas or []):
        val = fila[0] if fila else ""
        if not val:
            break            # primer hueco = fin del blob
        partes.append(val)
    return "".join(partes) if partes else None


def _raw_read(tab: str) -> str | None:
    """Lee el blob completo repartido por la columna A y lo reensambla
    concatenando celdas consecutivas hasta el primer hueco. Con reintentos."""
    for intento in range(4):
        try:
            return _reensamblar(_ws(tab).get(f"A1:A{_MAX_ROWS}"))
        except Exception:
            if intento < 3:
                time.sleep(1.5)
    return None


TABS = (TAB_RESERVAS, TAB_OFERTAS, TAB_PRECIOS)


@st.cache_data(ttl=6, show_spinner=False)
def _leer_todas() -> dict:
    """Las tres pestañas en UNA sola petición, cacheadas juntas 6 segundos.

    Antes cada pestaña era una llamada independiente y la página de OTC hace
    tres en cada recarga. `values_batch_get` las trae de golpe, así que
    cambiar de pestaña en la lista de reservas deja de costar tres viajes a
    Google.

    Si la llamada agrupada falla se devuelve vacío y cada lectura cae a su
    camino individual: es un atajo, no la única vía.
    """
    try:
        res = _libro().values_batch_get([f"'{t}'!A1:A{_MAX_ROWS}" for t in TABS])
        bloques = res.get("valueRanges", [])
        return {t: _reensamblar(b.get("values")) for t, b in zip(TABS, bloques)}
    except Exception:       # noqa: BLE001
        return {}


def _cached_read(tab: str) -> str | None:
    """El contenido de una pestaña, de la lectura agrupada si está disponible.

    Una pestaña que no esté en `TABS` —o una lectura agrupada que falló— se
    pide por su cuenta.
    """
    if tab in TABS:
        todas = _leer_todas()
        if tab in todas:
            return todas[tab]
    return _raw_read(tab)


def _parse_list(val: str | None):
    """Parsea el blob a lista. Devuelve None si el JSON está corrupto (señal
    para que el llamante reintente por otra vía en lugar de perder datos)."""
    if not val:
        return []
    try:
        parsed = json.loads(val)
    except Exception:
        return None
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):      # formato antiguo: un dict = una reserva
        return [parsed]
    return []


def _parse_dict(val: str | None):
    if not val:
        return {}
    try:
        parsed = json.loads(val)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else {}


def read_list(tab: str, fresh: bool = False) -> list:
    """Lee una pestaña como lista. `fresh=True` salta la caché de 6 s (útil
    justo antes de mutar y guardar, para minimizar la ventana de carrera)."""
    val = _raw_read(tab) if fresh else _cached_read(tab)
    parsed = _parse_list(val)
    if parsed is None:
        # El blob reensamblado no parsea (p.ej. un resto antiguo en A2 de un
        # estado previo). Degradar con gracia leyendo solo A1.
        try:
            parsed = _parse_list(_ws(tab).acell("A1").value)
        except Exception:
            parsed = None
    return parsed or []


def read_dict(tab: str, fresh: bool = False) -> dict:
    val = _raw_read(tab) if fresh else _cached_read(tab)
    parsed = _parse_dict(val)
    if parsed is None:
        try:
            parsed = _parse_dict(_ws(tab).acell("A1").value)
        except Exception:
            parsed = None
    return parsed or {}


def write(tab: str, data) -> bool:
    """Guarda `data` como JSON repartido en trozos por la columna A, en UNA
    sola escritura que además vacía filas sobrantes de escrituras anteriores
    (evita dejar restos que corrompan la relectura). `raw=True` guarda los
    trozos tal cual, sin que Sheets interprete uno que empiece por '=', '+' o
    un número como fórmula. Devuelve True si la escritura tuvo éxito."""
    blob   = json.dumps(data, ensure_ascii=False)
    chunks = [blob[i:i + _CHUNK_SIZE] for i in range(0, len(blob), _CHUNK_SIZE)] or [""]
    total  = max(len(chunks), _MAX_ROWS)
    filas  = [[chunks[i]] if i < len(chunks) else [""] for i in range(total)]
    for intento in range(4):
        try:
            _ws(tab).update(filas, f"A1:A{total}", raw=True)
            _leer_todas.clear()    # invalida caché inmediatamente tras escritura
            return True
        except Exception:
            if intento < 3:
                time.sleep(1.5)
    return False


def clear_cache():
    """Invalida la caché de lectura (compartida por todas las páginas del proceso).

    Se limpia la lectura agrupada, que es donde vive ahora el contenido. El
    cliente y el libro NO se tiran: son conexiones, no datos, y rehacerlas
    cuesta dos viajes a Google sin ganar nada.
    """
    _leer_todas.clear()
