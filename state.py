"""state.py — Estado persistente (posiciones abiertas, señales ya enviadas, histórico).

Se guarda como JSON atómico. En Railway monta un Volume para que sobreviva a los redeploys.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict

from strategy import DAY_MS, Position

log = logging.getLogger("qfused.state")

KEEP_SENT_MS = 4 * DAY_MS
KEEP_CLOSED = 300


class State:
    def __init__(self, path: str):
        self.path = path
        self.positions: dict = {}   # symbol -> Position (señales abiertas)
        self.sent: dict = {}        # clave -> timestamp ms (deduplicación)
        self.closed: list = []      # histórico de señales cerradas
        self.meta: dict = {}
        self.dirty = False
        self.load()

    # ------------------------------------------------------------------ IO
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:  # JSON corrupto
            log.warning("Estado ilegible (%s); empiezo vacío y guardo copia .bad", type(e).__name__)
            try:
                os.replace(self.path, self.path + ".bad")
            except OSError:
                pass
            return
        for sym, p in (d.get("positions") or {}).items():
            try:
                self.positions[sym] = Position(**p)
            except TypeError:
                log.warning("Posición %s con formato antiguo; descartada", sym)
        self.sent = d.get("sent") or {}
        self.closed = d.get("closed") or []
        self.meta = d.get("meta") or {}
        log.info("Estado cargado: %d posiciones abiertas, %d claves, %d cerradas",
                 len(self.positions), len(self.sent), len(self.closed))

    def save(self):
        data = {"positions": {s: asdict(p) for s, p in self.positions.items()},
                "sent": self.sent, "closed": self.closed[-KEEP_CLOSED:], "meta": self.meta}
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError as e:
            log.error("No pude guardar el estado: %s", e)

    # ------------------------------------------------------------ helpers
    def once(self, key: str) -> bool:
        """True la primera vez que se ve la clave (y la marca); False si ya existía."""
        if key in self.sent:
            return False
        self.sent[key] = int(time.time() * 1000)
        self.dirty = True
        return True

    def close_position(self, pos: Position, reason: str, close_ts: int):
        self.positions.pop(pos.symbol, None)
        self.closed.append({"symbol": pos.symbol, "setup": pos.setup, "side": pos.side,
                            "entry_ts": pos.entry_ts, "close_ts": close_ts,
                            "r": round(pos.realized_r, 3), "reason": reason})
        self.dirty = True

    def prune(self):
        cutoff = int(time.time() * 1000) - KEEP_SENT_MS
        old = [k for k, t in self.sent.items() if t < cutoff]
        for k in old:
            del self.sent[k]
        if old:
            self.dirty = True
