MAMBA_PREFIX := $(CURDIR)/.mamba-env
UV := $(MAMBA_PREFIX)/bin/uv
export UV_CACHE_DIR ?= $(CURDIR)/.uv-cache
export UV_PYTHON_INSTALL_DIR ?= $(CURDIR)/.uv-python
# 让 uv 创建的 .venv 也能找到 Mamba 提供的 ecCodes 动态库。
export ECCODES_DIR ?= $(MAMBA_PREFIX)

COPERNICUS_ENV ?= $(CURDIR)/.env.copernicus

.PHONY: env-create env-update sync sync-all test lint check demo acquire-gfs acquire-copernicus acquire-static doctor clean

env-create:
	mamba env create --prefix $(MAMBA_PREFIX) -f environment.yml

env-update:
	mamba env update --prefix $(MAMBA_PREFIX) -f environment.yml --prune

sync:
	@test -x "$(UV)" || (echo "请先执行: make env-create" && exit 1)
	$(UV) sync --python "$(MAMBA_PREFIX)/bin/python" --locked

sync-all:
	@test -x "$(UV)" || (echo "请先执行: make env-create" && exit 1)
	$(UV) sync --python "$(MAMBA_PREFIX)/bin/python" --locked --extra acquisition --extra contracts

test:
	$(UV) run --extra acquisition --extra contracts pytest

lint:
	$(UV) run --extra acquisition ruff check src tests

check: lint test
	$(UV) sync --check --extra acquisition --extra contracts
	$(UV) run --extra acquisition arctic-data --help

demo:
	$(UV) run arctic-data demo --workspace data/demo-run --reset

acquire-gfs:
	$(UV) run --extra acquisition arctic-data acquire-forecast \
		--shared-scenario "$${SCENARIO:-tromso_isfjorden_july_2026_retrospective_v1}" \
		--sources gfs \
		$${SIMULATION_START:+--shared-simulation-start $$SIMULATION_START} \
		$${TYPES:+--types $$TYPES}

acquire-copernicus:
	@test -f "$(COPERNICUS_ENV)" || (echo "缺少 $(COPERNICUS_ENV)" && exit 1)
	$(UV) run --extra acquisition arctic-data acquire-forecast \
		--shared-scenario "$${SCENARIO:-tromso_isfjorden_july_2026_retrospective_v1}" \
		--sources copernicus --copernicus-env-file "$(COPERNICUS_ENV)" \
		$${SIMULATION_START:+--shared-simulation-start $$SIMULATION_START} \
		$${TYPES:+--types $$TYPES}

acquire-static:
	$(UV) run --extra acquisition arctic-data acquire-forecast \
		--shared-scenario "$${SCENARIO:-tromso_isfjorden_july_2026_retrospective_v1}" \
		--sources gebco emodnet \
		$${SIMULATION_START:+--shared-simulation-start $$SIMULATION_START} \
		$${TYPES:+--types $$TYPES}

doctor:
	$(UV) run arctic-data doctor --data-root data

clean:
	rm -rf .venv .pytest_cache .ruff_cache htmlcov .coverage data/demo-run
