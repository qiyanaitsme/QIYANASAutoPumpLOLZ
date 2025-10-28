import json
from pathlib import Path
from dataclasses import dataclass


@dataclass
class Config:
    bot_token: str
    api_base_url: str
    api_token: str
    db_path: str
    bump_interval_hours: float
    
    @classmethod
    def load(cls, config_path: str = "config.json") -> "Config":
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        return cls(
            bot_token=data["bot"]["api_token"],
            api_base_url=data["api"]["base_url"],
            api_token=data["api"]["auth_token"],
            db_path=data["database"]["path"],
            bump_interval_hours=data["scheduling"]["bump_interval_hours"]
        )
