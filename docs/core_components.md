# Core Components Architecture

The core components form the backbone of `midum_pkg`, handling initialization, global configuration, state management, and the main entry point.

## 1. `main.py`
- **Purpose**: Entry point for the application. Initializes the event loops, loads configurations, sets up signal handlers, and launches the primary user interface or headless daemon.
- **Key Functions**:
  - `main()`: Parses command line arguments, initializes state stores, and starts the GUI application loop.

## 2. `config.py`
- **Purpose**: Manages global configuration variables, API endpoint mappings, timeout limits, and feature flags.
- **Key Features**:
  - Environment variable overrides.
  - Default paths for local storage, assets, and caches.

## 3. `state.py`
- **Purpose**: Tracks runtime state across modules, including active agent threads, current goals, window focus, and session status.
- **Key Classes**:
  - `SystemState`: Singleton or thread-safe state store holding active tasks and context variables.
