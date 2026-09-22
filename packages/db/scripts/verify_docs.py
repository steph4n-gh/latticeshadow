#!/usr/bin/env python3
"""
Verification Script for latticeshadow-db documentation.
- Parses all markdown files to build a map of files, headers, and anchors.
- Validates all relative file, directory, and anchor links.
- Uses ast.parse to compile and verify all Python code blocks.
"""

import os
import re
import ast
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".rollback_snapshots",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "benchmark_results",
    "foundry_memory",
    "latticeshadow_db.egg-info",
}

# Color terminal output
GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"
BOLD = "\033[1m"

def log_success(msg):
    print(f"{GREEN}✓ {msg}{RESET}")

def log_failure(msg):
    print(f"{RED}✗ {msg}{RESET}", file=sys.stderr)

def clean_header_text(text: str) -> str:
    """Strip basic markdown formatting from header text."""
    text = text.replace('**', '').replace('*', '').replace('`', '')
    return text.strip()

def header_to_anchor(text: str) -> str:
    """Convert header text to GitHub-style anchor format."""
    text = clean_header_text(text)
    text = text.lower()
    chars = []
    for c in text:
        if c.isalnum() or c in ('-', '_'):
            chars.append(c)
        elif c == ' ':
            chars.append('-')
    anchor = "".join(chars)
    anchor = re.sub(r'-+', '-', anchor)
    return anchor.strip('-')

def parse_markdown_file(file_path: Path):
    """
    Parses a markdown file and extracts:
    1. All anchors (generated from headers).
    2. All relative links and their anchors.
    3. All python code blocks.
    """
    content = file_path.read_text(encoding="utf-8")
    
    # 1. Extract Headers and generate Anchors
    anchors = set()
    header_pattern = re.compile(r'^#{1,6}\s+(.+)$', re.MULTILINE)
    for match in header_pattern.finditer(content):
        header_text = match.group(1).rstrip('#').strip()
        anchor = header_to_anchor(header_text)
        anchors.add(anchor)
        
    # 2. Extract Links (inline links like [label](path))
    # We ignore links that start with http://, https://, or mailto:
    links = []
    link_pattern = re.compile(r'\[([^\]]*)\]\(([^)]+)\)')
    for match in link_pattern.finditer(content):
        href = match.group(2).strip()
        if href.startswith(('http://', 'https://', 'mailto:')):
            continue
        links.append(href)
        
    # 3. Extract Python Code Blocks
    code_blocks = []
    code_pattern = re.compile(r'```python\n(.*?)\n```', re.DOTALL)
    for match in code_pattern.finditer(content):
        code_blocks.append(match.group(1))
        
    return anchors, links, code_blocks

def main():
    root_dir = Path(__file__).resolve().parents[1]
    
    # Find all .md files in the repository
    md_files = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        # Exclude directories like .git, .venv, pycache, etc.
        dirnames[:] = [
            name for name in dirnames
            if name not in SKIP_DIRS and not name.endswith(".egg-info")
        ]
        if any(part.startswith('.') for part in Path(dirpath).parts):
            continue
        for f in filenames:
            if f.endswith('.md'):
                md_files.append(Path(dirpath) / f)
                
    if not md_files:
        log_failure("No markdown files found in the repository!")
        sys.exit(1)
        
    # Parse all markdown files
    all_anchors = {}  # file_path -> set(anchors)
    all_links = {}    # file_path -> list(links)
    all_code = {}     # file_path -> list(code_blocks)
    
    for f in md_files:
        try:
            anchors, links, code = parse_markdown_file(f)
            all_anchors[f] = anchors
            all_links[f] = links
            all_code[f] = code
            log_success(f"Parsed {f.relative_to(root_dir)}: found {len(anchors)} headers, {len(links)} local links, {len(code)} Python blocks.")
        except Exception as e:
            log_failure(f"Failed to parse {f}: {e}")
            sys.exit(1)
            
    has_errors = False
    
    # 1. Validate Python Code Block Syntax
    print(f"\n{BOLD}Step 1: Validating Python Code Block Syntax...{RESET}")
    for f, blocks in all_code.items():
        rel_path = f.relative_to(root_dir)
        for idx, block in enumerate(blocks, 1):
            try:
                ast.parse(block)
                log_success(f"{rel_path}: Python block #{idx} compiles successfully.")
            except SyntaxError as e:
                log_failure(f"{rel_path}: Python block #{idx} has syntax error on line {e.lineno}: {e.msg}\n--- Code ---\n{block}\n------------")
                has_errors = True
                
    # 2. Validate Relative and Anchor Links
    print(f"\n{BOLD}Step 2: Validating Markdown Links & Anchors...{RESET}")
    for source_file, links in all_links.items():
        source_dir = source_file.parent
        rel_source = source_file.relative_to(root_dir)
        
        for link in links:
            # Parse link into file path and anchor
            if '#' in link:
                file_part, anchor_part = link.split('#', 1)
            else:
                file_part, anchor_part = link, None
                
            # If path points to another file
            if file_part:
                target_path = (source_dir / file_part).resolve()
                # Verify existence of path
                if not target_path.exists():
                    log_failure(f"{rel_source}: Broken link '{link}' (path does not exist: {target_path})")
                    has_errors = True
                    continue
                # If target is a directory, anchors don't apply, path is verified
                if target_path.is_dir():
                    if anchor_part:
                        log_failure(f"{rel_source}: Cannot check anchor '{anchor_part}' on directory path '{file_part}'")
                        has_errors = True
                    else:
                        log_success(f"{rel_source}: Link to directory '{link}' is valid.")
                    continue
                # If target is a file
                target_file = target_path
            else:
                # Local anchor within the same file
                target_file = source_file
                
            # Verify anchor if present
            if anchor_part:
                if target_file not in all_anchors:
                    # Target file is not parsed (might not be a .md file)
                    log_failure(f"{rel_source}: Anchor link '{link}' targets unparsed file '{target_file.relative_to(root_dir)}'")
                    has_errors = True
                elif anchor_part not in all_anchors[target_file]:
                    log_failure(f"{rel_source}: Broken anchor link '{link}' (anchor '#{anchor_part}' not found in target file; available anchors: {sorted(list(all_anchors[target_file]))})")
                    has_errors = True
                else:
                    log_success(f"{rel_source}: Link '{link}' is valid.")
            else:
                log_success(f"{rel_source}: Link to file '{link}' is valid.")
                
    if has_errors:
        log_failure("\nDocumentation verification failed!")
        sys.exit(1)
    else:
        log_success("\nAll documentation links and code block syntax verified successfully!")
        sys.exit(0)

if __name__ == "__main__":
    main()
