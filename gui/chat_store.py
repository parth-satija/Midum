# --- AUTO-SPLITTER: imports added by automated pass, please review ---
import datetime
import json
import os
import threading

# --- from gui.py, section 1 ---
class MidumSession:
    def __init__(self):
        self.history          = []
        self.turn_counter     = 1
        self.system_prompt    = ""
        self.memory_injections = []
        self._lock            = threading.Lock()

    def initialise(self, system_prompt: str, memory_injections: list):
        with self._lock:
            self.system_prompt    = system_prompt
            self.memory_injections = memory_injections
            self.history = [{"role": "system", "content": system_prompt}]
            for inj in memory_injections:
                self.history.append({"role": "system", "content": inj})

    def reset(self):
        with self._lock:
            self.history = [{"role": "system", "content": self.system_prompt}]
            if self.memory_injections:
                self.history.append({"role": "system", "content": self.memory_injections[0]})
            self.turn_counter = 1

    def append(self, msg: dict):
        with self._lock:
            self.history.append(msg)

    def snapshot(self) -> list:
        with self._lock:
            return list(self.history)


# =============================================================================
# PERSISTENT CHAT STORAGE — one JSON file per chat under storage/chats/
# =============================================================================

# --- from gui.py, section 2 ---
class ChatStore:
    """
    Persists chat sessions to disk so they survive app restarts and can be
    reopened later. Each chat is a single JSON file named "<id>.json"
    containing:
        id            — uuid4 hex
        title         — derived from the first user message (editable later)
        created_at    — ISO timestamp, set once
        updated_at    — ISO timestamp, refreshed on every save
        history       — full LLM message history (role/content dicts) so the
                         chat can be resumed with complete context
        display       — list of (tag, text) tuples used to replay the chat
                         bubbles in the GUI without re-running anything
    """

    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, chat_id: str) -> str:
        return os.path.join(self.dir, f"{chat_id}.json")

    def list_chats(self) -> list:
        """Returns [{id, title, created_at, updated_at}, ...] newest first."""
        items = []
        with self._lock:
            try:
                fnames = os.listdir(self.dir)
            except Exception:
                fnames = []
            for fname in fnames:
                if not fname.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(self.dir, fname), "r", encoding="utf-8") as f:
                        data = json.load(f)
                    items.append({
                        "id":         data.get("id", fname[:-5]),
                        "title":      data.get("title") or "Untitled chat",
                        "created_at": data.get("created_at", ""),
                        "updated_at": data.get("updated_at", ""),
                    })
                except Exception:
                    continue
        items.sort(key=lambda x: x["updated_at"], reverse=True)
        return items

    def load(self, chat_id: str) -> dict:
        with self._lock:
            with open(self._path(chat_id), "r", encoding="utf-8") as f:
                return json.load(f)

    def save(self, chat_id: str, title: str, history: list, display_log: list) -> None:
        now = datetime.datetime.now().isoformat(timespec="seconds")
        created_at = now
        path = self._path(chat_id)
        with self._lock:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        created_at = json.load(f).get("created_at", now)
                except Exception:
                    pass
            data = {
                "id": chat_id,
                "title": title or "Untitled chat",
                "created_at": created_at,
                "updated_at": now,
                "history": history,
                "display": display_log,
            }
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)

    def delete(self, chat_id: str) -> None:
        with self._lock:
            path = self._path(chat_id)
            if os.path.exists(path):
                os.remove(path)

    def rename(self, chat_id: str, new_title: str) -> None:
        with self._lock:
            path = self._path(chat_id)
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["title"] = new_title or "Untitled chat"
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)


# =============================================================================
# CHAT HISTORY BROWSER — lists persisted chats, not a sidebar tab
# =============================================================================

