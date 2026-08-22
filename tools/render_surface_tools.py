# =============================================================================
# RENDER SURFACE TOOLS
# =============================================================================
# Lets Midum open a panel on the RIGHT HALF of the app window and fill it
# with arbitrary HTML+CSS+JS -- a dashboard after crunching some numbers, a
# gallery of images pulled from the web, a small interactive widget, a
# rendered document, anything visual the model wants to hand the user
# directly instead of describing in chat.
#
# The surface is a two-way channel, not just a display: the HTML/JS you
# write into it can send data BACK to you by calling, from inside that
# document,
#
#     parent.postMessage(<any JSON-serialisable value>, "*")
#
# -- e.g. on a form submit, a button click, a slider change, a custom
# widget's selection, anything. The desktop app's frontend catches that
# postMessage and forwards it to you as a normal new chat turn (prefixed
# "[Render Surface input]"), so you see it exactly like a message the user
# typed and can act on it -- read a submitted form's fields, respond to a
# button's payload, update the surface again via edit_render_code/
# write_render_code, whatever the input calls for. This is the ONLY path
# out of the surface for free-form input: it renders in a sandboxed iframe
# with no access to pywebview.api, cookies, localStorage, or the parent
# window, so free-form data must be posted this way.
#
# For calling Midum tools directly, every surface also gets a ready-made
# window.midum.call(name, args) function injected automatically -- no
# postMessage boilerplate required. It works like a normal async function:
#
#     const rows = await midum.call("list_directory", {path: "D:\\Notes"});
#     document.getElementById("out").textContent = rows;
#
# Call it from anywhere in your script -- on load (automatic), on a timer,
# or inside a button's onclick/a form's submit handler (on user
# interaction) -- and `await` (or `.then(...)`) its return value like any
# other function call; the resolved value can be stored in a variable,
# passed to another midum.call(), used to update the DOM, whatever the
# script needs. `name` selects which of the three tool universes to hit:
#   midum.call("some_native_tool", {...})     -- a built-in Midum tool
#   midum.call("mcp:server/tool_name", {...}) -- one tool on an MCP server
#   midum.call("flow:flow_name", {})          -- run a saved Flow end-to-end
# A bare native-looking name that isn't actually a built-in tool is still
# auto-routed to the right MCP server if exactly one connected server
# exposes a tool by that name, same as normal chat turns already do -- the
# "mcp:" prefix is for disambiguation, not strictly required. On failure
# the returned Promise rejects (catch it, or the call throws inside an
# async function) with the tool's error message. Every call -- automatic
# or interactive -- is also logged as a normal tool call in the Log pane,
# since it's real tool/MCP/Flow execution underneath, not a simulation.
#
# Opening the surface disables every OTHER openable panel (the top tab bar,
# and the Log/Parameters buttons tucked in the sidebar's Settings overlay)
# EXCEPT the sidebar itself, which stays reachable -- see the
# body.render-surface-active CSS rule and openRenderSurface()/
# closeRenderSurfaceUI() in gui/app.py.
#
# The markup only ever exists in memory: it's held in the GUI's Api object
# as a plain Python string, then pushed to a sandboxed <iframe>'s srcdoc on
# the frontend. Nothing here is ever written to a temp file or anywhere
# else on the user's disk.
#
# Like tools/user_prompt_tools.py's _gui_ask_hook, these tools do nothing on
# their own -- they're only useful once the desktop GUI installs a hook here
# at startup (see gui/app.py's Api.__init__: `_render_surface_tools._gui_render_hook = self._handle_render`).
# When running main.py standalone (no GUI attached), every call below just
# returns an explanatory error instead of doing anything.
_gui_render_hook = None

# ── Persisted Render Surfaces ──────────────────────────────────────────────
# save_render_surface/load_render_surface let the model (or the user, via
# the GUI's own picker) name and keep a snapshot of a Render Surface's
# HTML/title on disk -- unlike the live surface itself (RAM-only, wiped on
# close), a SAVED one survives across turns, sessions, and app restarts, so
# a dashboard or tool built once can be brought back up later without
# regenerating the HTML. Stored as one plain JSON file per saved surface
# under storage/render_surfaces/, named after a sanitised version of
# `name` -- looked up directly by os, so list/delete work even with no GUI
# attached; only save/load (which touch the CURRENTLY OPEN surface's
# in-memory state, or push a fresh one to the iframe) need the GUI hook.
import json
import os
import re

from config import STORAGE_DIR

RENDER_SURFACES_DIR = os.path.join(STORAGE_DIR, "render_surfaces")


def _saved_surface_path(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip()).strip("_")
    safe = safe or "untitled"
    return os.path.join(RENDER_SURFACES_DIR, f"{safe}.json")


# ── WebAPI access / Asset streaming / Asset search ─────────────────────────
# Plain native tools (no _gui_render_hook needed) meant to be called via
# window.midum.call(name, args) from WITHIN a Render Surface's own HTML/JS --
# giving the surface a way to hit external web APIs, pull an image/file from
# disk into the surface as a data URI, and search for assets already on disk,
# all without leaving the sandboxed iframe or needing a postMessage round
# trip. They work the same as any other native tool if called by the model
# directly too.
import mimetypes

try:
    import requests as _webapi_requests
    _WEBAPI_REQUESTS_AVAILABLE = True
except ImportError:
    _webapi_requests = None
    _WEBAPI_REQUESTS_AVAILABLE = False

# Directories a Render Surface can reasonably want to pull/search assets from.
_ASSET_SEARCH_DIRS = [
    RENDER_SURFACES_DIR,
    os.path.join(STORAGE_DIR, "generated_images"),
    os.path.join(STORAGE_DIR, "app_maps"),
]
_ASSET_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico",
    ".json", ".txt", ".md", ".pdf", ".mp3", ".wav", ".mp4", ".webm",
}


def webapi_fetch(url: str, method: str = "GET", headers: dict = None,
                  body: str = None, timeout: int = 20) -> str:
    """
    Make a direct HTTP request to an external web API and return the
    response as text (JSON responses are returned as-is -- parse with
    JSON.parse in your Render Surface JS). Callable by the model directly,
    or from inside a Render Surface via `await midum.call("webapi_fetch",
    {url, method, headers, body})` -- since the sandboxed iframe has no
    `fetch` access to arbitrary origins itself, this is the surface's path
    to calling any REST/JSON API on the open internet.

    `method` defaults to GET. `headers` is an optional plain object of
    header name/value pairs (e.g. an Authorization token). `body` is an
    optional raw string sent as the request body (JSON-encode it yourself
    first if the API expects JSON) -- ignored for GET/HEAD. `timeout` caps
    how long to wait, in seconds (default 20).

    Returns the response body as text, prefixed with the HTTP status code,
    or an error message if the request failed outright (network error,
    timeout, bad URL).
    """
    if not _WEBAPI_REQUESTS_AVAILABLE:
        return "[WEBAPI ERROR] 'requests' package not installed. pip install requests"

    url = (url or "").strip()
    if not url:
        return "[WEBAPI ERROR] 'url' is required."
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url

    method = (method or "GET").strip().upper()
    try:
        timeout = max(1, min(int(timeout or 20), 60))
    except (TypeError, ValueError):
        timeout = 20

    try:
        resp = _webapi_requests.request(
            method, url, headers=headers or None,
            data=body if body else None, timeout=timeout,
        )
    except Exception as e:
        return f"[WEBAPI ERROR] Request failed: {e}"

    text = resp.text or ""
    MAX_CHARS = 40000
    truncated = ""
    if len(text) > MAX_CHARS:
        truncated = f"\n\n[...truncated, {len(text)} chars total]"
        text = text[:MAX_CHARS]
    return f"[HTTP {resp.status_code}] {url}\n{text}{truncated}"


def stream_asset(path: str) -> str:
    """
    Read a local file and return it as a base64 data URI (e.g.
    'data:image/png;base64,...'), ready to drop straight into an <img src>,
    a CSS background, an <audio>/<video> tag, or a fetch()-free download
    link inside a Render Surface. Callable from inside a surface via
    `await midum.call("stream_asset", {path})` -- the sandboxed iframe
    cannot read the user's disk itself, so this is the surface's path to
    pulling a specific file in. Use search_assets first if you don't
    already know the exact path.

    Caps at 15MB to avoid choking the iframe -- for anything larger, this
    returns an error instead of a huge string.
    """
    path = (path or "").strip()
    if not path or not os.path.isfile(path):
        return f"[ASSET ERROR] File not found: {path}"

    MAX_BYTES = 15 * 1024 * 1024
    try:
        size = os.path.getsize(path)
        if size > MAX_BYTES:
            return (f"[ASSET ERROR] '{path}' is {size / 1024 / 1024:.1f}MB, "
                     f"over the 15MB streaming cap.")
        with open(path, "rb") as f:
            raw = f.read()
    except Exception as e:
        return f"[ASSET ERROR] Could not read '{path}': {e}"

    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def search_assets(query: str) -> str:
    """
    Search for asset files (images, audio, video, small text/JSON/PDF) by
    filename substring across the directories a Render Surface is likely to
    care about -- saved render surfaces, generated_images, and app_maps
    under storage/. Callable from inside a surface via
    `await midum.call("search_assets", {query})`, or by the model directly.
    Returns a plain list of full paths (with size) matching `query`,
    case-insensitively -- follow up with stream_asset(path) to actually
    pull one into the surface as a data URI.
    """
    query = (query or "").strip().lower()
    if not query:
        return "[ASSET ERROR] 'query' is required."

    matches = []
    for root_dir in _ASSET_SEARCH_DIRS:
        if not os.path.isdir(root_dir):
            continue
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in _ASSET_EXTS:
                    continue
                if query not in fname.lower():
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    size = f"{os.path.getsize(full) / 1024:.1f}KB"
                except Exception:
                    size = "?"
                matches.append((full, size))

    if not matches:
        return f"No assets matching '{query}' found under: {', '.join(_ASSET_SEARCH_DIRS)}"

    lines = [f"Assets matching '{query}' ({len(matches)} found):"]
    for full, size in matches:
        lines.append(f"  {size:>10}  {full}")
    lines.append("\nUse stream_asset(path) to pull one in as a data URI.")
    return "\n".join(lines)


try:
    from ddgs import DDGS as _AssetDDGS
    _DDGS_AVAILABLE = True
except ImportError:
    _AssetDDGS = None
    _DDGS_AVAILABLE = False

_WEB_ASSET_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp",
    ".mp3", ".wav", ".mp4", ".webm", ".pdf",
}


def search_web_assets(query: str, count: int = 8) -> str:
    """
    Search the open web for images matching `query` (via DuckDuckGo image
    search -- no API key needed) and return a numbered list of direct
    image URLs, thumbnails, sources, and dimensions. Callable from inside a
    Render Surface via `await midum.call("search_web_assets", {query})`, or
    by the model directly. This is the web counterpart to search_assets
    (which only looks at files already on disk) -- use this when you need
    an asset that doesn't exist locally yet. Follow up with
    stream_web_asset(url) to actually pull a chosen result's bytes in as a
    data URI (the surface's sandboxed iframe cannot hotlink arbitrary
    external images directly in all cases, and can't fetch() cross-origin
    at all), or just use the URL directly in an <img src> if hotlinking is
    acceptable for your use case.
    """
    if not _DDGS_AVAILABLE:
        return "[WEB ASSET ERROR] 'ddgs' package not installed. pip install ddgs"

    query = (query or "").strip()
    if not query:
        return "[WEB ASSET ERROR] 'query' is required."
    try:
        count = max(1, min(int(count or 8), 20))
    except (TypeError, ValueError):
        count = 8

    try:
        with _AssetDDGS() as ddgs:
            results = list(ddgs.images(query, max_results=count))
    except Exception as e:
        return f"[WEB ASSET ERROR] Image search failed: {e}"

    if not results:
        return f"No web image results found for '{query}'."

    lines = [f"Web image results for '{query}' ({len(results)} found):"]
    for i, r in enumerate(results):
        title  = (r.get("title") or "")[:60]
        img    = r.get("image", "")
        thumb  = r.get("thumbnail", "")
        source = r.get("source", "")
        w, h   = r.get("width", "?"), r.get("height", "?")
        lines.append(f"  [{i}] {title}  ({w}x{h}, {source})")
        lines.append(f"      image: {img}")
        if thumb and thumb != img:
            lines.append(f"      thumb: {thumb}")
    lines.append("\nUse stream_web_asset(url) to pull a chosen image's bytes in as a data URI.")
    return "\n".join(lines)


def stream_web_asset(url: str, timeout: int = 20) -> str:
    """
    Fetch an asset (image, audio, video, PDF) from a remote URL and return
    it as a base64 data URI, ready to drop straight into an <img src>, a
    CSS background, or an <audio>/<video> tag inside a Render Surface.
    Callable from inside a surface via `await midum.call("stream_web_asset",
    {url})` -- the sandboxed iframe cannot fetch() cross-origin resources
    itself, so this is the surface's path to actually pulling remote bytes
    in (as opposed to hotlinking a URL directly, which is fine when the
    source allows it but silently fails for many sites). Pair with
    search_web_assets(query) when you don't already have a URL in hand, or
    with any URL you already know (a search result, an API response, etc).

    Caps at 15MB to avoid choking the iframe -- for anything larger, this
    returns an error instead of a huge string.
    """
    if not _WEBAPI_REQUESTS_AVAILABLE:
        return "[WEB ASSET ERROR] 'requests' package not installed. pip install requests"

    url = (url or "").strip()
    if not url:
        return "[WEB ASSET ERROR] 'url' is required."
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    try:
        timeout = max(1, min(int(timeout or 20), 60))
    except (TypeError, ValueError):
        timeout = 20

    MAX_BYTES = 15 * 1024 * 1024
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        }
        resp = _webapi_requests.get(url, headers=headers, timeout=timeout, stream=True)
        resp.raise_for_status()
        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_BYTES:
            return (f"[WEB ASSET ERROR] '{url}' reports "
                     f"{int(content_length) / 1024 / 1024:.1f}MB, over the 15MB streaming cap.")
        raw = b""
        for chunk in resp.iter_content(chunk_size=65536):
            raw += chunk
            if len(raw) > MAX_BYTES:
                return f"[WEB ASSET ERROR] '{url}' exceeded the 15MB streaming cap while downloading."
    except Exception as e:
        return f"[WEB ASSET ERROR] Could not fetch '{url}': {e}"

    if not raw:
        return f"[WEB ASSET ERROR] '{url}' returned no data."

    mime = (resp.headers.get("Content-Type", "").split(";")[0].strip()
            or mimetypes.guess_type(url)[0]
            or "application/octet-stream")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def write_render_code(code: str, title: str = "") -> str:
    """
    Open (or completely replace the contents of) the Render Surface -- a
    panel occupying the right half of the screen -- with a full HTML
    document you provide. Use this to SHOW the user something visual
    instead of describing it in chat: a dashboard built from data you just
    gathered, a gallery of images, a chart, a small interactive tool, a
    formatted report, anything.

    `code` should be a complete, self-contained HTML document (it may
    include inline <style> and <script> tags; there is no separate CSS/JS
    upload -- everything goes in this one string). It renders inside a
    sandboxed iframe, so it cannot read cookies/localStorage from the rest
    of the app or navigate the parent window.

    To get input back from the surface (a form, a button, a slider, a
    custom widget's selection -- any kind of user interaction), have your
    HTML/JS call `parent.postMessage(<your data>, "*")` whenever there's
    something to send. Each call arrives back to you as a new chat turn
    (prefixed "[Render Surface input]") containing whatever you posted, so
    you can read it and respond -- including updating the surface again via
    edit_render_code/write_render_code. Without a postMessage call the
    surface never sends free-form data back to you this way.

    Separately, EVERY surface also gets a ready-made `window.midum.call(name,
    args)` function for free, no setup needed -- an ordinary-looking async
    function your HTML/JS can call directly, either automatically (on load,
    on a timer) or in response to user interaction (a button's onclick, a
    form's submit handler), and `await` its result straight into a variable:

        const result = await midum.call("search_internet", {query: "..."});
        el.textContent = result;

    `name` is a built-in Midum tool name, "mcp:server/tool_name" for one
    tool on a connected MCP server, or "flow:flow_name" to run a saved Flow
    end-to-end -- see the module docstring at the top of this file for the
    full details. This runs the real tool/MCP/Flow, synchronously from the
    surface's point of view (the Promise resolves once it finishes), and is
    independent of the postMessage channel above -- use it for pulling data
    or taking action from inside the surface, and postMessage for handing
    raw user input back to you as a chat turn.

    `title` is an optional short label shown in the panel's header bar.

    Calling this again while the surface is already open completely
    replaces whatever was there before. For a smaller, incremental change
    to what's already showing, prefer edit_render_code instead of resending
    the whole document.
    """
    if _gui_render_hook is None:
        return ("[RENDER SURFACE ERROR] No GUI attached -- this tool only "
                "works inside the Midum desktop app.")
    return _gui_render_hook("write", {"code": code, "title": title})


def edit_render_code(old_code: str, new_code: str) -> str:
    """
    Make a targeted edit to the HTML/CSS/JS currently showing in the Render
    Surface, without resending the entire document. `old_code` must be an
    EXACT, character-for-character, UNIQUE substring of what you last wrote
    with write_render_code (or the most recent edit_render_code) -- it gets
    replaced with `new_code`. If it doesn't match, or matches more than
    once, nothing is changed and you'll get an error telling you which.

    Requires the Render Surface to already be open (call write_render_code
    first). For a large-scale change, it's often simpler and more reliable
    to just call write_render_code again with the full new document.
    """
    if _gui_render_hook is None:
        return ("[RENDER SURFACE ERROR] No GUI attached -- this tool only "
                "works inside the Midum desktop app.")
    return _gui_render_hook("edit", {"old_code": old_code, "new_code": new_code})


def close_render_surface() -> str:
    """
    Close the Render Surface panel and discard the HTML/CSS/JS it was
    showing (it only ever lived in memory, so there's nothing to clean up
    on disk). Every other panel (top tabs, Log/Parameters) goes back to
    being usable normally. The user can also close it themselves at any
    time with the panel's own ✕ button.
    """
    if _gui_render_hook is None:
        return ("[RENDER SURFACE ERROR] No GUI attached -- this tool only "
                "works inside the Midum desktop app.")
    return _gui_render_hook("close", {})


def save_render_surface(name: str) -> str:
    """
    Save a snapshot of the CURRENTLY OPEN Render Surface -- its HTML/CSS/JS
    and title, exactly as they are right now -- to disk under `name`, so it
    can be brought back up later with load_render_surface(name), even in a
    future session or after the app restarts. The live surface itself stays
    open and untouched; this only writes a copy out.

    Requires a Render Surface to already be open (write_render_code first).
    Saving again under a `name` that already exists OVERWRITES the previous
    save under that name. The user can also save/load/delete these from the
    Render Surface panel's own 💾 button and the "Render Surfaces" picker in
    Settings, without going through you at all.
    """
    if _gui_render_hook is None:
        return ("[RENDER SURFACE ERROR] No GUI attached -- this tool only "
                "works inside the Midum desktop app.")
    return _gui_render_hook("save", {"name": name})


def list_saved_render_surfaces() -> str:
    """
    List every Render Surface previously saved with save_render_surface (by
    you or the user), by name, with its title and when it was saved. Follow
    up with load_render_surface(name) to bring one back up, or
    delete_saved_render_surface(name) to remove one you no longer need.
    Works even with no GUI attached, since it only reads from disk.
    """
    os.makedirs(RENDER_SURFACES_DIR, exist_ok=True)
    try:
        files = sorted(f[:-5] for f in os.listdir(RENDER_SURFACES_DIR) if f.endswith(".json"))
    except Exception as e:
        return f"[RENDER SURFACE ERROR] Could not list saved surfaces: {e}"

    if not files:
        return "No saved Render Surfaces yet. Use save_render_surface(name) while one is open."

    lines = [f"Saved Render Surfaces ({len(files)}):"]
    for safe_name in files:
        path = os.path.join(RENDER_SURFACES_DIR, f"{safe_name}.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            real_name = data.get("name") or safe_name
            title     = data.get("title") or real_name
            saved_at  = data.get("saved_at", "")
            suffix    = f", saved {saved_at}" if saved_at else ""
            lines.append(f"- {real_name}  (title: \"{title}\"{suffix})")
        except Exception:
            lines.append(f"- {safe_name}")
    lines.append("\nUse load_render_surface(name) to bring one back up.")
    return "\n".join(lines)


def load_render_surface(name: str) -> str:
    """
    Open the Render Surface using a previously SAVED snapshot (see
    save_render_surface / list_saved_render_surfaces) -- same effect as
    write_render_code, just pulling the HTML/title from disk instead of
    what you write out fresh. Completely replaces whatever is currently
    showing in the surface, same as write_render_code. Call
    list_saved_render_surfaces() first if you're not sure of the exact
    saved name.
    """
    if _gui_render_hook is None:
        return ("[RENDER SURFACE ERROR] No GUI attached -- this tool only "
                "works inside the Midum desktop app.")
    return _gui_render_hook("load", {"name": name})


def delete_saved_render_surface(name: str) -> str:
    """
    Permanently delete a saved Render Surface by name (see
    list_saved_render_surfaces). Does not affect a surface currently open
    on screen -- only the saved copy on disk. Works even with no GUI
    attached, since it only touches disk.
    """
    path = _saved_surface_path(name)
    if not os.path.exists(path):
        return f"[RENDER SURFACE ERROR] No saved Render Surface named '{name}'."
    try:
        os.remove(path)
        return f"Deleted saved Render Surface '{name}'."
    except Exception as e:
        return f"[RENDER SURFACE ERROR] Could not delete '{name}': {e}"


# =============================================================================
