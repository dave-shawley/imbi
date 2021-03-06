REVISION = $(shell git rev-parse HEAD | cut -b 1-7)

ifneq (,$(wildcard ./.env))
	include .env
	export $(shell sed 's/=.*//' .env)
endif

.PHONY: all
all: setup scaffolding/postgres/ddl.sql all-tests build-ui

.PHONY: build-ui
build-ui:
	@ cd ui && npm run-script build


.PHONY: build-ui-dev
build-ui-dev:
	@ cd ui && npm run-script dev-build

.PHONY: clean
clean:
	@ docker-compose down
	@ rm -f scaffolding/postgres/ddl.sql
	@ rm -rf imbi/static/css/* imbi/static/fonts/* imbi/static/js/*
	@ rm -rf .env build dist imbi.egg-info env ui/node_modules

.env: scaffolding/postgres/ddl.sql
	@ ./bootstrap

env:
	@ python3 -m venv env
	@ pip3 install -e '.[testing]'

.PHONY: postgres-ready
postgres-ready: .env
ifeq ($(docker-compose ps postgres |grep -c healthy), 1)
	@ $(error Docker image for PostgreSQL is not running, perhaps you forget to run "make bootstrap" or you should "make clean" and try again)
endif

scaffolding/postgres/ddl.sql:
	@ cd ddl && bin/build.sh ../scaffolding/postgres/ddl.sql

.PHONY: setup
setup: .env env ui/node_modules

.PHONY: ddl-setup
ddl-setup: .env

.PHONY: python-setup
python-setup: .env env

.PHONY: ui-setup
ui-setup: ui/node_modules

.PHONY: dist
dist: ui-setup
	@ rm -rf dist
	@ mkdir -p imbi/static/css imbi/static/js
	@ cd ui && npm run sass
	@ cd ui && npm run build
	@ cd ddl && bin/build.sh ../ddl.sql
	@ python3 setup.py sdist

ui/node_modules:
	@ cd ui && npm install

# Testing

.PHONEY: bandit
bandit: env
	@ printf "\nRunning Bandit\n\n"
	@ env/bin/bandit -r imbi

.PHONY: coverage
coverage: .env env
	@ printf "\nRunning Python Tests\n\n"
	@ env/bin/coverage run
	@ env/bin/coverage xml
	@ env/bin/coverage report

.PHONY: eslint
eslint: ui/node_modules
	@ printf "\nRunning eslint\n\n"
	@ cd ui && npm run eslint

.PHONY: flake8
flake8: env
	@ printf "\nRunning Flake8 Tests\n\n"
	@ flake8 --tee --output-file=build/flake8.txt

.PHONY: jest
jest: ui/node_modules
	@ printf "\nRunning jest\n\n"
	@ cd ui && npm run test

# Testing Groups

.PHONY: all-tests
all-tests: ddl-tests python-tests ui-tests

.PHONY: ddl-tests
ddl-tests: postgres-ready
	@ printf "\nRunning DDL Tests\n\n"
	@ docker-compose exec -T postgres /usr/bin/dropdb --if-exists ${REVISION}
	@ docker-compose exec -T postgres /usr/bin/createdb ${REVISION} > /dev/null
	@ cd ddl && bin/build.sh ../build/ddl-${REVISION}.sql
	@ docker-compose exec -T postgres /usr/bin/psql -d ${REVISION} -v ON_ERROR_STOP=1 -f /build/ddl-${REVISION}.sql -X -q --pset=pager=off
	@ docker-compose exec -T postgres /usr/bin/psql -d ${REVISION} -v ON_ERROR_STOP=1 -c "CREATE EXTENSION pgtap;" -X -q --pset=pager=off
	@ cd ddl && docker-compose exec -T postgres /usr/local/bin/pg_prove -v -f -d ${REVISION} tests/*.sql
	@ docker-compose exec -T postgres /usr/bin/dropdb --if-exists  ${REVISION}
	@ rm build/ddl-${REVISION}.sql

.PHONY: python-tests
python-tests: bandit flake8 coverage

.PHONY: ui-tests
ui-tests: ui/node_modules eslint jest
