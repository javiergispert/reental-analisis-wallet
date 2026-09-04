"""
Calculadora de status (Reentel / ReentelPro / SuperReentel) empotrada.

La calculadora es un HTML autocontenido que mantiene Jesús González: proyecta
patrimonio a varios años comparando status, y añade un módulo de apalancamiento
con Health Factor. No hace ninguna llamada de red — calcula todo en el navegador
— así que empotrarla no añade latencia ni depende de terceros.

QUÉ APORTA ESTE MÓDULO
----------------------
El HTML original trae los supuestos congelados en el momento en que se escribió.
Aquí se le inyectan los REALES, que la herramienta ya conoce:

    interés del préstamo   12% fijo   →   media histórica medida en el pool
    umbral de liquidación  80% fijo   →   media ponderada del oráculo

Ambos salen de la foto diaria del mercado Aave, que ya está en memoria: no
cuesta ni una llamada extra. Si la foto no está disponible, no se inyecta nada y
la calculadora usa sus valores originales, que eran razonables.

Las rentabilidades por status (12/14/17%) NO se tocan: son un parámetro
comercial del producto, no algo que midamos nosotros, y el usuario ya puede
ajustarlas en la propia interfaz.

RENDIMIENTO
-----------
El HTML se lee de disco una vez por proceso (`@st.cache_data`), y lo que cambia
en cada visita es solo la etiqueta `<script>` de configuración, que ocupa unos
cientos de bytes. `scripts/preparar_simulador.py` ya dejó el fichero en 249 KB
—el original pesaba 2 MB por un logo desproporcionado—, que es lo que viaja al
navegador al abrir la sección.
"""
from __future__ import annotations

import json
import os

import streamlit as st

RUTA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "data", "simulador", "calculadora.html")


@st.cache_data(show_spinner=False)
def _plantilla(ruta: str = RUTA) -> str:
    """El HTML preparado. Se cachea sin TTL: es un fichero versionado en el
    repo, solo cambia con un despliegue, y releerlo en cada interacción serían
    250 KB de disco por rerun."""
    try:
        with open(ruta, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def configuracion(foto_aave: dict | None) -> dict:
    """Los supuestos reales que se le pasan a la calculadora.

    Devuelve solo lo que se ha podido medir: una clave ausente hace que la
    calculadora se quede con su valor por defecto, que es mejor que inventarse
    uno. `foto_aave` es el snapshot de `aave_snapshot.cargar()`.
    """
    cfg: dict = {}
    if not foto_aave:
        return cfg

    salud = foto_aave.get("salud") or {}
    umbral = salud.get("umbral_medio")
    if umbral and 0.3 < umbral < 1.0:
        cfg["liqThreshold"] = round(float(umbral), 4)

    # Media histórica acumulada del tipo de préstamo. Se toma el último punto de
    # la serie, que es justo esa media desde el despliegue del contrato. Se
    # pondera por lo prestado de cada stablecoin: USDT mueve seis veces más que
    # USDC y promediarlas a partes iguales desplazaría el dato.
    tipos = foto_aave.get("tipos") or {}
    stables = foto_aave.get("stables") or {}
    num = den = 0.0
    for sym, serie in tipos.items():
        valores = (serie or {}).get("borrow_apr") or []
        if not valores:
            continue
        peso = float((stables.get(sym) or {}).get("borrow_total") or 0)
        if peso <= 0:
            continue
        num += float(valores[-1]) * peso
        den += peso
    if den > 0:
        cfg["rlApr"] = round(num / den, 4)

    return cfg


# Ajuste de altura del iframe.
#
# `components.html` fija la altura y, si el contenido es más largo, saca una
# barra de desplazamiento DENTRO de la página, que ya tiene la suya. Dos barras
# anidadas son incómodas: se hace scroll y se mueve la que no toca.
#
# Streamlit no reajusta el iframe solo, pero el documento se sirve por `srcdoc`,
# así que comparte origen con la página y desde dentro se puede tocar
# `window.frameElement`. El propio contenido se mide y estira su iframe, de modo
# que la única barra que queda es la de la página.
#
# Se reajusta con `ResizeObserver` porque la altura cambia sola: al desplegar el
# bloque de apalancamiento o al redibujarse los gráficos.
_AUTOALTURA = """
<script>
(function(){
  var marco = window.frameElement;
  if(!marco) return;                      // fuera de un iframe: nada que hacer
  var ultima = 0;
  function ajustar(){
    var alto = Math.max(
      document.documentElement.scrollHeight,
      document.body ? document.body.scrollHeight : 0
    );
    if(!alto || Math.abs(alto - ultima) < 8) return;   // evita bucles por 1-2 px
    ultima = alto;
    marco.style.height = alto + 'px';
    marco.setAttribute('height', alto);
    // Streamlit envuelve el iframe en contenedores con altura fija; si no se
    // sueltan, el iframe crece por dentro y se recorta por fuera.
    for(var n = marco.parentElement, i = 0; n && i < 4; n = n.parentElement, i++){
      if(n.style && n.style.height) n.style.height = 'auto';
    }
  }
  window.addEventListener('load', ajustar);
  document.addEventListener('DOMContentLoaded', ajustar);
  // Se observa el BODY, no el documentElement: la caja de este último la fija
  // el propio iframe, así que no cambia de tamaño cuando el contenido crece
  // —solo crece su scrollHeight— y el observador no llegaba a dispararse.
  if(window.ResizeObserver && document.body){
    new ResizeObserver(ajustar).observe(document.body);
  }
  // Y un repaso periódico como red de seguridad. Lo barato es comparar un
  // número: solo se toca el DOM cuando la altura ha cambiado de verdad. Cubre
  // lo que se pinta tarde (fuentes, SVG) y los despliegues que el observador
  // no vea, como el bloque de apalancamiento.
  setInterval(ajustar, 400);
})();
</script>
"""


def html(foto_aave: dict | None = None) -> str:
    """La calculadora lista para `st.components.v1.html`, o "" si falta."""
    base = _plantilla()
    if not base:
        return ""
    cfg = configuracion(foto_aave)
    if not cfg:
        return base
    # El bloque va ANTES del <script> de la calculadora, que lee la variable en
    # su primera línea. Se inyecta al principio del <body> para no depender de
    # dónde esté el script.
    inyeccion = (
        "<script>window.__RNT_CFG=" + json.dumps(cfg) + ";"
        "document.addEventListener('DOMContentLoaded',function(){"
        "  var c=window.__RNT_CFG||{};"
        "  var m=document.getElementById('rnt_aprMedio');"
        "  if(m&&c.rlApr){m.textContent=(c.rlApr*100).toFixed(2).replace('.',',')+'%';}"
        "});</script>" + _AUTOALTURA
    )
    return base.replace("<body", inyeccion + "<body", 1) if "<body" in base \
        else inyeccion + base


def resumen_config(cfg: dict) -> str:
    """Una línea que declara qué se inyectó, para poder mostrarla en la página.
    Servir supuestos sin decir de dónde salen es lo que hace que nadie confíe
    en el número."""
    if not cfg:
        return ("La calculadora usa sus supuestos por defecto: interés del préstamo 12% "
                "y umbral de liquidación 80%.")
    partes = []
    if "rlApr" in cfg:
        partes.append(f"interés del préstamo **{cfg['rlApr'] * 100:.2f}%** "
                      "(media histórica real del pool, ponderada por volumen)")
    if "liqThreshold" in cfg:
        partes.append(f"umbral de liquidación **{cfg['liqThreshold'] * 100:.1f}%** "
                      "(media ponderada del oráculo)")
    return "Supuestos tomados del mercado RNT Lend real: " + " · ".join(partes) + "."
