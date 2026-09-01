.PHONY: help data test bench image invoke clean

IMAGE ?= tep-guard
REGION ?= us-east-1

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-10s %s\n", $$1, $$2}'

data: ## Download the TEP benchmark data (26 MB)
	python scripts/download_data.py

test: ## Run the test suite
	python -m pytest

bench: ## Fit, benchmark all 21 faults, write artifacts/ and reports/
	python scripts/run_benchmark.py

image: ## Build the Lambda container image
	docker build -f lambda/Dockerfile -t $(IMAGE) .

invoke: ## Run the handler locally against fault 4 (no Docker, no AWS)
	@PYTHONPATH=src:lambda MODEL_PATH=artifacts/monitor.json python scripts/local_invoke.py

clean:
	rm -rf .pytest_cache __pycache__ src/tepguard/__pycache__ tests/__pycache__
