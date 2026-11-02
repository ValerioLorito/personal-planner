"""
sources.py — grounded material intelligence for the general university planner.

The planner was born in a thesis repository, where `project_context` could
ground every estimate in real artefacts (git history, audits, run folders).
University coursework lives somewhere else: an iCloud Drive folder with one
subfolder per subject. This module makes that material *visible* to the
estimating brain:

  categories(cfg)      -> ["Thesis"] + one entry per subject folder
  inventory(cfg)       -> a bounded walk of the university root (depth ≤ 2,
                          ≤ MAX_ENTRIES entries) plus each subject's
                          planner-notes.md, if the user wrote one
  category_block(...)  -> the prompt-ready text block for ONE category
  material_blocks(...) -> every block appended to the LLM context
  extract_text(paths)  -> full text of picked files (PDF via PDFKit through
                          osascript, PPTX/DOCX via stdlib zipfile), cached by
                          (path, mtime, size) in planner/data/extract_cache.json

Design rules
------------
* Read-only. Nothing here writes to the user's folders; the only file the
  module creates is its own extraction cache.
* Bounded. Walks are depth-limited and entry-capped, extraction has a hard
  per-call character budget, and every truncation is recorded in the returned
  data (never silent).
* Degrades gracefully. iCloud placeholders (`.name.icloud`), scanned PDFs with
  no text layer, and Keynote files come back as explicit "unknown" entries
  with a reason, so the LLM can say what it does and does not know.
"""
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
CACHE_PATH = os.path.join(DATA_DIR, "extract_cache.json")

THESIS_CATEGORY = "Thesis"
NOTES_FILENAME = "planner-notes.md"

MAX_ENTRIES = 400          # inventory entries per walk
MAX_DEPTH = 2              # university_root -> subject -> child
MAX_NOTE_CHARS = 4000      # per subject's planner-notes.md
DEFAULT_BUDGET = 60000     # total chars handed to the LLM per extraction call
MAX_CACHED_CHARS = 200000  # per file, in the cache (truncation happens later)
EXTRACT_TIMEOUT = 30       # seconds per osascript/PDFKit call

SKIP_NAMES = {".DS_Store", "Icon\r", ".git", ".icloud"}
EXTRACTABLE = (".pdf", ".pptx", ".docx")


class SourceError(RuntimeError):
    pass


# ---------------------------------------------------------------- categories
def _unalias(name):
    """Normalize an iCloud placeholder name: `.name.icloud` -> `name`."""
    name = str(name or "").strip()
    if name.startswith(".") and name.endswith(".icloud") and len(name) > 8:
        return name[1:-len(".icloud")]
    return name


def university_root(cfg):
    """The configured university folder, or '' when unset/missing."""
    root = str((cfg.get("university_root") or "")).strip()
    if not root:
        return ""
    root = os.path.expanduser(root)
    return root if os.path.isdir(root) else ""


def categories(cfg):
    """Task areas that are real folders: ['Thesis'] + one per subject."""
    out = [THESIS_CATEGORY]
    root = university_root(cfg)
    if root:
        try:
            for name in sorted(os.listdir(root)):
                if name in SKIP_NAMES or name.startswith("."):
                    continue
                if os.path.isdir(os.path.join(root, name)):
                    out.append(_unalias(name))
        except OSError:
            pass
    return out


def subject_dir(cfg, category):
    """Absolute folder for a subject, or None for Thesis / unknown ones."""
    root = university_root(cfg)
    if not root or category == THESIS_CATEGORY:
        return None
    path = os.path.join(root, category)
    return path if os.path.isdir(path) else None


# ---------------------------------------------------------------- inventory
def _entry(root, rel, category, is_dir, size, mtime, placeholder=False):
    return {"path": rel, "category": category, "kind": ("dir" if is_dir
                                                       else "file"),
            "size": size, "mtime": mtime, "placeholder": placeholder}


def _scan_subject(root, category, entries, remaining):
    """One subject folder -> entries (depth ≤ MAX_DEPTH), placeholders named.

    `remaining` is the global entry budget left; returns (used, truncated)."""
    base = os.path.join(root, category)
    count = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(base):
        rel_base = os.path.relpath(dirpath, root)
        depth = 0 if os.path.abspath(dirpath) == os.path.abspath(base) else \
            rel_base.count(os.sep) - category.count(os.sep) + 1
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_NAMES and not d.startswith("."))
        for name in sorted(filenames):
            # iCloud placeholders look like dotfiles (.name.icloud) but are
            # exactly the entries that must stay visible (content unknown).
            if name in SKIP_NAMES:
                continue
            if name.startswith(".") and not name.endswith(".icloud"):
                continue
            if count >= remaining:
                return count, True
            rel = os.path.normpath(os.path.join(rel_base, name))
            ph = name.endswith(".icloud")
            size = None
            mtime = None
            if not ph:
                try:
                    st = os.stat(os.path.join(dirpath, name))
                    size, mtime = st.st_size, int(st.st_mtime)
                except OSError:
                    pass
            entries.append(_entry(root, rel, category, False, size, mtime, ph))
            count += 1
        if depth + 1 >= MAX_DEPTH:
            dirnames[:] = []
        for d in dirnames:
            if count >= remaining:
                return count, True
            rel = os.path.normpath(os.path.join(rel_base, d))
            entries.append(_entry(root, rel, category, True, None, None))
            count += 1
    return count, truncated


def _read_notes(cfg, category):
    """The subject's planner-notes.md, capped, or '' when absent."""
    base = subject_dir(cfg, category)
    if not base:
        return ""
    for name in (NOTES_FILENAME, _unalias(NOTES_FILENAME)):
        path = os.path.join(base, name)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    return fh.read(MAX_NOTE_CHARS).strip()
            except OSError:
                return ""
    return ""


def inventory(cfg):
    """Bounded walk of the university root.

    Returns {"root", "categories", "entries", "notes", "truncated"} — entries
    are relative to the root (never absolute paths leak into prompts), notes
    maps subject -> planner-notes.md text (may be missing keys).
    """
    root = university_root(cfg)
    subjects = categories(cfg)[1:]          # drop Thesis
    entries, notes = [], {}
    truncated = False
    if root:
        for subject in subjects:
            used, trunc = _scan_subject(root, subject, entries,
                                        MAX_ENTRIES - len(entries))
            truncated = truncated or trunc
            note = _read_notes(cfg, subject)
            if note:
                notes[subject] = note
    return {"root": root, "categories": subjects, "entries": entries,
            "notes": notes, "truncated": truncated}


# ---------------------------------------------------------------- prompting
def _human_size(num):
    if num is None:
        return "size unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return "%s %s" % (num if unit == "B" else "%.1f" % num, unit)
        num /= 1024.0
    return "%s GB" % num


def category_block(inv, category, limit=60):
    """Prompt-ready file listing for ONE category ('' when it has none)."""
    entries = [e for e in inv.get("entries", []) if e["category"] == category]
    if not entries and not inv.get("notes", {}).get(category):
        return ""
    lines = ['MATERIAL — subject "%s" (%d listed item(s)):' % (category,
                                                              len(entries))]
    shown = entries[:limit]
    for e in shown:
        if e["kind"] == "dir":
            lines.append("- %s/ (folder)" % e["path"])
        elif e["placeholder"]:
            lines.append("- %s (iCloud placeholder — not downloaded, "
                         "content unknown)" % e["path"])
        else:
            lines.append("- %s (%s)" % (e["path"], _human_size(e["size"])))
    if len(entries) > len(shown):
        lines.append("… and %d more (listing capped)" % (len(entries) -
                                                         len(shown)))
    note = inv.get("notes", {}).get(category)
    if note:
        lines.append('notes from %s: "%s"'
                     % (NOTES_FILENAME, note.replace("\n", " ")[:1200]))
    return "\n".join(lines)


def material_blocks(inv, category):
    """Context blocks for an intake: the chosen subject + the whole root map.

    Two blocks so the LLM sees the picked subject in detail and still knows
    what *other* subjects exist (cross-referencing a lecture file while doing
    a thesis task is rare but legal).
    """
    blocks = []
    if category and category != THESIS_CATEGORY:
        block = category_block(inv, category)
        if block:
            blocks.append(block)
        others = [c for c in inv.get("categories", []) if c != category]
    else:
        others = inv.get("categories", [])
    if others:
        lines = ["MATERIAL — university folder map (one folder per subject):"]
        counts = {}
        for e in inv.get("entries", []):
            counts[e["category"]] = counts.get(e["category"], 0) + 1
        for name in others:
            lines.append("- %s (%s item(s) on file)"
                         % (name, counts.get(name, 0)))
        blocks.append("\n".join(lines))
    return blocks


# ---------------------------------------------------------------- extraction
def _cache_load():
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _cache_save(cache):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass                      # the cache is an optimisation, never a need


def _cache_key(path):
    st = os.stat(path)
    return "%s|%d|%d" % (path, int(st.st_mtime), st.st_size)


def _run_js(script, timeout=EXTRACT_TIMEOUT):
    """osascript JXA with a hard timeout; stdout or SourceError."""
    argv = ["osascript", "-l", "JavaScript", "-e", script]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SourceError("osascript timed out after %ss" % timeout) from exc
    except FileNotFoundError as exc:
        raise SourceError("osascript not found (needs macOS)") from exc
    except OSError as exc:
        raise SourceError("failed to launch osascript: %s" % exc) from exc
    if proc.returncode != 0:
        raise SourceError((proc.stderr or proc.stdout).strip()[:300]
                          or "osascript exited %s" % proc.returncode)
    return proc.stdout.strip()


_PDF_JS = r"""
ObjC.import('Foundation');
ObjC.import('Quartz');
function esc(p){ return p.replace(/\\/g, '\\\\').replace(/"/g, '\\"'); }
const path = esc($(PATH_JSON));
const url = $.NSURL.fileURLWithPath(path);
const doc = $.PDFDocument.alloc.initWithURL(url);
if (doc.isNil()) { 'ERROR: cannot open'; } else {
  const n = doc.pageCount;
  let out = '';
  for (let i = 0; i < n; i++) {
    const page = doc.pageAtIndex(i);
    const s = page ? page.string : null;
    out += '\n<<<page ' + (i + 1) + '>>>\n' + (s ? s.js : '');
  }
  JSON.stringify({pages: n, text: out});
}
"""


def _extract_pdf(path):
    """PDF text via PDFKit (JXA). Returns {"pages", "text"} or raises."""
    js = _PDF_JS.replace("$(PATH_JSON)",
                         json.dumps(os.path.abspath(path)))
    raw = _run_js(js)
    if raw.startswith("ERROR:"):
        raise SourceError(raw[6:].strip())
    try:
        data = json.loads(raw)
        return {"pages": int(data.get("pages") or 0),
                "text": str(data.get("text") or "")}
    except (ValueError, TypeError) as exc:
        raise SourceError("unparseable PDFKit output") from exc


def _xml_text(data):
    """All text of a pptx/docx zip member: strip tags, keep words."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ""
    return re.sub(r"\s+", " ", " ".join(
        t.strip() for t in root.itertext() if t and t.strip())).strip()


def _extract_zip_text(path):
    """PPTX/DOCX text with stdlib only: read the XML members, strip tags."""
    import zipfile
    parts = []
    pages = 0
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if path.lower().endswith(".pptx"):
                slides = sorted(
                    (n for n in names
                     if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                    key=lambda n: int(re.search(r"(\d+)", n).group(1)))
                pages = len(slides)
                members = slides
            else:
                members = [n for n in
                           ("word/document.xml", "word/footnotes.xml")
                           if n in names]
            for name in members:
                text = _xml_text(zf.read(name))
                if text:
                    parts.append(text)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise SourceError("unreadable office file: %s" % exc) from exc
    return {"pages": pages or None, "text": "\n".join(parts)}


def extract_one(path):
    """Full text of ONE file. Returns a result dict; never raises."""
    rel_kind = path.lower().rsplit(".", 1)[-1] if "." in path else ""
    base = {"path": path, "kind": rel_kind, "pages": None, "text": "",
            "error": None, "cached": False}
    if not os.path.isfile(path):
        base["error"] = "file not found (or an iCloud placeholder)"
        return base
    if path.endswith(".icloud"):
        base["error"] = ("iCloud placeholder — not downloaded; "
                         "content unknown until you open it once")
        return base
    try:
        key = _cache_key(path)
    except OSError as exc:
        base["error"] = str(exc)
        return base
    cache = _cache_load()
    hit = cache.get(key)
    if hit and not hit.get("error"):
        base.update(hit, cached=True)
        return base

    try:
        if rel_kind == "pdf":
            res = _extract_pdf(path)
            if not res["text"].strip():
                base["error"] = ("no text layer (likely a scan) — %d page(s), "
                                 "content unknown" % res["pages"])
                base["pages"] = res["pages"]
                entry = {"pages": res["pages"], "text": "",
                         "error": base["error"]}
                cache[key] = entry
                _cache_save(cache)
                return base
            entry = {"pages": res["pages"],
                     "text": res["text"][:MAX_CACHED_CHARS]}
        elif rel_kind in ("pptx", "docx"):
            res = _extract_zip_text(path)
            if not res["text"].strip():
                base["error"] = "no extractable text"
                entry = {"pages": res["pages"], "text": "", "error": base["error"]}
                cache[key] = entry
                _cache_save(cache)
                return base
            entry = {"pages": res["pages"],
                     "text": res["text"][:MAX_CACHED_CHARS]}
        elif rel_kind == "key":
            base["error"] = ("Keynote format has no extractable text — "
                             "export a PDF if the content matters")
            return base
        else:
            base["error"] = "unsupported format for full-text extraction"
            return base
        cache[key] = entry
        _cache_save(cache)
        base.update(entry)
        return base
    except SourceError as exc:
        base["error"] = str(exc)
        return base                     # graceful: one bad file must not stop intake
    except Exception as exc:            # noqa: BLE001 — same reason
        base["error"] = "extraction failed: %s" % exc
        return base


def extract_text(paths, budget=DEFAULT_BUDGET):
    """Full text for several files inside one character budget.

    Water-filling: every file gets an equal share per round; a file satisfied
    with less drops out and its unused budget flows to the rest (a one-page
    syllabus never starves the 40-page paper). Total is capped at `budget`,
    every truncation is marked in the text itself and in the entry, so the
    LLM (and the analysis) can say 'I read half of it'. Returns a list in the
    same order as `paths`.
    """
    paths = [str(p) for p in (paths or [])]
    results = [extract_one(p) for p in paths]
    lengths = [len(r.get("text") or "") for r in results]
    allowed = [0] * len(results)
    waiting = [i for i in range(len(results)) if lengths[i]]
    remaining = budget
    while waiting and remaining > 0:
        want = sum(lengths[i] - allowed[i] for i in waiting)
        if want <= remaining:                    # everything fits in full
            for i in waiting:
                allowed[i] = lengths[i]
            break
        share = remaining // len(waiting)
        if share <= 0:                           # dust left: split by need
            for i in waiting:
                allowed[i] += int(remaining
                                  * (lengths[i] - allowed[i]) / want)
            break
        for i in list(waiting):                  # one equal round each
            give = min(lengths[i] - allowed[i], share)
            allowed[i] += give
            remaining -= give
            if allowed[i] == lengths[i]:
                waiting.remove(i)
    for r, a, ln in zip(results, allowed, lengths):
        r["chars"] = a
        r["truncated"] = a < ln
        if r["truncated"]:
            head = (r.get("text") or "")[:a].rsplit("\n", 1)[0] if a else ""
            head += ("\n…[truncated: showed %d of %d chars]"
                     % (len(head), ln))
            r["text"] = head
    return results


def excerpts_block(results):
    """The MATERIAL EXCERPTS context block built from extract_text output."""
    used = [r for r in results
            if (r.get("text") or "").strip() and not r.get("error")]
    if not used:
        return ""
    lines = ["MATERIAL EXCERPTS — full text of the files the plan picked:"]
    for r in used:
        lines.append("\n--- %s (%s; %s%s) ---"
                     % (r["path"], r.get("kind") or "?",
                        "%d chars" % r.get("chars", 0),
                        ", truncated" if r.get("truncated") else ""))
        lines.append(r["text"].strip())
    failed = [r for r in results if r.get("error")]
    if failed:
        lines.append("NOT readable (stated honestly): "
                     + "; ".join("%s — %s" % (r["path"], r["error"])
                                 for r in failed))
    return "\n".join(lines)
