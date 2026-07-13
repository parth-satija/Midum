# --- from main.py, section 1 ---
# 1. TOOL SCHEMAS
# =============================================================================

tools = [
    {
        "type": "function",
        "function": {
            "name": "manual_scan_app_layouts",
            "description": "Scan an active window to find its major layout containers (subtrees). Use this first when exploring a new app.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_title": {"type": "string", "description": "Exact title of the window (e.g., 'Gemini')."}
                },
                "required": ["window_title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manual_inspect_app_subtree",
            "description": "Scan a specific layout container (found via manual_scan_app_layouts) to reveal the interactive buttons and text fields inside it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_title": {"type": "string"},
                    "subtree_key": {"type": "string", "description": "The name or automation_id of the container."}
                },
                "required": ["window_title", "subtree_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_ui_element",
            "description": (
                "PREFERRED tool for interacting with desktop app UI elements (buttons, "
                "fields, menu items, close/minimize buttons, etc). ONE call does "
                "everything: finds the window, searches its ENTIRE UI tree for an "
                "element matching your plain-English description, and acts on it. "
                "Supports normal app windows AND Windows shell surfaces — pass "
                "window_title as 'taskbar', 'start', 'tray', 'desktop', "
                "'action center', 'search', or 'tray overflow' to interact with "
                "the Start button, system tray, notification area, clock, Wi-Fi, "
                "volume, battery, and other shell controls that have no normal title. "
                "Examples: click_ui_element('taskbar', 'Start') clicks the Start button. "
                "click_ui_element('tray', 'Wi-Fi') clicks the Wi-Fi tray icon. "
                "click_ui_element('tray overflow', 'Show hidden icons') opens the overflow. "
                "If this returns 'No element matched', it shows all available element "
                "names grouped by type — retry with one of those exact names. "
                "If it says canvas/WebGL, use fallback_click_text instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_title": {
                        "type": "string",
                        "description": (
                            "Window title or substring, OR a shell surface alias: "
                            "'taskbar', 'start', 'tray', 'desktop', 'action center', "
                            "'search', 'tray overflow', 'secondary taskbar'."
                        )
                    },
                    "description": {
                        "type": "string",
                        "description": "Plain-English description of the element, e.g. 'Close button', 'Send message', 'Start button', 'Wi-Fi'."
                    },
                    "action": {
                        "type": "string",
                        "description": "'click' (default), 'set_text', or 'get_text'.",
                        "enum": ["click", "set_text", "get_text"]
                    },
                    "text_to_type": {
                        "type": "string",
                        "description": "Required if action is 'set_text' — the text to enter."
                    }
                },
                "required": ["window_title", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot",
            "description": (
                "ONE tool that snapshots ALL visible, interactive elements into a numbered "
                "indexed table — works IDENTICALLY on a desktop app window (Discord, VS Code, "
                "Settings, any app) AND a browser tab. It is NOT browser-only. "
                "Returns: IDX | TYPE | NAME/LABEL | STATUS. "
                "Follow up with act(index, action) to interact by index — exact, never wrong. "
                "\n\n"
                "For DESKTOP WINDOWS: pass target='<window title>' e.g. target='Google Chrome', "
                "target='Visual Studio Code', target='taskbar', target='tray'. "
                "Uses UIA (Windows UI Automation) to read the app's accessibility tree. "
                "\n\n"
                "For BROWSER PAGES: pass target='browser' (reads the active tab) or "
                "target='browser:N' (reads tab N). "
                "Uses CDP (Chrome DevTools Protocol) to read the live DOM. "
                "Requires Chrome with --remote-debugging-port=9222. "
                "\n\n"
                "filter_type: optional, e.g. 'button', 'edit', 'link', 'input', 'tab', 'menuitem'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "What to snapshot. "
                            "Desktop: canonical window title or shell alias ('Google Chrome', 'taskbar', 'tray'). "
                            "Browser page: 'browser' for active tab, 'browser:1' for tab 1."
                        )
                    },
                    "filter_type": {
                        "type": "string",
                        "description": "Optional type filter: 'button', 'edit', 'link', 'input', 'tab', 'menuitem', 'checkbox'."
                    }
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "act",
            "description": (
                "Interact with an element from the last snapshot() call by its index. "
                "Works for both desktop windows and browser pages — same tool, same syntax. "
                "The index is exact: no scoring, no fuzzy matching, no wrong element. "
                "Always call snapshot(target) first, then act(index). "
                "\n\n"
                "action: 'click' (default), 'set_text' (type into a field), 'get_text'. "
                "target: same value you passed to snapshot() — needed to look up the right cache."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Same target you passed to snapshot(). E.g. 'Google Chrome' or 'browser'."
                    },
                    "index": {
                        "type": "integer",
                        "description": "Element index from the snapshot() output."
                    },
                    "action": {
                        "type": "string",
                        "enum": ["click", "set_text", "get_text"],
                        "description": "'click' (default), 'set_text', or 'get_text'."
                    },
                    "text_to_type": {
                        "type": "string",
                        "description": "Required if action is 'set_text'."
                    }
                },
                "required": ["target", "index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manual_interact_with_ui",
            "description": (
                "Most tasks should use click_ui_element instead — it's a single call and "
                "has automatic coordinate fallback. Only use this if click_ui_element "
                "failed and you have an EXACT automation_id from manual_inspect_app_subtree."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_title": {"type": "string"},
                    "control_type": {"type": "string", "description": "'button', 'edit', 'text', etc."},
                    "search_property": {"type": "string", "description": "'automation_id', 'name', or 'class_name'"},
                    "property_value": {"type": "string", "description": "The target identifier value."},
                    "action": {"type": "string", "description": "'click', 'set_text', or 'get_text'"},
                    "text_to_type": {"type": "string", "description": "Only required if action is 'set_text'."}
                },
                "required": ["window_title", "control_type", "search_property", "property_value", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_gemini_app",
            "description": (
                "Sends a prompt to Gemini via the actual WEB CHAT INTERFACE "
                "(https://gemini.google.com/app) using the community `gemini_webapi` "
                "library — NOT a browser automation, NOT a desktop application, and NOT "
                "the metered developer API. It talks directly to Gemini's internal web "
                "endpoints using session cookies, so there's no page to load or button "
                "to click — just a plain request. Uses the free web app's own usage "
                "limits. Returns ONLY the single newest reply, never the full "
                "conversation history. Prefer consult_gemini for most cases — it already "
                "routes through this same mechanism, with no fallback to the metered API."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The complex query, data processing prompt, or reasoning task to send to Gemini."
                    }
                },
                "required": ["prompt"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_gemini_chat",
            "description": "Manage the Gemini application state by performing actions like starting a new chat or selecting a recent chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["new_chat", "open_recent"],
                        "description": "The action to perform."
                    },
                    "chat_name": {
                        "type": "string",
                        "description": "Required if action is 'open_recent'. The specific name of the chat to open."
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_active_windows",
            "description": "List all currently open and visible window titles on the desktop. Use this if you are unsure of the exact window_title to pass to the UI interaction tools.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_aggregated_text",
            "description": "Read text from a window or specific container by merging sibling TextControl elements into readable paragraphs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_title": {"type": "string"},
                    "container_key": {"type": "string", "description": "Optional: Specific subtree key to read from."}
                },
                "required": ["window_title"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_local_file",
            "description": "Read the contents of a local file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative path to the file."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_local_file",
            "description": "Completely overwrite or clear a file. Pass '' to wipe it clean.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_local_file",
            "description": "Add content to the end of a file without modifying existing content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_terminal_command",
            "description": (
                "Run a PowerShell command on the local Windows machine. "
                "To open an app: ALWAYS use Start-Process with the full path from paths.md. "
                "Example: Start-Process 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'. "
                "NEVER use 'start chrome', 'start firefox', or any shorthand — always the full path. "
                "If you do not know the path, call read_paths first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command":           {"type": "string", "description": "The exact PowerShell command to execute."},
                    "working_directory": {"type": "string", "description": "Optional: absolute folder path to run from."}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_internet",
            "description": "Search the internet for real-time information or documentation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fallback_view_screen",
            "description": (
                "Capture a live screenshot of the desktop, downscaled to canvas size with a "
                "coordinate grid burned in. Use this when you need to visually inspect the screen "
                "or when you need to identify coordinates for fallback_click_grid. "
                "For text-heavy GUIs, prefer fallback_find_text over reading the grid manually."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fallback_find_text",
            "description": (
                "Use OCR (Tesseract) to locate a text string on the current screen. "
                "Returns the canvas coordinates of the best match and a full list of all detected "
                "text with their positions. Use this instead of reading the grid image when you "
                "want to click a button, label, or menu item that has visible text — it is faster "
                "and more accurate than visual grid estimation. "
                "Pass the exact text or a substring of it. Case-insensitive."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text string to search for on screen. Case-insensitive substring match."
                    }
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fallback_click_grid",
            "description": (
                "Simulate a mouse click at canvas coordinates from the grid screenshot. "
                "Python scales these to real screen pixels automatically. "
                "Use this when you have read coordinates from the fallback_view_screen grid. "
                "For clicking text elements, prefer fallback_find_text which gives you "
                "precise coordinates without needing to read the grid."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Canvas x-coordinate from the grid screenshot."},
                    "y": {"type": "integer", "description": "Canvas y-coordinate from the grid screenshot."},
                    "click_type": {
                        "type": "string",
                        "description": "'left_click' (default), 'right_click', or 'double_click'.",
                        "enum": ["left_click", "right_click", "double_click"]
                    }
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fallback_click_text",
            "description": (
                "Find a text element on screen using OCR and click it in one step. "
                "This is the most accurate way to click buttons, menu items, and labels. "
                "Use this whenever the element you want to click has readable text. "
                "If multiple matches exist, clicks the one with the highest OCR confidence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The visible text of the element to click. Case-insensitive substring match."
                    },
                    "click_type": {
                        "type": "string",
                        "description": "'left_click' (default), 'right_click', or 'double_click'.",
                        "enum": ["left_click", "right_click", "double_click"]
                    }
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": (
                "Type text at the current cursor position using keyboard simulation. "
                "Always set expected_window to the title of the window you just clicked — "
                "this prevents accidentally typing into the wrong app (e.g. Midum's own terminal). "
                "Use special_key for Enter, Tab, Escape, F-keys etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text":            {"type": "string", "description": "The text to type."},
                    "special_key":     {"type": "string", "description": "Optional key to press after typing: 'Enter', 'Tab', 'Escape', 'F5', etc."},
                    "expected_window": {"type": "string", "description": "Title substring of the window that should be in the foreground. Typing is aborted if a different window is active."}
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_memory",
            "description": "Persist important information. 'target' must be 'master', 'project', or 'session'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target":  {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["target", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_current_goal",
            "description": "Update the current goal. Use goal='none' to clear when a task is done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal":   {"type": "string"},
                    "reason": {"type": "string"}
                },
                "required": ["goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "Load a skill file by name (without .md). Call list_skills first if unsure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string"}
                },
                "required": ["skill_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "List all available skills with descriptions.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_instructions",
            "description": (
                "Read instructions.md — user preferences and behavioural rules "
                "(e.g. preferred command style, formatting, workflow habits). "
                "Consult before any task where HOW matters, not just what to do."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_instruction",
            "description": (
                "Add a preference or behavioural rule to instructions.md. "
                "Call when the user states a preference or corrects your behaviour."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {"type": "string", "description": "The rule to record."}
                },
                "required": ["instruction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_paths",
            "description": (
                "Read paths.md — absolute paths to apps, folders, and files. "
                "Consult when you need a path you are not certain of."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_path",
            "description": "Add a labelled path entry to paths.md.",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Short name, e.g. 'Blender'."},
                    "path":  {"type": "string", "description": "Absolute path on disk."},
                    "note":  {"type": "string", "description": "Optional extra context."}
                },
                "required": ["label", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_domain_knowledge",
            "description": (
                "Create a domain-specific knowledge file (like commands.md but for a specific "
                "tool, e.g. blender_commands.md). Registered in domain_index.md."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name":            {"type": "string", "description": "snake_case name, no extension."},
                    "description":     {"type": "string", "description": "One-line description."},
                    "initial_content": {"type": "string", "description": "Optional seed content."}
                },
                "required": ["name", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_domain_knowledge",
            "description": "List all registered domain knowledge files with descriptions.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_domain_knowledge",
            "description": "Read a domain knowledge file by name (without .md extension).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Filename without extension."}
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_domain_skill",
            "description": (
                "Create a domain-specific skill file for a tool or workflow. "
                "Stored in skills dir, registered in domain_skills_index.md and skills.md."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name":        {"type": "string", "description": "snake_case filename, no extension."},
                    "domain":      {"type": "string", "description": "Tool this belongs to, e.g. 'blender'."},
                    "description": {"type": "string", "description": "One-line description."},
                    "content":     {"type": "string", "description": "Full Markdown skill instructions."}
                },
                "required": ["name", "domain", "description", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_domain_skills",
            "description": "List all domain-specific skills grouped by domain.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consult_gemini",
            "description": (
                "DEFAULT consult tool — try this FIRST for reasoning/analysis/research tasks. "
                "Send a reasoning, analysis, or research task to a Gemini model via Google AI Studio. "
                "Use this when: (1) the user explicitly says 'ask Gemini' or 'consult Gemini', "
                "(2) the task requires deep reasoning, complex analysis, code review, architectural "
                "decisions, or multi-step planning that exceeds your own confident ability, "
                "(3) you need a second opinion or want to cross-check your own reasoning. "
                "Midum selects the most appropriate model automatically based on task complexity "
                "unless you specify task_type. "
                "Models available (free tier): "
                "quick=gemini-2.0-flash-lite (fast, simple tasks), "
                "balanced=gemini-2.0-flash (default, multi-step reasoning), "
                "hard=gemini-2.5-flash (complex analysis, long context), "
                "expert=gemini-2.5-pro (hardest problems, slowest)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The full question or task for Gemini. Be specific and complete."
                    },
                    "task_type": {
                        "type": "string",
                        "description": "Model tier: 'auto' (default), 'quick', 'balanced', 'hard', or 'expert'."
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional: relevant context to include (file contents, memory, prior results)."
                    }
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consult_openrouter",
            "description": (
                "Send a reasoning, analysis, or research task to a model on OpenRouter "
                "(currently configured: see OPENROUTER_MODEL). "
                "This is a FALLBACK/explicit-request tool, not a default choice — prefer "
                "consult_gemini first. Only use consult_openrouter when: "
                "(1) the user explicitly says 'ask OpenRouter' or names OpenRouter, or "
                "(2) consult_gemini just failed/errored and you still need a second-brain "
                "answer. Do not reach for this casually as an alternative to Gemini."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The full question or task for the model. Be specific and complete."
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional: relevant context to include (file contents, memory, prior results)."
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional: override the default OpenRouter model ID for this call."
                    }
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_openrouter",
            "description": (
                "Hand off an entire task to OpenRouter as a real COWORKER, not just a "
                "text consultant. Unlike consult_openrouter (which only returns text), "
                "the OpenRouter model spun up here gets FULL access to every tool Midum "
                "has — it can click UI elements, run terminal commands, read/write files, "
                "browse the web, everything — and works through the task independently, "
                "then reports back a final summary that you relay to the user. "
                "\n\n"
                "Use this when a sub-task is complex enough to benefit from a stronger "
                "model actually DOING the work end-to-end (not just planning it), or "
                "when the user explicitly asks to delegate/offload something. "
                "\n\n"
                "This runs in an ISOLATED conversation — the delegate does not see your "
                "conversation history, only what you put in `task` and `context`. Be "
                "complete and specific in both."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "The complete task to hand off, written as a self-contained "
                            "instruction — the delegate has no other context except this "
                            "and whatever you put in `context`."
                        )
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional: relevant background the delegate needs (file contents, prior findings, constraints)."
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional: override the default OpenRouter model ID for this delegated task."
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": "Optional: cap on tool-call steps the delegate can take before it must report back. Default 10."
                    }
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_openrouter_models",
            "description": (
                "List models available on OpenRouter as a numbered indexed table "
                "(IDX | context length | free? | model ID). Defaults to free-tier models only. "
                "Follow up with set_openrouter_model_by_index(index) to switch the active model — "
                "this is the CHOOSE-pattern way to pick a model instead of typing an exact ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "free_only": {
                        "type": "boolean",
                        "description": "If true (default), only show free-tier models. Set false to see all models including paid."
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_openrouter_model_by_index",
            "description": "Switch the active OpenRouter model to the entry at `index` from list_openrouter_models.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Index from list_openrouter_models output."}
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_openrouter_model",
            "description": (
                "Switch the active OpenRouter model directly by its exact model ID "
                "(e.g. 'anthropic/claude-3.7-sonnet', 'meta-llama/llama-3.3-70b-instruct:free'). "
                "Use list_openrouter_models + set_openrouter_model_by_index instead if you don't "
                "know the exact ID. Takes effect immediately, no restart needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model_id": {"type": "string", "description": "Exact OpenRouter model ID."}
                },
                "required": ["model_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consult_gemini_api",
            "description": (
                "Send a plain reasoning/planning prompt to Gemini via the OFFICIAL "
                "Google Gemini API (real API key, structured request — not the web-chat "
                "scraping used by consult_gemini). Returns text only, no tool access — "
                "use delegate_to_gemini_api instead if the task needs real actions taken."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "The question or planning prompt."},
                    "context": {"type": "string", "description": "Optional background context."},
                    "model": {
                        "type": "string",
                        "description": "Optional: override the default Gemini API model ID for this call."
                    }
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_gemini_api",
            "description": (
                "Hand off an entire task to the OFFICIAL Gemini API as a real COWORKER, "
                "not just a text consultant — same idea as delegate_to_openrouter but "
                "running on Gemini through a real API key with native structured tool "
                "calling (not the web-chat session used by delegate_to_gemini_web). The "
                "Gemini sub-agent gets FULL access to every tool Midum has — UI "
                "automation, terminal, files, browser, MCP servers, everything — and "
                "works through the task independently in an ISOLATED conversation, then "
                "reports back a final summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "The complete task to hand off, written as a self-contained "
                            "instruction — the delegate has no other context except this "
                            "and whatever you put in `context`."
                        )
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional: relevant background the delegate needs (file contents, prior findings, constraints)."
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional: override the default Gemini API model ID for this delegated task."
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": "Optional: cap on tool-call steps the delegate can take before it must report back. Default 10."
                    }
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_gemini_api_model",
            "description": (
                "Switch the active Gemini API model directly by its exact model ID "
                "(e.g. 'gemini-3.1-flash-lite'). Takes effect immediately, no restart needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model_id": {"type": "string", "description": "Exact Gemini API model ID."}
                },
                "required": ["model_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consult_groq",
            "description": (
                "Send a reasoning, analysis, or research task to a model on GroqCloud "
                "(currently configured: see GROQ_MODEL) — GroqCloud's free tier gives "
                "fast inference with no credit card required. "
                "This is a FALLBACK/explicit-request tool, not a default choice — prefer "
                "consult_gemini first. Only use consult_groq when: "
                "(1) the user explicitly says 'ask Groq' or names GroqCloud, or "
                "(2) consult_gemini just failed/errored and you still need a second-brain "
                "answer. Do not reach for this casually as an alternative to Gemini."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The full question or task for the model. Be specific and complete."
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional: relevant context to include (file contents, memory, prior results)."
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional: override the default Groq model ID for this call."
                    }
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_groq",
            "description": (
                "Hand off an entire task to GroqCloud as a real COWORKER, not just a "
                "text consultant. Unlike consult_groq (which only returns text), "
                "the Groq model spun up here gets FULL access to every tool Midum "
                "has — it can click UI elements, run terminal commands, read/write files, "
                "browse the web, everything — and works through the task independently, "
                "then reports back a final summary that you relay to the user. GroqCloud "
                "runs on fast LPU hardware, so this is a good pick when the user wants "
                "quick free-tier turnaround on a delegated sub-task. "
                "\n\n"
                "This runs in an ISOLATED conversation — the delegate does not see your "
                "conversation history, only what you put in `task` and `context`. Be "
                "complete and specific in both."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "The complete task to hand off, written as a self-contained "
                            "instruction — the delegate has no other context except this "
                            "and whatever you put in `context`."
                        )
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional: relevant background the delegate needs (file contents, prior findings, constraints)."
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional: override the default Groq model ID for this delegated task."
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": "Optional: cap on tool-call steps the delegate can take before it must report back. Default 10."
                    }
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_groq_models",
            "description": (
                "List models currently available on GroqCloud as a numbered indexed "
                "table (IDX | model ID | owner). Follow up with "
                "set_groq_model_by_index(index) to switch the active model — this is "
                "the CHOOSE-pattern way to pick a model instead of typing an exact ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_groq_model_by_index",
            "description": "Switch the active Groq model to the entry at `index` from list_groq_models.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Index from list_groq_models output."}
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_groq_model",
            "description": (
                "Switch the active Groq model directly by its exact model ID "
                "(e.g. 'llama-3.3-70b-versatile', 'qwen/qwen3-32b'). "
                "Use list_groq_models + set_groq_model_by_index instead if you don't "
                "know the exact ID. Takes effect immediately, no restart needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model_id": {"type": "string", "description": "Exact Groq model ID."}
                },
                "required": ["model_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_gemini_web",
            "description": (
                "Hand off an entire task to Gemini (via the web-app account session, "
                "gemini_webapi) as a real COWORKER, not just a text consultant — "
                "same idea as delegate_to_openrouter but running on Gemini's own "
                "account session instead. The Gemini sub-agent gets FULL access to "
                "every tool Midum has and works through the task independently in "
                "an ISOLATED conversation (its own ChatSession), then reports back a "
                "final summary. Slower per-step than delegate_to_openrouter (each "
                "step is a real gemini.google.com round trip) — prefer "
                "delegate_to_openrouter unless the user specifically asks for Gemini."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "The complete task to hand off, written as a self-contained "
                            "instruction — the delegate has no other context except this "
                            "and whatever you put in `context`."
                        )
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional: relevant background the delegate needs (file contents, prior findings, constraints)."
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": "Optional: cap on tool-call steps the delegate can take before it must report back. Default 10."
                    }
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_gemini_web_model",
            "description": (
                "Pin the model Gemini-web uses (when MODEL_PROVIDER=='gemini_web', or "
                "for delegate_to_gemini_web) to an exact model_name/display_name string "
                "(e.g. 'gemini-3-flash'). Pass an empty string to go back to "
                "auto-selecting the fastest available model at runtime."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model_name": {"type": "string", "description": "Exact Gemini model name/display name, or '' for auto."}
                },
                "required": ["model_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Pause execution for a specific number of seconds. Use this when waiting for an application to launch, a web page to load, or a background process to complete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "number",
                        "description": "The number of seconds to pause (can be a decimal, e.g., 1.5)."
                    }
                },
                "required": ["seconds"]
            }
        }
    },
    {"type":"function","function":{"name":"read_file_smart","description":"Read any file: txt/md/py/json/csv/html/.pdf(requires pymupdf)/.docx(requires mammoth). Returns chunk 1 for large files with chunk count — call read_file_chunk for rest.","parameters":{"type":"object","properties":{"path":{"type":"string","description":"Absolute path to the file."}},"required":["path"]}}},
    {"type":"function","function":{"name":"read_file_chunk","description":"Read chunk N (1-based) of a large file after read_file_smart reports multiple chunks.","parameters":{"type":"object","properties":{"path":{"type":"string"},"chunk_index":{"type":"integer","description":"1-based chunk number."}},"required":["path","chunk_index"]}}},
    {"type":"function","function":{"name":"write_docx_file","description":"Write a .docx Word document from Markdown-style text (# headings, **bold**). Requires python-docx: pip install python-docx.","parameters":{"type":"object","properties":{"path":{"type":"string","description":"Absolute path ending in .docx."},"content":{"type":"string","description":"Markdown-style text content."}},"required":["path","content"]}}},
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "Generate one or more images from a text prompt using Gemini's web app "
                "(gemini.google.com) via the free gemini_webapi session — the same session "
                "used by query_gemini_app/delegate_to_gemini_web. No image API key, no "
                "per-image metering, and the image is NOT saved to disk automatically — it's "
                "kept in memory and shown inline in the GUI chat with Download/Copy buttons "
                "the user can click if they want to keep it. Use this whenever the user asks "
                "you to create/generate/draw/make a picture or image."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed description of the image to generate, e.g. 'A watercolor painting of a lighthouse at sunset'."
                    },
                    "count": {
                        "type": "integer",
                        "description": "How many image variations to request (default 1)."
                    }
                },
                "required": ["prompt"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_flowchart",
            "description": (
                "Build and render a full explanatory flowchart in the chat (a real box-and-arrow "
                "diagram in the GUI, plus ASCII and Mermaid fallbacks in plain terminal mode). "
                "Use this any time you need to explain a process, decision tree, algorithm, or "
                "step-by-step system visually instead of (or in addition to) prose. "
                "Give every node a short unique 'id', a human-readable 'label', a 'type' "
                "(start | process | decision | io | end), and a 'next' list of the ids it flows "
                "into — optionally with an edge 'label' (e.g. 'yes'/'no' out of a decision node)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short title for the flowchart."
                    },
                    "steps": {
                        "type": "array",
                        "description": "The nodes of the flowchart, in any order (at least one should be type='start').",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "Short unique identifier for this node, e.g. 'check_login'."
                                },
                                "label": {
                                    "type": "string",
                                    "description": "The text shown inside the node's box."
                                },
                                "type": {
                                    "type": "string",
                                    "enum": ["start", "process", "decision", "io", "end"],
                                    "description": "Node shape/role. 'start' = entry point, 'decision' = branching question, 'io' = input/output, 'end' = terminal node."
                                },
                                "next": {
                                    "type": "array",
                                    "description": "IDs (or {to, label} objects) this node flows into. Use edge labels like 'yes'/'no' on decision branches.",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "to": {"type": "string", "description": "id of the next node."},
                                            "label": {"type": "string", "description": "Optional label for this edge, e.g. 'yes', 'no', 'on error'."}
                                        },
                                        "required": ["to"]
                                    }
                                }
                            },
                            "required": ["id", "label", "type"]
                        }
                    }
                },
                "required": ["title", "steps"]
            }
        }
    },
    {"type":"function","function":{"name":"write_response_memory","description":"Overwrite the response scratchpad (response_memory.md). Call FIRST for any multi-step task with a numbered plan. Wiped automatically when set_current_goal(none) fires.","parameters":{"type":"object","properties":{"content":{"type":"string","description":"Plan, checklist, or notes."}},"required":["content"]}}},
    {"type":"function","function":{"name":"append_response_memory","description":"Append a note or partial result to the response scratchpad. Use to log progress and accumulate partial outputs during a task.","parameters":{"type":"object","properties":{"content":{"type":"string","description":"Note or partial result."}},"required":["content"]}}},
    {"type":"function","function":{"name":"read_response_memory","description":"Read the current response scratchpad to check your plan or assemble a final answer from accumulated notes.","parameters":{"type":"object","properties":{}}}},
    {
        "type": "function",
        "function": {
            "name": "say",
            "description": (
                "Print a message to the user mid-turn, then continue acting. "
                "Use this to narrate what you are doing WHILE doing it — e.g. say('Opening Chrome...') "
                "then immediately call execute_terminal_command. "
                "Do NOT use this as a substitute for acting. Always follow a say() with a real tool call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "The text to show the user right now."}
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": (
                "List a directory as a numbered indexed table (IDX | TYPE | SIZE | NAME). "
                "PREFERRED over explore_path — returns an index you can act on. "
                "Follow up with open_path(path, index) to open a file or enter a subfolder."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the directory."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_path",
            "description": (
                "Act on a directory entry from list_directory by index. "
                "Directories: drills in and returns another indexed listing. "
                "Files: reads and returns the file content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":  {"type": "string",  "description": "The same path you passed to list_directory."},
                    "index": {"type": "integer", "description": "Entry index from list_directory output."}
                },
                "required": ["path", "index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_file",
            "description": (
                "Search for a file by name under a root directory. "
                "Returns a numbered indexed list of all matches. "
                "Follow up with open_path_by_index(index) to open the chosen file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename":    {"type": "string", "description": "Filename or substring to search for."},
                    "search_root": {"type": "string", "description": "Optional: directory to search under. Defaults to home directory."}
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_path_by_index",
            "description": "Open/read a file from the last find_file() result by index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Index from find_file output."}
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills_indexed",
            "description": (
                "List all available skills as a numbered indexed table. "
                "PREFERRED over list_skills — returns an index. "
                "Follow up with load_skill_by_index(index)."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill_by_index",
            "description": "Load a skill from the list_skills_indexed snapshot by index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Index from list_skills_indexed output."}
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_paths_indexed",
            "description": (
                "Parse paths.md into a numbered indexed table (IDX | LABEL | PATH). "
                "PREFERRED over read_paths — returns an index. "
                "Follow up with get_path(index) to retrieve the exact path string."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_path",
            "description": "Return the full path string for an entry from list_paths_indexed by index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Index from list_paths_indexed output."}
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_domain_knowledge_indexed",
            "description": (
                "List domain knowledge files as a numbered indexed table. "
                "PREFERRED over list_domain_knowledge. "
                "Follow up with read_domain_by_index(index)."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_domain_by_index",
            "description": "Read a domain knowledge file by its index from list_domain_knowledge_indexed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Index from list_domain_knowledge_indexed output."}
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_domain_skills_indexed",
            "description": (
                "List all domain skills as a numbered indexed table grouped by domain. "
                "PREFERRED over list_domain_skills. "
                "Follow up with load_skill(name) using the name shown."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_search_result",
            "description": (
                "Open a web search result from the last search_internet call by index. "
                "Much faster than typing the URL manually. "
                "Call search_internet first, then open_search_result(index) to open the chosen result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index":   {"type": "integer", "description": "Result index from search_internet output (0-based)."},
                    "browser": {"type": "string",  "description": "Browser to open in: 'chrome' (default), 'brave', 'firefox', 'edge'."}
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ocr_snapshot",
            "description": (
                "Take a screenshot and OCR the entire screen into a numbered indexed table "
                "(IDX | CONF | CX | CY | TEXT). "
                "PREFERRED over fallback_find_text when you want to see all text at once. "
                "Follow up with click_ocr_index(index) to click any element by index. "
                "Use when UIA returns no elements (canvas/WebGL apps, games, etc)."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_ocr_index",
            "description": "Click a text element from the last ocr_snapshot() by index. Exact — no text matching needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index":      {"type": "integer", "description": "Index from ocr_snapshot output."},
                    "click_type": {"type": "string",  "description": "'left_click' (default), 'right_click', or 'double_click'.", "enum": ["left_click", "right_click", "double_click"]}
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_browser_page",
            "description": (
                "Read the full text content of the currently active browser tab using Chrome DevTools Protocol (CDP). "
                "Returns the page title, URL, and clean readable text extracted from the DOM — "
                "NO HTML tags, just the actual text content. "
                "Works on ANY page including Google results, articles, YouTube descriptions, etc. "
                "Use this instead of read_aggregated_text for browser content — UIA cannot read web page text. "
                "Requires Chrome to be running with remote debugging enabled (see setup note)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_index": {
                        "type": "integer",
                        "description": "Which tab to read (0 = first/active tab, 1 = second tab, etc). Default 0."
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_browser_tabs",
            "description": (
                "List all open tabs in Chrome as a numbered indexed table (IDX | TITLE | URL). "
                "Follow up with read_browser_page(tab_index=N) or open_url to navigate."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_js_in_browser",
            "description": (
                "Execute arbitrary JavaScript in the current browser tab via CDP and return the result. "
                "Use for reading page data that other tools can't reach: "
                "document.title, element values, computed content, etc. "
                "Example: run_js_in_browser(\"document.title\") returns the page title. "
                "Example: run_js_in_browser(\"document.querySelector('h1').textContent\") reads the first heading."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "script":    {"type": "string",  "description": "JavaScript expression or statement to execute."},
                    "tab_index": {"type": "integer", "description": "Tab index (0 = active). Default 0."}
                },
                "required": ["script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": (
                "Open a URL directly in Chrome (or the default browser). "
                "This is the fastest way to navigate — ONE call replaces: "
                "click address bar → type URL → press Enter. "
                "If Chrome is already open, the URL opens in a new tab. "
                "If Chrome is not open, it launches Chrome and opens the URL. "
                "Prefer this over manually clicking the address bar and typing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": (
                            "The full URL to open, e.g. 'https://youtube.com', "
                            "'https://google.com/search?q=python'. "
                            "http:// or https:// prefix is added automatically if missing."
                        )
                    },
                    "browser": {
                        "type": "string",
                        "description": (
                            "Which browser to use: 'chrome' (default), 'brave', 'firefox', 'edge', "
                            "or 'default' to use the system default browser."
                        ),
                        "enum": ["chrome", "brave", "firefox", "edge", "default"]
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_mcp_servers",
            "description": (
                "List connected MCP (Model Context Protocol) servers ONLY — names, "
                "connection status, and tool COUNT, not the tools themselves. "
                "Always call this first before using any MCP server. Follow up with "
                "show_server_tools(server) to see what a specific server offers."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_server_tools",
            "description": (
                "Show the tool names, descriptions, and JSON input schemas for ONE "
                "connected MCP server. Call list_mcp_servers() first to get the "
                "server's index or name. Use this right before call_mcp_tool so you "
                "know the exact tool_name and arguments shape to send."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {
                        "type": "string",
                        "description": "Server index (e.g. '0') or name, from list_mcp_servers()."
                    }
                },
                "required": ["server"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_mcp_tool",
            "description": (
                "Call ANY tool on ANY connected MCP server. This is the single, "
                "uniform way to invoke MCP tools — check show_server_tools(server) "
                "first for the exact tool_name and the arguments it expects."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {
                        "type": "string",
                        "description": "Server index (e.g. '0') or name, from list_mcp_servers()."
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "Exact tool name, from show_server_tools()."
                    },
                    "arguments": {
                        "type": "object",
                        "description": (
                            "Arguments object matching that tool's input schema "
                            "(from show_server_tools()). Use {} if it takes none."
                        )
                    }
                },
                "required": ["server", "tool_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "connect_mcp_server",
            "description": (
                "Connect a new MCP server and (by default) remember it for future "
                "startups — the easy way to add a server. Use transport='stdio' for "
                "a local server launched as a subprocess (command + args), or "
                "transport='http'/'sse' for a remote server reachable by URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "A short unique name for this server, e.g. 'filesystem'."},
                    "transport": {
                        "type": "string",
                        "enum": ["stdio", "http", "sse"],
                        "description": "How to connect. 'stdio' for a local subprocess, 'http' or 'sse' for a remote URL."
                    },
                    "command": {"type": "string", "description": "Executable to launch (stdio only), e.g. 'npx' or 'python'."},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Command-line arguments (stdio only), e.g. ['-y', '@modelcontextprotocol/server-filesystem', '/home/user']."
                    },
                    "url": {"type": "string", "description": "Server URL (http/sse only)."},
                    "env": {"type": "object", "description": "Extra environment variables for the subprocess (stdio only)."},
                    "headers": {"type": "object", "description": "Extra HTTP headers, e.g. an Authorization token (http/sse only)."},
                    "persist": {
                        "type": "boolean",
                        "description": "Save to storage/mcp_servers.json so it auto-connects next startup. Default true."
                    }
                },
                "required": ["name", "transport"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "disconnect_mcp_server",
            "description": "Disconnect a connected MCP server. Optionally forget it so it stops auto-connecting on future startups.",
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {"type": "string", "description": "Server index (e.g. '0') or name, from list_mcp_servers()."},
                    "forget": {"type": "boolean", "description": "Also remove it from storage/mcp_servers.json. Default false."}
                },
                "required": ["server"],
            },
        },
    },
    # ── GUI user-interaction tools ─────────────────────────────────────────────
    # Optional, situational. Default is to call NONE of these. Use one only
    # when you actually need something specific from the user that you can't
    # reasonably infer or find yourself. Several can be called in the same turn.
    {
        "type": "function",
        "function": {
            "name": "ask_user_text",
            "description": (
                "Pop up a GUI textbox and ask the user to type a free-form answer. "
                "Use for anything with no fixed set of options — a name, a value, "
                "clarifying detail, etc. Blocks until the user submits or cancels."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "The question/instruction shown above the textbox."},
                    "title": {"type": "string", "description": "Dialog window title. Optional."}
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user_file_path",
            "description": (
                "Pop up a native file-picker dialog. Use this whenever the user tells "
                "you to open/read/edit/save a file but does not give you a path — ask "
                "instead of guessing. Blocks until the user picks a file or cancels."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Dialog title, e.g. 'Select the file to open'. Optional."},
                    "must_exist": {
                        "type": "boolean",
                        "description": "True (default) for picking an existing file to open. False for a save/new-file dialog."
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user_approval",
            "description": (
                "Pop up an Approve / Decline GUI dialog with two buttons. Use before "
                "anything the user should explicitly sign off on — deleting or "
                "overwriting files, sending a message on their behalf, spending money, "
                "running a risky or irreversible command. Blocks until the user clicks "
                "a button. Returns 'APPROVED' or 'DECLINED'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Short, bold headline of what's being approved, e.g. 'Delete 12 files in Downloads?'"},
                    "details": {"type": "string", "description": "Optional extra context/detail shown below the headline."}
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user_choice",
            "description": (
                "Pop up a multiple-choice GUI dialog: your question plus up to 4 "
                "options you define as buttons, and (by default) a 5th free-text box "
                "so the user can type something else. Use this to disambiguate what "
                "the user wants with a tap instead of back-and-forth text. Blocks "
                "until the user picks a button or submits custom text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question shown at the top of the dialog."},
                    "choice_1": {"type": "string", "description": "First option button label."},
                    "choice_2": {"type": "string", "description": "Second option button label. Optional."},
                    "choice_3": {"type": "string", "description": "Third option button label. Optional."},
                    "choice_4": {"type": "string", "description": "Fourth option button label. Optional."},
                    "allow_custom": {
                        "type": "boolean",
                        "description": "Whether to show a free-text 'Other...' box in addition to the buttons. Default true."
                    }
                },
                "required": ["question", "choice_1"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_native_tools",
            "description": (
                "List every built-in Midum tool by name + one-line description "
                "ONLY — not full parameter schemas. Follow up with "
                "show_native_tool_schema(tool_name) to get the exact arguments a "
                "tool expects before calling it. Mainly useful when you were not "
                "given the full native tool catalogue up front."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_native_tool_schema",
            "description": (
                "Show the full JSON parameter schema for ONE native Midum tool. "
                "Call list_native_tools() first to get the tool's index or exact "
                "name, then call this right before using it so you know the exact "
                "arguments shape to send."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Tool index (e.g. '0') or exact name, from list_native_tools()."
                    }
                },
                "required": ["tool_name"],
            },
        },
    },
]

# =============================================================================

