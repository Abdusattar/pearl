"""Сколько сегодня едят (23.09, склад по нормам).

Три числа в день — школа, садик, персонал — и строка «что готовят». Главная дверь —
чат: Махабат пишет одной строкой, бот записывает. Экран — та же запись, чтобы
поправить или внести пропущенный день. Норма продукта потом = расход между
пересчётами ÷ едоки за эти дни, поэтому число должно быть правдоподобным: сверяем
с числом активных детей по списку и с персоналом вчера — не запрещаем, а говорим.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.models import AuditLog, MealCount, Student, User
from app.services.purchases import site_orgs
from app.services.students import TEST_PIN_THRESHOLD

FIELDS = ("school", "sadik", "staff")
LABEL = {"school": "школа", "sadik": "садик", "staff": "персонал"}
LOW_SHARE = 0.7          # меньше 70 % списка — переспросить (эпидемия бывает, опечатка чаще)
STAFF_JUMP = 3           # персонал почти постоянен: скачок больше трёх — переспросить
EXAMPLE = "школа 310, садик 48, персонал 12. Завтрак: каша, чай. Обед: борщ, плов, компот"

_WORDS = {
    "school": r"школ\w*|ученик\w*|класс\w*",
    "sadik": r"сади?к\w*|детсад\w*|бала\s*бакч\w*",
    "staff": r"персонал\w*|сотрудник\w*|взросл\w*|работник\w*|учител\w*\s*и\s*персонал\w*",
}
_NUM = r"(\d{1,4})"


def parse(text: str, today: date | None = None) -> dict | None:
    """«школа 310, садик 48, персонал 12. Меню: борщ, плов» → числа, меню, дата.
    Число можно и перед словом («310 школа»). Нужно хотя бы два из трёх — иначе это
    не про едоков (одно «школа 18000» — про деньги). «вчера» — за вчера."""
    if not text:
        return None
    today = today or date.today()
    low = text.lower().replace("ё", "е")
    found = {}
    for key, words in _WORDS.items():
        nums = re.findall(rf"(?:{words})\s*[:\-–—]?\s*{_NUM}\b", low) or re.findall(rf"\b{_NUM}\s*(?:{words})", low)
        if nums:
            # Махабат 23.09: «школа 328, персонал 33, садик 97, персонал 10» — персонал
            # по объектам; едят все, поэтому складываем
            found[key] = sum(int(n) for n in nums) if key == "staff" else int(nums[0])
    if len(found) < 2:
        return None
    if any(v > 2000 for v in found.values()):
        return None   # тысячи — это деньги, не люди
    # Меню (владелец 23.09): что дали на завтрак и на обед. С первого «завтрак:/обед:/
    # полдник:» — до конца, с подписями; «меню:/готовят:» — просто список после двоеточия.
    menu = None
    m = re.search(r"(завтрак|обед|полдник|ужин|меню|готов\w*|блюда)\s*[:\-–—]\s*(.+)$", text, re.I | re.S)
    if m:
        body = m.group(0) if m.group(1).lower() in ("завтрак", "обед", "полдник", "ужин") else m.group(2)
        menu = body.strip().strip(".").strip() or None
    d = today - timedelta(days=1) if re.search(r"\bвчера\b", low) else today
    return {**found, "menu": menu, "date": d}


def roster(db: Session, site_org_id: int) -> dict:
    """Сколько детей по списку: школа и садик площадки (активные, без тестовых PIN)."""
    out = {"school": 0, "sadik": 0}
    for o in site_orgs(db, site_org_id):
        key = "school" if o.type == "school" else "sadik" if o.type == "kindergarten" else None
        if key is None:
            continue
        pins = [p for (p,) in db.query(Student.pin).filter(Student.organization_id == o.id, Student.status == "active").all()]
        out[key] += sum(1 for p in pins if not (p and p.isdigit() and int(p) >= TEST_PIN_THRESHOLD))
    return out


def get(db: Session, site_org_id: int, d: date) -> MealCount | None:
    return db.query(MealCount).filter(MealCount.site_org_id == site_org_id, MealCount.date == d).first()


def doubts(db: Session, site_org_id: int, values: dict, d: date) -> list[str]:
    """Что в числах странно, словами. Пусто — всё правдоподобно.
    Больше, чем по списку, — всегда вопрос. Меньше — сравниваем с тем, что сами писали
    последние дни (обычная посещаемость садика может быть 60 % списка, и спрашивать об
    этом каждый день — докучать); по списку — только пока своей истории нет."""
    out = []
    r = roster(db, site_org_id)
    recent = (db.query(MealCount).filter(MealCount.site_org_id == site_org_id, MealCount.date < d,
                                         MealCount.date >= d - timedelta(days=14))
              .order_by(MealCount.date.desc()).limit(5).all())
    for key in FIELDS:
        v = values.get(key)
        if v is None:
            continue
        total = r.get(key)
        if total and v > total:
            out.append(f"{LABEL[key]}: {v}, а по списку {total}")
            continue
        hist = sorted(getattr(m, key) for m in recent if getattr(m, key) is not None)
        if len(hist) >= 2:
            usual = hist[len(hist) // 2]
            if key == "staff" and abs(v - usual) > STAFF_JUMP:
                out.append(f"персонал: {v}, обычно {usual}")
            elif key != "staff" and usual and abs(v - usual) > usual * (1 - LOW_SHARE):
                out.append(f"{LABEL[key]}: {v}, обычно около {usual}")
        elif total and key != "staff" and v < total * LOW_SHARE:
            out.append(f"{LABEL[key]}: {v} из {total} по списку")
    return out


def record(db: Session, *, site_org_id: int, d: date, values: dict, menu: str | None, user: User | None,
           source: str) -> MealCount:
    """Записать или поправить день. Пустое поле при правке не стирает прежнее число."""
    row = get(db, site_org_id, d)
    old = {k: getattr(row, k) for k in (*FIELDS, "menu")} if row else None
    if row is None:
        row = MealCount(site_org_id=site_org_id, date=d, source=source, created_by=user.id if user else None)
        db.add(row)
    for k in FIELDS:
        if values.get(k) is not None:
            setattr(row, k, int(values[k]))
    if menu:
        row.menu = menu[:500]
    row.updated_by = user.id if user else None
    db.flush()
    db.add(AuditLog(entity_type="meal_count", entity_id=row.id, action="update" if old else "insert",
                    user_id=user.id if user else None, old_data=old,
                    new_data={**{k: getattr(row, k) for k in (*FIELDS, "menu")}, "date": d.isoformat(), "source": source}))
    return row


def text(row: MealCount) -> str:
    return ", ".join(f"{LABEL[k]} {getattr(row, k)}" for k in FIELDS if getattr(row, k) is not None)


def expected_today(db: Session, site_org_id: int, d: date | None = None) -> bool:
    from app.services import rules
    d = d or date.today()
    return d.weekday() in rules.kitchen_weekdays(db)


def missing_today(db: Session, site_org_id: int) -> bool:
    return expected_today(db, site_org_id) and get(db, site_org_id, date.today()) is None
