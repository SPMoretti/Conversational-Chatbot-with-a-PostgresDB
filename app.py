# app.py

import streamlit as st
import os
import json
import time
from datetime import datetime
import pandas as pd

from backend import (
    client, get_engine, build_conversation,
    clean_text, parse_json_decision, extract_sql_queries,
    run_sql_query, safe_chat_completion, LOG_FILE
)
from utils import load_txt
from rag import create_rag_store, retrieve_relevant_chunks
from base_prompt import DECIDE_INSTRUCTION as decide_instruction, BASE_SYSTEM_PROMPT

# ---------------- Streamlit setup ----------------
st.set_page_config(page_title="LLM + SQL Chat", layout="wide")
st.title("🤖 Chat con la Base de Datos")

# ---------------- Session state ----------------
if "messages" not in st.session_state:
    st.session_state["messages"] = []
if "total_tokens" not in st.session_state:
    st.session_state["total_tokens"] = 0
if "tokens_per_message" not in st.session_state:
    st.session_state["tokens_per_message"] = []

# ---------------- Engine & conversation ----------------
engine = get_engine()
db_name = os.getenv("POSTGRES_DB", "N/A")
conversation = build_conversation(engine)
first_table = "N/A" if engine is None else None
if engine:
    res = run_sql_query(engine, 
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name LIMIT 1")
    if res.get("rows"):
        first_table = res["rows"][0]["table_name"]

# ---------------- Sidebar ----------------
st.sidebar.header("🔗 Conexión")
st.sidebar.markdown(f"**Base de datos:** `{db_name}`")
st.sidebar.markdown(f"**Primera tabla:** `{first_table}`")
st.sidebar.markdown(f"**Tokens consumidos:** {st.session_state['total_tokens']}")

if st.sidebar.button("📜 Ver historial de conversación"):
    for msg in st.session_state["messages"]:
        ts = msg.get("time", "")
        role = "Usuario" if msg["role"] == "user" else "Asistente"
        tokens = msg.get("tokens", "N/A")
        st.sidebar.write(f"[{ts}] {role} ({tokens} tokens): {msg['content']}")

if st.sidebar.button("📘 Ver contexto relevante"):
    txt_content = load_txt()
    if txt_content:
        vectordb = create_rag_store(txt_content, chunk_size=1000, chunk_overlap=50)
        context = retrieve_relevant_chunks("", vectordb, top_k=5)
        st.sidebar.text_area("Contexto relevante", value=context, height=300)

# ---------------- RAG ----------------
txt_content = load_txt()
vectordb = create_rag_store(txt_content, chunk_size=1000, chunk_overlap=50) if txt_content else None

# ---------------- Función de burbujas de chat ----------------
def chat_bubble(role, content, timestamp):
    color = "#0682F5" if role == "user" else "#22F106"
    align = "right" if role == "user" else "left"
    st.markdown(f"""
        <div style="
            background-color: {color};
            padding: 10px 15px;
            border-radius: 15px;
            margin: 5px;
            max-width: 70%;
            text-align: left;
            float: {align};
            clear: both;
        ">
            {content}<br>
            <span style="font-size:0.7em; color:white;">{timestamp}</span>
        </div>
        <div style="clear:both;"></div>
    """, unsafe_allow_html=True)

# ---------------- Mostrar chat previo ----------------
for msg in st.session_state["messages"]:
    chat_bubble(msg["role"], msg["content"], msg["time"])

# ---------------- Input del usuario ----------------
user_prompt = st.chat_input("Escribe tu pregunta sobre los datos...")

if user_prompt:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_tokens = len(user_prompt.split())
    st.session_state["messages"].append({"role": "user", "content": user_prompt, "time": timestamp, "tokens": user_tokens})
    st.session_state["tokens_per_message"].append({"role": "user", "tokens": user_tokens})
    chat_bubble("user", user_prompt, timestamp)

    MAX_HISTORY = 10
    recent_history = st.session_state["messages"][-MAX_HISTORY:]

    # ---------------- Construir prompt optimizado ----------------
    # Inyectar ejemplos avanzados de SQL como contexto
    advanced_context = BASE_SYSTEM_PROMPT.replace("{db_schema}", "").replace("{table_name}", first_table).replace("{table_examples}", "")
    if vectordb:
        relevant_context = retrieve_relevant_chunks(user_prompt, vectordb)
        if relevant_context:
            advanced_context += f"\n\nInformación relevante del archivo:\n{relevant_context}"

    messages_for_model = conversation + recent_history + [
        {"role": "system", "content": advanced_context},
        {"role": "system", "content": decide_instruction}
    ]

    placeholder = st.empty()
    placeholder.markdown('<div style="font-style: italic; color: gray;">Asistente está escribiendo...</div>', unsafe_allow_html=True)

    with st.spinner("Generando respuesta..."):
        time.sleep(0.5)
        # ---------------- Paso 1: Generar decisión JSON ----------------
        try:
            dec_resp = safe_chat_completion(model="gpt-4o-mini", messages=messages_for_model)
            dec_text = dec_resp.choices[0].message.content
            used_tokens = getattr(dec_resp.usage, "total_tokens", 0)
            st.session_state["total_tokens"] += used_tokens
            st.session_state["tokens_per_message"].append({"role": "assistant", "tokens": used_tokens})
        except Exception as e:
            dec_text = f'{{"needs_sql": false, "sql": [], "notes": "error: {str(e)}"}}'

        # ---------------- Paso 2: Parsear decisión ----------------
        decision = parse_json_decision(dec_text)
        queries = []
        needs_sql = False
        if decision:
            needs_sql = bool(decision.get("needs_sql", False))
            queries = decision.get("sql") or []
            if isinstance(queries, str):
                queries = extract_sql_queries(queries)
            queries = [q.strip().rstrip(";") + ";" for q in queries if q]
        else:
            queries = extract_sql_queries(dec_text)
            needs_sql = bool(queries)

        # ---------------- Paso 3: Ejecutar SQL y generar respuesta final ----------------
        final_answer = None
        if needs_sql and queries:
            results_for_model = []
            all_errors = []
            for q in queries:
                res = run_sql_query(engine, q)
                results_for_model.append({"query": q, "result": res})
                if "error" in res:
                    all_errors.append(res["error"])

            results_json = json.dumps(results_for_model, ensure_ascii=False, default=str)

            if all_errors:
                final_answer = "⚠️ Algunas consultas fallaron:\n" + "\n".join(all_errors)
            else:
                final_instr = """Entrega UNA RESPUESTA clara y concisa basada en los resultados de SQL.
Resume solo lo que pide el usuario.
No repitas información de respuestas anteriores.
No muestres SQL ni tablas crudas."""

                final_messages = conversation + recent_history + [
                    {"role": "system", "content": final_instr},
                    {"role": "user", "content": f"Pregunta original: {user_prompt}\nResultados ejecutados: {results_json}"}
                ]

                try:
                    final_resp = safe_chat_completion(model="gpt-4o-mini", messages=final_messages)
                    final_answer = clean_text(final_resp.choices[0].message.content)
                    used_tokens = getattr(final_resp.usage, "total_tokens", 0)
                    st.session_state["total_tokens"] += used_tokens
                    st.session_state["tokens_per_message"].append({"role": "assistant", "tokens": used_tokens})
                except Exception as e:
                    final_answer = f"Error generando respuesta final: {e}"
        else:
            # ---------------- Consulta general sin SQL ----------------
            general_prompt = """
Responde solo con lenguaje natural si no se requiere SQL.
"""
            context_info = ""
            if vectordb:
                relevant_context = retrieve_relevant_chunks(user_prompt, vectordb)
                if relevant_context:
                    context_info = f"\nInformación contextual de la base:\n{relevant_context}"

            try:
                general_resp = safe_chat_completion(model="gpt-4o-mini", messages=[
                    {"role": "system", "content": general_prompt + context_info},
                    {"role": "user", "content": user_prompt}
                ])
                final_answer = clean_text(general_resp.choices[0].message.content)
                used_tokens = getattr(general_resp.usage, "total_tokens", 0)
                st.session_state["total_tokens"] += used_tokens
                st.session_state["tokens_per_message"].append({"role": "assistant", "tokens": used_tokens})
            except Exception as e:
                final_answer = f"Error generando respuesta conversacional: {e}"

        # ---------------- Mostrar respuesta ----------------
        placeholder.empty()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st.session_state["messages"].append({"role": "assistant", "content": final_answer, "time": timestamp, "tokens": used_tokens})
        chat_bubble("assistant", final_answer, timestamp)



















