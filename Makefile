.PHONY: all setup data assistant geo travel demand forecast export web test run clean

VENV = .venv/bin/python
NPM = npm

all: geo travel demand forecast export web

# One-time: Python env (uv) and the two raw downloads (~580 MB). Geofabrik supports resuming with -C -.
setup:
	uv venv -p 3.12 .venv && uv pip install -p .venv -r requirements.txt
	cd web && $(NPM) install

data:
	mkdir -p data/raw
	curl -L -C - --retry 5 -o data/raw/southern-zone-latest.osm.pbf https://download.geofabrik.de/asia/india/southern-zone-latest.osm.pbf
	curl -L --retry 5 -o data/raw/ind_ppp_2020_1km_Aggregated.tif https://data.worldpop.org/GIS/Population/Global_2000_2020_1km/2020/IND/ind_ppp_2020_1km_Aggregated.tif

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

# Dispatch assistant backend on :8787 (needs GEMINI_API_KEY in .env); the dev server proxies /api to it
assistant:
	$(VENV) backend/assistant.py

run:
	cd web && $(NPM) run dev

test:
	$(VENV) -c "import highspy, osmnx, geopandas, h3, lightgbm, simpy; print('All core dependencies intact!')"
	cd web && npx tsc -b

clean:
	rm -rf data/processed/* web/public/data/*
