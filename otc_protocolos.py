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

# La consulta libre es otro modal distinto: no bloquea nada, se abre cuando el
# usuario quiere releer los pasos. Streamlit solo admite uno abierto a la vez,
# así que abrir cualquiera cierra los demás.
_CONSULTA = "_otc_protocolo_consulta"


def solicitar(clave: str) -> None:
    """Marca que hay que enseñar el aviso antes de continuar.

    Cierra cualquier otro aviso: Streamlit solo admite un modal abierto, y dos
    pendientes a la vez harían fallar la página entera.
    """
    for otra in PROTOCOLOS:
        if otra != clave:
            limpiar(otra)
    st.session_state.pop(_CONSULTA, None)
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


def _html(clave: str, con_intro: bool = True) -> str:
    """Los pasos en HTML. `con_intro` añade la banda ámbar de advertencia.

    En la consulta libre se omite: está redactada para el momento de la acción
    («estás realizando una publicación…») y a quien solo viene a releer los pasos
    le suena a que ha empezado algo que no ha empezado.
    """
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
    intro = (
        '<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:10px;'
        'padding:14px 18px;margin-bottom:6px;font-size:0.92rem;color:#78350f;">'
        f'⚠️ {p["intro"]}</div>'
    ) if con_intro else ""
    return intro + "".join(filas)


# ── Consulta libre del protocolo ─────────────────────────────────────────────
# Los modales de arriba se enseñan UNA vez, justo antes de guardar. Pero la
# operación se alarga días: cuando llega la respuesta del otro comercial, quien
# leyó los pasos ya no se acuerda de en qué punto estaba ni de qué le toca. Este
# botón deja releerlos en cualquier momento, separados por rol para que cada uno
# encuentre su secuencia sin leer la del otro.
#
# Reutiliza los mismos textos de PROTOCOLOS: si el proceso cambia, se edita una
# vez y cambian a la vez el aviso obligatorio y la consulta.

ROLES = [
    (OFERTA_TERCERO,  "🏷️ Represento al VENDEDOR",
     "Publicaste la oferta de un inversor que quiere vender sus tokens."),
    (RESERVA_TERCERO, "🤝 Represento al COMPRADOR",
     "Vas a reservar, o ya reservaste, tokens que pertenecen a un tercero."),
]


def abrir_consulta() -> None:
    """Abre la chuleta. Cierra los avisos de guardado por si hubiera uno vivo."""
    for clave in PROTOCOLOS:
        limpiar(clave)
    st.session_state[_CONSULTA] = True


# Ámbar de marca Reental. El botón va relleno y no en gris para que se vea a la
# primera: si hay que buscarlo, la gente vuelve a hacer la operación de memoria,
# que es justo lo que se quiere evitar. Se estila por su `key`, que Streamlit
# expone como clase `.st-key-<key>`, para no pintar de ámbar todos los botones.
AMBAR_REENTAL = "#f5a623"
_CSS_BOTON = """
    <style>
    .st-key-{key} button {{
        background-color: {amb} !important;
        color: #0d1b2e !important;
        border: none !important;
        font-weight: 700 !important;
        border-radius: 8px !important;
    }}
    .st-key-{key} button:hover {{
        background-color: #e0951f !important;
        color: #0d1b2e !important;
    }}
    .st-key-{key} button:active {{
        background-color: #c47f16 !important;
        color: #0d1b2e !important;
    }}
    </style>
"""


def boton_consulta(key: str = "btn_protocolo_otc", ancho: bool = True) -> None:
    """Botón + modal de consulta. Se llama una sola vez por página."""
    st.markdown(_CSS_BOTON.format(key=key, amb=AMBAR_REENTAL), unsafe_allow_html=True)
    if st.button("📖 Ver protocolo de operación", key=key,
                 use_container_width=ancho,
                 help="Los pasos a seguir en una operación OTC de tercero, según seas "
                      "el comercial del vendedor o el del comprador. Consultable en "
                      "cualquier momento."):
        abrir_consulta()
        st.rerun()

    if st.session_state.get(_CONSULTA):
        _modal_consulta()


@st.dialog("Protocolo de una operación OTC de tercero", width="large")
def _modal_consulta() -> None:
    st.caption(
        "Una venta entre inversores la ejecuta Reental en dos tramos: primero le compra "
        "los tokens al vendedor y después se los vende al comprador. Por eso hay dos "
        "secuencias distintas y cada comercial tiene que hacer la suya."
    )
    pestanas = st.tabs([etiqueta for _, etiqueta, _ in ROLES])
    for pestana, (clave, _, cuando) in zip(pestanas, ROLES):
        with pestana:
            st.markdown(f"**Cuándo te aplica:** {cuando}")
            st.markdown(_html(clave, con_intro=False), unsafe_allow_html=True)

    st.markdown("---")
    st.markdown(
        f'<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;'
        f'padding:12px 16px;font-size:0.84rem;color:#334155;">'
        f'<b>Datos que siempre hacen falta</b><br>'
        f'SAFE OTC de Reental: <code>{SAFE_OTC}</code><br>'
        f'Excel de OFF-RAMP: <a href="{URL_OFFRAMP}" target="_blank">abrir</a> '
        f'— imprescindible el hash del envío de los tokens.</div>',
        unsafe_allow_html=True,
    )
    # El error caro del proceso, repetido aquí: es el único paso cuyo orden
    # dispara algo irreversible fuera de la herramienta.
    st.warning(
        "**El justificante de pago no se sube al ADMIN hasta que los tokens estén en la "
        "wallet OTC.** Subirlo antes dispara el envío de contratos de una operación que "
        "todavía puede caerse."
    )
    if st.button("Cerrar", use_container_width=True, key="_otc_cerrar_consulta"):
        st.session_state.pop(_CONSULTA, None)
        st.rerun()


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
