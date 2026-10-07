# ☀️ Solar Track Heatmap & Telemetry Dashboard

> **Real-time solar irradiance estimation, vehicle telemetry replay, and solar-optimal racing line generation for solar-powered racing vehicles.**

---

## 📌 Overview

**Solar Track Heatmap** is an end-to-end telemetry and physics modeling platform engineered for solar electric vehicles (SEVs). The system estimates available solar power at any position along a race track, compares real-time/historical MPPT power generation against theoretical models, and visualizes the car's telemetry on a headless web dashboard.

### Key Capabilities:
- **High-Precision Solar Modeling:** Evaluates track slope, heading, solar elevation/azimuth (via `pvlib`), real-time weather metrics, and environmental shading (OSM 3D obstacles).
- **Live & Historical Replay Dashboard:** Headless, browser-based telemetry monitoring using real-time GPS and MPPT data from InfluxDB.
- **Solar Model vs. MPPT Validation:** Automated error calculation (MAE, RMSE, R²) benchmarking model output against physical vehicle measurements.
- **Solar Racing Line Optimization:** Derives optimal vehicle positioning within track boundaries to maximize sun exposure and minimize shadow losses.

---

## 🏗️ Architecture & Data Pipeline

```text
[ OpenStreetMap (OSM) ] ──> Track Centerline & Obstacles ──┐
[ Copernicus GLO-30 DEM ] ─> 3D Elevation & Track Slope   ──┼──> [ Solar Physics Model ]
[ Open-Meteo Weather ]   ──> DNI / DHI / Cloud Coverage    ──┤          │
                                                            │          ▼
[ InfluxDB (Telemetry) ] ──> GPS & MPPT Measurements     ───┴──> [ Web Dashboard / Grafana ]
```

---

## 🛠️ Tech Stack

- **Core:** Python, NumPy, pandas, GeoPandas, Shapely
- **Solar & Weather:** `pvlib-python`, Open-Meteo API (Forecast & Historical Archive)
- **Geodata:** OpenStreetMap (Overpass API), Copernicus GLO-30 DEM (WGS84 / EPSG:4326)
- **Telemetry & Visualization:** InfluxDB v2, Grafana, Docker, Docker Compose

---

## 🚀 Getting Started

### 1. Prerequisites & Installation

Clone the repository and install dependencies:

```bash
git clone https://github.com/<your-username>/solar-track-heatmap.git
cd solar-track-heatmap
pip install -r requirements.txt
```

### 2. Digital Elevation Model (DEM) Setup

The project uses the Copernicus GLO-30 DEM from OpenTopography.

Download the corresponding `.tif` files covering each track in WGS84 (`EPSG:4326`) and place them in `data/raw/dem/`:

```text
data/raw/dem/zolder_dem.tif
data/raw/dem/bucharest_dem.tif
data/raw/dem/targu_mures_dem.tif
```

---

## ⚡ Data Processing Pipeline

All execution scripts accept the target circuit via the `--track` flag (`zolder`, `bucharest`, `targu_mures`). Run these commands from the project root:

```bash
# 1. Fetch circuit centerline from OpenStreetMap
python src/01_fetch_track.py --track bucharest

# 2. Sample 3D track elevation and compute slopes from DEM
python src/02_sample_evaluation.py --track bucharest

# 3. Generate track layout and gradient plots
python src/03_plot_track.py --track bucharest

# 4. Fetch surrounding obstacles (buildings, trees) for shadow calculation
python src/04_fetch_obstacles.py --track bucharest

# 5. Compute the solar-optimized ideal racing line (requires boundaries GeoJSON)
python src/05_ideal_racing_line.py --track bucharest
```

> **Note on Racing Lines:** Calculating the optimal line requires inner and outer track limits in `data/processed/<track>_track_boundaries.geojson`.

---

## 📊 Telemetry Dashboards

### Option A: Local Telemetry Dashboard (`src/08_solar_dashboard_influx.py`)

* **Live Mode:** Continuously pulls the latest GPS and MPPT status from InfluxDB.
  ```bash
  python src/08_solar_dashboard_influx.py --track targu_mures --live-influx --update-seconds 2
  ```

* **Historical Replay Mode:** Replays a specific telemetry log from CSV with solar validation.
  ```bash
  python src/08_solar_dashboard_influx.py --track bucharest --comparison-csv outputs/solar_model_vs_mppt.csv
  ```

### Option B: Grafana Publisher (`src/09_grafana_publisher.py`)

Publishes continuous solar model computations back to InfluxDB under the measurement `solar_model_live` for visualization in Grafana:

```bash
python src/09_grafana_publisher.py --track bucharest --interval-seconds 2
```
*Import the pre-configured board from `grafana/solar_dashboard.json` into your Grafana instance.*

---

## 🐳 Production Deployment (Docker / VPS)

The core monitoring UI (**CSI**) is a headless web service designed for deployment on a remote server.

### 1. Environment Configuration

Create and edit the environment file:

```bash
cp .env.example .env
```

Configure your InfluxDB connection:

```dotenv
INFLUXDB_URL=https://your-influx-server.example.com
INFLUXDB_TOKEN=your-service-token
INFLUXDB_ORG=your-organization
INFLUXDB_BUCKET=your-bucket
CSI_DEFAULT_TRACK=targu_mures
CSI_USE_LIVE_WEATHER=true
```

### 2. Start Services

```bash
# Build and run the service
docker compose up -d --build

# Verify container health
docker compose ps
```

The service will be accessible at:
- **Dashboard UI:** `http://<SERVER-IP>:8080`
- **Healthchecks:** `http://<SERVER-IP>:8080/health/live` | `/health/ready`

---

## 🔌 API & Modular Usage

The physics engine is decoupled from visualization and can be integrated into broader vehicle simulations or a **Digital Twin**:

```python
from solar_model import SolarModel

# Initialize model with online weather integration
model = SolarModel(use_weather_api=True)

# Query estimated solar power for a specific circuit point and timestamp
result = model.get_solar_input(
    track="bucharest",
    point_id=320,
    timestamp="2026-06-10 13:30:00"
)

print(f"Estimated Solar Output: {result['solar_power_w']:.2f} W")
print(f"Shadow Attenuation Factor: {result['shadow_factor']:.2f}")
```

---

## 📁 Repository Structure

```text
├── data/
│   ├── raw/dem/            # GeoTIFF elevation models
│   └── processed/          # 3D Track nodes, boundary GeoJSONs
├── grafana/                # Pre-built Grafana dashboard configurations
├── outputs/plots/          # Generated heatmaps and racing lines
├── src/
│   ├── config.py           # Circuit coordinates and catalog configurations
│   ├── settings.py         # Runtime settings and environment parsing
│   ├── solar_model.py      # Standalone solar calculation engine
│   ├── 01_fetch_track.py   # OSM track downloader
│   ├── 02_sample_evaluation.py # DEM sampler and slope calculator
│   ├── 05_ideal_racing_line.py # Solar line optimization script
│   ├── 08_solar_dashboard_influx.py # Main telemetry and replay interface
│   └── 09_grafana_publisher.py      # Telemetry consumer/publisher pipeline
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

---

## 🗺️ Roadmap

- [ ] Complete inner/outer edge boundary mapping for Transilvania Motor Ring (`targu_mures`).
- [ ] Implement Extended Kalman Filter (EKF) for enhanced GPS/inertial tracking.
- [ ] Couple the solar model directly with a high-voltage battery state-of-charge (SoC) model.
- [ ] Integrate full vehicle lateral and longitudinal dynamics into the racing line solver.

---

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.
