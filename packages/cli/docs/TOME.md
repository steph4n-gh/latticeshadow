# Technical Tome: LatticeShadow CLI Architecture & Internals

This is the implementation reference for the macOS client. It includes
experimental modules as well as the everyday CLI path. A module appearing here
does not mean it is enabled, production ready, or part of the recommended
workflow. For setup and feature status, start with the [CLI README](../README.md)
and [root guide](../../../README.md). The [future directions](FUTURE.md) describe
work that has not been completed.

---

## System Architecture

The LatticeShadow client operates as a decoupled, local-first system designed for macOS. It acts as the bridge between native OS pasteboard events and the underlying vector database managed by `latticeshadow-db`.

```
                  ┌────────────────────────────────────────┐
                  │          macOS User Session            │
                  │  ┌──────────────┐    ┌──────────────┐  │
                  │  │ AppKit Menu  │    │  Spotlight   │  │
                  │  │ (Status Bar) │    │ Search Panel │  │
                  │  └──────┬───────┘    └──────┬───────┘  │
                  └─────────┼───────────────────┼──────────┘
                            │ PyObjC            │ PyObjC
                            ▼                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       LatticeShadow Daemon (shadowd)                 │
│                                                                      │
│  ┌───────────────────────┐    ┌─────────────────┐    ┌────────────┐  │
│  │ NSPasteboard Monitor  │───►│   Local SQLite  │◄───│ P2P Gossip │  │
│  │ & History Watcher     │    │   Vector Vault  │    │ Mesh Node  │  │
│  └───────────────────────┘    └────────┬────────┘    └─────┬──────┘  │
└────────────────────────────────────────┼───────────────────┼─────────┘
                                         ▼                   ▼
                                ┌─────────────────┐    ┌────────────┐
                                │ Keychain / file │    │ Multicast  │
                                │ Enclave optional│    │ UDP / TCP  │
                                └─────────────────┘    └────────────┘
```

The diagram shows available components, not default startup behavior. Once
enabled, the daemon checks configured clipboard and terminal inputs and writes
events to a local database. The menu bar and search panel are optional clients.
The P2P node and network listeners are opt-in experimental paths. The CLI's
canonical clipboard collection stores FlyHash bitmasks; its optional dense hot
index stores distance-preserving rotated vectors. These are different storage
paths with different leakage properties.

---

## Cocoa & AppKit Hooks

LatticeShadow integrates deeply with AppKit via PyObjC to achieve its desktop experience.

### Status Bar Integration

The status bar entry is initialized in [latticeshadow/menu.py](../latticeshadow/menu.py) using the class `ShadowMenuApp`. It calls:
```python
self.statusItem = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
    AppKit.NSVariableStatusItemLength
)
self.statusItem.button().setTitle_("⏣")
```
This registers a status item while the menu app runs. The dropdown menu contains options to trigger REM sleep, open diagnostics, or search history.

### Spotlight-Style Panel

The Spotlight-style panel uses a customized subclass of `NSPanel` named `SpotlightWindow` to override standard window behaviors:
- **Floating Panel**: It uses `NSWindowStyleMaskNonactivatingPanel` and `NSWindowStyleMaskBorderless` masks. This enables the panel to hover above fullscreen applications without stealing active window focus from the user.
- **Keyboard Handling**: It captures custom key presses. The text field uses a custom `NSTextField` subclass to dismiss the panel when the `Escape` key is pressed. The control delegate intercepts Arrow keys (`moveDown:`, `moveUp:`) to navigate the results table and `insertNewline:` to copy the highlighted result.
- **Dynamic Resizing**: The panel height adapts to the number of search results retrieved from the vector vault. Height calculations trigger animated window updates:
  ```python
new_height = 60 + len(results) * 35
new_frame = AppKit.NSMakeRect(frame.origin.x, new_y, frame.size.width, new_height)
self.setFrame_display_animate_(new_frame, True, True)
```

### GridMetalView GPU Rendering

To create a visually distinct backdrop for the search results, the Spotlight window incorporates a custom subclass of `MTKView` (MetalKit View) called `GridMetalView`.
- **Dynamic Framework Loading**: The view attempts to load the Metal frameworks dynamically:
  ```python
objc.loadBundle('Metal', globals(), bundle_path='/System/Library/Frameworks/Metal.framework')
```
  If Metal is not supported (such as in headless or legacy environments), the view falls back to a standard transparent `NSView`.
- **Shader Compilation**: Shaders are loaded from `Shaders.metal` and compiled at runtime:
  ```python
self.library = metal_device.newLibraryWithSource_options_error_(source_code, None, None)
```
- **Real-Time Uniforms**: The render pass passes a time variable as fragment input bytes to execute dynamic grid animations on the GPU.

---

## P2P Gossip Mesh & Cryptographic Protocols

Peer synchronization is managed by `LocalMeshNode` in [latticeshadow/p2p.py](../latticeshadow/p2p.py). This is an opt-in, experimental network protocol. Do not expose it to untrusted networks or rely on its research cryptography for confidentiality or integrity.

### Discovery via UDP Multicast

Discovery uses UDP multicast over standard class D addresses:
- **Multicast Group**: `224.0.0.1` (all hosts on local subnet).
- **Default Port**: `5050`.
- **Protocol**: Nodes broadcast a `HELO` payload containing their unique node ID and a dynamically bound TCP port. Discovered peers are tracked in `self.peers` with a last-seen timestamp.

### Homomorphic Queries

The prototype sends encrypted bitmask queries rather than literal query text.
This is not a demonstrated end-to-end private search protocol: discovery,
traffic metadata, peer authentication, response handling, and the native
cryptography need independent review before making a confidentiality claim.
1. The search query is converted into a binary index mask (Drosophila binary hash).
2. The searcher encrypts this bitmask using Learning With Errors (LWE) cryptography via the compiled library `liblwe.dylib` (managed in [latticeshadow/homomorphic.py](../latticeshadow/homomorphic.py)), producing matrices `enc_a` and vectors `enc_b`.
3. The query containing `enc_a` and `enc_b` is sent to mesh peers over TCP.
4. Peers run homomorphic distance calculations on their encrypted local databases:
   ```python
a_sum, b_sum = homomorphic.evaluate_distance_homomorphically(enc_a, enc_b, local_bitmask)
```
5. The resulting encrypted distance (`a_sum`, `b_sum`) is returned to the searcher, who decrypts it using the secret key to find the Hamming distance.

### Prototype Proof of Distance

Nodes generate a deterministic Groth16-shaped proof string for the calculated
distance threshold (e.g. 35). It is a simulation: anyone can recompute it, so
it does not stop a malicious peer from fabricating a response. It is not a
zk-SNARK or an integrity guarantee:
- The responder calls `generate_distance_snark_proof(a_sum, b_sum, threshold)`.
- The searcher verifies the proof using `verify_distance_snark_proof(proof, a_sum, b_sum, threshold)` before dedicating compute cycles to decrypt the distance.

### Swarm Intelligence

If a local system loop is detected by the `TopologicalLoopDetector`, the daemon writes a speculative fix command to `~/.latticeshadow/.speculative_fix` and records a pending repair proposal. Network-discovered swarm knowledge is record-only unless explicit trust and approval are added; peers must not automatically execute remote repair commands.

---

## Daemon Mechanics

The clipboard daemon implementation lives in [latticeshadow/shadowd.py](../latticeshadow/shadowd.py).

### Pasteboard Monitoring & Concealment

The daemon monitors the general pasteboard `AppKit.NSPasteboard.generalPasteboard()` every `POLL_INTERVAL` (0.5 seconds).
- **Deduplication**: SHA-256 hashes are used to prevent capture feedback loops (where writing a sync event to the pasteboard triggers a capture event).
- **Concealment**: The daemon ignores clipboard entries with these known markers. This is a partial filter, not a guarantee that secrets or personal data will be excluded:
  - `org.nspasteboard.ConcealedType` (1Password, Bitwarden, KeePassXC)
  - `org.nspasteboard.TransientType` (temporary clipboard contents)
  - `com.agilebits.onepassword` (legacy 1Password)

### REM Sleep & LLM Dreams

Periodically (or after `AUTO_CONSOLIDATE_THRESHOLD` entries), the daemon triggers "REM Sleep":
- **Consolidation**: Runs vector index consolidation via `vault.consolidate()`.
- **Dream Cycle**: Spins up a background thread using [latticeshadow/dreamer.py](../latticeshadow/dreamer.py) to summarize, cluster, and enrich captured entries using local or cloud LLMs.

---

## Advanced Subsystems & Modules

The following modules are implemented to varying degrees. Several are
research or opt-in paths and some have known runtime gaps; read their source and
tests before using them with personal data.

### 1. Auto-Doctor (`latticeshadow/auto_doctor.py`)

- **Path**: `[latticeshadow/auto_doctor.py](../latticeshadow/auto_doctor.py)`
- **Purpose**: System optimization daemon that detects idle times, creates temporary git worktree sandboxes, uses an LLM to propose code hardening/optimization mutations, validates them by running the project's test suite, generates unified diff patches, and notifies the user.
- **Key Classes & Methods**:
  - `get_system_idle_time() -> float`: Executes macOS `ioreg` command via a shell subprocess to read `HIDIdleTime` in nanoseconds, converting it to seconds by dividing by $1,000,000,000$. Returns `0.0` on failure.
  - `GitSandbox`: Encapsulates an isolated git worktree sandbox.
    - `__init__(self, project_dir: str)`: Creates unique workspace paths under `~/.latticeshadow/mutations/sandbox_[timestamp]` and a unique branch name `auto_doctor_mutation_[timestamp]`.
    - `create(self) -> bool`: Creates a new git worktree tracking the new branch: `git worktree add -b <branch> <sandbox_path>`.
    - `destroy(self) -> None`: Removes the worktree via `git worktree remove --force`, deletes the branch via `git branch -D`, and deletes the workspace directory.
  - `select_mutation_target(sandbox_path: str) -> str`: Finds `.py` files inside the sandbox's `latticeshadow` folder, excluding configuration, entry points, and `__init__.py`, and selects one randomly as the target.
  - `generate_mutation(file_content: str) -> str`: Checks the daily LLM budget via `budget.check_budget_and_increment()`. Calls the LLM (`ShadowLLM.from_config()`) to suggest code hardening changes, receiving complete Python file contents and stripping out any markdown code blocks.
  - `run_tests(sandbox_path: str, command: str) -> bool`: Runs verification tests inside the sandbox (defaulting to the configured test command, e.g., `pytest`) using `subprocess.run`.
  - `run_mutation_cycle() -> None`: Coordinates the entire cycle (create sandbox $\rightarrow$ select target $\rightarrow$ read file $\rightarrow$ query LLM $\rightarrow$ write changes $\rightarrow$ run tests $\rightarrow$ generate unified diff via `git diff --unified` $\rightarrow$ save diff under `~/.latticeshadow/pending_diffs/mutation_[timestamp].diff` $\rightarrow$ trigger desktop notification via `_notify` $\rightarrow$ destroy sandbox).
  - `AutoDoctorThread(threading.Thread)`: Periodic background daemon loop checking system idle time against the configuration threshold (default 300s). Sleeps for 15 minutes after a cycle to avoid spinning.

### 2. Autonomous OS Immune System (AOIS) (`latticeshadow/immune_system.py`)

- **Path**: `[latticeshadow/immune_system.py](../latticeshadow/immune_system.py)`
- **Purpose**: Prototype dependency scanner that queries the Open Source Vulnerability (OSV) database. It reports matches but does not install fixes.
- **Key Classes & Methods**:
  - `ImmuneSystem(threading.Thread)`: Background daemon thread.
    - `__init__(self, pot_chain=None)`: Stores reference to the Proof-of-Thought (PoT) chain ledger, configuring a `_stop_event` and default scanning interval of 86,400 seconds (24 hours).
    - `stop(self) -> None`: Signals the stop event to terminate the scanning loop.
    - `run(self) -> None`: Starts with a 30-second buffer delay to prevent startup blocking, then runs `run_vaccination_scan()` periodically.
    - `run_vaccination_scan(self) -> None`: Queries local dependencies using `pip list --format=json` via a subprocess. For each package, executes an HTTP POST to the OSV query endpoint `https://api.osv.dev/v1/query` with package payload details. If the returned payload contains a `vulns` list, it records details and triggers `_apply_hotpatch`.
    - `_apply_hotpatch(self, name: str, version: str, vulns: list) -> None`: Despite its name, currently writes a simulated patch event to the Proof-of-Thought ledger when configured; it does not change installed packages. Do not interpret its output as remediation.

### 3. iCloud Synchronization Engine (`latticeshadow/sync.py`)

- **Path**: `[latticeshadow/sync.py](../latticeshadow/sync.py)`
- **Purpose**: Syncs clipboard databases across devices via iCloud Drive using transaction packets encrypted locally with AES-GCM.
- **Key Classes & Methods**:
  - `ICloudSyncEngine`: Manages exporting and importing encrypted transaction packets.
    - `__init__(self, vault, device_id=None)`: Generates a unique device ID incorporating the hostname and a random hex suffix, defines paths for `.last_sync_id` and `.processed_packets` inside `~/.latticeshadow`, and gets the vault's master encryption key: `self.vault._privacy._master_key_bytes`.
    - `get_sync_dir(self) -> str`: Standardizes the iCloud target folder: `~/Library/Mobile Documents/com~apple~CloudDocs/LatticeShadow/sync_packets`, falling back to local `~/.latticeshadow/sync_packets` if iCloud is missing.
    - `get_last_sync_id(self) -> str` / `save_last_sync_id(self, last_id) -> None`: Reads and saves the last synchronized document ID.
    - `get_processed_packets(self) -> set` / `mark_packet_processed(self, filename) -> None`: Reads and logs processed file tracking.
    - `export_packets(self) -> None`: Fetches un-exported documents from the SQLite backend using `id > last_sync_id`. If local storage encryption is active, decrypts entries to plaintext first. Combines documents into a JSON payload and encrypts it using AES-GCM.
      - *Key Derivation*: $\text{sync\_key} = \text{SHA-256}(\text{master\_key\_bytes} \mathbin{\Vert} \text{b"latticeshadow\_sync"})$
      - *IV/Nonce*: 12-byte random IV (`os.urandom(12)`).
      - *Format*: Concatenates `nonce + ciphertext` and base64-encodes the block.
      - Saves to a `.tmp` file before renaming to `[device_id]_[timestamp_ms].enc`.
    - `import_packets(self) -> None`: Scans the sync directory, skipping non-`.enc` files, files generated by the local device, and files listed in the processed list. Decrypts using the same derived `sync_key` via AES-GCM, extracts the documents, runs duplicate ID checks against SQLite, adds new records to the local vault (which automatically encrypts them locally using the vault's native key), and logs the file as processed.

### 4. Sensitivity Classification (`latticeshadow/sensitivity.py`)

- **Path**: `[latticeshadow/sensitivity.py](../latticeshadow/sensitivity.py)`
- **Purpose**: Heuristic classification and redaction for some credential-shaped strings. It can miss secrets and should not be treated as a complete boundary for remote model calls.
- **Key Components & Methods**:
  - `_SENSITIVE_PATTERNS`: A list of 13 precompiled regex objects detecting API keys, AWS credentials, PEM private keys, database connection URIs containing credentials, JWT tokens, SSH keys, passwords, GitHub/GitLab tokens, Slack tokens, Stripe keys, OpenAI keys, Google API keys, and large hexadecimal secrets.
  - `_REDACT_PATTERN`: A single union regex matching specific credentials and PEM blocks from start to end.
  - `classify(text: str) -> str`: Assigns a classification tier:
    - `"safe"`: Text is shorter than 3 chars or contains no sensitive matches.
    - `"sensitive"`: Text matches any pattern in `_SENSITIVE_PATTERNS`.
    - `"unknown"`: Heuristic match for long random sequences ($>32$ chars, no spaces, standard Base64 character set) that might be unrecognized secrets.
  - `redact(text: str) -> str`: Replaces matches from `_REDACT_PATTERN` with `"[REDACTED]"`. Additionally runs a regex substitute to catch password values in assignment formats: `(password|passwd|pwd|secret|token) = value` becomes `\1[REDACTED]`.

### 5. Holographic Memory Indexing (`latticeshadow/holographic_index.py`)

- **Path**: `[latticeshadow/holographic_index.py](../latticeshadow/holographic_index.py)`
- **Purpose**: Experimental Holographic Reduced Representation (HRR) cache. It has no zero-latency guarantee; compare its recall and latency with ordinary search before enabling it.
- **Key Classes & Methods**:
  - `HolographicIndex`: Manages high-dimensional semantic indexing.
    - `__init__(self, dim: int = 1024)`: Allocates `dim`-dimensional zeroed memory tensor, codebook dictionary, codebook entries tracker, and value projection matrix.
    - `compile(self, documents: List[str], embeddings: List[torch.Tensor]) -> None`:
      1. Generates $\min(n, \text{dim})$ random base vectors, orthogonalizing them via Gram-Schmidt to produce zero-crosstalk base vectors $\mathbf{u}_i$:
         $$\mathbf{v}_i \leftarrow \mathbf{v}_i - \sum_{j < i} \langle \mathbf{v}_i, \mathbf{u}_j \rangle \mathbf{u}_j, \quad \mathbf{u}_i = \frac{\mathbf{v}_i}{\|\mathbf{v}_i\|}$$
      2. For each document:
         - Generates a unitary batch key $B_k$ for its chunk batch (seed derived from SHA-256 of `batch_<idx>`).
         - Computes combined pointer: $C_i = B_k \circledast V_{in\_batch}$ where $\circledast$ denotes circular convolution.
         - Converts the semantic embedding key into a unitary representation to preserve vector distances and eliminate scale distortion: $K_i = \text{make\_unitary}(embeddings[i])$.
         - Binds the unitary key with pointer $C_i$ via circular convolution: $bound = K_i \circledast C_i$.
         - Aggregates bounds into the global memory vector: $M \leftarrow M + bound$.
         - Maps the hash of $C_i$ to the document text in the codebook and stores the mapping.
    - `recall(self, query_embedding: torch.Tensor) -> Tuple[str, float] | Tuple[None, float]`:
      1. Converts the query embedding to unitary representation: $Q = \text{make\_unitary}(query\_embedding)$.
      2. Correlates $Q$ with the global memory vector $M$: $V_{\text{noisy}} = M \star Q$ where $\star$ denotes circular correlation.
      3. Normalizes $V_{\text{noisy}}$ to unit norm.
      4. Computes cosine similarity of $V_{\text{noisy}}$ against all clean position vectors stacked in the `_value_matrix`.
      5. Returns the document corresponding to the highest similarity score if it exceeds $0.05$.
    - `_make_unitary(self, vec: torch.Tensor) -> torch.Tensor`: Projects Fourier components of the vector onto the unit circle:
      $$\mathbf{F} = \text{FFT}(\mathbf{v}), \quad \mathbf{F}_{\text{unitary}} = \frac{\mathbf{F}}{|\mathbf{F}|}, \quad \mathbf{v}_{\text{unitary}} = \text{Real}(\text{IFFT}(\mathbf{F}_{\text{unitary}}))$$
      Replaces magnitudes $< 1e-9$ with $1.0$ to avoid division by zero.
    - `_gram_schmidt(self, vectors: List[torch.Tensor]) -> List[torch.Tensor]`: Performs strict Gram-Schmidt orthogonalization. Projects and subtracts component overlays from existing unit vectors. If the norm collapses ($< 1e-6$), falls back to a normalized random vector.
    - `save(self, filepath: str)` and `load(cls, filepath: str)`: PyTorch model state serializers.

### 6. Vision-based UI Automation (`latticeshadow/vision_agent.py`)

- **Path**: `[latticeshadow/vision_agent.py](../latticeshadow/vision_agent.py)`
- **Purpose**: Programmatic desktop interactions on macOS using Apple's Vision OCR to find specific text on screen and simulated mouse input via Quartz coordinate tracking.
- **Key Classes & Methods**:
  - `VisionAgent`: Coordinator for screen captures, text parsing, and click simulation.
    - `__init__(self)`: Initializes the screenshot path property to `/tmp/shadow_vision.png`.
    - `take_screenshot(self) -> str`: Invokes the macOS CLI utility `screencapture -x` to capture the entire display silently and returns the screenshot file path.
    - `find_text_coordinates(self, target_text: str) -> Optional[Tuple[float, float]]`:
      1. Uses Apple's native Vision framework to locate target text on screen from the screenshot image URL.
      2. Configures `VNRecognizeTextRequest` to `VNRequestTextRecognitionLevelAccurate` and performs a synchronous scan.
      3. Obtains screen dimensions using Quartz: `CGDisplayBounds(CGMainDisplayID())`.
      4. Performs a case-insensitive substring search over recognized text strings. If found, extracts the normalized bounding box $box$ (values in range $[0.0, 1.0]$ with bottom-left origin).
      5. Converts coordinates to Quartz screen space (pixel coords, origin top-left):
         $$x_{\text{min}} = \text{box.origin.x} \times \text{width}$$
         $$y_{\text{min}} = \text{height} - (\text{box.origin.y} \times \text{height})$$
         $$w = \text{box.size.width} \times \text{width}$$
         $$h = \text{box.size.height} \times \text{height}$$
         $$\text{center\_x} = x_{\text{min}} + \frac{w}{2}$$
         $$\text{center\_y} = y_{\text{min}} - \frac{h}{2}$$
         Returns `(center_x, center_y)`.
    - `click(self, x: float, y: float) -> None`: Simulates a mouse-down and mouse-up event at `(x, y)` using Quartz CGEvent API. Respects the configuration safety setting `automation.live_dangerously`: if `False`, performs a dry-run log; if `True`, posts event tap signals via `CGEventPost`.
    - `execute_replay(self, target_text: str) -> bool`: Combines the pipeline (capture $\rightarrow$ search text $\rightarrow$ click target).

### 7. Neural Autocomplete (`latticeshadow/compose.py`)

- **Path**: `[latticeshadow/compose.py](../latticeshadow/compose.py)`
- **Purpose**: Semantic shell command predictor that fetches clipboard history and terminal commands, formats a context window, and queries an LLM to predict the next logical command.
- **Key Classes & Methods**:
  - `NeuralComposer`: Primary coordinator for prompt building and autocomplete prediction.
    - `__init__(self, db_path: str = None)`: Resolves SQLite store path and instantiates a `ShadowLLM` connection.
    - `_get_recent_history(self, limit: int = 10) -> List[Dict[str, str]]`: Queries the SQLite database `vectors` table for clipboard and terminal events in reversed (chronological) order:
      ```sql
      SELECT metadata_json, document 
      FROM vectors 
      WHERE collection = 'clipboard' 
      ORDER BY rowid DESC 
      LIMIT ?
      ```
    - `predict_next_command(self) -> str`: Compiles history list, truncates clipboard entries exceeding 500 characters, appends terminal prompt instructions, and calls `self.llm.generate(prompt)`.
      *Architectural Note*: Calling `self.llm.generate(prompt)` directly is a known runtime bug because `ShadowLLM` only exposes the `complete(self, system, user, temperature)` method. This requires a patch or wrapper translation layer to avoid crashing at runtime.
      Cleans up response strings by stripping markdown block wrappers (e.g. ` ``` `).

### 8. Daily LLM Budget Tracker (`latticeshadow/budget.py`)

- **Path**: `[latticeshadow/budget.py](../latticeshadow/budget.py)`
- **Purpose**: Restricts daily LLM API request volumes to prevent runaway costs, storing state in a secure JSON file.
- **Key Functions**:
  - `_load_budget() -> dict`: Reads and deserializes `~/.latticeshadow/budget.json`. Returns `{"date": "", "request_count": 0}` if file is missing or corrupted.
  - `_save_budget(data: dict) -> None`: Serializes state, creating parent directories and enforcing strict permissions (`0o600`).
  - `check_budget_and_increment() -> bool`: Resolves the daily limit via config key `automation.max_daily_llm_requests` (defaults to 50). Resolves today's ISO date string. If the file date matches today and request count exceeds the limit, returns `False`. Otherwise, increments the count, saves, and returns `True`. If a new day has started, resets the count to 1 and returns `True`.
  - `get_today_requests() -> int`: Returns today's active request count.

### 9. Native Tooltips Interface (`latticeshadow/tooltip.py`)

- **Path**: `[latticeshadow/tooltip.py](../latticeshadow/tooltip.py)`
- **Purpose**: Generates non-blocking, floating notifications near the top-right of the desktop display using Tkinter, running on a background daemon thread to prevent freezing parent process run loops.
- **Key Functions**:
  - `show_tooltip(text: str, duration: float = 5.0) -> None`: Spawns a background daemon thread running `_run()`.
    - `_run()`: Imports `tkinter` (with terminal fallback if headless). Configures root window with `overrideredirect(True)` to hide system headers/borders, forces on top (`wm_attributes("-topmost", True)`), sets window opacity (`wm_attributes("-alpha", 0.9)`), and applies a dark theme `#2d2d2d`. Resolves coordinates using screen width details:
      $$x = \text{screen\_width} - 350, \quad y = 50$$
      Packs the label and schedules `root.destroy` using `root.after` before launching `root.mainloop()`.

### 10. Proof-of-Thought Cryptographic Ledger (`latticeshadow/pot_chain.py`)

- **Path**: `[latticeshadow/pot_chain.py](../latticeshadow/pot_chain.py)`
- **Purpose**: Local JSON Lines event journal with SHA-256 block chaining and an Ed25519 signature over a generated proof. It can reveal local event metadata and cannot attest to events that were never recorded.
- **Key Classes & Methods**:
  - `PoTChain`: Main cryptographic ledger coordinator.
    - `__init__(self, data_dir: str = "~/.latticeshadow")`: Resolves paths, generates or loads the Ed25519 signing key, sets up the thread lock, and initializes the genesis block.
    - `_get_or_create_key(self) -> ed25519.Ed25519PrivateKey`: Loads or generates a new Ed25519 key, writing the PEM format to `~/.latticeshadow/.pot_key.pem` with owner-only permissions (`0o600`).
    - `_ensure_genesis_block(self) -> None`: Appends the genesis block (seq 0, `prev_hash` and `content_fingerprint` set to 64 zero-characters) to the `.pot_chain.jsonl` file if it is empty.
    - `_hash_block(self, block: dict) -> str`: Formats block metadata as a colon-delimited string and returns its SHA-256 hash:
      $$S_i = \text{seq}_i \mathbin{:} \text{timestamp}_i \mathbin{:} \text{event\_type}_i \mathbin{:} \text{content\_fingerprint}_i \mathbin{:} \text{prev\_hash}_i$$
      $$\text{block\_hash}_i = \text{SHA-256}(S_i)$$
    - `_fingerprint_content(self, content: str) -> str`: To avoid storing plaintext in the log, it obfuscates raw text strings using a deterministic salted SHA-256 content fingerprint:
      $$\text{content\_fingerprint} = \text{SHA-256}(\text{content\_salt} \mathbin{\Vert} \text{content})$$
      where `content_salt` is the first 16 characters of the Ed25519 public key hex.
    - `append_event(self, event_type: str, content: str) -> None`: Thread-safe block builder. Reads the last block to determine sequence number and `prev_hash`, computes block hash, and writes the JSON block to the JSONL log file.
    - `generate_proof(self) -> dict`: Reads all blocks from the chain file, signs the final block's hash using the private key, and returns the verification payload.
    - `verify_proof(proof: dict) -> Tuple[bool, str]`: Static method. Re-evaluates block hash calculations, verifies sequence and chain linking continuity, and validates the final block signature using the public key.

#### Biological Hashing Algorithm: Drosophila (FlyHash)
The external package `latticeshadow_db` provides the `DrosophilaHasher` class which implements the biological olfactory system projection of the fruit fly (*Drosophila melanogaster*):
1. Projects input vector $x \in \mathbb{R}^D$ using a random sparse projection matrix $M \in \mathbb{R}^{10000 \times D}$ where columns have standard normal values with a fixed sparsity of 6.
2. Applies a Winner-Take-All (WTA) selection mask $m \in \{0, 1\}^{10000}$ where only the top 5% values are set to 1.
3. Packs binary values into 8-bit bytes (1250 bytes) using `np.packbits`.
4. Computes Hamming distance as:
   $$d_H(H_a, H_b) = \sum \operatorname{popcount}(H_a \oplus H_b)$$
   implemented with SIMD-vectorized operations (`torch.bitwise_count`) or a fast parallel bitwise popcount fallback:
   ```python
c = xor_result.to(torch.int32)
c = (c & 0x55) + ((c >> 1) & 0x55)
c = (c & 0x33) + ((c >> 2) & 0x33)
c = (c & 0x0F) + ((c >> 4) & 0x0F)
dist = c.sum(dim=-1)
   ```

---

## Gotchas, Lookouts, and Edge Cases

### PyObjC Headless & Execution Loop Restrictions

- **Window Server Requirement**: PyObjC AppKit GUI elements (such as the status menu and spotlight panel) require connection to the macOS WindowServer. If you start the app or run tests from a GUI-less SSH session or headless CI environment, they will fail to instantiate or crash.
- **Run Loop Blockers**: Cocoa UI events require an active thread-level Cocoa Run Loop (`AppHelper.runEventLoop()`). Standard python blocking calls (like `time.sleep()`) will lock up the GUI. All heavy operations (such as vector database search or network queries) must run on secondary background threads, updating the UI via `AppHelper.callAfter()`.

### macOS Keychain and Secure Enclave Popups

- **Key Retrieval Waterfall**: The master key retrieval follows a strict waterfall: macOS Keychain -> flat file (`~/.latticeshadow/.key`).
- **Enclave Prompting**: The key path attempts Secure Enclave wrapping, but the code also has a raw-key fallback when wrapping fails. Check the actual key state on the target machine; do not assume every installation has hardware-backed protection. Keychain permission failures may show system prompts or stop access to the vault.

### Virtual Environment Linking & sys.path Resolution

- **Database Separation**: The database backend (`latticeshadow-db`) is a separate package in this monorepo. Install both packages from the repository root with `make setup` on macOS.
- **Interpreters in launchd**: `launchd` runs in a system session. If your launchd plist (`com.latticedb.shadow.plist`) references a system Python `/usr/bin/python3`, it will fail to load the packages installed inside your virtual environment. The installation script `shadow install` automatically writes the absolute path of the *currently running* python interpreter into the plist.

### Metal GPU Rendering Dependencies

- **Class Lookup Safety**: To prevent crashes on macOS instances without Metal support (e.g., VMs or servers), class lookups must check for the presence of the Metal framework:
  ```python
try:
    MTKViewBase = objc.lookUpClass('MTKView')
except Exception:
    MTKViewBase = AppKit.NSView
```
- **Fallback Flag**: The view sets an internal `self._fallback = True` flag if compiling shaders or initializing Metal buffers fails, defaulting back to standard vector rendering.
