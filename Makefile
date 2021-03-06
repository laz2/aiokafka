# Some simple testing tasks (sorry, UNIX only).

FLAGS=
SCALA_VERSION?=2.11
KAFKA_VERSION?=0.10.1.0
DOCKER_IMAGE=aiolibs/kafka:$(SCALA_VERSION)_$(KAFKA_VERSION)

setup:
	pip install -r requirements-dev.txt
	pip install -Ue .

flake:
	extra=$$(python -c "import sys;sys.stdout.write('--exclude tests/test_pep492.py') if sys.version_info[:3] < (3, 5, 0) else sys.stdout.write('')"); \
	flake8 aiokafka tests $$extra

test: flake
	py.test -s --no-print-logs --docker-image $(DOCKER_IMAGE) $(FLAGS) tests

vtest: flake
	py.test -s -v --no-print-logs --docker-image $(DOCKER_IMAGE) $(FLAGS) tests

cov cover coverage: flake
	py.test -s --no-print-logs --cov aiokafka --cov-report html --docker-image $(DOCKER_IMAGE) $(FLAGS) tests
	@echo "open file://`pwd`/htmlcov/index.html"

clean:
	rm -rf `find . -name __pycache__`
	rm -f `find . -type f -name '*.py[co]' `
	rm -f `find . -type f -name '*~' `
	rm -f `find . -type f -name '.*~' `
	rm -f `find . -type f -name '@*' `
	rm -f `find . -type f -name '#*#' `
	rm -f `find . -type f -name '*.orig' `
	rm -f `find . -type f -name '*.rej' `
	rm -f .coverage
	rm -rf htmlcov
	rm -rf docs/_build/
	rm -rf cover
	rm -rf dist

doc:
	make -C docs html
	@echo "open file://`pwd`/docs/_build/html/index.html"

.PHONY: all flake test vtest cov clean doc
