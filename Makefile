# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

# lpbridge -- dev commands
#
# The agent is macOS system Python 3, stdlib only. One shared podman image
# ("lpbridge:latest") carries the foomatic database + the common open driver
# families, so adding a printer never rebuilds it.

PYTHON   ?= /usr/bin/python3
PROFILE  ?=
PORT     ?= 8631
PROVIDER ?= applehv

export CONTAINERS_MACHINE_PROVIDER = $(PROVIDER)

PROFILE_ARG = $(if $(PROFILE),--profile $(PROFILE),--auto)

.PHONY: help
help:
	@sed -n 's/^## //p' $(MAKEFILE_LIST)

## image             build the shared "any printer" filter image
.PHONY: image
image:
	$(PYTHON) -c "from agent.runtime.podman import PodmanRuntime as R; R().build(force=True)"

## discover          list detected printers + the driver each one needs
.PHONY: discover
discover:
	$(PYTHON) -m agent --discover

## agent-run         run the agent in the foreground (PROFILE=<id> or auto)
.PHONY: agent-run
agent-run:
	$(PYTHON) -m agent $(PROFILE_ARG) --port $(PORT) \
		--transport dryrun --keep-spool --verbose

## resolve           ask the engine's cupsd which drivers a printer gets, ranked
##                   e.g. make resolve MAKE=Samsung MODEL="ML-2580N"
.PHONY: resolve
resolve:
	$(PYTHON) -c "from agent.runtime.podman import PodmanRuntime as R; \
		from agent.engine import Engine; import json; \
		print(json.dumps(Engine(R()).rank_drivers(dict(make='$(MAKE)', model='$(MODEL)')), indent=2))"

## test              unit + golden (golden auto-skips without podman)
.PHONY: test
test:
	$(PYTHON) tests/run.py

## test-unit         unit tests only (no podman)
.PHONY: test-unit
test-unit:
	$(PYTHON) tests/run.py codec attributes profiles pipeline

## lint-profiles     schema-validate every profiles/*.yaml
.PHONY: lint-profiles
lint-profiles:
	$(PYTHON) -c "from agent.profiles import schema; \
		p=schema.load_dir('profiles'); print('ok:', ', '.join(sorted(p)))"

## machine           bring the podman machine up (first run does 'init')
.PHONY: machine
machine:
	@podman machine inspect >/dev/null 2>&1 && podman machine start || \
		podman machine init --now

## uninstall         remove a printer's macOS queues (PROFILE=<id>)
.PHONY: uninstall
uninstall:
	$(PYTHON) -m agent $(PROFILE_ARG) --uninstall

## bootstrap         from nothing to an installed app: CLT, podman, engine VM, app
.PHONY: bootstrap
bootstrap:
	tools/bootstrap.sh

## app               assemble self-contained build/lpbridge.app (ad-hoc signed)
.PHONY: app
app:
	$(PYTHON) packaging/build_app.py

## dmg               build the drag-to-Applications installer (after make app)
.PHONY: dmg
dmg:
	$(PYTHON) packaging/build_dmg.py

## vm                build the bundled micro-VM image (drops the podman dep)
.PHONY: vm
vm:
	tools/build-vm.sh

## vm-install        build the VM image and drop it where the agent finds it
.PHONY: vm-install
vm-install: vm
	mkdir -p "$(HOME)/Library/Application Support/lpbridge/vm"
	cp build/lpbridge-vm-*.img "$(HOME)/Library/Application Support/lpbridge/vm/"

## sources           collect the corresponding source for the engine (release asset)
.PHONY: sources
sources:
	tools/collect-sources.sh

## clean             drop caches and per-job spool
.PHONY: clean
clean:
	rm -rf "$(HOME)/Library/Application Support/lpbridge/cache" \
	       "$(HOME)/Library/Application Support/lpbridge/ppd" \
	       "$(HOME)/Library/Application Support/lpbridge/spool"
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
