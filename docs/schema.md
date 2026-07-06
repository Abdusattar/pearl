# Схема БД — Жемчужина

> Версия: 1.0 | Дата: 2026-06-01
> Аудит: мудрец (Qwen Max) + Claude Code
> Стек: PostgreSQL + Alembic (миграции)

---

## Принципы

1. **Масштабируемость без переделки** — новый модуль = новая таблица
2. **Иерархия организаций** — `parent_id` в organizations: Жемчужина → Школа / Садики → Сокулук / Кожомкул
3. **Единая таблица транзакций** — приход и расход в одной таблице, `type` разделяет
4. **Квитанции независимо от учёта** — OCR → подтверждение человеком → только тогда транзакция
5. **Multi-tenant через `organization_id`** — везде, собственник видит всё
6. **Soft delete** — `deleted_at` вместо физического удаления
7. **Аудит лог** — кто/что/когда изменил

---

## Иерархия организаций

```
Жемчужина (id=1)
├── Школа (id=2)
└── Садики (id=3)
    ├── Садик Сокулук (id=4)
    └── Садик Кожомкул (id=5)
```

---

## Таблицы

### organizations
```sql
CREATE TABLE organizations (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    parent_id   INTEGER REFERENCES organizations(id),
    type        org_type NOT NULL,  -- school | kindergarten | group
    created_at  TIMESTAMP DEFAULT now()
);
```

### users
```sql
CREATE TABLE users (
    id              SERIAL PRIMARY KEY,
    tg_id           BIGINT UNIQUE,
    name            VARCHAR(100) NOT NULL,
    role            user_role NOT NULL,  -- owner | director | manager | staff
    organization_id INTEGER REFERENCES organizations(id),
    created_at      TIMESTAMP DEFAULT now(),
    deleted_at      TIMESTAMP
);
-- role=owner → organization_id=1 (Жемчужина), видит всё
```

### groups
```sql
CREATE TABLE groups (
    id              SERIAL PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    name            VARCHAR(50) NOT NULL,   -- "1А", "Солнышко"
    type            VARCHAR(20) NOT NULL,   -- class | kindergarten_group
    updated_at      TIMESTAMP DEFAULT now(),
    deleted_at      TIMESTAMP
);
```

### students
```sql
CREATE TABLE students (
    id              SERIAL PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    name            VARCHAR(100) NOT NULL,
    pin             VARCHAR(20) UNIQUE NOT NULL,  -- формат ДДММГГГН
    parent_contact  VARCHAR(200),                 -- телефон/Telegram (временно, фаза 2 → таблица parents)
    status          VARCHAR(10) DEFAULT 'active', -- active | inactive
    extra           JSONB,                        -- аллергии, группа здоровья и т.д.
    created_at      TIMESTAMP DEFAULT now(),
    updated_at      TIMESTAMP DEFAULT now(),
    deleted_at      TIMESTAMP
);
```

### enrollments
```sql
-- История принадлежности ученика к классу/группе
CREATE TABLE enrollments (
    id          SERIAL PRIMARY KEY,
    student_id  INTEGER NOT NULL REFERENCES students(id),
    group_id    INTEGER NOT NULL REFERENCES groups(id),
    start_date  DATE NOT NULL,
    end_date    DATE,  -- NULL = текущий
    created_at  TIMESTAMP DEFAULT now(),
    CONSTRAINT no_overlap CHECK (end_date IS NULL OR end_date > start_date)
);
CREATE INDEX ON enrollments(student_id);
```

### expense_categories
```sql
CREATE TABLE expense_categories (
    id              SERIAL PRIMARY KEY,
    organization_id INTEGER REFERENCES organizations(id),  -- NULL = глобальная (для всех)
    name            VARCHAR(100) NOT NULL,
    parent_id       INTEGER REFERENCES expense_categories(id),
    created_at      TIMESTAMP DEFAULT now()
);
-- Глобальные (organization_id IS NULL): Питание, Хозтовары, Зарплаты, Канцелярия
-- Локальные (organization_id задан): переопределения под конкретный объект
```

### receipts
```sql
CREATE TABLE receipts (
    id               SERIAL PRIMARY KEY,
    organization_id  INTEGER NOT NULL REFERENCES organizations(id),
    file_path        VARCHAR(500) NOT NULL,
    ocr_raw          TEXT,                 -- сырой текст от Google Cloud Vision
    ocr_status       ocr_status_type NOT NULL DEFAULT 'pending',
                     -- pending | processing | processed | confirmed | rejected
    amount_detected  NUMERIC(12,2),        -- сумма из OCR
    amount_confirmed NUMERIC(12,2),        -- сумма после ручной проверки
    confirmed_by     INTEGER REFERENCES users(id),
    confirmed_at     TIMESTAMP,
    created_by       INTEGER REFERENCES users(id),
    created_at       TIMESTAMP DEFAULT now()
);
```

### receipt_transactions *(связь many-to-many)*
```sql
-- Один чек может покрывать несколько статей расхода
CREATE TABLE receipt_transactions (
    receipt_id     INTEGER NOT NULL REFERENCES receipts(id),
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    amount         NUMERIC(12,2) NOT NULL,  -- сколько из этого чека на эту транзакцию
    PRIMARY KEY (receipt_id, transaction_id)
);
```

### transactions
```sql
CREATE TABLE transactions (
    id              SERIAL PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    type            tx_type NOT NULL,   -- income | expense
    amount          NUMERIC(12,2) NOT NULL,
    category_id     INTEGER REFERENCES expense_categories(id),
    student_id      INTEGER REFERENCES students(id),  -- для оплат (income)
    description     TEXT,
    date            DATE NOT NULL,
    created_by      INTEGER REFERENCES users(id),
    created_at      TIMESTAMP DEFAULT now(),
    updated_at      TIMESTAMP DEFAULT now(),
    deleted_at      TIMESTAMP,
    CONSTRAINT income_needs_student
        CHECK (type = 'expense' OR student_id IS NOT NULL)
);
CREATE INDEX ON transactions(organization_id, date DESC);
CREATE INDEX ON transactions(student_id);
CREATE INDEX ON transactions(category_id);
```

### audit_log
```sql
CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    entity_type VARCHAR(50) NOT NULL,   -- 'transaction' | 'receipt' | 'student' ...
    entity_id   INTEGER NOT NULL,
    action      VARCHAR(20) NOT NULL,   -- insert | update | delete
    user_id     INTEGER REFERENCES users(id),
    old_data    JSONB,
    new_data    JSONB,
    created_at  TIMESTAMP DEFAULT now()
);
CREATE INDEX ON audit_log(entity_type, entity_id);
```

---

## ENUM типы
```sql
CREATE TYPE org_type       AS ENUM ('root', 'school', 'kindergarten', 'group');
CREATE TYPE user_role      AS ENUM ('owner', 'director', 'manager', 'staff');
CREATE TYPE tx_type        AS ENUM ('income', 'expense');
CREATE TYPE ocr_status_type AS ENUM ('pending', 'processing', 'processed', 'confirmed', 'rejected');
```

---

## Глобальные категории расходов (seed data)
```
Питание
├── Продукты
└── Готовая еда / доставка
Хозяйство
├── Бытовая химия
└── Инвентарь
Канцелярия
Зарплаты
Коммунальные
Транспорт
Прочее
```

---

## Матрица доступа (зафиксировано 02.06.2026)

| Роль | Финансы | Ученики | Посещ. | Группы | Объекты |
|---|---|---|---|---|---|
| **owner** (собственник) | Все + дашборд → drill-down | Все | Все | Просмотр | Вся Жемчужина |
| **director** (Айжан) | Школа + **просмотр садиков** | Школа | Школа | Школа (создаёт/ведёт) | Школа + просмотр садиков |
| **manager** (Мунара) | Садики (Сокулук + Кожомкул) | Садики | Садики | Садики (создаёт/ведёт) | Только садики |
| **staff** (сотрудник объекта — закуп и т.п.) | Свой объект | Свой класс/группа | Свой класс/группа | — | Свой объект |

**Ввод расходов:**
- Школьные расходы → Айжан (и при необходимости другой уполномоченный)
- Расходы садиков → Мунара
- Реализация: фильтрация через `WHERE organization_id IN (...)` по роли пользователя

**UIX решения:**
- Переключатель объекта — **глобальный в topbar** (выбрал один раз, весь интерфейс перестраивается)
- Мунара в topbar видит только: Садики / Сокулук / Кожомкул
- Айжан в topbar видит: Школа + просмотр садиков
- Собственник — дашборд с итогами + возможность провалиться в детали
- Тема — **светлая**

**Общие закупки на оба садика (решено 02.06):**
- Бывают. Решение: `organization_id = Садики (id=3)` — родительский узел
- В UIX при загрузке квитанции Мунара выбирает "Сокулук / Кожомкул / Оба садика"
- При подтверждении с выбором "Оба садика" → появляется сплиттер: делится поровну или вручную
- Создаются две транзакции: одна на Сокулук, одна на Кожомкул
- Схема не меняется — иерархия organizations уже поддерживает это

---

## Что откладываем (фаза 2)
- `parents` + `parent_student` — нужно для Telegram уведомлений родителям
- RLS (Row Level Security) — сейчас фильтрация через WHERE в запросах
- `attendance` — посещаемость
- `payroll` — зарплатный модуль
