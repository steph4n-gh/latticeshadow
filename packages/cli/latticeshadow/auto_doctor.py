import os
import time
import random
import subprocess
import glob
import logging
import threading
from latticeshadow import config
from latticeshadow.llm import ShadowLLM, LLMError
from latticeshadow import budget

logger = logging.getLogger("shadowd.auto_doctor")

# Directory of the project to optimize
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MUTATIONS_DIR = os.path.expanduser("~/.latticeshadow/mutations")
DIFFS_DIR = os.path.expanduser("~/.latticeshadow/pending_diffs")

def get_system_idle_time() -> float:
    """Returns the system idle time in seconds using macOS ioreg."""
    try:
        res = subprocess.run(
            "ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print int($NF/1000000000); exit}'",
            shell=True,
            capture_output=True,
            text=True
        )
        val = res.stdout.strip()
        if val.isdigit():
            return float(val)
    except Exception as e:
        logger.warning("Failed to query system idle time: %s", e)
    return 0.0

class GitSandbox:
    def __init__(self, project_dir: str):
        self.project_dir = project_dir
        self.timestamp = time.time_ns()
        self.branch_name = f"auto_doctor_mutation_{self.timestamp}"
        self.sandbox_path = os.path.join(MUTATIONS_DIR, f"sandbox_{self.timestamp}")

    def create(self) -> bool:
        """Create a new git worktree sandbox."""
        try:
            repo_check = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
            )
            if repo_check.returncode != 0 or repo_check.stdout.strip() != "true":
                logger.warning("Auto-Doctor disabled: %s is not a git worktree.", self.project_dir)
                return False

            os.makedirs(MUTATIONS_DIR, exist_ok=True)
            # Create a separate worktree on a new branch
            res = subprocess.run(
                ["git", "worktree", "add", "-b", self.branch_name, self.sandbox_path],
                cwd=self.project_dir,
                capture_output=True,
                text=True
            )
            if res.returncode != 0:
                logger.error("Failed to create git worktree: %s", res.stderr)
                return False
            return True
        except Exception as e:
            logger.error("Exception creating git worktree: %s", e)
            return False

    def destroy(self):
        """Removes the worktree and deletes the branch."""
        try:
            # Remove worktree
            subprocess.run(
                ["git", "worktree", "remove", self.sandbox_path, "--force"],
                cwd=self.project_dir,
                capture_output=True
            )
            # Delete branch
            subprocess.run(
                ["git", "branch", "-D", self.branch_name],
                cwd=self.project_dir,
                capture_output=True
            )
            # Cleanup directory if it persists
            if os.path.exists(self.sandbox_path):
                subprocess.run(["rm", "-rf", self.sandbox_path])
        except Exception as e:
            logger.warning("Failed to destroy sandbox worktree cleanly: %s", e)

def select_mutation_target(sandbox_path: str) -> str:
    """Scans the sandbox for source files to mutate."""
    # Only target latticeshadow source files
    py_files = glob.glob(os.path.join(sandbox_path, "latticeshadow", "*.py"))
    # Exclude __init__.py, config.py, and setup files
    py_files = [
        f for f in py_files 
        if not f.endswith("__init__.py") and not f.endswith("config.py") and not f.endswith("auto_doctor.py")
    ]
    if py_files:
        return random.choice(py_files)
    return ""

def generate_mutation(file_content: str) -> str:
    """Invokes LLM to propose a mutation (optimization/hardening)."""
    # Check daily request budget first
    if not budget.check_budget_and_increment():
        logger.warning("Daily LLM request budget exceeded. Mutation cycle aborted.")
        return ""

    prompt = (
        "You are an expert systems engineer. I will give you the contents of a Python file. "
        "Propose a single specific optimization or security hardening change (e.g. replacing bare excepts, "
        "closing unclosed file descriptors, replacing os.system with subprocess, or improving loop performance). "
        "Output the COMPLETE new file contents. Do NOT explain your changes. Output ONLY the raw Python code."
    )
    
    try:
        llm = ShadowLLM.from_config()
        if not llm:
            return ""
        mutated_code = llm.complete(prompt, file_content)
        # Strip markdown code blocks if any
        if mutated_code.startswith("```"):
            lines = mutated_code.splitlines()
            if lines[0].startswith("```python") or lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            mutated_code = "\n".join(lines)
        return mutated_code.strip()
    except Exception as e:
        logger.error("Failed to generate LLM mutation: %s", e)
        return ""

def run_tests(sandbox_path: str, command: str) -> bool:
    """Runs verification tests inside the sandbox."""
    try:
        # We need to run PYTHONPATH=. pytest or similar in the sandbox
        res = subprocess.run(
            command,
            shell=True,
            cwd=sandbox_path,
            capture_output=True,
            text=True
        )
        if res.returncode == 0:
            return True
        logger.info("Sandbox verification failed (exit code %d): %s", res.returncode, res.stderr)
        return False
    except Exception as e:
        logger.error("Exception during sandbox test verification: %s", e)
        return False

def run_mutation_cycle():
    """Performs a full mutation cycle: sandbox -> mutate -> verify -> save diff -> destroy sandbox."""
    sandbox = GitSandbox(PROJECT_DIR)
    if not sandbox.create():
        return
        
    try:
        target_file = select_mutation_target(sandbox.sandbox_path)
        if not target_file:
            logger.info("No suitable mutation targets found.")
            return
            
        logger.info("Auto-Doctor target selected: %s", os.path.basename(target_file))
        
        with open(target_file, "r") as f:
            original_content = f.read()
            
        mutated = generate_mutation(original_content)
        if not mutated or mutated == original_content:
            logger.info("No mutations proposed by LLM.")
            return
            
        with open(target_file, "w") as f:
            f.write(mutated)
            
        test_cmd = config.get("automation.test_command") or "pytest"
        
        # Verify the changes
        # Run test inside worktree
        if run_tests(sandbox.sandbox_path, test_cmd):
            # Tests passed! Get unified diff using git diff
            res = subprocess.run(
                ["git", "diff", "--unified"],
                cwd=sandbox.sandbox_path,
                capture_output=True,
                text=True
            )
            diff_text = res.stdout
            if diff_text:
                os.makedirs(DIFFS_DIR, exist_ok=True)
                diff_path = os.path.join(DIFFS_DIR, f"mutation_{sandbox.timestamp}.diff")
                with open(diff_path, "w") as f:
                    f.write(diff_text)
                logger.info("Successfully generated and verified mutation: %s", diff_path)
                try:
                    from latticeshadow.repair_queue import create_repair_proposal

                    create_repair_proposal(
                        summary=f"Auto-Doctor verified mutation for {os.path.basename(target_file)}",
                        source="auto_doctor",
                        diff_path=diff_path,
                        risk="medium",
                        tests=[test_cmd],
                        provenance={
                            "target_file": os.path.relpath(target_file, sandbox.sandbox_path),
                            "sandbox_branch": sandbox.branch_name,
                        },
                    )
                except Exception as qe:
                    logger.warning("Failed to queue Auto-Doctor repair proposal: %s", qe)
                
                # Notify the user
                from latticeshadow.shadowd import _notify
                _notify(
                    "LatticeShadow Auto-Doctor", 
                    f"Evolved {os.path.basename(target_file)} successfully! Press Ctrl+G to apply."
                )
    finally:
        sandbox.destroy()

class AutoDoctorThread(threading.Thread):
    def __init__(self):
        super().__init__(name="AutoDoctorThread", daemon=True)
        self.running = True
        
    def run(self):
        logger.info("Auto-Doctor background monitor started.")
        while self.running:
            from latticeshadow import consent
            enabled = consent.surface_enabled("auto_doctor")
            if enabled:
                threshold = config.get("automation.idle_threshold_seconds") or 300
                idle = get_system_idle_time()
                if idle >= threshold:
                    logger.info("System is idle (%d seconds). Initiating mutation cycle...", idle)
                    try:
                        run_mutation_cycle()
                    except Exception as e:
                        logger.error("Error in Auto-Doctor mutation cycle: %s", e)
                    # Don't spin immediately; wait at least 15 minutes after a cycle
                    time.sleep(900)
            # Sleep 30 seconds between checks
            time.sleep(30)
