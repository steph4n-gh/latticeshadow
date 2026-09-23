import os
import sys
import argparse
import hashlib
import subprocess
import plistlib
import time
import json
import shlex
from datetime import datetime

# Ensure the parent directory is in sys.path to import latticeshadow_db
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from latticeshadow import keychain
from latticeshadow import config
from latticeshadow.vaults import (
    HOT_INDEX_STRATEGY,
    chmod_vault_files,
    hot_collection_exists,
    invalidate_holographic_indexes,
    open_hot_vault,
    open_main_vault,
)

def connect(*args, **kwargs):
    from latticeshadow_db.latticedb import connect as _orig_connect
    vault = None
    last_error = None
    for attempt in range(10):
        try:
            vault = _orig_connect(*args, **kwargs)
            break
        except Exception as exc:
            last_error = exc
            if "locked" not in str(exc).lower() or attempt == 9:
                raise
            time.sleep(0.1)
    if vault is None:
        raise last_error
    db_path = str(kwargs.get("db_path", args[0] if args else get_db_path()))
    collection = str(kwargs.get("collection", args[1] if len(args) > 1 else "clipboard"))
    chmod_vault_files(db_path, collection=collection)

    original_count = vault.count

    def live_count():
        try:
            import sqlite3

            with sqlite3.connect(db_path, timeout=5.0) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM vectors WHERE collection = ?",
                    (collection,),
                ).fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return original_count()

    vault.count = live_count
    return vault

LOG_DIR = os.path.expanduser("~/.latticeshadow")
KEY_FILE = os.path.join(LOG_DIR, ".key")
DB_PATH = os.path.join(LOG_DIR, "shadow.sqlite")

def get_log_dir():
    if LOG_DIR != os.path.expanduser("~/.latticeshadow"):
        return LOG_DIR
    return config.get_data_dir()

def get_key_file():
    default_key = os.path.join(os.path.expanduser("~/.latticeshadow"), ".key")
    if KEY_FILE != default_key:
        return KEY_FILE
    return os.path.join(get_log_dir(), ".key")

def get_db_path():
    default_db = os.path.join(os.path.expanduser("~/.latticeshadow"), "shadow.sqlite")
    if DB_PATH != default_db:
        return DB_PATH
    return os.path.join(get_log_dir(), "shadow.sqlite")
PLIST_LABEL = "com.latticedb.shadow"
PLIST_PATH = os.path.expanduser("~/Library/LaunchAgents/com.latticedb.shadow.plist")
ALIAS_MARKER = "# latticeshadow-alias"
SHELL_MARKER = "# LATTICESHADOW_SHELL_OPT_IN"


# ── Key Management ────────────────────────────────────────────────────────────

def get_or_create_master_key() -> str:
    """
    Resolve the master key using a 3-tier waterfall:
    1. macOS Keychain (preferred, hardware-backed on Apple Silicon via Secure Enclave)
    2. Flat file at ~/.latticeshadow/.key (backward compatibility)
    3. Generate new key → store in both Keychain and flat file (Enclave-wrapped)
    """
    import base64
    import hashlib
    from latticeshadow import security
    LABEL = "masterkey"
    
    def wrap_and_encode(raw_key: str) -> str:
        ciphertext = security.encrypt_with_secure_enclave(LABEL, raw_key.encode('utf-8'))
        return base64.b64encode(ciphertext).decode('utf-8')
        
    def decode_and_unwrap(wrapped_key_str: str) -> str | None:
        try:
            ciphertext = base64.b64decode(wrapped_key_str.encode('utf-8'))
            decrypted = security.decrypt_with_secure_enclave(LABEL, ciphertext)
            return decrypted.decode('utf-8')
        except Exception:
            return None

    # 1. Try Keychain first
    try:
        stored = keychain.retrieve_key()
        if stored:
            unwrapped = decode_and_unwrap(stored)
            if unwrapped:
                return unwrapped
            else:
                # Fallback check for legacy plaintext key
                if len(stored) == 64 and all(c in "0123456789abcdef" for c in stored):
                    wrapped = wrap_and_encode(stored)
                    keychain.store_key(wrapped)
                    key_file = get_key_file()
                    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as f:
                        f.write(wrapped)
                    print("\u2713 Migrated legacy master key to Secure Enclave wrapped key.")
                    return stored
    except Exception:
        pass

    # 2. Fall back to flat file
    key_file = get_key_file()
    if os.path.exists(key_file):
        with open(key_file, "r") as f:
            stored = f.read().strip()
        if stored:
            unwrapped = decode_and_unwrap(stored)
            if unwrapped:
                try:
                    keychain.store_key(stored)
                except Exception:
                    pass
                return unwrapped
            elif len(stored) == 64 and all(c in "0123456789abcdef" for c in stored):
                wrapped = wrap_and_encode(stored)
                try:
                    keychain.store_key(wrapped)
                except Exception:
                    pass
                fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(wrapped)
                print("\u2713 Migrated flat-file legacy master key to Secure Enclave wrapped key.")
                return stored

    # 3. Generate new key
    raw_key = hashlib.sha256(os.urandom(64)).hexdigest()
    try:
        wrapped_key = wrap_and_encode(raw_key)
        enclave_wrapped = True
    except Exception:
        # Keep the legacy fallback, but report its actual protection level.
        wrapped_key = raw_key
        enclave_wrapped = False
        
    try:
        keychain.store_key(wrapped_key)
        print("\u2713 Stored master key in macOS Keychain.")
    except Exception:
        pass
        
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(wrapped_key)
    print(f"\u2713 Generated master key at {key_file} (Secure Enclave wrapped: {enclave_wrapped})")
    return raw_key


# ── Vault Connection ──────────────────────────────────────────────────────────

def get_vault(create_if_missing=False):
    db_path = get_db_path()
    if not os.path.exists(db_path):
        if not create_if_missing:
            print("Error: LatticeShadow database not found. Is the daemon running?")
            print("  Run: shadow enable")
            sys.exit(1)
        os.makedirs(os.path.dirname(db_path), mode=0o700, exist_ok=True)
        os.chmod(os.path.dirname(db_path), 0o700)

    master_key = get_or_create_master_key()

    try:
        return open_main_vault(
            db_path=get_db_path(),
            master_key=master_key,
            device=config.get_device(),
        )
    except PermissionError:
        print("FATAL: The database has been crypto-shredded and is unrecoverable.")
        print("  To start fresh, run: shadow remove && shadow install && shadow enable")
        sys.exit(1)
    except ValueError as exc:
        if "embedding model" in str(exc):
            raise SystemExit("Stored vectors use an older embedding model. Run: shadow disable && shadow rebuild-index --yes") from exc
        raise


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts_from_doc_id(doc_id: str) -> str:
    """Extract a human-readable timestamp from a doc_id like 'clip_1719500000000'."""
    try:
        ms = int(doc_id.split("_", 1)[1])
        dt = datetime.fromtimestamp(ms / 1000.0)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (IndexError, ValueError, OSError):
        return ""


def _print_event_lines(events, empty_message="No memory events found."):
    from latticeshadow.timeline import format_event

    if not events:
        print(empty_message)
        return
    for event in events:
        print(format_event(event))


def _print_json(payload):
    print(json.dumps(payload, indent=2, sort_keys=True))


# ── Core Commands ─────────────────────────────────────────────────────────────

def do_search(query, paste_mode=False):
    try:
        vault = get_vault()
        if not vault or vault.count() == 0:
            print("No matching memories found.")
            return
    except Exception as exc:
        raise SystemExit(f"Search failed while opening the vault: {exc}") from exc

    if not paste_mode:
        print(f"Searching for '{query}'...")
    try:
        res = vault.search(query, n_results=5, hybrid=True)
        if not res or not res.documents:
            print("No matching memories found.")
            return

        if paste_mode:
            # Copy the top result back to the clipboard and exit
            top_doc = res.documents[0]
            subprocess.run(["pbcopy"], input=top_doc.encode("utf-8"), check=True)
            # Truncate for display
            display = top_doc if len(top_doc) <= 120 else top_doc[:120] + "..."
            print(f"✓ Copied to clipboard: {display}")
            return

        for i, doc in enumerate(res.documents):
            score = res.scores[i] if hasattr(res, 'scores') and res.scores else 0.0
            doc_id = res.ids[i] if hasattr(res, 'ids') and res.ids else ""
            ts = _ts_from_doc_id(doc_id)
            ts_str = f"  \033[90m({ts})\033[0m" if ts else ""

            # Truncate long entries for display
            display = doc if len(doc) <= 200 else doc[:200] + "..."
            print(f"\n\033[96m--- Result {i+1} (Score: {score:.4f}){ts_str} ---\033[0m")
            print(display)
    except Exception as exc:
        raise SystemExit(f"Search failed: {exc}") from exc


def do_paste(query):
    do_search(query, paste_mode=True)


def do_unswap(query):
    print(f"Searching for swap page matching '{query}'...")
    try:
        vault = get_vault()
        results = vault.search(query, n_results=10)
        
        swap_result = None
        swap_metadata = None
        
        if results and getattr(results, "ids", None):
            for i, doc_id in enumerate(results.ids):
                with vault._store._connect() as conn:
                    cursor = conn.execute(
                        "SELECT metadata_json, document FROM vectors WHERE doc_id = ? AND collection = ?",
                        (doc_id, vault.name)
                    )
                    row = cursor.fetchone()
                    if row:
                        try:
                            meta = json.loads(row[0])
                        except Exception:
                            meta = {}
                        doc_text = row[1]
                        if vault._privacy and doc_text.startswith("enc:"):
                            try:
                                doc_text = vault._privacy.decrypt_document(doc_text)
                            except Exception:
                                pass
                        if meta.get("type") == "swap_page":
                            swap_result = doc_text
                            swap_metadata = meta
                            break

        if swap_result and swap_metadata:
            app = swap_metadata.get("app")
            title = swap_metadata.get("title", "")
            url = swap_metadata.get("url", "")
            
            print(f"\n\033[92m★ Found Swapped Page Context ★\033[0m")
            print(f"Application: {app}")
            if title:
                print(f"Title: {title}")
            if url:
                print(f"URL: {url}")
            
            snippet = swap_result[:300] + "..." if len(swap_result) > 300 else swap_result
            print(f"\nSnippet:\n{snippet}\n")
            
            print(f"Re-hydrating {app} application state...")
            
            # Execute AppleScript to restore app state
            script = f'tell application "{app}" to activate'
            if url and app in ("Safari", "Google Chrome"):
                script += f'\ntell application "{app}" to open location "{url}"'
            
            subprocess.run(["osascript", "-e", script])
            print("✓ Application state restored successfully.")
        else:
            print("No matching swapped application state found in memory.")
    except Exception as e:
        print(f"Error restoring swap page: {e}")


def do_shred():
    vault = get_vault()
    db_path = get_db_path()
    shred_hot = hot_collection_exists(db_path)

    print("\033[91mWARNING: You are about to crypto-shred your entire clipboard history.\033[0m")
    print("This action is IRREVERSIBLE. The encryption keys will be destroyed.")
    confirm = input("Type 'SHRED' to confirm: ")
    if confirm.strip() == "SHRED":
        vault.crypto_shred()
        if shred_hot:
            try:
                hot_vault = open_hot_vault(
                    db_path=db_path,
                    master_key=get_or_create_master_key(),
                    device=config.get_device(),
                    strategy=HOT_INDEX_STRATEGY,
                )
                hot_vault.crypto_shred()
            except Exception as e:
                print(f"Warning: Hot index shred failed: {e}")
        # Also destroy the Keychain entry
        try:
            keychain.delete_key()
        except Exception:
            pass
        print("\033[92mSUCCESS\033[0m: Keys zeroed. Database scrambled. Keychain entry destroyed. History destroyed.")
    else:
        print("Aborted.")


def do_sleep():
    vault = get_vault()
    count_before = vault.count()
    print(f"Triggering REM Sleep consolidation on {count_before} entries...")
    vault.consolidate()
    count_after = vault.count()
    print(f"Consolidation complete: {count_before} → {count_after} entries.")
    # Run LLM dream enrichment if configured
    try:
        from latticeshadow.dreamer import dream_cycle
        dream_cycle(vault)
        print("Dream enrichment complete.")
    except Exception as e:
        print(f"Dream enrichment skipped: {e}")


def do_watch():
    log_path = os.path.join(get_log_dir(), "shadowd.log")
    if not os.path.exists(log_path):
        print("Error: No log file found. Is the daemon installed?")
        return
    print("Watching clipboard captures (Ctrl+C to stop)...\n")
    try:
        # Use tail -f for a live stream, filtering to only show capture events
        proc = subprocess.Popen(
            ["tail", "-f", log_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        for line in proc.stdout:
            # Show capture lines and consolidation lines, skip noise
            if "Captured:" in line or "consolidat" in line.lower() or "shred" in line.lower():
                # Colorize the output
                if "Captured:" in line:
                    print(f"\033[92m{line.rstrip()}\033[0m")
                elif "shred" in line.lower():
                    print(f"\033[91m{line.rstrip()}\033[0m")
                else:
                    print(f"\033[93m{line.rstrip()}\033[0m")
    except KeyboardInterrupt:
        proc.terminate()
        print("\nStopped watching.")


# ── System Integration Commands ───────────────────────────────────────────────

def _get_python_path():
    return sys.executable

def _get_daemon_path():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), 'shadowd.py'))

def _configure_shell(enable: bool) -> None:
    """Manage only LatticeShadow's marked shell lines."""
    zshrc = os.path.expanduser("~/.zshrc")
    lines = []
    if os.path.exists(zshrc):
        with open(zshrc) as file:
            lines = file.readlines()
    cleaned = [
        line for line in lines
        if ALIAS_MARKER not in line
        and "# LATTICESHADOW_ZSH" not in line
        and SHELL_MARKER not in line
    ]
    plugin_dest = os.path.join(get_log_dir(), "latticeshadow.zsh")
    if enable:
        import shutil

        os.makedirs(get_log_dir(), mode=0o700, exist_ok=True)
        shutil.copyfile(os.path.join(os.path.dirname(__file__), "latticeshadow.zsh"), plugin_dest)
        cleaned.append(f"[[ -f {shlex.quote(plugin_dest)} ]] && source {shlex.quote(plugin_dest)}  {SHELL_MARKER}\n")
    elif os.path.exists(plugin_dest):
        os.remove(plugin_dest)
    if cleaned != lines:
        with open(zshrc, "w") as file:
            file.writelines(cleaned)


def do_shell(args):
    _configure_shell(args.shell_command == "enable")
    print(f"Shell integration {args.shell_command}d. Open a new shell to apply the change.")


def do_rebuild_index(args):
    from latticeshadow.rebuild import rebuild_embeddings

    if not args.yes and input("Type REBUILD to rebuild stored vectors: ").strip() != "REBUILD":
        print("Aborted.")
        return
    status = subprocess.run(["launchctl", "list", PLIST_LABEL], capture_output=True)
    if status.returncode == 0:
        raise SystemExit("Stop LatticeShadow first with: shadow disable")
    count, backup = rebuild_embeddings(
        get_db_path(),
        get_or_create_master_key(),
        hot_collection=config.get("memory.hot_index_collection") or "clipboard_hot",
    )
    print(f"Rebuilt {count} stored vector(s).")
    if backup:
        print(f"Database backup (keep private): {backup}")


def _set_launch_agent_enabled(enabled: bool) -> None:
    action = "enable" if enabled else "disable"
    service = f"gui/{os.getuid()}/{PLIST_LABEL}"
    result = subprocess.run(["launchctl", action, service], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"Failed to {action} LatticeShadow at login: {result.stderr.strip()}")


def do_install():
    # A plist in LaunchAgents is discovered at login. Keep installation itself
    # from enrolling capture, including on the next login or reboot.
    if os.path.exists(PLIST_PATH):
        do_disable()
    else:
        _set_launch_agent_enabled(False)

    log_dir = get_log_dir()
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    os.chmod(log_dir, 0o700)

    python_path = _get_python_path()
    daemon_path = _get_daemon_path()

    # Generate master key on first install
    get_or_create_master_key()

    # Write launchd plist
    plist = {
        "Label": PLIST_LABEL,
        "ProgramArguments": [python_path, daemon_path],
        "RunAtLoad": True,
        # A normal shutdown or an integrity refusal must stay stopped.
        "KeepAlive": {"SuccessfulExit": False},
        "StandardErrorPath": os.path.join(get_log_dir(), "launchd.err"),
        "StandardOutPath": os.path.join(get_log_dir(), "launchd.out"),
    }

    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)
    print(f"✓ Installed launchd plist at {PLIST_PATH}")

    # A checkout update changes the daemon's source hashes. The explicit
    # install step accepts the currently installed code as the new baseline.
    from latticeshadow.integrity import compute_manifest, write_manifest

    write_manifest(compute_manifest())
    print("✓ Refreshed local daemon integrity baseline.")

    # Migrate old automatic hooks, preserving an explicit shell opt-in.
    zshrc = os.path.expanduser("~/.zshrc")
    opted_in = False
    if os.path.exists(zshrc):
        with open(zshrc) as file:
            opted_in = SHELL_MARKER in file.read()
    _configure_shell(opted_in)

    print("\nInstall complete! Run 'shadow enable' to start the daemon.")
    print("Before first run, choose capture sources with: shadow consent wizard")
    print("Optional shell widgets: shadow shell enable")
    print("\n💡 Tip: Unlock advanced features:")
    print("  - Enable active browser tracking: 'shadow config set inputs.ambient_context true'")
    print("  - Enable real-time P2P mesh sync: 'shadow config set sync.mesh_sync true'")



def do_enable():
    from latticeshadow import consent
    from latticeshadow.integrity import verify_integrity

    pending = consent.pending_capture_sources()
    if pending:
        raise SystemExit(
            f"Choose capture sources before starting: {', '.join(pending)}. "
            "Run 'shadow consent wizard' or 'shadow consent set <source> on|off' for each."
        )
    passed, violations = verify_integrity()
    if not passed:
        raise SystemExit(
            f"Daemon source changed ({len(violations)} file(s)). "
            "Review the update, then run 'shadow install' to refresh the local integrity baseline."
        )
    if os.environ.get("LATTICESHADOW_EMBEDDING_MODEL") != "hash":
        from latticeshadow.vaults import _local_model

        try:
            _local_model()
        except Exception as exc:
            raise SystemExit(f"Cannot start capture until the local embedding model loads: {exc}") from exc
    service = None
    try:
        import ServiceManagement
        if ".app/Contents/MacOS" in sys.executable:
            service = ServiceManagement.SMAppService.mainAppService()
    except Exception:
        pass

    if service:
        success, error = service.registerAndReturnError_(None)
        if success:
            print("✓ LatticeShadow registered as login item via SMAppService.")
            return
        else:
            print(f"Warning: SMAppService registration failed: {error}. Falling back to launchd plist.")

    if not os.path.exists(PLIST_PATH):
        raise SystemExit("Error: plist not found. Run 'shadow install' first.")
    subprocess.run(["launchctl", "unload", PLIST_PATH],
                    capture_output=True)  # unload first to avoid double-load
    try:
        _set_launch_agent_enabled(True)
    except SystemExit as exc:
        raise SystemExit(f"Failed to start LatticeShadow: {exc}") from exc
    result = subprocess.run(["launchctl", "load", PLIST_PATH], capture_output=True, text=True)
    if result.returncode != 0:
        _set_launch_agent_enabled(False)
        raise SystemExit(f"Failed to start LatticeShadow: {result.stderr.strip()}")
    print("✓ LatticeShadow daemon enabled and started.")


def do_disable():
    service = None
    try:
        import ServiceManagement
        if ".app/Contents/MacOS" in sys.executable:
            service = ServiceManagement.SMAppService.mainAppService()
    except Exception:
        pass

    if service:
        success, error = service.unregisterAndReturnError_(None)
        if success:
            print("✓ LatticeShadow unregistered from login items via SMAppService.")
            return
        else:
            print(f"Warning: SMAppService unregistration failed: {error}.")

    if os.path.exists(PLIST_PATH):
        _set_launch_agent_enabled(False)
        result = subprocess.run(["launchctl", "unload", PLIST_PATH], capture_output=True, text=True)
        if result.returncode != 0:
            status = subprocess.run(["launchctl", "list", PLIST_LABEL], capture_output=True)
            if status.returncode == 0:
                raise SystemExit(f"Failed to stop LatticeShadow: {result.stderr.strip()}")
        print("✓ LatticeShadow daemon stopped.")
    else:
        print("Daemon is not installed.")


def do_remove():
    # 1. Stop the daemon
    do_disable()

    # 2. Remove the plist
    if os.path.exists(PLIST_PATH):
        os.remove(PLIST_PATH)
        print("✓ Removed launchd plist.")

    # 3. Remove only the marked shell integration lines.
    _configure_shell(False)

    # 4. Preserve the decryption key unless data deletion was requested.
    log_dir = get_log_dir()
    if os.path.exists(log_dir):
        confirm = input(f"Delete all data in {log_dir}? [y/N]: ")
        if confirm.strip().lower() == "y":
            import shutil
            shutil.rmtree(log_dir)
            print(f"\u2713 Deleted {log_dir}")
            try:
                if keychain.delete_key():
                    print("\u2713 Removed master key from macOS Keychain.")
            except Exception:
                pass
        else:
            print(f"  Data preserved at {log_dir}")

    print("\u2713 Uninstallation complete.")


def do_status():
    from latticeshadow.capture_state import get_status

    status = get_status()
    if status["daemon_running"] is True:
        print("Daemon:   \033[92m● RUNNING\033[0m")
    elif status["daemon_running"] is False:
        print("Daemon:   \033[91m● STOPPED\033[0m")
    else:
        print("Daemon:   ● UNKNOWN")
    if status["paused"]:
        print("Capture:  PAUSED (run 'shadow resume' to resume selected sources)")
    elif status["state"] == "capturing":
        active = ", ".join(name for name, item in status["sources"].items() if item["capturing"])
        print(f"Capture:  {active}")
    elif status["state"] == "idle":
        print("Capture:  No sources enabled")
    if status["error"]:
        print(f"Status:   {status['error']}")
    pending = status["consent_needed"]
    if pending:
        print(f"Capture choices needed: {', '.join(pending)} (run 'shadow consent wizard')")

    # Database info
    db_path = get_db_path()
    if os.path.exists(db_path):
        size_kb = os.path.getsize(db_path) / 1024
        print(f"Database: {db_path} ({size_kb:.1f} KB)")
        try:
            import sqlite3
            conn = sqlite3.connect(db_path, timeout=5.0)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM vectors")
            count = cursor.fetchone()[0]
            conn.close()
            print(f"Entries:  {count}")
        except Exception:
            print("Entries:  (database locked, uninitialized, or shredded)")
    else:
        print("Database: Not initialized yet.")

    # Log file
    log_path = os.path.join(get_log_dir(), "shadowd.log")
    if os.path.exists(log_path):
        print(f"Log:      {log_path}")


def do_pause():
    from latticeshadow.consent import set_paused

    set_paused(True)
    print("Capture paused. Your source choices are preserved.")


def do_resume():
    from latticeshadow.consent import set_paused

    try:
        set_paused(False)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print("Capture resumed for enabled sources.")


# ── Memory Timeline Commands ─────────────────────────────────────────────────

def do_now(args):
    """Show the current private memory context without calling an LLM."""
    from latticeshadow.timeline import current_context

    vault = get_vault()
    payload = current_context(vault, limit=args.limit)
    if args.json:
        _print_json(payload)
        return

    summary = payload["summary"]
    print(f"Current context: {summary['count']} event(s)")
    if summary.get("types"):
        type_text = ", ".join(f"{name}={count}" for name, count in sorted(summary["types"].items()))
        print(f"Types: {type_text}")
    print()
    _print_event_lines(payload["events"], empty_message="No recent memory events found.")


def do_timeline(args):
    """Print a recent or semantic timeline slice."""
    from latticeshadow.timeline import fetch_events, search_events, summarize_events

    vault = get_vault()
    if args.query:
        events = search_events(vault, args.query, limit=args.limit)
    else:
        events = fetch_events(
            vault,
            limit=args.limit,
            source=args.source,
            since=args.since,
            until=args.until,
        )

    if args.json:
        _print_json({"summary": summarize_events(events), "events": events})
        return
    _print_event_lines(events)


def do_remember(args):
    from latticeshadow.timeline import add_event

    metadata = {}
    if args.metadata:
        try:
            metadata = json.loads(args.metadata)
        except json.JSONDecodeError as exc:
            print(f"Invalid metadata JSON: {exc}")
            return
    vault = get_vault(create_if_missing=True)
    doc_id = add_event(vault, args.event_type, args.text, metadata=metadata, doc_id=args.id)
    try:
        from latticeshadow.audit_log import append_audit_event

        append_audit_event(
            "capture",
            {
                "source": args.event_type,
                "doc_id": doc_id,
                "content_hash": hashlib.sha256(args.text.encode("utf-8", errors="replace")).hexdigest(),
            },
            data_dir=get_log_dir(),
        )
    except Exception:
        pass
    print(doc_id)


def do_why(args):
    """Explain why a query is relevant by showing matching memory context."""
    from latticeshadow.timeline import fetch_events, format_event, search_events

    vault = get_vault()
    matches = search_events(vault, args.query, limit=args.limit)
    recent = fetch_events(vault, limit=min(5, args.limit))

    if not matches:
        print("No matching memory events found.")
        return

    print(f"Why '{args.query}' looks relevant:")
    for idx, event in enumerate(matches, start=1):
        print(f"{idx}. {format_event(event)}")

    if recent:
        print("\nRecent surrounding context:")
        for event in recent:
            print(f"- {format_event(event)}")


def do_summarize(args):
    from latticeshadow.summarizer import summarize_events as summarize_memory_events
    from latticeshadow.timeline import fetch_events, search_events

    vault = get_vault()
    events = search_events(vault, args.query, limit=args.limit) if args.query else fetch_events(vault, limit=args.limit)
    payload = summarize_memory_events(events, prefer_foundation=not args.no_foundation)
    if args.json:
        _print_json({**payload, "events": events[:5]})
    else:
        print(payload["summary"])


def do_open_context(args):
    """Open the best URL/file target attached to a memory search result."""
    from latticeshadow.timeline import open_target, search_events

    vault = get_vault()
    events = search_events(vault, args.query, limit=args.limit)
    for event in events:
        target = open_target(event)
        if target:
            print(target)
            if not args.dry_run:
                subprocess.run(["open", target], check=False)
            return
    print("No openable URL or file path found for that query.")


def do_forget(args):
    """Delete selected memory events by id or bounded search/source filter."""
    from latticeshadow.timeline import fetch_events, forget_events, search_events

    vault = get_vault()
    ids = list(args.id or [])
    selected_events = []

    if args.query:
        selected_events.extend(search_events(vault, args.query, limit=args.limit))
    elif args.source:
        selected_events.extend(fetch_events(vault, limit=args.limit, source=args.source))

    ids.extend(event["id"] for event in selected_events if event.get("id"))
    ids = list(dict.fromkeys(ids))

    if not ids:
        print("Nothing selected. Provide --id, --query, or --source.")
        return

    print("Selected memory ids:")
    for doc_id in ids:
        print(f"  {doc_id}")

    if not args.yes:
        confirm = input("Type FORGET to delete these events: ")
        if confirm.strip() != "FORGET":
            print("Aborted.")
            return

    deleted = forget_events(vault, ids)
    hot_deleted = None
    if hot_collection_exists(get_db_path()):
        try:
            hot_vault = open_hot_vault(
                db_path=get_db_path(),
                master_key=get_or_create_master_key(),
                device=config.get_device(),
                strategy=HOT_INDEX_STRATEGY,
            )
            hot_deleted = forget_events(hot_vault, ids)
        except Exception as exc:
            print(f"Warning: Hot index forget failed: {exc}")
    if deleted:
        try:
            invalidate_holographic_indexes(get_log_dir())
        except OSError as exc:
            print(f"Warning: Could not remove a derived memory index: {exc}")
    try:
        from latticeshadow.pot_chain import PoTChain

        PoTChain(data_dir=get_log_dir()).append_event("delete", ",".join(ids))
    except Exception:
        pass
    try:
        from latticeshadow.audit_log import append_audit_event

        append_audit_event(
            "delete",
            {"ids": ids, "main_deleted": deleted, "hot_deleted": hot_deleted},
            data_dir=get_log_dir(),
        )
    except Exception:
        pass
    message = f"Deleted {deleted} memory event(s)."
    if hot_deleted is not None:
        message += f" Hot index deleted {hot_deleted} mirrored event(s)."
    print(message)


def do_privacy_report(args):
    from latticeshadow.moonshot import generate_privacy_report

    report = generate_privacy_report(get_db_path(), data_dir=get_log_dir())
    if args.json:
        _print_json(report)
        return

    print(f"Privacy status: {report['status']}")
    print(f"Database: {report['db_path']}")
    print(f"Hot index enabled: {report['hot_index_enabled']}")
    print(f"Consent completed: {report['consent']['completed']}")
    print(f"Audit log: {report['audit_log']['message']}")
    pending_repairs = report["repair_queue"]["counts"].get("pending", 0)
    print(f"Pending repairs: {pending_repairs}")
    enabled = [name for name, value in report["listeners"].items() if value]
    print(f"Enabled listeners: {', '.join(enabled) if enabled else 'none'}")
    if report["issues"]:
        print("Issues:")
        for issue in report["issues"]:
            print(f"  - {issue}")
    else:
        print("Issues: none")


def do_privacy_test(args):
    from latticeshadow.moonshot import run_privacy_leakage_harness, write_json_report

    report = run_privacy_leakage_harness(
        count=args.count,
        dim=args.dim,
        queries=args.queries,
    )
    rendered = write_json_report(report, args.output)
    if args.output:
        print(f"Wrote privacy leakage harness report to {rendered}")
    else:
        print(rendered)


def do_bench(args):
    if args.bench_command == "moonshot":
        from latticeshadow.moonshot import run_moonshot_bench, write_json_report

        report = run_moonshot_bench(
            count=args.count,
            dim=args.dim,
            queries=args.queries,
            engines=args.engine,
        )
        rendered = write_json_report(report, args.output)
        if args.output:
            print(f"Wrote moonshot benchmark report to {rendered}")
        else:
            print(rendered)
    else:
        print("Usage: shadow bench moonshot")


def do_mcp(args):
    if args.mcp_command == "serve":
        from latticeshadow.mcp_server import serve
        from latticeshadow.timeline import forget_events

        def forget_all_stores(ids):
            main_deleted = forget_events(get_vault(), ids)
            payload = {"deleted": main_deleted, "ids": ids}
            if hot_collection_exists(get_db_path()):
                try:
                    hot_vault = open_hot_vault(
                        db_path=get_db_path(),
                        master_key=get_or_create_master_key(),
                        device=config.get_device(),
                        strategy=HOT_INDEX_STRATEGY,
                    )
                    payload["hot_deleted"] = forget_events(hot_vault, ids)
                except Exception as exc:
                    payload["hot_error"] = str(exc)
            if main_deleted:
                try:
                    invalidate_holographic_indexes(get_log_dir())
                except OSError as exc:
                    payload["cache_error"] = str(exc)
            return payload

        serve(
            lambda: get_vault(),
            db_path=get_db_path(),
            data_dir=get_log_dir(),
            forgetter=forget_all_stores,
        )
    else:
        print("Usage: shadow mcp serve")


def do_consent(args):
    from latticeshadow import consent

    action = args.consent_command
    if action == "status":
        _print_json(consent.consent_status())
    elif action == "wizard":
        _print_json(consent.run_wizard())
    elif action == "set":
        enabled = args.state.lower() in ("on", "true", "yes", "1")
        _print_json(consent.set_consent(args.surface, enabled))
    else:
        print("Usage: shadow consent {status|wizard|set}")


def do_audit(args):
    from latticeshadow.audit_log import SignedAuditLog

    ok, message = SignedAuditLog(data_dir=get_log_dir()).verify()
    if args.json:
        _print_json({"verified": ok, "message": message})
    else:
        print(("OK: " if ok else "FAILED: ") + message)


def do_repair(args):
    from latticeshadow.repair_queue import format_repair, list_repairs, update_repair_status

    if args.repair_command == "list":
        repairs = list_repairs(status=args.status, limit=args.limit, data_dir=get_log_dir())
        if args.json:
            _print_json({"repairs": repairs})
            return
        if not repairs:
            print("No repair proposals found.")
            return
        for repair in repairs:
            print(format_repair(repair))
    elif args.repair_command == "status":
        repair = update_repair_status(args.id, args.status, data_dir=get_log_dir())
        try:
            from latticeshadow.audit_log import append_audit_event

            append_audit_event(
                "repair_status",
                {"id": args.id, "status": args.status},
                data_dir=get_log_dir(),
            )
        except Exception:
            pass
        _print_json(repair)
    else:
        print("Usage: shadow repair {list|status}")


def do_native(args):
    from latticeshadow.native_bridge import intent_contract, swift_intent_source_hint

    if args.native_command == "intents":
        payload = intent_contract()
        if args.json:
            _print_json(payload)
        else:
            print(f"Swift scaffold: {swift_intent_source_hint()}")
            bridge = payload["foundation_bridge"]
            print(f"Foundation bridge: {bridge['executable']} ({bridge['environment']})")
            for intent in payload["intents"]:
                print(f"{intent['name']}: shadow {' '.join(intent['command'])}")
    else:
        print("Usage: shadow native intents")


def do_trust(args):
    from latticeshadow.trust import DeviceTrustStore

    store = DeviceTrustStore(data_dir=get_log_dir())
    if args.trust_command == "local":
        payload = store.local_identity(node_id=args.node_id)
        _print_json(payload)
    elif args.trust_command == "list":
        _print_json({"devices": store.list_devices()})
    elif args.trust_command == "add":
        record = store.trust_device(args.node_id, args.public_key, label=args.label)
        _print_json(record)
    elif args.trust_command == "revoke":
        record = store.revoke_device(args.node_id)
        _print_json(record)
    else:
        print("Usage: shadow trust {local|list|add|revoke}")


# ── Memory Commands (LLM-powered) ────────────────────────────────────────────

def do_ask(question):
    """Ask a freeform question about your clipboard history."""
    from latticeshadow.llm import ShadowLLM, LLMError

    llm = ShadowLLM.from_config()
    if llm is None:
        print("LLM not configured. Run: shadow config set memory.provider gemini")
        return

    vault = get_vault()
    if not vault or vault.count() == 0:
        print("No clipboard history to search.")
        return

    # Search for relevant clips using existing hybrid search
    try:
        results = vault.search(question, n_results=10, hybrid=True)
        if not results or not results.documents:
            print("No relevant clipboard history found.")
            return
    except Exception:
        print("No relevant clipboard history found.")
        return

    # Build context from results
    context_lines = []
    for i, doc in enumerate(results.documents):
        doc_id = results.ids[i] if hasattr(results, 'ids') and results.ids else ""
        ts = _ts_from_doc_id(doc_id)
        ts_str = f" ({ts})" if ts else ""
        context_lines.append(f"[{i+1}]{ts_str}: {doc[:500]}")

    context = "\n\n".join(context_lines)

    try:
        answer = llm.complete(
            system=(
                "You are a helpful assistant. Answer the user's question using ONLY "
                "the clipboard history context provided below. Be concise and specific. "
                "If the answer isn't in the context, say so."
            ),
            user=f"Clipboard context:\n{context}\n\nQuestion: {question}",
        )
        print(answer)
    except LLMError as e:
        print(f"LLM error: {e}")


def do_recap():
    """Summarize today's clipboard activity."""
    from latticeshadow.llm import ShadowLLM, LLMError

    llm = ShadowLLM.from_config()
    if llm is None:
        print("LLM not configured. Run: shadow config set memory.provider gemini")
        return

    vault = get_vault()
    if not vault:
        print("No clipboard history.")
        return

    today_clips = vault.get_today()
    if not today_clips:
        print("No clipboard activity today.")
        return

    # Format clips for the LLM
    clip_lines = []
    for clip in today_clips:
        ts = clip.get("created_at", "")
        text = clip["document"][:300]
        clip_lines.append(f"[{ts}] {text}")

    try:
        answer = llm.complete(
            system=(
                "Summarize this person's day based on their clipboard activity. "
                "Group by time blocks and topics. Use bullet points. Be concise. "
                "Focus on what they were DOING, not what they copied."
            ),
            user="\n\n".join(clip_lines),
        )
        print(f"\n\033[96m── Today's Recap ({len(today_clips)} clips) ──\033[0m\n")
        print(answer)
    except LLMError as e:
        print(f"LLM error: {e}")


def do_context():
    """What are you working on right now?"""
    from latticeshadow.llm import ShadowLLM, LLMError

    llm = ShadowLLM.from_config()
    if llm is None:
        print("LLM not configured. Run: shadow config set memory.provider gemini")
        return

    vault = get_vault()
    if not vault:
        print("No clipboard history.")
        return

    recent = vault.get_recent(limit=20)
    if not recent:
        print("No recent clipboard activity.")
        return

    clip_lines = []
    for clip in recent:
        text = clip["document"][:300]
        tags = clip.get("metadata", {}).get("tags", [])
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        clip_lines.append(f"{text}{tag_str}")

    try:
        answer = llm.complete(
            system=(
                "Based on these recent clipboard entries (most recent first), describe "
                "what the user is currently working on. Be specific about technologies, "
                "files, and tasks. One paragraph, no bullet points."
            ),
            user="\n\n".join(clip_lines),
        )
        print(f"\n\033[96m── Current Context ──\033[0m\n")
        print(answer)
    except LLMError as e:
        print(f"LLM error: {e}")


def do_config(args):
    """View or modify LatticeShadow settings."""
    from latticeshadow import config as cfg

    action = getattr(args, "config_action", None)

    if action == "set":
        from latticeshadow import consent

        surface = next(
            (name for name, spec in consent.SURFACES.items() if spec["config_key"] == args.key),
            None,
        )
        if surface:
            value = args.value.lower()
            if value not in ("on", "off", "true", "false", "yes", "no", "1", "0"):
                raise SystemExit("Use on|off for a capture, listener, or sync setting.")
            consent.set_consent(surface, value in ("on", "true", "yes", "1"))
        else:
            cfg.set(args.key, args.value)
        print(f"Set {args.key} = {args.value}")
        # Show auto-model if it was set
        if args.key == "memory.provider":
            model = cfg.get("memory.model")
            if model:
                print(f"Auto-selected model: {model}")
    elif action == "get":
        value = cfg.get(args.key)
        if value is not None:
            print(f"{args.key} = {value}")
        else:
            print(f"{args.key}: not set")
    elif action == "show":
        print(cfg.format_config())
    elif action == "set-key":
        from latticeshadow.llm import store_api_key, LLMError
        try:
            store_api_key(args.provider, args.api_key)
            print(f"✓ API key for '{args.provider}' stored in Keychain.")
        except LLMError as e:
            print(f"Error: {e}")
    else:
        print("Usage: shadow config {set|get|show|set-key}")
def do_pot(args):
    try:
        from latticeshadow.pot_chain import PoTChain
    except ImportError:
        print("Error: Could not import PoTChain. Ensure cryptography is installed.")
        return

    pot_chain = PoTChain()

    if args.pot_command == "generate":
        proof = pot_chain.generate_proof()
        if not proof:
            print("No Proof of Thought blocks exist yet. Run the daemon to capture events.")
            return
        
        import json
        with open(args.output, "w") as f:
            json.dump(proof, f, indent=2)
        print(f"\u2713 Generated Proof of Thought chain with {len(proof['blocks'])} blocks.")
        print(f"  Saved to: {args.output}")

    elif args.pot_command == "verify":
        import json
        try:
            with open(args.file, "r") as f:
                proof = json.load(f)
        except Exception as e:
            print(f"Failed to load proof file: {e}")
            return
        
        passed, msg = PoTChain.verify_proof(proof)
        if passed:
            print(f"\u2713 {msg}")
        else:
            print(f"\u2717 {msg}")


def do_calibrate(args):
    """Calibrate local alignment or check for cloud drift."""
    if args.check:
        print("\n\033[1mInitializing Active Cognitive Drift Check (50 Anchors)...\033[0m")
        # 50 anchor words/phrases
        ANCHOR_WORDS = [
            "time", "year", "people", "way", "day", "man", "thing", "woman", "life", "child",
            "world", "school", "state", "family", "student", "group", "country", "problem", "hand", "part",
            "place", "case", "week", "company", "system", "program", "question", "work", "government", "number",
            "night", "point", "home", "water", "room", "mother", "area", "money", "story", "fact",
            "month", "lot", "right", "study", "book", "eye", "job", "word", "business", "issue"
        ]
        
        import sys
        import torch
        import numpy as np
        from latticeshadow_db.bridge import ZkBridge
        from latticeshadow_db.alignment import ProcrustesAligner
        from latticeshadow import config as cfg
        
        vault = get_vault()
        
        if not vault._privacy or not vault._privacy._adapter:
            print("Error: Privacy Engine or Cayley adapter not initialized.")
            sys.exit(1)
            
        adapter = vault._privacy._adapter
        local_dim = vault._embedding_dim
        
        pathway = cfg.get("memory.provider") or "gemini"
        pathway_param = pathway if pathway in ("gemini", "custom") else "gemini"
        
        from latticeshadow.llm import _get_api_key
        api_key = _get_api_key(pathway_param)
        api_endpoint = cfg.get(f"memory.endpoints.{pathway_param}")
        mock_mode = (vault._embedder._cloud_fn is None)
        
        # Retrieve or default cloud_dim
        cloud_dim = 1536 if pathway_param == "gemini" else local_dim
        
        aligner = ProcrustesAligner(local_dim=local_dim, cloud_dim=cloud_dim)
        bridge = ZkBridge(
            adapter=adapter,
            aligner=aligner,
            api_endpoint=api_endpoint,
            api_key=api_key,
            pathway=pathway_param,
            mock_mode=mock_mode,
        )
        
        local_states = []
        cloud_states = []
        
        print(f"Obtaining local and cloud activations for {len(ANCHOR_WORDS)} anchor words...")
        for word in ANCHOR_WORDS:
            # Generate local embedding vector
            h_local = vault._embedder.embed(word)
            if h_local.dim() > 1:
                h_local_flat = h_local.view(-1)
            else:
                h_local_flat = h_local
            local_states.append(h_local_flat)
            
            # Rotate and fetch cloud representation
            h_rotated = adapter.rotate(h_local)
            h_cloud = bridge._fetch_cloud_representation(h_rotated, word)
            if h_cloud is None:
                print(f"Error: Failed to retrieve cloud representation for word '{word}'. Aborting.")
                sys.exit(1)
                
            h_cloud_tensor = torch.from_numpy(h_cloud) if not isinstance(h_cloud, torch.Tensor) else h_cloud
            h_cloud_tensor = h_cloud_tensor.to(device=h_local.device, dtype=h_local.dtype)
            if h_cloud_tensor.dim() > 1:
                h_cloud_flat = h_cloud_tensor.view(-1)
            else:
                h_cloud_flat = h_cloud_tensor
            cloud_states.append(h_cloud_flat)

        # Verify shape consistency in cloud states before stacking
        if cloud_states:
            expected_shape = cloud_states[0].shape
            for idx, state in enumerate(cloud_states):
                if state.shape != expected_shape:
                    print(
                        f"Error: Dimension mismatch in cloud representation. "
                        f"Expected shape {list(expected_shape)} (found at entry 0 for word '{ANCHOR_WORDS[0]}'), "
                        f"but got shape {list(state.shape)} at entry {idx} for word '{ANCHOR_WORDS[idx]}'."
                    )
                    sys.exit(1)

        # Stack and convert to numpy
        X_local = torch.stack(local_states).cpu().numpy()
        Y_cloud = torch.stack(cloud_states).cpu().numpy()
        
        # Set the actual cloud dimension dynamically on the aligner
        aligner.cloud_dim = Y_cloud.shape[1]
        
        # Run Procrustes calibration
        metrics = aligner.calibrate(X_local, Y_cloud)
        
        print("\n\033[1m--- SVD Alignment Health Audit ---\033[0m")
        print(f"  Average Cosine Correlation: {metrics['correlation']:.6f}")
        print(f"  Shannon Entropy (H):        {metrics['entropy']:.6f}")
        print(f"  Condition Number (\u03ba):       {metrics['condition_number']:.6f}")
        print(f"  Matrix Trace (Trace(Q)):    {metrics['trace']:.6f}")
        print(f"  Geometric Residuals (MSE):  {metrics['mse']:.6f}")
        
        # Check thresholds
        max_entropy = np.log(min(local_dim, aligner.cloud_dim))
        entropy_threshold = 0.6 * max_entropy
        
        failures = []
        if metrics["entropy"] < entropy_threshold or metrics["entropy"] < 3.0:
            failures.append(f"Shannon Entropy too low ({metrics['entropy']:.4f} < {max(3.0, entropy_threshold):.4f})")
        if metrics["condition_number"] > 150.0:
            failures.append(f"Condition Number too high ({metrics['condition_number']:.4f} > 150.0)")
        if metrics["mse"] > 0.15:
            failures.append(f"Residual MSE too high ({metrics['mse']:.4f} > 0.15)")
        if metrics["correlation"] < 0.75:
            failures.append(f"Correlation too low ({metrics['correlation']:.4f} < 0.75)")
            
        if failures:
            alert_msg = "Active audit detected alignment warnings: " + ", ".join(failures)
            print(f"\n\033[91m[WARNING] {alert_msg}\033[0m")
            from latticeshadow.notifications import notify_drift
            notify_drift("LatticeShadow Alignment Warning", alert_msg)
            print("\u2713 Dispatched native macOS desktop warning notification.")
        else:
            print("\n\033[92m\u2713 Latent space alignment is stable. No drift detected.\033[0m")
    else:
        print("Please specify --check to run active drift calibration verification.")




# ── Holographic Memory Indexing ──────────────────────────────────────────────

def do_compile(day_str=None):
    """Compile history into a holographic memory vector."""
    from latticeshadow.holographic_index import HolographicIndex

    vault = get_vault()
    if not vault:
        print("No clipboard history found.")
        return

    # Check if a specific day is requested, otherwise use today
    if day_str:
        with vault._store._connect() as conn:
            cursor = conn.execute(
                """SELECT document FROM vectors
                   WHERE collection = ? AND date(created_at) = date(?)
                   ORDER BY created_at ASC""",
                (vault.name, day_str),
            )
            clips = []
            for row in cursor:
                doc = row[0]
                if vault._privacy and doc and doc.startswith("enc:"):
                    try:
                        doc = vault._privacy.decrypt_document(doc)
                    except Exception:
                        pass
                clips.append({"document": doc})
    else:
        clips = vault.get_today()

    if not clips:
        day_label = day_str if day_str else "today"
        print(f"No clips found for {day_label} to compile.")
        return

    print(f"Compiling {len(clips)} clips into holographic memory...")

    texts = [c["document"] for c in clips]
    embeddings = [vault._embedder.embed(t) for t in texts]

    # Create holographic index with the same dimension
    dim = vault._embedding_dim
    index = HolographicIndex(dim=dim)
    index.compile(texts, embeddings)

    # Save path: ~/.latticeshadow/holographic_<day>.bin
    filename = f"holographic_{day_str or 'today'}.bin"
    save_path = os.path.join(get_log_dir(), filename)
    index.save(save_path)
    print(f"✓ Holographic memory index written to {save_path} (Size: {os.path.getsize(save_path)/1024:.1f} KB)")


def do_recall(query, day_str=None):
    """Recall a document from the holographic memory index."""
    from latticeshadow.holographic_index import HolographicIndex

    filename = f"holographic_{day_str or 'today'}.bin"
    save_path = os.path.join(get_log_dir(), filename)

    if not os.path.exists(save_path):
        print(f"No holographic memory index found at {save_path}. Run 'shadow compile' first.")
        return

    vault = get_vault()
    if not vault:
        print("Database not initialized.")
        return

    # Embed the query
    query_emb = vault._embedder.embed(query)

    print(f"Recalling '{query}' from holographic index {filename}...")
    index = HolographicIndex.load(save_path)
    result, score = index.recall(query_emb)

    if result:
        print(f"\n\033[92m★ Holographic Recall Result (Match score: {score:.4f}) ★\033[0m\n")
        print(result)
        print()
    else:
        print(f"No match found in holographic memory (Match score: {score:.4f} is too low).")


def generate_speculative_fix(vault, loop_docs=None) -> str | None:
    """Generate a speculative fix command based on the last clipboard error and command history."""
    if loop_docs:
        try:
            from latticeshadow.llm import ShadowLLM
            llm = ShadowLLM.from_config()
            if llm:
                prompt = (
                    "The user is stuck in a terminal/clipboard debugging loop. "
                    "Here is the sequence of recent clipboard inputs/tracebacks (oldest to newest):\n\n"
                )
                for idx, doc in enumerate(reversed(loop_docs)):
                    prompt += f"--- Event {idx + 1} ---\n{doc}\n\n"
                prompt += (
                    "Analyze the sequence. Determine what the user is trying to accomplish and why it is failing. "
                    "Provide a single terminal command that will solve or bypass this issue. "
                    "Your response must contain ONLY the command itself, without backticks, quotes, or explanations."
                )
                fix = llm.complete("You are an expert system recovery assistant.", prompt)
                if fix:
                    return fix.strip()
        except Exception:
            pass

    try:
        # 1. Grab last clipboard content
        try:
            import AppKit
            pb = AppKit.NSPasteboard.generalPasteboard()
            clip_content = pb.stringForType_(AppKit.NSPasteboardTypeString)
        except Exception:
            proc = subprocess.run(["pbpaste"], capture_output=True, text=True)
            clip_content = proc.stdout

        if not clip_content:
            return None

        clip_content = clip_content.strip()

        # 2. Check if clipboard text looks like an error/traceback
        error_keywords = ["error", "exception", "traceback", "failed", "exit code", "err!", "fatal", "undefined", "crash"]
        looks_like_error = any(kw in clip_content.lower() for kw in error_keywords) or len(clip_content.splitlines()) > 5

        if not looks_like_error:
            return None

        # 3. Find past matching errors using semantic search
        results = vault.search(clip_content, n_results=5)
        if not results or not getattr(results, "ids", None):
            return None

        past_error_times = []
        with vault._store._connect() as conn:
            placeholders = ",".join("?" * len(results.ids))
            cursor = conn.execute(
                f"SELECT doc_id, created_at FROM vectors WHERE doc_id IN ({placeholders}) AND collection = ?",
                results.ids + [vault.name]
            )
            for doc_id, created_at in cursor.fetchall():
                past_error_times.append((doc_id, created_at))

        if not past_error_times:
            return None

        # For each past error, query the command history executed within +5 minutes
        suggested_commands = []
        with vault._store._connect() as conn:
            for doc_id, created_at in past_error_times:
                cursor = conn.execute(
                    """
                    SELECT document FROM vectors 
                    WHERE collection = ? 
                      AND json_extract(metadata_json, '$.source') = 'terminal'
                      AND created_at >= ?
                      AND created_at <= datetime(?, '+5 minutes')
                    ORDER BY created_at ASC
                    """,
                    (vault.name, created_at, created_at)
                )
                for row in cursor.fetchall():
                    cmd = row[0]
                    if vault._privacy and cmd and cmd.startswith("enc:"):
                        try:
                            cmd = vault._privacy.decrypt_document(cmd)
                        except Exception:
                            pass
                    suggested_commands.append(cmd)

        if not suggested_commands:
            return None

        # Rank suggestions
        from collections import Counter
        counts = Counter(suggested_commands)
        ignored = {"ls", "clear", "pwd", "cd", "shadow doctor", "shadow search", "git status"}
        ranked = [cmd for cmd, count in counts.most_common() if cmd.strip() not in ignored]

        if not ranked:
            ranked = [cmd for cmd, count in counts.most_common()]

        if ranked:
            return ranked[0]
        return None
    except Exception:
        return None


def do_get_ghost_paste():
    """Print the speculative ghost paste from .ambient_context"""
    context_path = os.path.join(get_log_dir(), ".ambient_context")
    if os.path.exists(context_path):
        try:
            with open(context_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    print(content)
                    sys.exit(0)
        except Exception:
            pass
    sys.exit(1)

def do_compose():
    from latticeshadow.compose import NeuralComposer
    composer = NeuralComposer()
    result = composer.predict_next_command()
    if result:
        print(result)

def do_time_travel(args):
    from latticeshadow import config
    from latticeshadow.time_travel import EnvironmentSnapshotter
    
    enabled = config.get("time_travel.enabled") or False
    if not enabled:
        print("Time travel is not enabled in your config. Set time_travel.enabled to true.")
        sys.exit(1)
        
    snapshotter = EnvironmentSnapshotter(time_travel_enabled=True)
    if args.list:
        snapshots = snapshotter.list_snapshots()
        if snapshots:
            print("Available snapshots:")
            for s in snapshots:
                print(f"  - {s}")
        else:
            print("No snapshots available.")
    elif args.rollback:
        snapshotter.rollback(args.rollback)
    else:
        print("Please specify --list or --rollback <hash>")


def do_get_loop_fix():
    speculative_fix_path = os.path.join(get_log_dir(), ".speculative_fix")
    if os.path.exists(speculative_fix_path):
        try:
            with open(speculative_fix_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                print(content)
                sys.exit(0)
        except Exception:
            pass
    sys.exit(1)


def do_ghost_paste(query):
    # 1. Retrieve match from the vault
    vault = get_vault()
    if not vault:
        print("Error: Database not initialized.")
        sys.exit(1)
    try:
        res = vault.search(query, n_results=1, hybrid=True)
        if not res or not res.documents:
            print("No matching vault entry found.")
            sys.exit(1)
        target_text = res.documents[0]
    except Exception as e:
        print(f"Search failed: {e}")
        sys.exit(1)

    # 2. Back up entire NSPasteboard (all formats)
    import AppKit
    pb = AppKit.NSPasteboard.generalPasteboard()
    backup = []
    for item in pb.pasteboardItems() or []:
        item_backup = {}
        for ptype in item.types():
            data = pb.dataForType_(ptype)
            if data:
                item_backup[ptype] = data
        if item_backup:
            backup.append(item_backup)

    try:
        # 3. Write target text to pasteboard
        pb.clearContents()
        pb.setString_forType_(target_text, AppKit.NSPasteboardTypeString)

        # 4. Simulate Cmd+V keystroke
        script = 'tell application "System Events" to keystroke "v" using {command down}'
        subprocess.run(["osascript", "-e", script], check=True)

        # 5. Small delay to allow the active application to read the pasteboard
        time.sleep(0.15)
    except Exception as e:
        print(f"Keystroke simulation failed: {e}")
    finally:
        # 6. Restore original pasteboard items
        pb.clearContents()
        if backup:
            for item_backup in backup:
                pb.declareTypes_owner_(list(item_backup.keys()), None)
                for ptype, data in item_backup.items():
                    pb.setData_forType_(data, ptype)
        print("✓ Ghost paste completed. Clipboard restored.")


def do_fix():
    """Retrieve the last copied traceback/error and suggest a fix command based on historical actions."""
    # 1. Grab last clipboard content
    try:
        import AppKit
        pb = AppKit.NSPasteboard.generalPasteboard()
        clip_content = pb.stringForType_(AppKit.NSPasteboardTypeString)
    except Exception:
        # Fallback to pbpaste if PyObjC AppKit is not present/fails
        proc = subprocess.run(["pbpaste"], capture_output=True, text=True)
        clip_content = proc.stdout
        
    if not clip_content:
        print("Clipboard is empty.")
        return
        
    clip_content = clip_content.strip()
    
    # 2. Check if clipboard text looks like an error/traceback
    error_keywords = ["error", "exception", "traceback", "failed", "exit code", "err!", "fatal", "undefined", "crash"]
    looks_like_error = any(kw in clip_content.lower() for kw in error_keywords) or len(clip_content.splitlines()) > 5
    
    if not looks_like_error:
        print("The current clipboard text doesn't look like an error traceback.")
        print("Clipboard preview:\n" + clip_content[:200] + "...")
        print("To search anyway, run: shadow search '<query>'")
        return
        
    vault = get_vault()
    if not vault:
        return
        
    print("Analyzing clipboard error...")
    
    # 3. Find past matching errors using semantic search
    results = vault.search(clip_content, n_results=5)
    if not results.ids:
        print("No matching past errors found in memory.")
        return
        
    # We will search the DB for commands executed after those past clipboard errors.
    past_error_times = []
    with vault._store._connect() as conn:
        placeholders = ",".join("?" * len(results.ids))
        cursor = conn.execute(
            f"SELECT doc_id, created_at FROM vectors WHERE doc_id IN ({placeholders}) AND collection = ?",
            results.ids + [vault.name]
        )
        for doc_id, created_at in cursor.fetchall():
            past_error_times.append((doc_id, created_at))
            
    if not past_error_times:
        print("No timestamp metadata available for past errors.")
        return
        
    # For each past error, query the command history executed within +5 minutes
    suggested_commands = []
    with vault._store._connect() as conn:
        for doc_id, created_at in past_error_times:
            cursor = conn.execute(
                """
                SELECT document FROM vectors 
                WHERE collection = ? 
                  AND json_extract(metadata_json, '$.source') = 'terminal'
                  AND created_at >= ?
                  AND created_at <= datetime(?, '+5 minutes')
                ORDER BY created_at ASC
                """,
                (vault.name, created_at, created_at)
            )
            cmds = [row[0] for row in cursor.fetchall()]
            if cmds:
                suggested_commands.extend(cmds)
                
    if not suggested_commands:
        print("Found past instances of this error, but no shell commands were logged immediately after them.")
        return
        
    # 4. Rank suggestions by frequency/recency
    from collections import Counter
    counts = Counter(suggested_commands)
    # Filter out common simple commands
    ignored = {"ls", "clear", "pwd", "cd", "shadow doctor", "shadow search", "git status"}
    ranked = [cmd for cmd, count in counts.most_common() if cmd.strip() not in ignored]
    
    if not ranked:
        ranked = [cmd for cmd, count in counts.most_common()]
        
    if ranked:
        best_fix = ranked[0]
        print("\n\033[92m★ Speculative Fix Found! ★\033[0m")
        print(f"Based on historical clipboard errors and shell commands, the fix command is likely:\n")
        print(f"  \033[96m\033[1m{best_fix}\033[0m")
        print()
        try:
            from latticeshadow.repair_queue import create_repair_proposal

            proposal = create_repair_proposal(
                summary="Historical traceback recovery command",
                source="fix",
                command=best_fix,
                risk="medium",
                provenance={"matched_errors": len(past_error_times)},
                data_dir=get_log_dir(),
            )
            print(f"Queued repair proposal: {proposal['id']}")
        except Exception:
            pass
        subprocess.run(["pbcopy"], input=best_fix.encode("utf-8"))
        print("\033[90m(This command has been copied to your clipboard. Press Cmd+V in terminal to paste and run it.)\033[0m\n")
    else:
        print("No fix commands could be extracted from shell history.")


def do_gui():
    """Start the native macOS Menu Bar application."""
    try:
        from latticeshadow.menu import run_menu_app
        print("Starting native macOS Menu Bar application...")
        run_menu_app()
    except Exception as e:
        print(f"Failed to start GUI: {e}")
        print("Ensure 'pyobjc-framework-Cocoa' is installed.")


def do_doctor():
    """Run a diagnostic health check on LatticeShadow."""
    import stat
    import plistlib
    from latticeshadow import config as cfg
    from latticeshadow.llm import ShadowLLM

    print("\n\033[1mLatticeShadow Health Check\033[0m")
    print("──────────────────────────────")

    checks = []
    
    # 1. Python Path & Version
    checks.append((True, "Python", f"{sys.executable} ({sys.version.split()[0]})", None))

    # 2. PyObjC
    try:
        import AppKit
        checks.append((True, "PyObjC", "AppKit importable", None))
    except ImportError:
        checks.append((False, "PyObjC", "AppKit NOT found", "pip install pyobjc-framework-Cocoa"))

    # 3. Core Dependencies
    missing_deps = []
    for dep in ["torch", "numpy"]:
        try:
            __import__(dep)
        except ImportError:
            missing_deps.append(dep)
    if missing_deps:
        checks.append((True, "Dependencies (Lightweight Mode)", f"Optional dependencies {', '.join(missing_deps)} not found. Local ML and alignment capabilities will be disabled.", f"Install them if you want local vectors: pip install {' '.join(missing_deps)}"))
    else:
        checks.append((True, "Dependencies", "torch, numpy — all found (Local ML acceleration active)", None))

    # 4. Data directory
    log_dir = get_log_dir()
    if os.path.exists(log_dir):
        mode = os.stat(log_dir).st_mode
        if (mode & 0o077) != 0:
            checks.append((False, "Data Directory", f"Exists at {log_dir} but permissions are too open: {oct(mode & 0o777)}", "chmod 700 " + log_dir))
        else:
            checks.append((True, "Data Directory", f"Exists at {log_dir} with secure permissions", None))
    else:
        checks.append((False, "Data Directory", "NOT found", "run 'shadow install'"))

    # 5. Master key in Keychain
    key_found = False
    try:
        if keychain.retrieve_key():
            checks.append((True, "Master key", "Keychain entry found (com.latticedb.shadow)", None))
            key_found = True
        else:
            checks.append((False, "Master key", "Keychain entry NOT found", "run 'shadow install' or unlock macOS Keychain"))
    except Exception as e:
        checks.append((False, "Master key", f"Error accessing Keychain: {e}", "run 'shadow install'"))

    # 6. Key backup file
    key_file = get_key_file()
    if os.path.exists(key_file):
        mode = os.stat(key_file).st_mode
        if (mode & 0o177) != 0:
            checks.append((False, "Key backup", f"Exists at {key_file} but permissions are too open: {oct(mode & 0o777)}", "chmod 600 " + key_file))
        else:
            checks.append((True, "Key backup", f"Exists at {key_file} with secure permissions", None))
    else:
        checks.append((False, "Key backup", "NOT found", "run 'shadow install' to regenerate key & backup"))

    # 7. Database & entries
    db_path = get_db_path()
    if os.path.exists(db_path):
        mode = os.stat(db_path).st_mode
        too_open = (mode & 0o177) != 0
        try:
            vault = get_vault()
            count = vault.count()
            db_detail = f"shadow.sqlite ({count} entries)"
            if too_open:
                checks.append((False, "Database", f"Exists but permissions are too open: {oct(mode & 0o777)}", "chmod 600 " + db_path))
            else:
                checks.append((True, "Database", db_detail, None))
        except Exception as e:
            checks.append((False, "Database", f"Failed to open database: {e}", "run 'shadow shred' to reset database"))
    else:
        checks.append((False, "Database", "NOT found", "run 'shadow install' and copy some text to initialize"))

    # 8. Plist Agent
    if os.path.exists(PLIST_PATH):
        checks.append((True, "Launchd Plist", f"Exists at {PLIST_PATH}", None))
    else:
        checks.append((False, "Launchd Plist", "NOT found", "run 'shadow install'"))

    # 9. Plist Python mismatch check
    if os.path.exists(PLIST_PATH):
        try:
            with open(PLIST_PATH, "rb") as f:
                pl = plistlib.load(f)
            args = pl.get("ProgramArguments", [])
            if args and args[0] != sys.executable:
                checks.append((False, "Plist Python", f"Mismatch! Plist uses {args[0]}, current is {sys.executable}", "run 'shadow install' to rewrite plist"))
            else:
                checks.append((True, "Plist Python", "Matches current Python interpreter", None))
        except Exception as e:
            checks.append((False, "Plist Python", f"Failed to parse plist: {e}", "run 'shadow install'"))
    else:
        checks.append((True, "Plist Python", "Launchd Plist not present (skipped)", None))

    # 10. Daemon status
    res = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    is_running = False
    pid = None
    for line in res.stdout.splitlines():
        if PLIST_LABEL in line:
            parts = line.split()
            if len(parts) >= 3:
                is_running = parts[0] != "-"
                if is_running:
                    pid = parts[0]
            else:
                is_running = True
            break
    if is_running:
        checks.append((True, "Daemon status", f"RUNNING (PID {pid})", None))
    else:
        checks.append((False, "Daemon status", "NOT RUNNING", "run 'shadow enable'"))

    # 11. Shell alias
    zshrc = os.path.expanduser("~/.zshrc")
    alias_found = False
    if os.path.exists(zshrc):
        with open(zshrc, "r") as f:
            content = f.read()
        if ALIAS_MARKER in content:
            alias_found = True
    if alias_found:
        checks.append((True, "Shell alias", "Found 'shadow' alias in ~/.zshrc", None))
    else:
        checks.append((False, "Shell alias", "NOT found in ~/.zshrc", "run 'shadow install'"))

    # 12. Config parse
    try:
        cfg_data = cfg.load_config()
        checks.append((True, "Config validation", "config.toml valid", None))
    except Exception as e:
        checks.append((False, "Config validation", f"Failed to parse config: {e}", "delete ~/.latticeshadow/config.toml and re-run settings"))

    # 13. LLM provider reachable
    provider = cfg.get("memory.provider")
    if provider and provider != "none":
        llm = ShadowLLM.from_config()
        if llm:
            try:
                # Use quick reachability/models check or a timeout check
                if llm.is_reachable():
                    checks.append((True, "LLM Reachability", f"{provider} reachable (model: {llm.model})", None))
                else:
                    checks.append((False, "LLM Reachability", f"{provider} endpoint NOT reachable at {llm.endpoint}", "check internet connection or model server status"))
            except Exception as e:
                checks.append((False, "LLM Reachability", f"Error connecting to {provider}: {e}", "verify API key or endpoint configuration"))
        else:
            checks.append((False, "LLM Reachability", f"LLM client could not be created for provider {provider}", f"run 'shadow config set-key {provider} <key>' or verify endpoint"))
    else:
        checks.append((True, "LLM Reachability", "LLM disabled (provider is 'none')", None))

    # Print results
    failed_checks = 0
    for ok, label, detail, fix in checks:
        if ok:
            print(f"\033[92m✓\033[0m \033[1m{label:<15}\033[0m: {detail}")
        else:
            print(f"\033[91m✗\033[0m \033[1m{label:<15}\033[0m: {detail}")
            if fix:
                print(f"  \033[90m↳ Suggestion: {fix}\033[0m")
            failed_checks += 1

    print("──────────────────────────────")
    if failed_checks == 0:
        print("\033[92mAll checks passed. LatticeShadow is healthy!\033[0m\n")
    else:
        print(f"\033[91m{failed_checks} issue(s) detected. Please run the suggestions above.\033[0m\n")


def do_evolve():
    """Review and apply pending code mutations generated by the Auto-Doctor."""
    import glob
    diffs_dir = os.path.expanduser("~/.latticeshadow/pending_diffs")
    diff_files = glob.glob(os.path.join(diffs_dir, "*.diff"))
    
    if not diff_files:
        print("\u2717 No pending code mutations from the Auto-Doctor.")
        return
        
    print(f"\n\033[1mFound {len(diff_files)} pending code mutation(s):\033[0m")
    print("──────────────────────────────────────────")
    
    for idx, diff_path in enumerate(diff_files):
        print(f"\n[{idx + 1}] Mutation file: {os.path.basename(diff_path)}")
        with open(diff_path, "r") as f:
            diff_content = f.read()
            
        # Print colored unified diff lines
        for line in diff_content.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                print(f"\033[32m{line}\033[0m") # Green
            elif line.startswith("-") and not line.startswith("---"):
                print(f"\033[31m{line}\033[0m") # Red
            elif line.startswith("@@"):
                print(f"\033[36m{line}\033[0m") # Cyan
            else:
                print(line)
                
        resp = input("\nApply this mutation to your working tree? (y/N): ").strip().lower()
        if resp in ("y", "yes"):
            # Apply diff using git apply
            res = subprocess.run(
                ["git", "apply", diff_path],
                capture_output=True,
                text=True
            )
            if res.returncode == 0:
                print("\u2713 Successfully applied mutation!")
                try:
                    from latticeshadow.integrity import compute_manifest, write_manifest
                    write_manifest(compute_manifest())
                    print("\u2713 Refreshed daemon integrity manifest for the accepted mutation.")
                except Exception as e:
                    print(f"Warning: mutation applied, but integrity manifest refresh failed: {e}")
                os.remove(diff_path)
            else:
                print(f"\u2717 Failed to apply mutation: {res.stderr}")
        else:
            print("Mutation skipped.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LatticeShadow — Private Local-First Memory Companion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""commands:
  search <query>  Search saved memories semantically
  paste  <query>  Search and copy the #1 result back to clipboard
  watch           Live stream of clipboard captures (Ctrl+C to stop)
  status          Check daemon and database status
  pause           Pause capture without changing source choices
  resume          Resume chosen capture sources
  now             Show current private memory context
  timeline        Show a recent or searched memory timeline
  remember        Add a normalized memory event
  why <query>     Explain relevance with matching memory events
  summarize       Summarize current or searched memory context
  open-context    Open a URL/file target from matching context
  forget          Delete selected memory events with confirmation
  privacy-report  Inspect local privacy posture
  privacy-test    Run synthetic privacy leakage harness
  consent          Manage listener and sync consent
  audit            Verify signed local audit log
  trust            Manage paired-device trust
  repair           Review human-approved repair proposals
  native intents   Show native App Intents command contract
  bench moonshot  Run a moonshot retrieval smoke benchmark
  mcp serve       Serve redacted memory over MCP stdio
  sleep           Trigger REM sleep consolidation
  shred           Panic button: crypto-shred all history
  install         Install the background daemon and CLI alias
  enable          Start the background daemon
  disable         Stop the background daemon
  remove          Uninstall everything
  doctor          Run diagnostic health check
  compile         Compile daily history into a holographic memory vector
  recall <query>  Recall a document from the holographic memory vector
  gui             Start native macOS Menu Bar GUI
  fix             Speculatively resolve clipboard error tracebacks from history
  --- Memory (LLM-powered) ---
  ask    <question> Ask a question about your clipboard history
  recap             Summarize today's clipboard activity
  context           What are you working on right now?
  config            View or modify LatticeShadow settings""",
    )
    subparsers = parser.add_subparsers(dest="command")

    sp = subparsers.add_parser("search", help="Search clipboard history")
    sp.add_argument("query", type=str, help="Semantic query")

    unsp = subparsers.add_parser("unswap", help="Restore the state of a backgrounded application using semantic context")
    unsp.add_argument("query", type=str, help="Semantic query of the lost state")

    pp = subparsers.add_parser("paste", help="Search and re-copy #1 result to clipboard")
    pp.add_argument("query", type=str, help="Semantic query")

    subparsers.add_parser("watch", help="Live stream of clipboard captures")
    subparsers.add_parser("status", help="Check daemon and database status")
    subparsers.add_parser("pause", help="Persistently pause all capture sources")
    subparsers.add_parser("resume", help="Resume chosen capture sources after consent")

    now_p = subparsers.add_parser("now", help="Show current private memory context")
    now_p.add_argument("--limit", type=int, default=20, help="Number of recent events to show")
    now_p.add_argument("--json", action="store_true", help="Emit JSON")

    timeline_p = subparsers.add_parser("timeline", help="Show a recent or searched memory timeline")
    timeline_p.add_argument("--limit", type=int, default=20, help="Number of events to show")
    timeline_p.add_argument("--source", type=str, help="Filter by event source/type")
    timeline_p.add_argument("--since", type=str, help="Only events after this SQLite timestamp/date")
    timeline_p.add_argument("--until", type=str, help="Only events before this SQLite timestamp/date")
    timeline_p.add_argument("--query", type=str, help="Semantic search query")
    timeline_p.add_argument("--json", action="store_true", help="Emit JSON")

    remember_p = subparsers.add_parser("remember", help="Add a normalized memory event")
    remember_p.add_argument(
        "event_type",
        choices=["clipboard", "terminal", "ambient", "file", "url", "app", "repair", "sync", "model_call"],
        help="Event type",
    )
    remember_p.add_argument("text", type=str, help="Event text")
    remember_p.add_argument("--metadata", type=str, help="Additional metadata JSON")
    remember_p.add_argument("--id", type=str, help="Explicit document id")

    why_p = subparsers.add_parser("why", help="Show matching memory events for a query")
    why_p.add_argument("query", type=str, help="Semantic query")
    why_p.add_argument("--limit", type=int, default=5, help="Number of matching events")

    summarize_p = subparsers.add_parser("summarize", help="Summarize current or searched memory context")
    summarize_p.add_argument("--query", type=str, help="Semantic query")
    summarize_p.add_argument("--limit", type=int, default=20, help="Number of events")
    summarize_p.add_argument("--json", action="store_true", help="Emit JSON")
    summarize_p.add_argument("--no-foundation", action="store_true", help="Skip Foundation Models bridge")

    open_p = subparsers.add_parser("open-context", help="Open a URL/file target from matching context")
    open_p.add_argument("query", type=str, help="Semantic query")
    open_p.add_argument("--limit", type=int, default=10, help="Number of matching events to inspect")
    open_p.add_argument("--dry-run", action="store_true", help="Print the target without opening it")

    forget_p = subparsers.add_parser("forget", help="Delete selected memory events")
    forget_p.add_argument("--id", action="append", help="Memory document id to delete")
    forget_p.add_argument("--query", type=str, help="Delete top matches for a semantic query")
    forget_p.add_argument("--source", type=str, help="Delete recent events from a source/type")
    forget_p.add_argument("--limit", type=int, default=10, help="Maximum events selected by query/source")
    forget_p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    privacy_p = subparsers.add_parser("privacy-report", help="Inspect local privacy posture")
    privacy_p.add_argument("--json", action="store_true", help="Emit JSON")

    privacy_test_p = subparsers.add_parser("privacy-test", help="Run synthetic privacy leakage harness")
    privacy_test_p.add_argument("--count", type=int, default=32, help="Number of synthetic events")
    privacy_test_p.add_argument("--dim", type=int, default=128, help="Embedding dimension")
    privacy_test_p.add_argument("--queries", type=int, default=8, help="Number of harness queries")
    privacy_test_p.add_argument("--output", type=str, help="Write JSON report to a file")

    consent_p = subparsers.add_parser("consent", help="Manage listener and sync consent")
    consent_sub = consent_p.add_subparsers(dest="consent_command")
    consent_sub.add_parser("status", help="Show consent status")
    consent_sub.add_parser("wizard", help="Run the first-run consent wizard")
    consent_set = consent_sub.add_parser("set", help="Set consent for a surface")
    consent_set.add_argument("surface", type=str, help="Consent surface name")
    consent_set.add_argument("state", choices=["on", "off", "true", "false", "yes", "no"], help="Consent state")

    audit_p = subparsers.add_parser("audit", help="Verify signed local audit log")
    audit_p.add_argument("--json", action="store_true", help="Emit JSON")

    trust_p = subparsers.add_parser("trust", help="Manage paired-device trust")
    trust_sub = trust_p.add_subparsers(dest="trust_command")
    trust_local = trust_sub.add_parser("local", help="Show this device public identity")
    trust_local.add_argument("--node-id", type=str, help="Node id to include in the pairing payload")
    trust_sub.add_parser("list", help="List trusted devices")
    trust_add = trust_sub.add_parser("add", help="Trust a peer device")
    trust_add.add_argument("node_id", type=str, help="Peer node id")
    trust_add.add_argument("public_key", type=str, help="Peer Ed25519 public key hex")
    trust_add.add_argument("--label", type=str, help="Human-readable label")
    trust_revoke = trust_sub.add_parser("revoke", help="Revoke a trusted device")
    trust_revoke.add_argument("node_id", type=str, help="Peer node id")

    repair_p = subparsers.add_parser("repair", help="Review human-approved repair proposals")
    repair_sub = repair_p.add_subparsers(dest="repair_command")
    repair_list = repair_sub.add_parser("list", help="List repair proposals")
    repair_list.add_argument("--status", type=str, help="Filter by status")
    repair_list.add_argument("--limit", type=int, default=20, help="Maximum proposals to show")
    repair_list.add_argument("--json", action="store_true", help="Emit JSON")
    repair_status = repair_sub.add_parser("status", help="Update a repair proposal status")
    repair_status.add_argument("id", type=str, help="Repair proposal id")
    repair_status.add_argument("status", choices=["pending", "approved", "rejected", "applied"], help="New status")

    native_p = subparsers.add_parser("native", help="Native macOS companion contracts")
    native_sub = native_p.add_subparsers(dest="native_command")
    native_intents = native_sub.add_parser("intents", help="Show App Intents command contract")
    native_intents.add_argument("--json", action="store_true", help="Emit JSON")

    bench_p = subparsers.add_parser("bench", help="Benchmark experimental memory paths")
    bench_sub = bench_p.add_subparsers(dest="bench_command")
    moonshot_p = bench_sub.add_parser("moonshot", help="Run a moonshot retrieval smoke benchmark")
    moonshot_p.add_argument("--count", type=int, default=250, help="Number of synthetic events")
    moonshot_p.add_argument("--dim", type=int, default=128, help="Embedding dimension")
    moonshot_p.add_argument("--queries", type=int, default=10, help="Number of benchmark queries")
    moonshot_p.add_argument(
        "--engine",
        action="append",
        choices=["streaming_exact", "hnsw_rerank", "pq_rerank", "diskann_rerank", "matryoshka_64", "all"],
        help="Retrieval lane to benchmark; repeat or use 'all'",
    )
    moonshot_p.add_argument("--output", type=str, help="Write JSON report to a file")

    mcp_p = subparsers.add_parser("mcp", help="MCP memory server commands")
    mcp_sub = mcp_p.add_subparsers(dest="mcp_command")
    mcp_sub.add_parser("serve", help="Serve redacted memory over MCP stdio")

    subparsers.add_parser("sleep", help="Trigger REM sleep consolidation")
    subparsers.add_parser("shred", help="Crypto-shred clipboard history")
    subparsers.add_parser("install", help="Prepare the daemon without enabling capture or shell hooks")
    rebuild_p = subparsers.add_parser("rebuild-index", help="Re-embed saved events with the pinned local model")
    rebuild_p.add_argument("--yes", action="store_true", help="Skip the REBUILD prompt")
    shell_p = subparsers.add_parser("shell", help="Manage optional Zsh widgets")
    shell_sub = shell_p.add_subparsers(dest="shell_command", required=True)
    shell_sub.add_parser("enable")
    shell_sub.add_parser("disable")
    subparsers.add_parser("enable", help="Start the daemon")
    subparsers.add_parser("disable", help="Stop the daemon")
    subparsers.add_parser("remove", help="Uninstall everything")
    subparsers.add_parser("doctor", help="Run diagnostic health check")

    # Drift Alarms / Calibration commands
    cal_p = subparsers.add_parser("calibrate", help="Check for cloud drift or calibrate local alignment")
    cal_p.add_argument("--check", action="store_true", help="Run active SVD drift check on 50 anchor phrases")

    # Holographic Memory commands
    comp = subparsers.add_parser("compile", help="Compile daily history into a holographic memory vector")
    comp.add_argument("--day", type=str, help="Specific day in YYYY-MM-DD format (defaults to today)")

    rec = subparsers.add_parser("recall", help="Recall a document from the holographic memory vector")
    rec.add_argument("query", type=str, help="Semantic query")
    rec.add_argument("--day", type=str, help="Specific day in YYYY-MM-DD format (defaults to today)")

    # GUI command
    subparsers.add_parser("gui", help="Start native macOS Menu Bar GUI")
    
    subparsers.add_parser("compose", help="Predict and compose the next shell command based on context")
    subparsers.add_parser("fix", help="Speculatively fix the last clipboard error traceback based on history")
    subparsers.add_parser("evolve", help="Review and apply pending code mutations generated by the Auto-Doctor")
    
    tt_p = subparsers.add_parser("time-travel", help="Environment state rollback ('Git for Reality')")
    tt_p.add_argument("--list", action="store_true", help="List available snapshots")
    tt_p.add_argument("--rollback", type=str, help="Rollback to a specific snapshot hash")

    subparsers.add_parser("get-ghost-paste", help="Get the ambient context content")
    subparsers.add_parser("get-loop-fix", help="Get the speculative loop fix command")
    gpp = subparsers.add_parser("ghost-paste", help="Speculatively paste vault query matches")
    gpp.add_argument("query", type=str, help="Semantic query to search and paste")

    # Memory commands (LLM-powered)
    ap = subparsers.add_parser("ask", help="Ask a question about clipboard history")
    ap.add_argument("question", type=str, help="Freeform question")

    subparsers.add_parser("recap", help="Summarize today's clipboard activity")
    subparsers.add_parser("context", help="What are you working on right now?")

    # Config commands
    cp = subparsers.add_parser("config", help="View or modify settings")
    config_sub = cp.add_subparsers(dest="config_action")

    cs = config_sub.add_parser("set", help="Set a config value")
    cs.add_argument("key", type=str, help="Config key (dot notation, e.g. memory.provider)")
    cs.add_argument("value", type=str, help="Value to set")

    cg = config_sub.add_parser("get", help="Get a config value")
    cg.add_argument("key", type=str, help="Config key (dot notation)")

    config_sub.add_parser("show", help="Show all settings")

    ck = config_sub.add_parser("set-key", help="Store an API key in Keychain")
    ck.add_argument("provider", type=str, help="Provider name (gemini, openai)")
    ck.add_argument("api_key", type=str, help="API key")

    pot_p = subparsers.add_parser("pot", help="Proof of Thought (PoT) commands")
    pot_sub = pot_p.add_subparsers(dest="pot_command", required=True)
    
    pot_gen = pot_sub.add_parser("generate", help="Generate a signed PoT proof file")
    pot_gen.add_argument("--output", type=str, default="proof.json", help="Output file path")
    
    pot_ver = pot_sub.add_parser("verify", help="Verify a signed PoT proof file")
    pot_ver.add_argument("file", type=str, help="Path to proof file")

    args = parser.parse_args()

    commands = {
        "search": lambda: do_search(args.query),
        "unswap": lambda: do_unswap(args.query),
        "paste": lambda: do_paste(args.query),
        "compose": do_compose,
        "watch": do_watch,
        "status": do_status,
        "pause": do_pause,
        "resume": do_resume,
        "now": lambda: do_now(args),
        "timeline": lambda: do_timeline(args),
        "remember": lambda: do_remember(args),
        "why": lambda: do_why(args),
        "summarize": lambda: do_summarize(args),
        "open-context": lambda: do_open_context(args),
        "forget": lambda: do_forget(args),
        "privacy-report": lambda: do_privacy_report(args),
        "privacy-test": lambda: do_privacy_test(args),
        "consent": lambda: do_consent(args),
        "audit": lambda: do_audit(args),
        "trust": lambda: do_trust(args),
        "repair": lambda: do_repair(args),
        "native": lambda: do_native(args),
        "bench": lambda: do_bench(args),
        "mcp": lambda: do_mcp(args),
        "sleep": do_sleep,
        "shred": do_shred,
        "install": do_install,
        "rebuild-index": lambda: do_rebuild_index(args),
        "shell": lambda: do_shell(args),
        "enable": do_enable,
        "disable": do_disable,
        "remove": do_remove,
        "doctor": do_doctor,
        "calibrate": lambda: do_calibrate(args),
        "evolve": do_evolve,
        "time-travel": lambda: do_time_travel(args),
        "compile": lambda: do_compile(args.day),
        "recall": lambda: do_recall(args.query, args.day),
        "gui": do_gui,
        "fix": do_fix,
        "get-ghost-paste": do_get_ghost_paste,
        "get-loop-fix": do_get_loop_fix,
        "ghost-paste": lambda: do_ghost_paste(args.query),
        "ask": lambda: do_ask(args.question),
        "recap": do_recap,
        "context": do_context,
        "config": lambda: do_config(args),
        "pot": lambda: do_pot(args),
    }

    handler = commands.get(args.command)
    if handler:
        handler()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
