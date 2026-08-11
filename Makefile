MAMBA_PREFIX := $(CURDIR)/.mamba-env
UV := $(MAMBA_PREFIX)/bin/uv
export UV_CACHE_DIR ?= $(CURDIR)/.uv-cache
export UV_PYTHON_INSTALL_DIR ?= $(CURDIR)/.uv-python
# 让 uv 创建的 .venv 也能找到 Mamba 提供的 ecCodes 动态库。
export ECCODES_DIR ?= $(MAMBA_PREFIX)

COPERNICUS_ENV ?= $(CURDIR)/.env.copernicus

.PHONY: env-create env-update sync sync-all test lint check demo acquire-gfs acquire-copernicus doctor clean

env-create:
	mamba env create --prefix $(MAMBA_PREFIX) -f environment.yml

env-update:
	mamba env update --prefix $(MAMBA_PREFIX) -f environment.yml --prune

sync:
	@test -x "$(UV)" || (echo "请先执行: make env-create" && exit 1)
	$(UV) sync --python "$(MAMBA_PREFIX)/bin/python" --locked

sync-all:
	@test -x "$(UV)" || (echo "请先执行: make env-create" && exit 1)
	$(UV) sync --python "$(MAMBA_PREFIX)/bin/python" --locked --extra acquisition

test:
	$(UV) run --extra acquisition pytest

lint:
	$(UV) run --extra acquisition ruff check src tests

check: lint test
	$(UV) sync --check --extra acquisition
	$(UV) run --extra acquisition arctic-data --help

demo:
	$(UV) run arctic-data demo --workspace data/demo-run --reset

acquire-gfs:
	@if [ -n "$$START" ]; then \
		$(UV) run --extra acquisition arctic-data acquire-forecast \
			--corridor "$${CORRIDOR:-tromso_to_svalbard}" --sources gfs \
			--horizon-hours "$${HORIZON_HOURS:-156}" --start "$$START" $${TYPES:+--types $$TYPES}; \
	else \
		$(UV) run --extra acquisition arctic-data acquire-forecast \
			--corridor "$${CORRIDOR:-tromso_to_svalbard}" --sources gfs \
			--horizon-hours "$${HORIZON_HOURS:-156}" $${TYPES:+--types $$TYPES}; \
	fi

acquire-copernicus:
	@test -f "$(COPERNICUS_ENV)" || (echo "缺少 $(COPERNICUS_ENV)" && exit 1)
	@if [ -n "$$START" ]; then \
			$(UV) run --extra acquisition arctic-data acquire-forecast \
				--corridor "$${CORRIDOR:-tromso_to_svalbard}" --sources copernicus \
				--copernicus-env-file "$(COPERNICUS_ENV)" \
				--horizon-hours "$${HORIZON_HOURS:-156}" --start "$$START" $${TYPES:+--types $$TYPES}; \
		else \
			$(UV) run --extra acquisition arctic-data acquire-forecast \
				--corridor "$${CORRIDOR:-tromso_to_svalbard}" --sources copernicus \
				--copernicus-env-file "$(COPERNICUS_ENV)" \
				--horizon-hours "$${HORIZON_HOURS:-156}" $${TYPES:+--types $$TYPES}; \
		fi

doctor:
	$(UV) run arctic-data doctor --data-root data

clean:
	rm -rf .venv .pytest_cache .ruff_cache htmlcov .coverage data/demo-run
