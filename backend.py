# backend.py
import os
import re
import json
import ast
import logging
import time
from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy import create_engine, text
from base_prompt import BASE_SYSTEM_PROMPT
from utils import load_txt  # moved to utils

# Logging
LOG_FILE = "llm_sql.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Load env & OpenAI client
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# DB connection factory (engine created lazily)
def get_engine():
    db_user = os.getenv("POSTGRES_USER")
    db_pass = os.getenv("POSTGRES_PASSWORD")
    db_host = os.getenv("POSTGRES_HOST", "localhost")
    db_port = os.getenv("POSTGRES_PORT", "XXXX")
    db_name = os.getenv("POSTGRES_DB")
    if not all([db_user, db_pass, db_name]):
        return None
    return create_engine(f"postgresql://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}")

# ===================== UTILIDADES ======================

def get_db_schema_text(engine):
    """Obtiene el esquema de tablas y columnas de la base."""
    if engine is None:
        return "Conexión a la DB no configurada."
    schema_info = []
    with engine.connect() as conn:
        tables = conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name"
        ))
        for (table_name,) in tables:
            cols = conn.execute(text("""
                SELECT column_name, data_type 
                FROM information_schema.columns 
                WHERE table_name = :t
                ORDER BY ordinal_position
            """), {"t": table_name})
            col_list = [f"{col} ({dtype})" for col, dtype in cols]
            schema_info.append(f"Tabla {table_name}: columnas = {', '.join(col_list)}")
    return "\n".join(schema_info)

def get_first_table(engine):
    """Obtiene el nombre de la primera tabla pública."""
    if engine is None:
        return "N/A"
    with engine.connect() as conn:
        res = conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name LIMIT 1"
        ))
        row = res.fetchone()
        return row[0] if row else "N/A"

def get_table_samples(engine, limit=5):
    """Devuelve algunas filas de ejemplo (en texto) para contextualizar al modelo."""
    t = get_first_table(engine)
    if t == "N/A" or engine is None:
        return "No se encontraron tablas en la base de datos."
    try:
        with engine.connect() as conn:
            res = conn.execute(text(f"SELECT * FROM \"{t}\" LIMIT {limit}"))
            rows = [dict(r) for r in res.mappings().all()]
            return json.dumps(rows, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Error al obtener ejemplos de datos: {str(e)}"

# ===================== CONFIGURACIÓN DE CONVERSACIÓN (factory) ======================
def build_conversation(engine):
    """Construye el system message usando estado actual de la DB y archivo instrucciones."""
    extra_context = load_txt()
    base = BASE_SYSTEM_PROMPT \
        .replace("{db_schema}", get_db_schema_text(engine)) \
        .replace("{table_name}", get_first_table(engine)) \
        .replace("{table_examples}", get_table_samples(engine))
    if extra_context:
        base = base + ("\n\n📘 Contexto adicional del archivo:\n" + extra_context)
    return [{"role": "system", "content": base}]

# ===================== FUNCIONES SQL ======================

def is_safe_sql(q: str) -> bool:
    """Valida que la query sea SELECT/ WITH (ignorando comentarios/espacios iniciales)."""
    if not q:
        return False
    # remover comentarios SQL de línea
    q_clean = re.sub(r'--.*?(\n|$)', ' ', q)
    # remover comentarios C style
    q_clean = re.sub(r'/\*.*?\*/', ' ', q_clean, flags=re.DOTALL)
    q_s = q_clean.strip().lower()
    return q_s.startswith("select") or q_s.startswith("with")

def run_sql_query(engine, query: str):
    """Ejecuta SQL si es seguro."""
    if engine is None:
        return {"error": "Conexión a la base de datos no configurada."}
    if not is_safe_sql(query):
        return {"error": "Query bloqueada: solo se permiten SELECT/WITH."}
    try:
        with engine.connect() as conn:
            res = conn.execute(text(query))
            rows = [dict(r) for r in res.mappings().all()]
            return {"rows": rows}
    except Exception as e:
        logging.exception("Error ejecutando SQL")
        return {"error": str(e)}

# ===================== FUNCIONES AUXILIARES ======================

def clean_text(text: str) -> str:
    if text is None:
        return ""
    text = re.sub(r"```(?:\w+)?", "", text)
    return text.strip()

def parse_json_decision(text: str):
    """Interpreta el JSON devuelto por el modelo."""
    text = clean_text(text)
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    json_text = m.group(0) if m else text
    try:
        return json.loads(json_text)
    except Exception:
        try:
            return ast.literal_eval(json_text)
        except Exception:
            return None

def extract_sql_queries(text: str):
    """Extrae consultas SQL de un texto generado."""
    t = clean_text(text)
    # Buscar bloques de triple backtick con sql
    blocks = re.findall(r"(?is)```(?:sql)?\s*(select.*?);?\s*```", t)
    if blocks:
        return [b.strip().rstrip(";") + ";" for b in blocks]
    selects = re.findall(r"(?is)(select\b.*?;)", t)
    if selects:
        return [s.strip() for s in selects]
    lines = re.findall(r"(?im)^select\b.*", t)
    if lines:
        return [l.strip() for l in lines]
    return []

# ===================== LLM wrapper ======================
def safe_chat_completion(model: str, messages: list, max_retries: int = 3, backoff: float = 1.0):
    """Wrapper simple con reintentos exponenciales ante fallos transitorios."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(model=model, messages=messages)
            return resp
        except Exception as e:
            last_exc = e
            logging.warning(f"Chat completion error (attempt {attempt+1}/{max_retries}): {e}")
            time.sleep(backoff * (2 ** attempt))
    logging.error("safe_chat_completion: todas las reintentos fallaron")
    raise last_exc

# Exports
__all__ = [
    "client", "get_engine", "build_conversation",
    "run_sql_query", "extract_sql_queries", "parse_json_decision",
    "clean_text", "safe_chat_completion", "LOG_FILE"
]







