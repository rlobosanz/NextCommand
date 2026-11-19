"""Parser NSML: el único sitio de NextCommand que interpreta NSML. Lector tolerante: lo desconocido se ignora.

Solo lo que necesita la escaleta de playout: cabeceras (campos y marcas) y columnas del formulario.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from .soap import local


class NsmlError(ValueError):
    pass


def _parse(nsml: str) -> ET.Element:
    try:
        return ET.fromstring(nsml.encode("utf-8") if isinstance(nsml, str) else nsml)
    except ET.ParseError as e:
        raise NsmlError(str(e)) from e


def _find(el: ET.Element | None, name: str) -> ET.Element | None:
    if el is None:
        return None
    for c in el:
        if local(c.tag) == name:
            return c
    return None


def _text(el: ET.Element | None) -> str:
    return "".join(el.itertext()) if el is not None else ""


def _bool(v: str | None) -> bool:
    return (v or "").lower() in ("true", "1", "yes")


def parse_fields(root: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    fields = _find(root, "fields")
    if fields is None:
        return out
    for f in fields:
        fid = f.get("id")
        if fid:
            out[fid] = _text(f).strip()
    return out


def parse_header(nsml: str) -> dict[str, Any]:
    """Campos y marcas (flotada, break, retenida) de una historia, con o sin cuerpo."""
    root = _parse(nsml)
    meta = _find(_find(root, "head"), "meta")
    m = meta.attrib if meta is not None else {}
    return {"fields": parse_fields(root),
            "flags": {"floated": _bool(m.get("float")), "break": _bool(m.get("break")), "held": _bool(m.get("hold"))}}


def parse_form(nsml: str) -> list[dict[str, Any]]:
    """Columnas de un formulario de cola: primera fila de celdas con idref."""
    if not nsml.strip():
        return []
    root = _parse(nsml)
    cols = []
    for row in root.iter():
        if local(row.tag) != "row":
            continue
        for cell in row:
            name = local(cell.tag)
            if not name.endswith("cell") or not cell.get("idref"):
                continue
            label_el = _find(cell, "label")
            label = (_text(label_el) if label_el is not None else (cell.text or "")).strip()
            cols.append({"field": cell.get("idref"), "label": label or cell.get("idref")})
        if cols:
            break
    return cols
