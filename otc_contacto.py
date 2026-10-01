"""
El correo del inversor en una reserva o una oferta de OTC.

POR QUÉ ESTÁ AQUÍ
-----------------
El correo es el campo con el que alguien que revisa una reserva identifica al
inversor en el CRM. Si está mal escrito no sirve de nada: no es un adorno, es
una clave de búsqueda.

Lo piden tres sitios —la reserva nueva y la oferta de tercero en la gestión OTC,
y la reserva que crea el constructor de propuestas— y cada uno comprobaba (o no)
una cosa distinta: la oferta solo miraba que no estuviera vacío y el constructor
no lo pedía siquiera. Tres validaciones del mismo campo son tres criterios que
divergen, así que vive aquí. Es la regla del repositorio.

NO importa Streamlit, para poder probarlo desde un script.

QUÉ SE DESCARTÓ
---------------
Una expresión regular que persiguiera el RFC 5322 completo. Es larga, nadie la
revisa y rechaza direcciones válidas raras mientras acepta erratas corrientes.
Lo que de verdad falla al teclear a mano es otra cosa —falta la arroba, falta el
punto del dominio, se cuela un espacio, se pegan dos direcciones— y eso es lo
que se comprueba. Quien escriba `juan@gmial.com` pasará el filtro aquí y lo
pillará el CRM, que es donde se puede pillar.
"""
from __future__ import annotations

# Un correo más largo que esto no existe en la práctica y delata que se ha
# pegado algo que no era un correo (una fila entera, una lista de direcciones).
_LARGO_MAXIMO = 254

# El valor que ocupa el hueco cuando todavía no se sabe el correo. Existe porque
# el campo es obligatorio y había reservas vivas creadas antes de que existiera:
# sin un valor explícito, o se bloqueaban esas reservas para cualquier otro
# cambio, o se dejaban en blanco y el blanco se lee como «este inversor no tiene
# correo» en vez de «esto falta por rellenar».
PENDIENTE = "pendiente de indicar"


def normalizar(email: str | None) -> str:
    """Quita espacios alrededor y pasa a minúsculas.

    Se normaliza a minúsculas a propósito: el dominio es insensible a mayúsculas
    y las búsquedas en el CRM también, así que guardar `Juan@Reental.co` y
    `juan@reental.co` como dos cosas distintas solo crea duplicados. El coste es
    que un buzón que de verdad distinga mayúsculas en la parte local —legal según
    la norma, inexistente en la práctica— se guardaría en minúsculas.
    """
    return (email or "").strip().lower()


def error(email: str | None, obligatorio: bool = True,
          permitir_pendiente: bool = False) -> str | None:
    """Devuelve el mensaje de error, o None si el correo vale.

    Devuelve el texto para enseñarlo tal cual en vez de un booleano: el mensaje
    tiene que decir QUÉ está mal, porque «email inválido» obliga a adivinar.

    `permitir_pendiente` acepta además el valor PENDIENTE. Se activa al EDITAR
    una reserva y no al crearla: una reserva nueva se crea hablando con el
    comprador, así que el correo se tiene; una antigua hay que poder guardarla
    reconociendo que falta, o no se puede ni corregirle el precio.
    """
    valor = normalizar(email)

    if permitir_pendiente and valor == PENDIENTE:
        return None
    if not valor:
        return "El email del inversor es obligatorio." if obligatorio else None
    # El orden importa: se comprueba primero lo que falta y después lo que sobra,
    # porque «sin arroba» con un espacio dentro es un texto que no es un correo y
    # el aviso útil es el de la arroba, no el del espacio.
    if "@" not in valor:
        return "El email tiene que llevar una arroba. Comprueba que no sea el nombre del inversor."
    if valor.count("@") != 1:
        return ("El email debe llevar una sola arroba. Si estás pegando varias "
                "direcciones, anota solo la del titular.")
    if " " in valor:
        return "El email no puede contener espacios."

    local, dominio = valor.split("@")
    if not local:
        return "Falta la parte anterior a la arroba."
    if not dominio:
        return "Falta el dominio (lo que va después de la arroba)."
    if "." not in dominio:
        return f"El dominio «{dominio}» no parece completo: le falta la extensión (.com, .es…)."
    if dominio.startswith(".") or dominio.endswith(".") or ".." in dominio:
        return f"El dominio «{dominio}» está mal escrito."
    if len(valor) > _LARGO_MAXIMO:
        return "El email es demasiado largo: comprueba que no se haya pegado otra cosa."
    return None


def valor(email: str | None) -> str:
    """Lo que se enseña y se exporta: el correo, o PENDIENTE si no lo hay.

    Un hueco vacío en la lista se lee como «este inversor no tiene correo» en vez
    de «esto falta por rellenar», así que no se deja ninguno.

    Esto resuelve el hueco **al mostrar**, no reescribiendo las filas antiguas
    del almacén. Rellenar cientos de reservas vivas de golpe es una escritura
    masiva sobre datos que comprometen tokens, con el riesgo de pisar lo que otro
    esté guardando en ese momento (ver trampa 9); y no haría falta para nada,
    porque lo que se ve acaba siendo exactamente lo mismo. Una fila se actualiza
    cuando alguien la edita, que es cuando de verdad se sabe el correo.
    """
    return normalizar(email) or PENDIENTE


def es_pendiente(email: str | None) -> bool:
    """Si ese correo todavía no se sabe —esté vacío o puesto como PENDIENTE—."""
    return valor(email) == PENDIENTE
