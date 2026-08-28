"""
Avisos de protocolo operativo del OTC interno.

La herramienta registra la oferta y la reserva, pero la operación solo se cierra
bien si FUERA de ella se dan unos pasos y en un orden concreto: quién envía los
tokens, a qué wallet, cuándo se anota en el OFF-RAMP y en qué momento se sube el
justificante al ADMIN. Ese orden se olvida, y equivocarlo cuesta caro — subir el
justificante antes de que los tokens estén en la wallet OTC dispara el envío de
contratos de una operación que todavía puede caerse.

Por eso los dos puntos críticos se cierran con un modal que hay que leer y
confirmar antes de guardar nada:

  * `OFERTA_TERCERO` — al publicar tokens que están en manos de un inversor.
  * `RESERVA_TERCERO` — al reservar tokens cuyo propietario es un tercero.

El texto vive aquí y no incrustado en la página para poder actualizarlo sin
tocar la lógica de guardado, que es la parte delicada.

Uso en una página (el patrón es el mismo para los dos avisos)::

    if st.button("Guardar"):
        errores = validar()
        if not errores:
            otc_protocolos.solicitar(otc_protocolos.RESERVA_TERCERO)
            st.rerun()

    otc_protocolos.gestionar(otc_protocolos.RESERVA_TERCERO)

    if otc_protocolos.consumir(otc_protocolos.RESERVA_TERCERO):
        persistir()

Se valida ANTES de abrir el modal: hacer leer todo el protocolo para responder
después «falta el campo Comercial» es la forma más rápida de que se lea en
diagonal.
"""
from __future__ import annotations

import os

import streamlit as st

OFERTA_TERCERO  = "oferta_tercero"
RESERVA_TERCERO = "reserva_tercero"

SAFE_OTC = os.getenv("OTC_WALLET", "0xCE0719ec1bDA336Ba069C6961aD167767829301A")

URL_OFFRAMP = ("https://docs.google.com/spreadsheets/u/1/d/"
               "1yNiqL2dWPCt6OW8D48Z3sWEIG6fQ19mOWzS_W1IPSa0/edit?gid=0#gid=0")

# Cada paso es un texto y, opcionalmente, un detalle en viñetas. `html=True`
# permite meter el enlace al OFF-RAMP y la dirección del SAFE sin escaparlos.
PROTOCOLOS = {
    OFERTA_TERCERO: {
        "titulo": "📢 Antes de publicar: oferta de tokens de un tercero",
        "intro": (
            "Estás realizando una publicación de oferta de Tokens que se encuentran en "
            "posesión de un tercero, por lo que tendrás que tener en cuenta los "
            "siguientes pasos para que la operación se realice correctamente."
        ),
        "pasos": [
            {
                "texto": (
                    "Una vez publicada la oferta con los datos solicitados has de esperar a ir "
                    "recibiendo solicitudes de reservas, las cuales podrás ir siguiendo más abajo "
                    "en <b>«Reservas activas»</b> para conocer quién hay interesado y hablar con "
                    "el agente que la representa, y así coordinar los próximos pasos."
                ),
            },
            {
                "texto": (
                    "Cuando tengas confirmado que un nuevo inversor se quedará los tokens "
                    "reservados, entonces ya podría Reental proceder a comprarle primero los "
                    "tokens a tu inversor para luego vendérselos al nuevo. Los pasos por tu lado "
                    "serían:"
                ),
                "detalle": [
                    ("El inversor propietario del token lo tendrá que enviar al "
                     f"<b>SAFE OTC de Reental</b>:<br><code>{SAFE_OTC}</code>"),
                    ("El responsable de ese inversor indicará la operación en el excel de "
                     f"<b>OFF-RAMP</b> (<a href=\"{URL_OFFRAMP}\" target=\"_blank\">abrir</a>) "
                     "para que Reental procese la compra y así pagarle según se indique. "
                     "Será <b>imprescindible</b> tener el hash del envío de los tokens."),
                ],
            },
            {
                "texto": (
                    "Avisa al comercial comprador de que los tokens ya han sido enviados a la "
                    "wallet OTC, para que él pueda continuar con su proceso."
                ),
            },
        ],
        "confirmar": "📢 Lo he leído — publicar oferta",
    },

    RESERVA_TERCERO: {
        "titulo": "🤝 Antes de reservar: tokens en posesión de un tercero",
        "intro": (
            "Estás realizando una reserva sobre un token en posesión de un Tercero, por lo "
            "que presta atención a los pasos a seguir <b>antes de realizar el pago</b> por "
            "los mismos."
        ),
        "pasos": [
            {"texto": ("Ponte en contacto con el comercial / agente que representa al "
                       "propietario de estos tokens. Lo puedes encontrar en el listado "
                       "<b>«Tokens de terceros publicados para venta»</b>, en la columna "
                       "<b>«Comercial»</b>.")},
            {"texto": "Confírmale el interés en dichos tokens, así como el precio."},
            {"texto": ("Confirmado el compromiso, el comercial que representa al vendedor "
                       "avisará a su cliente para que envíe los tokens a la wallet.")},
            {"texto": ("Coordina con el comprador el envío del capital y recibe el justificante "
                       "de la transferencia, que por el momento guardarás "
                       "<b>SIN subirlo al ADMIN</b>.")},
            {"texto": ("Una vez los tokens se hayan recibido en la wallet OTC, entonces ya "
                       "puedes cargar el justificante en el ADMIN para que desde administración "
                       "le envíen los contratos al inversor y, una vez firmado, le envíen los "
                       "tokens.")},
        ],
        "confirmar": "💾 Lo he leído — guardar reserva",
    },
}

# El estado del aviso vive en `session_state` y no en una variable local porque
# el modal sobrevive a varios reruns: abrirlo, marcar la casilla y confirmar son
# tres pasadas distintas del script.
_PENDIENTE  = "pendiente"
_CONFIRMADO = "confirmado"

_ESTADO = "_otc_protocolo_{}"
_LEIDO  = "_otc_protocolo_leido_{}"


def solicitar(clave: str) -> None:
    """Marca que hay que enseñar el aviso antes de continuar.

    Cierra cualquier otro aviso: Streamlit solo admite un modal abierto, y dos
    pendientes a la vez harían fallar la página entera.
    """
    for otra in PROTOCOLOS:
        if otra != clave:
            limpiar(otra)
    st.session_state[_ESTADO.format(clave)] = _PENDIENTE
    st.session_state.pop(_LEIDO.format(clave), None)


def pendiente(clave: str) -> bool:
    """Si el aviso está abierto. La página lo usa para no cerrar el
    desplegable del formulario por debajo del modal."""
    return st.session_state.get(_ESTADO.format(clave)) == _PENDIENTE


def limpiar(clave: str) -> None:
    st.session_state.pop(_ESTADO.format(clave), None)
    st.session_state.pop(_LEIDO.format(clave), None)


def consumir(clave: str) -> bool:
    """True una sola vez, en la pasada siguiente a que el usuario confirme.

    Se limpia al leerlo para que un rerun posterior no vuelva a disparar el
    guardado: sin esto, una reserva podría anotarse dos veces.
    """
    if st.session_state.get(_ESTADO.format(clave)) != _CONFIRMADO:
        return False
    limpiar(clave)
    return True


def gestionar(clave: str) -> None:
    """Pinta el modal mientras el aviso esté pendiente de confirmar."""
    if st.session_state.get(_ESTADO.format(clave)) == _PENDIENTE:
        _modal(clave)


def _html(clave: str) -> str:
    p = PROTOCOLOS[clave]
    filas = []
    for i, paso in enumerate(p["pasos"], 1):
        detalle = ""
        if paso.get("detalle"):
            puntos = "".join(
                f'<li style="margin:6px 0;">{d}</li>' for d in paso["detalle"]
            )
            detalle = (
                '<ul style="margin:8px 0 0 0;padding-left:18px;color:#334155;'
                'font-size:0.86rem;list-style:disc;">' + puntos + "</ul>"
            )
        filas.append(
            '<div style="display:flex;gap:12px;margin:14px 0;">'
            '<div style="flex:0 0 26px;height:26px;border-radius:50%;background:#0ea5e9;'
            'color:#fff;font-weight:700;font-size:0.82rem;display:flex;align-items:center;'
            f'justify-content:center;">{i}</div>'
            f'<div style="flex:1;font-size:0.9rem;color:#1e293b;line-height:1.5;">'
            f'{paso["texto"]}{detalle}</div></div>'
        )
    return (
        '<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:10px;'
        'padding:14px 18px;margin-bottom:6px;font-size:0.92rem;color:#78350f;">'
        f'⚠️ {p["intro"]}</div>' + "".join(filas)
    )


@st.dialog("Protocolo operativo", width="large")
def _modal(clave: str) -> None:
    p = PROTOCOLOS[clave]
    st.markdown(f"#### {p['titulo']}")
    st.markdown(_html(clave), unsafe_allow_html=True)
    st.markdown("---")

    leido = st.checkbox("He leído y entiendo los pasos a seguir",
                        key=_LEIDO.format(clave))
    c1, c2 = st.columns([3, 1])
    if c1.button(p["confirmar"], type="primary", use_container_width=True,
                 disabled=not leido, key=f"_otc_ok_{clave}"):
        st.session_state[_ESTADO.format(clave)] = _CONFIRMADO
        st.rerun()
    if c2.button("Cancelar", use_container_width=True, key=f"_otc_no_{clave}"):
        limpiar(clave)
        st.rerun()
