from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "Webdom Monitor"
    DATABASE_URL: str = "sqlite:///./webdom_monitor.db"
    SECRET_KEY: str = "your-secret-key-change-this-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    CORS_ORIGINS: List[str] = ["http://localhost:5173", "http://localhost:3000"]

    SMTP_SERVER: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = "webdomreports@gmail.com"
    SMTP_PASSWORD: str = ""

    SCAN_INTERVAL_SECONDS: int = 300
    SCAN_RETRIES: int = 5
    ALARM_REMINDER_DAYS: int = 7

    MODBUS_PORT: int = 502
    # Unit ID usado por el control avanzado (multiGW usa 0 para hablar con el gateway)
    GW_CONTROL_UNIT_ID: int = 0
    MODBUS_TIMEOUT: float = 5.0

    VPN_EXECUTABLE_OPENVPN: str = "C:\\Program Files\\OpenVPN\\bin\\openvpn.exe"
    VPN_EXECUTABLE_FORTICLIENT: str = "C:\\Program Files\\Fortinet\\FortiClient\\FortiClient.exe"

    DEMO_MODE: bool = False

settings = Settings()
