from datetime import date, timedelta

from sqlalchemy.orm import Session
from app.models import Student, Enrollment

PIN_DIGITS = 4
ARCHIVE_AFTER_DAYS = 365


def get_next_free_pin(db: Session) -> str:
    """Следующий PIN — монотонно возрастает, никогда не переиспользуется
    (даже после того, как ребёнок выбыл), чтобы исключить перепутывание
    платежей на стороне Optima."""
    pins = [int(p) for (p,) in db.query(Student.pin).all() if p.isdigit()]
    n = (max(pins) if pins else 0) + 1
    limit = 10 ** PIN_DIGITS - 1
    if n > limit:
        raise ValueError(f"Все PIN 0001-{limit} заняты")
    return f"{n:0{PIN_DIGITS}d}"


def get_student_by_pin(db: Session, pin: str) -> Student | None:
    return db.query(Student).filter(Student.pin == pin, Student.status == "active").first()


def deactivate_student(db: Session, student_id: int) -> Student:
    """Перевод в статус выбыл. PIN не переиспользуется — навсегда закреплён за ребёнком."""
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise ValueError(f"Студент {student_id} не найден")
    student.status = "inactive"
    db.query(Enrollment).filter(
        Enrollment.student_id == student_id, Enrollment.end_date.is_(None)
    ).update({"end_date": date.today()})
    db.flush()
    return student


def update_student(db: Session, student_id: int, name: str, group_id: int | None) -> Student:
    """Правка ФИО и/или группы. Смена группы закрывает текущий Enrollment
    и открывает новый — история переводов между группами сохраняется."""
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise ValueError(f"Студент {student_id} не найден")
    student.name = name.strip()

    current = db.query(Enrollment).filter(
        Enrollment.student_id == student_id, Enrollment.end_date.is_(None)
    ).first()
    if group_id and (not current or current.group_id != group_id):
        if current:
            current.end_date = date.today()
        db.add(Enrollment(student_id=student_id, group_id=group_id, start_date=date.today()))
    elif not group_id and current:
        current.end_date = date.today()

    db.flush()
    return student


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
