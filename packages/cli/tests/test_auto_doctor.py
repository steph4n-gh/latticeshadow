import os
import json
import pytest
from unittest.mock import patch, MagicMock

from latticeshadow.auto_doctor import GitSandbox, select_mutation_target, run_mutation_cycle, run_tests, get_system_idle_time
from latticeshadow import budget

class TestAutoDoctor:
    @patch("subprocess.run")
    def test_get_system_idle_time(self, mock_run):
        mock_run.return_value.stdout = "42\n"
        mock_run.return_value.returncode = 0
        idle = get_system_idle_time()
        assert idle == 42.0
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_sandbox_creation_and_destruction(self, mock_run):
        sandbox = GitSandbox("/mock/project")
        
        # Test create
        repo_ok = MagicMock(returncode=0, stdout="true\n")
        worktree_ok = MagicMock(returncode=0, stderr="")
        cleanup_ok = MagicMock(returncode=0)
        mock_run.side_effect = [repo_ok, worktree_ok, cleanup_ok, cleanup_ok]
        assert sandbox.create() is True
        
        # Test destroy
        sandbox.destroy()
        assert mock_run.call_count >= 2

    @patch("subprocess.run")
    def test_sandbox_creation_skips_non_git_snapshot(self, mock_run):
        sandbox = GitSandbox("/mock/project")
        mock_run.return_value.returncode = 128
        mock_run.return_value.stdout = ""

        assert sandbox.create() is False

    def test_select_mutation_target(self, tmp_path):
        # Create a mock sandbox structure
        latticeshadow_dir = tmp_path / "latticeshadow"
        os.makedirs(latticeshadow_dir)
        
        # Add files
        target = latticeshadow_dir / "target.py"
        target.touch()
        
        # __init__ should be ignored
        init = latticeshadow_dir / "__init__.py"
        init.touch()
        
        selected = select_mutation_target(str(tmp_path))
        assert selected == str(target)

    @patch("subprocess.run")
    def test_run_tests_success(self, mock_run):
        mock_run.return_value.returncode = 0
        assert run_tests("/mock/sandbox", "pytest") is True

    @patch("subprocess.run")
    def test_run_tests_failure(self, mock_run):
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "Test failed"
        assert run_tests("/mock/sandbox", "pytest") is False

    @patch("latticeshadow.auto_doctor.config.get")
    @patch("latticeshadow.auto_doctor.generate_mutation")
    @patch("latticeshadow.auto_doctor.run_tests")
    @patch("latticeshadow.auto_doctor.GitSandbox")
    @patch("subprocess.run")
    def test_run_mutation_cycle_success(self, mock_sub, mock_sandbox_class, mock_run_tests, mock_gen, mock_config):
        # Setup mock sandbox
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_path = "/mock/sandbox"
        mock_sandbox.timestamp = 12345
        mock_sandbox_class.return_value = mock_sandbox
        mock_sandbox.create.return_value = True

        mock_config.return_value = "pytest"
        mock_gen.return_value = "def mutated_func():\n    return 42\n"
        mock_run_tests.return_value = True
        
        # Mock git diff command
        mock_sub.return_value.stdout = "+def mutated_func():\n"
        mock_sub.return_value.returncode = 0

        # Mock select_mutation_target to return a dummy file path
        from unittest.mock import mock_open
        with patch.dict("sys.modules", {"AppKit": MagicMock(), "Cocoa": MagicMock()}), \
             patch("latticeshadow.auto_doctor.select_mutation_target") as mock_target, \
             patch("builtins.open", mock_open(read_data="def original():\n    pass\n")) as m_open:
            
            mock_target.return_value = "/mock/sandbox/latticeshadow/target.py"
            
            # Run the cycle
            run_mutation_cycle()
            
            mock_sandbox.destroy.assert_called_once()

class TestBudgetTracking:
    @patch("latticeshadow.budget._load_budget")
    @patch("latticeshadow.budget._save_budget")
    def test_budget_exhaustion(self, mock_save, mock_load):
        # Mock budget to be at max for today dynamically
        import datetime
        today = datetime.date.today().isoformat()
        mock_load.return_value = {"date": today, "request_count": 50}
        
        with patch("latticeshadow.config.get") as mock_config_get:
            mock_config_get.return_value = 50
            
            # Attempt check
            allowed = budget.check_budget_and_increment()
            assert allowed is False
