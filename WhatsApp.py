from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from openai import OpenAI

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env" if (SCRIPT_DIR / ".env").exists() else None)

META_TOKEN = os.getenv("META_TOKEN", "").strip()
META_PHONE_NUMBER_ID = os.getenv(
    "META_PHONE_NUMBER_ID", "1282850558252324"
).strip()
META_API_VERSION = os.getenv("META_API_VERSION", "v26.0").strip()
META_WEBHOOK_VERIFY_TOKEN = os.getenv(
    "META_WEBHOOK_VERIFY_TOKEN", ""
).strip()

WHATSAPP_ALERT_TEMPLATE_NAME = os.getenv(
    "WHATSAPP_ALERT_TEMPLATE_NAME", "alerta_dof"
).strip()
WHATSAPP_ALERT_TEMPLATE_LANGUAGE = os.getenv(
    "WHATSAPP_ALERT_TEMPLATE_LANGUAGE", "es_MX"
).strip()

ADMIN_WHATSAPP_ID = os.getenv(
    "ADMIN_WHATSAPP_ID", "5215543531413"
).strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_TRANSCRIPTION_MODEL = os.getenv(
    "OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe"
).strip()

GRAPH_BASE = f"https://graph.facebook.com/{META_API_VERSION}"
HTTP_TIMEOUT = int(os.getenv("META_HTTP_TIMEOUT", "30"))


def _normalizar(numero: str) -> str:
    return re.sub(r"\D", "", str(numero or ""))


def validar_configuracion_meta() -> None:
    faltantes = []

    if not META_TOKEN:
        faltantes.append("META_TOKEN")

    if not META_PHONE_NUMBER_ID:
        faltantes.append("META_PHONE_NUMBER_ID")

    if faltantes:
        raise RuntimeError(
            "Faltan variables de Meta: " + ", ".join(faltantes)
        )


def _headers() -> dict[str, str]:
    validar_configuracion_meta()

    return {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json",
    }


def _post_message(payload: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        f"{GRAPH_BASE}/{META_PHONE_NUMBER_ID}/messages",
        headers=_headers(),
        json=payload,
        timeout=HTTP_TIMEOUT,
    )

    if not response.ok:
        raise RuntimeError(
            f"Meta WhatsApp | HTTP {response.status_code} | {response.text}"
        )

    return response.json()


def _enviar_boton_url(
    telefono: str,
    texto: str,
    url: str,
    texto_boton="Realizar pago",
) -> dict[str, Any]:
    telefono = _normalizar(telefono)
    texto = str(texto or "").strip()
    url = str(url or "").strip()
    texto_boton = str(texto_boton or "").strip()

    if not telefono:
        raise ValueError("El teléfono está vacío.")

    if not texto:
        raise ValueError("El texto está vacío.")

    if not url:
        raise ValueError("La URL del botón está vacía.")

    if not texto_boton:
        raise ValueError("El texto del botón está vacío.")

    return _post_message(
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": telefono,
            "type": "interactive",
            "interactive": {
                "type": "cta_url",
                "body": {
                    "text": texto,
                },
                "action": {
                    "name": "cta_url",
                    "parameters": {
                        "display_text": texto_boton,
                        "url": url,
                    },
                },
            },
        }
    )


def enviar_texto(telefono: str, texto: str) -> dict[str, Any]:
    telefono = _normalizar(telefono)
    texto = str(texto or "").strip()

    if not telefono:
        raise ValueError("El teléfono está vacío.")

    if not texto:
        raise ValueError("El texto está vacío.")

    patron_pago = re.compile(
        r"(?:\r?\n){0,2}\[Usa este enlace para pagar\]\((https?://[^\s)]+)\)\s*$",
        flags=re.IGNORECASE,
    )
    coincidencia = patron_pago.search(texto)

    if coincidencia:
        url_pago = coincidencia.group(1).strip()
        cuerpo = texto[:coincidencia.start()].strip()
        return _enviar_boton_url(
            telefono=telefono,
            texto=cuerpo,
            url=url_pago,
            texto_boton="Realizar pago",
        )

    return _post_message(
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": telefono,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": texto,
            },
        }
    )


def enviar_alerta_template(
    telefono: str,
    contenido: str,
) -> dict[str, Any]:
    """
    Envía la plantilla Utility alerta_dof usando {{1}}
    como contenido dinámico.
    """

    telefono = _normalizar(telefono)
    contenido = str(contenido or "").strip()

    # Meta no permite saltos de línea ni tabs dentro
    # de parámetros de templates.
    contenido = re.sub(r"[\r\n\t]+", " ", contenido)
    contenido = re.sub(r" {2,}", " ", contenido).strip()

    # Formatear cada alerta encontrada dentro del mismo {{1}}.
    #
    # Formato recibido actualmente desde Alerta.py:
    #
    # Alerta 1: "Ley del IVA" Resumen: ...
    # Referencia: Página ..., edición Matutina.
    # Alerta 2: "Ley del ISR" Resumen: ...
    #
    contenido = re.sub(
        r'Alerta\s+(\d+):\s*["“]?(.+?)["”]?\s+Resumen:\s*',
        r'🔔 *Alerta \1: \2* · 📌 *Resumen:* ',
        contenido,
        flags=re.IGNORECASE,
    )

    contenido = re.sub(
        r'\s+Referencia:\s*',
        r' · 📄 *Referencia:* ',
        contenido,
        flags=re.IGNORECASE,
    )

    # Separar visualmente alertas consecutivas sin utilizar
    # saltos de línea, ya que Meta los rechaza dentro de {{1}}.
    contenido = re.sub(
        r'\.\s+(?=🔔 \*Alerta\s+\d+:)',
        r'.  ┃  ',
        contenido,
    )

    # Seguridad adicional: evitar más de cuatro espacios
    # consecutivos después del formato.
    contenido = re.sub(r" {2,}", " ", contenido).strip()

    if not telefono:
        raise ValueError("El teléfono está vacío.")

    if not contenido:
        raise ValueError("El contenido de la alerta está vacío.")

    return _post_message(
        {
            "messaging_product": "whatsapp",
            "to": telefono,
            "type": "template",
            "template": {
                "name": WHATSAPP_ALERT_TEMPLATE_NAME,
                "language": {
                    "code": WHATSAPP_ALERT_TEMPLATE_LANGUAGE,
                },
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {
                                "type": "text",
                                "text": contenido,
                            }
                        ],
                    }
                ],
            },
        }
    )



def _limpiar_parametro_template(texto: str) -> str:
    texto = str(texto or "").strip()
    texto = re.sub(r"[\r\n\t]+", " ", texto)
    texto = re.sub(r" {2,}", " ", texto).strip()
    return texto


def enviar_alerta_template_por_cantidad(
    telefono: str,
    contenidos: list[str],
) -> dict[str, Any]:
    """
    Envía alerta_dof_1 ... alerta_dof_5 según la cantidad de
    alertas relevantes.

    Cada alerta utiliza tres variables en la plantilla de Meta:
    tema, resumen y referencia.
    """
    telefono = _normalizar(telefono)

    if not telefono:
        raise ValueError("El teléfono está vacío.")

    contenidos_validos = [
        str(contenido or "").strip()
        for contenido in contenidos
        if str(contenido or "").strip()
    ]

    cantidad = len(contenidos_validos)

    if cantidad < 1 or cantidad > 5:
        raise ValueError(
            "La cantidad de alertas relevantes debe estar entre 1 y 5."
        )

    parametros = []

    for contenido in contenidos_validos:
        contenido_limpio = _limpiar_parametro_template(contenido)

        match = re.match(
            r'^Alerta\s+\d+:\s*["“]?(.+?)["”]?\s+'
            r'Resumen:\s*(.+?)\s+'
            r'Referencia:\s*(.+?)\s*$',
            contenido_limpio,
            flags=re.IGNORECASE,
        )

        if not match:
            raise ValueError(
                "No se pudo separar la alerta en tema, resumen y referencia: "
                + contenido_limpio
            )

        tema = _limpiar_parametro_template(match.group(1))
        resumen = _limpiar_parametro_template(match.group(2))
        referencia = _limpiar_parametro_template(match.group(3))

        parametros.extend(
            [
                {"type": "text", "text": tema},
                {"type": "text", "text": resumen},
                {"type": "text", "text": referencia},
            ]
        )

    nombre_plantilla = f"alerta_dof_{cantidad}"

    return _post_message(
        {
            "messaging_product": "whatsapp",
            "to": telefono,
            "type": "template",
            "template": {
                "name": nombre_plantilla,
                "language": {
                    "code": WHATSAPP_ALERT_TEMPLATE_LANGUAGE,
                },
                "components": [
                    {
                        "type": "body",
                        "parameters": parametros,
                    }
                ],
            },
        }
    )

def notificar_administrador(texto: str) -> dict[str, Any]:
    """Envía una notificación operativa al administrador de Monitor DOF."""
    return enviar_texto(ADMIN_WHATSAPP_ID, texto)

def obtener_media_url(media_id: str) -> str:
    response = requests.get(
        f"{GRAPH_BASE}/{media_id}",
        headers={
            "Authorization": f"Bearer {META_TOKEN}",
        },
        timeout=HTTP_TIMEOUT,
    )

    if not response.ok:
        raise RuntimeError(
            f"Meta media | HTTP {response.status_code} | {response.text}"
        )

    url = str(response.json().get("url") or "").strip()

    if not url:
        raise RuntimeError(
            "Meta no devolvió URL para el audio."
        )

    return url


def descargar_media(media_id: str) -> tuple[bytes, str]:
    url = obtener_media_url(media_id)

    response = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {META_TOKEN}",
        },
        timeout=HTTP_TIMEOUT,
    )

    if not response.ok:
        raise RuntimeError(
            f"Descarga media | HTTP {response.status_code} | "
            f"{response.text}"
        )

    return (
        response.content,
        response.headers.get(
            "content-type",
            "application/octet-stream",
        ),
    )


def transcribir_audio(media_id: str) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "Falta OPENAI_API_KEY para transcribir audio."
        )

    contenido, content_type = descargar_media(media_id)

    extension = ".ogg"

    if "mpeg" in content_type:
        extension = ".mp3"
    elif "mp4" in content_type:
        extension = ".m4a"
    elif "wav" in content_type:
        extension = ".wav"

    client = OpenAI(
        api_key=OPENAI_API_KEY,
    )

    temporal = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=extension,
            delete=False,
        ) as fh:
            fh.write(contenido)
            temporal = fh.name

        with open(temporal, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model=OPENAI_TRANSCRIPTION_MODEL,
                file=audio_file,
            )

        return str(
            getattr(transcription, "text", "") or ""
        ).strip()

    finally:
        if temporal:
            Path(temporal).unlink(
                missing_ok=True,
            )


def extraer_mensajes(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    mensajes: list[dict[str, Any]] = []

    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}

            for message in value.get("messages", []) or []:
                item = dict(message)

                item["phone_number_id"] = (
                    value.get("metadata") or {}
                ).get("phone_number_id")

                mensajes.append(item)

    return mensajes


def obtener_texto_entrante(
    message: dict[str, Any],
) -> tuple[str | None, str]:
    tipo = str(
        message.get("type") or ""
    ).strip().lower()

    if tipo == "text":
        return (
            str(
                (message.get("text") or {}).get("body") or ""
            ).strip(),
            tipo,
        )

    if tipo == "audio":
        media_id = str(
            (message.get("audio") or {}).get("id") or ""
        ).strip()

        if not media_id:
            raise RuntimeError(
                "El evento de audio no contiene media_id."
            )

        return (
            transcribir_audio(media_id),
            tipo,
        )

    return None, tipo


def mensaje_tipo_no_soportado() -> str:
    return (
        "Por ahora puedo recibir mensajes de texto o audio."
    )


def notificar_ayuda_administrador(
    conversation: dict,
) -> dict[str, Any] | None:
    ayuda = conversation.get("ayuda") or {}

    if ayuda.get("pendiente") is not True:
        return None

    telefono = str(
        ayuda.get("telefono")
        or conversation.get("conversation_id")
        or ""
    ).strip()

    tipo = str(
        ayuda.get("tipo") or "otra_ayuda"
    ).replace("_", " ")

    mensaje_usuario = str(
        ayuda.get("mensaje_usuario") or ""
    ).strip()

    contexto = str(
        ayuda.get("contexto") or ""
    ).strip()

    detalle = str(
        ayuda.get("detalle_tecnico") or ""
    ).strip()

    texto = (
        "AYUDA MONITOR DOF\n"
        f"Teléfono: {telefono}\n"
        f"Tipo: {tipo}\n"
        f"Mensaje: {mensaje_usuario}\n"
        f"Contexto: {contexto}"
    )

    if detalle:
        texto += f"\nDetalle: {detalle}"

    texto += (
        "\n\nResponde a este mensaje con una instrucción en lenguaje natural. "
        "El agente la usará para redactar la respuesta al usuario."
    )

    return enviar_texto(
        ADMIN_WHATSAPP_ID,
        texto,
    )


def procesar_respuesta_administrador(
    texto: str,
) -> tuple[str, str] | None:
    """
    Formato:
    TELEFONO | instrucción para el agente.
    """

    match = re.match(
        r"^\s*([+\d\s()-]{8,})\s*\|\s*(.+?)\s*$",
        str(texto or ""),
        re.S,
    )

    if not match:
        return None

    telefono = _normalizar(
        match.group(1)
    )

    instruccion = match.group(2).strip()

    if not telefono or not instruccion:
        return None

    return telefono, instruccion