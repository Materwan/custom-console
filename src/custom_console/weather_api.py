import requests
from datetime import datetime
import time

# Fonction pour récupérer la météo via l'API OpenMeteo
def get_weather(lat, lon, city_name, units="metric", timezone="auto"):
    """
    Récupère la météo pour des coordonnées données.
    
    Parameters:
    - lat (float): Latitudes
    - lon (float): Longitudes
    - city_name (str): Nom de la ville pour l'affichage
    - units (str, optional): "metric" ou "imperial"
    - timezone (str, optional): Formatage du fuseau horaire
    
    Returns:
    - dict: Données météo
    """
    # URL de base de l'API OpenMeteo
    url = "https://api.open-meteo.com/v1/forecast"
    
    params = {
        "latitude": lat,
        "longitude": lon,
        "current_weather_units": units,
        "timezone": timezone,
        "current": "true"  # Assure que les données actuelles sont retournées
    }
    
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        
        data = response.json()
        
        # Vérifier si la réponse contient les données attendues
        if "current_weather" in data:
            current_weather = data["current_weather"]
            
            weather_info = {
                "city": city_name,
                "temperature": current_weather["temperature"],
                "weather_code": current_weather["weathercode"],
                "wind_speed": current_weather["windspeed"],
                "wind_direction": current_weather["winddirection"],
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "api_source": "OpenMeteo"
            }
            
            return weather_info
        
        else:
            print(f"Erreur: 'current_weather' non trouvé dans la réponse. Données récupérées : {data}")
            return None
            
    except requests.exceptions.RequestException as e:
        print(f"Erreur lors de la requête: {e}")
        return None

# Code pour afficher le code météo WMO
def get_weather_description(code):
    """Traduit et affiche la description du code météo WMO"""
    descriptions = {
        0: "Ciel dégagé",
        1: "Ciel partiellement nuageux",
        2: "Nuages en développement",
        3: "Nuages lourds",
        45: "Brouillard",
        48: "Brouillard épais",
        51: "Givres gelées légères",
        53: "Givres gelées modérées",
        55: "Givres gelées fortes",
        61: "Pluie légère",
        63: "Pluie modérée",
        65: "Pluie forte",
        71: "Neige légère",
        73: "Neige modérée",
        75: "Neige forte",
        80: "Averse légère",
        81: "Averse modérée",
        82: "Averse forte",
        95: "Grêle léger",
        96: "Grêle modéré",
    }
    
    return descriptions.get(code, f"Code WMO inconnu: {code}")

# Exécution du script - récupérer la météo actuelle
def get_current_weather_coordinates():
    """Simule la récupération des coordonnées (dans un cas réel, utiliser une API de géolocalisation)"""
    # Pour cet exemple, utilisons les coordonnées d'un lieu précis
    # Vous pouvez modifier selon vos besoins
    latitude = 48.8566
    longitude = 2.3522
    return (latitude, longitude)

# Récupération de la météo locale
coordinates = get_current_weather_coordinates()
weather_data = get_weather(
    lat=coordinates[0],
    lon=coordinates[1],
    city_name="Paris",
    units="metric"
)

if weather_data:
    print(f"\n{'='*60}")
    print(f"🌤️  Météo actuelle à {weather_data['city']}")
    print(f"{'='*60}")
    print(f"⚠️   Température : {weather_data['temperature']}°C")
    print(f"🌬️   Vitesse du vent : {weather_data['wind_speed']} m/s")
    print(f"💨 Direction du vent : {weather_data['wind_direction']}°")
    print(f"🕒 Heure actuelle : {weather_data['time']}")
    print(f"💡 Code météo WMO : {weather_data['weather_code']}")
    
    description = get_weather_description(weather_data['weather_code'])
    print(f"💬 Description : {description}")
    
    print(f"\n{'='*60}")
else:
    print("❌ Impossible de récupérer les données météo.")
    print("Veuillez vérifier vos coordonnées ou l'API OpenMeteo.")
