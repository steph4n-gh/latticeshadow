import os
import sys
import time
import signal
import hashlib
import logging
import subprocess
from logging.handlers import RotatingFileHandler

# Ensure the parent directory is in sys.path to import latticeshadow_db
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import AppKit
from latticeshadow import keychain
from latticeshadow.tda import TopologicalLoopDetector
from latticeshadow.p2p import LocalMeshNode
from latticeshadow.mobile_api import MobileAPIServer
from latticeshadow.vaults import (
    chmod_collection_files,
    hot_index_enabled,
    open_hot_vault,
    open_main_vault,
)
import socket
import secrets
import queue
import threading

# ── Configuration ─────────────────────────────────────────────────────────────
from latticeshadow import config, consent

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
MAX_CONTENT_BYTES = 1_000_000
MIN_CONTENT_CHARS = 3
POLL_INTERVAL = 0.5  # seconds
AUTO_CONSOLIDATE_THRESHOLD = 500  # trigger REM sleep every N new entries

# Pasteboard types that signal "do not record" (password managers, transient copies)
CONCEALED_TYPES = [
    "org.nspasteboard.ConcealedType",   # Community standard (1Password, Bitwarden, KeePassXC)
    "org.nspasteboard.TransientType",    # Temporary/behind-the-scenes copies
    "com.agilebits.onepassword",         # 1Password proprietary marker
]

os.makedirs(get_log_dir(), mode=0o700, exist_ok=True)
os.chmod(get_log_dir(), 0o700)

# ── Log Rotation (max 1 MB, keep 3 backups) ──────────────────────────────────
logger = logging.getLogger("shadowd")
logger.setLevel(logging.INFO)
log_file_path = os.path.join(get_log_dir(), "shadowd.log")
_handler = RotatingFileHandler(
    log_file_path,
    maxBytes=1_048_576,  # 1 MB
    backupCount=3,
)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_handler)
if os.path.exists(log_file_path):
    os.chmod(log_file_path, 0o600)


def _notify(title: str, message: str):
    """Send a macOS notification banner via osascript. Fire-and-forget."""
    try:
        # Escape double quotes in message to prevent AppleScript injection
        safe_msg = message.replace('"', '\\"')
        safe_title = title.replace('"', '\\"')
        subprocess.Popen(
            ["osascript", "-e", f'display notification "{safe_msg}" with title "{safe_title}"'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass  # Never crash the daemon for a notification failure


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
                    logger.info("Migrated legacy master key to Secure Enclave wrapped key.")
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
                logger.info("Migrated flat-file legacy master key to Secure Enclave wrapped key.")
                return stored

    # 3. Generate new key
    raw_key = hashlib.sha256(os.urandom(64)).hexdigest()
    try:
        wrapped_key = wrap_and_encode(raw_key)
        enclave_wrapped = True
    except Exception:
        wrapped_key = raw_key
        enclave_wrapped = False
        
    try:
        keychain.store_key(wrapped_key)
        logger.info("Stored master key in macOS Keychain.")
    except Exception:
        pass
        
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(wrapped_key)
    logger.info("Generated master key at %s (Secure Enclave wrapped: %s)", key_file, enclave_wrapped)
    return raw_key


# ── Graceful Shutdown ─────────────────────────────────────────────────────────
_running = True

def _shutdown_handler(signum, frame):
    global _running
    sig_name = signal.Signals(signum).name
    logger.info("Received %s, shutting down gracefully...", sig_name)
    _running = False

signal.signal(signal.SIGTERM, _shutdown_handler)
signal.signal(signal.SIGINT, _shutdown_handler)


def handle_loop_and_speculative_fix(vault, logger, p2p_node=None):
    speculative_fix_path = os.path.join(LOG_DIR, ".speculative_fix")
    is_loop = False
    loop_docs = []
    try:
        loop_detector = TopologicalLoopDetector(max_history=100, distance_threshold=0.3)
        is_loop, loop_docs = loop_detector.detect_loop(vault)
    except Exception as te:
        logger.warning("Topological analysis failed: %s", te)

    if is_loop:
        logger.info("Topological cognitive loop detected!")
        _notify("LatticeShadow", "Cognitive loop detected. Run 'shadow fix' to resolve.")
        try:
            from latticeshadow.shadow_cli import generate_speculative_fix
            fix_cmd = generate_speculative_fix(vault, loop_docs=loop_docs)
            if fix_cmd:
                fd = os.open(speculative_fix_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(fix_cmd)
                logger.info("Speculative fix written.")
                try:
                    from latticeshadow.repair_queue import create_repair_proposal

                    create_repair_proposal(
                        summary="Loop detector proposed a recovery command",
                        source="loop_detector",
                        command=fix_cmd,
                        risk="medium",
                        provenance={"loop_docs": len(loop_docs)},
                        data_dir=get_log_dir(),
                    )
                except Exception as qe:
                    logger.warning("Failed to queue loop repair proposal: %s", qe)
                
                if p2p_node and consent.surface_enabled("swarm_knowledge"):
                    logger.info("Swarm knowledge broadcast skipped: signed pairing is not configured.")
        except Exception as ge:
            logger.warning("Failed to generate speculative fix: %s", ge)
    else:
        if os.path.exists(speculative_fix_path):
            try:
                os.remove(speculative_fix_path)
            except Exception:
                try:
                    with open(speculative_fix_path, "w") as f:
                        pass
                except Exception:
                    pass


def get_vault(master_key: str | None = None):
    master_key = master_key or get_or_create_master_key()
    return open_main_vault(
        db_path=get_db_path(),
        master_key=master_key,
        device=config.get_device(),
    )


def get_hot_vault(master_key: str | None = None):
    master_key = master_key or get_or_create_master_key()
    return open_hot_vault(
        db_path=get_db_path(),
        master_key=master_key,
        device=config.get_device(),
    )


def mirror_to_hot_vault(hot_vault, document: str, doc_id: str, metadata: dict) -> None:
    if not hot_vault:
        return
    try:
        hot_vault.add(
            documents=[document],
            ids=[doc_id],
            metadatas=[metadata],
        )
        chmod_collection_files(hot_vault)
    except Exception as e:
        logger.warning("Hot index mirror failed for %s: %s", doc_id, e)


def run_daemon():
    pending = consent.pending_capture_sources()
    if pending:
        logger.error(
            "Capture choices required for %s; run 'shadow consent wizard' before enabling.",
            ", ".join(pending),
        )
        return
    # ── Runtime Self-Integrity Check ──────────────────────────────────────
    # Verify that no critical source modules have been tampered with since
    # the last known-good state. On first run, establishes the baseline.
    if "pytest" not in sys.modules:
        try:
            from latticeshadow.integrity import verify_integrity
            passed, violations = verify_integrity()
            if not passed:
                for v in violations:
                    logger.critical("INTEGRITY VIOLATION: %s", v)
                _notify("LatticeShadow — INTEGRITY ALERT",
                        f"{len(violations)} source file(s) modified since install. Daemon refused to start.")
                logger.critical("Daemon startup aborted due to integrity violations.")
                return
        except Exception as e:
            logger.warning("Integrity check skipped: %s", e)

    pasteboard = AppKit.NSPasteboard.generalPasteboard()
    startup_events: list[str] = []
    startup_lock = threading.Lock()
    startup_stop = threading.Event()

    def watch_startup_clipboard():
        last_startup_count = -1
        while not startup_stop.is_set() and _running:
            try:
                if not consent.capture_enabled("clipboard"):
                    startup_stop.wait(POLL_INTERVAL)
                    continue
                current_count = pasteboard.changeCount() if hasattr(pasteboard, "changeCount") else 0
                if current_count != last_startup_count:
                    last_startup_count = current_count
                    if pasteboard.availableTypeFromArray_(CONCEALED_TYPES) is None:
                        content = pasteboard.stringForType_(AppKit.NSPasteboardTypeString)
                        if content:
                            with startup_lock:
                                startup_events.append(content)
            except Exception:
                pass
            startup_stop.wait(POLL_INTERVAL)

    startup_thread = threading.Thread(target=watch_startup_clipboard, daemon=True)
    startup_thread.start()

    from latticeshadow import security
    security.shield_process()

    master_key = get_or_create_master_key()
    key_bytes = bytearray(master_key.encode('utf-8'))
    security.lock_buffer(key_bytes)
    
    logger.info("Starting LatticeShadow daemon... Database: %s", get_db_path())

    vault = None
    vault_error = None
    for attempt in range(10):
        try:
            vault = get_vault()
            break
        except Exception as e:
            vault_error = e
            if "locked" not in str(e).lower() or attempt == 9:
                break
            time.sleep(0.1)

    if vault is None:
        logger.error("Failed to connect to LatticeDB: %s", vault_error)
        startup_stop.set()
        startup_thread.join(timeout=0.2)
        security.unlock_buffer(key_bytes)
        return

    hot_vault = None
    if hot_index_enabled():
        try:
            hot_vault = get_hot_vault(master_key=master_key)
            logger.info("Streaming exact hot index active.")
        except Exception as e:
            logger.warning("Hot index disabled: %s", e)

    node_id = f"{socket.gethostname()}_{secrets.token_hex(4)}"
    p2p_node = None

    sync_queue = queue.Queue()
    ignored_hashes = set()
    ignored_lock = threading.Lock()

    def on_sync_received(content, sender_id):
        if not consent.surface_enabled("mesh_sync"):
            return
        content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
        with ignored_lock:
            ignored_hashes.add(content_hash)
        sync_queue.put((content, content_hash))

    if consent.surface_enabled("mesh_sync"):
        p2p_node = LocalMeshNode(node_id, vault)
        p2p_node.register_sync_callback(on_sync_received)
        p2p_node.start()
        logger.info("P2P mesh node '%s' started.", node_id)
    else:
        logger.info("P2P mesh node disabled (sync.mesh_sync=false).")

    # Start Mobile API Server
    mobile_server = None
    if consent.surface_enabled("mobile_api"):
        mobile_host = config.get("mobile.host") or "127.0.0.1"
        mobile_port = config.get("mobile.port") or 5052
        mobile_server = MobileAPIServer(vault, host=mobile_host, port=int(mobile_port))
        pairing_code = mobile_server.generate_pairing_code()
        mobile_server.start()
        logger.info("Mobile API server started on %s:%s.", mobile_server.host, mobile_server.port)
        _notify("LatticeShadow", f"Mobile API active. Pairing code: {pairing_code}")
    else:
        logger.info("Mobile API disabled (mobile.enabled=false).")

    # Sticky Proxy (Opt-in)
    proxy = None
    dns_override = None
    if config.get("proxy.enabled"):
        try:
            from latticeshadow_db.mitm import StickyTLSProxy
            from latticeshadow_db.dns_override import DNSOverride
            dns_override = DNSOverride()
            dns_override.apply()
            proxy = StickyTLSProxy()
            proxy.start()
            logger.info("Sticky TLS Proxy started.")
        except Exception as e:
            logger.error("Failed to start Sticky TLS Proxy: %s", e)

    # Apply chmod 600 to database files
    db_path = get_db_path()
    for f in [db_path, db_path + "-wal", db_path + "-shm", db_path + "_clipboard_vectors.bin", db_path + "_clipboard_vectors.bin.lock"]:
        if os.path.exists(f):
            try:
                os.chmod(f, 0o600)
            except Exception:
                pass

    last_content_hash: str | None = None
    inserts_since_consolidation = 0

    def replay_startup_events():
        nonlocal last_content_hash, inserts_since_consolidation
        with startup_lock:
            pending_startup_events = list(startup_events)
            startup_events.clear()

        for startup_content in pending_startup_events:
            if not consent.capture_enabled("clipboard"):
                break
            try:
                content = startup_content.strip()
                if MIN_CONTENT_CHARS < len(content) < MAX_CONTENT_BYTES:
                    content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
                    if content_hash != last_content_hash:
                        last_content_hash = content_hash
                        doc_id = f"clip_{int(time.time() * 1000)}"
                        vault.add(
                            documents=[content],
                            ids=[doc_id],
                            metadatas=[{"source": "clipboard"}],
                        )
                        chmod_collection_files(vault)
                        mirror_to_hot_vault(
                            hot_vault,
                            content,
                            doc_id,
                            {"source": "clipboard"},
                        )
                        if pot_chain:
                            pot_chain.append_event("clipboard", content)
                        try:
                            from latticeshadow.audit_log import append_audit_event

                            append_audit_event(
                                "capture",
                                {
                                    "source": "clipboard",
                                    "doc_id": doc_id,
                                    "content_hash": content_hash,
                                },
                                data_dir=get_log_dir(),
                            )
                        except Exception:
                            pass
                        inserts_since_consolidation += 1
                        logger.info(
                            "Captured: %s (len: %d, total_new: %d)",
                            doc_id, len(content), inserts_since_consolidation,
                        )
            except Exception as se:
                logger.warning("Failed to replay startup clipboard event: %s", se)

    # Initialize Proof of Thought Chain
    try:
        from latticeshadow.pot_chain import PoTChain
        pot_chain = PoTChain()
        logger.info("Proof-of-Thought cryptographic ledger initialized.")
    except Exception as e:
        logger.error("Failed to initialize PoT Chain: %s", e)
        pot_chain = None

    replay_startup_events()

    # Start Ambient Context Monitor if enabled in configuration
    ambient_monitor = None
    if consent.surface_enabled("ambient_context"):
        try:
            from latticeshadow.ambient_monitor import AmbientContextMonitor
            ambient_monitor = AmbientContextMonitor(interval=5.0, pot_chain=pot_chain, vault=vault)
            ambient_monitor.start()
            logger.info("Ambient context monitoring active.")
        except Exception as ae:
            logger.error("Failed to initialize Ambient Context Monitor: %s", ae)
            
    # Experimental background services require an explicit choice.
    immune_system = None
    if consent.surface_enabled("immune_scan"):
        try:
            from latticeshadow.immune_system import ImmuneSystem
            immune_system = ImmuneSystem(pot_chain=pot_chain)
            immune_system.start()
            logger.info("Experimental dependency scan active.")
        except Exception as ie:
            logger.error("Failed to start dependency scan: %s", ie)

    # Start Auto-Doctor
    auto_doctor = None
    if "pytest" not in sys.modules and consent.surface_enabled("auto_doctor"):
        try:
            from latticeshadow.auto_doctor import AutoDoctorThread
            auto_doctor = AutoDoctorThread()
            auto_doctor.start()
            logger.info("Auto-Doctor (Self-Healing Chaos Engineering) active.")
        except Exception as ade:
            logger.error("Failed to start Auto-Doctor: %s", ade)

    # Start Semantic Swapper Daemon
    swapper_daemon = None
    if consent.surface_enabled("semantic_swapper"):
        try:
            from latticeshadow.virtual_swapper import SemanticSwapperDaemon
            swapper_daemon = SemanticSwapperDaemon(vault=vault, pot_chain=pot_chain)
            swapper_daemon.start()
            logger.info("Experimental app snapshotting active.")
        except Exception as sde:
            logger.error("Failed to start app snapshotting: %s", sde)

    # Setup history watcher if configured
    history_watcher = None
    from latticeshadow import config as cfg
    # A persisted pause must not prevent the watcher from being ready to resume.
    terminal_choice = consent.consent_status()["surfaces"]["terminal_history"]
    if terminal_choice["enabled"] and not terminal_choice["needs_consent"]:
        try:
            try:
                from latticeshadow.history_watcher import TerminalHistoryWatcher as HistoryWatcherClass
            except ImportError:
                from latticeshadow.history_watcher import HistoryWatcher as HistoryWatcherClass
            history_watcher = HistoryWatcherClass()
            logger.info("Terminal history capture active.")
        except Exception as he:
            logger.error("Failed to initialize Terminal history capture: %s", he)

    replay_startup_events()
    startup_stop.set()
    startup_thread.join(timeout=0.2)
    replay_startup_events()

    try:
        last_change_count = pasteboard.changeCount() if hasattr(pasteboard, "changeCount") else 0
    except Exception:
        last_change_count = 0

    if consent.capture_enabled("clipboard"):
        logger.info("Listening for clipboard events...")
        _notify("LatticeShadow", "Clipboard monitoring active.")

    sync_engine = None
    last_sync_time = 0
    SYNC_INTERVAL = 30

    while _running:
        try:
            if p2p_node and not consent.surface_enabled("mesh_sync"):
                p2p_node.stop()
                p2p_node = None
            if mobile_server and not consent.surface_enabled("mobile_api"):
                mobile_server.stop()
                mobile_server = None
            # Check if master key has been destroyed (crypto-shred deletes both Keychain and file)
            key_available = os.path.exists(get_key_file())
            if not key_available:
                try:
                    key_available = keychain.retrieve_key() is not None
                except Exception:
                    pass
            if not key_available:
                raise PermissionError("Master key destroyed (crypto-shred detected).")

            current_change_count = last_change_count
            if hasattr(pasteboard, "changeCount"):
                try:
                    current_change_count = pasteboard.changeCount()
                except Exception:
                    pass
            clipboard_enabled = consent.capture_enabled("clipboard")
            if not clipboard_enabled:
                last_change_count = current_change_count
            if clipboard_enabled and current_change_count != last_change_count:
                last_change_count = current_change_count

                # Skip concealed/sensitive content (password managers)
                if pasteboard.availableTypeFromArray_(CONCEALED_TYPES) is not None:
                    logger.debug("Skipped concealed/transient clipboard content (password manager?)")
                    time.sleep(POLL_INTERVAL)
                    continue

                try:
                    content = pasteboard.stringForType_(AppKit.NSPasteboardTypeString)
                    if content:
                        content = content.strip()
                        if MIN_CONTENT_CHARS < len(content) < MAX_CONTENT_BYTES:
                            # Deduplicate: skip if identical to the last captured content
                            content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
                            is_ignored = False
                            with ignored_lock:
                                if content_hash in ignored_hashes:
                                    ignored_hashes.remove(content_hash)
                                    is_ignored = True

                            if is_ignored:
                                last_change_count = current_change_count
                                last_content_hash = content_hash
                                logger.info("Feedback loop prevented for hash: %s", content_hash)
                            else:
                                if content_hash != last_content_hash:
                                    last_content_hash = content_hash
                                    doc_id = f"clip_{int(time.time() * 1000)}"
                                    vault.add(
                                        documents=[content],
                                        ids=[doc_id],
                                        metadatas=[{"source": "clipboard"}]
                                    )
                                    chmod_collection_files(vault)
                                    mirror_to_hot_vault(
                                        hot_vault,
                                        content,
                                        doc_id,
                                        {"source": "clipboard"},
                                    )
                                    if pot_chain:
                                        pot_chain.append_event("clipboard", content)
                                    try:
                                        from latticeshadow.audit_log import append_audit_event

                                        append_audit_event(
                                            "capture",
                                            {
                                                "source": "clipboard",
                                                "doc_id": doc_id,
                                                "content_hash": content_hash,
                                            },
                                            data_dir=get_log_dir(),
                                        )
                                    except Exception:
                                        pass
                                    inserts_since_consolidation += 1
                                    logger.info(
                                        "Captured: %s (len: %d, total_new: %d)",
                                        doc_id, len(content), inserts_since_consolidation,
                                    )
                                    if consent.surface_enabled("mesh_sync") and p2p_node:
                                        p2p_node.broadcast_sync(content, doc_id)

                                    # Run Topological Loop Detection and speculative fix generation
                                    handle_loop_and_speculative_fix(vault, logger, p2p_node)

                                    # Auto-consolidation
                                    if inserts_since_consolidation >= AUTO_CONSOLIDATE_THRESHOLD:
                                        logger.info(
                                            "Auto-consolidation triggered at %d new entries...",
                                            inserts_since_consolidation,
                                        )
                                        
                                        # Time-Travel Environment Snapshot
                                        if config.get("time_travel.enabled"):
                                            try:
                                                from latticeshadow.time_travel import EnvironmentSnapshotter
                                                snapshotter = EnvironmentSnapshotter(time_travel_enabled=True)
                                                snapshotter.capture()
                                            except Exception as e:
                                                logger.warning("Failed to capture environment snapshot: %s", e)
                                                
                                        try:
                                            count_before = vault.count()
                                            vault.consolidate()
                                            count_after = vault.count()
                                            logger.info(
                                                "Auto-consolidation complete: %d → %d entries.",
                                                count_before, count_after,
                                            )
                                            _notify("LatticeShadow", f"Consolidated {count_before} → {count_after} entries.")
                                        except Exception as ce:
                                            logger.warning("Auto-consolidation failed: %s", ce)
                                        # Run LLM dream enrichment if configured
                                        try:
                                            from latticeshadow.dreamer import dream_cycle
                                            dream_cycle(vault, logger)
                                        except Exception as de:
                                            logger.debug("Dream enrichment skipped: %s", de)
                                        inserts_since_consolidation = 0
                except (UnicodeEncodeError, UnicodeDecodeError, UnicodeError, ValueError) as ue:
                    logger.warning("Ignored encoding error during pasteboard string retrieval: %s", ue)

            # Poll terminal history
            if history_watcher and consent.capture_enabled("terminal_history"):
                try:
                    new_cmds = history_watcher.poll()
                    if new_cmds:
                        for i, cmd in enumerate(new_cmds):
                            cmd_text = cmd["text"]
                            cmd_ts = cmd["timestamp"]
                            doc_id = f"cmd_{int(time.time() * 1000)}_{i}"
                            vault.add(
                                documents=[cmd_text],
                                ids=[doc_id],
                                metadatas=[{
                                    "source": "terminal",
                                    "timestamp": cmd_ts
                                }]
                            )
                            chmod_collection_files(vault)
                            mirror_to_hot_vault(
                                hot_vault,
                                cmd_text,
                                doc_id,
                                {
                                    "source": "terminal",
                                    "timestamp": cmd_ts
                                },
                            )
                            if pot_chain:
                                pot_chain.append_event("terminal", cmd_text)
                            try:
                                from latticeshadow.audit_log import append_audit_event

                                append_audit_event(
                                    "capture",
                                    {
                                        "source": "terminal",
                                        "doc_id": doc_id,
                                        "content_hash": hashlib.sha256(
                                            cmd_text.encode("utf-8", errors="replace")
                                        ).hexdigest(),
                                    },
                                    data_dir=get_log_dir(),
                                )
                            except Exception:
                                pass
                            inserts_since_consolidation += 1
                            logger.info("Captured command: %s (total_new: %d)", doc_id, inserts_since_consolidation)
                        
                        # Run Topological Loop Detection and speculative fix generation
                        handle_loop_and_speculative_fix(vault, logger, p2p_node)
                except Exception as he:
                    logger.warning("Error polling terminal history: %s", he)

            try:
                sync_event = sync_queue.get(timeout=POLL_INTERVAL)
                content, content_hash = sync_event
                pasteboard.clearContents()
                pasteboard.setString_forType_(content, AppKit.NSPasteboardTypeString)
                last_change_count = pasteboard.changeCount()
                last_content_hash = content_hash
                logger.info("Wrote sync event to General Pasteboard. Hash: %s", content_hash)
            except queue.Empty:
                pass

            # Periodically execute iCloud Sync if enabled
            if consent.surface_enabled("icloud_sync"):
                current_time = time.time()
                if current_time - last_sync_time >= SYNC_INTERVAL:
                    last_sync_time = current_time
                    try:
                        from latticeshadow.sync import ICloudSyncEngine
                        if sync_engine is None:
                            sync_engine = ICloudSyncEngine(vault, device_id=node_id)
                        
                        def run_sync_cycle():
                            try:
                                sync_engine.export_packets()
                                sync_engine.import_packets()
                            except Exception as se:
                                logger.warning("iCloud sync execution error: %s", se)
                        
                        threading.Thread(target=run_sync_cycle, daemon=True).start()
                    except Exception as se_init:
                        logger.warning("Failed to initialize iCloud sync: %s", se_init)

            time.sleep(0.001)

        except PermissionError:
            # Database was crypto-shredded while daemon was running
            logger.error("Database was crypto-shredded. Daemon exiting.")
            _notify("LatticeShadow", "Database crypto-shredded. Daemon stopping.")
            break
        except Exception as e:
            logger.error("Error in daemon loop: %s", e)
            time.sleep(1)

    if ambient_monitor:
        try:
            ambient_monitor.stop()
            ambient_monitor.join(timeout=2.0)
        except Exception:
            pass
    if swapper_daemon:
        try:
            swapper_daemon.stop()
            swapper_daemon.join(timeout=2.0)
        except Exception:
            pass
    if p2p_node:
        p2p_node.stop()
    if mobile_server:
        mobile_server.stop()
    
    if proxy:
        proxy.stop()
    if dns_override:
        dns_override.remove()
        
    logger.info("Daemon shut down cleanly.")


if __name__ == "__main__":
    run_daemon()
