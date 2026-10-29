# PRQA — common commands. `make run` after every reboot (the drive's mount path can change; see scripts/prqa_up.sh).
SHELL   := /bin/bash
ROOT    := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
SECRETS ?= $(HOME)/.config/prqa/secrets.env
COMPOSE := docker compose -f "$(ROOT)/compose/docker-compose.yml" --env-file "$(ROOT)/compose/.env" \
           $(if $(wildcard $(SECRETS)),--env-file "$(SECRETS)") --profile phase2

.DEFAULT_GOAL := help
.PHONY: help install check-install run stop restart status ps logs bridge-logs test lint preview

help:            ## list the commands
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-14s %s\n", $$1, $$2}'

install:         ## install everything missing: apt packages, docker access, poetry env, Claude Code CLI, Docker images
	@scripts/setup/install.sh

check-install:   ## report what is installed / missing (changes nothing)
	@scripts/setup/install.sh --check

run:             ## start everything (containers, n8n bridge, label trigger) and check readiness
	@scripts/prqa_up.sh

stop:            ## stop the bridge and all PRQA containers (data is kept)
	-@systemctl --user stop prqa-n8n-bridge.service
	@$(COMPOSE) down

restart: stop run ## stop, then run

status:          ## readiness, eligible PRs and processed runs (read-only)
	@python3 scripts/pipeline/prqa_auto_runner.py status

ps:              ## container states
	@$(COMPOSE) ps -a

logs:            ## follow container logs (S=service to pick one, e.g. make logs S=prqa-worker)
	@$(COMPOSE) logs -f --tail 50 $(S)

bridge-logs:     ## follow the n8n bridge log
	@journalctl --user -u prqa-n8n-bridge -f

test:            ## unit tests + workflow validation (+ synthetic e2e when the stack is up)
	@scripts/run_synthetic_tests.sh

lint:            ## static checks (ruff, from the poetry environment)
	@poetry run ruff check scripts worker proxy mocks tests

preview:         ## build and gate the prompt for one PR without running it (PR=N)
	@test -n "$(PR)" || { echo "usage: make preview PR=<number>"; exit 64; }
	@python3 scripts/pipeline/prqa_run_single_pr.py preview --pr $(PR) --preview-only
