# utils.py
import os

TXT_FILE = "instrucciones.txt"

def load_txt(file_path=TXT_FILE):
    """Carga el archivo .txt de descripción de la tabla, devuelve texto limpio."""
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            # no print en producción; usar logging si es necesario
            return ""
    return ""
