PYTHON ?= python3
VENV := $(CURDIR)/.venv
VPY := $(VENV)/bin/python

.PHONY: setup setup-db native-lwe test test-db test-cli test-model test-hardware docs

setup:
	$(PYTHON) -m venv "$(VENV)"
	"$(VPY)" -m pip install --upgrade pip
	@set -e; if [ "$$(uname -s)" = Darwin ]; then \
		"$(VPY)" -m pip install -r requirements-dev.txt -e './packages/db[server]' -e ./packages/cli; \
		$(MAKE) native-lwe; \
	else \
		"$(VPY)" -m pip install -r requirements-dev.txt -e './packages/db[server]'; \
	fi

setup-db:
	$(PYTHON) -m venv "$(VENV)"
	"$(VPY)" -m pip install --upgrade pip
	"$(VPY)" -m pip install -r requirements-dev.txt -e './packages/db[server]'

native-lwe:
	env -u SDKROOT xcrun --sdk macosx clang++ -std=c++17 -O2 -mmacosx-version-min=11.0 -dynamiclib packages/cli/latticeshadow/lwe.cpp -o packages/cli/latticeshadow/liblwe.dylib

test: test-db
	@if [ "$$(uname -s)" = Darwin ]; then $(MAKE) test-cli; fi

test-db:
	"$(VPY)" -m pytest packages/db/tests -q

test-cli:
	"$(VPY)" -m pytest packages/cli/tests -q

test-model:
	"$(VPY)" -m pytest packages/cli/tests/test_real_model.py -q -m model

test-hardware:
	"$(VPY)" -m pytest packages/cli/tests/test_security_enclave.py -q -m hardware

docs:
	"$(VPY)" packages/db/scripts/verify_docs.py
	"$(VPY)" packages/cli/scripts/verify_docs.py --root .
