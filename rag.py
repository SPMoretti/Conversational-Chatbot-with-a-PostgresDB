# rag.py
import os
import hashlib
from langchain.text_splitter import RecursiveCharacterTextSplitter

# Try to import embeddings with backward/forward compatibility
try:
    # Newer packaging
    from langchain_openai import OpenAIEmbeddings
except Exception:
    try:
        from langchain.embeddings.openai import OpenAIEmbeddings
    except Exception:
        OpenAIEmbeddings = None

from langchain.vectorstores import Chroma

VECTOR_DIR = "vector_store"

def _get_text_hash(text: str):
    """Genera un hash del texto para detectar cambios."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def _chroma_store_exists(vector_dir: str):
    """Comprobación más robusta de persistencia de Chroma."""
    if not os.path.exists(vector_dir):
        return False
    # Chroma puede usar diferentes estructuras (index/, chroma.sqlite3, etc.)
    for name in ("chroma.sqlite3", "index", ".chromadb"):
        if os.path.exists(os.path.join(vector_dir, name)):
            return True
    # fallback: any files present
    return any(os.scandir(vector_dir))

def create_rag_store(text, vector_dir=VECTOR_DIR, chunk_size=1000, chunk_overlap=50):
    """Crea o carga el vector store de manera persistente según hash del texto."""
    if not text:
        return None

    if OpenAIEmbeddings is None:
        raise ImportError("OpenAIEmbeddings no está disponible. Revisa la versión de LangChain/langchain-openai.")

    os.makedirs(vector_dir, exist_ok=True)
    hash_file = os.path.join(vector_dir, "text_hash.txt")
    old_hash = None
    if os.path.exists(hash_file):
        try:
            with open(hash_file, "r", encoding="utf-8") as f:
                old_hash = f.read().strip()
        except Exception:
            old_hash = None

    current_hash = _get_text_hash(text)

    need_rebuild = current_hash != old_hash or not _chroma_store_exists(vector_dir)

    if need_rebuild:
        splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        chunks = splitter.split_text(text)
        embeddings = OpenAIEmbeddings()
        vectordb = Chroma.from_texts(chunks, embedding=embeddings, persist_directory=vector_dir) \
            if hasattr(Chroma, "from_texts") else Chroma.from_texts(chunks, embeddings, persist_directory=vector_dir)
        # persist may be a method or automatic depending on version
        try:
            vectordb.persist()
        except Exception:
            pass
        try:
            with open(hash_file, "w", encoding="utf-8") as f:
                f.write(current_hash)
        except Exception:
            pass
    else:
        vectordb = load_rag_store(vector_dir)

    return vectordb

def load_rag_store(vector_dir=VECTOR_DIR):
    """Carga el vector store existente si existe."""
    if OpenAIEmbeddings is None:
        raise ImportError("OpenAIEmbeddings no está disponible. Revisa la versión de LangChain/langchain-openai.")
    if not _chroma_store_exists(vector_dir):
        return None
    embeddings = OpenAIEmbeddings()
    # In some versions constructor signature is different
    try:
        vectordb = Chroma(persist_directory=vector_dir, embedding_function=embeddings)
    except Exception:
        vectordb = Chroma(persist_directory=vector_dir, embeddings=embeddings)
    return vectordb

def retrieve_relevant_chunks(query, vectordb, top_k=5):
    """Recupera los chunks más relevantes para la pregunta."""
    if not vectordb:
        return ""
    # similarity_search vs similar_documents naming may vary
    try:
        docs = vectordb.similarity_search(query, k=top_k)
    except Exception:
        docs = vectordb.similarity_search(query, top_k)
    return "\n".join(getattr(d, "page_content", str(d)) for d in docs)


