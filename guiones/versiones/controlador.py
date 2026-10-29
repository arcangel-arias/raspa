#!/usr/bin/env python3
"""
Controlador de interacción mutua entre dos Pi.

El MISMO archivo se copia en las dos Pi y se lanza con su rol:
    python3 controlador.py controladora
    python3 controlador.py reproductora

Cada Pi vigila su propia reproducción. Cuando pasa por una marca de la tabla
REGLAS cuyo "disparador" es ella, ordena a la OTRA Pi cambiar de clip y/o
saltar a un fotograma. Cada Pi ignora las reglas del otro.
"""
import socket, json, time, threading, os, sys

LOCAL_SOCKET = "/tmp/mpvsocket"
PARES = {
    "controladora": ("192.168.1.1", 5000),
    "reproductora": ("192.168.1.2", 5000),
}
FPS_POR_DEFECTO = 25

# ---------------------------------------------------------------------------
# REGLAS (idénticas en las dos Pi):
#   (quién dispara, clip donde está la marca, fotograma de la marca,
#    clip destino en la OTRA Pi, fotograma destino)
#   - clip destino = None -> no cambia de clip, solo salta dentro del actual.
#   - Solo el nombre del archivo, p. ej. "01_a.mp4".
# ---------------------------------------------------------------------------
REGLAS = [
    ("controladora", "01_a.mp4", 100, "01_x.mp4", 0),
    ("controladora", "01_a.mp4", 300, "01_x.mp4", 450),
    ("reproductora", "01_x.mp4", 200, "01_a.mp4", 50),
    ("reproductora", "02_y.mp4", 120, None,       400),
]

SALTO_MAXIMO = 15     # ignora saltos grandes de fotograma (seeks): no cuentan como "recorridos"
PAUSA_MINIMA = 1.0    # segundos mínimos entre dos disparos de esta Pi (freno anti-bucles)

# ---------------------------------------------------------------------------
if len(sys.argv) != 2 or sys.argv[1] not in PARES:
    sys.exit("Uso: controlador.py controladora|reproductora")
YO = sys.argv[1]
OTRA = "reproductora" if YO == "controladora" else "controladora"


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
PAR = MPV(OTRA, socket.AF_INET, PARES[OTRA])
_ultimo_disparo = 0.0


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


def mandar_al_par(clip, fotograma):
    """Cambia (si hace falta) de clip en la otra Pi y salta al fotograma."""
    try:
        if clip and nombre_clip(PAR.get("path")) != clip:
            lista = PAR.get("playlist") or []
            indices = [i for i, e in enumerate(lista) if nombre_clip(e.get("filename")) == clip]
            if not indices:
                log(f"[{OTRA}] el clip {clip} no está en su lista de reproducción")
                return
            PAR.cmd("playlist-play-index", indices[0])
            if not esperar_clip(PAR, clip):
                log(f"[{OTRA}] no se pudo cargar {clip}")
                return
        fps = PAR.get("container-fps") or FPS_POR_DEFECTO
        PAR.cmd("seek", fotograma / fps, "absolute+exact")
        PAR.cmd("set_property", "pause", False)
        log(f"[{OTRA}] -> {clip or '(mismo clip)'} fotograma {fotograma}")
    except Exception as e:
        log(f"[{OTRA}] error:", e)


def disparar(clip, fotograma):
    global _ultimo_disparo
    ahora = time.time()
    if ahora - _ultimo_disparo < PAUSA_MINIMA:
        log("[freno] disparo ignorado (demasiado seguido)")
        return
    _ultimo_disparo = ahora
    threading.Thread(target=mandar_al_par, args=(clip, fotograma), daemon=True).start()


def vigilar_local():
    """Observa la reproducción local y dispara las reglas al cruzar cada marca."""
    mis_reglas = [r[1:] for r in REGLAS if r[0] == YO]
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
                        log(f"[{YO}] clip: {clip}")

                    elif msg["name"] == "time-pos" and dato is not None and clip:
                        if fps is None:
                            fps = LOCAL.get("container-fps") or FPS_POR_DEFECTO
                        frame = int(dato * fps)
                        if ultimo is not None and 0 < frame - ultimo <= SALTO_MAXIMO:
                            for c_loc, f_loc, c_dst, f_dst in mis_reglas:
                                if c_loc == clip and ultimo < f_loc <= frame:
                                    log(f"[{YO}] marca {clip}:{f_loc}")
                                    disparar(c_dst, f_dst)
                        ultimo = frame
        except OSError:
            pass
        time.sleep(2)   # mpv aún no está listo, o se perdió la conexión: reintenta


if __name__ == "__main__":
    log(f"Rol: {YO} | par: {OTRA} {PARES[OTRA]}")
    time.sleep(10)   # deja arrancar el escritorio y mpv
    vigilar_local()
