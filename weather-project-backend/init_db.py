from app import SUPPORTED_CITIES


if __name__ == "__main__":
    print("Локальная инициализация базы больше не нужна.")
    print("Сервис получает живые данные из Open-Meteo для городов:")
    for city in SUPPORTED_CITIES:
        print(f"- {city['name']}, {city['country']}")
