#!/usr/bin/env python3
"""
Controlador por marcas de fotograma (versión unidireccional).

La reproducción de ESTA Pi (controladora) es la "partitura": cuando pasa por
ciertos fotogramas de ciertos clips, la Pi reproductora salta a otro clip
y/o a otro fotograma.
"""
import socket, json, time, threading, os

LOCAL_SOCKET = "/tmp/mpvsocket"
REMOTA_DIR = ("192.168.1.2", 5000)
FPS_POR_DEFECTO = 25

# ---------------------------------------------------------------------------
# REGLAS: (clip local, fotograma local) -> (clip remoto, fotograma remoto)
#   - Los nombres son solo el nombre del archivo, p. ej. "01_mar.mp4".
#   - clip remoto = None  -> no cambia de clip, solo salta dentro del actual.
# Edita esta tabla a tu gusto.
# ---------------------------------------------------------------------------
REGLAS = [
    ("01-a.mp4", 186, "01-x.mp4", 320),
    ("01-a.mp4", 318, "01-x.mp4", 480),
    ("01-a.mp4", 423, "01-x.mp4", 680),
    ("01-a.mp4", 555, "01-x.mp4", 1006),
    ("01-a.mp4", 853, "01-x.mp4", 1239),
    ("01-a.mp4", 1364 "01-x.mp4", 1540),
    # ("02-b.mp4", 150, None,       900),
]

# Ignora saltos grandes de fotograma (p. ej. un seek) para no disparar marcas
# que en realidad no se han "recorrido" reproduciendo.
SALTO_MAXIMO = 15


def log(*a):
    print(*a, flush=True)


class MPV:
    """Envía órdenes sueltas a un mpv (socket Unix o TCP) y devuelve la respuesta."""
    def __init__(self, nombre, familia, direccion):
        self.nombre, self.familia, self.direccion = nombre, familia, direccion

    def cmd(self, *args):
        rid = int(time.time() * 1000) % 10**9
        try:
            with socket.socket(self.familia, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                s.connect(self.direccion)
                s.sendall((json.dumps({"command": list(args), "request_id": rid}) + "\n").encode())
                buf = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        return None
                    buf += chunk
                    for linea in buf.split(b"\n"):
                        try:
                            msg = json.loads(linea)
                        except ValueError:
                            continue
                        if msg.get("request_id") == rid:
                            return msg
        except OSError:
            return None

    def get(self, propiedad):
        r = self.cmd("get_property", propiedad)
        return r.get("data") if r and r.get("error") == "success" else None


LOCAL = MPV("local", socket.AF_UNIX, LOCAL_SOCKET)
REMOTA = MPV("remota", socket.AF_INET, REMOTA_DIR)


def nombre_clip(ruta):
    return os.path.basename(ruta) if ruta else None


def esperar_clip(p, clip, timeout=4):
    """Espera a que el clip esté cargado y listo para hacer seek."""
    fin = time.time() + timeout
    while time.time() < fin:
        if nombre_clip(p.get("path")) == clip and p.get("duration"):
            return True
        time.sleep(0.1)
    return False


def mandar_a_remota(clip, fotograma):
    """Cambia (si hace falta) de clip en la reproductora y salta al fotograma."""
    try:
        if clip and nombre_clip(REMOTA.get("path")) != clip:
            lista = REMOTA.get("playlist") or []
            indices = [i for i, e in enumerate(lista) if nombre_clip(e.get("filename")) == clip]
            if not indices:
                log(f"[remota] el clip {clip} no está en su lista de reproducción")
                return
            REMOTA.cmd("playlist-play-index", indices[0])
            if not esperar_clip(REMOTA, clip):
                log(f"[remota] no se pudo cargar {clip}")
                return
        fps = REMOTA.get("container-fps") or FPS_POR_DEFECTO
        REMOTA.cmd("seek", fotograma / fps, "absolute+exact")
        REMOTA.cmd("set_property", "pause", False)
        log(f"[remota] -> {clip or '(mismo clip)'} fotograma {fotograma}")
    except Exception as e:
        log("[remota] error:", e)


def vigilar_local():
    """Observa la reproducción local y dispara las reglas al cruzar cada marca."""
    while True:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(LOCAL_SOCKET)
                for i, prop in enumerate(("path", "time-pos"), start=1):
                    s.sendall((json.dumps({"command": ["observe_property", i, prop]}) + "\n").encode())

                clip, fps, ultimo = None, None, None
                for linea in s.makefile("r"):
                    try:
                        msg = json.loads(linea)
                    except ValueError:
                        continue
                    if msg.get("event") != "property-change":
                        continue
                    dato = msg.get("data")

                    if msg["name"] == "path":
                        clip, fps, ultimo = nombre_clip(dato), None, None
                        log(f"[local] clip: {clip}")

                    elif msg["name"] == "time-pos" and dato is not None and clip:
                        if fps is None:
                            fps = LOCAL.get("container-fps") or FPS_POR_DEFECTO
                        frame = int(dato * fps)
                        if ultimo is not None and 0 < frame - ultimo <= SALTO_MAXIMO:
                            for c_loc, f_loc, c_rem, f_rem in REGLAS:
                                if c_loc == clip and ultimo < f_loc <= frame:
                                    log(f"[local] marca {clip}:{f_loc}")
                                    threading.Thread(target=mandar_a_remota,
                                                     args=(c_rem, f_rem), daemon=True).start()
                        ultimo = frame
        except OSError:
            pass
        time.sleep(2)   # mpv aún no está listo, o se perdió la conexión: reintenta


if __name__ == "__main__":
    time.sleep(10)   # deja arrancar el escritorio y mpv
    vigilar_local()
