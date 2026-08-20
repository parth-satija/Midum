# --- AUTO-SPLITTER: imports added by automated pass, please review ---
from config import DOMAIN_INDEX, DOMAIN_SKILLS_INDEX, INSTRUCTIONS_FILE, PATHS_FILE, PDF_SOURCES_DIR, PDF_SOURCES_INDEX, SKILLS_DIR, SKILLS_INDEX, STORAGE_DIR
import datetime
import json
import os
import re

# --- from main.py, section 1 ---
# 4. KNOWLEDGE BASE — instructions.md, paths.md, domain files
# =============================================================================

def _ensure_kb_files():
    from tools_registry import write_local_file
    os.makedirs(STORAGE_DIR, exist_ok=True)
    if not os.path.exists(INSTRUCTIONS_FILE):
        write_local_file(INSTRUCTIONS_FILE,
            "# Midum Instructions & Preferences\n"
            "User preferences and behavioural rules.\n"
            "Format: one rule per line, starting with '- '.\n\n"
            "## Preferences\n")
    if not os.path.exists(PATHS_FILE):
        write_local_file(PATHS_FILE,
            "# Midum Paths\n"
            "Absolute paths to applications, folders and files.\n\n"
            "## Paths\n")
    if not os.path.exists(DOMAIN_INDEX):
        write_local_file(DOMAIN_INDEX,
            "# Midum Domain Knowledge Index\n"
            "Registered domain-specific knowledge files.\n"
            "Format: `filename_without_ext` - description\n\n"
            "## Files\n")
    if not os.path.exists(DOMAIN_SKILLS_INDEX):
        write_local_file(DOMAIN_SKILLS_INDEX,
            "# Midum Domain Skills Index\n"
            "Registered domain-specific skill files.\n"
            "Format: [domain] `filename_without_ext` - description\n\n"
            "## Skills\n")
    os.makedirs(PDF_SOURCES_DIR, exist_ok=True)
    if not os.path.exists(PDF_SOURCES_INDEX):
        write_local_file(PDF_SOURCES_INDEX,
            "# Midum PDF Sources Index\n"
            "Registered PDF sources (heading/sub-heading structure only, no body content).\n"
            "Format: `filename_without_ext` - description\n\n"
            "## Files\n")


def read_instructions():
    from tools_registry import read_local_file
    _ensure_kb_files()
    return read_local_file(INSTRUCTIONS_FILE)


def add_instruction(instruction):
    from tools_registry import append_local_file
    _ensure_kb_files()
    result = append_local_file(INSTRUCTIONS_FILE, f"- {instruction.strip()}")
    print(f"📌 [Instruction added]: {instruction.strip()[:80]}")
    return result


def read_paths():
    from tools_registry import read_local_file
    _ensure_kb_files()
    return read_local_file(PATHS_FILE)


def add_path(label, path, note=""):
    from tools_registry import append_local_file
    _ensure_kb_files()
    note_part = f"  _{note.strip()}_" if note.strip() else ""
    result = append_local_file(PATHS_FILE, f"- **{label.strip()}**: `{path.strip()}`{note_part}")
    print(f"📍 [Path added]: {label} -> {path}")
    return result


def create_domain_knowledge(name, description, initial_content=""):
    from tools_registry import append_local_file, write_local_file
    _ensure_kb_files()
    safe = re.sub(r'[^a-zA-Z0-9_]', '_', name.strip().lower())
    fpath = os.path.join(STORAGE_DIR, f"{safe}.md")
    if os.path.exists(fpath):
        return f"Domain knowledge '{safe}.md' already exists at {fpath}."
    header = (f"# Domain Knowledge: {safe}\n_{description.strip()}_\n\n"
              f"Created: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
    write_local_file(fpath, header + (initial_content.strip() + "\n" if initial_content.strip() else ""))
    append_local_file(DOMAIN_INDEX, f"- `{safe}` - {description.strip()}")
    print(f"📚 [Domain knowledge created]: {safe}.md")
    return f"Success: created '{safe}.md' at {fpath} and registered in domain index."


def list_domain_knowledge():
    from tools_registry import read_local_file
    _ensure_kb_files()
    return read_local_file(DOMAIN_INDEX)


def read_domain_knowledge(name):
    from tools_registry import read_local_file
    _ensure_kb_files()
    safe  = re.sub(r'[^a-zA-Z0-9_]', '_', name.strip().lower())
    fpath = os.path.join(STORAGE_DIR, f"{safe}.md")
    if not os.path.exists(fpath):
        try:
            match = next((e for e in os.listdir(STORAGE_DIR) if e.lower() == f"{safe}.md"), None)
            if match:
                fpath = os.path.join(STORAGE_DIR, match)
            else:
                return f"Domain knowledge '{safe}.md' not found. Call list_domain_knowledge."
        except Exception:
            return f"Domain knowledge '{safe}.md' not found."
    return read_local_file(fpath)


def create_domain_skill(name, domain, description, content):
    from tools_registry import append_local_file, write_local_file
    _ensure_kb_files()
    os.makedirs(SKILLS_DIR, exist_ok=True)
    safe  = re.sub(r'[^a-zA-Z0-9_]', '_', name.strip().lower())
    fpath = os.path.join(SKILLS_DIR, f"{safe}.md")
    if os.path.exists(fpath):
        return f"Domain skill '{safe}.md' already exists."
    header = (f"# Domain Skill: {safe}\n**Domain**: {domain.strip()}\n"
              f"_{description.strip()}_\n\n"
              f"Created: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n---\n\n")
    write_local_file(fpath, header + content.strip() + "\n")
    entry = f"- [{domain.strip()}] `{safe}` - {description.strip()}"
    append_local_file(DOMAIN_SKILLS_INDEX, entry)
    append_local_file(SKILLS_INDEX, entry)
    print(f"📋 [Domain skill created]: {safe}.md (domain: {domain})")
    return f"Success: created '{safe}.md' registered in both indexes."


def list_domain_skills():
    from tools_registry import read_local_file
    _ensure_kb_files()
    return read_local_file(DOMAIN_SKILLS_INDEX)


def read_domain_skill(name):
    """Read the raw markdown content of a skill file from SKILLS_DIR (the
    same files listed in the GUI's Skills tab / list_skill_files, and
    created via create_domain_skill). Unlike skills.load_skill(), this
    returns the PLAIN text with no base64/decode-instruction wrapper --
    it's meant for direct injection into a prompt (e.g. an agent's
    attached-skills context), not as a model tool-call result."""
    from tools_registry import read_local_file
    _ensure_kb_files()
    safe  = re.sub(r'[^a-zA-Z0-9_]', '_', name.strip().lower())
    fpath = os.path.join(SKILLS_DIR, f"{safe}.md")
    if not os.path.exists(fpath):
        try:
            match = next((e for e in os.listdir(SKILLS_DIR) if e.lower() == f"{safe}.md"), None)
            if match:
                fpath = os.path.join(SKILLS_DIR, match)
            else:
                return f"Skill '{safe}.md' not found. Call list_domain_skills."
        except Exception:
            return f"Skill '{safe}.md' not found."
    return read_local_file(fpath)


# =============================================================================
# 5. PDF SOURCES — no automatic structure detection at all (no pdfstructx
#    anywhere in this module). Registering a source just records its path
#    and page count via PyMuPDF (fitz); the heading hierarchy comes ONLY
#    from what the user manually tags in the PDF Heading Tagger window
#    (see gui/legacy/dialogs.py: PdfHeadingTaggerDialog), which opens the
#    real PDF, lets the user click a line of text and assign it a heading
#    level, and saves that list via set_pdf_source_headings().
# =============================================================================

def add_pdf_source(pdf_path, description=""):
    """Register a new PDF source: just its path, title (from PDF metadata
    if present) and page count, via PyMuPDF -- NOT via any structure/
    heading auto-detection. `headings` starts empty; the user tags them
    manually afterwards from the Source tab. Returns (safe_name, record)."""
    from tools_registry import append_local_file, write_local_file
    _ensure_kb_files()
    pdf_path = os.path.abspath(pdf_path.strip())
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    safe = re.sub(r'[^a-zA-Z0-9_]', '_', base.strip().lower())
    fpath = os.path.join(PDF_SOURCES_DIR, f"{safe}.json")
    if os.path.exists(fpath):
        return safe, json.loads(open(fpath, encoding="utf-8").read())

    import fitz
    doc = fitz.open(pdf_path)
    page_count = doc.page_count
    title = (doc.metadata or {}).get("title") or base
    doc.close()

    record = {
        "name": safe,
        "source_path": pdf_path,
        "description": description.strip(),
        "added": datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
        "title": title,
        "page_count": page_count,
        "headings": [],       # manually tagged: [{page, line_id, text, level}, ...]
        "part_levels": [],    # subset of tagged levels chosen as part boundaries
    }
    write_local_file(fpath, json.dumps(record, indent=2, ensure_ascii=False))
    desc_part = f" - {description.strip()}" if description.strip() else ""
    append_local_file(PDF_SOURCES_INDEX, f"- `{safe}`{desc_part} (`{pdf_path}`)")
    print(f"📄 [PDF source added]: {safe}.json ({page_count} pages, from {pdf_path}) — tag headings manually next.")
    return safe, record


def list_pdf_sources():
    """Return sorted list of registered PDF source names (without .json)."""
    _ensure_kb_files()
    names = []
    if os.path.exists(PDF_SOURCES_DIR):
        for f in os.listdir(PDF_SOURCES_DIR):
            if f.endswith(".json") and os.path.isfile(os.path.join(PDF_SOURCES_DIR, f)):
                names.append(f[:-5])
    return sorted(names)


def read_pdf_source(name):
    """Return the stored heading-hierarchy record for a registered PDF
    source (dict with title/page_count/outline/etc.), or None if missing."""
    _ensure_kb_files()
    safe = re.sub(r'[^a-zA-Z0-9_]', '_', name.strip().lower())
    fpath = os.path.join(PDF_SOURCES_DIR, f"{safe}.json")
    if not os.path.exists(fpath):
        return None
    try:
        return json.loads(open(fpath, encoding="utf-8").read())
    except Exception as e:
        return {"error": f"Failed to read PDF source '{safe}': {e}"}


def format_pdf_sources_for_prompt(names):
    """Render the user-tagged heading list of one or more registered PDF
    sources as a single markdown block, for injecting into a turn's
    prompt (KB Only mode). Skips names that can't be resolved; never
    raises. Headings here are exactly what the user manually tagged in
    the PDF Heading Tagger window -- nothing auto-detected."""
    blocks = []
    for name in names or []:
        record = read_pdf_source(name)
        if not record or "error" in record:
            continue
        title = record.get("title") or name
        page_count = record.get("page_count", "?")
        headings = sorted(record.get("headings") or [], key=lambda h: (h.get("page", 0), h.get("line_id", "")))
        if headings:
            outline_md = "\n".join(f"- (H{h.get('level')}, p.{h.get('page','?')}) {h.get('text','(untitled)')}" for h in headings)
        else:
            outline_md = "(No headings tagged yet — open the Source tab and tag headings manually.)"
        blocks.append(f"### Source: {title} ({page_count} pages)\n{outline_md}")
    return "\n\n".join(blocks)


# =============================================================================
# 6. PDF LINE-LEVEL TEXT (PyMuPDF, no pdfstructx) — used by Explain Mode
#    and as the coverage guarantee for part-building.
#
# extract_pdf_lines() opens the PDF directly with PyMuPDF (fitz) and reads
# out literally every text line on every page, in reading order, each
# tagged with a stable line_id ('p{page}_l{n}'). This is deliberately the
# most primitive possible reading of the PDF's content -- no structure
# detection, no paragraph grouping, no heading guessing. It exists so that
# (a) the PDF Heading Tagger window can show the user real lines to click
# on and tag, and (b) build_pdf_source_parts() below has a literal,
# complete line-by-line transcript to walk, so "not a single line in any
# source should be skipped" is a guarantee about this raw extraction, not
# about anyone's guess at the document's structure.
# =============================================================================

_PDF_LINES_CACHE = {}   # pdf_path -> (mtime, lines) -- avoids re-parsing on every Explain Mode turn


def _line_style_signature(spans):
    """Derive a simple, comparable formatting signature (font family name,
    size rounded to 1 decimal, bold flag) from a PyMuPDF line's spans --
    just the first span, since headings are effectively always a single
    consistent run of formatting. Used both to describe a line for the
    Heading Tagger's overlay/learning UI and to match other lines against
    a formatting the user has already tagged (see
    find_pdf_lines_matching_style below)."""
    if not spans:
        return {"font": "", "size": 0.0, "bold": False}
    span = spans[0]
    font_name = span.get("font", "") or ""
    size = round(float(span.get("size", 0) or 0), 1)
    flags = int(span.get("flags", 0) or 0)
    bold = bool(flags & (1 << 4)) or "bold" in font_name.lower()
    return {"font": font_name, "size": size, "bold": bold}


def extract_pdf_lines(pdf_path):
    """Open `pdf_path` with PyMuPDF and return a flat, ordered list of
    every non-empty text line in the document: [{page, line_id, text,
    bbox, font, size, bold}, ...], in reading order (page by page, top to
    bottom within each page). `font`/`size`/`bold` are a lightweight
    formatting signature (see _line_style_signature) used to power the
    Heading Tagger's optional "auto-detect matching formatting" feature.
    Raises on missing file / open failure."""
    import fitz
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    doc = fitz.open(pdf_path)
    lines = []
    try:
        for page_index in range(doc.page_count):
            page = doc[page_index]
            page_dict = page.get_text("dict")
            line_no = 0
            for block in page_dict.get("blocks", []):
                for line in block.get("lines", []):
                    spans = line.get("spans", [])
                    text = "".join(span.get("text", "") for span in spans).strip()
                    if not text:
                        continue
                    bbox = line.get("bbox")
                    style = _line_style_signature(spans)
                    lines.append({
                        "page": page_index + 1,
                        "line_id": f"p{page_index + 1}_l{line_no}",
                        "text": text,
                        "bbox": list(bbox) if bbox else None,
                        "font": style["font"],
                        "size": style["size"],
                        "bold": style["bold"],
                    })
                    line_no += 1
    finally:
        doc.close()
    return lines


def _cached_extract_pdf_lines(pdf_path):
    """Same as extract_pdf_lines() but keeps the last parse per path in
    memory (keyed on mtime) so Explain Mode doesn't re-read the whole PDF
    on every single turn."""
    try:
        mtime = os.path.getmtime(pdf_path)
    except OSError:
        mtime = None
    cached = _PDF_LINES_CACHE.get(pdf_path)
    if cached and cached[0] == mtime:
        return cached[1]
    lines = extract_pdf_lines(pdf_path)
    _PDF_LINES_CACHE[pdf_path] = (mtime, lines)
    return lines


def find_pdf_lines_matching_style(pdf_path, style, exclude_keys=None, size_tolerance=0.6):
    """Scan the WHOLE document (every page, via the cached line
    extraction above -- not just one page) for every line whose
    formatting signature approximately matches `style`
    ({"font": str, "size": float, "bold": bool}), as
    [{"page": int, "line_id": str, "text": str}, ...].

    Powers the Heading Tagger's optional "auto-detect matching
    formatting": once the user tags one line with a heading level, its
    formatting is captured as `style` and this call finds every other
    line in the document that looks the same, so they can all be
    pre-tagged at the same level automatically. `exclude_keys` is an
    optional set of (page, line_id) tuples to skip -- typically lines
    already tagged, so the caller only gets NEW suggestions back.

    Matching is approximate on purpose (PDFs sometimes report the same
    visual heading at very slightly different sizes across pages): bold
    must match exactly, size must be within `size_tolerance` points, and
    font family only has to match if `style` actually specifies one.
    Never raises -- returns [] if the style has no usable size or the PDF
    can't be read."""
    target_size = (style or {}).get("size")
    if target_size is None:
        return []
    target_font = (style or {}).get("font") or ""
    target_bold = bool((style or {}).get("bold"))
    exclude_keys = exclude_keys or set()
    try:
        lines = _cached_extract_pdf_lines(pdf_path)
    except Exception:
        return []
    matches = []
    for line in lines:
        key = (line["page"], line["line_id"])
        if key in exclude_keys:
            continue
        if bool(line.get("bold")) != target_bold:
            continue
        if abs(float(line.get("size") or 0) - float(target_size)) > size_tolerance:
            continue
        if target_font and line.get("font") != target_font:
            continue
        matches.append({"page": line["page"], "line_id": line["line_id"], "text": line["text"]})
    return matches


# =============================================================================
# 6b. PART SELECTION — user-chosen heading levels that define "parts" for
#    Explain Mode, set ONCE per source (not per heading) from the GUI's
#    Source tab.
#
# A "part" is a contiguous run of the source's flat, ordered section list
# that starts at a heading whose level is one of the user's selected
# levels. If several selected levels are nested inside one another (e.g.
# the user picked both H1 and H3), the ACTIVE part at any point in the
# document is always the most recently opened selected heading — i.e. the
# lowest (most specific/deepest) selected heading level for that stretch
# of text, exactly matching how a reader would expect "part" boundaries
# to nest. Every section's text is attached to exactly one part and
# nothing is ever dropped: if a heading isn't itself a selected level, its
# text is simply folded into whichever part is currently open, and the
# very first section always opens part 1 even if its own level was not
# selected — so the walk covers the source from its first line to its
# last with no gaps.
# =============================================================================

def get_pdf_source_available_levels(name):
    """Return the sorted list of heading levels the user has actually
    tagged for this source (e.g. [1, 2, 3]), for the GUI to render one
    checkbox per real tagged level instead of a blind H1-H6 guess."""
    record = read_pdf_source(name)
    if not record or "error" in record:
        return []
    return sorted({h.get("level") for h in (record.get("headings") or []) if h.get("level")})


def _heading_sort_key(h):
    try:
        _, lpart = h["line_id"].split("_l")
        return (h.get("page", 0), int(lpart))
    except Exception:
        return (h.get("page", 0), 0)


def set_pdf_source_headings(name, headings):
    """
    Persist the user's manually-tagged heading lines for this source —
    overwrites any previous tagging. `headings` is a list of
    {page, line_id, text, level} dicts, exactly as produced by the PDF
    Heading Tagger window (each one a line the user clicked on and
    assigned a heading level to). Returns a confirmation string, or an
    error string if the source isn't found.
    """
    _ensure_kb_files()
    safe = re.sub(r'[^a-zA-Z0-9_]', '_', name.strip().lower())
    fpath = os.path.join(PDF_SOURCES_DIR, f"{safe}.json")
    if not os.path.exists(fpath):
        return f"PDF source '{safe}.json' not found. Call list_pdf_sources."
    try:
        record = json.loads(open(fpath, encoding="utf-8").read())
    except Exception as e:
        return f"Failed to read PDF source '{safe}': {e}"

    clean = []
    for h in headings or []:
        try:
            level = int(h.get("level"))
        except (TypeError, ValueError):
            continue
        line_id = h.get("line_id")
        if not line_id:
            continue
        clean.append({
            "page": int(h.get("page", 0) or 0),
            "line_id": line_id,
            "text": (h.get("text") or "").strip(),
            "level": level,
        })
    clean.sort(key=_heading_sort_key)

    record["headings"] = clean
    # drop any previously-selected part levels that are no longer tagged at all
    tagged_levels = {h["level"] for h in clean}
    record["part_levels"] = sorted(l for l in (record.get("part_levels") or []) if l in tagged_levels)

    from tools_registry import write_local_file
    write_local_file(fpath, json.dumps(record, indent=2, ensure_ascii=False))
    print(f"🏷️ [PDF source headings tagged]: {safe} -> {len(clean)} heading(s)")
    return f"Success: saved {len(clean)} manually-tagged heading(s) for '{safe}'."


def set_pdf_source_part_levels(name, levels):
    """
    Persist the heading levels (list of ints, e.g. [1, 3]) the user picked
    as "part" boundaries for this source, set once for the whole source
    (not per heading). Overwrites any previous selection. Returns a
    confirmation string, or an error string if the source isn't found.
    """
    _ensure_kb_files()
    safe = re.sub(r'[^a-zA-Z0-9_]', '_', name.strip().lower())
    fpath = os.path.join(PDF_SOURCES_DIR, f"{safe}.json")
    if not os.path.exists(fpath):
        return f"PDF source '{safe}.json' not found. Call list_pdf_sources."
    try:
        record = json.loads(open(fpath, encoding="utf-8").read())
    except Exception as e:
        return f"Failed to read PDF source '{safe}': {e}"

    clean_levels = sorted({int(l) for l in (levels or []) if str(l).strip()})
    record["part_levels"] = clean_levels
    from tools_registry import write_local_file
    write_local_file(fpath, json.dumps(record, indent=2, ensure_ascii=False))
    print(f"🧩 [PDF source part levels set]: {safe} -> {clean_levels or '(none — whole doc is one part)'}")
    return f"Success: '{safe}' will now be split into parts at heading level(s) {clean_levels or '(none — treated as a single part)'}."


def get_pdf_source_part_levels(name):
    """Return the list of heading levels registered for this source (may be empty)."""
    record = read_pdf_source(name)
    if not record or "error" in record:
        return []
    return sorted(record.get("part_levels") or [])


def build_pdf_source_parts(name):
    """
    Split a registered PDF source's full, literal line-by-line text (see
    extract_pdf_lines) into an ordered list of "parts", using ONLY the
    user's own manually-tagged headings restricted to the saved
    part_levels. Returns a list of dicts:
        {"heading": str, "level": int|None, "page": int, "sections": [...]}
    where `sections` is the exact slice of the flat {page, line_id, text}
    line list that belongs to that part, in original document order.

    Guarantees full coverage: EVERY line from the source's raw PyMuPDF
    extraction is placed into exactly one part, in order — nothing is
    ever skipped or duplicated, regardless of which levels were selected
    (including none, or a source with no tagged headings at all — then
    the whole document is simply one part). Returns [] if the source
    can't be resolved.
    """
    record = read_pdf_source(name)
    if not record or "error" in record:
        return []
    source_path = record.get("source_path")
    if not source_path or not os.path.exists(source_path):
        return []
    try:
        lines = _cached_extract_pdf_lines(source_path)
    except Exception:
        return []
    if not lines:
        return []

    heading_by_key = {(h.get("page"), h.get("line_id")): h for h in (record.get("headings") or [])}
    part_levels = set(record.get("part_levels") or [])

    parts = []
    current = None
    for line in lines:
        tagged = heading_by_key.get((line["page"], line["line_id"]))
        is_boundary = current is None or (
            tagged is not None and (not part_levels or tagged["level"] in part_levels)
        )
        if is_boundary:
            current = {
                "heading": tagged["text"] if tagged else (line["text"][:100] or "(untitled)"),
                "level": tagged["level"] if tagged else None,
                "page": line["page"],
                "sections": [],
            }
            parts.append(current)
        current["sections"].append(line)
    return parts


def list_pdf_source_parts(name):
    """
    Return a numbered, human-readable index of the parts built from this
    source's saved part_levels — for the runtime to tell the model which
    parts exist, and for the GUI/tools to pick one by index. Format:
    'IDX | Level | Page | Heading'. Returns a message if no source/levels.
    """
    record = read_pdf_source(name)
    if not record or "error" in record:
        return f"PDF source '{name}' not found. Call list_pdf_sources."
    parts = build_pdf_source_parts(name)
    if not parts:
        return f"No extractable parts found for '{name}'."
    levels = record.get("part_levels") or []
    header = (
        f"Source: {record.get('title') or name} — {len(parts)} part(s)"
        + (f", split at heading level(s) {sorted(levels)}" if levels
           else " (no part levels set — whole source is one part; "
                "set part levels from the Source tab for finer-grained Explain Mode)")
        + "\n"
    )
    lines = [header, "IDX | LEVEL | PAGE | HEADING"]
    for i, p in enumerate(parts):
        lvl = f"H{p['level']}" if p.get("level") else "-"
        lines.append(f"{i} | {lvl} | {p.get('page', '?')} | {p.get('heading', '(untitled)')}")
    return "\n".join(lines)


def format_pdf_source_part_for_prompt(name, part_index):
    """
    Render ONE part (by index into list_pdf_source_parts/build_pdf_source_parts)
    as a markdown block for Explain Mode, with an explicit header telling
    the model exactly which part of the source it is now explaining and
    how many parts remain — this is the "runtime clarifies to the model
    what part they have to explain" step. Every heading in the part is
    followed immediately by its own full paragraph text, in flat
    top-to-bottom order, so nothing inside the part is skipped or
    summarized away. Returns an error string if name/index don't resolve.
    """
    record = read_pdf_source(name)
    if not record or "error" in record:
        return f"PDF source '{name}' not found. Call list_pdf_sources."
    parts = build_pdf_source_parts(name)
    if not parts:
        return f"No extractable parts found for '{name}'."
    if not (0 <= part_index < len(parts)):
        return f"Part index {part_index} out of range — '{name}' has {len(parts)} part(s) (0-{len(parts)-1})."

    part = parts[part_index]
    title = record.get("title") or name
    level = part.get("level")
    hashes = "#" * min(max(level, 1), 6) if level else "##"
    body = "\n".join(l["text"] for l in part["sections"])
    body_block = f"{hashes} {part['heading']} (p.{part.get('page', '?')})\n{body or '(No text found in this part.)'}"

    header = (
        f"### EXPLAIN MODE — Source: {title}\n"
        f"You are now explaining PART {part_index + 1} of {len(parts)}: "
        f"\"{part['heading']}\" (starting p.{part.get('page', '?')}).\n"
        f"Explain this part as flowing, connected concepts, not a line-by-line "
        f"reading — but every detail below (names, numbers, terms, examples, "
        f"minor points included) must surface somewhere in your explanation "
        f"before moving on. Don't skip, summarize away, or guess at content not "
        f"shown below. "
        + (f"After this, {len(parts) - part_index - 1} part(s) remain."
           if part_index + 1 < len(parts) else "This is the LAST part of this source.")
        + "\n\n"
    )
    return header + body_block


def _build_full_text_with_page_markers(record):
    """Like format_pdf_sources_full_text_for_prompt, but for a single
    already-loaded source record, with an explicit '[--- PAGE N ---]'
    marker inserted every time the page number changes. Used by
    Page-by-Page Explain Mode so the model can see exactly where page
    boundaries fall within the full text, instead of only being told a
    bare page number with no way to locate it in the text itself."""
    source_path = record.get("source_path")
    if not source_path or not os.path.exists(source_path):
        return ""
    try:
        lines = _cached_extract_pdf_lines(source_path)
    except Exception:
        return ""
    if not lines:
        return ""
    out = []
    current_page = None
    for line in lines:
        if line["page"] != current_page:
            current_page = line["page"]
            out.append(f"\n[--- PAGE {current_page} ---]\n")
        out.append(line["text"])
    return "\n".join(out)


def format_pdf_source_page_for_prompt(name, page_number):
    """
    Page-by-Page counterpart to format_pdf_source_part_for_prompt(). Renders
    ONE registered PDF source as a markdown block for Explain Mode: the FULL
    text of the source (with '[--- PAGE N ---]' markers so page boundaries
    are visible), plus an explicit header telling the model exactly which
    page it must explain right now and how many remain.

    Unlike Part-by-Part mode, this needs NO manually-tagged headings and NO
    part-level selection at all -- pages are an inherent property of the PDF
    itself (page_count is already known the moment add_pdf_source() runs),
    so Page-by-Page mode works immediately on any registered source.

    A little forward bleed into the next page (briefly finishing a
    sentence/paragraph that spans the page boundary) or leaving a small
    trailing bit of the current page for next time is expected and fine --
    explaining content that clearly belongs to other pages is not. Returns
    an error string if name/source can't be resolved.
    """
    record = read_pdf_source(name)
    if not record or "error" in record:
        return f"PDF source '{name}' not found. Call list_pdf_sources."
    page_count = record.get("page_count") or 0
    if page_count <= 0:
        return f"PDF source '{name}' has no known page count."
    try:
        page_number = int(page_number)
    except (TypeError, ValueError):
        page_number = 1
    page_number = max(1, min(page_number, page_count))

    title = record.get("title") or name
    full_text = _build_full_text_with_page_markers(record)
    if not full_text:
        full_text = "(No extractable text found in this PDF.)"

    header = (
        f"### EXPLAIN MODE (Page-by-Page) \u2014 Source: {title}\n"
        f"You are now explaining PAGE {page_number} of {page_count}. "
        f"The FULL text of this source is included below, with "
        f"'[--- PAGE N ---]' markers showing exactly where each page starts, "
        f"so you always have complete context -- but explain ONLY page "
        f"{page_number} right now. Don't jump ahead to later pages or "
        f"re-explain earlier ones unless the user asks. If a paragraph or "
        f"idea clearly spans the boundary into the next page, it's fine to "
        f"briefly finish that thought, or to leave a small amount of it for "
        f"the next page -- don't force an artificial hard cut mid-sentence."
        + (f" After this, {page_count - page_number} page(s) remain."
           if page_number < page_count else " This is the LAST page of this source.")
        + "\n\n"
    )
    return header + full_text


def format_pdf_sources_full_text_for_prompt(names):
    """Render the FULL raw line-by-line text of one or more registered
    PDF sources for Explain Mode -- literally every line PyMuPDF read
    from the PDF, in document order, no heading-based grouping. Skips
    names that can't be resolved or whose source file has moved/been
    deleted; never raises."""
    blocks = []
    for name in names or []:
        record = read_pdf_source(name)
        if not record or "error" in record:
            continue
        source_path = record.get("source_path")
        if not source_path or not os.path.exists(source_path):
            continue
        try:
            lines = _cached_extract_pdf_lines(source_path)
        except Exception:
            continue
        title = record.get("title") or name
        page_count = record.get("page_count", "?")
        body = "\n".join(l["text"] for l in lines)
        block = f"### SOURCE: {title} ({page_count} pages)\n\n" + (body or "(No extractable text found in this PDF.)")
        blocks.append(block)
    return "\n\n---\n\n".join(blocks)


# =============================================================================

