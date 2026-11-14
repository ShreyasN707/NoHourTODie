.PHONY: all geo travel demand forecast export web test run clean

VENV = .venv/bin/python
NPM = npm

all: geo travel demand forecast export web

geo:
	$(VENV) pipeline/geo.py

travel:
	$(VENV) pipeline/travel.py

demand:
	$(VENV) pipeline/demand.py

forecast:
	$(VENV) pipeline/forecast.py

export:
	$(VENV) pipeline/export.py

web:
	cd web && $(NPM) run build

run:
	cd web && $(NPM) run dev

test:
	$(VENV) -c "import highspy, osmnx, geopandas, h3, lightgbm, simpy; print('All core dependencies intact!')"
	cd web && npx tsc -b

clean:
	rm -rf data/processed/* web/public/data/*
