# -*- coding: utf-8 -*-
import os
import sys
import json
import math
import time
import io
import xml.etree.ElementTree as ET
import requests

from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter

os.environ["PYTHONIOENCODING"] = "utf-8"
import streamlit as st
import folium
from streamlit_folium import st_folium
import streamlit.components.v1 as components

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload

import gspread

# --- הגדרות Google Sheets ---
SPREADSHEET_ID = "17XwCMZnaXCr6049QYfCf33RoCzrEemQ70hYpccBEFQA"

def get_sheets_client():
    if st.session_state.get('force_offline', False):
        return None
    try:
        info = st.secrets["gcs_connections"]
        return gspread.service_account_from_dict(info)
    except Exception as e:
        if not st.session_state.get('force_offline', False):
            st.error(f"שגיאה בהתחברות ל-Google Sheets: {e}")
        return None

st.set_page_config(layout="wide", page_title="מדריך טיולים עירוני", initial_sidebar_state="expanded")

# --- רישום ה-PWA והזרקת המלצות התקנה בדפדפן ---
pwa_script = """
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#4CAF50">
<script>
    if ('serviceWorker' in navigator) {
        window.addEventListener('load', () => {
            navigator.serviceWorker.register('/service-worker.js')
                .then((reg) => console.log('PWA Service Worker registered successfully:', reg.scope))
                .catch((err) => console.error('PWA Service Worker registration failed:', err));
        });
    }
</script>
"""
components.html(pwa_script, height=0, width=0)

# --- הזרקת CSS ליישור סרגל הצד והרכיבים לימין (RTL) ---
st.markdown("""
    <style>
    [data-testid="stSidebar"] { direction: rtl !important; text-align: right !important; }
    [data-testid="stSidebar"] * { direction: rtl !important; text-align: right !important; }
    .stMultiSelect, .stSelectbox, .stTextInput, .stRadio { direction: rtl !important; text-align: right !important; }
    h1, h2, h3, h4, h5, h6 { text-align: right !important; direction: rtl !important; }
    </style>
""", unsafe_allow_html=True)

# --- הגדרות Google Drive ---
DRIVE_FOLDER_ID = "1ZjZlkh2TxPAiRpnaN9AI3Cgs_OU997GO"
FILE_NAME = "custom_db.json"

def get_drive_service():
    if st.session_state.get('force_offline', False):
        return None
    try:
        info = st.secrets["gcs_connections"]
        credentials = service_account.Credentials.from_service_account_info(info)
        return build('drive', 'v3', credentials=credentials)
    except Exception as e:
        if not st.session_state.get('force_offline', False):
            st.error(f"שגיאה בהתחברות ל-Google Drive: {e}")
        return None

def save_custom_spot_to_db(city, spot):
    """שמירת נקודה אישית ב-Google Sheets בענן"""
    if st.session_state.get('force_offline', False):
        return
    client = get_sheets_client()
    if not client: return
    try:
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        all_values = sheet.get_all_values()
        
        # אם הגיליון ריק לחלוטין, ניצור כותרות
        if not all_values:
            sheet.append_row(["city", "name", "lat", "lng", "description"])
            all_values = [["city", "name", "lat", "lng", "description"]]
            
        # מניעת שמירת כפילויות
        for row in all_values[1:]:
            if len(row) >= 2 and row[0].strip() == city.strip() and row[1].strip() == spot['name'].strip():
                return 

        row_to_insert = [
            city,
            spot["name"],
            str(spot["coords"][0]), 
            str(spot["coords"][1]), 
            spot["description"]
        ]
        sheet.append_row(row_to_insert)
        st.toast("✔️ הנקודה נשמרה בהצלחה ב-Google Sheets!", icon="💾")
    except Exception as e:
        st.error(f"שגיאה בשמירה לטבלה: {e}")

def fetch_custom_spots(city):
    """משיכת נקודות אישיות מ-Google Sheets בצורה חסינה למגרעות טקסט/רווחים"""
    if st.session_state.get('force_offline', False):
        return []
    client = get_sheets_client()
    if not client: return []
    try:
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        all_values = sheet.get_all_values()
        
        if not all_values or len(all_values) <= 1:
            return []
            
        custom_spots = []
        target_city_clean = city.strip().lower()
        
        for row in all_values[1:]:
            if len(row) >= 4:
                row_city = row[0].strip().lower()
                if row_city in target_city_clean or target_city_clean in row_city:
                    try:
                        lat_val = float(row[2].strip())
                        lng_val = float(row[3].strip())
                        spot_name = row[1].strip()
                        desc_val = row[4].strip() if len(row) > 4 and row[4].strip() else f"<div style='direction: rtl; text-align: right;'><strong>{spot_name}</strong></div>"
                        
                        custom_spots.append({
                            "name": spot_name,
                            "coords": [lat_val, lng_val],
                            "description": desc_val,
                            "audio_text": f"הגעת אל {spot_name}",
                            "image_url": None
                        })
                    except (ValueError, TypeError):
                        continue
        return custom_spots
    except Exception as e:
        st.warning(f"לא ניתן היה למשוך נקודות מ-Google Sheets: {e}")
        return []

# --- פונקציית חישוב מרחק גיאוגרפי (Haversine formula) ---
def calculate_geodesic_distance(coord1, coord2):
    lat1, lon1 = coord1
    lat2, lon2 = coord2
    R = 6371.0 

    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def calculate_route_total_distance(coords_list):
    total_dist = 0.0
    for i in range(len(coords_list) - 1):
        total_dist += calculate_geodesic_distance(coords_list[i], coords_list[i+1])
    return total_dist

# --- פונקציית ניווט רחובות מול OSRM ---
def get_osrm_walking_segment(start_coords, end_coords):
    if st.session_state.get('force_offline', False):
        return [start_coords, end_coords]
        
    url = f"http://router.project-osrm.org/route/v1/foot/{start_coords[1]},{start_coords[0]};{end_coords[1]},{end_coords[0]}?overview=full&geometries=geojson"
    try:
        response = requests.get(url, timeout=3)
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "Ok" and data.get("routes"):
                geometry = data["routes"][0]["geometry"]["coordinates"]
                route_coords = [[lat, lng] for lng, lat in geometry]
                
                waypoints = data.get("waypoints", [])
                if len(waypoints) >= 2:
                    target_snap_lng, target_snap_lat = waypoints[1]["location"]
                    snap_coords = [target_snap_lat, target_snap_lng]
                    if math.sqrt((snap_coords[0]-end_coords[0])**2 + (snap_coords[1]-end_coords[1])**2) > 0.0001:
                        route_coords.append(end_coords)
                        
                return route_coords
    except Exception:
        pass
        
    return [start_coords, end_coords]

def get_full_street_route(route_coords_list):
    if len(route_coords_list) < 2:
        return route_coords_list
        
    cache_key = f"cached_route_{hash(str(route_coords_list))}"
    if cache_key in st.session_state:
        return st.session_state[cache_key]

    full_path = []
    for i in range(len(route_coords_list) - 1):
        segment = get_osrm_walking_segment(route_coords_list[i], route_coords_list[i+1])
        if full_path and segment:
            full_path.extend(segment[1:])
        else:
            full_path.extend(segment)
            
    st.session_state[cache_key] = full_path
    return full_path

# --- פונקציה לייצור קובץ KML ---
def generate_kml(city_name, route_spots):
    kml = ET.Element('kml', xmlns="http://www.opengis.net/kml/2.2")
    document = ET.SubElement(kml, 'Document')
    
    doc_name = ET.SubElement(document, 'name')
    doc_name.text = f"מסלול טיול ב-{city_name}"
    
    doc_desc = ET.SubElement(document, 'description')
    doc_desc.text = "מסלול שחושב ונבנה באמצעות מדריך הטיולים העירוני"

    line_coords = []
    for idx, spot in enumerate(route_spots, start=1):
        lat, lng = spot['coords']
        line_coords.append(f"{lng},{lat},0")
        
        placemark = ET.SubElement(document, 'Placemark')
        p_name = ET.SubElement(placemark, 'name')
        p_name.text = f"{idx}. {spot['name']}"
        
        p_desc = ET.SubElement(placemark, 'description')
        p_desc.text = f"תחנה {idx} במסלול"
        
        point = ET.SubElement(placemark, 'Point')
        coordinates = ET.SubElement(point, 'coordinates')
        coordinates.text = f"{lng},{lat},0"

    if len(line_coords) > 1:
        route_placemark = ET.SubElement(document, 'Placemark')
        r_name = ET.SubElement(route_placemark, 'name')
        r_name.text = "קו המסלול"
        
        line_string = ET.SubElement(route_placemark, 'LineString')
        tessellate = ET.SubElement(line_string, 'tessellate')
        tessellate.text = "1"
        
        route_coords_elem = ET.SubElement(line_string, 'coordinates')
        route_coords_elem.text = " ".join(line_coords)

    return ET.tostring(kml, encoding='utf-8', xml_declaration=True).decode('utf-8')

# --- פונקציות שמירה מקומית ב-LocalStorage ---
def save_spot_to_local_storage(city, spot):
    current_spots = st.session_state.custom_spots_dict.get(city, [])
    js_save_code = f"""
    <script>
        (function() {{
            try {{
                const updatedSpots = {json.dumps({city: current_spots})};
                const existingData = window.parent.localStorage.getItem('user_custom_spots');
                let fullData = existingData ? JSON.parse(existingData) : {{}};
                fullData['{city}'] = updatedSpots['{city}'];
                window.parent.localStorage.setItem('user_custom_spots', JSON.stringify(fullData));
            }} catch(e) {{
                console.error("Error saving spot to localStorage:", e);
            }}
        }})();
    </script>
    """
    components.html(js_save_code, height=0, width=0)

def save_optimized_route_to_local_storage(city, route_names):
    if not route_names:
        return
    js_route_code = f"""
    <script>
        setTimeout(() => {{
            try {{
                const routeData = {{
                    city: {json.dumps(city)},
                    route: {json.dumps(route_names)},
                    timestamp: new Date().getTime()
                }};
                window.parent.localStorage.setItem('user_optimized_route_{city}', JSON.stringify(routeData));
            }} catch(e) {{
                console.error("Error saving route:", e);
            }}
        }}, 100);
    </script>
    """
    components.html(js_route_code, height=0, width=0)

# --- מאגר נתונים רב-עירוני סטנדרטי ---
STANDARD_CITIES_DB = {
    "רומא, איטליה": {
        "main_spots": [
            {"name": "קולוסאום", "coords": [41.8902, 12.4922], "description": "<div style='direction: rtl; text-align: right;'><strong>קולוסאום</strong><p>האמפיתיאטרון הגדול בעולם.</p></div>", "audio_text": "ברוכים הבאים לקולוסאום", "image_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/de/Colosseo_2020.jpg/1024px-Colosseo_2020.jpg"},
            {"name": "הפורום הרומאי", "coords": [41.8925, 12.4853], "description": "<div style='direction: rtl; text-align: right;'><strong>הפורום הרומאי</strong><p>לב האימפריה העתיקה.</p></div>", "audio_text": "הפורום הרומאי", "image_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/62/Roman_Forum_-_panoramic_view.jpg/1024px-Roman_Forum_-_panoramic_view.jpg"},
            {"name": "מזרקת טרווי", "coords": [41.9009, 12.4833], "description": "<div style='direction: rtl; text-align: right;'><strong>מזרקת טרווי</strong><p>מזרקת הבארוק המפורסמת.</p></div>", "audio_text": "מזרקת טרווי", "image_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/c/c9/Trevi_Fountain%2C_Rome%2C_Italy.jpg/1024px-Trevi_Fountain%2C_Rome%2C_Italy.jpg"}
        ],
        "by_the_way": [
            {"name": "בית קפה - La Casa del Caffè", "type": "בית קפה", "coords": [41.8995, 12.4800]},
            {"name": "מסעדה - Pane e Salame", "type": "מסעדה", "coords": [41.9002, 12.4820]},
            {"name": "פארק - Colle Oppio", "type": "פארק", "coords": [41.8915, 12.4960]},
            {"name": "שוק - Mercato Campagna Amica", "type": "שוק", "coords": [41.8890, 12.4830]},
            {"name": "תחנת מטרו - Colosseo Station", "type": "תחנת מטרו", "coords": [41.8912, 12.4915]}
        ]
    },
    "בודפשט, הונגריה": {
        "main_spots": [
            {"name": "בניין הפרלמנט ההונגרי", "coords": [47.5071, 19.0405], "description": "<div style='direction: rtl; text-align: right;'><strong>בניין הפרלמנט ההונגרי</strong><p>מבנה ניאו-גותי מרשים על גדות נהר הדנובה.</p></div>", "audio_text": "ברוכים הבאים לבניין הפרלמנט ההונגרי", "image_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1c/V%C3%A1rosk%C3%A9p_a_Budai_V%C3%A1rb%C3%B3l_%28Orsz%C3%A1gh%C3%A1z%29.jpg/1024px-V%C3%A1rosk%C3%A9p_a_Budai_V%C3%A1rb%C3%B3l_%28Orsz%C3%A1gh%C3%A1z%29.jpg"},
            {"name": "טירת בודה", "coords": [47.4962, 19.0396], "description": "<div style='direction: rtl; text-align: right;'><strong>טירת בודה</strong><p>מתחם הארמון ההיסטורי של מלכי הונגריה.</p></div>", "audio_text": "טירת בודה, מתחם הארמון ההיסטורי המפואר.", "image_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/f/f7/Buda_Castle_and_Chain_Bridge_at_night.jpg/1024px-Buda_Castle_and_Chain_Bridge_at_night.jpg"},
            {"name": "באסטילת הדייגים", "coords": [47.5022, 19.0348], "description": "<div style='direction: rtl; text-align: right;'><strong>באסטילת הדייגים</strong><p>מבנה מרפסות מעוטר המציע תצפית פנורמית.</p></div>", "audio_text": "באסטילת הדייגים, נקודת התצפית הפנורמית המרהיבה.", "image_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/9/9f/Fisherman%27s_Bastion_at_night.jpg/1024px-Fisherman%27s_Bastion_at_night.jpg"}
        ],
        "by_the_way": [
            {"name": "בית קפה - Ruszwurm Cukrászda", "type": "בית קפה", "coords": [47.5015, 19.0325]},
            {"name": "מסעדה - Hunter's Restaurant", "type": "מסעדה", "coords": [47.5065, 19.0430]},
            {"name": "פארק - Elysium Park", "type": "פארק", "coords": [47.4980, 19.0370]},
            {"name": "שוק - Central Market Hall", "type": "שוק", "coords": [47.4870, 19.0585]},
            {"name": "תחנת מטרו - Batthyány tér Station", "type": "תחנת מטרו", "coords": [47.5060, 19.0390]}
        ]
    }
}

# --- אתחול משתני זיכרון (Session State) ---
if 'current_city' not in st.session_state: st.session_state.current_city = "בודפשט, הונגריה"
if 'city_error' not in st.session_state: st.session_state.city_error = False
if 'selected_spots_names' not in st.session_state: st.session_state.selected_spots_names = []
if 'selected_spot_name' not in st.session_state: st.session_state.selected_spot_name = ""
if 'is_optimized' not in st.session_state: st.session_state.is_optimized = False
if 'optimized_route_names' not in st.session_state: st.session_state.optimized_route_names = []
if 'mock_gps_location' not in st.session_state: st.session_state.mock_gps_location = "נמצא ביעד (רומא/בודפשט)"
if 'sim_running' not in st.session_state: st.session_state.sim_running = False
if 'work_mode' not in st.session_state: st.session_state.work_mode = "מצב תכנון"
if 'spots_combo_key' not in st.session_state: st.session_state.spots_combo_key = 0
if 'selected_by_the_way_types' not in st.session_state: st.session_state.selected_by_the_way_types = []
if 'custom_spots_dict' not in st.session_state: st.session_state.custom_spots_dict = {}
if 'last_processed_click' not in st.session_state: st.session_state.last_processed_click = None
if 'show_dialog_trigger' not in st.session_state: st.session_state.show_dialog_trigger = False
if 'user_live_location' not in st.session_state: st.session_state.user_live_location = [47.4980, 19.0400]
if 'user_heading' not in st.session_state: st.session_state.user_heading = 0
if 'force_offline' not in st.session_state: st.session_state.force_offline = False
if 'restored_from_storage' not in st.session_state: st.session_state.restored_from_storage = False

# =======================================================================
# 🌐 טעינה ראשונית מ-Google Sheets (לפני בניית הממשק וה-Multiselect!)
# =======================================================================
current_city = st.session_state.current_city

if current_city not in st.session_state.custom_spots_dict or not st.session_state.custom_spots_dict[current_city]:
    cloud_spots = fetch_custom_spots(current_city)
    if cloud_spots:
        st.session_state.custom_spots_dict[current_city] = cloud_spots

# =======================================================================
# 🌐 משיכת מיקום ה-GPS וכיוון המצפן מהדפדפן
# =======================================================================
js_geo_script = """
<script>
    navigator.geolocation.watchPosition(
        (position) => {
            const data = {
                lat: position.coords.latitude,
                lng: position.coords.longitude,
                heading: position.coords.heading
            };
            window.parent.postMessage({type: 'streamlit:setComponentValue', value: data}, '*');
        },
        (error) => {
            window.parent.postMessage({type: 'streamlit:setComponentValue', value: {error: error.message}}, '*');
        },
        { enableHighAccuracy: true, timeout: 5000, maximumAge: 0 }
    );

    window.addEventListener('deviceorientation', function(event) {
        let heading = null;
        if (event.webkitCompassHeading) {
            heading = event.webkitCompassHeading;
        } else if (event.alpha !== null) {
            heading = 360 - event.alpha;
        }

        if (heading !== null) {
            window.parent.postMessage({
                type: 'streamlit:setComponentValue', 
                value: { heading: Math.round(heading) }
            }, '*');
        }
    }, true);
</script>
"""
geo_data = components.html(js_geo_script, height=0, width=0)

if geo_data and isinstance(geo_data, dict):
    if "lat" in geo_data:
        st.session_state.user_live_location = [geo_data["lat"], geo_data["lng"]]
    if "heading" in geo_data and geo_data["heading"] is not None:
        st.session_state.user_heading = geo_data["heading"]

# =======================================================================
# 💾 מנגנון קריאה ושחזור אוטומטי מ-LocalStorage בטעינת הדף
# =======================================================================
curr_city_for_js = st.session_state.current_city

if not st.session_state.restored_from_storage:
    js_storage_script = f"""
    <script>
        (function() {{
            try {{
                const localCustomSpots = window.parent.localStorage.getItem('user_custom_spots');
                const customSpots = localCustomSpots ? JSON.parse(localCustomSpots) : null;

                const localRoute = window.parent.localStorage.getItem('user_optimized_route_{curr_city_for_js}');
                const routeData = localRoute ? JSON.parse(localRoute) : null;

                window.parent.postMessage({{
                    type: 'streamlit:setComponentValue', 
                    value: {{ 
                        custom_spots: customSpots,
                        saved_route: routeData,
                        is_loaded: true
                    }}
                }}, '*');
            }} catch(e) {{
                console.error("Error reading from local storage:", e);
            }}
        }})();
    </script>
    """
    storage_data = components.html(js_storage_script, height=0, width=0)

    if storage_data and isinstance(storage_data, dict) and storage_data.get("is_loaded"):
        need_rerun = False
        if "custom_spots" in storage_data and storage_data["custom_spots"]:
            loaded_spots = storage_data["custom_spots"]
            if isinstance(loaded_spots, dict):
                for city_name, spots_list in loaded_spots.items():
                    existing = st.session_state.custom_spots_dict.get(city_name, [])
                    existing_names = {s["name"] for s in existing}
                    for spot in spots_list:
                        if spot["name"] not in existing_names:
                            existing.append(spot)
                            existing_names.add(spot["name"])
                            need_rerun = True
                    st.session_state.custom_spots_dict[city_name] = existing

        if "saved_route" in storage_data and storage_data["saved_route"]:
            saved_r = storage_data["saved_route"]
            if isinstance(saved_r, dict) and saved_r.get("city") == curr_city_for_js and saved_r.get("route"):
                st.session_state.selected_spots_names = saved_r["route"]
                st.session_state.optimized_route_names = saved_r["route"]
                st.session_state.is_optimized = True
        
        st.session_state.restored_from_storage = True
        if need_rerun:
            st.rerun()

def handle_city_change():
    new_val = st.session_state.city_text_input_key
    if new_val in STANDARD_CITIES_DB:
        st.session_state.current_city = new_val
        st.session_state.city_error = False
        st.session_state.custom_spots_dict[new_val] = fetch_custom_spots(new_val)
    else:
        st.session_state.city_error = True
    st.session_state.selected_spots_names = []
    st.session_state.selected_spot_name = ""
    st.session_state.is_optimized = False
    st.session_state.optimized_route_names = []
    st.session_state.sim_running = False
    st.session_state.selected_by_the_way_types = []
    st.session_state.last_processed_click = None
    st.session_state.show_dialog_trigger = False
    st.session_state.restored_from_storage = False
    st.session_state.spots_combo_key += 1

# --- בניית בריכת הנקודות המלאה (סטנדרטיות + אישיות) ---
city_data = STANDARD_CITIES_DB.get(st.session_state.current_city, {"main_spots": [], "by_the_way": []})
standard_main_spots = city_data["main_spots"]
custom_main_spots = st.session_state.custom_spots_dict.get(st.session_state.current_city, [])

full_main_spots_pool = list(standard_main_spots)
seen_names = {s["name"] for s in standard_main_spots}
for spot in custom_main_spots:
    if spot["name"] not in seen_names:
        full_main_spots_pool.append(spot)
        seen_names.add(spot["name"])

active_btw_spots = city_data.get("by_the_way", [])

# --- סרגל צד (Sidebar) ---
with st.sidebar:
    st.header("⚙️ הגדרות טיול")
    
    force_offline = st.toggle("מצב אופליין יזום ✈️", value=st.session_state.force_offline, help="חוסם פניות לרשת ולענן ועובד אך ורק מזיכרון המכשיר")
    if force_offline != st.session_state.force_offline:
        st.session_state.force_offline = force_offline
        st.rerun()

    mock_gps = st.selectbox(
        "📍 סימולטור מיקום ה-GPS שלך:",
        options=["נמצא ביעד (רומא/בודפשט)", "מרוחק (ישראל)"],
        index=0 if st.session_state.mock_gps_location == "נמצא ביעד (רומא/בודפשט)" else 1
    )
    st.session_state.mock_gps_location = mock_gps

    if mock_gps == "מרוחק (ישראל)":
        user_coords = [32.0833, 34.8000]
    else:
        if st.session_state.current_city == "בודפשט, הונגריה":
            user_coords = [47.4980, 19.0400]
        else:
            user_coords = [41.8910, 12.4900]

    st.session_state.user_live_location = user_coords

    st.session_state.user_heading = st.slider(
        "🧩 סימולטור כיוון מצפן (מעלות):", 
        min_value=0, 
        max_value=360, 
        value=int(st.session_state.get('user_heading', 0))
    )

    distance_to_target = 0
    if standard_main_spots:
        distance_to_target = calculate_geodesic_distance(user_coords, standard_main_spots[0]["coords"])

    chosen_mode = st.radio("בחר תצורת עבודה:", options=["מצב תכנון", "מצב טיול"], key="work_mode")

    if chosen_mode == "מצב טיול" and distance_to_target > 10:
        st.error(f"🚨 המערכת זיהתה שאתה מרוחק מהיעד ({round(distance_to_target, 1)} ק\"מ).")
        st.warning("בשל המרחק מנקודת היעד, המערכת תעבוד בתצורת תכנון.")
        st.session_state.work_mode = "מצב תכנון"
        st.rerun()

    st.markdown("---")
    is_planning = (st.session_state.work_mode == "מצב תכנון")
    city_input_val = st.text_input(
        "לאן מטיילים? (עיר, מדינה):",
        value=st.session_state.current_city,
        disabled=not is_planning,
        key="city_text_input_key",
        on_change=handle_city_change
    )
    
    if is_planning:
        if st.button("טען יעד 🌍", use_container_width=True):
            handle_city_change()
            st.rerun()

    if st.session_state.city_error and is_planning:
        st.error("⚠️ היעד לא זוהה! נסה 'רומא, איטליה' או 'בודפשט, הונגריה'.")

    if is_planning:
        st.markdown("---")
        with st.expander("➕ הוסף נקודת עניין אישית"):
            custom_name = st.text_input("שם המקום האישי:", placeholder="למשל: המלון שלי")
            custom_address = st.text_input("כתובת:", placeholder="רחוב ומספר")
            
            if st.button("שמור נקודה אישית 💾", use_container_width=True):
                if custom_name and custom_address:
                    if any(s["name"] == custom_name for s in full_main_spots_pool):
                        st.error("❌ שם זה כבר קיים במאגר נקודות העניין!")
                    else:
                        simulated_lat, simulated_lng = 47.4985, 19.0410
                        if not st.session_state.force_offline:
                            try:
                                geolocator = Nominatim(user_agent="travel_app_sharon")
                                full_search_query = f"{custom_address}, {st.session_state.current_city}"
                                location = geolocator.geocode(full_search_query)
                                if location:
                                    simulated_lat = location.latitude
                                    simulated_lng = location.longitude
                            except Exception:
                                pass
                        
                        new_spot = {
                            "name": custom_name,
                            "coords": [simulated_lat, simulated_lng],
                            "description": f"<div style='direction: rtl; text-align: right;'><strong>{custom_name}</strong><p>נקודה אישית בכתובת: {custom_address}</p></div>",
                            "audio_text": f"הגעת אל {custom_name}",
                            "image_url": None
                        }
                        
                        st.session_state.custom_spots_dict[st.session_state.current_city].append(new_spot)
                        save_spot_to_local_storage(st.session_state.current_city, new_spot)
                        save_custom_spot_to_db(st.session_state.current_city, new_spot)
                        
                        st.session_state.selected_spots_names.append(custom_name)
                        st.success(f"✔️ הנקודה '{custom_name}' אותרה ונשמרה!")
                        st.rerun()

    st.markdown("---")
    st.subheader("📍 נקודות עניין ביעד (משולב)")
    
    options_list = [s["name"] for s in full_main_spots_pool]
    valid_defaults = [n for n in st.session_state.selected_spots_names if n in options_list]

    selected_spots = st.multiselect(
        "בחר אתרים למסלול:",
        options=options_list,
        default=valid_defaults,
        max_selections=5,
        key=f"spots_multiselect_uid_{st.session_state.spots_combo_key}"
    )
    
    if selected_spots != st.session_state.selected_spots_names:
        st.session_state.selected_spots_names = selected_spots
        st.session_state.is_optimized = False
        st.session_state.sim_running = False
        if len(selected_spots) < 2:
            st.session_state.selected_by_the_way_types = []
        if selected_spots and st.session_state.selected_spot_name not in selected_spots:
            st.session_state.selected_spot_name = selected_spots[0]
        st.rerun()

    st.markdown("---")
    st.subheader("☕ נקודות על הדרך")
    by_the_way_options = ["בית קפה", "מסעדה", "פארק", "שוק", "תחנת מטרו"]
    
    if st.session_state.work_mode == "מצב טיול":
        is_btw_disabled = len(st.session_state.selected_spots_names) < 1
    else:
        is_btw_disabled = len(st.session_state.selected_spots_names) < 2
        
    btw_default = [] if is_btw_disabled else st.session_state.selected_by_the_way_types

    selected_btw = st.multiselect(
        "הצג קטגוריות על המפה:",
        options=by_the_way_options,
        default=btw_default,
        disabled=is_btw_disabled,
        key=f"btw_multiselect_fixed_v4_{st.session_state.spots_combo_key}_{is_btw_disabled}"
    )
    st.session_state.selected_by_the_way_types = selected_btw

    if len(st.session_state.selected_spots_names) > 1:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("חשב מסלול אופטימלי 🏃‍♂️", use_container_width=True):
            st.session_state.optimized_route_names = st.session_state.selected_spots_names
            st.session_state.is_optimized = True
            st.session_state.sim_running = False
            st.rerun()

    if st.session_state.is_optimized:
        st.markdown("---")
        if st.session_state.sim_running:
            if st.button("עצור סימולציה ⏹️", use_container_width=True, type="primary"):
                st.session_state.sim_running = False
                st.rerun()
        else:
            if st.button("התחל סימולציית הליכה 🚶‍♂️", use_container_width=True):
                st.session_state.sim_running = True
                st.rerun()

    # =======================================================================
    # 🗺️ כפתור ייצוא המסלול לקובץ KML
    # =======================================================================
    if st.session_state.is_optimized and st.session_state.optimized_route_names:
        st.markdown("---")
        st.subheader("📤 ייצוא מסלול")
        
        active_route_spots = [
            next(s for s in full_main_spots_pool if s["name"] == name)
            for name in st.session_state.optimized_route_names
            if any(s["name"] == name for s in full_main_spots_pool)
        ]
        
        kml_string = generate_kml(st.session_state.current_city, active_route_spots)
        city_file_slug = st.session_state.current_city.split(',')[0].strip()
        
        st.download_button(
            label=f"🗺️ הורד קובץ KML ל-Google Maps",
            data=kml_string,
            file_name=f"route_{city_file_slug}.kml",
            mime="application/vnd.google-earth.kml+xml",
            use_container_width=True
        )

    # =======================================================================
    # 📥 כפתור הורדת חבילת אופליין לעיר הפעילה
    # =======================================================================
    st.markdown("---")
    st.subheader("✈️ הכנה לאופליין")
    
    current_download_city = st.session_state.current_city
    
    if st.button(f"📥 הורד חבילת אופליין ל-{current_download_city.split(',')[0]}", use_container_width=True):
        city_spots = full_main_spots_pool
        image_urls = [s["image_url"] for s in city_spots if "image_url" in s and s["image_url"]]
        
        js_cache_download_script = f"""
        <script>
            (async function() {{
                const cacheName = 'city-offline-{current_download_city}';
                const cache = await caches.open(cacheName);
                const urlsToCache = {json.dumps(image_urls)};
                
                window.parent.localStorage.setItem('offline_city_data_{current_download_city}', JSON.stringify({json.dumps(city_spots)}));
                
                let savedCount = 0;
                for (const url of urlsToCache) {{
                    try {{
                        const response = await fetch(url, {{ mode: 'no-cors' }});
                        await cache.put(url, response);
                        savedCount++;
                    }} catch (e) {{
                        console.warn("Could not cache image:", url, e);
                    }}
                }}
                
                alert("✔️ החבילה עבור {current_download_city} מוכנה! (" + savedCount + "/" + urlsToCache.length + " תמונות נשמרו ב-Cache)");
            }})();
        </script>
        """
        components.html(js_cache_download_script, height=0, width=0)
        st.success(f"הורדת חבילת אופליין עבור {current_download_city} הופעלה בדפדפן!")

# --- כותרת ראשית ---
st.title("מדריך טיולים עירוני - MVP")

if st.session_state.force_offline:
    st.warning("✈️ **מצב אופליין יזום פעיל:** פניות לרשת ולענן חסומות. האפליקציה פועלת מתוך זיכרון המכשיר בלבד.")

st.subheader(f"🌐 יעד פעיל: {st.session_state.current_city} | 🛠️ תצורת עבודה: {st.session_state.work_mode}")

# --- רכיב המפה ---
def render_map_section(full_main_spots):
    route_names = st.session_state.optimized_route_names if st.session_state.is_optimized else [n for n in st.session_state.selected_spots_names if n in [s["name"] for s in full_main_spots]]
    route_data = [next(s for s in full_main_spots if s["name"] == n) for n in route_names if any(s["name"] == n for s in full_main_spots)]
    route_coords = [s["coords"] for s in route_data]
    
    center_coords = full_main_spots[0]["coords"] if full_main_spots else [0,0]
    if st.session_state.selected_spot_name and any(s["name"] == st.session_state.selected_spot_name for s in full_main_spots):
        current_spot = next((s for s in full_main_spots if s["name"] == st.session_state.selected_spot_name), None)
        if current_spot: center_coords = current_spot["coords"]

    info_container = st.empty()
    map_container = st.empty()

    def add_by_the_way_markers(map_obj):
        for btw_spot in active_btw_spots:
            if btw_spot["type"] in st.session_state.selected_by_the_way_types:
                folium.Marker(
                    location=btw_spot["coords"],
                    tooltip=f"({btw_spot['type']}) {btw_spot['name']}",
                    icon=folium.Icon(color="orange", icon="info-sign")
                ).add_to(map_obj)

    m_normal = folium.Map(location=center_coords, zoom_start=14)
    
    if st.session_state.user_live_location and st.session_state.work_mode == "מצב טיול":
        heading_deg = st.session_state.get('user_heading', 0)
        
        arrow_html = f"""
        <div style="
            transform: rotate({heading_deg}deg);
            width: 32px;
            height: 32px;
            display: flex;
            align-items: center;
            justify-content: center;
            filter: drop-shadow(0px 2px 4px rgba(0,0,0,0.4));
        ">
            <svg width="30" height="30" viewBox="0 0 24 24" fill="#1E88E5" stroke="#FFFFFF" stroke-width="2">
                <polygon points="12 2 19 21 12 17 5 21 12 2"/>
            </svg>
        </div>
        """
        
        folium.Marker(
            location=st.session_state.user_live_location,
            popup=f"מיקום נוכחי (כיוון: {heading_deg}°)",
            icon=folium.DivIcon(
                html=arrow_html,
                icon_size=(32, 32),
                icon_anchor=(16, 16)
            )
        ).add_to(m_normal)

    # 📊 חישוב והצגת מידע מרחקים על המסלול
    if st.session_state.work_mode == "מצב טיול" and len(route_data) == 1 and st.session_state.user_live_location:
        start_coords = st.session_state.user_live_location
        end_coords = route_data[0]["coords"]
        
        dist_km = calculate_geodesic_distance(start_coords, end_coords)
        est_walk_time_min = int(round((dist_km / 4.5) * 60))
        
        street_route = get_osrm_walking_segment(start_coords, end_coords)
        folium.PolyLine(locations=street_route, color="red", weight=5, dash_array="5, 10").add_to(m_normal)
        folium.Marker(end_coords, tooltip=route_data[0]["name"]).add_to(m_normal)
        
        info_container.info(f"🗺️ **מסלול ניווט ממיקומך אל {route_data[0]['name']}** | 📏 מרחק: **{round(dist_km, 2)} ק\"מ** | ⏱️ זמן הליכה משוער: **~{est_walk_time_min} דקות**")
    else:
        if route_coords:
            total_dist_km = calculate_route_total_distance(route_coords)
            total_walk_time_min = int(round((total_dist_km / 4.5) * 60))
            
            street_route = get_full_street_route(route_coords)
            folium.PolyLine(locations=street_route, color="green" if st.session_state.is_optimized else "red", weight=5).add_to(m_normal)
            for idx, s in enumerate(route_data):
                if st.session_state.is_optimized:
                    html = f"""<div style="font-size: 12px; color: white; background-color: green; border-radius: 50%; width: 22px; height: 22px; text-align: center; line-height: 22px; font-weight: bold; border: 2px solid white;">{idx+1}</div>"""
                    folium.Marker(location=s["coords"], tooltip=s["name"], icon=folium.DivIcon(html=html, icon_size=(22,22), icon_anchor=(11,11))).add_to(m_normal)
                else:
                    folium.Marker(s["coords"], tooltip=s["name"]).add_to(m_normal)
            
            info_container.success(f"📊 **נתוני מסלול הטיול:** 📏 מרחק כולל: **{round(total_dist_km, 2)} ק\"מ** | ⏱️ זמן הליכה משוער: **~{total_walk_time_min} דקות** ({len(route_data)} תחנות)")

    add_by_the_way_markers(m_normal)
    
    with map_container:
        map_output = st_folium(m_normal, width=700, height=450, key="multi_city_map", returned_objects=["last_clicked"])
        
        if is_planning and map_output and map_output.get("last_clicked"):
            click_event = map_output["last_clicked"]
            if click_event != st.session_state.last_processed_click:
                st.session_state.last_processed_click = click_event
                st.session_state.last_clicked_coords = [click_event["lat"], click_event["lng"]]
                st.session_state.show_dialog_trigger = True
                st.rerun()

render_map_section(full_main_spots_pool)

# --- הוספת נקודה אישית מהמפה ---
@st.dialog("🗺️ הוספת נקודה אישית חדשה")
def show_add_spot_dialog(coords):
    st.write(f"מיקום גיאוגרפי נבחר: `{coords}`")
    click_name = st.text_input("שם הנקודה האישית:", placeholder="למשל: המלון שלי")
    
    col1, col2 = st.columns(2)
    with col1:
        submitted = st.button("שמור והוסף ✨", use_container_width=True)
    with col2:
        canceled = st.button("ביטול ❌", use_container_width=True)
        
    if submitted and click_name:
        if any(s["name"] == click_name for s in full_main_spots_pool):
            st.error("❌ שם זה כבר קיים במאגר!")
        else:
            new_map_spot = {
                "name": click_name,
                "coords": coords,
                "description": f"<div style='direction: rtl; text-align: right;'><strong>{click_name}</strong><p>נקודה אישית שנבחרה ישירות מתוך המפה.</p></div>",
                "audio_text": f"הגעת אל {click_name}",
                "image_url": None
            }
            st.session_state.custom_spots_dict[st.session_state.current_city].append(new_map_spot)
            save_spot_to_local_storage(st.session_state.current_city, new_map_spot)
            save_custom_spot_to_db(st.session_state.current_city, new_map_spot)
            st.session_state.selected_spots_names.append(click_name)
            
            st.session_state.show_dialog_trigger = False
            st.rerun()
            
    if canceled:
        st.session_state.show_dialog_trigger = False
        st.rerun()

if is_planning and st.session_state.show_dialog_trigger and 'last_clicked_coords' in st.session_state:
    show_add_spot_dialog(st.session_state.last_clicked_coords)

# --- הצגת התוכן והמדריך הקולי ---
if full_main_spots_pool and st.session_state.selected_spot_name:
    selected_spot_data = next((spot for spot in full_main_spots_pool if spot["name"] == st.session_state.selected_spot_name), None)
    if selected_spot_data:
        st.markdown("<hr>", unsafe_allow_html=True)
        st.markdown(f"<h2 style='direction: rtl; text-align: right;'>{selected_spot_data['name']}</h2>", unsafe_allow_html=True)
        st.markdown(selected_spot_data["description"], unsafe_allow_html=True)
        
        img_url = selected_spot_data.get("image_url")
        if img_url and str(img_url).strip():
            st.image(img_url, caption=selected_spot_data["name"], width=600)

        custom_audio_button_html = f"""
        <div style="direction: rtl; text-align: right;">
        <button id="audioGuideButton" style="background-color: #4CAF50; border: none; color: white; padding: 10px 20px; font-size: 16px; cursor: pointer; border-radius: 8px;">
            השמע מדריך קולי ל{selected_spot_data['name']} 🔊
        </button>
        </div>
        <script>
            const audioButton = document.getElementById('audioGuideButton');
            const textToSpeak = {json.dumps(selected_spot_data['audio_text'])};
            audioButton.addEventListener('click', () => {{
                if ('speechSynthesis' in window) {{
                    window.speechSynthesis.cancel(); 
                    var utterance = new SpeechSynthesisUtterance(textToSpeak);
                    utterance.lang = 'he-IL';
                    window.speechSynthesis.speak(utterance);
                }}
            }});
        </script>
        """
        components.html(custom_audio_button_html, height=80, width=250)

# 💾 שמירת המסלול ב-localStorage בסוף טעינת הדף
if st.session_state.is_optimized and st.session_state.optimized_route_names:
    save_optimized_route_to_local_storage(
        st.session_state.current_city, 
        st.session_state.optimized_route_names
    )
