"""
El maestro de inmuebles, leído una sola vez para toda la herramienta.

POR QUÉ ESTÁ AQUÍ
----------------
La hoja maestra es la fuente de verdad de todo lo que no vive en la cadena:
el nombre del proyecto, dónde está, bajo qué emisión se tokenizó, qué
rentabilidad se estimó para cada estatus y —en los cerrados— cuál acabó
siendo la real. El analizador de wallets la leía para su propio uso y el
constructor de propuestas necesita bastante más, así que la lectura sube a un
módulo común en vez de duplicarse. Es la regla del repositorio.

CÓMO SE LEE
-----------
Cada columna se busca primero por el NOMBRE de su cabecera y solo cae a la
posición si no aparece. El maestro lo mantienen personas: una columna nueva
insertada en medio desplaza todo lo que va detrás, y un informe que se
construye sobre posiciones fijas no falla —devuelve el número de al lado—.
Buscar por nombre hace que eso deje de importar.

Los porcentajes vienen como texto ("28,94%") y las fechas en formatos
variados; ambos se normalizan aquí para que nadie más tenga que hacerlo.
"""
from __future__ import annotations

import io
import os
from datetime import date, datetime

import pandas as pd
import requests

GSHEET_CSV_URL = os.getenv("GSHEET_CSV_URL", "")

# nombre de cabecera → posición de reserva (0-based) por si la cabecera cambia
COLUMNAS = {
    "id":                    ("ID", 0),
    "nombre":                ("Nombre del proyecto", 1),
    "estado":                ("ESTADO", 2),
    "lanzamiento":           ("LANZAMIENTO", 3),
    "fecha_financiacion":    ("Fecha de Financiación", 6),
    "fecha_inicio_renta":    ("Estimación fecha Inicio de Proyecto desde Financiación", 7),
    "fecha_fin_estimada":    ("Estimación fecha fin desde Financiación", 8),
    "tokens_emitidos":       ("Nº de Tokens", 9),
    "precio_emision":        ("Px Emisión  Token", 10),
    "divisa_raw":            ("Divisa", 11),
    "ubicacion":             ("Ubicación", 14),
    "tipologia_explotacion": ("Tipología de explotación", 15),
    "tipologia_dividendo":   ("Tipología de Dividendo", 16),
    "emision":               ("Emisión de Tokenización", 17),
    "address":               ("Token Address", 18),
    "colateralizable":       ("Permite colateralización", 21),
    # Rentabilidades ESTIMADAS por estatus. No todos los proyectos las
    # diferencian: los anteriores al programa de estatus repiten la misma
    # cifra en los tres, y eso es correcto, no un fallo de lectura.
    "est_total_rnt":         ("Estimación Rentab. Total Reentel", 22),
    "est_recurr_rnt":        ("Estimación Rentab. Rendim. Recurr. anualizados Reentel", 23),
    "est_plusvalia_rnt":     ("Estimación Rentab. Plusvalía Reentel", 24),
    "est_anual_rnt":         (None, 25),
    "est_total_rp":          ("Estimación Rentab. Total RP", 26),
    "est_recurr_rp":         ("Estimación Rentab. Rendim. Recurr. anualizados RP", 27),
    "est_plusvalia_rp":      ("Estimación Rentab. Plusvalía RP", 28),
    "est_anual_rp":          (None, 29),
    "est_total_sr":          ("Estimación Rentab. Total SR", 30),
    "est_recurr_sr":         ("Estimación Rentab. Rendim. Recurr. anualizados SR", 31),
    "est_plusvalia_sr":      ("Estimación Rentab. Plusvalía SR", 32),
    "est_anual_sr":          (None, 33),
    "meses_pendientes":      ("Estimación Nº Meses pendientes de renta hasta Estimación fin", 44),
    "meses_en_curso":        ("Real Nº Meses en curso en rentabilidad", 43),
    # Cierre real: solo lo tienen los proyectos CERRADOS y es lo que sostiene
    # el track record.
    "fecha_fin_real":        ("Real fecha de fin", 55),
    "real_total_rnt":        ("Real Rentab. Total Reentel", 59),
    "real_anual_rnt":        ("Real Rentab. Total Anualizada Reentel", 61),
    "real_total_sr":         ("Real Rentab. Total SR", 69),
    "real_anual_sr":         ("Real Rentab. Total Anualizada SR", 71),
    "descripcion":           ("Descripción", 74),
    "link_web":              ("Link a inmueble en la web publica", 75),
    "motivo_cierre":         ("Motivos de cierre Estimado vs Real", 80),
    "dossier":               ("Link a Dossier Comercial", 94),
    "whitepaper":            ("Link a Whitepaper en Español", 95),
}

ABIERTOS = ("EN EXPLOTACIÓN", "EN REFORMA", "EN CONSTRUCCIÓN", "FINANCIÁNDOSE", "PRELANZAMIENTO")

# Las tres categorías, con el sufijo que usan las columnas del maestro.
ESTATUS = (("Reentel", "rnt"), ("ReentelPro", "rp"), ("SuperReentel", "sr"))


# ─── Normalización ───────────────────────────────────────────────────────────

def texto(valor) -> str:
    s = str(valor).strip()
    return "" if s.lower() in ("nan", "none", "-", "no hay") else s


def numero(valor):
    """Un número del maestro, venga como '28,94%', '1.234,56' o ya numérico."""
    s = str(valor).strip().replace("%", "").replace(" ", "")
    if not s or s.lower() in ("nan", "none", "-", "cerrado"):
        return None
    # Miles con coma y decimales con punto (formato anglosajón de la hoja),
    # o al revés. Se decide por cuál aparece el último.
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def porcentaje(valor):
    """Un porcentaje del maestro como fracción: '28,94%' → 0.2894."""
    n = numero(valor)
    return None if n is None else n / 100.0


def fecha(valor):
    """Una fecha del maestro, probando los formatos que aparecen en ella."""
    s = texto(valor)
    if not s:
        return None
    if isinstance(valor, (datetime, date)):
        return valor if isinstance(valor, date) else valor.date()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ─── Lectura ─────────────────────────────────────────────────────────────────

def _valor(row, cabeceras: dict, clave: str):
    nombre, pos = COLUMNAS[clave]
    if nombre and nombre in cabeceras:
        try:
            return row.iloc[cabeceras[nombre]]
        except (IndexError, KeyError):
            pass
    try:
        return row.iloc[pos]
    except (IndexError, KeyError):
        return ""


def descargar(url: str = "") -> pd.DataFrame:
    """El maestro en bruto, sin cabecera: la fila de títulos se localiza sola
    porque arriba del todo hay filas decorativas que cambian de tamaño."""
    url = url or GSHEET_CSV_URL
    if not url:
        return pd.DataFrame()
    r = requests.get(url, timeout=25, allow_redirects=True)
    r.raise_for_status()
    return pd.read_csv(io.BytesIO(r.content), header=None, encoding="utf-8")


def proyectos(url: str = "") -> list:
    """Todos los proyectos del maestro, ya normalizados."""
    crudo = descargar(url)
    if crudo.empty:
        return []
    fila_cab = next((i for i, row in crudo.iterrows()
                     if any("Token Address" in str(v) for v in row.values)), None)
    if fila_cab is None:
        return []
    cabeceras = {}
    for i, v in enumerate(crudo.iloc[fila_cab].tolist()):
        nombre = str(v).strip()
        if nombre and nombre not in cabeceras:     # la primera gana: hay títulos repetidos
            cabeceras[nombre] = i
    filas = crudo.iloc[fila_cab + 1:]

    salida = []
    for _, row in filas.iterrows():
        addr = texto(_valor(row, cabeceras, "address")).lower()
        if not addr.startswith("0x") or len(addr) != 42:
            continue
        divisa_raw = texto(_valor(row, cabeceras, "divisa_raw"))
        p = {
            "address": addr,
            "divisa": "USD" if "$" in divisa_raw else "EUR",
            "precio_emision": numero(_valor(row, cabeceras, "precio_emision")),
            "tokens_emitidos": numero(_valor(row, cabeceras, "tokens_emitidos")),
            "meses_pendientes": numero(_valor(row, cabeceras, "meses_pendientes")),
            "meses_en_curso": numero(_valor(row, cabeceras, "meses_en_curso")),
        }
        for clave in ("id", "nombre", "estado", "ubicacion", "tipologia_explotacion",
                      "tipologia_dividendo", "emision", "colateralizable",
                      "descripcion", "link_web", "motivo_cierre", "dossier", "whitepaper"):
            p[clave] = texto(_valor(row, cabeceras, clave))
        for clave in ("lanzamiento", "fecha_financiacion", "fecha_inicio_renta",
                      "fecha_fin_estimada", "fecha_fin_real"):
            p[clave] = fecha(_valor(row, cabeceras, clave))
        for clave in COLUMNAS:
            if clave.startswith(("est_", "real_")):
                p[clave] = porcentaje(_valor(row, cabeceras, clave))
        p["label"] = p["id"] or p["nombre"] or addr[:10]
        p["abierto"] = p["estado"].upper() in ABIERTOS
        salida.append(p)
    return salida


def por_direccion(lista: list) -> dict:
    return {p["address"]: p for p in lista}


def rentabilidad(proy: dict, estatus_sufijo: str, campo: str = "anual"):
    """Rentabilidad estimada de un proyecto para un estatus.

    `campo` es 'anual' (total anualizada), 'total', 'recurr' o 'plusvalia'.
    Si el proyecto no diferencia por estatus —los anteriores al programa
    repiten la misma cifra en los tres— devuelve esa, que es lo correcto.
    """
    v = proy.get(f"est_{campo}_{estatus_sufijo}")
    return v if v is not None else proy.get(f"est_{campo}_rnt")
