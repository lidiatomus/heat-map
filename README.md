# Solar Track Heatmap

Solar irradiance estimation and visualization for the SOLIS solar vehicle.

The project estimates the solar power available at each position of a race track and provides a live replay dashboard using historical telemetry. The reusable `solar_model.py` module is intended to be integrated into a larger Digital Twin.

## VPS deployment: CSI connected to the official InfluxDB

`requirements.txt` lists the direct dependencies. `requirements.lock.txt`
pins the complete dependency tree tested inside the Docker image.

The production entry point is a headless web application. It reads the latest
GPS position from InfluxDB and draws the car on the selected circuit in a
browser; Matplotlib and a desktop session are not required.

### First deployment

On the VPS, clone the repository and create the runtime environment file:

```bash
cp .env.example .env
nano .env
```

Set the connection values for the existing official InfluxDB:

```dotenv
INFLUXDB_URL=https://your-existing-influx.example.com
INFLUXDB_TOKEN=replace-with-the-official-influx-token
INFLUXDB_ORG=replace-with-the-official-org
INFLUXDB_BUCKET=replace-with-the-official-bucket
```

Start CSI:

```bash
docker compose up -d --build --remove-orphans
docker compose ps
```

`--remove-orphans` removes the old InfluxDB container created by previous
versions of this Compose file. It does not delete its named Docker volumes or
touch the official remote InfluxDB. After confirming CSI works, the unused old
local volumes can be reviewed and removed separately by the VPS administrator.

On Windows/PowerShell, from the repository directory, use the same commands:

```powershell
Copy-Item .env.example .env   # first run only
# Edit .env before starting the services
docker compose up -d --build --remove-orphans  # first run or migration
docker compose ps
```

For subsequent starts, when the image is already built:

```powershell
docker compose up -d
docker compose ps
```

Open `http://localhost:8080` for the CSI track view. A healthy installation
shows the `csi` service as `healthy` in `docker compose ps`.

CSI is then available at:

```text
CSI live view: http://<VPS-IP>:8080
```

CSI uses `restart: unless-stopped`, so it restarts after a VPS reboot. It reads
telemetry from the existing official InfluxDB configured in `.env`; this
repository does not create or expose another InfluxDB server. CSI mounts
`data/` read-only, mounts `outputs/` from the repository for generated racing
lines, and keeps its cache in a named volume.

Useful operations:

```bash
docker compose logs -f csi
docker compose restart csi
docker compose pull
docker compose up -d --build
docker compose down
```

`docker compose down` stops CSI. It does not affect the official InfluxDB.

Health endpoints:

```text
http://<VPS-IP>:8080/health/live
http://<VPS-IP>:8080/health/ready
```

### Existing InfluxDB

The CSI image is not tied to the Compose hostname. To connect the same image to
an existing local or remote InfluxDB, set `INFLUXDB_URL` and the other values in
an env file and run:

```bash
docker run -d --name csi --restart unless-stopped \
  --env-file .env \
  -p 8080:8080 \
  -v "$(pwd)/data:/app/data:ro" \
  -v "$(pwd)/outputs:/app/outputs" \
  solis-csi:latest
```

Compose passes `INFLUXDB_URL`, token, organization, and bucket from `.env`
directly to CSI. These four settings are required, and no local InfluxDB
fallback is started.

### Live view configuration

The relevant settings are centralized in `src/settings.py` and supplied by
environment variables:

```dotenv
CSI_DEFAULT_TRACK=targu_mures
CSI_POLL_SECONDS=2
CSI_STALE_AFTER_SECONDS=15
CSI_MAX_TRACK_DISTANCE_KM=10
CSI_COMPARISON_MAX_DISTANCE_KM=0.25
CSI_USE_LIVE_WEATHER=true
INFLUXDB_LOOKBACK=60d
```

The browser can switch between every circuit that has a generated
`data/processed/<track>_track_3d.csv` file.

The compact browser dashboard has two navigation tabs:

1. `Track map`: irradiance map on the left, with calculated-versus-measured
   power and sector irradiance panels on the right;
2. `Racing line`: the generated ideal racing line and the same car marker.

Both maps support `+`, `-`, and reset controls, as well as mouse-wheel zoom.
The power chart keeps up to 300 samples and supports horizontal navigation by
scrollbar, mouse wheel, or click-and-drag.

The telemetry selector provides two modes:

- `Live` reads the latest GPS and MPPT values from InfluxDB. Weather and the
  full-track irradiance map use the current time and the selected circuit, so
  they continue to update even when GPS is missing or stale.
- `History` accepts a start/end date, reads historical GPS positions from
  InfluxDB, and provides play, pause, seek, and replay-speed controls. GPS
  samples farther than `CSI_COMPARISON_MAX_DISTANCE_KM` from the selected
  circuit are excluded.

Live weather is provided by the Open-Meteo Forecast API. Historical replay
uses the Open-Meteo Archive API. If either API is unavailable, the solar model
falls back to pvlib clear-sky data and shows the fallback as the weather source.
The solar calculation still applies track heading, slope, solar position,
obstacle shadowing, panel area, and panel efficiency.

During replay, the marker advances according to the actual differences between
InfluxDB timestamps. Available speeds are 1 minute, 5 minutes, 15 minutes, or
1 hour of historical time per real second. The complete irradiance map is
recalculated when the replay enters a new weather hour. At every GPS sample,
CSI recalculates solar power at the nearest track point and compares it with
MPPT telemetry near the same timestamp.

The ideal-line overlay is loaded from
`outputs/plots/<track>_ideal_racing_line_live.csv`. If that file has not yet
been generated for a circuit, the normal track remains visible and the page
reports that the racing line is unavailable. Generate it with:

```powershell
python src/05_ideal_racing_line.py --track targu_mures
```

Generating a real ideal racing line also requires two manually traced circuit
boundaries:

```text
data/processed/<track>_track_boundaries.geojson
```

The GeoJSON must contain the inner and outer track edges. These files currently
exist for Bucharest and Zolder, so their racing-line overlays are available.
The Targu Mures centerline and 3D track data are available, but its inner and
outer edges have not yet been traced. Until
`data/processed/targu_mures_track_boundaries.geojson` is added and the generator
is run, the dashboard intentionally displays only the base circuit for this
section.

### Current web-dashboard status

Implemented:

- browser-based headless view suitable for a VPS;
- live or latest GPS position read from InfluxDB;
- configurable local or remote InfluxDB connection through `.env`;
- circuit selection for Bucharest, Zolder, and Targu Mures;
- irradiance heatmap with the GPS marker;
- ideal racing-line overlay with the same marker when boundary/output files exist;
- calculated solar power versus measured MPPT power;
- average irradiance by sector;
- stale-data indication for historical samples;
- current Open-Meteo weather independent of GPS freshness;
- Open-Meteo historical weather during replay;
- calendar-based GPS replay with play, pause, seek, and time-based speed;
- historical solar-versus-MPPT comparison at matching timestamps;
- hourly historical irradiance-map updates;
- map zoom and horizontally pannable power history;
- compact black-and-white dashboard layout with side panels;
- Docker image and CSI-only Compose deployment with health checks and connection to the official InfluxDB.

### Planned work

The following items are intentionally left for the next development sessions:

1. Trace the inner and outer edges of Transilvania Motor Ring, save them as
   `data/processed/targu_mures_track_boundaries.geojson`, and generate its ideal
   racing line.
2. Embed or link the CSI web dashboard in Grafana after the live and replay
   workflows are stable.

### Troubleshooting: `organization not found`

If the live view displays `400 Bad Request` and `organization not found`, CSI
can reach InfluxDB, but `INFLUXDB_ORG` from `.env` does not match an
organization that exists in that InfluxDB instance.

1. Check the organization and bucket names in the official InfluxDB.
2. Set `INFLUXDB_ORG`, `INFLUXDB_BUCKET`, and `INFLUXDB_TOKEN` in `.env` to
   values belonging to that same InfluxDB instance.
3. Recreate only the CSI container so it receives the updated environment:

```powershell
docker compose up -d --force-recreate csi
```

Always match `.env` to the existing official InfluxDB configuration. CSI does
not initialize, rename, or delete organizations, buckets, or InfluxDB data.

The track itself can still be displayed while telemetry is unavailable. The
car marker appears after InfluxDB returns valid values for
`VCU_GPS_Latitude` and `VCU_GPS_Longitude` (or the fields configured in
`INFLUXDB_LAT_FIELD` and `INFLUXDB_LON_FIELD`).

---

# Main components

## Pregatirea datelor si selectarea circuitului

Toate scripturile folosesc catalogul `TRACKS` din `src/config.py`. Circuitul se
alege din terminal prin argumentul `--track`:

- `--track zolder`
- `--track bucharest`
- `--track targu_mures`

Comenzile trebuie rulate din directorul principal al proiectului.

### 1. Instalarea dependentelor

```powershell
pip install -r requirements.txt
```

### 2. Descarcarea fisierelor DEM `.tif`

Modelul de elevatie utilizat este
[Copernicus GLO-30 DEM din OpenTopography](https://portal.opentopography.org/raster?opentopoID=OTSDEM.032021.4326.3).

Pentru fiecare circuit:

1. Deschide pagina OpenTopography.
2. Selecteaza pe harta o zona care acopera complet circuitul.
3. Pastreaza sistemul de coordonate WGS84 (`EPSG:4326`).
4. Selecteaza modelul `Digital Surface Model (DSM)`.
5. Alege formatul de iesire `GeoTIFF`/`TIF`.
6. Descarca arhiva sau fisierul rezultat.
7. Redenumeste fisierul si copiaza-l in `data/raw/dem/`.

Numele fisierelor trebuie sa fie exact:

```text
data/raw/dem/zolder_dem.tif
data/raw/dem/bucharest_dem.tif
data/raw/dem/targu_mures_dem.tif
```

Fisierul trebuie sa acopere intreaga pista. Daca rasterul nu acopera toate
punctele circuitului, valorile de elevatie generate de `02_sample_evaluation.py`
vor fi invalide.

### 3. Flux complet pentru Zolder

```powershell
python src/01_fetch_track.py --track zolder
python src/02_sample_evaluation.py --track zolder
python src/03_plot_track.py --track zolder
python src/04_fetch_obstacles.py --track zolder
python src/05_ideal_racing_line.py --track zolder
python src/06_live_dashboard_with_racing_line.py --track zolder
python src/07_solar_dashboard_four_windows.py --track zolder
python src/08_solar_dashboard_influx.py --track zolder
```

### 4. Flux complet pentru Bucuresti

```powershell
python src/01_fetch_track.py --track bucharest
python src/02_sample_evaluation.py --track bucharest
python src/03_plot_track.py --track bucharest
python src/04_fetch_obstacles.py --track bucharest
python src/05_ideal_racing_line.py --track bucharest
python src/06_live_dashboard_with_racing_line.py --track bucharest
python src/07_solar_dashboard_four_windows.py --track bucharest
python src/08_solar_dashboard_influx.py --track bucharest
```

### 5. Flux complet pentru Targu Mures

```powershell
python src/01_fetch_track.py --track targu_mures
python src/02_sample_evaluation.py --track targu_mures
python src/03_plot_track.py --track targu_mures
python src/04_fetch_obstacles.py --track targu_mures
python src/05_ideal_racing_line.py --track targu_mures
python src/06_live_dashboard_with_racing_line.py --track targu_mures
python src/07_solar_dashboard_four_windows.py --track targu_mures
python src/08_solar_dashboard_influx.py --track targu_mures
```

`01_fetch_track.py` si `04_fetch_obstacles.py` folosesc implicit o raza de
1.500 m in jurul circuitului. Raza poate fi modificata, de exemplu:

```powershell
python src/01_fetch_track.py --track targu_mures --search-radius 1800
```

O raza prea mare poate include alte obiecte OSM marcate `highway=raceway`.

### 6. Dashboardul cu racing line

Dashboardul recomandat, care afiseaza si racing line, se porneste cu:

```powershell
python src/06_live_dashboard_with_racing_line.py --track targu_mures
```

Pentru calcularea racing line sunt necesare doua margini ale pistei intr-un
fisier GeoJSON:

```text
data/processed/zolder_track_boundaries.geojson
data/processed/bucharest_track_boundaries.geojson
data/processed/targu_mures_track_boundaries.geojson
```

Fisierul trebuie sa contina cel putin doua geometrii `LineString`, reprezentand
cele doua margini ale pistei. Se poate transmite si un fisier diferit:

```powershell
python src/06_live_dashboard_with_racing_line.py --track targu_mures --boundary-file data/processed/targu_mures_track_boundaries.geojson
```

Fara fisierul de margini, dashboardul poate afisa heatmap-ul, dar nu poate
calcula o racing line intre limitele reale ale pistei.

### 7. Dashboard live cu GPS si MPPT din InfluxDB

Pentru testul real cu masina, foloseste dashboardul Influx in modul live:

```powershell
python src/08_solar_dashboard_influx.py --track targu_mures --live-influx
```

Acest mod nu foloseste fisiere CSV. La fiecare actualizare citeste direct din
InfluxDB:

- ultima latitudine si longitudine GPS;
- tensiunea, curentul si starea celor patru MPPT-uri;
- timestampurile telemetriei.

Configurarea conexiunii se face in fisierul `.env`:

```dotenv
INFLUXDB_URL=https://serverul-influx
INFLUXDB_TOKEN=tokenul-influx
INFLUXDB_ORG=organizatia
INFLUXDB_BUCKET=bucketul
INFLUXDB_MEASUREMENT=solar_vehicle
INFLUXDB_ECU=VCU
INFLUXDB_LAT_FIELD=VCU_GPS_Latitude
INFLUXDB_LON_FIELD=VCU_GPS_Longitude
INFLUXDB_LOOKBACK=30d
```

Implicit, pozițiile GPS si valorile MPPT mai vechi de 30 de secunde sunt
respinse, pentru a nu afisa ca fiind live date ramase in bucket. Pragul si
frecventa de actualizare pot fi schimbate:

```powershell
python src/08_solar_dashboard_influx.py --track targu_mures --live-influx --update-seconds 2 --influx-max-age-seconds 10
```

Modul istoric pe baza unui CSV ramane disponibil separat prin
`--comparison-csv`; `--live-influx` si `--comparison-csv` nu se folosesc
simultan.

### 8. Publicarea datelor pentru Grafana

Grafana citeste measurement-ul `solar_model_live`, generat de procesul:

```powershell
python src/09_grafana_publisher.py --track bucharest
```

Procesul citeste GPS si MPPT din measurement-ul masinii, ruleaza modelul solar
pentru pozitia curenta si scrie rezultatele inapoi in InfluxDB. Telemetria
originala nu este modificata.

Pentru o verificare cu ultima telemetrie Bucuresti din ultimele 90 de zile:

```powershell
python src/09_grafana_publisher.py --track bucharest --lookback 90d --max-age-seconds 7776000 --once
```

Pentru publicare continua in timpul unui test:

```powershell
python src/09_grafana_publisher.py --track bucharest --interval-seconds 2
```

Configurari optionale in `.env`:

```dotenv
GRAFANA_INFLUX_BUCKET=tests
GRAFANA_INFLUX_MEASUREMENT=solar_model_live
```

Tokenul InfluxDB trebuie sa aiba drept de scriere in bucketul ales. Dashboardul
se importa in Grafana din:

```text
grafana/solar_dashboard.json
```

La import se selecteaza sursa InfluxDB configurata cu Flux. Dashboardul contine
harta pozitiei masinii, puterea calculata versus MPPT, numarul de MPPT-uri
active si iradierea calculata.

## solar_model.py

Reusable solar model that estimates:

- solar irradiance
- solar power
- orientation factor
- shadow factor
- pitch factor

using:

- track position
- vehicle heading
- slope
- pvlib sun position
- Open-Meteo weather
- OSM obstacles

The module is independent of the dashboard and can be called directly by the Digital Twin.

Example:

```python
from solar_model import SolarModel

model = SolarModel(use_weather_api=True)

result = model.get_solar_input(
    track="bucharest",
    point_id=320,
    timestamp="2026-06-10 13:30"
)

print(result["solar_power_w"])
```

---

# Replay dashboard

Main dashboard:

```powershell
python src/08_solar_dashboard_influx.py --track bucharest --comparison-csv outputs/solar_model_vs_mppt_2026-06-09_to_2026-06-12.csv
```

The dashboard replays historical telemetry and combines:

- live solar heatmap
- solar racing line
- vehicle GPS position from InfluxDB
- Solar Model vs measured MPPT power
- average irradiance per sector
- historical weather from Open-Meteo

The dashboard can also replay telemetry at different speeds for demonstrations.

---

# Historical telemetry

Telemetry is loaded from InfluxDB.

The replay uses:

- GPS position
- timestamp
- MPPT measurements

Historical GPS positions are displayed directly on the track while the dashboard computes the corresponding solar estimation.

---

# Weather

Two weather modes are supported:

- Open-Meteo Forecast API (live mode)
- Open-Meteo Historical Weather API (historical replay)

If weather data is unavailable, the model automatically falls back to the pvlib clear-sky model.

---

# Solar Model vs MPPT

The project compares

- calculated solar power
- measured MPPT power

using historical telemetry.

Outputs include:

- comparison CSV
- comparison plots
- MAE
- RMSE
- R²

allowing validation of the solar model against real measurements.

---

# Racing line

The project computes an approximate solar racing line using:

- irradiance
- shadowing
- weather
- track boundaries
- smoothness

Outputs:

```
outputs/plots/*ideal_racing_line*.csv
outputs/plots/*ideal_racing_line*.geojson
outputs/plots/*ideal_racing_line*.png
```

---

# Project structure

```
src/
    solar_model.py
    05_ideal_racing_line.py
    06_live_dashboard_with_racing_line.py
    07_solar_dashboard_four_windows.py
    08_solar_dashboard_influx.py
    tools/
        check_gps_history.py
        check_mppt_history.py
        check_obs.py
        compare_historical_solar.py
        list_influx_fields.py

data/
    processed/

outputs/
    plots/
```

---

# Technologies

- Python
- pandas
- GeoPandas
- matplotlib
- pvlib
- Open-Meteo API
- InfluxDB
- OpenStreetMap

---

# Current features

- solar heatmap
- reusable solar model
- historical weather integration
- GPS replay from InfluxDB
- Solar Model vs MPPT comparison
- solar racing line
- vehicle position visualization
- sector irradiance statistics

---

# Future work

- Digital Twin integration
- real-time telemetry
- EKF integration
- battery model
- improved racing line using vehicle dynamics
