"""
Добавление нового пользователя.
Запуск: railway run python scripts/add_user.py
Идемпотентен по имени — если пользователь уже есть, обновляет роль/орг/пароль.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import bcrypt
from app.database import SessionLocal
from app.models import User

NEW_USERS = [
    {"name": "Махабат", "role": "staff", "organization_id": 4, "password": "003"},
    {"name": "Айдай", "role": "founder", "organization_id": 4, "password": "004"},
    {"name": "Талас", "role": "owner", "organization_id": 4, "password": "005"},
]


def main():
    db = SessionLocal()
    try:
        for spec in NEW_USERS:
            user = db.query(User).filter(User.name == spec["name"]).first()
            if not user:
                user = User(name=spec["name"])
                db.add(user)
                print(f"  + создан пользователь {spec['name']}")
            user.role = spec["role"]
            user.organization_id = spec["organization_id"]
            user.password_hash = bcrypt.hashpw(spec["password"].encode(), bcrypt.gensalt()).decode()
            print(f"  OK {spec['name']} -> role={spec['role']} org_id={spec['organization_id']} пароль установлен")
        db.commit()
        print("\nГотово.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
