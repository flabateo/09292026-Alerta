import os
import subprocess
import sys
import threading

from fastapi import FastAPI
import uvicorn


app = FastAPI()


@app.get("/")
def health():
    return {
        "status": "ok",
        "service": "Monitor DOF Alerta",
    }


@app.get("/health")
def health_check():
    return {
        "status": "ok",
    }


def ejecutar_alerta():
    resultado = subprocess.run(
        [sys.executable, "Alerta.py"],
        check=False,
    )

    if resultado.returncode != 0:
        print(
            f"Alerta.py terminó inesperadamente "
            f"con código {resultado.returncode}",
            flush=True,
        )


if __name__ == "__main__":
    hilo_alerta = threading.Thread(
        target=ejecutar_alerta,
        daemon=True,
    )
    hilo_alerta.start()

    puerto = int(os.getenv("PORT", "8000"))

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=puerto,
    )
