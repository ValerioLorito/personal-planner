"""
sources.py — read-only access to the knowledge folders the agents ground on.

Categories:
  * `thesis`          → the thesis repository (config thesis.root)
  * `uni:<Folder>`    → one per subfolder of university.root (a course)

Every path an agent asks for is resolved with realpath and must stay inside
its category root; nothing here ever writes outside data/ (the only file it
creates is the text-extraction cache).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import threading
import zipfile
from html import unescape

from .config import DATA_DIR, expand

TEXT_EXT = {".py", ".md", ".txt", ".tex", ".bib", ".json", ".yaml", ".yml", ".toml", ".cfg",
            ".ini", ".sh", ".csv", ".tsv", ".rst", ".js", ".ts", ".html", ".css", ".java",
            ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".r", ".m", ".sql", ".slurm", ".qasm",
            ".lp", ".pl", ".hs", ".ml", ".scala", ".kt", ".swift", ".ipynb", ".org", ".jl"}
DOC_EXT = {".pdf", ".pptx", ".docx"}
STOP = {"and", "the", "of", "in", "for", "to", "a", "an", "with", "on", "vu", "vo", "ue", "se"}
MAX_TEXT_BYTES = 1_500_000
CACHE_PATH = os.path.join(DATA_DIR, "extract_cache.json")
_cache_lock = threading.Lock()


class SourceError(Exception):
    pass


# ---------------------------------------------------------------- categories
def categories(cfg: dict) -> list:
    out = []
    t = cfg["thesis"]
    troot = expand(t["root"])
    out.append({"id": "thesis", "label": t.get("label", "Thesis"), "kind": "thesis",
                "root": troot, "exists": os.path.isdir(troot),
                "calendar": cfg["calendars"]["thesis"], "keywords": ["thesis"]})
    KNOWN_COURSES = ["GENERATIVE AI", "HYBRID QUANTUM", "KNOWLEDGE REPRESENTATION", "PROJECT IN LOGIC AND AI"]
    uroot = expand(cfg["university"]["root"])
    excl = {e.strip().lower() for e in cfg["university"].get("exclude", [])}
    aliases = cfg["university"].get("aliases", {})
    entries = []
    if os.path.isdir(uroot):
        try:
            entries = sorted(os.listdir(uroot))
        except OSError:
            entries = []
    if not entries:
        entries = KNOWN_COURSES

    for name in entries:
        p = os.path.join(uroot, name)
        if name.startswith(".") or name.strip().lower() in excl:
            continue
        kw = [w for w in re.findall(r"[a-zà-ü0-9.]+", name.lower()) if len(w) >= 3 and w not in STOP]
        kw += [a.lower() for a in aliases.get(name, [])]
        exists = os.path.isdir(p)
        out.append({"id": "uni:" + name, "label": name.strip().title(), "kind": "course",
                    "root": p, "exists": exists,
                    "calendar": cfg["calendars"]["university"], "keywords": kw})
    return out


def get_category(cfg: dict, cat_id: str) -> dict:
    for c in categories(cfg):
        if c["id"] == cat_id or c["label"].lower() == (cat_id or "").lower():
            return c
    raise SourceError("unknown category %r (known: %s)" % (
        cat_id, ", ".join(c["id"] for c in categories(cfg))))


def resolve(cat: dict, rel: str) -> str:
    rel = (rel or "").strip().lstrip("/")
    root = cat["root"]
    path = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, path]) != root:
        raise SourceError("path escapes the %s folder" % cat["label"])
    return path


def _skip_dirs(cfg: dict, cat: dict) -> set:
    return set(cfg["thesis"].get("skip_dirs", [])) if cat["kind"] == "thesis" else {".git", "__pycache__"}


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%d%s" % (n, unit)
        n //= 1024
    return "%dTB" % n


# ---------------------------------------------------------------- listing
def list_dir(cfg: dict, cat: dict, rel: str = "", depth: int = 1, cap: int = 200) -> str:
    base = resolve(cat, rel)
    if not os.path.isdir(base):
        raise SourceError("not a directory: %s" % rel)
    skip = _skip_dirs(cfg, cat)
    lines, count = [], 0
    depth = max(1, min(int(depth or 1), 4))

    def walk(path, level):
        nonlocal count
        try:
            entries = sorted(os.listdir(path))
        except OSError as e:
            lines.append("  " * level + "(unreadable: %s)" % e)
            return
        for name in entries:
            if count >= cap:
                return
            if name.startswith(".") and not name.endswith(".icloud"):
                continue
            full = os.path.join(path, name)
            if name.endswith(".icloud"):
                lines.append("  " * level + "%s  [in iCloud, not downloaded]" % name[1:-7])
                count += 1
                continue
            if os.path.isdir(full):
                if name in skip:
                    lines.append("  " * level + name + "/  [skipped]")
                    count += 1
                    continue
                lines.append("  " * level + name + "/")
                count += 1
                if level + 1 < depth:
                    walk(full, level + 1)
            else:
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                lines.append("  " * level + "%s  (%s)" % (name, _human(size)))
                count += 1

    walk(base, 0)
    if count >= cap:
        lines.append("… (listing truncated at %d entries)" % cap)
    return "\n".join(lines) or "(empty)"


def iter_files(cfg: dict, cat: dict, rel: str = "", limit: int = 5000):
    base = resolve(cat, rel)
    skip = _skip_dirs(cfg, cat)
    n = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in skip and not d.startswith("."))
        for f in sorted(filenames):
            if f.startswith("."):
                continue
            n += 1
            if n > limit:
                return
            yield os.path.join(dirpath, f)


# ---------------------------------------------------------------- reading
def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_PATH)


def _xml_text(xml: str) -> str:
    xml = re.sub(r"</a:p>|</w:p>|<w:br/>|<a:br/>", "\n", xml)
    return unescape(re.sub(r"<[^>]+>", "", xml))


def extract_text(path: str, calendar=None, max_chars: int = 60000) -> str:
    """Full text of a file (cached for PDFs/Office files by path+mtime+size)."""
    ext = os.path.splitext(path)[1].lower()
    st = os.stat(path)
    if ext in DOC_EXT:
        key = "%s|%d|%d" % (path, int(st.st_mtime), st.st_size)
        with _cache_lock:
            cache = _load_cache()
            if key in cache:
                return cache[key][:max_chars]
        text = ""
        if ext == ".pdf":
            if calendar is None:
                raise SourceError("PDF extraction needs plannerkit")
            r = calendar.pdf_text(path, 200000)
            text = r.get("text", "")
            if not text.strip():
                text = "(PDF has no text layer — probably scanned; content unknown)"
        else:
            with zipfile.ZipFile(path) as z:
                if ext == ".docx":
                    text = _xml_text(z.read("word/document.xml").decode("utf-8", "replace"))
                else:
                    slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                                    key=lambda n: int(re.findall(r"\d+", n)[-1]))
                    parts = []
                    for i, n in enumerate(slides, 1):
                        parts.append("--- slide %d ---\n%s" % (i, _xml_text(z.read(n).decode("utf-8", "replace"))))
                    text = "\n".join(parts)
        with _cache_lock:
            cache = _load_cache()
            cache[key] = text
            _save_cache(cache)
        return text[:max_chars]
    if ext == ".ipynb":
        with open(path, encoding="utf-8", errors="replace") as f:
            nb = json.load(f)
        parts = []
        for cell in nb.get("cells", []):
            src = "".join(cell.get("source", []))
            parts.append("# [%s]\n%s" % (cell.get("cell_type", "cell"), src))
        return "\n\n".join(parts)[:max_chars]
    if ext in TEXT_EXT or st.st_size < 200_000:
        with open(path, "rb") as f:
            raw = f.read(min(st.st_size, MAX_TEXT_BYTES))
        if b"\x00" in raw[:4096]:
            raise SourceError("binary file (%s), content not readable" % _human(st.st_size))
        return raw.decode("utf-8", "replace")[:max_chars]
    raise SourceError("unsupported file type %s (%s)" % (ext or "(none)", _human(st.st_size)))


def read_file(cfg: dict, cat: dict, rel: str, calendar=None, max_chars: int = 12000,
              offset: int = 0) -> str:
    path = resolve(cat, rel)
    if os.path.isdir(path):
        return list_dir(cfg, cat, rel, depth=1)
    if not os.path.exists(path):
        icloud = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".icloud")
        if os.path.exists(icloud):
            raise SourceError("file is in iCloud but not downloaded; content unknown")
        raise SourceError("no such file: %s" % rel)
    text = extract_text(path, calendar, max_chars=offset + max_chars + 1)
    chunk = text[offset:offset + max_chars]
    more = len(text) > offset + max_chars
    if more:
        chunk += "\n… [truncated; call read_file again with offset=%d for more]" % (offset + max_chars)
    return chunk


def search(cfg: dict, cat: dict, query: str, calendar=None, max_hits: int = 30,
           glob: str = "") -> str:
    q = (query or "").strip()
    if not q:
        raise SourceError("empty query")
    try:
        rx = re.compile(q, re.I) if "|" in q or "\\" in q else re.compile(re.escape(q), re.I)
    except re.error:
        rx = re.compile(re.escape(q), re.I)
    hits, scanned_docs = [], 0
    root = cat["root"]
    for path in iter_files(cfg, cat):
        rel = os.path.relpath(path, root)
        if glob and not fnmatch.fnmatch(rel, glob) and not fnmatch.fnmatch(os.path.basename(rel), glob):
            continue
        if rx.search(os.path.basename(rel)):
            hits.append("%s  [filename match]" % rel)
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext in DOC_EXT:
                if scanned_docs >= 60:
                    continue
                scanned_docs += 1
                text = extract_text(path, calendar, max_chars=300000)
            elif ext in TEXT_EXT:
                if os.path.getsize(path) > MAX_TEXT_BYTES:
                    continue
                text = extract_text(path, calendar, max_chars=MAX_TEXT_BYTES)
            else:
                continue
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append("%s:%d: %s" % (rel, i, line.strip()[:200]))
                if len(hits) >= max_hits:
                    return "\n".join(hits) + "\n… (more hits; refine the query)"
    return "\n".join(hits) or "(no matches)"


def related_events(cat: dict, events: list) -> list:
    """Calendar events whose title mentions one of the category keywords."""
    kws = [k for k in cat.get("keywords", []) if k]
    out = []
    for e in events:
        t = (e.get("title") or "").lower()
        if any(k in t for k in kws):
            out.append(e)
    return out
