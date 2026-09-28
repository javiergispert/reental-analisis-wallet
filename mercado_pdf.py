"""
Informe técnico de profundidad del mercado secundario.

QUÉ RESPONDE
------------
La pregunta que se hace un inversor antes de entrar: «si mañana quiero vender,
¿hay a quién?». No se responde con un precio medio, así que el documento va a
las métricas que sí lo dicen — cuánto se cruza, entre cuántos, a qué rango de
precios, con qué regularidad y en cuántos proyectos distintos.

Cubre los dos canales por los que se puede salir: el OTC que intermedia Reental
y RNTP2P, donde los inversores negocian entre ellos. Y no disimula que se
conocen de forma distinta: en OTC hay embudo completo y en P2P solo lo
ejecutado, porque las órdenes abiertas se firman fuera de la cadena.

Sale con marca de borrador mientras Legal y Compliance no lo revisen: lleva
cifras de mercado y va a inversores.
"""
from __future__ import annotations

import io
from datetime import date

import plotly.graph_objects as go
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

import mercado_secundario as _mkt
import pdf_estilos as _e


def _png(fig, ancho=1100, alto=420) -> bytes:
    fig.update_layout(template="plotly_white", margin=dict(l=55, r=25, t=40, b=45),
                      font=dict(family="Helvetica", size=14, color=_e.NAVY),
                      paper_bgcolor="white", plot_bgcolor="white")
    return fig.to_image(format="png", width=ancho, height=alto, scale=2)


def _grafico_evolucion(serie) -> bytes:
    """Volumen mensual por canal, con la línea de operaciones encima."""
    fig = go.Figure()
    for canal, color in ((_mkt.CANAL_P2P, _e.AZUL), (_mkt.CANAL_OTC, _e.DORADO)):
        s = serie[serie["canal"] == canal]
        if s.empty:
            continue
        fig.add_bar(name=canal, x=s["mes"], y=s["volumen"], marker_color=color)
    ops = serie.groupby("mes")["ops"].sum().reset_index()
    fig.add_scatter(name="Operaciones", x=ops["mes"], y=ops["ops"], yaxis="y2",
                    mode="lines+markers", line=dict(color="#0F9960", width=2))
    fig.update_layout(
        barmode="stack", yaxis_title="Volumen (USD)",
        yaxis2=dict(title="Operaciones", overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h", y=1.14, x=0))
    return _png(fig)


def _grafico_dispersion(disp: dict) -> bytes:
    """Dónde se cruza el precio: caja de percentiles."""
    fig = go.Figure(go.Box(
        q1=[disp["p25"]], median=[disp["mediana"]], q3=[disp["p75"]],
        lowerfence=[disp["p25"]], upperfence=[disp["p75"]],
        x=["Precio por token"], marker_color=_e.DORADO, boxpoints=False,
        showlegend=False))
    fig.update_layout(yaxis_title="USD por token")
    return _png(fig, 520, 400)


def construir(datos: dict) -> bytes:
    """El informe completo.

    `datos` trae: operaciones ya filtradas, los KPI del conjunto y de cada
    canal, el resumen por proyecto, el periodo, el proyecto seleccionado si lo
    hay y si va marcado como borrador.
    """
    E = _e.estilos()
    A = _e.ANCHO_UTIL
    buf = io.BytesIO()
    hoy = datos.get("fecha") or date.today()
    ambito = datos.get("proyecto") or "Todos los proyectos"
    doc = SimpleDocTemplate(buf, pagesize=_e.PAGINA,
                            leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                            topMargin=1.3 * cm, bottomMargin=1.6 * cm,
                            title=f"Profundidad del mercado secundario — {ambito}",
                            author="Reental Wealth")

    ops = datos["operaciones"]
    k, kp2p, kotc = datos["kpis"], datos["kpis_p2p"], datos["kpis_otc"]
    disp = datos.get("dispersion") or {}
    conc = datos.get("concentracion") or {}
    por_token = datos.get("por_token") or {}
    nombres = datos.get("nombres") or {}
    rotacion = datos.get("rotacion") or {}
    story = []

    # ── Portada ──────────────────────────────────────────────────────────────
    portada = Table(
        [[Paragraph("PROFUNDIDAD DEL<br/>MERCADO SECUNDARIO", E["h1"])],
         [Spacer(1, 0.4 * cm)],
         [Paragraph(f"Ámbito: <b>{ambito}</b>", E["h1sub"])],
         [Paragraph(f"Período analizado: <b>{datos['desde']:%d/%m/%Y}</b> a "
                    f"<b>{datos['hasta']:%d/%m/%Y}</b>", E["h1sub"])],
         [Spacer(1, 1.0 * cm)],
         [Paragraph(f"Reental Wealth · {hoy:%d/%m/%Y}", E["h1sub"])]],
        colWidths=[A])
    portada.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), _e.PDF_NAVY),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 28),
                                 ("RIGHTPADDING", (0, 0), (-1, -1), 28),
                                 ("TOPPADDING", (0, 0), (0, 0), 44),
                                 ("BOTTOMPADDING", (0, -1), (-1, -1), 44)]))
    story += [portada, Spacer(1, 0.5 * cm),
              Paragraph(
                  "Un inversor que entra en un activo tokenizado quiere saber si podrá salir. "
                  "Este informe mide esa capacidad con datos de operaciones <b>cerradas</b> en "
                  "los dos canales disponibles, no con estimaciones ni con ofertas publicadas.",
                  E["txt"]),
              PageBreak()]

    # ── 1. Resumen ───────────────────────────────────────────────────────────
    story += [_e.banda("1 · VOLUMEN Y ACTIVIDAD"), Spacer(1, 0.4 * cm),
              _e.kpis([(_e.num(k["ops"]), "operaciones cerradas"),
                       (_e.usd(k["volumen"], 0), "volumen negociado"),
                       (_e.num(k["tokens"], 1), "tokens que cambiaron de manos"),
                       (_e.usd(k["ticket_medio"], 0), "ticket medio por operación")], E),
              Spacer(1, 0.5 * cm)]

    cuota = lambda v: (v / k["volumen"] * 100) if k["volumen"] else None
    story += [_e.tabla(
        ["Canal", "Operaciones", "Volumen (USD)", "Cuota del volumen",
         "Ticket medio", "Precio medio por token"],
        [["RNTP2P — entre inversores", _e.num(kp2p["ops"]), _e.usd(kp2p["volumen"], 0),
          _e.pct_directo(cuota(kp2p["volumen"])), _e.usd(kp2p["ticket_medio"], 0),
          _e.usd(kp2p["precio_medio"])],
         ["OTC — intermediado por Reental", _e.num(kotc["ops"]), _e.usd(kotc["volumen"], 0),
          _e.pct_directo(cuota(kotc["volumen"])), _e.usd(kotc["ticket_medio"], 0),
          _e.usd(kotc["precio_medio"])],
         ["<b>Conjunto</b>", f"<b>{_e.num(k['ops'])}</b>", f"<b>{_e.usd(k['volumen'], 0)}</b>",
          "<b>100,0 %</b>", f"<b>{_e.usd(k['ticket_medio'], 0)}</b>",
          f"<b>{_e.usd(k['precio_medio'])}</b>"]],
        [A * 0.26, A * 0.13, A * 0.17, A * 0.15, A * 0.14, A * 0.15], E),
        Spacer(1, 0.35 * cm),
        Paragraph(
            "El precio medio se pondera por <b>importe</b>, no por operación: una venta de 100 "
            "tokens y otra de 0,3 no pueden pesar igual en el precio del mercado."
            + (f" Frente al precio de emisión, el secundario cotiza con una prima de "
               f"<b>{_e.pct_directo(k['prima_pct'])}</b>." if k.get("prima_pct") is not None else ""),
            E["nota"]),
        PageBreak()]

    # ── 2. Amplitud y ritmo ──────────────────────────────────────────────────
    repes = k["vendedores"] + k["compradores"] - k["usuarios"]
    story += [_e.banda("2 · AMPLITUD Y REGULARIDAD"), Spacer(1, 0.4 * cm),
              _e.kpis([(_e.num(k["usuarios"]), "inversores distintos han operado"),
                       (_e.num(k["vendedores"]), "han vendido"),
                       (_e.num(k["compradores"]), "han comprado"),
                       (_e.num(repes), "han hecho las dos cosas",
                        colors.HexColor(_e.VERDE_OSC))], E),
              Spacer(1, 0.4 * cm),
              _e.kpis([(_e.num(k["mediana_mes"], 1), "operaciones en el mes típico (mediana)"),
                       (_e.num(k["mediana_mes_3"], 1), "mediana de los últimos 3 meses",
                        colors.HexColor(_e.VERDE_OSC) if (k["mediana_mes_3"] or 0) >= (k["mediana_mes"] or 0)
                        else colors.HexColor(_e.ROJO)),
                       (_e.num(k["meses"]), "meses de serie histórica")], E),
              Spacer(1, 0.4 * cm),
              Paragraph(
                  "Se usa la <b>mediana</b> y no la media porque un mes excepcional —el "
                  "lanzamiento de un proyecto que mueve cien operaciones— desplaza la media y "
                  "deja de describir el mes normal. Los meses sin ninguna operación cuentan como "
                  "cero: un mes sin mercado es información sobre la profundidad, no ausencia de "
                  "dato. <b>Inversores distintos</b> no es la suma de vendedores y compradores: "
                  "quien vendió un mes y compró otro es una sola persona.", E["nota"]),
              PageBreak()]

    # ── 3. Precio ────────────────────────────────────────────────────────────
    if disp:
        story += [_e.banda("3 · A QUÉ PRECIO SE CRUZA"), Spacer(1, 0.4 * cm)]
        fila = [_e.kpis([(_e.usd(disp["p25"]), "percentil 25"),
                         (_e.usd(disp["mediana"]), "mediana"),
                         (_e.usd(disp["p75"]), "percentil 75"),
                         (_e.pct_directo(disp["dispersion_pct"]),
                          "rango intercuartílico sobre la mediana")], E, A * 0.58)]
        try:
            img = Image(io.BytesIO(_grafico_dispersion(disp)),
                        width=A * 0.36, height=A * 0.36 * 400 / 520)
            bloque = Table([[fila[0], img]], colWidths=[A * 0.60, A * 0.40])
            bloque.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
            story.append(bloque)
        except Exception:       # noqa: BLE001 — sin gráfico, las cifras ya están
            story.append(fila[0])
        story += [Spacer(1, 0.4 * cm),
                  Paragraph(
                      f"La mitad central de las {_e.num(disp['n'])} operaciones con precio "
                      f"conocido se cruzó entre {_e.usd(disp['p25'])} y {_e.usd(disp['p75'])} por "
                      f"token. Un rango estrecho indica que quien vende encuentra contrapartida "
                      f"cerca del precio de referencia y no tiene que malvender.", E["txt"]),
                  Spacer(1, 0.2 * cm),
                  Paragraph(
                      f"Extremos observados: {_e.usd(disp['min'])} y {_e.usd(disp['max'])}. "
                      f"Incluyen {_e.num(disp.get('marginales'))} operaciones de cuantía "
                      "marginal —fracciones de token o importe cero— que no se excluyen del "
                      "cálculo pero tampoco representan un precio de mercado. Por eso la lectura "
                      "se apoya en los percentiles y no en el mínimo y el máximo.", E["nota"]),
                  PageBreak()]

    # ── 4. Evolución ─────────────────────────────────────────────────────────
    serie = _mkt.serie_mensual(ops)
    if len(serie) > 1:
        story += [_e.banda("4 · EVOLUCIÓN MENSUAL"), Spacer(1, 0.35 * cm)]
        try:
            story.append(Image(io.BytesIO(_grafico_evolucion(serie)),
                               width=A * 0.92, height=A * 0.92 * 420 / 1100))
        except Exception:       # noqa: BLE001
            pass
        story += [Spacer(1, 0.3 * cm),
                  Paragraph(
                      "Barras apiladas: volumen de cada canal. Línea: número de operaciones, en "
                      "el eje de la derecha. Las dos cosas juntas distinguen un mes de mucho "
                      "dinero en pocas operaciones de otro con actividad repartida.", E["nota"]),
                  PageBreak()]

    # ── 5. Concentración ─────────────────────────────────────────────────────
    if conc:
        story += [_e.banda("5 · REPARTO ENTRE PROYECTOS"), Spacer(1, 0.4 * cm),
                  _e.kpis([(_e.num(conc["proyectos"]), "proyectos con operaciones"),
                           (_e.num(conc["equivalentes"], 1),
                            "proyectos equivalentes de tamaño idéntico"),
                           (_e.pct_directo(conc["top1_pct"]), "del volumen, el mayor proyecto"),
                           (_e.pct_directo(conc["top5_pct"]), "del volumen, los cinco mayores")], E),
                  Spacer(1, 0.4 * cm),
                  Paragraph(
                      "La concentración se mide con el índice de Herfindahl sobre el volumen por "
                      "proyecto, el mismo que emplean los reguladores de competencia. Su inverso "
                      "—los <b>proyectos equivalentes</b>— se lee mejor: dice entre cuántos "
                      "proyectos de tamaño idéntico estaría repartido el mercado si tuviera esta "
                      "misma concentración. Cuanto más cerca del número real de proyectos, más "
                      "repartida está la actividad y menos depende la liquidez de unos pocos "
                      "activos.", E["nota"]),
                  PageBreak()]

    # ── 6. Por proyecto ──────────────────────────────────────────────────────
    if por_token:
        story += [_e.banda("6 · LIQUIDEZ POR PROYECTO"), Spacer(1, 0.35 * cm)]
        filas = []
        orden = sorted(por_token.items(), key=lambda kv: -kv[1]["volumen"])
        for addr, d in orden[:22]:
            rot = rotacion.get(addr)
            filas.append([
                nombres.get(addr, addr[:10] + "…"),
                _e.num(d["ops"]), _e.num(d["tokens"], 1), _e.usd(d["volumen"], 0),
                _e.usd(d["precio_medio"]),
                _e.num(d["vendedores"]), _e.num(d["compradores"]),
                _e.pct_directo(rot) if rot is not None else "—",
                _e.num(d.get("dias_sin_operar")),
            ])
        story.append(_e.tabla(
            ["Proyecto", "Ops.", "Tokens", "Volumen (USD)", "Precio medio",
             "Vendedores", "Compradores", "Rotación", "Días sin operar"],
            filas,
            [A * c for c in (0.22, 0.06, 0.10, 0.14, 0.11, 0.09, 0.10, 0.09, 0.09)], E))
        story += [Spacer(1, 0.3 * cm),
                  Paragraph(
                      f"Los {len(filas)} proyectos de mayor volumen, de "
                      f"{_e.num(len(por_token))} con actividad. <b>Rotación</b> es la proporción "
                      "de los tokens emitidos que ha cambiado de manos: es lo que de verdad "
                      "importa a quien quiere salir, porque negociar 300 tokens dice poco si el "
                      "proyecto emitió 300 y mucho si emitió veinte mil. <b>Días sin operar</b> "
                      "avisa de lo que el recuento esconde: un proyecto con cuarenta operaciones "
                      "hace ocho meses y ninguna desde entonces no es líquido hoy.", E["nota"]),
                  PageBreak()]

    # ── 7. Metodología ───────────────────────────────────────────────────────
    cobertura = float(ops["detalle_ok"].mean() * 100) if len(ops) else 0.0
    story += [_e.banda("7 · CÓMO SE HA MEDIDO"), Spacer(1, 0.4 * cm),
              Paragraph(
                  "<b>Origen de los datos.</b> Operaciones cerradas en los dos canales por los "
                  "que un inversor puede vender sus tokens: el <b>OTC</b> que intermedia Reental, "
                  "registrado en sus propios libros, y <b>RNTP2P</b>, donde los inversores "
                  "negocian entre ellos, cuyas operaciones se exportan de la plataforma y se "
                  "completan contra la cadena de bloques. No se incluyen ofertas publicadas ni "
                  "intenciones: solo lo ejecutado.", E["txt"]),
              Spacer(1, 0.25 * cm),
              Paragraph(
                  "<b>Los dos canales no se conocen igual.</b> En OTC existe el embudo completo "
                  "—lo ofertado, lo reservado y lo cerrado—. En RNTP2P solo se conoce lo "
                  "ejecutado, porque las órdenes abiertas son compromisos firmados fuera de la "
                  "cadena que no dejan rastro. Comparar volúmenes cerrados es legítimo; deducir "
                  "de ahí qué canal tiene más oferta viva, no.", E["txt"]),
              Spacer(1, 0.25 * cm),
              Paragraph(
                  f"<b>Cobertura del detalle.</b> El {cobertura:,.1f} % de las operaciones del "
                  "período conserva la cantidad de tokens. La plataforma purga ese detalle pasadas "
                  "unas semanas y se reconstruye desde la cadena; las que no se han podido "
                  "recuperar cuentan para el volumen pero no para el precio medio ni para el "
                  "desglose por proyecto. Se dice en vez de repartirlas a ojo."
                  .replace(".", ",", 0), E["txt"]),
              Spacer(1, 0.25 * cm),
              Paragraph(
                  "<b>Divisa.</b> Todos los importes en dólares. Las operaciones en euros se "
                  "convierten con la referencia del Banco Central Europeo de la fecha de cada "
                  "operación, no con un tipo medio del período.", E["txt"]),
              Spacer(1, 0.4 * cm),
              Paragraph(
                  "Este documento describe el comportamiento pasado del mercado secundario y "
                  "<b>no constituye asesoramiento financiero ni una garantía de liquidez futura</b>. "
                  "Que un activo se haya negociado con regularidad no asegura que exista "
                  "contrapartida en un momento concreto. La inversión inmobiliaria tokenizada "
                  "conlleva riesgo de pérdida del capital.", E["txt"])]

    doc.build(story,
              onFirstPage=_e.pie(hoy, datos.get("borrador", True),
                                 f"Reental Wealth · Profundidad del mercado secundario · "
                                 f"{hoy:%d/%m/%Y} · No constituye asesoramiento"),
              onLaterPages=_e.pie(hoy, datos.get("borrador", True),
                                  f"Reental Wealth · Profundidad del mercado secundario · "
                                  f"{hoy:%d/%m/%Y} · No constituye asesoramiento"))
    return buf.getvalue()
