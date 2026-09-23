#!/usr/bin/env python3
"""
LatticeShadow CLI Documentation Verification Script.

1. Recursively parses all `.md` files in the repository (excluding .venv, .agents, etc.).
2. Extracts and validates all relative file/directory links and internal/cross-file header anchors.
3. Extracts and validates the syntax of all Python code blocks (```python) using ast.parse.
4. Checks that the manual names the core CLI commands and Recall controls
   that are present in the implementation.
5. Exits 0 on success, or 1 on any broken link, syntax, or product-reference error.
"""

import os
import sys
import re
import ast
import hashlib
import argparse
from typing import List, Dict, Set, Tuple

# Regex to find markdown links: [text](url)
# Excludes images like ![alt](url) by not matching ! before [
LINK_REGEX = re.compile(r'(?<!\!)\[([^\]]+)\]\(([^)]+)\)')

def slugify(text: str) -> str:
    """Standard markdown header anchor generator (GitHub style)."""
    # Lowercase and strip whitespace
    s = text.lower().strip()
    # Remove chars that are not alphanumeric, space, hyphen, or underscore
    chars = [c for c in s if c.isalnum() or c in (' ', '-', '_')]
    s = "".join(chars)
    # Replace spaces with hyphens
    s = s.replace(' ', '-')
    # Collapse multiple hyphens
    while '--' in s:
        s = s.replace('--', '-')
    return s.strip('-')

def parse_markdown_headers(file_path: str) -> Set[str]:
    """Read a markdown file and return all header slugs/anchors."""
    anchors = set()
    if not os.path.exists(file_path):
        return anchors

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#"):
                # Count '#' to verify it's a header
                parts = line.split(maxsplit=1)
                if len(parts) == 2 and all(c == '#' for c in parts[0]):
                    header_text = parts[1]
                    slug = slugify(header_text)
                    if slug:
                        anchors.add(slug)
    return anchors

def parse_markdown_links(file_path: str) -> List[Tuple[int, str, str]]:
    """
    Read a markdown file and return all links: (line_number, text, url).
    """
    links = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line_idx, line in enumerate(f, 1):
            for match in LINK_REGEX.finditer(line):
                text, url = match.groups()
                links.append((line_idx, text, url))
    return links

def parse_python_code_blocks(file_path: str) -> List[Tuple[int, str]]:
    """
    Read a markdown file and return all Python code blocks: (start_line, code_content).
    """
    blocks = []
    in_py_block = False
    current_block = []
    start_line = 0

    with open(file_path, "r", encoding="utf-8") as f:
        for line_idx, line in enumerate(f, 1):
            stripped = line.strip()
            if stripped.startswith("```python"):
                in_py_block = True
                current_block = []
                start_line = line_idx
            elif in_py_block and stripped.startswith("```"):
                in_py_block = False
                blocks.append((start_line, "".join(current_block)))
            elif in_py_block:
                current_block.append(line)
    return blocks

def verify_markdown_file(file_path: str, all_anchors: Dict[str, Set[str]]) -> List[str]:
    """Verify links and python blocks inside a single markdown file."""
    errors = []
    
    # 1. Verify Links
    links = parse_markdown_links(file_path)
    file_dir = os.path.dirname(file_path)
    
    for line_num, text, url in links:
        # Clean URL parameter/queries if any
        url = url.split("?")[0]
        
        # Skip external links
        if url.startswith(("http://", "https://", "mailto:", "ftp:")):
            continue
            
        url_parts = url.split("#", 1)
        path_part = url_parts[0]
        anchor_part = url_parts[1] if len(url_parts) > 1 else None
        
        # Internal anchor check
        if not path_part:
            if anchor_part:
                my_anchors = all_anchors.get(file_path, set())
                if anchor_part not in my_anchors:
                    errors.append(
                        f"{file_path}:{line_num}: Broken internal anchor '#{anchor_part}' in link '[{text}]({url})'"
                    )
            continue
            
        # Relative file/directory check
        target_path = os.path.normpath(os.path.join(file_dir, path_part))
        if not os.path.exists(target_path):
            errors.append(
                f"{file_path}:{line_num}: Broken relative link '[{text}]({url})' -> Target path '{target_path}' does not exist."
            )
            continue
            
        # Target cross-file anchor check
        if anchor_part and target_path.endswith(".md"):
            target_anchors = all_anchors.get(target_path, set())
            # In case it hasn't been loaded
            if not target_anchors:
                target_anchors = parse_markdown_headers(target_path)
                all_anchors[target_path] = target_anchors
                
            if anchor_part not in target_anchors:
                errors.append(
                    f"{file_path}:{line_num}: Broken cross-file anchor '#{anchor_part}' in link '[{text}]({url})'. Target file has headers: {list(target_anchors)}"
                )
                
    # 2. Verify Python Syntax
    py_blocks = parse_python_code_blocks(file_path)
    for start_line, code in py_blocks:
        try:
            ast.parse(code)
        except SyntaxError as e:
            absolute_line = start_line + e.lineno
            errors.append(
                f"{file_path}:{absolute_line}: Python syntax error in code block: {e.msg}\n"
                f"Code snippet around error:\n"
                f"{'-'*40}\n"
                f"{code.splitlines()[max(0, e.lineno-3):min(len(code.splitlines()), e.lineno+2)]}\n"
                f"{'-'*40}"
            )
            
    return errors


def verify_product_manual(repo_root: str) -> List[str]:
    """Keep the hand-written manual anchored to shipped command and UI names."""
    manual_path = os.path.join(repo_root, "docs", "USER_MANUAL.md")
    cli_path = os.path.join(repo_root, "packages", "cli", "latticeshadow", "shadow_cli.py")
    menu_path = os.path.join(repo_root, "packages", "cli", "latticeshadow", "menu.py")
    if not os.path.isfile(manual_path):
        return ["docs/USER_MANUAL.md: product manual is missing"]

    with open(manual_path, encoding="utf-8") as file:
        manual = file.read()
    html_path = os.path.join(repo_root, "docs", "USER_MANUAL.html")
    with open(manual_path, "rb") as file:
        manual_sha = hashlib.sha256(file.read()).hexdigest()
    if not os.path.isfile(html_path):
        return ["docs/USER_MANUAL.html: print edition is missing"]
    with open(html_path, encoding="utf-8") as file:
        html = file.read()
    if f"<!-- markdown-sha256: {manual_sha} -->" not in html:
        return ["docs/USER_MANUAL.html: stale print edition; run scripts/render_user_manual.sh"]
    with open(cli_path, encoding="utf-8") as file:
        cli_tree = ast.parse(file.read(), filename=cli_path)
    with open(menu_path, encoding="utf-8") as file:
        menu_tree = ast.parse(file.read(), filename=menu_path)

    parser_names = {
        node.args[0].value
        for node in ast.walk(cli_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_parser"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    top_level_commands = {
        node.args[0].value
        for node in ast.walk(cli_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_parser"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subparsers"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    menu_strings = {
        node.value
        for node in ast.walk(menu_tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    errors = []
    for command in sorted(top_level_commands):
        if not re.search(rf"`(?:shadow\s+)?{re.escape(command)}(?:\s|`)", manual):
            errors.append(f"docs/USER_MANUAL.md: add '{command}' to the command reference")
    core_commands = (
        "install", "enable", "disable", "remove", "status", "doctor",
        "consent", "pause", "resume", "remember", "timeline", "why",
        "assign-project", "forget", "backup", "mcp",
    )
    for command in core_commands:
        if command not in parser_names:
            errors.append(f"CLI command '{command}' is missing; update the manual and command contract")
        if not re.search(rf"\bshadow\s+{re.escape(command)}\b", manual):
            errors.append(f"docs/USER_MANUAL.md: document 'shadow {command}'")

    recall_controls = (
        "Open Recall…", "Pause capture", "Resume capture", "Copy",
        "Open link/file", "Assign project", "Forget…", "Project", "Source",
        "When", "Unassigned only",
    )
    for label in recall_controls:
        if label not in menu_strings:
            errors.append(f"Recall control '{label}' is missing; update the manual and UI contract")
        if label not in manual:
            errors.append(f"docs/USER_MANUAL.md: document Recall control '{label}'")
    return errors

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.path.dirname(script_dir))
    repo_root = os.path.abspath(parser.parse_args().root)
    
    print(f"Scanning repository root: {repo_root}")
    
    # Find all .md files (excluding .venv, .agents, and build folders)
    md_files = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in (
            '.venv', '.agents', '.migration-backup', '.pytest_cache',
            'latticeshadow_cli.egg-info', '.git', 'build', 'dist',
            '.build', '.swiftpm', 'benchmark_results', 'foundry_memory',
        )]
        for file in files:
            if file.endswith('.md'):
                md_files.append(os.path.join(root, file))
                
    print(f"Found {len(md_files)} markdown file(s) to verify:")
    for f in md_files:
        print(f"  - {os.path.relpath(f, repo_root)}")
        
    # Pre-parse headers and anchors for all files to support cross-referencing
    all_anchors: Dict[str, Set[str]] = {}
    for f in md_files:
        all_anchors[f] = parse_markdown_headers(f)
        
    # Run verification
    all_errors = []
    for f in md_files:
        errors = verify_markdown_file(f, all_anchors)
        all_errors.extend(errors)
    all_errors.extend(verify_product_manual(repo_root))
        
    print("\nVerification Results:")
    print("──────────────────────────────────────────")
    if all_errors:
        print(f"\033[91mFAILED: Found {len(all_errors)} documentation error(s):\033[0m")
        for err in all_errors:
            print(f"  - {err}")
        sys.exit(1)
    else:
        print("\033[92mPASSED: Links, anchors, Python blocks, and product manual references are valid!\033[0m")
        sys.exit(0)

if __name__ == "__main__":
    main()
