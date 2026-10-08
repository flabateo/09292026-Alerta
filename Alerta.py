from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urljoin
import re
import html

import requests
import csv
import os

import psycopg2
from dotenv import load_dotenv
from openai import OpenAI
from psycopg2 import sql
from psycopg2.extras import execute_values
from pypdf import PdfReader


# ============================================================
# CONFIGURACIÓN
# ============================================================

URL_DOF = "https://dof.gob.mx"
CARPETA_PUBLICACION = Path("Publicacion")
CARPETA_CSV = Path("CSV")
ARCHIVO_CSV = CARPETA_CSV / "Publicacion_DOF.csv"

# Tabla PostgreSQL que contendrá la publicación actual del DOF.
TABLA_PUBLICACION_DOF = "Publicacion_DOF"
CAMPOS_INDEXAR_PUBLICACION = ["pagina", "contenido"]
COLUMNA_CLAVE_PUBLICACION = "Clave"
COLUMNA_EMBEDDING_PUBLICACION = "Embedding"
INDEX_EMBEDDING_PUBLICACION = "idx_Publicacion_DOF_Embedding_hnsw"

# Cargar .env ubicado junto al script.
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_SCRIPT = SCRIPT_DIR / ".env"

if ENV_SCRIPT.exists():
    load_dotenv(ENV_SCRIPT)
else:
    load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_SSLMODE = os.getenv("DB_SSLMODE", "require")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_EMBEDDING_MODEL = os.getenv(
    "OPENAI_EMBEDDING_MODEL",
    "text-embedding-3-small",
)
OPENAI_EMBEDDING_DIMENSIONS = int(
    os.getenv("OPENAI_EMBEDDING_DIMENSIONS", "1536")
)
EMBEDDING_BATCH_SIZE = int(
    os.getenv("EMBEDDING_BATCH_SIZE", "100")
)
EMBEDDING_MAX_CHARS = int(
    os.getenv("EMBEDDING_MAX_CHARS", "12000")
)
INSERT_BATCH_SIZE = int(
    os.getenv("INSERT_BATCH_SIZE", "500")
)

# 0 = hoy / PRODUCCIÓN
# 1 = ayer
# 2 = hace dos días
# 3 = hace tres días
# etc.
DIAS_ATRAS = int(os.getenv("DIAS_ATRAS", "0"))

# Intervalo entre inicios de ejecución del monitor.
# Si una ejecución tarda más que este intervalo, la siguiente comienza
# inmediatamente después de terminar la anterior.
INTERVALO_MINUTOS = max(
    1,
    int(os.getenv("INTERVALO_MINUTOS", "15")),
)

# Mostrar parte del HTML recibido cuando algo falla.
MOSTRAR_DEBUG_HTML = True
MAX_DEBUG_HTML = 3000

# ============================================================
# FECHA
# ============================================================

FECHA_CONSULTA = (
    datetime.now() - timedelta(days=DIAS_ATRAS)
).strftime("%d/%m/%Y")


# ============================================================
# VARIABLES DE RESULTADO
# ============================================================

NUEVO_MATUTINO = "No"
NUEVO_VESPERTINO = "No"
NUEVO_CSV = "No"
Nueva_Tabla = "No"
URL_DESCARGA_EDICION = {}


# ============================================================
# SESIÓN HTTP
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/152.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/pdf;q=0.8,*/*;q=0.7"
    ),
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
})


# ============================================================
# UTILIDADES
# ============================================================

def es_pdf(response):
    """
    Comprueba la firma binaria real del archivo.
    """

    if response.status_code != 200:
        return False

    if not response.content:
        return False

    return response.content.lstrip().startswith(b"%PDF")


def mostrar_diagnostico(response, titulo):
    """
    Muestra información útil de una respuesta HTTP.
    """

    print()
    print("-" * 60)
    print(titulo)
    print("-" * 60)

    print(f"HTTP: {response.status_code}")
    print(f"URL final: {response.url}")

    print(
        "Content-Type: "
        + response.headers.get(
            "Content-Type",
            "No informado"
        )
    )

    print(
        "Content-Length: "
        + response.headers.get(
            "Content-Length",
            "No informado"
        )
    )

    print(
        f"Bytes recibidos: {len(response.content)}"
    )

    if response.history:

        print("Redirecciones HTTP:")

        for r in response.history:
            print(
                f"  {r.status_code} -> {r.url}"
            )

    else:
        print("Redirecciones HTTP: ninguna")


def mostrar_html_debug(response):
    """
    Muestra una parte del HTML para poder diagnosticar
    respuestas inesperadas del DOF.
    """

    if not MOSTRAR_DEBUG_HTML:
        return

    try:
        texto = response.text

    except Exception as error:
        print(
            f"No fue posible leer el HTML: {error}"
        )
        return

    print()
    print("INICIO HTML RECIBIDO")
    print("-" * 60)

    print(
        texto[:MAX_DEBUG_HTML]
    )

    print("-" * 60)
    print("FIN HTML RECIBIDO")


# ============================================================
# URL NOTA_TO_PDF
# ============================================================

def construir_url_nota(fecha, edicion):

    return (
        f"{URL_DOF}/nota_to_pdf.php"
        f"?fecha={fecha}"
        f"&edicion={edicion}"
    )


# ============================================================
# BUSCAR abrirPDF.php EN EL HTML
# ============================================================

def buscar_url_abrir_pdf(texto_html):
    """
    Busca una URL abrirPDF.php dentro del HTML/JavaScript
    devuelto por nota_to_pdf.php.

    Maneja &amp; y URLs relativas.
    """

    texto_html = html.unescape(texto_html)

    patrones = [

        # abrirPDF.php?archivo=...&anio=...
        r"""(?:https?://[^"'<> ]+)?/?abrirPDF\.php\?[^"'<> ]+""",

        # Casos donde la URL esté escapada.
        r"""(?:https?:\\/\\/[^"'<> ]+)?\\?/abrirPDF\.php\?[^"'<> ]+""",
    ]

    for patron in patrones:

        coincidencia = re.search(
            patron,
            texto_html,
            flags=re.IGNORECASE
        )

        if coincidencia:

            url = coincidencia.group(0)

            # Normalizar escapes JavaScript.
            url = url.replace("\\/", "/")

            # Convertir URL relativa a absoluta.
            return urljoin(
                URL_DOF + "/",
                url
            )

    return None


# ============================================================
# DESCARGAR URL
# ============================================================

def solicitar_url(url, referer=None):
    """
    Realiza una petición GET con manejo de errores.
    """

    headers = {}

    if referer:
        headers["Referer"] = referer

    try:

        response = session.get(
            url,
            headers=headers,
            timeout=(20, 120),
            allow_redirects=True
        )

        return response

    except requests.exceptions.Timeout:

        print(
            "ERROR: el servidor tardó demasiado "
            "en responder."
        )

    except requests.exceptions.ConnectionError as error:

        print(
            f"ERROR de conexión: {error}"
        )

    except requests.exceptions.RequestException as error:

        print(
            f"ERROR HTTP: {error}"
        )

    return None


# ============================================================
# PROCESAR EDICIÓN
# ============================================================

def procesar_edicion(fecha, edicion):

    print()
    print("=" * 60)
    print(f"PROCESANDO EDICIÓN {edicion}")
    print("=" * 60)

    # --------------------------------------------------------
    # NOMBRE DEL ARCHIVO LOCAL
    # --------------------------------------------------------

    fecha_archivo = datetime.strptime(
        fecha,
        "%d/%m/%Y"
    ).strftime("%Y-%m-%d")

    nombre_archivo = (
        f"DOF_{fecha_archivo}_{edicion}.pdf"
    )

    archivo_destino = (
        CARPETA_PUBLICACION / nombre_archivo
    )

    # --------------------------------------------------------
    # COMPROBAR SI YA EXISTE
    # --------------------------------------------------------

    if archivo_destino.exists():

        print(
            f"El archivo ya existe:"
        )

        print(
            archivo_destino
        )

        print(
            "No se realizará ninguna descarga."
        )

        return "No"

    # --------------------------------------------------------
    # PASO 1
    # nota_to_pdf.php
    # --------------------------------------------------------

    url_nota = construir_url_nota(
        fecha,
        edicion
    )

    print()
    print("PASO 1 - Consultando nota_to_pdf.php")

    print(
        f"URL: {url_nota}"
    )

    response = solicitar_url(
        url_nota
    )

    if response is None:

        print(
            "RESULTADO: no fue posible obtener "
            "respuesta del DOF."
        )

        return "No"

    mostrar_diagnostico(
        response,
        "RESPUESTA nota_to_pdf.php"
    )

    # --------------------------------------------------------
    # CASO A:
    # nota_to_pdf devuelve directamente un PDF
    # --------------------------------------------------------

    if es_pdf(response):

        print()
        print(
            "nota_to_pdf.php devolvió directamente "
            "un PDF."
        )

        archivo_destino.write_bytes(
            response.content
        )

        URL_DESCARGA_EDICION[edicion] = response.url

        print(
            f"Archivo guardado: {archivo_destino}"
        )

        return "Yes"

    # --------------------------------------------------------
    # CASO B:
    # DEVUELVE HTML
    # --------------------------------------------------------

    try:
        texto_respuesta = response.text

    except Exception as error:

        print(
            f"ERROR leyendo respuesta HTML: {error}"
        )

        return "No"

    # --------------------------------------------------------
    # DETECTAR MENSAJE DE DOCUMENTO INEXISTENTE
    # --------------------------------------------------------

    texto_minusculas = texto_respuesta.lower()

    if (
        "no existe el documento que busca"
        in texto_minusculas
    ):

        print()
        print(
            "DOF indica que el documento "
            "no existe."
        )

        return "No"

    # --------------------------------------------------------
    # PASO 2:
    # BUSCAR abrirPDF.php
    # --------------------------------------------------------

    print()
    print(
        "PASO 2 - Buscando abrirPDF.php "
        "dentro de la respuesta..."
    )

    url_pdf_real = buscar_url_abrir_pdf(
        texto_respuesta
    )

    if not url_pdf_real:

        print()
        print(
            "ERROR: no se encontró una URL "
            "abrirPDF.php en la respuesta."
        )

        mostrar_html_debug(
            response
        )

        return "No"

    print()
    print(
        "URL abrirPDF encontrada:"
    )

    print(
        url_pdf_real
    )

    # --------------------------------------------------------
    # PASO 3:
    # SOLICITAR abrirPDF.php
    # --------------------------------------------------------

    print()
    print(
        "PASO 3 - Solicitando abrirPDF.php"
    )

    response_pdf = solicitar_url(
        url_pdf_real,
        referer=url_nota
    )

    if response_pdf is None:

        print(
            "ERROR: no fue posible consultar "
            "abrirPDF.php."
        )

        return "No"

    mostrar_diagnostico(
        response_pdf,
        "RESPUESTA abrirPDF.php"
    )

    # --------------------------------------------------------
    # VALIDAR PDF FINAL
    # --------------------------------------------------------

    if not es_pdf(response_pdf):

        try:

            texto_final = (
                response_pdf.text.lower()
            )

        except Exception:

            texto_final = ""

        if (
            "no existe el documento que busca"
            in texto_final
        ):

            print()
            print(
                "DOF indica: "
                "No existe el documento que busca."
            )

        else:

            print()
            print(
                "ERROR: abrirPDF.php no devolvió "
                "un archivo PDF válido."
            )

            mostrar_html_debug(
                response_pdf
            )

        return "No"

    # --------------------------------------------------------
    # GUARDAR PDF
    # --------------------------------------------------------

    try:

        archivo_destino.write_bytes(
            response_pdf.content
        )

    except OSError as error:

        print()
        print(
            f"ERROR guardando archivo: {error}"
        )

        return "No"

    # --------------------------------------------------------
    # VERIFICAR ARCHIVO
    # --------------------------------------------------------

    if not archivo_destino.exists():

        print(
            "ERROR: después de guardar, "
            "el archivo no existe."
        )

        return "No"

    tamaño = archivo_destino.stat().st_size

    if tamaño == 0:

        print(
            "ERROR: el archivo guardado "
            "tiene 0 bytes."
        )

        archivo_destino.unlink(
            missing_ok=True
        )

        return "No"

    print()
    print(
        "PDF descargado correctamente."
    )

    print(
        f"Archivo: {archivo_destino}"
    )

    print(
        f"Tamaño: {tamaño:,} bytes"
    )

    URL_DESCARGA_EDICION[edicion] = response_pdf.url

    return "Yes"


# ============================================================
# CREAR CSV DESDE PDF NUEVOS
# ============================================================

def seleccionar_publicacion_mas_reciente(fecha):
    """
    Selecciona una sola publicación para la fecha consultada.

    Prioridad:
      1. Vespertina de la fecha consultada.
      2. Matutina de la fecha consultada.

    Si existe alguna de las dos, elimina de Publicacion todos los
    demás PDF para garantizar que quede únicamente el archivo más
    reciente disponible del DOF.

    Si no existe ninguna edición para la fecha consultada, no modifica
    el contenido de la carpeta Publicacion.
    """
    fecha_archivo = datetime.strptime(fecha, "%d/%m/%Y").strftime("%Y-%m-%d")

    archivo_mat = CARPETA_PUBLICACION / f"DOF_{fecha_archivo}_MAT.pdf"
    archivo_ves = CARPETA_PUBLICACION / f"DOF_{fecha_archivo}_VES.pdf"

    if archivo_ves.exists():
        archivo_seleccionado = archivo_ves
        edicion_codigo = "VES"
        edicion_nombre = "Vespertina"
    elif archivo_mat.exists():
        archivo_seleccionado = archivo_mat
        edicion_codigo = "MAT"
        edicion_nombre = "Matutina"
    else:
        print()
        print("No existe publicación MAT ni VES para la fecha consultada.")
        print("No se modificará la carpeta Publicacion.")
        return None

    for archivo_pdf in CARPETA_PUBLICACION.glob("*.pdf"):
        if archivo_pdf.resolve() == archivo_seleccionado.resolve():
            continue

        try:
            archivo_pdf.unlink()
            print(f"Archivo anterior eliminado: {archivo_pdf}")
        except OSError as error:
            print(f"ERROR eliminando archivo anterior {archivo_pdf}: {error}")
            raise

    print()
    print(f"Publicación seleccionada: {archivo_seleccionado}")
    print(f"Edición seleccionada: {edicion_nombre}")

    return {
        "archivo": archivo_seleccionado,
        "edicion_codigo": edicion_codigo,
        "edicion_nombre": edicion_nombre,
        "url_descarga": URL_DESCARGA_EDICION.get(edicion_codigo, ""),
    }


def crear_csv_publicacion(publicacion):
    """
    Crea CSV/Publicacion_DOF.csv usando exclusivamente la publicación
    seleccionada. Nunca combina MAT y VES.
    """
    if not publicacion:
        print()
        print("No hay publicación seleccionada. No se generará el CSV.")
        return "No"

    archivo_pdf = publicacion["archivo"]
    edicion_nombre = publicacion["edicion_nombre"]
    url_descarga = publicacion["url_descarga"]

    if not archivo_pdf.exists():
        print()
        print(f"ERROR: no existe el PDF esperado: {archivo_pdf}")
        return "No"

    try:
        CARPETA_CSV.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print()
        print(f"ERROR creando carpeta CSV: {error}")
        return "No"

    filas = []

    try:
        lector = PdfReader(str(archivo_pdf))

        for numero_pagina, pagina_pdf in enumerate(lector.pages, start=1):
            contenido = pagina_pdf.extract_text() or ""
            filas.append({
                "pagina": numero_pagina,
                "contenido": contenido,
                "edicion": edicion_nombre,
                "descarga": url_descarga,
            })
    except Exception as error:
        print()
        print(f"ERROR procesando PDF {archivo_pdf}: {error}")
        return "No"

    try:
        with ARCHIVO_CSV.open("w", newline="", encoding="utf-8-sig") as archivo:
            escritor = csv.DictWriter(
                archivo,
                fieldnames=["pagina", "contenido", "edicion", "descarga"],
            )
            escritor.writeheader()
            escritor.writerows(filas)
    except OSError as error:
        print()
        print(f"ERROR guardando CSV: {error}")
        return "No"

    print()
    print("CSV generado correctamente.")
    print(f"Archivo: {ARCHIVO_CSV}")
    print(f"Edición utilizada: {edicion_nombre}")
    print(f"Páginas procesadas: {len(filas)}")

    return "Yes"



# ============================================================
# POSTGRESQL / EMBEDDINGS - PUBLICACION_DOF
# ============================================================

def limpiar_texto_db(valor):
    if valor is None:
        return ""
    return str(valor).replace("\x00", "").strip()


def texto_para_embedding_db(valor):
    texto = limpiar_texto_db(valor)
    return re.sub(r"\s+", " ", texto).strip()


def generar_clave_publicacion(registro):
    partes = []

    for campo in CAMPOS_INDEXAR_PUBLICACION:
        valor = texto_para_embedding_db(
            registro.get(campo, "")
        )

        if valor:
            partes.append(
                f"{campo}: {valor}"
            )

    return " | ".join(partes)


def embedding_a_pgvector_publicacion(embedding):
    if embedding is None:
        return None

    return (
        "["
        + ",".join(
            str(numero)
            for numero in embedding
        )
        + "]"
    )


def validar_configuracion_publicacion():
    faltantes = []

    if not DB_HOST:
        faltantes.append("DB_HOST")
    if not DB_NAME:
        faltantes.append("DB_NAME")
    if not DB_USER:
        faltantes.append("DB_USER")
    if not DB_PASSWORD:
        faltantes.append("DB_PASSWORD")
    if not OPENAI_API_KEY:
        faltantes.append("OPENAI_API_KEY")

    if faltantes:
        raise RuntimeError(
            "Faltan variables requeridas en .env: "
            + ", ".join(faltantes)
        )

    if OPENAI_EMBEDDING_DIMENSIONS <= 0:
        raise RuntimeError(
            "OPENAI_EMBEDDING_DIMENSIONS debe ser mayor a 0."
        )

    if EMBEDDING_BATCH_SIZE <= 0:
        raise RuntimeError(
            "EMBEDDING_BATCH_SIZE debe ser mayor a 0."
        )

    if INSERT_BATCH_SIZE <= 0:
        raise RuntimeError(
            "INSERT_BATCH_SIZE debe ser mayor a 0."
        )


def cargar_csv_publicacion_db():
    if not ARCHIVO_CSV.exists():
        raise FileNotFoundError(
            f"No existe el archivo: {ARCHIVO_CSV.resolve()}"
        )

    with ARCHIVO_CSV.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as archivo:
        lector = csv.DictReader(archivo)

        columnas = lector.fieldnames or []

        if columnas != [
            "pagina",
            "contenido",
            "edicion",
            "descarga",
        ]:
            raise ValueError(
                "Publicacion_DOF.csv debe contener exactamente "
                "las columnas: pagina, contenido, edicion, descarga."
            )

        registros = []

        for fila in lector:
            registros.append({
                "pagina": limpiar_texto_db(
                    fila.get("pagina", "")
                ),
                "contenido": limpiar_texto_db(
                    fila.get("contenido", "")
                ),
                "edicion": limpiar_texto_db(
                    fila.get("edicion", "")
                ),
                "descarga": limpiar_texto_db(
                    fila.get("descarga", "")
                ),
            })

    if not registros:
        raise ValueError(
            "Publicacion_DOF.csv no contiene registros."
        )

    return registros


def generar_embeddings_publicacion(client, textos):
    embeddings = [None] * len(textos)
    pendientes = []

    for indice, texto in enumerate(textos):
        texto = (texto or "").strip()

        if not texto:
            continue

        pendientes.append(
            (
                indice,
                texto[:EMBEDDING_MAX_CHARS],
            )
        )

    total = len(pendientes)

    if total == 0:
        return embeddings

    print()
    print("=" * 60)
    print("GENERANDO EMBEDDINGS PARA Publicacion_DOF")
    print("=" * 60)

    def procesar_lote(lote):
        if not lote:
            return

        indices = [item[0] for item in lote]
        textos_lote = [item[1] for item in lote]

        try:
            response = client.embeddings.create(
                model=OPENAI_EMBEDDING_MODEL,
                input=textos_lote,
                dimensions=OPENAI_EMBEDDING_DIMENSIONS,
            )
        except Exception as exc:
            mensaje_error = str(exc).lower()
            excede_limite_tokens = (
                "maximum request size is 300000 tokens per request"
                in mensaje_error
            )

            if not excede_limite_tokens:
                raise

            if len(lote) == 1:
                raise RuntimeError(
                    "Un texto individual excede el límite de tokens "
                    "permitido por OpenAI para embeddings."
                ) from exc

            mitad = len(lote) // 2
            print(
                f"Lote de {len(lote)} textos excedió el límite de tokens; "
                "dividiendo automáticamente."
            )
            procesar_lote(lote[:mitad])
            procesar_lote(lote[mitad:])
            return

        if len(response.data) != len(textos_lote):
            raise RuntimeError(
                "OpenAI devolvió un número inesperado "
                "de embeddings."
            )

        for posicion, item in enumerate(response.data):
            if len(item.embedding) != OPENAI_EMBEDDING_DIMENSIONS:
                raise RuntimeError(
                    "El embedding generado no tiene "
                    "la dimensión esperada."
                )

            embeddings[indices[posicion]] = item.embedding

    procesados = 0

    for inicio in range(0, total, EMBEDDING_BATCH_SIZE):
        lote = pendientes[inicio:inicio + EMBEDDING_BATCH_SIZE]
        procesar_lote(lote)
        procesados += len(lote)
        print(f"Embeddings: {procesados:,}/{total:,}")

    return embeddings

def actualizar_tabla_publicacion_dof():
    """
    Crea o reemplaza el contenido de public."Publicacion_DOF"
    usando CSV/Publicacion_DOF.csv.

    Estructura funcional:
      pagina     TEXT
      contenido  TEXT
      edicion    TEXT
      descarga   TEXT
      Clave      TEXT
      Embedding  vector(...)

    La Clave y el embedding se construyen con pagina + contenido.
    Si la tabla existe, se vacía completamente antes de insertar
    la publicación actual.
    """

    global Nueva_Tabla

    Nueva_Tabla = "No"

    if NUEVO_CSV != "Yes":
        print()
        print(
            "NUEVO_CSV != Yes. "
            "No se actualizará Publicacion_DOF."
        )
        return "No"

    validar_configuracion_publicacion()
    registros = cargar_csv_publicacion_db()

    claves = [
        generar_clave_publicacion(registro)
        for registro in registros
    ]

    print()
    print("=" * 60)
    print("VALIDANDO CAMBIO EN Publicacion_DOF")
    print("=" * 60)

    conn = psycopg2.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        port=DB_PORT,
        sslmode=DB_SSLMODE,
    )

    cur = conn.cursor()

    try:
        # --------------------------------------------------------
        # COMPARAR LA PUBLICACIÓN NUEVA CONTRA Publicacion_DOF
        # --------------------------------------------------------
        # La tabla PostgreSQL es la fuente de verdad para decidir si
        # deben procesarse alertas. Se comparan únicamente los datos
        # que identifican el contenido de la publicación; no se usan
        # archivos locales ni alertas previamente enviadas como control.

        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = %s
            )
            """,
            (TABLA_PUBLICACION_DOF,),
        )

        tabla_existia = bool(cur.fetchone()[0])

        publicacion_actual = []

        if tabla_existia:
            cur.execute(
                sql.SQL(
                    "SELECT {}, {}, {} FROM {} "
                    "ORDER BY CASE WHEN {} ~ '^[0-9]+$' "
                    "THEN {}::INTEGER ELSE 2147483647 END, {}"
                ).format(
                    sql.Identifier("pagina"),
                    sql.Identifier("contenido"),
                    sql.Identifier("edicion"),
                    sql.Identifier(TABLA_PUBLICACION_DOF),
                    sql.Identifier("pagina"),
                    sql.Identifier("pagina"),
                    sql.Identifier("pagina"),
                )
            )

            publicacion_actual = [
                (
                    limpiar_texto_db(fila[0]),
                    limpiar_texto_db(fila[1]),
                    limpiar_texto_db(fila[2]),
                )
                for fila in cur.fetchall()
            ]

        publicacion_nueva = [
            (
                limpiar_texto_db(registro.get("pagina", "")),
                limpiar_texto_db(registro.get("contenido", "")),
                limpiar_texto_db(registro.get("edicion", "")),
            )
            for registro in registros
        ]

        if tabla_existia and publicacion_actual == publicacion_nueva:
            print(
                "Publicacion_DOF no cambió. "
                "No se reemplazará la tabla y no se procesarán alertas."
            )
            Nueva_Tabla = "No"
            return "No"

        print(
            "Publicacion_DOF cambió o todavía no existe. "
            "Se actualizará la tabla."
        )

        client = OpenAI(
            api_key=OPENAI_API_KEY
        )

        embeddings = generar_embeddings_publicacion(
            client,
            claves,
        )

        print()
        print("=" * 60)
        print("ACTUALIZANDO TABLA Publicacion_DOF")
        print("=" * 60)

        cur.execute(
            "CREATE EXTENSION IF NOT EXISTS vector"
        )

        # --------------------------------------------------------
        # CONTAR REGISTROS ANTES DE REEMPLAZAR LA TABLA
        # --------------------------------------------------------

        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = %s
            )
            """,
            (TABLA_PUBLICACION_DOF,),
        )

        tabla_existia = bool(
            cur.fetchone()[0]
        )

        if tabla_existia:
            cur.execute(
                sql.SQL(
                    "SELECT COUNT(*) FROM {}"
                ).format(
                    sql.Identifier(
                        TABLA_PUBLICACION_DOF
                    )
                )
            )

            registros_antes = (
                cur.fetchone()[0]
            )

        else:
            registros_antes = 0

        print()
        print(
            f"Registros antes del reemplazo: "
            f"{registros_antes:,}"
        )

        # Crear tabla si todavía no existe.
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    {} TEXT,
                    {} TEXT,
                    {} TEXT,
                    {} TEXT,
                    {} TEXT,
                    {} vector({})
                )
                """
            ).format(
                sql.Identifier(TABLA_PUBLICACION_DOF),
                sql.Identifier("pagina"),
                sql.Identifier("contenido"),
                sql.Identifier("edicion"),
                sql.Identifier("descarga"),
                sql.Identifier(COLUMNA_CLAVE_PUBLICACION),
                sql.Identifier(COLUMNA_EMBEDDING_PUBLICACION),
                sql.SQL(
                    str(OPENAI_EMBEDDING_DIMENSIONS)
                ),
            )
        )

        # Garantizar las columnas técnicas si la tabla venía
        # de una versión anterior.
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
            """,
            (TABLA_PUBLICACION_DOF,),
        )

        columnas_existentes = {
            fila[0]
            for fila in cur.fetchall()
        }

        if "pagina" not in columnas_existentes:
            cur.execute(
                sql.SQL(
                    "ALTER TABLE {} ADD COLUMN {} TEXT"
                ).format(
                    sql.Identifier(TABLA_PUBLICACION_DOF),
                    sql.Identifier("pagina"),
                )
            )

        if "contenido" not in columnas_existentes:
            cur.execute(
                sql.SQL(
                    "ALTER TABLE {} ADD COLUMN {} TEXT"
                ).format(
                    sql.Identifier(TABLA_PUBLICACION_DOF),
                    sql.Identifier("contenido"),
                )
            )

        if "edicion" not in columnas_existentes:
            cur.execute(
                sql.SQL(
                    "ALTER TABLE {} ADD COLUMN {} TEXT"
                ).format(
                    sql.Identifier(TABLA_PUBLICACION_DOF),
                    sql.Identifier("edicion"),
                )
            )

        if "descarga" not in columnas_existentes:
            cur.execute(
                sql.SQL(
                    "ALTER TABLE {} ADD COLUMN {} TEXT"
                ).format(
                    sql.Identifier(TABLA_PUBLICACION_DOF),
                    sql.Identifier("descarga"),
                )
            )

        if COLUMNA_CLAVE_PUBLICACION not in columnas_existentes:
            cur.execute(
                sql.SQL(
                    "ALTER TABLE {} ADD COLUMN {} TEXT"
                ).format(
                    sql.Identifier(TABLA_PUBLICACION_DOF),
                    sql.Identifier(COLUMNA_CLAVE_PUBLICACION),
                )
            )

        if COLUMNA_EMBEDDING_PUBLICACION not in columnas_existentes:
            cur.execute(
                sql.SQL(
                    "ALTER TABLE {} ADD COLUMN {} vector({})"
                ).format(
                    sql.Identifier(TABLA_PUBLICACION_DOF),
                    sql.Identifier(COLUMNA_EMBEDDING_PUBLICACION),
                    sql.SQL(
                        str(OPENAI_EMBEDDING_DIMENSIONS)
                    ),
                )
            )

        # Reemplazo total solicitado.
        cur.execute(
            sql.SQL(
                "TRUNCATE TABLE {}"
            ).format(
                sql.Identifier(TABLA_PUBLICACION_DOF)
            )
        )

        columnas_insertar = [
            "pagina",
            "contenido",
            "edicion",
            "descarga",
            COLUMNA_CLAVE_PUBLICACION,
            COLUMNA_EMBEDDING_PUBLICACION,
        ]

        columnas_sql = sql.SQL(", ").join(
            sql.Identifier(columna)
            for columna in columnas_insertar
        )

        consulta = sql.SQL(
            """
            INSERT INTO {} ({})
            VALUES %s
            """
        ).format(
            sql.Identifier(TABLA_PUBLICACION_DOF),
            columnas_sql,
        )

        datos = []

        for indice, registro in enumerate(registros):
            datos.append(
                (
                    registro["pagina"],
                    registro["contenido"],
                    registro["edicion"],
                    registro["descarga"],
                    claves[indice],
                    embedding_a_pgvector_publicacion(
                        embeddings[indice]
                    ),
                )
            )

        execute_values(
            cur,
            consulta.as_string(conn),
            datos,
            page_size=INSERT_BATCH_SIZE,
        )

        cur.execute(
            sql.SQL(
                """
                CREATE INDEX IF NOT EXISTS {}
                ON {}
                USING hnsw (
                    {} vector_cosine_ops
                )
                """
            ).format(
                sql.Identifier(
                    INDEX_EMBEDDING_PUBLICACION
                ),
                sql.Identifier(
                    TABLA_PUBLICACION_DOF
                ),
                sql.Identifier(
                    COLUMNA_EMBEDDING_PUBLICACION
                ),
            )
        )

        cur.execute(
            sql.SQL(
                "ANALYZE {}"
            ).format(
                sql.Identifier(
                    TABLA_PUBLICACION_DOF
                )
            )
        )

        conn.commit()

        cur.execute(
            sql.SQL(
                "SELECT COUNT(*) FROM {}"
            ).format(
                sql.Identifier(
                    TABLA_PUBLICACION_DOF
                )
            )
        )

        registros_despues = (
            cur.fetchone()[0]
        )

        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
            ORDER BY ordinal_position
            """,
            (TABLA_PUBLICACION_DOF,),
        )

        columnas_tabla_final = [
            fila[0]
            for fila in cur.fetchall()
        ]

        Nueva_Tabla = "Yes"

        print()
        print("=" * 60)
        print("RESULTADO ACTUALIZACIÓN Publicacion_DOF")
        print("=" * 60)
        print(
            f"Registros antes:   "
            f"{registros_antes:,}"
        )
        print(
            f"Registros después: "
            f"{registros_despues:,}"
        )
        print(
            f"Registros CSV:     "
            f"{len(registros):,}"
        )
        print(
            f"Columnas tabla:    "
            f"{len(columnas_tabla_final)}"
        )
        print(
            "Nombres columnas:  "
            + ", ".join(columnas_tabla_final)
        )

        if registros_despues == len(registros):
            print(
                "Validación: OK - la tabla contiene "
                "el mismo número de registros que el CSV."
            )
        else:
            print(
                "Validación: ADVERTENCIA - el número "
                "de registros de la tabla no coincide "
                "con el CSV."
            )

        print(
            f'Estado: tabla "{TABLA_PUBLICACION_DOF}" '
            f"reemplazada correctamente."
        )
        print(
            f"Índice HNSW: "
            f"{INDEX_EMBEDDING_PUBLICACION}"
        )
        print(
            f"Nueva_Tabla      = {Nueva_Tabla}"
        )
        print("=" * 60)

        return "Yes"

    except Exception:
        conn.rollback()
        Nueva_Tabla = "No"
        raise

    finally:
        cur.close()
        conn.close()


# ============================================================
# PROGRAMA PRINCIPAL
# ============================================================

def ejecutar_tabla_publicacion_dof():

    global NUEVO_MATUTINO
    global NUEVO_VESPERTINO
    global NUEVO_CSV
    global Nueva_Tabla

    print("=" * 60)
    print("MONITOR DOF")
    print("=" * 60)
    print(f"Fecha actual: {datetime.now().strftime('%d/%m/%Y')}")
    print(f"Días atrás: {DIAS_ATRAS}")
    print(f"Fecha de consulta: {FECHA_CONSULTA}")
    print(f"Carpeta: {CARPETA_PUBLICACION.resolve()}")

    try:
        CARPETA_PUBLICACION.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print()
        print(f"ERROR creando carpeta Publicacion: {error}")
        return

    # Guardar el estado inicial para saber si la publicación seleccionada
    # ya existía antes de esta ejecución.
    archivos_antes = {
        archivo.resolve()
        for archivo in CARPETA_PUBLICACION.glob("*.pdf")
    }

    # Consultar MAT.
    try:
        NUEVO_MATUTINO = procesar_edicion(FECHA_CONSULTA, "MAT")
    except Exception as error:
        print()
        print("ERROR INESPERADO procesando MAT:")
        print(repr(error))
        NUEVO_MATUTINO = "No"

    # Consultar VES. Si existe, tendrá prioridad sobre MAT.
    try:
        NUEVO_VESPERTINO = procesar_edicion(FECHA_CONSULTA, "VES")
    except Exception as error:
        print()
        print("ERROR INESPERADO procesando VES:")
        print(repr(error))
        NUEVO_VESPERTINO = "No"

    print()
    print("=" * 60)
    print("RESULTADOS DESCARGA")
    print("=" * 60)
    print(f"NUEVO_MATUTINO   = {NUEVO_MATUTINO}")
    print(f"NUEVO_VESPERTINO = {NUEVO_VESPERTINO}")

    # Elegir una sola publicación. VES > MAT para la misma fecha.
    publicacion = seleccionar_publicacion_mas_reciente(FECHA_CONSULTA)

    if not publicacion:
        NUEVO_CSV = "No"
        Nueva_Tabla = "No"
        print(f"NUEVO_CSV        = {NUEVO_CSV}")
        print(f"Nueva_Tabla      = {Nueva_Tabla}")
        return

    archivo_seleccionado = publicacion["archivo"].resolve()

    # Se regenera CSV/tabla únicamente cuando la publicación elegida es
    # nueva en esta ejecución. Esto incluye MAT nueva o VES nueva.
    publicacion_nueva = archivo_seleccionado not in archivos_antes

    if publicacion_nueva:
        NUEVO_CSV = crear_csv_publicacion(publicacion)
    else:
        NUEVO_CSV = "No"
        print()
        print("La publicación más reciente ya existía antes de esta ejecución.")
        print("No se regenerará el CSV ni la tabla PostgreSQL.")

    print(f"NUEVO_CSV        = {NUEVO_CSV}")

    if NUEVO_CSV == "Yes":
        Nueva_Tabla = actualizar_tabla_publicacion_dof()
    else:
        Nueva_Tabla = "No"

    print(f"Nueva_Tabla      = {Nueva_Tabla}")

# ============================================================
# IMPORTACIONES
# ============================================================

import hashlib
import json
import os
import logging
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import psycopg2

from dotenv import load_dotenv
from openai import (
    OpenAI,
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from psycopg2.extras import RealDictCursor

from Tabla_Conversaciones import agregar_mensaje_proactivo
from WhatsApp import (
    enviar_alerta_template,
    enviar_alerta_template_por_cantidad,
    notificar_administrador,
)


# ============================================================
# RUTAS
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent

ENV_SCRIPT = SCRIPT_DIR / ".env"


# ============================================================
# CARGAR .ENV
# ============================================================

if ENV_SCRIPT.exists():
    load_dotenv(ENV_SCRIPT)
else:
    load_dotenv()


# ============================================================
# VARIABLES DE ENTORNO
# ============================================================

DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_SSLMODE = os.getenv("DB_SSLMODE", "require")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

OPENAI_MODEL = os.getenv("MODEL_OPENAI")

OPENAI_EMBEDDING_MODEL = os.getenv(
    "OPENAI_EMBEDDING_MODEL",
    "text-embedding-3-small",
)

OPENAI_EMBEDDING_DIMENSIONS = int(
    os.getenv(
        "OPENAI_EMBEDDING_DIMENSIONS",
        "1536",
    )
)

CANDIDATOS_ALERTA = int(
    os.getenv(
        "CANDIDATOS_ALERTA",
        "20",
    )
)

RESULTADOS_ALERTA = int(
    os.getenv(
        "RESULTADOS_ALERTA",
        "8",
    )
)

OPENAI_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT", "90"))
OPENAI_MAX_RETRIES = int(os.getenv("OPENAI_MAX_RETRIES", "3"))
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "15"))
DB_STATEMENT_TIMEOUT_MS = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "45000"))
MAX_WORKERS_ALERTAS = max(
    1,
    min(5, int(os.getenv("MAX_WORKERS_ALERTAS", "5"))),
)

LOG_FILE = SCRIPT_DIR / "Alerta.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    encoding="utf-8",
)

logger = logging.getLogger("Alerta")


# ============================================================
# TABLAS
# ============================================================

TABLA_USUARIOS = "Usuarios_DOF"
TABLA_PUBLICACION = "Publicacion_DOF"

COLUMNA_EMBEDDING = "Embedding"


# ============================================================
# VALIDACIÓN
# ============================================================

def validar_configuracion():

    faltantes = []

    if not DB_HOST:
        faltantes.append("DB_HOST")

    if not DB_NAME:
        faltantes.append("DB_NAME")

    if not DB_USER:
        faltantes.append("DB_USER")

    if not DB_PASSWORD:
        faltantes.append("DB_PASSWORD")

    if not OPENAI_API_KEY:
        faltantes.append("OPENAI_API_KEY")

    if not OPENAI_MODEL:
        faltantes.append("MODEL_OPENAI")

    if faltantes:
        raise RuntimeError(
            "Faltan variables requeridas en .env: "
            + ", ".join(faltantes)
        )


# ============================================================
# OPENAI
# ============================================================

client = OpenAI(
    api_key=OPENAI_API_KEY,
    timeout=OPENAI_TIMEOUT,
    max_retries=OPENAI_MAX_RETRIES,
)


# ============================================================
# POSTGRESQL
# ============================================================

def conectar_db():

    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            port=DB_PORT,
            sslmode=DB_SSLMODE,
            connect_timeout=DB_CONNECT_TIMEOUT,
            application_name="Monitor_DOF_Alerta",
        )

        with conn.cursor() as cur:
            cur.execute(
                "SET statement_timeout = %s",
                (DB_STATEMENT_TIMEOUT_MS,),
            )

        return conn

    except Exception as error:
        logger.exception("Error de conexión PostgreSQL")
        raise RuntimeError(
            f"PostgreSQL | {type(error).__name__} | {error}"
        ) from error


def ejecutar_openai(funcion, etapa, **kwargs):

    ultimo_error = None

    for intento in range(1, OPENAI_MAX_RETRIES + 2):

        try:
            return funcion(**kwargs)

        except (
            APITimeoutError,
            APIConnectionError,
            RateLimitError,
            InternalServerError,
        ) as error:

            ultimo_error = error

            logger.exception(
                "%s | intento %s",
                etapa,
                intento,
            )

            if intento >= OPENAI_MAX_RETRIES + 1:
                break

            time.sleep(
                min(2 ** (intento - 1), 8)
            )

        except Exception as error:

            logger.exception(etapa)

            raise RuntimeError(
                f"{etapa} | "
                f"{type(error).__name__} | "
                f"{error}"
            ) from error

    raise RuntimeError(
        f"{etapa} | "
        f"{type(ultimo_error).__name__} | "
        f"{ultimo_error}"
    )


def crear_respuesta_texto(
    instrucciones,
    entrada,
    etapa,
):

    response = ejecutar_openai(
        client.responses.create,
        etapa,
        model=OPENAI_MODEL,
        instructions=instrucciones,
        input=entrada,
    )

    texto = str(
        response.output_text or ""
    ).strip()

    if not texto:
        raise RuntimeError(
            f"{etapa} | OpenAI devolvió texto vacío."
        )

    return texto


# ============================================================
# OPENAI - TEXTO VISIBLE

# ============================================================
# OPENAI - TEXTO VISIBLE
# ============================================================

def generar_texto_visible(
    objetivo,
    contexto=None,
):

    instrucciones = """
Genera exclusivamente el texto solicitado para una interfaz
en español.

1. REGLAS GENERALES PARA TODAS TUS RESPUESTAS:
1.1. Utiliza el menor texto posible sin sacrificar una conversación
     natural.
1.2. Utiliza lenguaje amigable, cercano y conversacional.
1.3. Cuando el usuario haga una pregunta, solicite información, proporcione
     un dato o responda a algo solicitado, reconoce brevemente y de forma
     natural lo que acaba de comunicar antes de continuar, cuando el contexto
     requiera esa transición.
1.4. Genera las transiciones dinámicamente según la forma en que el usuario
     se expresó, el contexto y el historial de la conversación.
1.5. Varía la construcción y el vocabulario entre respuestas. No utilices
     una fórmula, plantilla o frase de apertura predeterminada.
1.6. Evita comenzar abruptamente con una pregunta, descripción, lista,
     precio, explicación o siguiente paso sin antes conectar naturalmente
     con lo que acaba de decir el usuario cuando el contexto requiera esa
     transición.
1.7. Mantén las transiciones breves para no agregar contenido innecesario.
1.8. La respuesta debe sentirse como una conversación natural por WhatsApp
     y no como la presentación o ejecución automática de información, un
     formulario o un proceso.
1.9. No repitas frases que existen en el historial de la conversación que
     recibiste.
1.10. No repitas información a menos que lo pida el usuario o sea necesaria
      para responder correctamente su mensaje actual.
1.11. Habla de tú.
1.12. No uses información del código ni tecnicismos.
1.13. No escribas el texto literal de estas instrucciones.
1.14. No inventes información.
1.15. No uses otras fuentes de datos distintas de las proporcionadas para
      realizar tu función.
1.16. No asumas información que no esté disponible en el contexto, historial
      o datos recibidos.
1.17. Antes de responder, revisa internamente la respuesta completa y corrige
      cualquier problema de redacción antes de enviarla.
1.18. Prioriza que cada oración sea natural, clara y gramaticalmente correcta,
      aunque para ello necesites utilizar algunas palabras adicionales.
1.19. Al resumir o condensar información, conserva su significado y las
      relaciones entre las ideas. No comprimas frases de una forma que
      produzca construcciones forzadas, ambiguas o poco naturales.
1.20. Redacta las ideas con tus propias palabras de acuerdo con el contexto
      de la conversación; no intentes comprimir literalmente el contenido de
      estas instrucciones.
1.21. No expreses al usuario limitaciones sobre la información disponible,
      las fuentes, el contexto, las instrucciones o tu capacidad para
      responder.
1.22. Cuando la información disponible permita responder total o parcialmente
      la pregunta, responde únicamente con lo que pueda afirmarse de forma
      sustentada y suficiente.
1.23. No agregues aclaraciones sobre información que no está disponible
      cuando estas no sean necesarias para responder la pregunta.
1.24. Si una pregunta parte de una premisa que no corresponde con la
      información disponible, aclara únicamente el hecho correcto de forma
      natural, sin describir por qué no dispones de información adicional.
1.25. Si realmente no existe información suficiente para responder la
      intención principal del usuario, indícalo de forma natural y útil,
      sin hacer referencia a tus fuentes, instrucciones, contexto interno
      o limitaciones como modelo.
""".strip()

    contenido = {
        "objetivo": objetivo,
        "contexto": contexto or {},
    }

    return crear_respuesta_texto(
        instrucciones,
        json.dumps(
            contenido,
            ensure_ascii=False,
            default=str,
        ),
        "texto_visible",
    )




# ============================================================
# EJECUTAR PUBLICACIÓN DOF INTEGRADA
# ============================================================

def ejecutar_tabla_publicacion():
    ejecutar_tabla_publicacion_dof()
    return Nueva_Tabla

# ============================================================
# OBTENER USUARIOS
# ============================================================

def obtener_usuarios():

    conn = conectar_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                f'''
                SELECT
                    "telefono",
                    "alerta1",
                    "alerta2",
                    "alerta3",
                    "alerta4",
                    "alerta5"
                FROM "{TABLA_USUARIOS}"
                WHERE "status" = 'activo'
                ORDER BY "telefono"
                '''
            )

            return [
                dict(row)
                for row in cur.fetchall()
            ]

    finally:
        conn.close()


# ============================================================
# HUELLA DE LA PUBLICACIÓN
# ============================================================

def obtener_huella_publicacion():

    conn = conectar_db()

    try:

        with conn.cursor() as cur:

            cur.execute(
                f'''
                SELECT
                    "pagina",
                    "contenido",
                    "edicion",
                    "descarga"
                FROM "{TABLA_PUBLICACION}"
                ORDER BY
                    "edicion",
                    "pagina",
                    "descarga",
                    "contenido"
                '''
            )

            hash_publicacion = hashlib.sha256()

            total = 0

            for fila in cur:

                total += 1

                registro = json.dumps(
                    [
                        str(valor or "")
                        for valor in fila
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

                hash_publicacion.update(
                    registro.encode("utf-8")
                )

            if total == 0:
                return None

            return hash_publicacion.hexdigest()

    finally:
        conn.close()


# ============================================================
# VECTOR POSTGRESQL
# ============================================================

def vector_to_pg(vector):

    return (
        "["
        + ",".join(
            str(valor)
            for valor in vector
        )
        + "]"
    )


# ============================================================
# EMBEDDING
# ============================================================

def crear_embedding(texto):

    response = ejecutar_openai(
        client.embeddings.create,
        "embedding",
        model=OPENAI_EMBEDDING_MODEL,
        input=[texto],
        dimensions=OPENAI_EMBEDDING_DIMENSIONS,
    )

    if not response.data:
        raise RuntimeError(
            "embedding | OpenAI no devolvió embedding."
        )

    return response.data[0].embedding


# ============================================================
# PREPARAR CONSULTA SEMÁNTICA

# ============================================================
# PREPARAR CONSULTA SEMÁNTICA
# ============================================================

def preparar_consulta_semantica(
    texto_usuario,
):

    instrucciones = """
Analiza el texto recibido y genera una representación semántica
breve y fiel para buscar información relacionada dentro de una
publicación del Diario Oficial de la Federación.

Reglas:

1. Conserva nombres, instituciones, materias, conceptos,
   actividades, obligaciones, derechos, procedimientos,
   disposiciones y demás elementos relevantes.
2. Comprende sinónimos y expresiones equivalentes.
3. No respondas la consulta.
4. No agregues hechos no expresados.
5. No uses conocimiento externo.
6. Devuelve únicamente la representación semántica.
""".strip()

    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=instrucciones,
        input=texto_usuario,
    )

    consulta = str(
        response.output_text or ""
    ).strip()

    if not consulta:
        raise RuntimeError(
            "No fue posible preparar la búsqueda."
        )

    return consulta


# ============================================================
# BÚSQUEDA VECTORIAL
# ============================================================

def buscar_publicacion(
    consulta_semantica,
    exhaustiva=False,
):
    embedding = crear_embedding(
        consulta_semantica
    )

    vector_pg = vector_to_pg(
        embedding
    )

    conn = conectar_db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:
            if exhaustiva:
                # Escaneo exacto de toda la tabla, sin HNSW ni LIMIT.
                # Incluye páginas sin embedding para no omitir contenido.
                cur.execute(
                    f'''
                    SELECT
                        "pagina",
                        "contenido",
                        "edicion",
                        "descarga",
                        CASE
                            WHEN "{COLUMNA_EMBEDDING}" IS NOT NULL
                            THEN 1 - (
                                "{COLUMNA_EMBEDDING}" <=> %s::vector
                            )
                            ELSE NULL
                        END AS similitud
                    FROM "{TABLA_PUBLICACION}"
                    ORDER BY similitud DESC NULLS LAST, "pagina"
                    ''',
                    (vector_pg,),
                )
            else:
                # Comportamiento original de las alertas: sin cambios.
                cur.execute(
                    f'''
                    SELECT
                        "pagina",
                        "contenido",
                        "edicion",
                        "descarga",
                        1 - (
                            "{COLUMNA_EMBEDDING}"
                            <=> %s::vector
                        ) AS similitud
                    FROM "{TABLA_PUBLICACION}"
                    WHERE "{COLUMNA_EMBEDDING}" IS NOT NULL
                    ORDER BY
                        "{COLUMNA_EMBEDDING}"
                        <=> %s::vector
                    LIMIT %s
                    ''',
                    (
                        vector_pg,
                        vector_pg,
                        CANDIDATOS_ALERTA,
                    ),
                )

            return [
                dict(row)
                for row in cur.fetchall()
            ]
    finally:
        conn.close()


# ============================================================
# ANALIZAR UNA ALERTA
# ============================================================

def analizar_alerta(
    alerta,
):

    consulta_semantica = preparar_consulta_semantica(
        alerta
    )

    candidatos = buscar_publicacion(
        consulta_semantica
    )

    candidatos = candidatos[
        :RESULTADOS_ALERTA
    ]

    instrucciones = """
Analiza una alerta definida por un usuario y los registros
recuperados exclusivamente de la publicación actual del
Diario Oficial de la Federación.

Tu trabajo es determinar si los registros contienen información
realmente relevante para la alerta.

Reglas:

1. No consideres una coincidencia relevante solamente porque
   exista similitud semántica.
2. Debe existir una relación sustantiva entre la alerta y el
   contenido recuperado.
3. Usa exclusivamente los registros proporcionados.
4. No uses conocimiento externo.
5. No inventes información.
6. Si no existe información suficientemente relacionada,
   devuelve relevante=false.
7. Si existe información relevante, genera un resumen muy breve.
8. Incluye todas las páginas necesarias que sustentan el resumen.
9. Conserva la edición correspondiente cuando resulte necesaria
   para identificar correctamente la página.
10. No menciones similitud, embeddings ni detalles técnicos.

Devuelve exclusivamente JSON válido con esta estructura:

{
  "relevante": true o false,
  "resumen": "texto",
  "referencias": [
    {
      "pagina": "número",
      "edicion": "texto"
    }
  ]
}
""".strip()

    entrada = {
        "alerta": alerta,
        "registros": candidatos,
    }

    texto = crear_respuesta_texto(
        instrucciones,
        json.dumps(
            entrada,
            ensure_ascii=False,
            default=str,
        ),
        "analizar_alerta",
    )

    texto = texto.replace(
        "```json",
        ""
    ).replace(
        "```",
        ""
    ).strip()

    try:
        resultado = json.loads(
            texto
        )
    except Exception:
        raise RuntimeError(
            "OpenAI no devolvió JSON válido "
            "al analizar una alerta."
        )

    if not resultado.get(
        "relevante",
        False,
    ):
        return None

    resumen = str(
        resultado.get(
            "resumen",
            "",
        )
    ).strip()

    referencias = resultado.get(
        "referencias",
        [],
    )

    if not resumen:
        return None

    return {
        "alerta": alerta,
        "resumen": resumen,
        "referencias": referencias,
    }


# ============================================================
# GENERAR MENSAJE CONSOLIDADO
# ============================================================

def generar_mensaje_usuario(
    telefono,
    resultados,
):

    # El encabezado y el cierre viven en la plantilla Utility alerta_dof.
    # Aquí construimos exclusivamente el contenido dinámico de {{1}}.
    lineas = []

    for indice, resultado in enumerate(resultados):

        numero_alerta = resultado.get(
            "numero_alerta"
        )

        alerta_original = str(
            resultado.get(
                "alerta",
                "",
            )
            or ""
        ).strip()

        resumen = str(
            resultado.get(
                "resumen",
                "",
            )
            or ""
        ).strip()

        referencias = (
            resultado.get(
                "referencias",
                [],
            )
            or []
        )

        lineas.append(
            f'Alerta {numero_alerta}: "{alerta_original}"'
        )

        if resumen:
            lineas.append(
                f"Resumen: {resumen}"
            )

        referencias_agrupadas = {}

        for referencia in referencias:

            pagina = str(
                referencia.get(
                    "pagina",
                    "",
                )
                or ""
            ).strip()

            edicion = str(
                referencia.get(
                    "edicion",
                    "",
                )
                or ""
            ).strip()

            if not pagina:
                continue

            clave_edicion = (
                edicion
                if edicion
                else "Sin edición"
            )

            referencias_agrupadas.setdefault(
                clave_edicion,
                [],
            )

            if pagina not in referencias_agrupadas[
                clave_edicion
            ]:
                referencias_agrupadas[
                    clave_edicion
                ].append(
                    pagina
                )

        referencias_texto = []

        for edicion, paginas in referencias_agrupadas.items():

            etiqueta_paginas = (
                "Página"
                if len(paginas) == 1
                else "Páginas"
            )

            paginas_texto = ", ".join(
                paginas
            )

            if edicion == "Sin edición":
                referencias_texto.append(
                    f"{etiqueta_paginas} {paginas_texto}"
                )
            else:
                referencias_texto.append(
                    f"{etiqueta_paginas} {paginas_texto}, "
                    f"edición {edicion}"
                )

        if referencias_texto:
            lineas.append(
                f"Referencia: {FECHA_CONSULTA}, "
                + "; ".join(
                    referencias_texto
                )
                + "."
            )

        if indice < len(resultados) - 1:
            lineas.append("")

    return "\n".join(
        lineas
    ).strip()


# ============================================================
# PROCESAR USUARIOS
# ============================================================

def procesar_alertas_usuario(usuario):

    telefono = str(
        usuario.get(
            "telefono",
            "",
        )
        or ""
    ).strip()

    alertas = []

    for numero in range(1, 6):

        alerta = str(
            usuario.get(
                f"alerta{numero}",
                "",
            )
            or ""
        ).strip()

        if alerta:
            alertas.append(
                (numero, alerta)
            )

    resultados_por_numero = {}
    errores = []

    if not telefono or not alertas:
        return telefono, [], errores

    with ThreadPoolExecutor(
        max_workers=min(
            MAX_WORKERS_ALERTAS,
            len(alertas),
        )
    ) as executor:

        futuros = {
            executor.submit(
                analizar_alerta,
                alerta,
            ): (numero, alerta)
            for numero, alerta in alertas
        }

        for futuro in as_completed(futuros):

            numero, alerta = futuros[futuro]

            try:
                resultado = futuro.result()

                if resultado:
                    resultado["numero_alerta"] = numero

                    resultados_por_numero[
                        numero
                    ] = resultado

            except Exception as error:

                detalle = {
                    "telefono": telefono,
                    "numero_alerta": numero,
                    "alerta": alerta,
                    "tipo": type(error).__name__,
                    "error": str(error),
                }

                errores.append(detalle)

                logger.error(
                    json.dumps(
                        detalle,
                        ensure_ascii=False,
                        default=str,
                    )
                )

    resultados = [
        resultados_por_numero[numero]
        for numero
        in sorted(resultados_por_numero)
    ]

    return (
        telefono,
        resultados,
        errores,
    )


def procesar_usuarios_incremental():

    usuarios = obtener_usuarios()

    huella = obtener_huella_publicacion()

    if not huella:
        raise RuntimeError(
            "Publicacion_DOF no contiene registros."
        )

    for indice, usuario in enumerate(
        usuarios,
        start=1,
    ):

        telefono = str(
            usuario.get(
                "telefono",
                "",
            )
            or ""
        ).strip()

        try:

            (
                telefono,
                resultados,
                errores,
            ) = procesar_alertas_usuario(
                usuario
            )

            if resultados:

                mensaje = generar_mensaje_usuario(
                    telefono,
                    resultados,
                )

                # La alerta es proactiva; se envía mediante la plantilla Utility
                # aprobada en Meta, incluso fuera de la ventana de 24 horas.
                # PLANTILLA ACTUAL APROBADA.
                # Cuando alerta_dof_1 ... alerta_dof_5 estén aprobadas,
                # comenta únicamente este bloque y descomenta el bloque
                # inmediatamente inferior.
                contenidos_plantilla = [
                    generar_mensaje_usuario(telefono, [resultado])
                    for resultado in resultados
                ]
                meta_resultado = enviar_alerta_template_por_cantidad(
                    telefono=telefono,
                    contenidos=contenidos_plantilla,
                )

                # Solo registramos el mensaje en el historial después de que
                # Meta lo haya aceptado correctamente.
                agregar_mensaje_proactivo(
                    telefono=telefono,
                    contenido=mensaje,
                    metadata={
                        "tipo": "alerta_dof",
                        "huella_publicacion": huella,
                        "meta": meta_resultado,
                    },
                )

                yield {
                    "indice": indice,
                    "total": len(usuarios),
                    "telefono": telefono,
                    "huella_publicacion": huella,
                    "resultados": resultados,
                    "mensaje": mensaje,
                    "errores_alertas": errores,
                    "historial": [
                        {
                            "role": "assistant",
                            "content": mensaje,
                        }
                    ],
                }

            else:

                yield {
                    "indice": indice,
                    "total": len(usuarios),
                    "telefono": telefono,
                    "huella_publicacion": huella,
                    "resultados": [],
                    "mensaje": None,
                    "errores_alertas": errores,
                    "historial": [],
                }

        except Exception as error:

            logger.exception(
                "Error procesando teléfono %s",
                telefono,
            )

            yield {
                "indice": indice,
                "total": len(usuarios),
                "telefono": telefono,
                "huella_publicacion": huella,
                "resultados": [],
                "mensaje": None,
                "errores_alertas": [
                    {
                        "telefono": telefono,
                        "tipo": type(error).__name__,
                        "error": str(error),
                    }
                ],
                "historial": [],
            }

# ============================================================
# PROCESO PRINCIPAL DE CONSOLA
# ============================================================

def ejecutar_proceso_alerta():

    validar_configuracion()

    print("Alerta.py | iniciando proceso...")

    nueva_tabla = ejecutar_tabla_publicacion()

    print(f"Publicacion_DOF | Nueva_Tabla = {nueva_tabla}")

    if nueva_tabla != "Yes":
        print(
            "No se detectó una nueva publicación. "
            "No se procesaron alertas."
        )
        return

    usuarios = obtener_usuarios()

    print(f"Usuarios activos a procesar: {len(usuarios)}")

    mensajes_generados = 0
    errores_totales = 0

    for item in procesar_usuarios_incremental():

        telefono = item.get("telefono", "")
        indice = item.get("indice", 0)
        total = item.get("total", 0)

        errores = item.get("errores_alertas", []) or []
        errores_totales += len(errores)

        if item.get("mensaje"):
            mensajes_generados += 1
            print(
                f"[{indice}/{total}] "
                f"{telefono} | alerta registrada"
            )
        else:
            print(
                f"[{indice}/{total}] "
                f"{telefono} | sin resultados relevantes"
            )

        if errores:
            logger.warning(
                json.dumps(
                    errores,
                    ensure_ascii=False,
                    default=str,
                )
            )

    try:
        notificar_administrador(
            "MONITOR DOF | Proceso de alertas terminado. "
            f"Usuarios procesados: {len(usuarios)}. "
            f"Mensajes enviados: {mensajes_generados}. "
            f"Errores: {errores_totales}."
        )
        print("Administrador notificado por WhatsApp.")
    except Exception as error:
        logger.exception("No fue posible notificar al administrador al terminar las alertas")
        print(
            "ADVERTENCIA: no fue posible notificar al administrador: "
            f"{type(error).__name__}: {error}"
        )

    print("")
    print("Proceso terminado.")
    print(f"Mensajes registrados: {mensajes_generados}")
    print(f"Errores de alertas: {errores_totales}")
    print(f"Log: {LOG_FILE}")


# ============================================================
# PROGRAMADOR
# ============================================================

def main():
    """
    Ejecuta el proceso de alertas de forma secuencial.

    INTERVALO_MINUTOS define el tiempo entre el inicio de un ciclo y
    el inicio esperado del siguiente.

    Si el proceso termina antes del intervalo, espera el tiempo restante.
    Si el proceso tarda igual o más que el intervalo, inicia el siguiente
    ciclo inmediatamente después de terminar.

    No se crean procesos ni hilos adicionales, por lo que esta instancia
    nunca ejecuta dos ciclos de Alerta.py simultáneamente.
    """

    validar_configuracion()

    print(
        "Alerta.py | programador iniciado | "
        f"intervalo={INTERVALO_MINUTOS} minutos | "
        f"DIAS_ATRAS={DIAS_ATRAS}"
    )

    intervalo_segundos = INTERVALO_MINUTOS * 60

    while True:
        inicio_ciclo = time.monotonic()

        try:
            ejecutar_proceso_alerta()

        except KeyboardInterrupt:
            print("Alerta.py | ejecución detenida manualmente.")
            break

        except Exception as error:
            logger.exception(
                "Error no controlado en ciclo de Alerta.py"
            )
            print(
                "Alerta.py | ERROR | "
                f"{type(error).__name__}: {error}"
            )

        duracion = time.monotonic() - inicio_ciclo
        espera = max(
            0.0,
            intervalo_segundos - duracion,
        )

        if espera > 0:
            print(
                "Alerta.py | próximo ciclo en "
                f"{espera / 60:.1f} minutos."
            )

            try:
                time.sleep(espera)
            except KeyboardInterrupt:
                print(
                    "Alerta.py | ejecución detenida manualmente."
                )
                break

        else:
            print(
                "Alerta.py | el intervalo venció durante la ejecución; "
                "iniciando el siguiente ciclo inmediatamente."
            )


# ============================================================
# EJECUCIÓN
# ============================================================

if __name__ == "__main__":
    main()
