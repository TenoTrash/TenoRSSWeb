#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TenoRSSWeb - lector de RSS/Atom que genera una web para leer las noticias.

Sin dependencias externas: solo la biblioteca estandar de Python 3.

Uso:
    python3 TenoRSSWeb.py                 # sirve la web en http://localhost:8080
    python3 TenoRSSWeb.py --port 9000     # otro puerto
    python3 TenoRSSWeb.py --update        # solo descarga y actualiza cache.json
    python3 TenoRSSWeb.py --build index.html   # genera un HTML estatico y sale

Archivos (en la misma carpeta que este script):
    rss.txt     fuentes a leer           (seccion [Nombre] + URL="...")
    cache.json  ultimos items por fuente, con estado leido/no leido
"""

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
RSS_FILE = os.path.join(HERE, "rss.txt")
CACHE_FILE = os.path.join(HERE, "cache.json")
BACKUP_DIR = os.path.join(HERE, "backups")  # copias de rss.txt antes de cada edicion
MEDIA_DIR = os.path.join(HERE, "media")      # imagenes cacheadas para leer offline

APP_NAME = "TenoRSSWeb"
CACHE_VERSION = 1
MAX_ITEMS = 50            # items guardados por fuente
TEXT_LIMIT = 3000         # maximo de texto visible por item (se recorta el resto)
MAX_IMG_BYTES = 5 * 1024 * 1024  # imagenes mas grandes no se cachean (se dejan remotas)
HTTP_TIMEOUT = 15         # segundos por descarga
USER_AGENT = "TenoRSSWeb/1.0 (+https://github.com/ ; RSS reader)"

# Candado para que varias peticiones del navegador no escriban cache.json a la vez.
_cache_lock = threading.Lock()


# --------------------------------------------------------------------------
# Configuracion: rss.txt
# --------------------------------------------------------------------------
def parse_rss_txt(text):
    """Devuelve (lista de {'name','url'}, tz) a partir del contenido de rss.txt."""
    feeds = []
    tz = "auto"
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = {"name": line[1:-1].strip(), "url": ""}
            feeds.append(current)
            continue
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip().upper()
        val = val.strip().strip('"').strip("'").strip()
        if key == "TZ":
            tz = val or "auto"
        elif key == "URL" and current is not None:
            current["url"] = val
    return [f for f in feeds if f["url"]], tz


def load_config():
    if not os.path.exists(RSS_FILE):
        return [], "auto"
    with open(RSS_FILE, "r", encoding="utf-8") as fh:
        return parse_rss_txt(fh.read())


def tz_offset_seconds(tz):
    """tz de rss.txt -> segundos respecto de UTC. 'auto' = hora local del equipo."""
    if tz == "auto":
        off = datetime.now(timezone.utc).astimezone().utcoffset()
        return int(off.total_seconds()) if off else 0
    try:
        return int(float(tz) * 3600)
    except ValueError:
        return 0


def build_rss_txt(feeds):
    """Lista de {'name','url'} -> contenido de rss.txt (mismo formato)."""
    out = ["# TenoRSSWeb - fuentes RSS/Atom.\n",
           "# Una seccion por feed: [Nombre] y URL=\"...\".\n",
           "# Editado desde la pagina de administracion (/fuentes).\n\n"]
    for f in feeds:
        name = (f.get("name") or f.get("url") or "").replace("[", "").replace("]", "")
        name = name.replace("\n", " ").replace("\r", " ").strip()
        url = (f.get("url") or "").replace("\n", "").replace("\r", "").replace('"', "").strip()
        if not url:
            continue
        out.append("[%s]\n" % name)
        out.append('URL="%s"\n\n' % url)
    return "".join(out).rstrip() + "\n"


def backup_rss():
    """Copia el rss.txt actual a backups/ con fecha y hora. Devuelve la ruta."""
    if not os.path.exists(RSS_FILE):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP_DIR, "rss-%s.txt" % stamp)
    n = 1
    while os.path.exists(dest):  # si se guarda dos veces en el mismo segundo
        dest = os.path.join(BACKUP_DIR, "rss-%s-%d.txt" % (stamp, n))
        n += 1
    shutil.copy2(RSS_FILE, dest)
    return dest


def save_rss_txt(feeds):
    """Hace backup del rss.txt anterior y escribe el nuevo. Devuelve la ruta del backup."""
    backup = backup_rss()
    content = build_rss_txt(feeds)
    tmp = RSS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, RSS_FILE)
    return backup


def sync_cache_to_config():
    """Ajusta cache.json a las fuentes de rss.txt sin descargar: saca las que ya
    no estan, agrega las nuevas vacias y mantiene los items de las que siguen."""
    feeds_cfg, tz = load_config()
    with _cache_lock:
        cache = load_cache()
        old = {f.get("url"): f for f in cache.get("feeds", [])}
        new = []
        for cfg in feeds_cfg:
            f = old.get(cfg["url"])
            if f:
                f["name"] = cfg["name"]  # por si lo renombraron
                new.append(f)
            else:
                new.append({"name": cfg["name"], "url": cfg["url"],
                            "status": "sin actualizar", "updated": 0, "items": []})
        cache["feeds"] = new
        cache["tz_offset"] = tz_offset_seconds(tz)
        cache["dirty"] = True   # fuentes editadas: hay que actualizar
        prune_media(cache)      # imagenes de fuentes borradas quedan huerfanas
        save_cache(cache)
    return cache


# --------------------------------------------------------------------------
# Cache: cache.json
# --------------------------------------------------------------------------
def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {"app": APP_NAME, "version": CACHE_VERSION, "saved": 0,
                "tz_offset": 0, "feeds": []}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"app": APP_NAME, "version": CACHE_VERSION, "saved": 0,
                "tz_offset": 0, "feeds": []}
    if not isinstance(data, dict) or not isinstance(data.get("feeds"), list):
        return {"app": APP_NAME, "version": CACHE_VERSION, "saved": 0,
                "tz_offset": 0, "feeds": []}
    return data


def save_cache(cache):
    cache["app"] = APP_NAME
    cache["version"] = CACHE_VERSION
    cache["saved"] = int(time.time())
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(dump_cache(cache))
    os.replace(tmp, CACHE_FILE)


def dump_cache(cache):
    """JSON legible: un item por linea (como el TenoRSS original)."""
    out = []
    out.append('{"app":%s,"version":%d,"saved":%d,"tz_offset":%d,"dirty":%d,"feeds":[' % (
        json.dumps(cache.get("app", APP_NAME)), int(cache.get("version", CACHE_VERSION)),
        int(cache.get("saved", 0)), int(cache.get("tz_offset", 0)),
        1 if cache.get("dirty") else 0))
    feed_chunks = []
    for feed in cache.get("feeds", []):
        head = '\n{"name":%s,"url":%s,"updated":%d,"status":%s,"items":[' % (
            json.dumps(feed.get("name", ""), ensure_ascii=False),
            json.dumps(feed.get("url", ""), ensure_ascii=False),
            int(feed.get("updated", 0)),
            json.dumps(feed.get("status", "ok"), ensure_ascii=False))
        item_lines = []
        for it in feed.get("items", []):
            item_lines.append('\n' + json.dumps({
                "id": it.get("id", ""),
                "read": 1 if it.get("read") else 0,
                "date": int(it.get("date", 0)),
                "title": it.get("title", ""),
                "link": it.get("link", ""),
                "text": it.get("text", ""),
            }, ensure_ascii=False))
        feed_chunks.append(head + ",".join(item_lines) + "]}")
    out.append(",".join(feed_chunks))
    out.append("\n]}\n")
    return "".join(out)


# --------------------------------------------------------------------------
# Descarga y parseo de feeds
# --------------------------------------------------------------------------
def strip_tags(s):
    """Quita etiquetas HTML y normaliza espacios, para el resumen de cada item."""
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Saneador de HTML: deja pasar solo una lista blanca de etiquetas/atributos,
# descarta scripts/estilos/etc. y recorta el contenido a max_text caracteres
# visibles cerrando bien las etiquetas abiertas. Asi el content del feed se
# puede inyectar en la pagina sin riesgo.
_HTML_ALLOWED = {
    "p", "br", "b", "strong", "i", "em", "u", "s", "strike", "small", "sub", "sup",
    "a", "ul", "ol", "li", "blockquote", "q", "cite", "code", "pre", "kbd", "samp",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "img", "figure", "figcaption",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "dl", "dt", "dd",
}
_HTML_VOID = {"br", "img", "hr"}
_HTML_DROP = {
    "script", "style", "iframe", "object", "embed", "noscript", "template",
    "svg", "math", "form", "input", "button", "textarea", "select", "link",
    "meta", "head", "title", "base", "audio", "video", "canvas",
}


def _safe_url(url, allow_proto_relative=False):
    if not url:
        return ""
    u = url.strip()
    low = u.lower()
    if low.startswith(("http://", "https://", "mailto:")):
        return u
    if allow_proto_relative and u.startswith("//"):
        return "https:" + u
    return ""


class _Sanitizer(HTMLParser):
    def __init__(self, max_text):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []
        self.budget = max_text
        self.drop_depth = 0
        self.done = False

    def _starttag(self, tag, attrs):
        clean = []
        for k, v in attrs:
            k = k.lower()
            if tag == "a" and k == "href":
                u = _safe_url(v)
                if u:
                    clean.append(("href", u))
            elif tag == "img" and k == "src":
                u = _safe_url(v, allow_proto_relative=True)
                if u:
                    clean.append(("src", u))
            elif k in ("alt", "title") and tag in ("img", "a"):
                clean.append((k, v or ""))
        if tag == "a":
            clean += [("target", "_blank"), ("rel", "noopener noreferrer nofollow")]
        elif tag == "img":
            clean.append(("loading", "lazy"))
        buf = ["<", tag]
        for k, v in clean:
            buf.append(' %s="%s"' % (k, html.escape(v, quote=True)))
        buf.append(">")
        return "".join(buf)

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        tag = tag.lower()
        if tag in _HTML_DROP:
            self.drop_depth += 1
            return
        if self.drop_depth or tag not in _HTML_ALLOWED:
            return  # etiqueta ignorada, pero su contenido se conserva
        self.out.append(self._starttag(tag, attrs))
        if tag not in _HTML_VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if self.done:
            return
        tag = tag.lower()
        if tag in _HTML_DROP or self.drop_depth or tag not in _HTML_ALLOWED:
            return
        self.out.append(self._starttag(tag, attrs))  # <img/>, <br/>: sin apilar

    def handle_endtag(self, tag):
        if self.done:
            return
        tag = tag.lower()
        if tag in _HTML_DROP:
            if self.drop_depth:
                self.drop_depth -= 1
            return
        if self.drop_depth or tag in _HTML_VOID or tag not in _HTML_ALLOWED:
            return
        if tag in self.stack:
            while self.stack:
                top = self.stack.pop()
                self.out.append("</%s>" % top)
                if top == tag:
                    break

    def handle_data(self, data):
        if self.done or self.drop_depth or not data:
            return
        if len(data) <= self.budget:
            self.budget -= len(data)
            self.out.append(html.escape(data))
        else:
            self.out.append(html.escape(data[:self.budget]))
            self.out.append("…")
            self._close_all()
            self.done = True

    def _close_all(self):
        while self.stack:
            self.out.append("</%s>" % self.stack.pop())

    def result(self):
        if not self.done:
            self._close_all()
        return "".join(self.out).strip()


def sanitize_html(raw, max_text=TEXT_LIMIT):
    """HTML del feed -> HTML seguro (lista blanca) recortado a max_text chars."""
    if not raw:
        return ""
    p = _Sanitizer(max_text)
    try:
        p.feed(raw)
        p.close()
        return p.result()
    except Exception:
        # Ante cualquier problema, texto plano recortado y escapado.
        txt = strip_tags(raw)
        if len(txt) > max_text:
            txt = txt[:max_text].rstrip() + "…"
        return html.escape(txt)


def local_tag(tag):
    """Nombre de etiqueta sin el namespace: '{http://...}entry' -> 'entry'."""
    return tag.rsplit("}", 1)[-1].lower() if tag else ""


def parse_date(s):
    """Fecha de un item (RSS o Atom) -> epoch UTC, o 0 si no se pudo."""
    if not s:
        return 0
    s = s.strip()
    try:
        dt = parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
    except (TypeError, ValueError, IndexError):
        pass
    iso = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return 0


def item_id(link, title):
    h = hashlib.sha1(((link or "") + "|" + (title or "")).encode("utf-8"))
    return h.hexdigest()[:8]


def _find_text(el, names):
    for child in el:
        if local_tag(child.tag) in names:
            # itertext() junta nodos de texto y de hijos (contenido mixto).
            return "".join(child.itertext()).strip()
    return ""


def _looks_image(url):
    u = url.split("?")[0].lower()
    return u.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif"))


def _lead_image(node):
    """Imagen destacada de un item segun Media RSS / enclosure (URL http segura)."""
    fallback = ""
    for child in node:
        tag = local_tag(child.tag)
        url = (child.get("url") or "").strip()
        if not url:
            continue
        typ = (child.get("type") or "").lower()
        medium = (child.get("medium") or "").lower()
        if tag == "content" and (medium == "image" or typ.startswith("image/") or _looks_image(url)):
            safe = _safe_url(url, allow_proto_relative=True)
            if safe:
                return safe  # media:content tiene prioridad
        elif tag in ("thumbnail", "image") or (tag == "enclosure" and typ.startswith("image/")):
            safe = _safe_url(url, allow_proto_relative=True)
            if safe and not fallback:
                fallback = safe
    return fallback


def parse_feed(xml_bytes):
    """Parsea bytes de un feed RSS 2.0 / RSS 1.0 / Atom -> lista de items."""
    root = ET.fromstring(xml_bytes)
    items = []

    # Busca tanto <item> (RSS) como <entry> (Atom) en cualquier nivel.
    nodes = [el for el in root.iter() if local_tag(el.tag) in ("item", "entry")]
    for node in nodes:
        is_atom = local_tag(node.tag) == "entry"
        title = _find_text(node, {"title"})

        # Link
        link = ""
        if is_atom:
            alt = ""
            for child in node:
                if local_tag(child.tag) != "link":
                    continue
                rel = (child.get("rel") or "alternate").lower()
                href = child.get("href") or ""
                if rel == "alternate" and href:
                    link = href
                    break
                if href and not alt:
                    alt = href
            if not link:
                link = alt
        if not link:
            link = _find_text(node, {"link", "guid", "id"})

        # Texto / resumen
        body = ""
        for pref in (("content", "encoded"), ("summary", "description"), ("subtitle",)):
            body = _find_text(node, set(pref))
            if body:
                break
        text = sanitize_html(body, TEXT_LIMIT)
        # Si el cuerpo no trae imagen, usar la destacada de Media RSS/enclosure.
        if "<img" not in text:
            lead = _lead_image(node)
            if lead:
                text = '<img src="%s" loading="lazy">%s' % (html.escape(lead, quote=True), text)

        date = parse_date(_find_text(node, {"pubdate", "published", "updated", "date"}))

        title = strip_tags(title) or "(sin titulo)"
        items.append({
            "id": item_id(link, title),
            "read": 0,
            "date": date,
            "title": title,
            "link": link.strip(),
            "text": text,
        })

    # Mas nuevo primero si todos tienen fecha.
    if items and all(it["date"] for it in items):
        items.sort(key=lambda it: it["date"], reverse=True)
    return items


def fetch_feed(url):
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


# Imagenes: cacheo local para que el lector funcione offline tras actualizar.
_IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]+)(")', re.I)
_CT_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "image/svg+xml": ".svg", "image/bmp": ".bmp", "image/avif": ".avif",
}
_EXT_CT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
           ".webp": "image/webp", ".svg": "image/svg+xml", ".bmp": "image/bmp", ".avif": "image/avif"}


def _img_ext(url, ctype):
    if ctype in _CT_EXT:
        return _CT_EXT[ctype]
    u = url.split("?")[0].lower()
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".avif"):
        if u.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ""  # sin pista de que sea imagen: no se cachea


def _fetch_image(url):
    """Descarga una imagen a MEDIA_DIR y devuelve su ruta local (/media/...), o ''."""
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        for f in os.listdir(MEDIA_DIR):   # ya cacheada
            if f.startswith(h + "."):
                return "/media/" + f
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*"})
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            data = resp.read(MAX_IMG_BYTES + 1)
        if len(data) > MAX_IMG_BYTES:
            return ""
        ext = _img_ext(url, ctype)
        if not ext:
            return ""
        fname = h + ext
        tmp = os.path.join(MEDIA_DIR, fname + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, os.path.join(MEDIA_DIR, fname))
        return "/media/" + fname
    except Exception:
        return ""


def cache_images(html_str):
    """Reescribe los <img src="http..."> a rutas locales, descargando lo que falte.
    Si una imagen falla, deja la URL remota (online igual se ve)."""
    if not html_str or "<img" not in html_str:
        return html_str

    def repl(m):
        url = m.group(2)
        low = url.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            return m.group(0)  # ya es local u otro esquema
        local = _fetch_image(url)
        return (m.group(1) + local + m.group(3)) if local else m.group(0)

    return _IMG_SRC_RE.sub(repl, html_str)


def prune_media(cache):
    """Borra de MEDIA_DIR las imagenes que ya no referencia ningun item."""
    if not os.path.isdir(MEDIA_DIR):
        return
    used = set()
    for feed in cache.get("feeds", []):
        for it in feed.get("items", []):
            for m in _IMG_SRC_RE.finditer(it.get("text", "")):
                src = m.group(2)
                if src.startswith("/media/"):
                    used.add(src[len("/media/"):])
    for f in os.listdir(MEDIA_DIR):
        if f.endswith(".tmp") or f not in used:
            try:
                os.remove(os.path.join(MEDIA_DIR, f))
            except OSError:
                pass


def merge_items(fresh, old):
    """Combina lo recien bajado con el cache, conservando el estado 'read'."""
    read_by_id = {it["id"]: it.get("read", 0) for it in old}
    seen = set()
    merged = []
    for it in fresh:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        it["read"] = read_by_id.get(it["id"], 0)
        merged.append(it)
    # Conserva items viejos que ya no vienen en el feed, hasta completar MAX_ITEMS.
    for it in old:
        if len(merged) >= MAX_ITEMS:
            break
        if it["id"] not in seen:
            seen.add(it["id"])
            merged.append(it)
    return merged[:MAX_ITEMS]


def update_all(verbose=True):
    """Descarga todas las fuentes y reescribe cache.json. Devuelve el cache."""
    feeds_cfg, tz = load_config()
    with _cache_lock:
        cache = load_cache()
        old_by_url = {f.get("url"): f for f in cache.get("feeds", [])}
        new_feeds = []
        for cfg in feeds_cfg:
            name, url = cfg["name"], cfg["url"]
            old = old_by_url.get(url, {})
            old_items = old.get("items", [])
            try:
                raw = fetch_feed(url)
                fresh = parse_feed(raw)
                items = merge_items(fresh, old_items)
                status, updated = "ok", int(time.time())
                if verbose:
                    print("  [ok]  %-20s %d items" % (name, len(items)))
            except (URLError, HTTPError, ET.ParseError, ValueError) as exc:
                items = old_items
                status = "error: %s" % (exc.reason if hasattr(exc, "reason") else exc)
                updated = old.get("updated", 0)
                if verbose:
                    print("  [err] %-20s %s" % (name, status))
            for it in items:  # cachea imagenes nuevas (las ya locales se saltean)
                it["text"] = cache_images(it.get("text", ""))
            new_feeds.append({
                "name": name, "url": url, "status": status,
                "updated": updated, "items": items,
            })
        cache["feeds"] = new_feeds
        cache["tz_offset"] = tz_offset_seconds(tz)
        cache["dirty"] = False  # recien actualizado
        prune_media(cache)      # borra imagenes que ya no usa ningun item
        save_cache(cache)
    return cache


def set_read(item_hash, read):
    """Marca un item como leido/no leido en cache.json."""
    with _cache_lock:
        cache = load_cache()
        changed = False
        for feed in cache.get("feeds", []):
            for it in feed.get("items", []):
                if it.get("id") == item_hash:
                    it["read"] = 1 if read else 0
                    changed = True
        if changed:
            save_cache(cache)
        return changed


def set_read_feed(feed_url, read):
    with _cache_lock:
        cache = load_cache()
        changed = False
        for feed in cache.get("feeds", []):
            if feed.get("url") == feed_url:
                for it in feed.get("items", []):
                    it["read"] = 1 if read else 0
                    changed = True
        if changed:
            save_cache(cache)
        return changed


# --------------------------------------------------------------------------
# Generacion de la web
# --------------------------------------------------------------------------
def human_time(epoch, tz_offset):
    if not epoch:
        return ""
    dt = datetime.fromtimestamp(epoch + tz_offset, timezone.utc)
    return dt.strftime("%d/%m/%Y %H:%M")


def e(s):
    return html.escape(s or "", quote=True)


PAGE_CSS = """
/* Claro por defecto; oscuro segun el sistema, salvo que el toggle fuerce uno. */
:root{
  --bg:#f4f5f7; --panel:#ffffff; --panel2:#eef0f4; --line:#dcdfe6;
  --fg:#1b1f27; --muted:#5a6270; --accent:#1f9d57; --accent2:#b7791f; --link:#1a66d6;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#0f1115; --panel:#171a21; --panel2:#1f2430; --line:#2a2f3a;
    --fg:#e6e6e6; --muted:#8b93a7; --accent:#4cc38a; --accent2:#f2c14e; --link:#6aa9ff;
  }
}
:root[data-theme="dark"]{
  --bg:#0f1115; --panel:#171a21; --panel2:#1f2430; --line:#2a2f3a;
  --fg:#e6e6e6; --muted:#8b93a7; --accent:#4cc38a; --accent2:#f2c14e; --link:#6aa9ff;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;z-index:10;background:var(--panel);
  border-bottom:1px solid var(--line);padding:12px 16px;
  display:flex;align-items:center;gap:12px;flex-wrap:wrap}
header h1{font-size:18px;margin:0;letter-spacing:.5px}
header h1 .dot{color:var(--accent)}
header .spacer{flex:1}
header .meta{color:var(--muted);font-size:13px}
button,.btn{font:inherit;cursor:pointer;border:1px solid var(--line);
  background:var(--panel2);color:var(--fg);padding:6px 12px;border-radius:8px}
button:hover,.btn:hover{border-color:var(--accent)}
#themeBtn{min-width:40px;line-height:1}
button.needs-refresh{background:#e5484d;border-color:#e5484d;color:#fff;font-weight:600;
  animation:pulseRed 1.4s ease-in-out infinite}
button.needs-refresh:hover{background:#d13b40;border-color:#d13b40}
@keyframes pulseRed{0%,100%{box-shadow:0 0 0 0 rgba(229,72,77,.55)}
  50%{box-shadow:0 0 0 6px rgba(229,72,77,0)}}
@media (prefers-reduced-motion: reduce){button.needs-refresh{animation:none}}

/* Layout: barra lateral + contenido */
.layout{display:flex;align-items:flex-start;max-width:1150px;margin:0 auto;gap:0}
.sidebar{flex:0 0 240px;width:240px;position:sticky;top:57px;align-self:flex-start;
  max-height:calc(100vh - 57px);overflow:auto;padding:16px 10px 16px 16px}
.sidebar .side-title{font-size:12px;text-transform:uppercase;letter-spacing:.08em;
  color:var(--muted);margin:4px 8px 8px}
.sidebar a.side-title-link{display:flex;align-items:center;gap:6px;text-decoration:none;
  border-radius:6px;padding:2px 8px}
.sidebar a.side-title-link span{font-size:13px}
.sidebar a.side-title-link:hover{color:var(--accent)}
.sidebar a.src{display:flex;align-items:center;gap:8px;text-decoration:none;color:var(--fg);
  padding:7px 10px;border-radius:8px;font-size:14px;border:1px solid transparent}
.sidebar a.src:hover{background:var(--panel2)}
.sidebar a.src.active{background:var(--panel);border-color:var(--line)}
.sidebar a.src .name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sidebar a.src .c{color:var(--muted);font-size:12px;background:var(--panel2);
  border-radius:10px;padding:1px 8px;min-width:22px;text-align:center}
.sidebar a.src.active .c{background:var(--accent);color:#06240f}
.sidebar a.src.off .c{opacity:.5}
.sidebar a.src .doterr{color:#e06c75}

main{flex:1;min-width:0;padding:16px}
.feed{margin:0 0 22px}
.feed > h2{font-size:15px;margin:0 0 8px;padding:8px 12px;background:var(--panel);
  border:1px solid var(--line);border-radius:10px;display:flex;align-items:center;gap:10px}
.feed > h2 .count{color:var(--muted);font-weight:400;font-size:13px}
.feed > h2 .err{color:#e06c75;font-weight:400;font-size:12px}
.feed > h2 .spacer{flex:1}
.feed > h2 a.mark{color:var(--muted);font-size:12px;text-decoration:none;border-bottom:1px dotted var(--muted)}
.item{border:1px solid var(--line);border-left:3px solid var(--accent);
  background:var(--panel);border-radius:10px;margin:8px 0}
.item.read{border-left-color:var(--line);opacity:.62}
.item > summary{display:flex;gap:10px;align-items:baseline;cursor:pointer;
  padding:10px 14px;list-style:none;border-radius:10px}
.item > summary::-webkit-details-marker{display:none}
.item > summary::marker{content:""}
.item > summary:hover{background:var(--panel2)}
.item .caret{color:var(--muted);transition:transform .15s;flex:0 0 auto;
  font-size:11px;line-height:1.5}
.item[open] .caret{transform:rotate(90deg)}
.item .title{font-weight:600;font-size:15px;color:var(--fg)}
.item.read .title{font-weight:500}
.item .date{color:var(--muted);font-size:12px;white-space:nowrap;margin-left:auto}
.item .body{padding:0 14px 12px 34px}
.item .text{color:var(--fg);font-size:14px;line-height:1.55;margin:2px 0 0;overflow-wrap:break-word}
.item .text p{margin:.55em 0}
.item .text p:first-child{margin-top:0}
.item .text a{color:var(--link);text-decoration:underline}
.item .text img{max-width:100%;height:auto;border-radius:6px;margin:8px 0;display:block}
.item .text figure{margin:.6em 0}
.item .text figcaption{color:var(--muted);font-size:12px;margin-top:2px}
.item .text ul,.item .text ol{margin:.55em 0;padding-left:1.4em}
.item .text li{margin:.2em 0}
.item .text h1,.item .text h2,.item .text h3,.item .text h4,.item .text h5,.item .text h6{
  font-size:1.05em;margin:.9em 0 .3em;line-height:1.3}
.item .text blockquote{margin:.6em 0;padding:.2em 0 .2em .9em;
  border-left:3px solid var(--line);color:var(--muted)}
.item .text pre{background:var(--panel2);padding:10px;border-radius:8px;overflow:auto;font-size:12.5px}
.item .text code{background:var(--panel2);padding:.1em .35em;border-radius:5px;font-size:.9em}
.item .text pre code{background:none;padding:0}
.item .text table{border-collapse:collapse;max-width:100%;display:block;overflow:auto;font-size:13px}
.item .text td,.item .text th{border:1px solid var(--line);padding:4px 8px}
.item .text hr{border:none;border-top:1px solid var(--line);margin:.9em 0}
.item .acts{margin-top:10px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.item .acts a.link{color:var(--link);font-size:13px;text-decoration:underline;font-weight:500}
.item .acts a.link:hover{opacity:.85}
.item .acts a.mark-read{color:var(--muted);font-size:12px;text-decoration:none;cursor:pointer}
.item .acts a.mark-read:hover{color:var(--accent)}
.empty{color:var(--muted);text-align:center;padding:40px}
footer{color:var(--muted);text-align:center;font-size:12px;padding:24px}

@media (max-width:720px){
  .layout{flex-direction:column}
  .sidebar{position:static;width:auto;max-height:none;flex:none;overflow:visible;
    padding:12px 16px 0;display:flex;flex-wrap:wrap;gap:6px}
  .sidebar .side-title{width:100%;margin:0 0 2px}
  .sidebar a.src{flex:0 0 auto;border:1px solid var(--line)}
  .sidebar a.src .name{max-width:45vw}
  main{padding:12px 16px 16px}
}
"""

PAGE_JS = """
// Tema: default oscuro; el toggle guarda la eleccion.
function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t==='light'?'light':'dark');
}
function currentTheme(){
  return document.documentElement.getAttribute('data-theme')==='light' ? 'light':'dark';
}
function updateThemeBtn(){
  const b = document.getElementById('themeBtn');
  if(b) b.textContent = currentTheme()==='dark' ? '\\u2600\\ufe0f' : '\\u{1F319}';
}
function toggleTheme(){
  const next = currentTheme()==='dark' ? 'light':'dark';
  try{ localStorage.setItem('tenorss-theme', next); }catch(e){}
  applyTheme(next); updateThemeBtn();
}

// Filtro por fuente desde la barra lateral.
function filterSource(el){
  const url = el.dataset.url || '';
  document.querySelectorAll('.sidebar a.src').forEach(a=>a.classList.toggle('active', a===el));
  document.querySelectorAll('main .feed').forEach(f=>{
    f.style.display = (!url || f.dataset.url===url) ? '' : 'none';
  });
  try{ localStorage.setItem('tenorss-src', url); }catch(e){}
  window.scrollTo(0,0);
}
function restoreSource(){
  let url=''; try{ url = localStorage.getItem('tenorss-src')||''; }catch(e){}
  if(!url) return;
  const el = document.querySelector('.sidebar a.src[data-url="'+CSS.escape(url)+'"]');
  if(el) filterSource(el);
}

async function post(url){
  try{ const r = await fetch(url,{method:'POST'}); return r.ok; }
  catch(e){ return false; }
}
async function toggleRead(el){
  const item = el.closest('.item');
  const id = item.dataset.id;
  const read = item.classList.contains('read');
  const ok = await post('/api/read?id='+encodeURIComponent(id)+'&read='+(read?0:1));
  if(ok){
    item.classList.toggle('read');
    el.textContent = item.classList.contains('read') ? 'marcar no leido' : 'marcar leido';
  }
}
async function markFeed(a){
  const url = a.dataset.url;
  const ok = await post('/api/readfeed?url='+encodeURIComponent(url)+'&read=1');
  if(ok) location.reload();
}

document.addEventListener('DOMContentLoaded', function(){
  updateThemeBtn(); restoreSource();
});
"""

# Script minimo en <head>: el default es oscuro (data-theme='dark' en <html>);
# solo lo cambia si hay una eleccion guardada. Corre antes de pintar (sin parpadeo).
THEME_INIT = ("<script>try{var t=localStorage.getItem('tenorss-theme');"
              "if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t);}catch(e){}</script>")


def render_feed_html(feed, tz_offset, index=0, static=False):
    items = feed.get("items", [])
    unread = sum(1 for it in items if not it.get("read"))
    parts = []
    parts.append('<section class="feed" id="feed-%d" data-url="%s">' % (index, e(feed.get("url", ""))))
    head = ['<h2><span>%s</span>' % e(feed.get("name", ""))]
    head.append('<span class="count">%d items &middot; %d sin leer</span>' % (len(items), unread))
    status = feed.get("status", "ok")
    if status and status != "ok":
        head.append('<span class="err">%s</span>' % e(status))
    head.append('<span class="spacer"></span>')
    if not static:
        head.append('<a class="mark" href="#" data-url="%s" onclick="markFeed(this);return false;">marcar todo leido</a>'
                    % e(feed.get("url", "")))
    head.append('</h2>')
    parts.append("".join(head))

    if not items:
        parts.append('<div class="empty">Sin items todavia.</div>')
    for it in items:
        cls = "item read" if it.get("read") else "item"
        parts.append('<details class="%s" data-id="%s">' % (cls, e(it.get("id", ""))))
        # Colapsado: solo el titulo (y la fecha). Clic en el titulo -> expande.
        dt = human_time(it.get("date", 0), tz_offset)
        date_html = ('<span class="date">%s</span>' % e(dt)) if dt else ""
        parts.append('<summary class="top"><span class="caret">&#9656;</span>'
                     '<span class="title">%s</span>%s</summary>'
                     % (e(it.get("title", "")), date_html))
        # Expandido: el resumen y el enlace al original.
        parts.append('<div class="body">')
        text = it.get("text", "")
        if text:
            # Ya viene saneado por sanitize_html: se inyecta tal cual.
            parts.append('<div class="text">%s</div>' % text)
        else:
            parts.append('<p class="text">(sin resumen; abri el enlace para leer)</p>')
        parts.append('<div class="acts">')
        link = it.get("link", "")
        if link:
            parts.append('<a class="link" href="%s" target="_blank" rel="noopener">Abrir enlace original &#8599;</a>'
                         % e(link))
        if not static:
            label = "marcar no leido" if it.get("read") else "marcar leido"
            parts.append('<a class="mark-read" onclick="toggleRead(this)">%s</a>' % label)
        parts.append('</div>')   # acts
        parts.append('</div>')   # body
        parts.append('</details>')
    parts.append('</section>')
    return "".join(parts)


def render_page(cache, static=False):
    tz_offset = int(cache.get("tz_offset", 0))
    feeds = cache.get("feeds", [])
    total = sum(len(f.get("items", [])) for f in feeds)
    unread = sum(1 for f in feeds for it in f.get("items", []) if not it.get("read"))
    saved = cache.get("saved", 0)
    saved_txt = human_time(saved, tz_offset) if saved else "nunca"

    html_parts = []
    html_parts.append("<!doctype html><html lang='es' data-theme='dark'><head><meta charset='utf-8'>")
    html_parts.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    html_parts.append("<title>%s</title>" % APP_NAME)
    if not static:
        html_parts.append(THEME_INIT)
    html_parts.append("<style>%s</style>" % PAGE_CSS)
    html_parts.append("</head><body>")

    html_parts.append("<header><h1>Teno<span class='dot'>&bull;</span>RSS <span style='color:var(--muted);font-weight:400;font-size:13px'>Web</span></h1>")
    html_parts.append("<span class='meta'>%d sin leer / %d items</span>" % (unread, total))
    html_parts.append("<span class='spacer'></span>")
    html_parts.append("<span class='meta'>actualizado: %s</span>" % e(saved_txt))
    if not static:
        dirty = bool(cache.get("dirty"))
        btn_cls = " class='needs-refresh'" if dirty else ""
        btn_title = " title='Cambiaron las fuentes: actualiza para refrescar todo'" if dirty else ""
        html_parts.append("<form method='post' action='/refresh' style='margin:0'>"
                          "<button%s%s>Actualizar</button></form>" % (btn_cls, btn_title))
        html_parts.append("<button id='themeBtn' title='Cambiar tema' onclick='toggleTheme()'>&#9728;&#65039;</button>")
    html_parts.append("</header>")

    html_parts.append("<div class='layout'>")

    # Barra lateral con todas las fuentes.
    html_parts.append("<aside class='sidebar'>")
    if static:
        html_parts.append("<div class='side-title'>Fuentes</div>")
    else:
        html_parts.append("<a class='side-title side-title-link' href='/fuentes' "
                          "title='Administrar fuentes'>Fuentes <span>&#9881;</span></a>")
    if not static:
        html_parts.append("<a class='src active' data-url='' href='#' onclick='filterSource(this);return false;'>"
                          "<span class='name'>Todas</span><span class='c'>%d</span></a>" % unread)
    for i, feed in enumerate(feeds):
        fitems = feed.get("items", [])
        funread = sum(1 for it in fitems if not it.get("read"))
        err = feed.get("status", "ok") not in ("ok", "", None)
        off = "" if funread else " off"
        dot = "<span class='doterr' title='error'>&#9679;</span> " if err else ""
        if static:
            html_parts.append("<a class='src%s' href='#feed-%d'>%s<span class='name'>%s</span><span class='c'>%d</span></a>"
                              % (off, i, dot, e(feed.get("name", "")), funread))
        else:
            html_parts.append("<a class='src%s' data-url='%s' href='#feed-%d' onclick='filterSource(this);return false;'>"
                              "%s<span class='name'>%s</span><span class='c'>%d</span></a>"
                              % (off, e(feed.get("url", "")), i, dot, e(feed.get("name", "")), funread))
    html_parts.append("</aside>")

    # Contenido.
    html_parts.append("<main>")
    if not feeds:
        html_parts.append("<div class='empty'>No hay fuentes. Agregalas en <code>rss.txt</code> y actualiza.</div>")
    for i, feed in enumerate(feeds):
        html_parts.append(render_feed_html(feed, tz_offset, index=i, static=static))
    html_parts.append("</main>")

    html_parts.append("</div>")  # layout

    html_parts.append("<footer>%s &middot; lector de RSS</footer>" % APP_NAME)
    if not static:
        html_parts.append("<script>%s</script>" % PAGE_JS)
    html_parts.append("</body></html>")
    return "".join(html_parts)


ADMIN_CSS = """
.admin{max-width:760px;margin:0 auto;padding:18px 16px 40px}
.admin .intro{color:var(--muted);font-size:14px;margin:0 0 18px}
.admin .tz{display:flex;align-items:center;gap:10px;margin:0 0 16px;font-size:14px}
.admin .tz input{width:120px}
.admin input{font:inherit;background:var(--panel);color:var(--fg);
  border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.admin input:focus{outline:none;border-color:var(--accent)}
.admin .head-row,.admin .row{display:grid;grid-template-columns:1fr 2fr 40px;gap:8px;margin:6px 0}
.admin .head-row{color:var(--muted);font-size:12px;text-transform:uppercase;
  letter-spacing:.06em;margin-bottom:2px;padding:0 2px}
.admin .row input{width:100%}
.admin .del{border:1px solid var(--line);background:var(--panel2);color:var(--muted);
  border-radius:8px;cursor:pointer;font-size:14px;line-height:1}
.admin .del:hover{border-color:#e06c75;color:#e06c75}
.admin .add{margin:10px 0 0;background:transparent;border:1px dashed var(--line);
  color:var(--muted);width:100%;padding:9px;border-radius:8px}
.admin .add:hover{border-color:var(--accent);color:var(--accent)}
.admin .savebar{display:flex;align-items:center;gap:14px;margin-top:22px;
  padding-top:16px;border-top:1px solid var(--line)}
.admin .savebar button{background:var(--accent);color:#06240f;border:none;
  font-weight:600;padding:9px 20px}
.admin .savebar button:hover{opacity:.9}
.admin .savebar a{color:var(--muted);text-decoration:none}
.admin .savebar a:hover{color:var(--fg)}
.admin .note{color:var(--muted);font-size:12.5px;margin-top:18px}
.admin .ok{background:var(--panel);border:1px solid var(--accent);border-radius:8px;
  padding:10px 14px;margin:0 0 18px;font-size:14px}
@media (max-width:560px){
  .admin .head-row,.admin .row{grid-template-columns:1fr 40px}
  .admin .head-row span:nth-child(2){display:none}
  .admin .row .r-name{grid-column:1/2}
  .admin .row .r-url{grid-column:1/2}
  .admin .row .del{grid-row:1/3}
}
"""

ADMIN_JS = """
function applyTheme(t){ document.documentElement.setAttribute('data-theme', t==='light'?'light':'dark'); }
function currentTheme(){ return document.documentElement.getAttribute('data-theme')==='light'?'light':'dark'; }
function updateThemeBtn(){ var b=document.getElementById('themeBtn');
  if(b) b.textContent = currentTheme()==='dark' ? '\\u2600\\ufe0f' : '\\u{1F319}'; }
function toggleTheme(){ var n=currentTheme()==='dark'?'light':'dark';
  try{localStorage.setItem('tenorss-theme',n);}catch(e){} applyTheme(n); updateThemeBtn(); }

function rowHTML(){
  return '<input class="r-name" name="name" placeholder="Nombre" value="">'
    + '<input class="r-url" name="url" placeholder="https://ejemplo.com/feed" value="">'
    + '<button type="button" class="del" title="Borrar" onclick="delRow(this)">\\u2715</button>';
}
function addRow(){
  var d=document.createElement('div'); d.className='row'; d.innerHTML=rowHTML();
  document.getElementById('rows').appendChild(d);
  d.querySelector('.r-name').focus();
}
function delRow(b){ b.closest('.row').remove(); }
document.addEventListener('DOMContentLoaded', updateThemeBtn);
"""


def render_admin_page(saved_backup=None):
    feeds_cfg, _tz = load_config()
    p = []
    p.append("<!doctype html><html lang='es' data-theme='dark'><head><meta charset='utf-8'>")
    p.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    p.append("<title>%s &middot; Fuentes</title>" % APP_NAME)
    p.append(THEME_INIT)
    p.append("<style>%s%s</style>" % (PAGE_CSS, ADMIN_CSS))
    p.append("</head><body>")

    p.append("<header><h1>Teno<span class='dot'>&bull;</span>RSS "
             "<span style='color:var(--muted);font-weight:400;font-size:13px'>Fuentes</span></h1>")
    p.append("<span class='spacer'></span>")
    p.append("<a class='btn' href='/'>&larr; Volver</a>")
    p.append("<button id='themeBtn' title='Cambiar tema' onclick='toggleTheme()'>&#9728;&#65039;</button>")
    p.append("</header>")

    p.append("<div class='admin'>")
    if saved_backup:
        p.append("<div class='ok'>Guardado. Backup del rss.txt anterior: <code>%s</code></div>"
                 % e(os.path.join("backups", os.path.basename(saved_backup))))
    p.append("<p class='intro'>Administr&aacute; las fuentes RSS/Atom. Edit&aacute; nombre y URL, "
             "agreg&aacute; o borr&aacute; filas, y puls&aacute; <b>Guardar</b>. "
             "Antes de escribir se hace un backup del <code>rss.txt</code> anterior.</p>")

    p.append("<form method='post' action='/fuentes/save'>")
    p.append("<div class='head-row'><span>Nombre</span><span>URL del feed</span><span></span></div>")
    p.append("<div id='rows'>")
    for f in feeds_cfg:
        p.append("<div class='row'>"
                 "<input class='r-name' name='name' placeholder='Nombre' value='%s'>"
                 "<input class='r-url' name='url' placeholder='https://ejemplo.com/feed' value='%s'>"
                 "<button type='button' class='del' title='Borrar' onclick='delRow(this)'>&#10005;</button>"
                 "</div>" % (e(f.get("name", "")), e(f.get("url", ""))))
    p.append("</div>")
    p.append("<button type='button' class='add' onclick='addRow()'>+ Agregar fuente</button>")

    p.append("<div class='savebar'><button type='submit'>Guardar</button>"
             "<a href='/'>Cancelar</a></div>")
    p.append("</form>")

    p.append("<p class='note'>Los backups quedan en la carpeta <code>backups/</code> "
             "junto al programa, uno por cada guardado.</p>")
    p.append("</div>")
    p.append("<script>%s</script>" % ADMIN_JS)
    p.append("</body></html>")
    return "".join(p)


# --------------------------------------------------------------------------
# Servidor web
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "TenoRSSWeb"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, body, status=200, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _redirect(self, to="/"):
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return ""
        return self.rfile.read(length).decode("utf-8", "replace")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(render_page(load_cache()))
        elif parsed.path == "/fuentes":
            self._send(render_admin_page())
        elif parsed.path.startswith("/media/"):
            self._serve_media(parsed.path[len("/media/"):])
        elif parsed.path == "/favicon.ico":
            self._send(b"", status=204, ctype="image/x-icon")
        else:
            self._send("<h1>404</h1>", status=404)

    def _serve_media(self, name):
        # Solo nombres simples (evita salir de MEDIA_DIR).
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name or "") or name.endswith(".tmp"):
            self._send(b"", status=404, ctype="text/plain")
            return
        fpath = os.path.join(MEDIA_DIR, name)
        if not os.path.isfile(fpath):
            self._send(b"", status=404, ctype="text/plain")
            return
        ext = os.path.splitext(name)[1].lower()
        ctype = _EXT_CT.get(ext, "application/octet-stream")
        with open(fpath, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=31536000")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        if parsed.path == "/refresh":
            try:
                update_all(verbose=False)
            except Exception as exc:  # que un feed roto no tire el servidor
                sys.stderr.write("  refresh error: %s\n" % exc)
            self._redirect("/")
        elif parsed.path == "/fuentes/save":
            body = parse_qs(self._read_body(), keep_blank_values=True)
            names = body.get("name", [])
            urls = body.get("url", [])
            feeds = []
            for i, url in enumerate(urls):
                url = url.strip()
                if not url:
                    continue
                name = names[i].strip() if i < len(names) else ""
                feeds.append({"name": name, "url": url})
            try:
                save_rss_txt(feeds)
                sync_cache_to_config()  # refleja los cambios sin descargar
            except Exception as exc:
                sys.stderr.write("  save fuentes error: %s\n" % exc)
                self._send("Error al guardar: %s" % e(str(exc)), status=500, ctype="text/plain")
                return
            self._redirect("/")
        elif parsed.path == "/api/read":
            item_hash = (q.get("id") or [""])[0]
            read = (q.get("read") or ["1"])[0] == "1"
            ok = set_read(item_hash, read)
            self._send("ok" if ok else "no", status=200 if ok else 404, ctype="text/plain")
        elif parsed.path == "/api/readfeed":
            url = (q.get("url") or [""])[0]
            read = (q.get("read") or ["1"])[0] == "1"
            ok = set_read_feed(url, read)
            self._send("ok" if ok else "no", status=200 if ok else 404, ctype="text/plain")
        else:
            self._send("404", status=404, ctype="text/plain")


def serve(port, do_update=True):
    if do_update:
        feeds_cfg, _ = load_config()
        if feeds_cfg:
            print("Actualizando %d fuentes..." % len(feeds_cfg))
            try:
                update_all(verbose=True)
            except Exception as exc:
                print("  (aviso) fallo la actualizacion inicial: %s" % exc)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("\n%s en http://localhost:%d  (Ctrl+C para salir)" % (APP_NAME, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nChau.")
    finally:
        httpd.server_close()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="TenoRSSWeb - lector de RSS que genera una web.")
    ap.add_argument("--port", type=int, default=8080, help="puerto del servidor (def. 8080)")
    ap.add_argument("--update", action="store_true", help="solo descargar y actualizar cache.json")
    ap.add_argument("--build", metavar="ARCHIVO", help="generar un HTML estatico y salir")
    ap.add_argument("--no-update", action="store_true", help="no actualizar al arrancar el servidor")
    args = ap.parse_args(argv)

    if args.update:
        print("Actualizando fuentes...")
        update_all(verbose=True)
        print("cache.json actualizado.")
        return 0

    if args.build:
        update_all(verbose=True)
        page = render_page(load_cache(), static=True)
        with open(args.build, "w", encoding="utf-8") as fh:
            fh.write(page)
        print("HTML generado en %s" % args.build)
        return 0

    serve(args.port, do_update=not args.no_update)
    return 0


if __name__ == "__main__":
    sys.exit(main())
