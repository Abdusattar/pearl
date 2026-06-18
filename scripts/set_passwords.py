"""
Установка начальных паролей пользователей.
Запуск: python scripts/set_passwords.py
Идемпотентен — можно запускать повторно для смены пароля.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import bcrypt
from app.database import SessionLocal
from app.models import User

PASSWORDS = {
    "Абдусаттар": "000",
    "Айжан":      "001",
    "Мунара":     "002",
}


def main():
    db = SessionLocal()
    try:
        for name, password in PASSWORDS.items():
            user = db.query(User).filter(User.name == name).first()
            if not user:
                print(f"  ! Пользователь не найден: {name}")
                continue
            user.password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            print(f"  ✓ {name} → пароль установлен")
        db.commit()
        print("\nГотово.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
