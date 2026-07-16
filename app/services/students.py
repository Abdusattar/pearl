from datetime import date, timedelta

from sqlalchemy.orm import Session
from app.models import Student, Enrollment

PIN_DIGITS = 4
ARCHIVE_AFTER_DAYS = 365
# PIN 9001/9002 — постоянные фиктивные аккаунты для тестов банка Optima (не реальные
# дети, см. wiki/payments/optima.md), не должны сдвигать счётчик реальных детей —
# иначе первый же новый ребёнок получил бы 9003 вместо 0097.
TEST_PIN_THRESHOLD = 9000


def get_next_free_pin(db: Session) -> str:
    """Следующий PIN — монотонно возрастает, никогда не переиспользуется
    (даже после того, как ребёнок выбыл), чтобы исключить перепутывание
    платежей на стороне Optima. Тестовые PIN банка (>= TEST_PIN_THRESHOLD)
    из подсчёта исключены — это отдельный зарезервированный диапазон."""
    pins = [
        int(p) for (p,) in db.query(Student.pin).all()
        if p.isdigit() and int(p) < TEST_PIN_THRESHOLD
    ]
    n = (max(pins) if pins else 0) + 1
    limit = 10 ** PIN_DIGITS - 1
    if n > limit:
        raise ValueError(f"Все PIN 0001-{limit} заняты")
    return f"{n:0{PIN_DIGITS}d}"


def get_student_by_pin(db: Session, pin: str) -> Student | None:
    return db.query(Student).filter(Student.pin == pin, Student.status == "active").first()


def compose_name(last_name: str, first_name: str, patronymic: str | None) -> str:
    """Фамилия Имя [Отчество] — порядок как исторически принят в системе."""
    parts = [last_name.strip(), first_name.strip()]
    if patronymic and patronymic.strip():
        parts.append(patronymic.strip())
    return " ".join(p for p in parts if p)


def update_student(
    db: Session,
    student_id: int,
    last_name: str,
    first_name: str,
    patronymic: str,
    group_id: int | None,
    status: str | None = None,
    parent_name: str | None = None,
    parent_contact: str | None = None,
) -> Student:
    """Правка ФИО, группы, статуса и данных родителя. Смена группы закрывает
    текущий Enrollment и открывает новый — история переводов между группами
    сохраняется. Статус меняется в обе стороны (Активен ↔ Выбыл); PIN при этом
    не меняется — навсегда закреплён за ребёнком. При переводе в «Выбыл» текущая
    группа закрывается, новая не открывается."""
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise ValueError(f"Студент {student_id} не найден")
    student.last_name = last_name.strip()
    student.first_name = first_name.strip()
    student.patronymic = (patronymic or "").strip() or None
    student.name = compose_name(last_name, first_name, patronymic)
    student.parent_name = (parent_name or "").strip() or None
    student.parent_contact = (parent_contact or "").strip() or None
    if status in ("active", "inactive"):
        student.status = status

    current = db.query(Enrollment).filter(
        Enrollment.student_id == student_id, Enrollment.end_date.is_(None)
    ).first()

    if student.status != "active":
        if current:
            current.end_date = date.today()
    elif group_id and (not current or current.group_id != group_id):
        if current:
            current.end_date = date.today()
        db.add(Enrollment(student_id=student_id, group_id=group_id, start_date=date.today()))
    elif not group_id and current:
        current.end_date = date.today()

    db.flush()
    return student


def set_first_enrollment_start(db: Session, student_id: int, new_date: date) -> bool:
    """Правит дату САМОГО РАННЕГО зачисления ребёнка (историческая дата поступления,
    для бэкфилла реальных дат вместо технической даты миграции) — не создаёт новую
    запись, правит существующую. Важно: billing._proration_factor() читает именно
    MIN(Enrollment.start_date) — эта функция обязана целиться в ту же строку, иначе
    расхождение между тем, что поправили, и тем, что реально влияет на начисление."""
    earliest = (
        db.query(Enrollment)
        .filter(Enrollment.student_id == student_id)
        .order_by(Enrollment.start_date.asc(), Enrollment.id.asc())
        .first()
    )
    if not earliest:
        return False
    earliest.start_date = new_date
    return True


def archive_stale_students(db: Session) -> int:
    """Через год после перевода в 'выбыл' — переводим в архив.
    Данные и транзакции не удаляются (на transactions есть FK), только
    скрываются из активных списков."""
    cutoff = date.today() - timedelta(days=ARCHIVE_AFTER_DAYS)
    stale = db.query(Student).filter(
        Student.status == "inactive",
        Student.updated_at <= cutoff,
    ).all()
    for s in stale:
        s.status = "archived"
    if stale:
        db.flush()
    return len(stale)
