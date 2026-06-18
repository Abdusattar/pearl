"""Создать pearl_test БД. Запуск: python scripts/create_test_db.py"""
import subprocess, sys

# Пробуем через pg_ctl / psql окружение
result = subprocess.run(
    ["pg_isready", "-h", "localhost", "-U", "pearl"],
    capture_output=True, text=True
)
print("pg_isready:", result.stdout.strip() or result.stderr.strip())

import psycopg2

# Пробуем подключиться к postgres как pearl (может хватить прав)
for dsn in [
    "postgresql://pearl:pearl_dev@localhost:5432/postgres",
    "host=localhost dbname=postgres user=pearl password=pearl_dev",
]:
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", ("pearl_test",))
        if cur.fetchone():
            print("pearl_test уже существует")
        else:
            # Попытка создать — нужна роль CREATEDB
            cur.execute("ALTER USER pearl CREATEDB")
            cur.execute("CREATE DATABASE pearl_test OWNER pearl")
            print("pearl_test создана!")
        conn.close()
        sys.exit(0)
    except Exception as e:
        print(f"  Ошибка ({dsn[:30]}...): {e}")
        continue

print("Не удалось создать pearl_test автоматически.")
print("Запусти вручную в pgAdmin или psql:")
print("  ALTER USER pearl CREATEDB;")
print("  CREATE DATABASE pearl_test OWNER pearl;")
