from __future__ import annotations

import os

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import Json, RealDictCursor


# ============================================================
# CONFIGURACIÓN
# ============================================================

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "").strip()
DB_NAME = os.getenv("DB_NAME", "").strip()
DB_USER = os.getenv("DB_USER", "").strip()
DB_PASSWORD = os.getenv("DB_PASSWORD", "").strip()
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_SSLMODE = os.getenv("DB_SSLMODE", "require").strip()

TABLE = "Conversaciones_DOF"


# ============================================================
# CONEXIÓN POSTGRESQL
# ============================================================

def _conn():
    return psycopg2.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        port=DB_PORT,
        sslmode=DB_SSLMODE,
    )


# ============================================================
# CREAR TABLA SI NO EXISTE
# ============================================================

def asegurar_tabla() -> None:
    conn = _conn()

    try:
        with conn.cursor() as cur:
            cur.execute(
                f'''
                CREATE TABLE IF NOT EXISTS "{TABLE}" (
                    "telefono" TEXT PRIMARY KEY,
                    "conversation" JSONB NOT NULL,
                    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                '''
            )
            cur.execute('''
                CREATE TABLE IF NOT EXISTS "Conversaciones_Mensajes_DOF" (
                    "id" BIGSERIAL PRIMARY KEY,
                    "telefono" TEXT NOT NULL,
                    "indice" INTEGER NOT NULL,
                    "message_id_origen" TEXT,
                    "role" TEXT,
                    "content" TEXT,
                    "metadata" JSONB,
                    "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            ''')
            cur.execute('''CREATE INDEX IF NOT EXISTS "idx_conv_mensajes_dof_telefono_indice"
                           ON "Conversaciones_Mensajes_DOF" ("telefono", "indice")''')
            cur.execute('''CREATE UNIQUE INDEX IF NOT EXISTS "idx_conv_mensajes_dof_message_id_origen"
                           ON "Conversaciones_Mensajes_DOF" ("message_id_origen")
                           WHERE "message_id_origen" IS NOT NULL''')

        conn.commit()

    except Exception as error:
        conn.rollback()
        raise RuntimeError(
            f"Conversaciones_DOF | asegurar tabla | {type(error).__name__} | {error}"
        ) from error

    finally:
        conn.close()


def inicializar_tabla() -> None:
    """Crea la tabla si no existe y elimina únicamente sus registros."""
    asegurar_tabla()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f'TRUNCATE TABLE "{TABLE}"')
            cur.execute('TRUNCATE TABLE "Conversaciones_Mensajes_DOF" RESTART IDENTITY')
        conn.commit()
    except Exception as error:
        conn.rollback()
        raise RuntimeError(
            f"Conversaciones_DOF | vaciar tabla | {type(error).__name__} | {error}"
        ) from error
    finally:
        conn.close()


# ============================================================
# CARGAR CONVERSACIÓN
# ============================================================

def cargar_conversacion(
    telefono: str,
) -> dict | None:

    asegurar_tabla()

    conn = _conn()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                f'''
                SELECT "conversation"
                FROM "{TABLE}"
                WHERE "telefono" = %s
                ''',
                (telefono,),
            )

            row = cur.fetchone()

            if not row:
                return None

            conversation = row["conversation"]

            if isinstance(conversation, dict):
                return conversation

            return dict(conversation)

    finally:
        conn.close()


# ============================================================
# GUARDAR CONVERSACIÓN
# ============================================================

def guardar_conversacion(
    telefono: str,
    conversation: dict,
) -> None:

    asegurar_tabla()

    conn = _conn()

    try:
        with conn.cursor() as cur:

            # Conserva el JSONB actual para compatibilidad y, en paralelo, guarda
            # cada mensaje nuevo como registro individual para analítica futura.
            cur.execute(f'SELECT "conversation" FROM "{TABLE}" WHERE "telefono" = %s', (telefono,))
            fila_anterior = cur.fetchone()
            mensajes_anteriores = []
            if fila_anterior and isinstance(fila_anterior[0], dict):
                mensajes_anteriores = fila_anterior[0].get("messages", []) or []
            mensajes_actuales = conversation.get("messages", []) or []
            for indice, item in enumerate(mensajes_actuales[len(mensajes_anteriores):], start=len(mensajes_anteriores)):
                cur.execute('''
                    INSERT INTO "Conversaciones_Mensajes_DOF"
                        ("telefono","indice","message_id_origen","role","content","metadata")
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                ''', (
                    telefono, indice, (item.get("metadata") or {}).get("message_id"),
                    item.get("role"), str(item.get("content") or ""),
                    Json(item.get("metadata") or {}),
                ))

            cur.execute(
                f'''
                INSERT INTO "{TABLE}" (
                    "telefono",
                    "conversation",
                    "updated_at"
                )
                VALUES (
                    %s,
                    %s,
                    NOW()
                )

                ON CONFLICT ("telefono")
                DO UPDATE SET
                    "conversation" = EXCLUDED."conversation",
                    "updated_at" = NOW()
                ''',
                (
                    telefono,
                    Json(conversation),
                ),
            )

        conn.commit()

    finally:
        conn.close()


# ============================================================
# LISTAR CONVERSACIONES
# ============================================================

def listar_conversaciones() -> list[dict]:

    asegurar_tabla()

    conn = _conn()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                f'''
                SELECT
                    "telefono",
                    "conversation",
                    "updated_at"
                FROM "{TABLE}"
                ORDER BY "updated_at" DESC
                '''
            )

            return [
                dict(row)
                for row in cur.fetchall()
            ]

    finally:
        conn.close()


# ============================================================
# AGREGAR MENSAJE PROACTIVO
# ============================================================

def agregar_mensaje_proactivo(
    telefono: str,
    contenido: str,
    metadata: dict | None = None,
) -> dict:

    telefono = "".join(
        caracter
        for caracter in str(
            telefono or ""
        )
        if caracter.isdigit()
    )

    if not telefono:
        raise ValueError(
            "No se recibió un número de teléfono válido."
        )

    conversation = (
        cargar_conversacion(telefono)
        or {
            "conversation_id": telefono,
            "messages": [],
            "proceso_activo": None,
            "router_state": {
                "current_category": None,
                "previous_category": None,
                "continues_previous_topic": None,
                "topic_changed": None,
                "reason": None,
                "selected_module": None,
            },
        }
    )

    item = {
        "role": "assistant",
        "content": contenido,
    }

    if metadata:
        item["metadata"] = metadata

    conversation.setdefault(
        "messages",
        [],
    ).append(item)

    guardar_conversacion(
        telefono=telefono,
        conversation=conversation,
    )

    return conversation

if __name__ == "__main__":
    inicializar_tabla()
    print(f'Tabla "{TABLE}" lista; contenido eliminado sin modificar su estructura.')
